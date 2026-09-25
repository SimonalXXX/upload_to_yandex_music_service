// Настройки (все параметры config.yaml, кроме CLI-only) и мастер первого запуска.

import { api } from './api.js';
import { $, confirmDialog, count, debounce, esc, fmtIn, icon, toast } from './dom.js';
import { emit, on, state } from './store.js';
import { refreshStatus, runTask } from './tasks.js';

const SECTIONS = [
  ['SoundCloud', [
    { path: 'soundcloud.username', label: 'Профиль', hint: 'Имя из адреса soundcloud.com/имя или ссылка на профиль', type: 'text', check: 'sc', placeholder: 'имя или ссылка' },
    { path: 'soundcloud.max_tracks', label: 'Сколько лайков проверять', hint: '0 — все. С ограничением «пропавшие из лайков» не отмечаются', type: 'number', min: 0, step: 1 },
    { path: 'soundcloud.sleep_requests', label: 'Пауза между запросами, с', hint: 'Бережёт от ограничений SoundCloud', type: 'number', min: 0, max: 30, step: 0.5 },
    { path: 'soundcloud.cookies_browser', label: 'Cookies браузера', hint: 'Нужны только для приватных лайков', type: 'select',
      options: [['', 'Не использовать'], ['chrome', 'Chrome'], ['safari', 'Safari'], ['firefox', 'Firefox'], ['brave', 'Brave'], ['edge', 'Edge'], ['vivaldi', 'Vivaldi'], ['opera', 'Opera'], ['chromium', 'Chromium']] },
  ]],
  ['Яндекс Музыка', [
    { custom: 'auth' },
    { path: 'yandex_music.playlist_url', label: 'Плейлист', hint: 'Ссылка из адресной строки или «Поделиться» — только ваш плейлист', type: 'url', check: 'pl', placeholder: 'https://music.yandex.ru/playlists/…' },
    { path: 'yandex_music.upload_delay', label: 'Пауза между загрузками, с', type: 'number', min: 0, max: 60, step: 0.5 },
    { path: 'yandex_music.delete_after_upload', label: 'Удалять файл после загрузки', hint: 'Только когда трек найден в плейлисте. Плеер для таких треков будет недоступен', type: 'switch' },
  ]],
  ['Файлы', [
    { path: 'output.directory', label: 'Папка с музыкой', hint: 'Относительный путь — от папки приложения', type: 'text', folder: true },
    { path: 'output.format', label: 'Формат', type: 'select', options: [['mp3', 'MP3'], ['m4a', 'AAC (M4A)'], ['opus', 'Opus'], ['flac', 'FLAC']] },
    { path: 'output.quality', label: 'Битрейт', hint: 'Для MP3, AAC и Opus', type: 'select', options: [['128', '128 кбит/с'], ['192', '192 кбит/с'], ['256', '256 кбит/с'], ['320', '320 кбит/с']] },
  ]],
  ['Автосинхронизация', [
    { path: 'autosync.enabled', label: 'Синхронизировать автоматически', hint: 'Проверка лайков → скачивание → загрузка в ЯМ → сверка. Работает, пока запущено приложение', type: 'switch', next: true },
    { path: 'autosync.interval_hours', label: 'Как часто', type: 'select', num: true,
      options: [[1, 'Каждый час'], [3, 'Каждые 3 часа'], [6, 'Каждые 6 часов'], [12, 'Каждые 12 часов'], [24, 'Раз в день']] },
    { path: 'autosync.download', label: 'Скачивать новые треки', type: 'switch' },
    { path: 'autosync.upload', label: 'Загружать в Яндекс Музыку', type: 'switch' },
    { path: 'autosync.notify', label: 'Уведомления macOS', hint: 'Когда есть новые треки, загруженные или ошибки', type: 'switch' },
  ]],
];

let root;
let draft = null;
let checkAuthFn = async () => null;

const get = (obj, path) => path.split('.').reduce((o, k) => o?.[k], obj);
const set = (obj, path, v) => { const ks = path.split('.'); ks.slice(0, -1).reduce((o, k) => (o[k] ??= {}), obj)[ks.at(-1)] = v; };
const clone = o => JSON.parse(JSON.stringify(o));

