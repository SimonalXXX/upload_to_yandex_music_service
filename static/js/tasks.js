// Задачи: запуск, обработка событий сервера (SSE), индикатор в панели инструментов, итоги.

import { api } from './api.js';
import { $, esc, toast } from './dom.js';
import { TASK_TITLES, emit, patchTrack, setTracks, state } from './store.js';

const STEP_TITLES = { scan: 'Проверка лайков', download: 'Скачивание', upload: 'Загрузка в ЯМ', verify: 'Сверка плейлиста' };
const RING = 50.27;  // длина окружности r=8

// ── Данные ──

export async function refreshData() {
  const [tracks, status] = await Promise.all([api.tracks(), api.status()]);
  setTracks(tracks.tracks);
  state.status = status;
  emit('status');
}

export async function refreshStatus() {
  state.status = await api.status();
  emit('status');
}

// ── Запуск ──

/** name: scan | sync | download | upload. */
export async function runTask(name, ids = null, upload = false) {
  if (state.task) { toast('Дождитесь окончания текущей задачи', { kind: 'warn' }); return false; }
  try {
    if (name === 'scan') await api.scan();
    else if (name === 'sync') await api.sync();
    else if (name === 'download') await api.download(ids, upload);
    else if (name === 'upload') await api.upload(ids);
    return true;
  } catch (err) {
    toast(err.message, { kind: err.status === 409 ? 'warn' : 'error' });
    return false;
  }
}

export async function stopTask() {
  if (!state.task) return;
  state.task.stopping = true;
  renderActivity();
  try { await api.cancel(); } catch { /* сервер ответит событием */ }
  // Страховка на случай потерянного task_end: опрашиваем статус.
  let tries = 0;
  const poll = async () => {
    if (!state.task) return;
    try {
      const s = await api.status();
      if (!s.active_task) { finishTask(null); return; }
      if (++tries % 5 === 0) await api.forceClear().catch(() => {});
    } catch { /* повторим */ }
    setTimeout(poll, 2000);
  };
  setTimeout(poll, 3000);
}

// ── События сервера ──

function log(message, level = '') {
  state.taskLog.push({ t: Date.now(), message, level });
  if (state.taskLog.length > 500) state.taskLog.shift();
  renderLog();
}

export function handleEvent(ev) {
  const t = state.task;
  switch (ev.type) {
    case 'hello':
      if (ev.active_task && !t) {
        startTask({ task: ev.active_task, trigger: ev.trigger });
        state.task.detail = 'выполняется';
      } else if (!ev.active_task && t) {
        finishTask(null);  // task_end потерялся, пока не было связи
      }
      break;
    case 'task_start':
      startTask(ev);
      break;
    case 'task_end':
      finishTask(ev);
      break;
    case 'scanning':
      setPhase('Проверка лайков', { indeterminate: true, detail: 'Подключение к SoundCloud…' });
      break;
    case 'scan_progress':
      if (t) { t.detail = `Получено ${ev.found}`; renderActivity(); }
      break;
    case 'scan_complete':
      log(`Лайков: ${ev.total}, новых: ${ev.new_count}` + (ev.not_in_likes ? `, пропали из лайков: ${ev.not_in_likes}` : ''), 'ok');
      api.tracks().then(r => setTracks(r.tracks)).catch(() => {});
      if (t?.name === 'scan') state.lastResult = { status: 'ok', summary: ev };
      break;
    case 'sync_step':
      setPhase(STEP_TITLES[ev.step] || ev.step, { indeterminate: true, detail: '' });
      break;
    case 'dl_start':
      setPhase('Скачивание', { total: ev.total, done: 0, detail: '' });
      break;
    case 'downloading':
      if (ev.id) { state.live.set(ev.id, ev.stage === 'convert' ? 'convert' : 'download'); emit('track', ev.id); }
      if (t) {
        t.done = ev.downloaded; t.total = ev.total;
        t.detail = `${ev.downloaded + 1} из ${ev.total} · ${ev.stage === 'convert' ? 'конвертация' : ev.title}`;
        renderActivity();
      }
      break;
    case 'track_done':
      state.live.delete(ev.id);
      patchTrack(ev.id, { status: ev.status, has_file: true, error: '' });
      if (t) { t.done = ev.downloaded; renderActivity(); }
      log(`✓ ${ev.title}`, 'ok');
      break;
    case 'track_status':
      state.live.delete(ev.id);
      patchTrack(ev.id, { status: ev.status, error: ev.error });
      break;
    case 'dl_complete':
      log(`Скачано: ${ev.downloaded}` + (ev.failures ? `, не удалось: ${ev.failures}` : ''), ev.failures ? 'warn' : 'ok');
      break;
    case 'upload_start':
      setPhase('Загрузка в ЯМ', { total: ev.total, done: 0, detail: '' });
      break;
    case 'uploading':
      if (ev.id) { state.live.set(ev.id, 'upload'); emit('track', ev.id); }
      if (t) { t.done = ev.uploaded; t.total = ev.total; t.detail = `${ev.uploaded + 1} из ${ev.total} · ${ev.title}`; renderActivity(); }
      break;
    case 'track_uploaded':
      state.live.delete(ev.id);
      patchTrack(ev.id, { status: 'uploaded', error: '' });
      if (t) { t.done = (t.done || 0) + 1; renderActivity(); }
      log(`↑ ${state.byId.get(ev.id)?.title || ev.id} — в ЯМ`, 'ok');
      break;
    case 'log':
      log(ev.message, ev.level);
      if (ev.id && ev.level === 'error') { state.live.delete(ev.id); emit('track', ev.id); }
      break;
    case 'all_done':
      state.lastResult = { status: ev.level || 'ok', summary: ev };
      if (ev.message) log(ev.message, ev.level || '');
      break;
    case 'cancelled':
      state.lastResult = { status: 'cancelled', summary: ev };
      log('Остановлено', 'warn');
      break;
    case 'error':
      state.lastResult = { status: 'error', summary: { message: ev.message } };
      log(ev.message, 'error');
      break;
    default:
      break;
  }
}

