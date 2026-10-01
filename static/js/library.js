// Медиатека: виртуальный список треков, выбор, панель действий, меню трека.

import { api } from './api.js';
import {
  $, artStyle, confirmDialog, count, esc, fmtAdded, highlight, hydrateIcons, icon, openMenu, toast,
} from './dom.js';
import {
  LIVE_LABEL, STATUS_LABEL, TASK_TITLES, busy, emit, filterById, on, scAuthor, state, visibleTracks,
} from './store.js';
import * as player from './player.js';
import { refreshData, runTask } from './tasks.js';

const ROW = 52;
const OVERSCAN = 8;
let list, sizer, empty, selbar, chkAll;
let frame = 0;

export function init() {
  list = $('#list');
  sizer = $('#list-sizer');
  empty = $('#list-empty');
  selbar = $('#selbar');
  chkAll = $('#chk-all');

  list.addEventListener('scroll', schedule, { passive: true });
  new ResizeObserver(schedule).observe(list);
  // Выбор — на нажатие кнопки мыши (как в Finder), а не на click: строки часто перерисовываются
  // (прогресс, фокус), и если строку заменить между mousedown и mouseup, click не наступит.
  list.addEventListener('mousedown', onListPointer);
  // Галочку ведём сами (по состоянию выбора) — нативное переключение отключаем.
  list.addEventListener('click', e => { if (e.target.closest('label.chk')) e.preventDefault(); });
  list.addEventListener('contextmenu', onContextMenu);
  // Курсор на первую строку — только при фокусе с клавиатуры (Tab), не при клике мышью.
  list.addEventListener('focus', () => { if (!state.cursor && list.matches(':focus-visible')) moveCursor(0); });
  chkAll.addEventListener('change', () => toggleAllVisible(chkAll.checked));
  $('#btn-pick').addEventListener('click', e => openPickMenu(e.currentTarget));
  selbar.addEventListener('click', onSelbarClick);

  on('tracks', schedule);
  on('track', id => updateRow(id));
  on('selection', schedule);
  on('view', schedule);
  on('task', schedule);
  on('player', schedule);  // перерисовать и прошлую, и новую играющую строку
  schedule();
}

export function schedule() {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
}

// ── Рендер ──

function render() {
  if (state.view !== 'library') return;
  const items = visibleTracks();
  sizer.style.height = `${items.length * ROW}px`;
  const top = list.scrollTop;
  const h = list.clientHeight || 600;
  const first = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const last = Math.min(items.length, Math.ceil((top + h) / ROW) + OVERSCAN);
  let html = '';
  for (let i = first; i < last; i++) html += rowHtml(items[i], i);
  sizer.innerHTML = html;

  renderEmpty(items);
  renderHeader(items);
  renderSelbar();
}

function rowHtml(t, i) {
  const sel = state.selected.has(t.id);
  const live = state.live.get(t.id);
  const playing = player.currentId() === t.id;
  const q = state.query;
  const artist = t.artist
    ? `<div class="artist">${highlight(t.artist, q)}</div>`
    : `<div class="artist guess" title="Исполнитель появится после скачивания">@${highlight(scAuthor(t), q)}</div>`;
  const cls = ['row', sel && 'sel', state.cursor === t.id && 'cursor', playing && 'playing'].filter(Boolean).join(' ');
  return `<div class="${cls}" style="top:${i * ROW}px" data-id="${esc(t.id)}" role="option" aria-selected="${sel}">
    <label class="cell chk"><input type="checkbox" tabindex="-1" ${sel ? 'checked' : ''} aria-label="Выбрать"></label>
    <div class="cell art" style="${artStyle(t.artist || t.title)}">${playing ? eqHtml() : icon('music')}
      <button class="play-over ${t.has_file ? 'can' : ''}" type="button" tabindex="-1" data-act="play" aria-label="Слушать">${icon(playing && player.isPlaying() ? 'pause' : 'play')}</button></div>
    <div class="cell meta"><div class="title">${highlight(t.title, q)}</div>${artist}</div>
    <div class="cell added">${esc(fmtAdded(t.added))}</div>
    <div class="cell status-cell">${statusHtml(t, live)}</div>
    <div class="cell row-actions">
      <button class="icon-btn" type="button" tabindex="-1" data-act="play" aria-label="Слушать" ${t.has_file ? '' : 'disabled'}>${icon('play')}</button>
      <button class="icon-btn" type="button" tabindex="-1" data-act="more" aria-label="Действия">${icon('more')}</button>
    </div>
  </div>`;
}

function eqHtml() {
  return `<span class="eq ${player.isPlaying() ? '' : 'paused'}"><i></i><i></i><i></i></span>`;
}