export function setAuthChecker(fn) { checkAuthFn = fn; }

export function init() {
  root = $('#view-settings');
  root.addEventListener('input', onInput);
  root.addEventListener('change', onInput);
  root.addEventListener('click', onClick);
  on('view', () => { if (state.view === 'settings') render(); });
  on('settings', () => { if (state.view === 'settings' && !state.settingsDirty) render(); });
  on('auth', renderAuth);
  on('status', renderNext);
}

// ── Проверки при вводе ──

function makeChecker(fn) {
  const cache = new Map();
  let seq = 0;
  return {
    cache,
    async run(value, show) {
      value = value.trim();
      if (!value) { show(null); return null; }
      if (cache.has(value)) { show(cache.get(value)); return cache.get(value); }
      const my = ++seq;
      show({ pending: true });
      let res;
      try { res = await fn(value); } catch (err) { res = { ok: false, error: err.message }; }
      if (res.ok || !/недоступ|сервер/i.test(res.error || '')) cache.set(value, res);
      if (my === seq) show(res);
      return res;
    },
  };
}
const scChecker = makeChecker(api.validateSoundcloud);
const plChecker = makeChecker(api.validatePlaylist);

function checkHtml(res, kind) {
  if (!res) return '';
  if (res.pending) return '<span class="mini-spin"></span>Проверяю…';
  if (!res.ok) return `${icon('x')}${esc(res.error)}`;
  if (kind === 'sc') {
    return `${icon('check')}${esc(res.display_name)}${res.likes_count != null ? ` · ${count(res.likes_count, 'лайк', 'лайка', 'лайков')}` : ''}`;
  }
  return `${icon('check')}«${esc(res.title)}» · ${count(res.track_count ?? 0, 'трек', 'трека', 'треков')}`;
}

function showCheck(el, res, kind) {
  if (!el) return;
  el.className = `check-line${res && !res.pending ? (res.ok ? ' ok' : ' bad') : ''}`;
  el.innerHTML = checkHtml(res, kind);
}

const runSc = debounce((v, el) => scChecker.run(v, r => showCheck(el, r, 'sc')), 600);
const runPl = debounce((v, el) => plChecker.run(v, r => showCheck(el, r, 'pl')), 600);

// ── Форма ──

function fieldHtml(f) {
  if (f.custom === 'auth') {
    return `<div class="group-row"><div class="label">Аккаунт<span class="hint" id="auth-hint"></span></div>
      <div class="control"><span id="auth-state"></span><button class="btn" type="button" data-act="auth">Проверить снова</button></div></div>`;
  }
  const v = get(draft, f.path);
  const id = `f-${f.path.replace('.', '-')}`;
  let control;
  if (f.type === 'switch') {
    control = `<label class="switch"><input type="checkbox" id="${id}" data-path="${f.path}" ${v ? 'checked' : ''}><span></span></label>`;
  } else if (f.type === 'select') {
    control = `<select id="${id}" data-path="${f.path}" ${f.num ? 'data-num="1"' : ''}>${f.options.map(([val, label]) =>
      `<option value="${esc(val)}" ${String(val) === String(v) ? 'selected' : ''}>${esc(label)}</option>`).join('')}</select>`;
  } else {
    const attrs = f.type === 'number' ? `min="${f.min ?? ''}" max="${f.max ?? ''}" step="${f.step ?? 1}" data-num="1"` : '';
    control = `<input type="${f.type}" id="${id}" data-path="${f.path}" value="${esc(v ?? '')}" ${attrs}
      placeholder="${esc(f.placeholder || '')}" autocomplete="off" spellcheck="false">`;
    if (f.folder) control += `<button class="btn" type="button" data-act="folder" title="Открыть в Finder">${icon('folder')}</button>`;
    if (f.check === 'pl') control += `<button class="icon-btn" type="button" data-act="open-pl" title="Открыть плейлист" aria-label="Открыть плейлист">${icon('external')}</button>`;
  }
  let hint = f.hint ? `<span class="hint">${esc(f.hint)}</span>` : '';
  if (f.folder) hint += `<span class="hint">Сейчас: ${esc(state.settings?.output?.resolved_directory || '')}</span>`;
  if (f.next) hint += '<span class="hint" id="autosync-next"></span>';
  const check = f.check ? `<div class="check-line" id="check-${f.check}"></div>` : '';
  return `<div class="group-row"><label for="${id}">${esc(f.label)}${hint}${check}
    <span class="field-error" id="err-${f.path.replace('.', '-')}"></span></label><div class="control">${control}</div></div>`;
}

