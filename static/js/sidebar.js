// Боковая панель: разделы медиатеки со счётчиками, сверка, история, настройки; статус ЯМ и автосинхронизации.

import { $, esc, fmtIn, icon } from './dom.js';
import { FILTER_GROUPS, filterCounts, on, state } from './store.js';

let frame = 0;

export function init() {
  $('#nav').addEventListener('click', e => {
    const b = e.target.closest('[data-route]');
    if (!b) return;
    location.hash = `#${b.dataset.route}`;
    closeDrawer();
  });
  $('#sidebar-footer').addEventListener('click', () => { location.hash = '#settings'; closeDrawer(); });
  $('#btn-menu').addEventListener('click', () => {
    $('#sidebar').classList.add('open');
    $('#scrim').hidden = false;
  });
  $('#scrim').addEventListener('click', closeDrawer);
  for (const topic of ['tracks', 'track', 'status', 'auth', 'report', 'view', 'settings', 'settings-dirty']) on(topic, schedule);
  setInterval(schedule, 30000);  // «через N минут» в подвале
  schedule();
}

function closeDrawer() {
  $('#sidebar').classList.remove('open');
  $('#scrim').hidden = true;
}

function schedule() {
  if (frame) return;
  frame = requestAnimationFrame(() => { frame = 0; render(); });
}

function item(route, iconName, label, countHtml = '', active = false) {
  return `<button type="button" class="nav-item${active ? ' active' : ''}" data-route="${route}" ${active ? 'aria-current="page"' : ''}>
    ${icon(iconName)}<span>${esc(label)}</span>${countHtml}</button>`;
}

function render() {
  const counts = filterCounts();
  const lib = state.view === 'library';
  let html = '';
  // Разделы по признакам (диск / Яндекс Музыка / SoundCloud); пустые необязательные — скрыты.
  for (const g of FILTER_GROUPS) {
    let items = '';
    for (const f of g.items) {
      const active = lib && state.filter === f.id;
      if (!f.always && !counts[f.id] && !active) continue;
      const alert = f.alert && counts[f.id];
      items += item(`library/${f.id}`, f.icon, f.label, `<span class="count${alert ? ' alert' : ''}">${counts[f.id]}</span>`, active);
    }
    if (g.title === 'Яндекс Музыка') items += item('report', 'report', 'Сверка', '', state.view === 'report');
    if (items) html += (g.title ? `<div class="nav-section">${esc(g.title)}</div>` : '<div class="nav-gap"></div>') + items;
  }
  html += '<div class="nav-section">Приложение</div>';
  html += item('history', 'clock', 'История', '', state.view === 'history');
  html += item('settings', 'gear', 'Настройки', state.settingsDirty ? '<span class="dirty" title="Есть несохранённые изменения"></span>' : '', state.view === 'settings');
  $('#nav').innerHTML = html;
  renderFooter();
}

function renderFooter() {
  const a = state.auth;
  const ym = !a ? '<span>Яндекс Музыка: проверяю…</span>'
    : a.authorized ? `<span class="ok">●</span> Яндекс Музыка: ${esc(a.login)}`
    : '<span class="bad">●</span> Яндекс Музыка: нет доступа';
  const au = state.status?.autosync;
  const sync = !au ? '' : au.enabled
    ? `Автосинхронизация ${state.task?.trigger === 'auto' ? 'идёт' : fmtIn(au.next_run)}`
    : 'Автосинхронизация выключена';
  $('#sidebar-footer').innerHTML = `${ym}<br>${esc(sync)}`;
}
