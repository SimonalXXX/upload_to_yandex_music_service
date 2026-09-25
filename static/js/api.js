// Клиент локального API (specs/openapi.yaml) и поток событий (specs/sse-events.md).

async function req(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch { /* пустой ответ */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || `Ошибка сервера (${r.status})`);
    e.status = r.status; e.data = data;
    throw e;
  }
  return data;
}

const idPath = id => encodeURIComponent(id);

export const api = {
  status: () => req('GET', '/api/status'),
  tracks: () => req('GET', '/api/tracks'),
  settings: () => req('GET', '/api/settings'),
  saveSettings: patch => req('POST', '/api/settings', patch),
  validateSoundcloud: username => req('POST', '/api/validate/soundcloud', { username }),
  validatePlaylist: playlist_url => req('POST', '/api/validate/playlist', { playlist_url }),
  auth: refresh => req('GET', '/api/check-yandex' + (refresh ? '?refresh=1' : '')),
  scan: () => req('POST', '/api/scan'),
  sync: () => req('POST', '/api/sync'),
  download: (track_ids, upload) => req('POST', '/api/start', { track_ids, upload }),
  upload: track_ids => req('POST', '/api/upload', { track_ids }),
  cancel: () => req('POST', '/api/cancel'),
  forceClear: () => req('POST', '/api/force-clear'),
  mark: track_ids => req('POST', '/api/mark-uploaded', { track_ids }),
  unmark: track_ids => req('POST', '/api/unmark-uploaded', { track_ids }),
  reveal: id => req('POST', `/api/tracks/${idPath(id)}/reveal`),
  openFolder: () => req('POST', '/api/open-folder'),
  lastReport: () => req('GET', '/api/check-playlist'),
  checkPlaylist: () => req('POST', '/api/check-playlist'),
  history: () => req('GET', '/api/history'),
  historyItem: id => req('GET', `/api/history/${idPath(id)}`),
};

export const audioUrl = id => `/api/tracks/${idPath(id)}/audio`;

/** SSE с переподключением. onEvent(obj) на каждое событие. */
export function connectEvents(onEvent) {
  let es = null;
  let retry = 0;
  let timer = 0;
  const open = () => {
    clearTimeout(timer);
    if (es && es.readyState !== EventSource.CLOSED) return;
    es = new EventSource('/api/progress');
    es.onmessage = e => {
      retry = 0;
      try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    es.onerror = () => {
      es.close();
      timer = setTimeout(open, Math.min(10000, 1000 * 2 ** retry++));
    };
  };
  open();
  document.addEventListener('visibilitychange', () => {
    // После сна вкладки EventSource может «висеть» — переоткрываем.
    if (document.visibilityState === 'visible' && es && es.readyState === EventSource.CLOSED) open();
  });
}