function statusHtml(t, live) {
  if (live) return `<span class="badge st-live"><span class="mini-spin"></span>${LIVE_LABEL[live]}</span>`;
  const title = t.status === 'unavailable' ? ` title="${esc(t.error)}"` : '';
  const badge = `<span class="badge st-${t.status}"${title}>${STATUS_LABEL[t.status] || t.status}</span>`;
  const err = t.error && t.status !== 'unavailable'
    ? `<span class="err-dot" title="Последняя ошибка: ${esc(t.error)}" aria-label="Ошибка: ${esc(t.error)}"></span>` : '';
  return badge + err;
}

/** Точечное обновление строки без перерисовки окна. Возвращает true, если строка видна. */
function updateRow(id) {
  if (!id || state.view !== 'library') return false;
  const el = sizer.querySelector(`.row[data-id="${CSS.escape(id)}"]`);
  const t = state.byId.get(id);
  if (!el || !t) { schedule(); return false; }
  const idx = Math.round(parseFloat(el.style.top) / ROW);
  const visible = visibleTracks();
  if (visible[idx]?.id !== id) { schedule(); return false; }
  el.outerHTML = rowHtml(t, idx);
  renderHeader(visible);
  renderSelbar();
  return true;
}

function renderEmpty(items) {
  let html = '';
  if (!state.tracks.length) {
    html = `${icon('heart')}<h3>Медиатека пуста</h3><p>Нажмите «Проверить лайки» — появятся треки, которые вы лайкнули на SoundCloud.</p>`;
  } else if (!items.length) {
    html = state.query
      ? `${icon('search')}<h3>Ничего не найдено</h3><p>По запросу «${esc(state.query)}» в разделе «${esc(filterById(state.filter).label)}» треков нет.</p>`
      : `${icon(filterById(state.filter).icon)}<h3>Здесь пусто</h3><p>В разделе «${esc(filterById(state.filter).label)}» сейчас нет треков.</p>`;
  }
  empty.innerHTML = html;
  empty.hidden = !html;
}

function renderHeader(items) {
  const n = items.length;
  const sel = n ? items.reduce((a, t) => a + (state.selected.has(t.id) ? 1 : 0), 0) : 0;
  chkAll.checked = n > 0 && sel === n;
  chkAll.indeterminate = sel > 0 && sel < n;
  chkAll.disabled = !n;
  if (state.view === 'library') {
    $('#view-subtitle').textContent = state.query
      ? `найдено ${n} из ${state.tracks.length}`
      : count(n, 'трек', 'трека', 'треков');
  }
}

function renderSelbar() {
  const sel = [...state.selected].map(id => state.byId.get(id)).filter(Boolean);
  selbar.hidden = !sel.length;
  if (!sel.length) return;
  const by = s => sel.filter(t => t.status === s).length;
  const withFile = sel.filter(t => t.has_file).length;
  const inYm = by('uploaded');
  const sentN = by('sent');
  const parts = [
    by('pending') && `новых ${by('pending')}`,
    by('downloaded') && `на диске ${by('downloaded')}`,
    sentN && `ждут ЯМ ${sentN}`,
    inYm && `в ЯМ ${inYm}`,
    by('unavailable') && `недоступных ${by('unavailable')}`,
    by('not_in_likes') && `пропали из лайков ${by('not_in_likes')}`,
  ].filter(Boolean);
  const hidden = sel.length - sel.filter(t => filterById(state.filter).test(t)).length;
  const running = busy();
  const why = running ? `Идёт «${TASK_TITLES[state.task.name] || 'задача'}» — действия станут доступны после неё` : '';
  $('#selbar-text').innerHTML = `<b>Выбрано ${sel.length}</b>${parts.length ? ` · ${parts.join(', ')}` : ''}` +
    (why ? `<span class="warn">${esc(why)}</span>` : '') +
    (hidden ? `<span class="warn">${hidden} не видно в этом разделе</span>` : '') +
    (inYm + sentN ? `<span class="warn">${inYm + sentN === 1 ? 'Трек уже' : `${inYm + sentN} уже`} отправлен${inYm + sentN === 1 ? '' : 'ы'} в ЯМ — повторная отправка создаст дубль</span>` : '');
  const b = act => selbar.querySelector(`[data-act="${act}"]`);
  const set = (act, disabled, title = '') => { b(act).disabled = disabled; b(act).title = disabled && why ? why : title; };
  set('download', running);
  set('download-upload', running);
  set('upload', running || !withFile, withFile ? '' : 'У выбранных треков нет файлов — сначала скачайте');
  set('mark', running || sel.length === inYm);
  set('unmark', running || !(inYm + sentN));
}

// ── Выбор ──

