// Сверка с плейлистом ЯМ: результат записывается в состояние «Яндекс Музыка» каждого трека.
// Здесь — итоги и списки с действиями: «⚠ нет в плейлисте», «не приняты ЯМ», «ждут появления»,
// а также дубли и треки в обработке (specs/features/track-states.md).

import { api } from './api.js';
import { $, confirmDialog, count, esc, fmtWhen, icon, toast } from './dom.js';
import { actMark, actUnmark, setSelection } from './library.js';
import { on, emit, state } from './store.js';
import { refreshData, runTask } from './tasks.js';

// Выбор — отдельно для каждого списка с действиями.
const LISTS = {
  missing: {
    title: 'Нет в плейлисте',
    lead: 'Считались «В ЯМ», но при сверке не найдены по исполнителю и названию. Бывает, что ЯМ показывает трек под другим названием — тогда нажмите «Считать «В ЯМ»», и сверка перестанет его помечать.',
    filter: 'missing',
  },
  rejected: {
    title: 'Не приняты ЯМ',
    lead: 'ЯМ принял файл, но трек так и не появился в плейлисте за сутки. Можно отправить ещё раз.',
    filter: 'rejected',
  },
};
const picked = { missing: new Set(), rejected: new Set() };
let checking = false;
let root;
let dupes = null;          // предпросмотр: {groups, total_remove} из /api/playlist/duplicates
let dupBusy = false;
const dupPicked = new Set(); // id оставляемых копий выбранных групп

export function init() {
  root = $('#view-report');
  root.addEventListener('click', onClick);
  root.addEventListener('change', onChange);
  for (const t of ['report', 'view', 'task', 'settings', 'tracks', 'track']) on(t, render);
}

async function check() {
  if (checking) return;
  checking = true;
  render();
  try {
    state.report = await api.checkPlaylist();
    emit('report');
    await refreshData().catch(() => {});  // сверка записала состояние ЯМ в треки
    const c = state.report.changes || {};
    const parts = [
      c.confirmed && `подтверждено «В ЯМ»: ${c.confirmed}`,
      c.missing && `нет в плейлисте: ${c.missing}`,
      c.rejected && `не приняты ЯМ: ${c.rejected}`,
    ].filter(Boolean);
    toast(parts.length ? `Изменения: ${parts.join(', ')}` : 'Изменений нет — всё как при прошлой проверке',
      { kind: c.missing || c.rejected ? 'warn' : 'ok', title: 'Сверка' });
  } catch (err) {
    toast(err.message, { kind: 'error', title: 'Сверка не удалась' });
  } finally {
    checking = false;
    render();
  }
}

const byYm = ym => state.tracks.filter(t => t.ym === ym);

