// Медиатека: виртуальный список треков, выбор, панель действий, меню трека.

import { api } from './api.js';
import {
  $, artStyle, confirmDialog, count, esc, fmtAdded, fmtWhen, highlight, hydrateIcons, icon, openMenu, toast,
} from './dom.js';
import {
  LIVE_LABEL, TASK_TITLES, YM_CLAIMED, busy, emit, filterById, inYm, isNew, isReady, on, scAuthor, state,
  trackBadges, visibleTracks,
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
  // Несколько бейджей: Яндекс Музыка, диск, SoundCloud (specs/features/track-states.md).
  const badges = trackBadges(t, fmtWhen).map(b =>
    `<span class="badge b-${b.cls}" title="${esc(b.title)}">${esc(b.text)}</span>`).join('');
  const err = t.error && t.ym !== 'rejected'
    ? `<span class="err-dot" title="Последняя ошибка: ${esc(t.error)}" aria-label="Ошибка: ${esc(t.error)}"></span>` : '';
  return badges + err;
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
  const n = f => sel.filter(f).length;
  const withFile = n(t => t.has_file);
  const dupN = n(t => YM_CLAIMED.has(t.ym));
  const parts = [
    [n(isNew), 'новых'], [withFile, 'на диске'], [n(inYm), 'в ЯМ'],
    [n(t => t.ym === 'sent'), 'ждут ЯМ'], [n(t => t.ym === 'manual'), 'не проверены'],
    [n(t => t.ym === 'missing'), 'нет в плейлисте'], [n(t => t.ym === 'rejected'), 'не приняты ЯМ'],
    [n(t => t.sc === 'unavailable'), 'недоступны'],
  ].filter(([k]) => k).map(([k, label]) => `${label} ${k}`);
  const hidden = sel.length - sel.filter(t => filterById(state.filter).test(t)).length;
  const running = busy();
  const why = running ? `Идёт «${TASK_TITLES[state.task.name] || 'задача'}» — действия станут доступны после неё` : '';
  $('#selbar-text').innerHTML = `<b>Выбрано ${sel.length}</b>${parts.length ? ` · ${parts.join(', ')}` : ''}` +
    (why ? `<span class="warn">${esc(why)}</span>` : '') +
    (hidden ? `<span class="warn">${hidden} не видно в этом разделе</span>` : '') +
    (dupN ? `<span class="warn">${dupN === 1 ? 'Трек уже' : `${dupN} уже`} в ЯМ или отправлен${dupN === 1 ? '' : 'ы'} — повторная отправка создаст дубль</span>` : '');
  const b = act => selbar.querySelector(`[data-act="${act}"]`);
  const set = (act, disabled, title = '') => { b(act).disabled = disabled; b(act).title = disabled && why ? why : title; };
  set('download', running);
  set('download-upload', running);
  set('upload', running || !withFile, withFile ? '' : 'У выбранных треков нет файлов — сначала скачайте');
  set('mark', running || !n(t => ['none', 'sent', 'missing', 'rejected'].includes(t.ym)));
  set('unmark', running || !n(t => t.ym !== 'none'));
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
    { label: t.ym === 'missing' || t.ym === 'rejected' ? 'Отправить в ЯМ заново' : 'Скачать и загрузить в ЯМ',
      icon: 'upload', disabled: running, run: () => actDownloadUpload([t]) },
    ...(t.ym === 'missing' || t.ym === 'rejected'
      ? [{ label: 'Считать «В ЯМ» (сверка ошиблась)', icon: 'check', disabled: running, run: () => actMark([t]) }]
      : t.ym === 'none'
        ? [{ label: 'Уже в Яндекс Музыке', icon: 'check', disabled: running, run: () => actMark([t]) }]
        : []),
    ...(t.ym !== 'none' ? [{ label: 'Снять «В ЯМ»', icon: 'undo', disabled: running, run: () => actUnmark([t]) }] : []),
  ], at);
}

function openPickMenu(anchor) {
  const pick = (label, iconName, f) => {
    const ids = state.tracks.filter(f).map(t => t.id);
    return { label: `${label} (${ids.length})`, icon: iconName, disabled: !ids.length, run: () => setSelection(ids) };
  };
  openMenu([
    { label: 'Все в этом списке', icon: 'check', run: () => toggleAllVisible(true) },
    'sep',
    pick('Все новые', 'sparkle', isNew),
    pick('Все готовые к отправке', 'upload', isReady),
    pick('Все «нет в плейлисте»', 'alert', t => t.ym === 'missing'),
    pick('Все «не приняты ЯМ»', 'x', t => t.ym === 'rejected'),
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
  const dup = tracks.filter(t => YM_CLAIMED.has(t.ym)).length;
  if (!dup) return true;
  return confirmDialog({
    title: 'Отправить повторно?',
    text: `${count(dup, 'трек уже', 'трека уже', 'треков уже')} в ЯМ или отправлен${dup === 1 ? '' : 'ы'} («В ЯМ», «Ждёт ЯМ», «Не проверен»). Повторная отправка создаст дубли в плейлисте.`,
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
  const target = tracks.filter(t => ['none', 'sent', 'missing', 'rejected'].includes(t.ym));
  if (!target.length) return;
  const overrule = target.filter(t => t.ym === 'missing' || t.ym === 'rejected').length;
  if ((target.length > 1 || overrule) && !await confirmDialog({
    title: `Отметить ${count(target.length, 'трек', 'трека', 'треков')} «В ЯМ»?`,
    text: 'Они не попадут в следующие загрузки.' + (overrule
      ? `\n${count(overrule, 'трек', 'трека', 'треков')} сверка не нашла в плейлисте — после отметки они будут считаться «В ЯМ» по вашему подтверждению, и сверка перестанет их помечать.`
      : '\nПри ближайшей сверке приложение проверит, что они действительно в плейлисте.'),
    ok: 'Отметить', iconName: 'check',
  })) return;
  const ids = target.map(t => t.id);
  try {
    const r = await api.mark(ids);
    toast(`Отмечено «В ЯМ»: ${r.marked}`, { kind: 'ok' });
    clearSelection();
    await refreshData();
  } catch (err) { toast(err.message, { kind: 'error' }); }
}

export async function actUnmark(tracks) {
  const ids = tracks.filter(t => t.ym !== 'none').map(t => t.id);
  if (!ids.length) return;
  if (!await confirmDialog({
    title: `Снять «В ЯМ» с ${count(ids.length, 'трека', 'треков', 'треков')}?`,
    text: 'Треки станут «не отправлены» — их можно будет загрузить снова. Файлы на диске не меняются.',
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