export function setSelection(ids, { replace = true } = {}) {
  if (replace) state.selected.clear();
  ids.forEach(id => state.selected.add(id));
  emit('selection');
}

function toggle(id, on = !state.selected.has(id)) {
  on ? state.selected.add(id) : state.selected.delete(id);
  state.anchor = id;
  emit('selection');
}

function selectRange(toId) {
  const items = visibleTracks();
  const a = items.findIndex(t => t.id === (state.anchor ?? toId));
  const b = items.findIndex(t => t.id === toId);
  if (a < 0 || b < 0) return toggle(toId);
  const [from, to] = a < b ? [a, b] : [b, a];
  for (let i = from; i <= to; i++) state.selected.add(items[i].id);
  emit('selection');
}

export function toggleAllVisible(on) {
  const items = visibleTracks();
  if (on === undefined) on = !items.every(t => state.selected.has(t.id));
  items.forEach(t => on ? state.selected.add(t.id) : state.selected.delete(t.id));
  emit('selection');
}

export function clearSelection() {
  if (!state.selected.size) return false;
  state.selected.clear();
  emit('selection');
  return true;
}

// ── Курсор (клавиатура) ──

export function moveCursor(delta, { extend = false, absolute = false } = {}) {
  const items = visibleTracks();
  if (!items.length) return;
  let i = items.findIndex(t => t.id === state.cursor);
  i = absolute ? delta : (i < 0 ? 0 : i + delta);
  i = Math.max(0, Math.min(items.length - 1, i));
  const id = items[i].id;
  if (extend) {
    if (!state.anchor) state.anchor = state.cursor || id;
    state.cursor = id;
    selectRange(id);
  } else {
    state.cursor = id;
  }
  scrollToIndex(i);
  schedule();
}

function scrollToIndex(i) {
  const top = i * ROW;
  if (top < list.scrollTop) list.scrollTop = top;
  else if (top + ROW > list.scrollTop + list.clientHeight) list.scrollTop = top + ROW - list.clientHeight;
}

export function toggleCursor() {
  if (state.cursor) toggle(state.cursor);
}

export const cursorTrack = () => state.byId.get(state.cursor);

export function focusList() { list.focus({ preventScroll: true }); }

// ── Мышь ──

function rowFromEvent(e) {
  const el = e.target.closest('.row');
  return el ? state.byId.get(el.dataset.id) : null;
}

function onListPointer(e) {
  if (e.button !== 0) return;  // правая кнопка — onContextMenu
  const t = rowFromEvent(e);
  if (!t) return;
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (act === 'play') { e.preventDefault(); player.toggleTrack(t.id); return; }
  if (act === 'more') {
    e.preventDefault();
    state.cursor = t.id;
    openTrackMenu(t, e.target.closest('button'));
    schedule();
    return;
  }
  e.preventDefault();  // без выделения текста при Shift-клике; фокус ставим сами
  state.cursor = t.id;
  if (e.target.closest('label.chk')) {
    if (e.shiftKey) selectRange(t.id); else toggle(t.id);
  } else if (e.detail === 2 && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
    if (t.has_file) player.play(t.id);  // двойной клик — слушать
  } else if (e.shiftKey) {
    selectRange(t.id);
  } else if (e.metaKey || e.ctrlKey) {
    toggle(t.id);
  } else {
    state.anchor = t.id;
    setSelection([t.id]);  // как в Finder: клик выделяет только эту строку
  }
  focusList();
}

function onContextMenu(e) {
  const t = rowFromEvent(e);
  if (!t) return;
  e.preventDefault();
  state.cursor = t.id;
  schedule();
  openTrackMenu(t, { x: e.clientX, y: e.clientY });
}

// ── Меню трека ──

export function openTrackMenu(t, at) {
  if (!t) return;
  if (!at) {
    const el = sizer.querySelector(`.row[data-id="${CSS.escape(t.id)}"] [data-act=more]`);
    at = el || { x: innerWidth / 2, y: innerHeight / 2 };
  }
  const running = busy();
  openMenu([
    { label: player.currentId() === t.id && player.isPlaying() ? 'Пауза' : 'Слушать', icon: 'play', disabled: !t.has_file, run: () => player.toggleTrack(t.id) },
    { label: 'Открыть на SoundCloud', icon: 'external', disabled: !t.sc_url, run: () => window.open(t.sc_url, '_blank', 'noopener') },
    { label: 'Показать в Finder', icon: 'folder', disabled: !t.has_file, run: () => api.reveal(t.id).catch(err => toast(err.message, { kind: 'error' })) },
    'sep',
    { label: t.has_file ? 'Скачано' : 'Скачать', icon: 'download', disabled: running || t.has_file, run: () => runTask('download', [t.id], false) },
    { label: 'Скачать и загрузить в ЯМ', icon: 'upload', disabled: running, run: () => actDownloadUpload([t]) },
    t.status === 'uploaded' || t.status === 'sent'
      ? { label: t.status === 'sent' ? 'Снять «Ждёт ЯМ»' : 'Снять отметку «В ЯМ»', icon: 'undo', disabled: running, run: () => actUnmark([t]) }
      : { label: 'Уже в Яндекс Музыке', icon: 'check', disabled: running, run: () => actMark([t]) },
  ], at);
}

