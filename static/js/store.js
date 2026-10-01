// Состояние приложения + простая шина событий. Представления подписываются на темы.

export const state = {
  tracks: [],            // из /api/tracks, новые сверху
  byId: new Map(),
  status: null,          // /api/status
  settings: null,        // /api/settings
  auth: null,            // /api/check-yandex
  report: null,          // последняя сверка с ЯМ
  view: 'library',       // library | report | history | settings
  filter: 'all',
  query: '',
  sort: 'added',
  selected: new Set(),
  anchor: null,          // id для Shift-выбора
  cursor: null,          // id строки под «курсором» клавиатуры
  task: null,            // {name, trigger, phase, done, total, detail, indeterminate}
  live: new Map(),       // id → 'download' | 'convert' | 'upload'
  taskLog: [],
  lastResult: null,      // итог текущей задачи (all_done / cancelled / error)
  settingsDirty: false,
};

const listeners = new Map();

export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
}

export function emit(topic, payload) {
  listeners.get(topic)?.forEach(fn => {
    try { fn(payload); } catch (e) { console.error(e); }
  });
}

export function setTracks(list) {
  state.tracks = list;
  state.byId = new Map(list.map((t, i) => [t.id, Object.assign(t, { _i: i })]));
  for (const id of [...state.selected]) if (!state.byId.has(id)) state.selected.delete(id);
  if (state.cursor && !state.byId.has(state.cursor)) state.cursor = null;
  emit('tracks');
}

export function patchTrack(id, patch) {
  const t = state.byId.get(id);
  if (!t) return;
  Object.assign(t, patch);
  emit('track', id);
}

// Состояние трека — три признака (specs/features/track-states.md):
// has_file (диск) · ym (Яндекс Музыка) · liked + sc (SoundCloud) · error.
export const YM_IN = new Set(['confirmed', 'pinned']);
export const YM_CLAIMED = new Set(['confirmed', 'pinned', 'manual', 'sent']);  // повторная отправка → дубль
export const inYm = t => YM_IN.has(t.ym);
export const isNew = t => t.ym === 'none' && !t.has_file && t.sc !== 'unavailable' && t.liked !== false;
export const isReady = t => t.ym === 'none' && t.has_file;

// Разделы боковой панели по признакам. always — показывать и при нуле.
export const FILTER_GROUPS = [
  { title: 'Медиатека', items: [
    { id: 'all', label: 'Все треки', icon: 'music', test: () => true, always: true },
    { id: 'new', label: 'Новые', icon: 'sparkle', test: isNew, always: true },
    { id: 'ready', label: 'Готовы к отправке', icon: 'upload', test: isReady },
  ] },
  { title: 'Яндекс Музыка', items: [
    { id: 'in_ym', label: 'В ЯМ', icon: 'ym', test: inYm, always: true },
    { id: 'sent', label: 'Ждут ЯМ', icon: 'clock', test: t => t.ym === 'sent' },
    { id: 'missing', label: 'Нет в плейлисте', icon: 'alert', test: t => t.ym === 'missing', alert: true },
    { id: 'rejected', label: 'Не приняты ЯМ', icon: 'x', test: t => t.ym === 'rejected', alert: true },
    { id: 'manual', label: 'Не проверены', icon: 'info', test: t => t.ym === 'manual' },
    { id: 'not_in_ym', label: 'Не в ЯМ', icon: 'notym', test: t => !inYm(t) },
  ] },
  { title: 'Диск', items: [
    { id: 'on_disk', label: 'На диске', icon: 'disk', test: t => t.has_file, always: true },
    { id: 'cloud_only', label: 'Только в ЯМ', icon: 'ym', test: t => !t.has_file && inYm(t) },
  ] },
  { title: 'SoundCloud', items: [
    { id: 'unliked', label: 'Пропали из лайков', icon: 'heartoff', test: t => t.liked === false },
    { id: 'unavailable', label: 'Недоступные', icon: 'lock', test: t => t.sc === 'unavailable', alert: true },
  ] },
  { title: '', items: [
    { id: 'errors', label: 'С ошибкой', icon: 'alert', test: t => !!t.error, alert: true },
  ] },
];
export const FILTERS = FILTER_GROUPS.flatMap(g => g.items);
export const filterById = id => FILTERS.find(f => f.id === id) || FILTERS[0];