function listHtml(key, running) {
  const L = LISTS[key];
  const tracks = byYm(key);
  const sel = picked[key];
  for (const id of [...sel]) if (!tracks.some(t => t.id === id)) sel.delete(id);
  if (!tracks.length) return '';
  return `<h2>${esc(L.title)} · ${tracks.length}</h2>
    <p class="lead">${esc(L.lead)}</p>
    <div class="group" data-list="${key}">
      <div class="group-tools">
        <label><input type="checkbox" data-all="${key}" ${sel.size && sel.size === tracks.length ? 'checked' : ''}> Выбрать все</label>
        <span class="grow muted">${sel.size ? `выбрано ${sel.size}` : ''}</span>
        <button class="btn" data-act="show" data-list="${key}" type="button" ${sel.size ? '' : 'disabled'}>В медиатеке</button>
        <button class="btn" data-act="pin" data-list="${key}" type="button" ${sel.size && !running ? '' : 'disabled'}
          title="ЯМ переименовал трек, и сверка его не узнаёт — считать его «В ЯМ»">Считать «В ЯМ»</button>
        <button class="btn" data-act="unmark" data-list="${key}" type="button" ${sel.size && !running ? '' : 'disabled'}
          title="Трек станет «не отправлен»">Снять «В ЯМ»</button>
        <button class="btn primary" data-act="reupload" data-list="${key}" type="button" ${sel.size && !running ? '' : 'disabled'}>${icon('upload')}Отправить заново</button>
      </div>
      <ul class="plain-list">${tracks.map(t => `<li>
        <input type="checkbox" data-pick="${key}" data-id="${esc(t.id)}" ${sel.has(t.id) ? 'checked' : ''} aria-label="Выбрать">
        <div class="grow"><div>${esc(t.title)}</div><div class="sub">${esc(t.artist || '—')}
          · ${t.has_file ? 'на диске' : 'файла нет — скачается заново'}${t.liked === false ? ' · не в лайках' : ''}</div></div>
      </li>`).join('')}</ul>
    </div>`;
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
      <h3>Сверка ещё не проводилась</h3><p>Сравним треки, отмеченные «В ЯМ», с тем, что реально лежит в плейлисте, и отметим результат у каждого трека.</p>${btn}</div></div>`;
    return;
  }
  $('#view-subtitle').textContent = `сверено ${fmtWhen(r.checked_at)}`;
  const running = !!state.task;
  const inYm = state.tracks.filter(t => t.ym === 'confirmed' || t.ym === 'pinned').length;
  const missingN = byYm('missing').length;
  const sent = byYm('sent');
  const manual = byYm('manual').length;

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
      ${stat(inYm, 'у нас «В ЯМ»')}
      ${stat(missingN, 'нет в плейлисте', missingN)}
      ${stat(r.duplicates, 'дублей', r.duplicates)}
      ${stat(r.processing, 'в обработке')}
    </div>
    ${manual ? `<p class="lead">${icon('info')} ${count(manual, 'трек отмечен', 'трека отмечены', 'треков отмечены')} «Уже в ЯМ» вручную и ещё не проверены — нажмите «Сверить сейчас».</p>` : ''}

    ${listHtml('missing', running) || `<h2>Нет в плейлисте · 0</h2><div class="group"><ul class="plain-list"><li class="muted">${icon('check')} Все треки «В ЯМ» на месте</li></ul></div>`}
    ${listHtml('rejected', running)}

    ${sent.length ? `<h2>Ждут появления в ЯМ · ${sent.length}</h2>
    <p class="lead">ЯМ принял файлы, но в плейлисте их пока нет. Станут «В ЯМ», когда появятся; если не появятся за сутки — «Не приняты ЯМ».</p>
    <div class="group"><ul class="plain-list">${sent.map(t => `<li><div class="grow"><div>${esc(t.title)}</div>
      <div class="sub">${esc(t.artist || '—')}${t.sent_at ? ` · отправлен ${esc(fmtWhen(t.sent_at))}` : ''}</div></div></li>`).join('')}</ul></div>` : ''}

    ${dupHtml(r, running)}

    <h2>В обработке · ${r.processing_list.length}</h2>
    <div class="group"><ul class="plain-list">${r.processing_list.length
      ? r.processing_list.map(d => `<li><div class="grow"><div>${esc(d.title)}</div><div class="sub">${esc(d.artist)}</div></div></li>`).join('')
      : '<li class="muted">Все треки плейлиста доступны для прослушивания</li>'}</ul></div>
  </div>`;
}

function dupHtml(r, running) {
  const head = `<h2>Дубли в плейлисте · ${r.duplicates_list.length}</h2>`;
  if (!dupes) {
    return `${head}<p class="lead">Найдём лишние копии и покажем, какая останется. Ничего не удаляется, пока вы не подтвердите.</p>
      <div class="group"><ul class="plain-list">${r.duplicates_list.length
        ? r.duplicates_list.map(d => `<li><div class="grow"><div>${esc(d.title)}</div><div class="sub">${esc(d.artist)}</div></div><span class="num">× ${d.count}</span></li>`).join('')
        : `<li class="muted">${icon('check')} Дублей нет</li>`}</ul></div>
      ${r.duplicates_list.length ? `<p><button class="btn" data-act="dup-find" type="button" ${dupBusy ? 'disabled' : ''}>${dupBusy ? '<span class="mini-spin"></span>Ищу…' : 'Найти лишние копии'}</button></p>` : ''}`;
  }
  const g = dupes.groups;
  const n = [...dupPicked].reduce((a, id) => a + (g.find(x => x.keep.id === id)?.remove.length || 0), 0);
  return `${head}<p class="lead">В каждой группе остаётся самая ранняя копия, остальные удаляются из плейлиста. <b>Это необратимо</b> — резервная копия плейлиста сохраняется в папку backups/. Два разных трека с одинаковым названием и длительностью попадут сюда же — снимите галочку.</p>
    <div class="group">
      <div class="group-tools">
        <label><input type="checkbox" data-dup-all ${dupPicked.size && dupPicked.size === g.length ? 'checked' : ''}> Выбрать все</label>
        <span class="grow muted">${dupPicked.size ? `групп ${dupPicked.size} · удалится копий: ${n}` : `групп: ${g.length}`}</span>
        <button class="btn" data-act="dup-refresh" type="button" ${dupBusy ? 'disabled' : ''}>Обновить</button>
        <button class="btn danger" data-act="dup-delete" type="button" ${dupPicked.size && !running && !dupBusy ? '' : 'disabled'}>${dupBusy ? '<span class="mini-spin"></span>Удаляю…' : `Удалить лишние копии (${n})`}</button>
      </div>
      <ul class="plain-list">${g.length ? g.map(x => `<li>
        <input type="checkbox" data-dup="${esc(x.keep.id)}" ${dupPicked.has(x.keep.id) ? 'checked' : ''} aria-label="Выбрать группу">
        <div class="grow"><div>${esc(x.title)} ${esc(x.version)}</div>
          <div class="sub">${esc(x.artist)} · ${x.dur} с · копий ${x.remove.length + 1}: останется добавленная ${esc((x.keep.ts || '').slice(0, 16).replace('T', ' '))}, удалится ${x.remove.length}</div></div>
      </li>`).join('') : `<li class="muted">${icon('check')} Дублей нет</li>`}</ul>
    </div>`;
}

