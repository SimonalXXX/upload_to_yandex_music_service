// Мелкие помощники DOM: иконки, экранирование, форматирование, тосты, диалог, меню.

const P = {
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  sparkle: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 17l.7 1.8 1.8.7-1.8.7L19 22l-.7-1.8-1.8-.7 1.8-.7z"/>',
  disk: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M12 8v6m-3-3 3 3 3-3"/><path d="M7 17h10"/>',
  ym: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.5 2.5 4.5-5"/>',
  notym: '<circle cx="12" cy="12" r="9" stroke-dasharray="3 2.6"/><path d="M12 8v5m0 3h.01"/>',
  heartoff: '<path d="M19.5 13.6 12 21l-7-7C3.5 12.5 2 10.8 2 8.5A5.5 5.5 0 0 1 7.5 3c1.8 0 3 .5 4.5 2 1.5-1.5 2.7-2 4.5-2A5.5 5.5 0 0 1 22 8.5c0 .9-.2 1.7-.6 2.5"/><path d="m3 3 18 18"/>',
  heart: '<path d="M19 14c1.5-1.5 3-3.2 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.8 0-3 .5-4.5 2-1.5-1.5-2.7-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4 3 5.5l7 7z"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4m0 4h.01"/>',
  report: '<path d="m3 6 2 2 4-4M3 13l2 2 4-4"/><path d="M13 6h8M13 13h8M4 20h.01M9 20h12"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  gear: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  sync: '<path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  play: '<path d="M7 4.5v15l12.5-7.5z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M6.5 4h4v16h-4zM13.5 4h4v16h-4z" fill="currentColor" stroke="none"/>',
  prev: '<path d="M19 20 9 12l10-8z" fill="currentColor" stroke="none"/><path d="M5 19V5"/>',
  next: '<path d="m5 4 10 8-10 8z" fill="currentColor" stroke="none"/><path d="M19 5v14"/>',
  more: '<circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="19" cy="12" r="1.3" fill="currentColor"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  external: '<path d="M14 3h7v7M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  download: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
};

export function icon(name, cls = 'icon') {
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || ''}</svg>`;
}

/** Подставляет иконки во все элементы с data-icon внутри root. */
export function hydrateIcons(root = document) {
  root.querySelectorAll('[data-icon]').forEach(el => {
    if (el.dataset.iconDone === el.dataset.icon) return;
    el.querySelector(':scope > svg.icon')?.remove();
    el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon));
    el.dataset.iconDone = el.dataset.icon;
  });
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** Экранирует текст и подсвечивает совпадения запроса. */
export function highlight(text, query) {
  const s = String(text ?? '');
  if (!query) return esc(s);
  const i = s.toLowerCase().indexOf(query);
  if (i < 0) return esc(s);
  return esc(s.slice(0, i)) + '<mark>' + esc(s.slice(i, i + query.length)) + '</mark>' + esc(s.slice(i + query.length));
}

export function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
export const count = (n, one, few, many) => `${n} ${plural(n, one, few, many)}`;

const dayFmt = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short' });
const fullFmt = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short', year: 'numeric' });
const timeFmt = new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit' });

export function fmtAdded(iso) {
  if (!iso) return '';
  const d = new Date(iso + 'T00:00:00');
  if (isNaN(d)) return iso;
  return (d.getFullYear() === new Date().getFullYear() ? dayFmt : fullFmt).format(d).replace('.', '');
}

export function fmtWhen(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const same = d.toDateString() === now.toDateString();
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (same) return `сегодня в ${timeFmt.format(d)}`;
  if (d.toDateString() === y.toDateString()) return `вчера в ${timeFmt.format(d)}`;
  return `${dayFmt.format(d).replace('.', '')} в ${timeFmt.format(d)}`;
}

export function fmtIn(ts) {
  if (!ts) return '';
  const sec = Math.round(ts - Date.now() / 1000);
  if (sec <= 60) return 'в течение минуты';
  const min = Math.round(sec / 60);
  if (min < 60) return `через ${count(min, 'минуту', 'минуты', 'минут')}`;
  const h = Math.floor(min / 60), m = min % 60;
  return `через ${h} ч${m ? ` ${m} мин` : ''}`;
}

export function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} с`;
  const m = Math.floor(sec / 60), s = sec % 60;
  return s ? `${m} мин ${s} с` : `${m} мин`;
}

