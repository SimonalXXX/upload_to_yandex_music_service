// История операций: последние запуски с итогами; раскрытие — журнал операции.

import { api } from './api.js';
import { $, esc, fmtDuration, fmtWhen, icon, toast } from './dom.js';
import { TASK_TITLES, on, state } from './store.js';
import { resultText } from './tasks.js';

const STATUS = {
  ok: ['Готово', 'check'], warn: ['С замечаниями', 'alert'], error: ['Ошибка', 'alert'], cancelled: ['Остановлено', 'stop'],
};
const TASK_ICON = { scan: 'heart', download: 'download', upload: 'upload', sync: 'sync' };
let items = [];
let open = null;          // id раскрытой записи
const details = new Map(); // id → полная запись с events
let root;

export function init() {
  root = $('#view-history');
  root.addEventListener('click', onClick);
  root.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('.hist-item')) { e.preventDefault(); onClick(e); }
  });
  on('view', () => { if (state.view === 'history') load(); });
  on('task-end', () => { if (state.view === 'history') load(); });
}

async function load() {
  try {
    items = (await api.history()).items;
  } catch (err) {
    toast(err.message, { kind: 'error' });
  }
  render();
}

function render() {
  if (state.view !== 'history') return;
  $('#view-subtitle').textContent = items.length ? `последние ${items.length}` : '';
  if (!items.length) {
    root.innerHTML = `<div class="page"><div class="empty" style="position:static">${icon('clock')}
      <h3>Пока пусто</h3><p>Здесь появятся проверки лайков, скачивания, загрузки и синхронизации — с итогами и журналом.</p></div></div>`;
    return;
  }
  root.innerHTML = `<div class="page"><div class="group">${items.map(rec => {
    const [label, ic] = STATUS[rec.status] || STATUS.ok;
    const dur = rec.finished_at ? fmtDuration(rec.finished_at - rec.started_at) : '';
    const text = resultText(rec.task, { status: rec.status, summary: rec.summary || {} });
    return `<div class="hist-item" data-id="${esc(rec.id)}" role="button" tabindex="0" aria-expanded="${open === rec.id}">
      <div class="ic ${esc(rec.status)}" title="${esc(label)}">${icon(TASK_ICON[rec.task] || ic)}</div>
      <div class="grow"><div><b>${esc(TASK_TITLES[rec.task] || rec.task)}</b>${rec.trigger === 'auto' ? '<span class="chip">авто</span>' : ''}</div>
        <div class="muted">${esc(text)}</div></div>
      <div class="when">${esc(fmtWhen(rec.started_at))}${dur ? `<br><span class="muted">${esc(dur)}</span>` : ''}</div>
    </div>${open === rec.id ? logHtml(rec.id) : ''}`;
  }).join('')}</div></div>`;
}

const timeFmt = new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

function describe(ev) {
  switch (ev.type) {
    case 'task_start': return ['', ev.trigger === 'auto' ? 'Запуск по расписанию' : 'Запуск'];
    case 'scan_complete': return ['ok', `Лайков: ${ev.total}, новых: ${ev.new_count}`];
    case 'sync_step': return ['', { scan: 'Проверка лайков', download: 'Скачивание', upload: 'Загрузка в ЯМ', verify: 'Сверка плейлиста' }[ev.step] || ev.step];
    case 'dl_start': return ['', `Скачивание: ${ev.total}`];
    case 'track_done': return ['ok', `✓ ${ev.title}`];
    case 'dl_complete': return [ev.failures ? 'warn' : 'ok', `Скачано: ${ev.downloaded}${ev.failures ? `, не удалось: ${ev.failures}` : ''}`];
    case 'upload_start': return ['', `Загрузка в ЯМ: ${ev.total}`];
    case 'track_uploaded': return ['ok', `↑ ${state.byId.get(ev.id)?.title || ev.id}`];
    case 'log': return [ev.level || '', ev.message];
    case 'all_done': return [ev.level || 'ok', ev.message || 'Готово'];
    case 'cancelled': return ['warn', 'Остановлено'];
    case 'error': return ['error', ev.message];
    case 'task_end': return ['', 'Конец'];
    default: return null;
  }
}

function logHtml(id) {
  const rec = details.get(id);
  if (!rec) return '<div class="hist-log muted">Загружаю журнал…</div>';
  const lines = (rec.events || []).map(ev => {
    const d = describe(ev);
    return d ? `<div class="log-line ${esc(d[0])}"><time>${timeFmt.format(ev.t * 1000)}</time><span>${esc(d[1])}</span></div>` : '';
  }).join('');
  return `<div class="hist-log log">${lines || '<div class="log-empty">Журнал пуст</div>'}</div>`;
}

async function onClick(e) {
  const row = e.target.closest('.hist-item');
  if (!row) return;
  const id = row.dataset.id;
  open = open === id ? null : id;
  render();
  if (open && !details.has(id)) {
    try { details.set(id, await api.historyItem(id)); } catch (err) { toast(err.message, { kind: 'error' }); }
    render();
  }
}