function render() {
  if (!state.settings) { root.innerHTML = '<div class="page muted">Загружаю…</div>'; return; }
  if (!state.settingsDirty || !draft) draft = clone(state.settings);
  root.innerHTML = `<div class="page">${SECTIONS.map(([title, fields]) =>
    `<h2>${esc(title)}</h2><div class="group">${fields.map(fieldHtml).join('')}</div>`).join('')}
    <div class="page-actions">
      <span class="muted" id="dirty-note" style="margin-right:auto;align-self:center"></span>
      <button class="btn" type="button" data-act="revert">Отменить изменения</button>
      <button class="btn primary" type="button" data-act="save">Сохранить</button>
    </div></div>`;
  $('#view-subtitle').textContent = 'config.yaml';
  renderAuth();
  renderNext();
  updateDirty();
  const sc = $('#f-soundcloud-username');
  if (sc?.value) scChecker.run(sc.value, r => showCheck($('#check-sc'), r, 'sc'));
  const pl = $('#f-yandex_music-playlist_url');
  if (pl?.value && state.auth?.authorized) plChecker.run(pl.value, r => showCheck($('#check-pl'), r, 'pl'));
}

function renderAuth() {
  const el = $('#auth-state');
  if (!el) return;
  const a = state.auth;
  el.innerHTML = !a ? '<span class="mini-spin"></span>'
    : a.authorized ? `<span class="check-line ok">${icon('check')}${esc(a.login)}</span>`
    : `<span class="check-line bad">${icon('x')}Нет доступа</span>`;
  $('#auth-hint').textContent = !a ? 'Проверяю вход в Google Chrome…'
    : a.authorized ? 'Вход взят из Google Chrome'
    : a.error || 'Войдите в music.yandex.ru в Google Chrome';
}

function renderNext() {
  const el = $('#autosync-next');
  const au = state.status?.autosync;
  if (!el || !au) return;
  el.textContent = au.enabled ? `Следующий запуск ${fmtIn(au.next_run)}` : '';
}

function patchFromDraft() {
  const patch = {};
  for (const [, fields] of SECTIONS) {
    for (const f of fields) {
      if (!f.path) continue;
      const a = get(draft, f.path), b = get(state.settings, f.path);
      if (JSON.stringify(a) !== JSON.stringify(b)) set(patch, f.path, a);
    }
  }
  return patch;
}

function updateDirty() {
  const dirty = Object.keys(patchFromDraft()).length > 0;
  if (dirty !== state.settingsDirty) { state.settingsDirty = dirty; emit('settings-dirty'); }
  const save = root.querySelector('[data-act=save]');
  if (save) {
    save.disabled = !dirty;
    root.querySelector('[data-act=revert]').disabled = !dirty;
    $('#dirty-note').textContent = dirty ? 'Есть несохранённые изменения' : '';
  }
}

function onInput(e) {
  const el = e.target;
  const path = el.dataset?.path;
  if (!path) return;
  let v;
  if (el.type === 'checkbox') v = el.checked;
  else if (el.dataset.num) v = el.value === '' ? '' : Number(el.value);
  else v = el.value;
  set(draft, path, v);
  el.classList.remove('invalid');
  const err = $(`#err-${path.replace('.', '-')}`);
  if (err) err.textContent = '';
  if (path === 'soundcloud.username' && e.type === 'input') runSc(el.value, $('#check-sc'));
  if (path === 'yandex_music.playlist_url' && e.type === 'input') runPl(el.value, $('#check-pl'));
  updateDirty();
}

async function onClick(e) {
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (act === 'save') save();
  else if (act === 'revert') { state.settingsDirty = false; draft = null; emit('settings-dirty'); render(); }
  else if (act === 'auth') { $('#auth-state').innerHTML = '<span class="mini-spin"></span>'; await checkAuthFn(true); render(); }
  else if (act === 'folder') api.openFolder().catch(err => toast(err.message, { kind: 'error' }));
  else if (act === 'open-pl') { const u = get(draft, 'yandex_music.playlist_url'); if (u) window.open(u, '_blank', 'noopener'); }
}