/** Обложка-заглушка: цвет по хэшу строки (в стиле плиток Music.app). */
export function artStyle(key) {
  let h = 0;
  for (const ch of String(key)) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  const hue = h % 360;
  return `background:linear-gradient(135deg,hsl(${hue} 72% 62%),hsl(${(hue + 38) % 360} 70% 48%))`;
}

// ── Toasts ──
const TOAST_ICON = { ok: 'ym', warn: 'alert', error: 'alert', info: 'info' };

export function toast(text, { kind = 'info', title = '', action = null, timeout } = {}) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.innerHTML = `${icon(TOAST_ICON[kind] || 'info')}<div class="text">${title ? `<b>${esc(title)}</b>` : ''}${esc(text)}</div>`;
  if (action) {
    const b = document.createElement('button');
    b.className = 'btn'; b.type = 'button'; b.textContent = action.label;
    b.addEventListener('click', () => { action.run(); close(); });
    el.append(b);
  }
  const x = document.createElement('button');
  x.className = 'icon-btn'; x.type = 'button'; x.setAttribute('aria-label', 'Закрыть');
  x.innerHTML = icon('x');
  x.addEventListener('click', () => close());
  el.append(x);
  box.append(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  const ms = timeout ?? (kind === 'error' ? 12000 : kind === 'warn' ? 9000 : 5000);
  const timer = setTimeout(close, ms);
  function close() { clearTimeout(timer); el.remove(); }
  return close;
}

// ── Диалог подтверждения (native <dialog>) ──
export function confirmDialog({ title, text = '', ok = 'OK', cancel = 'Отмена', danger = false, iconName = 'music' }) {
  const dlg = $('#dialog');
  $('#dialog-title').textContent = title;
  $('#dialog-text').textContent = text;
  $('#dialog-ok').textContent = ok;
  $('#dialog-ok').className = `btn ${danger ? 'danger' : 'primary'}`;
  $('#dialog-cancel').textContent = cancel;
  const ic = $('.alert-icon', dlg);
  ic.dataset.icon = iconName;
  hydrateIcons(dlg);
  return new Promise(resolve => {
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
    dlg.returnValue = 'cancel';
    dlg.showModal();
    $('#dialog-ok').focus();
  });
}

// ── Контекстное меню ──
let menuCleanup = null;

export function closeMenu() {
  if (menuCleanup) { menuCleanup(); menuCleanup = null; }
}

/** items: [{label, icon, run, disabled}] | 'sep'. at: {x, y} или элемент-якорь. */
export function openMenu(items, at) {
  closeMenu();
  const menu = $('#menu');
  menu.innerHTML = '';
  for (const it of items) {
    if (it === 'sep') { menu.append(document.createElement('hr')); continue; }
    const b = document.createElement('button');
    b.type = 'button'; b.setAttribute('role', 'menuitem');
    b.innerHTML = `${icon(it.icon || '')}<span>${esc(it.label)}</span>`;
    b.disabled = !!it.disabled;
    b.addEventListener('click', () => { closeMenu(); it.run(); });
    menu.append(b);
  }
  menu.hidden = false;
  let x, y;
  if (at instanceof Element) {
    const r = at.getBoundingClientRect();
    x = r.right - menu.offsetWidth; y = r.bottom + 4;
  } else { ({ x, y } = at); }
  x = Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8));
  y = Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8));
  menu.style.left = `${x}px`; menu.style.top = `${y}px`;
  const buttons = $$('button:not(:disabled)', menu);
  buttons[0]?.focus();
  const onKey = e => {
    const i = buttons.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); buttons[(i + 1) % buttons.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length]?.focus(); }
  };
  const onDown = e => { if (!menu.contains(e.target)) closeMenu(); };
  const prevFocus = document.activeElement;
  document.addEventListener('keydown', onKey, true);
  setTimeout(() => document.addEventListener('mousedown', onDown), 0);
  addEventListener('blur', closeMenu, { once: true });
  menuCleanup = () => {
    menu.hidden = true;
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('mousedown', onDown);
    if (prevFocus && document.contains(prevFocus)) prevFocus.focus({ preventScroll: true });
  };
}

export const menuOpen = () => !$('#menu').hidden;

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
