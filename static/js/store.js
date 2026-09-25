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

export const FILTERS = [
  { id: 'all', label: 'Все треки', icon: 'music', test: () => true },
  { id: 'pending', label: 'Новые', icon: 'sparkle', test: t => t.status === 'pending' },
  { id: 'downloaded', label: 'На диске', icon: 'disk', test: t => t.status === 'downloaded' },
  { id: 'uploaded', label: 'В Яндекс Музыке', icon: 'ym', test: t => t.status === 'uploaded' },
  { id: 'not_in_ym', label: 'Не в ЯМ', icon: 'notym', test: t => t.status !== 'uploaded' },
  { id: 'not_in_likes', label: 'Пропали из лайков', icon: 'heartoff', test: t => t.status === 'not_in_likes' },
  { id: 'unavailable', label: 'Недоступные', icon: 'lock', test: t => t.status === 'unavailable' },
  { id: 'errors', label: 'С ошибкой', icon: 'alert', test: t => !!t.error && t.status !== 'unavailable' },
];
export const filterById = id => FILTERS.find(f => f.id === id) || FILTERS[0];

export const STATUS_LABEL = {
  pending: 'Новый',
  downloaded: 'На диске',
  uploaded: 'В ЯМ',
  not_in_likes: 'Пропал из лайков',
  unavailable: 'Недоступен',
};
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