export async function save() {
  const patch = patchFromDraft();
  if (!Object.keys(patch).length) return true;
  const userChanged = 'soundcloud' in patch && 'username' in patch.soundcloud;
  try {
    const r = await api.saveSettings(patch);
    state.settings = r.settings;
    draft = clone(r.settings);
    state.settingsDirty = false;
    emit('settings-dirty');
    emit('settings');
    refreshStatus().catch(() => {});
    toast('Настройки сохранены', {
      kind: 'ok',
      action: userChanged && r.settings.soundcloud.username ? { label: 'Проверить лайки', run: () => runTask('scan') } : null,
    });
    if (state.view === 'settings') render();
    return true;
  } catch (err) {
    const field = err.data?.field;
    if (field) {
      const input = root.querySelector(`[data-path="${field}"]`);
      input?.classList.add('invalid');
      input?.focus();
      const el = $(`#err-${field.replace('.', '-')}`);
      if (el) el.textContent = err.message;
    }
    toast(err.message, { kind: 'error', title: 'Не сохранено' });
    return false;
  }
}

export async function confirmLeave() {
  if (!state.settingsDirty) return true;
  const ok = await confirmDialog({
    title: 'Сохранить изменения?', text: 'В настройках есть несохранённые изменения.',
    ok: 'Сохранить', cancel: 'Не сохранять', iconName: 'gear',
  });
  if (ok) return save();
  state.settingsDirty = false;
  draft = null;
  emit('settings-dirty');
  return true;
}

// ── Мастер первого запуска ──

const ob = { step: 1, sc: null, pl: null };

export function openOnboarding() {
  ob.step = 1;
  $('#onboarding').hidden = false;
  renderOnboarding();
}

function closeOnboarding() {
  $('#onboarding').hidden = true;
}

function stepsHtml() {
  return `<div class="ob-steps">${[1, 2, 3].map(i => `<i class="${i <= ob.step ? 'done' : ''}"></i>`).join('')}</div>`;
}