/** Бейджи состояния трека: [{cls, text, title}] — ЯМ, диск, SoundCloud. */
export function trackBadges(t, when = () => '') {
  const out = [];
  const checked = t.ym_checked_at ? ` (проверено ${when(t.ym_checked_at)})` : '';
  switch (t.ym) {
    case 'confirmed': out.push({ cls: 'ym-ok', text: 'В ЯМ', title: `Найден в плейлисте${checked}` }); break;
    case 'pinned': out.push({ cls: 'ym-ok', text: 'В ЯМ', title: 'В ЯМ по вашему подтверждению — сверка его не помечает' }); break;
    case 'sent': out.push({ cls: 'ym-sent', text: 'Ждёт ЯМ', title: `ЯМ принял файл${t.sent_at ? ' ' + when(t.sent_at) : ''}; станет «В ЯМ», когда появится в плейлисте` }); break;
    case 'manual': out.push({ cls: 'ym-manual', text: 'Не проверен', title: 'Отмечен «Уже в ЯМ» вручную; проверится по плейлисту при сверке' }); break;
    case 'missing': out.push({ cls: 'ym-missing', text: 'Нет в плейлисте', title: `Считался в ЯМ, но не найден в плейлисте${checked}` }); break;
    case 'rejected': out.push({ cls: 'ym-rejected', text: 'Не принят ЯМ', title: t.error || 'ЯМ не добавил трек в плейлист' }); break;
    default:
      if (t.sc === 'unavailable') break;
      // «Новый» — только то, что попадает в раздел «Новые» (isNew): разлайкнутые туда не входят.
      out.push(isNew(t)
        ? { cls: 'new', text: 'Новый', title: 'Не скачан и не в Яндекс Музыке' }
        : { cls: 'ym-none', text: 'Не в ЯМ', title: t.has_file ? 'Скачан, в Яндекс Музыку не отправлялся' : 'Не скачан и не в Яндекс Музыке' });
  }
  if (t.has_file) out.push({ cls: 'disk', text: 'На диске', title: 'Файл в папке с музыкой' });
  if (t.sc === 'unavailable') out.push({ cls: 'unavail', text: 'Недоступен', title: t.sc_reason || 'Скачать нельзя' });
  if (t.liked === false) out.push({ cls: 'unliked', text: 'Не в лайках', title: 'Вы убрали лайк на SoundCloud' });
  return out;
}

export const LIVE_LABEL = { download: 'Скачивается', convert: 'Конвертация', upload: 'Отправка в ЯМ' };

export const TASK_TITLES = {
  scan: 'Проверка лайков', download: 'Скачивание', upload: 'Загрузка в ЯМ', sync: 'Синхронизация',
};

/** Автор из ссылки SoundCloud (soundcloud.com/<автор>/<трек>) — пока нет исполнителя. */
export function scAuthor(t) {
  try { return new URL(t.sc_url).pathname.split('/')[1] || ''; } catch { return ''; }
}

const collator = new Intl.Collator('ru', { sensitivity: 'base', numeric: true });

let cache = { key: null, list: [] };
let version = 0;
on('tracks', () => version++);
on('track', () => version++);

/** Треки текущего раздела с учётом поиска и сортировки (мемоизировано). */
export function visibleTracks() {
  const key = `${version}|${state.filter}|${state.query}|${state.sort}`;
  if (cache.key === key) return cache.list;
  const f = filterById(state.filter);
  const q = state.query;
  let list = state.tracks.filter(t => f.test(t) && (!q ||
    t.title.toLowerCase().includes(q) || t.artist.toLowerCase().includes(q) ||
    scAuthor(t).toLowerCase().includes(q)));
  if (state.sort === 'artist') {
    list = [...list].sort((a, b) => collator.compare(a.artist || scAuthor(a), b.artist || scAuthor(b)) || collator.compare(a.title, b.title));
  } else if (state.sort === 'title') {
    list = [...list].sort((a, b) => collator.compare(a.title, b.title));
  }
  cache = { key, list };
  return list;
}

export function filterCounts() {
  const counts = Object.fromEntries(FILTERS.map(f => [f.id, 0]));
  for (const t of state.tracks) for (const f of FILTERS) if (f.test(t)) counts[f.id]++;
  return counts;
}

export const busy = () => !!state.task;
