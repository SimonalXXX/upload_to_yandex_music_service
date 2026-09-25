// Сверка с плейлистом ЯМ: итоги, «нет в плейлисте» (загрузить заново / снять отметку), дубли, в обработке.

import { api } from './api.js';
import { $, confirmDialog, count, esc, fmtWhen, icon, toast } from './dom.js';
import { actUnmark, setSelection } from './library.js';
import { on, emit, state } from './store.js';
import { runTask } from './tasks.js';

const picked = new Set();
let checking = false;
let root;

export function init() {
  root = $('#view-report');
  root.addEventListener('click', onClick);
  root.addEventListener('change', onChange);
  for (const t of ['report', 'view', 'task', 'settings', 'tracks']) on(t, render);
}

async function check() {
  if (checking) return;
  checking = true;
  render();
  try {
    state.report = await api.checkPlaylist();
    picked.clear();
    emit('report');
  } catch (err) {
    toast(err.message, { kind: 'error', title: 'Сверка не удалась' });
  } finally {
    checking = false;
    render();
  }
}

function render() {
  if (state.view !== 'report') return;
  const r = state.report;
  const playlist = state.settings?.yandex_music?.playlist_url;
  if (!playlist) {
    root.innerHTML = `<div class="page"><div class="empty" style="position:static">${icon('report')}
      <h3>Плейлист не задан</h3><p>Укажите ссылку на плейлист Яндекс Музыки в настройках — тогда здесь можно будет сверить, всё ли на месте.</p>
      <button class="btn primary" data-act="settings" type="button">Открыть настройки</button></div></div>`;
    return;
  }
  const btn = `<button class="btn primary" data-act="check" type="button" ${checking ? 'disabled' : ''}>
    ${checking ? '<span class="mini-spin"></span>Сверяю…' : `${icon('sync')}Сверить сейчас`}</button>`;
  if (!r) {
    root.innerHTML = `<div class="page"><div class="empty" style="position:static">${icon('report')}
      <h3>Сверка ещё не проводилась</h3><p>Сравним треки, отмеченные «В ЯМ», с тем, что реально лежит в плейлисте.</p>${btn}</div></div>`;
    return;
  }
  $('#view-subtitle').textContent = `сверено ${fmtWhen(r.checked_at)}`;
  const missing = r.missing.filter(t => state.byId.has(t.id)).map(t => state.byId.get(t.id));
  for (const id of [...picked]) if (!missing.some(t => t.id === id)) picked.delete(id);
  const running = !!state.task;

  root.innerHTML = `<div class="page">
    <div class="report-head">
      <div class="art" style="background:linear-gradient(135deg,#ffcc00,#fa2d48)">${icon('music')}</div>
      <div class="grow"><h3>${esc(r.playlist?.title || 'Плейлист')}</h3>
        <p>${count(r.playlist_total, 'трек', 'трека', 'треков')} в плейлисте · сверено ${esc(fmtWhen(r.checked_at))}</p></div>
      <a class="btn" href="${esc(playlist)}" target="_blank" rel="noopener">${icon('external')}Открыть</a>
      ${btn}
    </div>
    <div class="stats">
      ${stat(r.playlist_total, 'в плейлисте')}
      ${stat(r.found, 'найдено')}
      ${stat(r.missing_total, 'нет в плейлисте', r.missing_total)}
      ${stat(r.duplicates, 'дублей', r.duplicates)}
      ${stat(r.processing, 'в обработке')}
    </div>

    <h2>Нет в плейлисте · ${missing.length}</h2>
    <p class="lead">Отмечены «В ЯМ», но в плейлисте не найдены по исполнителю и названию. Бывает, что ЯМ показывает трек под другим названием — проверьте перед повторной загрузкой.</p>
    ${missing.length ? `<div class="group">
      <div class="group-tools">
        <label><input type="checkbox" data-act="all" ${picked.size && picked.size === missing.length ? 'checked' : ''}> Выбрать все</label>
        <span class="grow muted">${picked.size ? `выбрано ${picked.size}` : ''}</span>
        <button class="btn" data-act="show" type="button" ${picked.size ? '' : 'disabled'}>Показать в медиатеке</button>
        <button class="btn" data-act="unmark" type="button" ${picked.size && !running ? '' : 'disabled'}>Снять «В ЯМ»</button>
        <button class="btn primary" data-act="reupload" type="button" ${picked.size && !running ? '' : 'disabled'}>${icon('upload')}Загрузить заново</button>
      </div>
      <ul class="plain-list">${missing.map(t => `<li>
        <input type="checkbox" data-id="${esc(t.id)}" ${picked.has(t.id) ? 'checked' : ''} aria-label="Выбрать">
        <div class="grow"><div>${esc(t.title)}</div><div class="sub">${esc(t.artist || '—')}${t.has_file ? '' : ' · файла нет — будет скачан заново'}</div></div>
      </li>`).join('')}</ul></div>` : `<div class="group"><ul class="plain-list"><li class="muted">${icon('check')} Все треки «В ЯМ» на месте</li></ul></div>`}

    <h2>Дубли в плейлисте · ${r.duplicates_list.length}</h2>
    <p class="lead">Удалить лишние копии можно в самой Яндекс Музыке. Здесь — только список.</p>
    <div class="group"><ul class="plain-list">${r.duplicates_list.length
      ? r.duplicates_list.map(d => `<li><div class="grow"><div>${esc(d.title)}</div><div class="sub">${esc(d.artist)}</div></div><span class="num">× ${d.count}</span></li>`).join('')
      : `<li class="muted">${icon('check')} Дублей нет</li>`}</ul></div>

    <h2>В обработке · ${r.processing_list.length}</h2>
    <div class="group"><ul class="plain-list">${r.processing_list.length
      ? r.processing_list.map(d => `<li><div class="grow"><div>${esc(d.title)}</div><div class="sub">${esc(d.artist)}</div></div></li>`).join('')
      : '<li class="muted">Все треки плейлиста доступны для прослушивания</li>'}</ul></div>
  </div>`;
}

function stat(n, label, bad = 0) {
  return `<div class="stat${bad ? ' bad' : ''}"><b>${n}</b><span>${esc(label)}</span></div>`;
}

function onChange(e) {
  const cb = e.target;
  if (cb.dataset.act === 'all') {
    const ids = (state.report?.missing || []).map(t => t.id).filter(id => state.byId.has(id));
    picked.clear();
    if (cb.checked) ids.forEach(id => picked.add(id));
  } else if (cb.dataset.id) {
    cb.checked ? picked.add(cb.dataset.id) : picked.delete(cb.dataset.id);
  }
  render();
}

async function onClick(e) {
  const act = e.target.closest('button[data-act]')?.dataset.act;
  if (!act) return;
  const tracks = [...picked].map(id => state.byId.get(id)).filter(Boolean);
  if (act === 'check') check();
  else if (act === 'settings') location.hash = '#settings';
  else if (act === 'show') { setSelection(tracks.map(t => t.id)); location.hash = '#library/uploaded'; }
  else if (act === 'unmark') { await actUnmark(tracks); picked.clear(); render(); }
  else if (act === 'reupload') {
    const ok = await confirmDialog({
      title: `Загрузить заново ${count(tracks.length, 'трек', 'трека', 'треков')}?`,
      text: 'Треки без файла сначала скачаются. Если трек на самом деле есть в плейлисте под другим названием, появится дубль.',
      ok: 'Загрузить', iconName: 'upload',
    });
    if (ok && await runTask('download', tracks.map(t => t.id), true)) picked.clear();
  }
}
