// Точка входа: загрузка данных, маршруты (#library/<раздел>, #report, #history, #settings),
// горячие клавиши, поток событий сервера.

import { api, connectEvents } from './api.js';
import { $, closeMenu, hydrateIcons, menuOpen, toast } from './dom.js';
import * as historyView from './history.js';
import * as library from './library.js';
import * as player from './player.js';
import * as report from './report.js';
import * as settings from './settings.js';
import * as sidebar from './sidebar.js';
import { FILTERS, emit, filterById, setTracks, state } from './store.js';
import { handleEvent, initActivity, runTask, stopTask } from './tasks.js';

const VIEWS = ['library', 'report', 'history', 'settings'];
const TITLES = { report: 'Сверка с Яндекс Музыкой', history: 'История', settings: 'Настройки' };
const standalone = matchMedia('(display-mode: standalone)').matches;

function load(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* приватный режим */ }
}

// ── Маршруты ──

let lastHash = '';

async function route() {
  const [view, sub] = (location.hash.slice(1) || `library/${load('filter', 'all')}`).split('/');
  const next = VIEWS.includes(view) ? view : 'library';
  if (state.view === 'settings' && next !== 'settings' && !(await settings.confirmLeave())) {
    window.history.replaceState(null, '', lastHash || '#library/all');
    return;
  }
  lastHash = location.hash;
  state.view = next;
  if (next === 'library') {
    state.filter = FILTERS.some(f => f.id === sub) ? sub : 'all';
    save('filter', state.filter);
  }
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== next;
  document.querySelectorAll('.lib-only').forEach(el => { el.hidden = next !== 'library'; });
  $('#view-title').textContent = next === 'library' ? filterById(state.filter).label : TITLES[next];
  if (next !== 'library') $('#view-subtitle').textContent = '';
  if (next === 'library') $('#list').scrollTop = 0;
  emit('view');
}

// ── Горячие клавиши (specs/features/ui-redesign.md, 3.8) ──

function onKey(e) {
  if (e.defaultPrevented || $('#dialog').open || menuOpen()) return;
  const mod = e.metaKey || e.ctrlKey;
  const key = e.key.toLowerCase();
  const inField = e.target.closest?.('input, textarea, select, [contenteditable]');
  if (!$('#onboarding').hidden) return;

  if (mod && key === 'f') {
    e.preventDefault();
    if (state.view !== 'library') location.hash = `#library/${state.filter}`;
    setTimeout(() => { $('#search').focus(); $('#search').select(); }, 0);
    return;
  }
  if (mod && e.key === '.') { e.preventDefault(); stopTask(); return; }
  if (mod && key === 'r' && standalone) { e.preventDefault(); runTask('sync'); return; }

  if (inField) {
    if (e.key === 'Escape') { e.target.blur(); library.focusList(); }
    else if (e.key === 'ArrowDown' && e.target.id === 'search') { e.preventDefault(); library.focusList(); library.moveCursor(0, { absolute: true }); }
    return;
  }
  if (e.key === 'Escape') {
    const pop = $('#activity-pop');
    if (!pop.hidden) { pop.hidden = true; return; }
    library.clearSelection();
    return;
  }
  if (state.view !== 'library') return;
  if (mod && key === 'a') { e.preventDefault(); library.toggleAllVisible(true); return; }
  if (mod) return;
  switch (e.key) {
    case 'ArrowDown': e.preventDefault(); library.moveCursor(1, { extend: e.shiftKey }); break;
    case 'ArrowUp': e.preventDefault(); library.moveCursor(-1, { extend: e.shiftKey }); break;
    case 'PageDown': e.preventDefault(); library.moveCursor(10, { extend: e.shiftKey }); break;
    case 'PageUp': e.preventDefault(); library.moveCursor(-10, { extend: e.shiftKey }); break;
    case 'Home': e.preventDefault(); library.moveCursor(0, { absolute: true }); break;
    case 'End': e.preventDefault(); library.moveCursor(1e9, { absolute: true }); break;
    case 'Enter': case 'x': case 'X': case 'ч': case 'Ч':
      e.preventDefault(); library.toggleCursor(); break;
    case ' ': {
      e.preventDefault();
      const t = library.cursorTrack();
      if (t?.has_file) player.toggleTrack(t.id); else player.toggle();
      break;
    }
    case 'ContextMenu': e.preventDefault(); library.openTrackMenu(library.cursorTrack()); break;
    case 'F10': if (e.shiftKey) { e.preventDefault(); library.openTrackMenu(library.cursorTrack()); } break;
    default: break;
  }
}

// ── Запуск ──

async function checkAuth(refresh = false) {
  try {
    state.auth = await api.auth(refresh);
  } catch (err) {
    state.auth = { authorized: false, error: err.message };
  }
  emit('auth');
  return state.auth;
}
settings.setAuthChecker(checkAuth);

async function boot() {
  hydrateIcons();
  initActivity();
  player.init();
  library.init();
  sidebar.init();
  report.init();
  historyView.init();
  settings.init();

  const search = $('#search');
  search.addEventListener('input', () => {
    state.query = search.value.trim().toLowerCase();
    $('#list').scrollTop = 0;
    emit('tracks');
  });
  const sort = $('#sort');
  state.sort = sort.value = load('sort', 'added');
  sort.addEventListener('change', () => { state.sort = sort.value; save('sort', sort.value); emit('tracks'); });

  addEventListener('hashchange', route);
  document.addEventListener('keydown', onKey);
  document.addEventListener('scroll', closeMenu, true);
  await route();

  try {
    const [status, tracks, conf] = await Promise.all([api.status(), api.tracks(), api.settings()]);
    state.status = status;
    state.settings = conf;
    setTracks(tracks.tracks);
    emit('status');
    emit('settings');
  } catch (err) {
    toast(`Не удалось загрузить данные: ${err.message}`, { kind: 'error', title: 'Сервер недоступен' });
  }
  if (state.status && !state.status.configured) settings.openOnboarding();

  api.lastReport().then(r => { state.report = r.report; emit('report'); }).catch(() => {});
  checkAuth();
  connectEvents(handleEvent);
  library.focusList();
}

boot();