function startTask(ev) {
  state.task = {
    name: ev.task, trigger: ev.trigger || 'manual', phase: TASK_TITLES[ev.task] || ev.task,
    done: 0, total: 0, detail: '', indeterminate: true, started: Date.now(),
  };
  state.taskLog = [];
  state.lastResult = null;
  if (ev.trigger === 'auto') log('Автосинхронизация по расписанию');
  renderActivity();
  renderLog();
  emit('task');
}

function setPhase(phase, patch) {
  if (!state.task) return;
  Object.assign(state.task, { phase, indeterminate: false }, patch);
  renderActivity();
}

function finishTask(ev) {
  const task = state.task;
  state.task = null;
  state.live.clear();
  renderActivity();
  emit('task');
  if (task) {
    const r = state.lastResult || (ev ? { status: ev.status, summary: {} } : null);
    if (r) showResult(task, r, ev?.history_id);
  }
  refreshData().catch(() => {});
  if (task && (task.name === 'sync' || task.name === 'upload' || task.name === 'download')) {
    api.lastReport().then(r => { if (r.report) { state.report = r.report; emit('report'); } }).catch(() => {});
  }
  emit('task-end', ev);
}

// ── Итог задачи ──

export function resultText(name, r) {
  const s = r.summary || {};
  const parts = [];
  if (s.new_count !== undefined && (name === 'scan' || name === 'sync')) {
    parts.push(s.new_count ? `новых лайков: ${s.new_count}` : 'новых лайков нет');
  }
  if (s.downloaded) parts.push(`скачано: ${s.downloaded}`);
  if (s.uploaded) parts.push(`в ЯМ: ${s.uploaded}`);
  if (s.failures) parts.push(`не скачано: ${s.failures}`);
  if (s.upload_errors) parts.push(`ошибок загрузки: ${s.upload_errors}`);
  if (s.missing) parts.push(`не найдено в плейлисте: ${s.missing}`);
  if (s.not_in_likes) parts.push(`пропали из лайков: ${s.not_in_likes}`);
  if (r.status === 'error' && !parts.length) return s.message || 'Ошибка';
  let text = parts.join(', ');
  if (s.message && r.status !== 'cancelled') text += (text ? '. ' : '') + s.message;
  if (r.status === 'cancelled') return 'Остановлено' + (text ? ` — ${text}` : '');
  return text ? text[0].toUpperCase() + text.slice(1) : 'Готово';
}

function showResult(task, r) {
  const kind = { ok: 'ok', warn: 'warn', error: 'error', cancelled: 'info' }[r.status] || 'info';
  const title = `${TASK_TITLES[task.name] || 'Задача'}${task.trigger === 'auto' ? ' (авто)' : ''}`;
  const failed = (r.summary?.failures || 0) + (r.summary?.upload_errors || 0);
  toast(resultText(task.name, r), {
    kind, title,
    action: failed || r.status === 'error'
      ? { label: 'Журнал', run: () => { location.hash = '#history'; } } : null,
  });
}

// ── Индикатор в панели инструментов ──

export function initActivity() {
  $('#btn-stop').addEventListener('click', stopTask);
  const main = $('#activity-main');
  const pop = $('#activity-pop');
  main.addEventListener('click', () => {
    pop.hidden = !pop.hidden;
    main.setAttribute('aria-expanded', String(!pop.hidden));
    if (!pop.hidden) renderLog();
  });
  document.addEventListener('mousedown', e => {
    if (!pop.hidden && !$('#activity').contains(e.target)) { pop.hidden = true; main.setAttribute('aria-expanded', 'false'); }
  });
  $('#btn-scan').addEventListener('click', () => runTask('scan'));
  $('#btn-sync').addEventListener('click', () => runTask('sync'));
}

function renderActivity() {
  const t = state.task;
  const box = $('#activity');
  box.hidden = !t;
  $('#btn-scan').disabled = !!t;
  $('#btn-sync').disabled = !!t;
  if (!t) {
    document.title = 'Музыкальный';
    $('#activity-pop').hidden = true;
    return;
  }
  const ring = $('.ring', box);
  const fg = $('#ring-fg');
  const pct = !t.indeterminate && t.total ? Math.min(100, Math.round((t.done / t.total) * 100)) : null;
  ring.classList.toggle('spin', pct === null);
  fg.style.strokeDashoffset = pct === null ? '' : String(RING * (1 - pct / 100));
  $('#act-phase').textContent = t.stopping ? 'Останавливаю…' : t.phase;
  $('#act-detail').textContent = t.stopping ? 'дожидаюсь конца текущего шага' : (t.detail || (t.trigger === 'auto' ? 'по расписанию' : ''));
  $('#btn-stop').disabled = !!t.stopping;
  document.title = `${pct !== null ? `${pct}% · ` : ''}${t.phase} — Музыкальный`;
}

const timeFmt = new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

function renderLog() {
  const pop = $('#activity-pop');
  if (pop.hidden) return;
  const el = $('#activity-log');
  el.innerHTML = state.taskLog.length
    ? state.taskLog.map(l => `<div class="log-line ${esc(l.level)}"><time>${timeFmt.format(l.t)}</time><span>${esc(l.message)}</span></div>`).join('')
    : '<div class="log-empty">Пока пусто</div>';
  el.scrollTop = el.scrollHeight;
}