function openPickMenu(anchor) {
  const ids = st => state.tracks.filter(t => t.status === st).map(t => t.id);
  const missing = (state.report?.missing || []).map(t => t.id).filter(id => state.byId.has(id));
  openMenu([
    { label: 'Все в этом списке', icon: 'check', run: () => toggleAllVisible(true) },
    'sep',
    { label: `Все новые (${ids('pending').length})`, icon: 'sparkle', disabled: !ids('pending').length, run: () => setSelection(ids('pending')) },
    { label: `Все на диске (${ids('downloaded').length})`, icon: 'disk', disabled: !ids('downloaded').length, run: () => setSelection(ids('downloaded')) },
    { label: state.report ? `Все ненайденные в ЯМ (${missing.length})` : 'Ненайденные в ЯМ — сначала сверка', icon: 'report', disabled: !missing.length, run: () => setSelection(missing) },
    'sep',
    { label: 'Снять выбор', icon: 'x', disabled: !state.selected.size, run: clearSelection },
  ], anchor);
}

// ── Действия над выбранными ──

const selectedTracks = () => [...state.selected].map(id => state.byId.get(id)).filter(Boolean);

function onSelbarClick(e) {
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (!act) return;
  const sel = selectedTracks();
  const ids = sel.map(t => t.id);
  if (act === 'clear') clearSelection();
  else if (act === 'download') runTask('download', ids, false);
  else if (act === 'download-upload') actDownloadUpload(sel);
  else if (act === 'upload') actUpload(sel);
  else if (act === 'mark') actMark(sel);
  else if (act === 'unmark') actUnmark(sel);
}

async function confirmDuplicates(tracks) {
  const inYm = tracks.filter(t => t.status === 'uploaded' || t.status === 'sent').length;
  if (!inYm) return true;
  return confirmDialog({
    title: 'Отправить повторно?',
    text: `${count(inYm, 'трек уже отправлен', 'трека уже отправлены', 'треков уже отправлены')} в ЯМ («В ЯМ» или «Ждёт ЯМ»). Повторная отправка создаст дубли в плейлисте.`,
    ok: 'Отправить', iconName: 'alert',
  });
}

async function actDownloadUpload(tracks) {
  if (await confirmDuplicates(tracks)) runTask('download', tracks.map(t => t.id), true);
}

async function actUpload(tracks) {
  const withFile = tracks.filter(t => t.has_file);
  if (await confirmDuplicates(withFile)) runTask('upload', withFile.map(t => t.id));
}

export async function actMark(tracks) {
  const ids = tracks.filter(t => t.status !== 'uploaded').map(t => t.id);
  if (!ids.length) return;
  if (ids.length > 1 && !await confirmDialog({
    title: `Отметить ${count(ids.length, 'трек', 'трека', 'треков')} «В ЯМ»?`,
    text: 'Они будут считаться загруженными и не попадут в следующие загрузки.',
    ok: 'Отметить', iconName: 'check',
  })) return;
  try {
    const r = await api.mark(ids);
    toast(`Отмечено «В ЯМ»: ${r.marked}`, { kind: 'ok' });
    clearSelection();
    await refreshData();
  } catch (err) { toast(err.message, { kind: 'error' }); }
}

export async function actUnmark(tracks) {
  const ids = tracks.filter(t => t.status === 'uploaded' || t.status === 'sent').map(t => t.id);
  if (!ids.length) return;
  if (!await confirmDialog({
    title: `Снять отметку «В ЯМ» с ${count(ids.length, 'трека', 'треков', 'треков')}?`,
    text: 'Треки с файлом станут «На диске», без файла — «Новыми». Их можно будет загрузить снова.',
    ok: 'Снять отметку', iconName: 'undo',
  })) return;
  try {
    const r = await api.unmark(ids);
    toast(`Отметка снята: ${r.unmarked}`, { kind: 'ok' });
    await refreshData();
  } catch (err) { toast(err.message, { kind: 'error' }); }
}

export function openFolder() {
  api.openFolder().catch(err => toast(err.message, { kind: 'error' }));
}

hydrateIcons();