function renderOnboarding() {
  const box = $('#onboarding');
  if (ob.step === 1) {
    box.innerHTML = `<div class="ob-card" role="dialog" aria-modal="true" aria-labelledby="ob-title">${stepsHtml()}
      <h2 id="ob-title">Добро пожаловать</h2>
      <p>Музыкальный скачивает ваши лайки с SoundCloud в MP3 и складывает их в плейлист Яндекс Музыки.</p>
      <label for="ob-sc"><b>Ваш профиль на SoundCloud</b></label>
      <input type="text" id="ob-sc" placeholder="имя или ссылка soundcloud.com/…" autocomplete="off" spellcheck="false" value="${esc(ob.sc?.username || '')}">
      <div class="check-line" id="ob-sc-check"></div>
      <div class="ob-buttons"><span class="grow"></span><button class="btn primary" id="ob-next" type="button" disabled>Далее</button></div></div>`;
    const input = $('#ob-sc');
    const btn = $('#ob-next');
    const check = debounce(v => scChecker.run(v, r => {
      showCheck($('#ob-sc-check'), r, 'sc');
      ob.sc = r?.ok ? r : null;
      btn.disabled = !ob.sc;
    }), 500);
    input.addEventListener('input', () => { btn.disabled = true; check(input.value); });
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !btn.disabled) btn.click(); });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const r = await api.saveSettings({ soundcloud: { username: ob.sc.username } });
        state.settings = r.settings;
        emit('settings');
        ob.step = 2;
        renderOnboarding();
      } catch (err) { toast(err.message, { kind: 'error' }); btn.disabled = false; }
    });
    if (input.value) check(input.value);
    input.focus();
    return;
  }

  if (ob.step === 2) {
    const a = state.auth;
    const authHtml = !a ? '<span class="mini-spin"></span> Проверяю вход в Google Chrome…'
      : a.authorized ? `<span class="check-line ok">${icon('check')}Вы вошли как ${esc(a.login)}</span>`
      : `<span class="check-line bad">${icon('x')}${esc(a.error || 'Нет доступа')}</span>`;
    box.innerHTML = `<div class="ob-card" role="dialog" aria-modal="true" aria-labelledby="ob-title">${stepsHtml()}
      <h2 id="ob-title">Яндекс Музыка</h2>
      <p>Чтобы загружать треки, войдите в music.yandex.ru в Google Chrome — приложение возьмёт вход оттуда. Можно пропустить и только скачивать.</p>
      <div class="ob-auth">${authHtml}<button class="btn" type="button" id="ob-recheck">Проверить снова</button></div>
      ${a && !a.authorized ? `<div class="help">Если пишет про доступ к данным Chrome:<ol>
        <li>Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску</li>
        <li>Включите «Терминал», закройте его (⌘Q) и запустите «Запуск.command» снова</li></ol></div>` : ''}
      <label for="ob-pl"><b>Ссылка на ваш плейлист</b></label>
      <input type="url" id="ob-pl" placeholder="https://music.yandex.ru/playlists/…" autocomplete="off" value="${esc(ob.pl?.url || state.settings?.yandex_music?.playlist_url || '')}" ${a?.authorized ? '' : 'disabled'}>
      <div class="check-line" id="ob-pl-check"></div>
      <div class="ob-buttons"><button class="btn" type="button" id="ob-back">Назад</button><span class="grow"></span>
        <button class="btn" type="button" id="ob-skip">Пропустить</button>
        <button class="btn primary" type="button" id="ob-next" disabled>Далее</button></div></div>`;
    const input = $('#ob-pl');
    const btn = $('#ob-next');
    const check = debounce(v => plChecker.run(v, r => {
      showCheck($('#ob-pl-check'), r, 'pl');
      ob.pl = r?.ok ? { ...r, url: v.trim() } : null;
      btn.disabled = !ob.pl;
    }), 500);
    input.addEventListener('input', () => { btn.disabled = true; check(input.value); });
    $('#ob-recheck').addEventListener('click', async () => { state.auth = null; renderOnboarding(); await checkAuthFn(true); renderOnboarding(); });
    $('#ob-back').addEventListener('click', () => { ob.step = 1; renderOnboarding(); });
    $('#ob-skip').addEventListener('click', () => { ob.pl = null; ob.step = 3; renderOnboarding(); });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const r = await api.saveSettings({ yandex_music: { playlist_url: ob.pl.url } });
        state.settings = r.settings;
        emit('settings');
        ob.step = 3;
        renderOnboarding();
      } catch (err) { toast(err.message, { kind: 'error' }); btn.disabled = false; }
    });
    if (!a) checkAuthFn(false).then(() => { if (ob.step === 2) renderOnboarding(); });
    else if (a.authorized && input.value) check(input.value);
    (a?.authorized ? input : $('#ob-recheck')).focus();
    return;
  }

  box.innerHTML = `<div class="ob-card" role="dialog" aria-modal="true" aria-labelledby="ob-title">${stepsHtml()}
    <h2 id="ob-title">Всё готово</h2>
    <p>Сначала проверим лайки. Потом выберите треки и нажмите «Скачать и в ЯМ» — или просто «Синхронизировать».</p>
    <div class="group">
      <div class="group-row"><div class="label">SoundCloud</div><div class="control">${esc(ob.sc?.display_name || state.settings?.soundcloud?.username || '')}${ob.sc?.likes_count != null ? ` · ${count(ob.sc.likes_count, 'лайк', 'лайка', 'лайков')}` : ''}</div></div>
      <div class="group-row"><div class="label">Яндекс Музыка</div><div class="control">${ob.pl ? `«${esc(ob.pl.title)}»` : '<span class="muted">не подключена — можно позже в настройках</span>'}</div></div>
    </div>
    <div class="ob-buttons"><button class="btn" type="button" id="ob-back">Назад</button><span class="grow"></span>
      <button class="btn primary" type="button" id="ob-finish">${icon('heart')}Проверить лайки</button></div></div>`;
  $('#ob-back').addEventListener('click', () => { ob.step = 2; renderOnboarding(); });
  $('#ob-finish').addEventListener('click', async () => {
    closeOnboarding();
    await refreshStatus().catch(() => {});
    runTask('scan');
  });
  $('#ob-finish').focus();
}