async function dupFind() {
  dupBusy = true; render();
  try {
    dupes = await api.duplicates();
    for (const id of [...dupPicked]) if (!dupes.groups.some(x => x.keep.id === id)) dupPicked.delete(id);
  } catch (err) { toast(err.message, { kind: 'error', title: 'Поиск дублей' }); }
  dupBusy = false; render();
}

async function dupDelete() {
  const g = dupes.groups.filter(x => dupPicked.has(x.keep.id));
  const n = g.reduce((a, x) => a + x.remove.length, 0);
  const ok = await confirmDialog({
    title: `Удалить ${count(n, 'копию', 'копии', 'копий')} из плейлиста?`,
    text: `Групп: ${g.length}. В каждой останется одна самая ранняя копия. Действие необратимо, резервная копия плейлиста сохранится в папке backups/.`,
    ok: 'Удалить', danger: true, iconName: 'alert',
  });
  if (!ok) return;
  dupBusy = true; render();
  try {
    const r = await api.dedupe(g.map(x => x.keep.id));
    toast(r.ok ? `Удалено копий: ${r.removed}. В плейлисте было ${r.before}, стало ${r.after}.`
      : `Удалено ${r.removed}, но проверка не сошлась (было ${r.before}, стало ${r.after}) — проверьте плейлист.`,
      { kind: r.ok ? 'ok' : 'warn', title: 'Дубли' });
    dupPicked.clear();
    await check();   // свежая сверка и список
    dupes = null;
  } catch (err) { toast(err.message, { kind: 'error', title: 'Не удалось удалить' }); }
  dupBusy = false; render();
}

function stat(n, label, bad = 0) {
  return `<div class="stat${bad ? ' bad' : ''}"><b>${n}</b><span>${esc(label)}</span></div>`;
}

function onChange(e) {
  const cb = e.target;
  if (cb.dataset.dupAll !== undefined) {
    dupPicked.clear();
    if (cb.checked) dupes.groups.forEach(x => dupPicked.add(x.keep.id));
  } else if (cb.dataset.dup) {
    cb.checked ? dupPicked.add(cb.dataset.dup) : dupPicked.delete(cb.dataset.dup);
  } else if (cb.dataset.all) {
    const key = cb.dataset.all;
    picked[key].clear();
    if (cb.checked) byYm(key).forEach(t => picked[key].add(t.id));
  } else if (cb.dataset.pick) {
    const set = picked[cb.dataset.pick];
    cb.checked ? set.add(cb.dataset.id) : set.delete(cb.dataset.id);
  }
  render();
}

async function onClick(e) {
  const b = e.target.closest('button[data-act]');
  if (!b) return;
  const act = b.dataset.act;
  const key = b.dataset.list;
  const tracks = key ? [...picked[key]].map(id => state.byId.get(id)).filter(Boolean) : [];
  if (act === 'check') check();
  else if (act === 'dup-find' || act === 'dup-refresh') dupFind();
  else if (act === 'dup-delete') dupDelete();
  else if (act === 'settings') location.hash = '#settings';
  else if (act === 'show') { setSelection(tracks.map(t => t.id)); location.hash = `#library/${LISTS[key].filter}`; }
  else if (act === 'pin') { await actMark(tracks); picked[key].clear(); render(); }
  else if (act === 'unmark') { await actUnmark(tracks); picked[key].clear(); render(); }
  else if (act === 'reupload') {
    const ok = await confirmDialog({
      title: `Отправить заново ${count(tracks.length, 'трек', 'трека', 'треков')}?`,
      text: 'Треки без файла сначала скачаются. Если трек на самом деле есть в плейлисте под другим названием, появится дубль — в этом случае лучше «Считать «В ЯМ»».',
      ok: 'Отправить', iconName: 'upload',
    });
    if (ok && await runTask('download', tracks.map(t => t.id), true)) picked[key].clear();
  }
}
