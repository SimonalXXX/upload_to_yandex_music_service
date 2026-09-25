// Встроенный плеер скачанных треков. Media Session — клавиши мультимедиа и «Пункт управления» macOS.

import { audioUrl } from './api.js';
import { $, artStyle, fmtTime, hydrateIcons, icon, toast } from './dom.js';
import { emit, scAuthor, state, visibleTracks } from './store.js';

let audio, seek, current = null, seeking = false;

export const currentId = () => current;
export const isPlaying = () => !!audio && !audio.paused && !!current;

export function init() {
  audio = $('#audio');
  seek = $('#pl-seek');
  $('#pl-play').addEventListener('click', toggle);
  $('#pl-prev').addEventListener('click', () => step(-1));
  $('#pl-next').addEventListener('click', () => step(1));
  $('#pl-close').addEventListener('click', close);
  seek.addEventListener('input', () => { seeking = true; $('#pl-cur').textContent = fmtTime((seek.value / 1000) * (audio.duration || 0)); });
  seek.addEventListener('change', () => { if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration; seeking = false; });
  audio.addEventListener('timeupdate', onTime);
  audio.addEventListener('loadedmetadata', onTime);
  audio.addEventListener('play', sync);
  audio.addEventListener('pause', sync);
  audio.addEventListener('ended', () => step(1, { stopAtEnd: true }));
  audio.addEventListener('error', () => {
    if (current) toast('Не удалось воспроизвести файл — возможно, он удалён', { kind: 'error' });
  });
  if ('mediaSession' in navigator) {
    const ms = navigator.mediaSession;
    ms.setActionHandler('play', () => audio.play());
    ms.setActionHandler('pause', () => audio.pause());
    ms.setActionHandler('previoustrack', () => step(-1));
    ms.setActionHandler('nexttrack', () => step(1));
    ms.setActionHandler('seekto', d => { if (d.seekTime != null) audio.currentTime = d.seekTime; });
  }
}

export function play(id) {
  const t = state.byId.get(id);
  if (!t || !t.has_file) return;
  if (current !== id) {
    current = id;
    audio.src = audioUrl(id);
    $('#pl-title').textContent = t.title;
    $('#pl-artist').textContent = t.artist || `@${scAuthor(t)}`;
    const art = $('#pl-art');
    art.setAttribute('style', artStyle(t.artist || t.title));
    art.innerHTML = icon('music');
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: t.title, artist: t.artist || scAuthor(t), album: 'Музыкальный' });
    }
  }
  $('#player').hidden = false;
  audio.play().catch(() => {});
  sync();
}

export function toggle() {
  if (!current) {
    const t = state.byId.get(state.cursor);
    if (t?.has_file) play(t.id);
    return;
  }
  audio.paused ? audio.play().catch(() => {}) : audio.pause();
}

/** Пробел / кнопка в строке: тот же трек — пауза/продолжить, другой — играть его. */
export function toggleTrack(id) {
  if (current === id) toggle();
  else play(id);
}

function step(delta, { stopAtEnd = false } = {}) {
  const items = visibleTracks().filter(t => t.has_file);
  if (!items.length) return;
  let i = items.findIndex(t => t.id === current);
  i += delta;
  if (i < 0) i = 0;
  if (i >= items.length) { if (stopAtEnd) { audio.pause(); return; } i = items.length - 1; }
  play(items[i].id);
}

function close() {
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  current = null;
  $('#player').hidden = true;
  sync();
}

function onTime() {
  if (!seeking && audio.duration) seek.value = String(Math.round((audio.currentTime / audio.duration) * 1000));
  if (!seeking) $('#pl-cur').textContent = fmtTime(audio.currentTime);
  $('#pl-dur').textContent = fmtTime(audio.duration);
}

function sync() {
  const btn = $('#pl-play');
  const playing = isPlaying();
  btn.dataset.icon = playing ? 'pause' : 'play';
  btn.setAttribute('aria-label', playing ? 'Пауза' : 'Играть');
  hydrateIcons(btn.parentElement);
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  emit('player');
}
