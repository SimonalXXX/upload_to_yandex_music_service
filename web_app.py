#!/usr/bin/env python3
"""Web interface for Музыкальный — SoundCloud ↔ Яндекс Музыка."""

from __future__ import annotations

import csv
import json
import queue
import re
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import yaml
import yt_dlp
from flask import Flask, Response, jsonify, request
from yt_dlp.postprocessor.metadataparser import MetadataParserPP

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
TRACKS_PATH = BASE_DIR / "tracks.csv"
TRACKS_JSON_PATH = BASE_DIR / "tracks.json"
ARCHIVE_PATH = BASE_DIR / "archive.txt"
CSV_FIELDS = ["sc_id", "title", "artist", "sc_url", "status", "file", "added"]
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".flac"}
# Незавершённые скачивания yt-dlp (после отмены/сбоя) — подчищаем.
PARTIAL_GLOBS = ("*.part", "*.part-Frag*", "*.ytdl")
# Некоторые заливают трек с названием «… .mp3» — без этого получаются «….mp3.mp3».
TITLE_EXT_CLEANUP_PP = {
    "key": "MetadataParser",
    "when": "pre_process",
    "actions": [(MetadataParserPP.Actions.REPLACE, "title",
                 r"(?i)\.(?:mp3|wav|flac|m4a|aiff?|ogg|opus)$", "")],
}

app = Flask(__name__)

_ALLOWED_HOSTS = {"127.0.0.1:5555", "localhost:5555", "127.0.0.1", "localhost"}


@app.before_request
def _csrf_guard():
    """Защита локального API: Host против DNS rebinding, Origin против CSRF."""
    if (request.host or "").lower() not in _ALLOWED_HOSTS:
        return jsonify({"error": "Недопустимый заголовок Host"}), 403
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc.lower() not in _ALLOWED_HOSTS:
            return jsonify({"error": "Кросс-доменный запрос отклонён"}), 403

# ── Shared state ─────────────────────────────────────────────────────────
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()
_task_lock = threading.Lock()
# RLock: держим его на весь цикл «прочитал → изменил → записал», при этом
# _load_tracks/_save_tracks могут брать его повторно из того же потока.
_db_lock = threading.RLock()
_active_task: str | None = None
# Поток текущей задачи: пока он жив, новую задачу не запускаем — иначе
# _cancel.clear() «оживит» недоостановленный воркер и два потока пишут в CSV.
_worker: threading.Thread | None = None
_cancel = threading.Event()
# None = ещё не проверяли; False = браузер/профиль не найден, cookies не используем
_browser_cookies_ok: bool | None = None


def _is_cookie_db_error(err: str) -> bool:
    return "cookies database" in err.lower()


def _norm(s: str | None) -> str:
    """Нормализация названия/артиста для сверки с плейлистом ЯМ."""
    return " ".join((s or "").lower().replace("ё", "е").split())


# ── Config helpers ───────────────────────────────────────────────────────

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _music_dir(cfg: dict | None = None) -> Path:
    """Папка музыки. Относительный путь из конфига — от папки проекта, а не от cwd."""
    if cfg is None:
        cfg = _load_config()
    p = Path(cfg.get("output", {}).get("directory", "./Music")).expanduser()
    return (p if p.is_absolute() else BASE_DIR / p).resolve()


def _profile_url(value: str) -> str:
    """Имя или любая ссылка на профиль SoundCloud → URL страницы лайков."""
    v = value.strip()
    m = re.search(r"soundcloud\.com/([^/?#\s]+)", v)
    user = m.group(1) if m else v.strip("/@ ")
    return f"https://soundcloud.com/{user}/likes"


def _abs_file(stored: str | None) -> str | None:
    """Путь из CSV → абсолютный. В CSV храним относительно папки проекта,
    чтобы база переживала перенос папки (старые абсолютные пути тоже читаются)."""
    if not stored:
        return None
    p = Path(stored)
    return str(p if p.is_absolute() else BASE_DIR / p)


def _rel_file(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if p.is_absolute() and p.is_relative_to(BASE_DIR):
        return str(p.relative_to(BASE_DIR))
    return str(p)


def _cleanup_partials(music_dir: Path | None = None) -> None:
    """Удаляет недокачанные файлы yt-dlp — после отмены они остаются мусором в папке.

    Заодно — обложку недокачанного трека («X.m4a.part» → «X.jpg»), если готового
    аудиофайла с тем же именем нет: обложку вшивает последний шаг, до него не дошли.
    """
    music_dir = music_dir or _music_dir()
    if not music_dir.exists():
        return
    stems: set[str] = set()
    for pattern in PARTIAL_GLOBS:
        for f in music_dir.glob(pattern):
            stems.add(Path(f.name.split(".part")[0].removesuffix(".ytdl")).stem)
            f.unlink(missing_ok=True)
    for stem in stems:
        if any((music_dir / f"{stem}{ext}").exists() for ext in AUDIO_EXTS):
            continue
        for ext in (".jpg", ".webp", ".png"):
            (music_dir / f"{stem}{ext}").unlink(missing_ok=True)


# ── Tracks DB (CSV) ──────────────────────────────────────────────────────
# Key = SoundCloud numeric ID (str).
# Value = {title, artist, sc_url, status, file, added}
# status: "pending" | "downloaded" | "uploaded" | "not_in_likes" (нет в текущем списке лайков SC)

def _load_tracks() -> dict:
    # Ошибку чтения не глотаем: вернуть {} значило бы, что следующий
    # _save_tracks перезапишет всю базу одной-двумя записями.
    with _db_lock:
        if not TRACKS_PATH.exists():
            return {}
        result: dict = {}
        with open(TRACKS_PATH, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                sc_id = row.get("sc_id", "")
                if not sc_id:
                    continue
                result[sc_id] = {
                    "title": row.get("title", ""),
                    "artist": row.get("artist", ""),
                    "sc_url": row.get("sc_url", ""),
                    "status": row.get("status", "pending"),
                    "file": _abs_file(row.get("file")),
                    "added": row.get("added", ""),
                }
        return result


def _save_tracks(data: dict) -> None:
    with _db_lock:
        tmp = TRACKS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for sc_id, info in data.items():
                row = {"sc_id": sc_id, **info}
                row["file"] = _rel_file(row.get("file"))
                writer.writerow(row)
        tmp.replace(TRACKS_PATH)


def _track_update(sc_id: str, **fields) -> None:
    with _db_lock:
        tracks = _load_tracks()
        entry = tracks.get(sc_id, {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        tracks[sc_id] = entry
        _save_tracks(tracks)


def _migrate_json_to_csv() -> None:
    """One-time migration: convert tracks.json → tracks.csv."""
    if not TRACKS_JSON_PATH.exists() or TRACKS_PATH.exists():
        return
    try:
        with open(TRACKS_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        with open(TRACKS_PATH, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for sc_id, info in data.items():
                row = {"sc_id": sc_id, **info}
                row["file"] = row.get("file") or ""
                writer.writerow(row)
        TRACKS_JSON_PATH.rename(TRACKS_JSON_PATH.with_suffix(".json.bak"))
    except Exception:
        pass


def _cleanup_stale_paths() -> None:
    """Чинит пути к файлам в базе при старте.

    Если файл не найден по сохранённому пути (например, папку проекта перенесли,
    а в CSV остался старый абсолютный путь) — ищем его по имени в папке музыки;
    не нашли — очищаем путь. Сохранение заодно переводит пути в относительные.
    """
    music_dir = _music_dir()
    with _db_lock:
        tracks = _load_tracks()
        for info in tracks.values():
            fpath = info.get("file")
            if fpath and not Path(fpath).exists():
                moved = music_dir / Path(fpath).name
                info["file"] = str(moved) if moved.exists() else None
        if tracks:
            _save_tracks(tracks)


def _sync_archive() -> None:
    """Merge tracks DB with archive.txt — never remove entries yt-dlp already wrote."""
    with _db_lock:
        existing: set[str] = set()
        if ARCHIVE_PATH.exists():
            with open(ARCHIVE_PATH, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        existing.add(stripped)

        tracks = _load_tracks()
        changed = False
        for sc_id, info in tracks.items():
            key = f"soundcloud {sc_id}"
            if info.get("status") in ("downloaded", "uploaded"):
                existing.add(key)
            elif key in existing and info.get("status") in ("pending", "not_in_likes"):
                fpath = info.get("file")
                if fpath and Path(fpath).exists():
                    tracks[sc_id]["status"] = "downloaded"
                    changed = True
                else:
                    existing.discard(key)
        if changed:
            _save_tracks(tracks)

        with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
            for entry in sorted(existing):
                f.write(entry + "\n")


def _remove_from_archive(sc_id: str) -> None:
    """Убирает id из archive.txt, чтобы yt-dlp не пропустил повторное скачивание."""
    if not ARCHIVE_PATH.exists():
        return
    key = f"soundcloud {sc_id}"
    with _db_lock:
        raw = [ln.strip() for ln in ARCHIVE_PATH.read_text(encoding="utf-8").splitlines()]
        if key not in raw:
            return
        lines = [ln for ln in raw if ln and ln != key]
        ARCHIVE_PATH.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )


def _delete_file_safe(filepath: str | None) -> None:
    if not filepath:
        return
    p = Path(filepath)
    if p.exists():
        p.unlink(missing_ok=True)
    music_dir = _music_dir()
    try:
        parent = p.parent.resolve()
        # Подчищаем только пустые подпапки ВНУТРИ папки музыки — иначе при
        # смене output.directory цикл ушёл бы удалять папки вверх до корня.
        while (
            parent != music_dir
            and parent.is_relative_to(music_dir)
            and parent.exists()
            and not any(parent.iterdir())
        ):
            parent.rmdir()
            parent = parent.parent
    except OSError:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────

def _broadcast(event: dict) -> None:
    data = json.dumps(event, ensure_ascii=False)
    with _clients_lock:
        dead: list[queue.Queue] = []
        for q in _clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


def _music_stats() -> dict:
    tracks = _load_tracks()
    total = len(tracks)
    uploaded = sum(1 for t in tracks.values() if t.get("status") == "uploaded")
    downloaded = sum(1 for t in tracks.values() if t.get("status") == "downloaded")
    pending = sum(1 for t in tracks.values() if t.get("status") == "pending")
    not_in_likes = sum(1 for t in tracks.values() if t.get("status") == "not_in_likes")
    music_dir = _music_dir()
    size = 0
    local_files = 0
    if music_dir.exists():
        for f in music_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                size += f.stat().st_size
                local_files += 1
    return {
        "total": total,
        "uploaded": uploaded,
        "downloaded": downloaded,
        "pending": pending,
        "not_in_likes": not_in_likes,
        "local_files": local_files,
        "size_mb": round(size / (1024 * 1024), 1),
    }


def _extract_username(profile_url: str) -> str:
    if not profile_url:
        return ""
    parts = profile_url.rstrip("/").split("/")
    for i, p in enumerate(parts):
        if "soundcloud.com" in p and i + 1 < len(parts):
            return parts[i + 1]
    return profile_url


def _set_task(name: str | None) -> None:
    global _active_task
    with _task_lock:
        _active_task = name


def _start_task(name: str, target, *args) -> str | None:
    """Запускает фоновую задачу. Возвращает текст ошибки, если что-то уже выполняется
    (в т.ч. если прошлый воркер ещё не доостановился после «Остановить»)."""
    global _active_task, _worker
    with _task_lock:
        if _active_task:
            return f"Уже выполняется: {_active_task}"
        if _worker is not None and _worker.is_alive():
            return "Предыдущая задача ещё останавливается — подождите пару секунд"
        _active_task = name
        _cancel.clear()
        _worker = threading.Thread(target=target, args=args, daemon=True)
        _worker.start()
    return None


class _SilentLogger:
    def debug(self, msg: str) -> None: pass
    def info(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str) -> None: pass


# ── Scan worker ──────────────────────────────────────────────────────────

def _scan_worker(config: dict) -> None:
    global _browser_cookies_ok
    try:
        sc = config.get("soundcloud", {})
        target_url = sc.get("profile_url", "")
        if not target_url:
            _broadcast({"type": "error", "message": "URL профиля не задан"})
            return

        _broadcast({"type": "scanning"})

        flat_opts: dict = {
            "format": "bestaudio/best",
            "extract_flat": True,
            "skip_download": True,
            "quiet": True,
            "no_color": True,
            "logger": _SilentLogger(),
        }
        cookies_browser = sc.get("cookies_browser")
        if cookies_browser and _browser_cookies_ok is not False:
            flat_opts["cookiesfrombrowser"] = (cookies_browser,)

        max_tracks = sc.get("max_tracks", 0)
        if max_tracks and max_tracks > 0:
            flat_opts["playlistend"] = max_tracks

        def _flat_extract() -> list[dict]:
            with yt_dlp.YoutubeDL(flat_opts) as ydl:
                info = ydl.extract_info(target_url, download=False)
                if info and "entries" in info:
                    return [e for e in info["entries"] if e]
            return []

        entries: list[dict] = []
        try:
            entries = _flat_extract()
        except Exception as e:
            # Браузер не установлен / профиль не найден — пробуем без cookies:
            # для публичных лайков они и не нужны.
            if _is_cookie_db_error(str(e)) and "cookiesfrombrowser" in flat_opts:
                _browser_cookies_ok = False
                flat_opts.pop("cookiesfrombrowser", None)
                _broadcast({"type": "log", "level": "warn", "message":
                            f"Cookies браузера «{cookies_browser}» недоступны "
                            "(браузер не установлен?) — сканирую без них"})
                try:
                    entries = _flat_extract()
                except Exception as e2:
                    _broadcast({"type": "error", "message": f"Ошибка сканирования: {e2!s:.200}"})
                    return
            else:
                _broadcast({"type": "error", "message": f"Ошибка сканирования: {e!s:.200}"})
                return

        if not entries:
            # Пустой ответ без исключения (сбой SoundCloud, опечатка в имени) не должен
            # переводить все ожидающие треки в «пропал из лайков».
            _broadcast({"type": "error", "message":
                        "SoundCloud вернул пустой список лайков — статусы не изменены. "
                        "Проверьте имя пользователя и повторите."})
            return

        with _db_lock:
            tracks = _load_tracks()
            new_entries: list[dict] = []
            first_seen_count = 0
            for e in entries:
                sc_id = str(e.get("id", ""))
                if not sc_id:
                    continue
                prev = tracks.get(sc_id)
                sc_url = e.get("url") or e.get("webpage_url") or (prev or {}).get("sc_url", "")
                if prev and prev.get("status") in ("downloaded", "uploaded"):
                    if not prev.get("title"):
                        prev.update(
                            title=e.get("title") or "",
                            artist=e.get("uploader") or prev.get("artist", ""),
                            sc_url=sc_url,
                        )
                else:
                    new_entries.append(e)
                    if prev is None:
                        first_seen_count += 1
                    # Плоский скан не отдаёт исполнителя — не затираем уже известного.
                    tracks[sc_id] = {
                        "title": e.get("title") or (prev or {}).get("title", ""),
                        "artist": e.get("uploader") or (prev or {}).get("artist", ""),
                        "sc_url": sc_url,
                        "status": "pending",
                        "file": (prev or {}).get("file"),
                        "added": (prev or {}).get("added") or time.strftime("%Y-%m-%d"),
                    }
            not_in_likes_n = 0
            # С лимитом max_tracks список неполный — «пропавшие» за лимитом не пропали.
            if not (max_tracks and max_tracks > 0):
                seen_ids = {str(e.get("id", "")) for e in entries}
                for sid, info in tracks.items():
                    if info.get("status") == "pending" and sid not in seen_ids and not info.get("file"):
                        info["status"] = "not_in_likes"
                        not_in_likes_n += 1

            _save_tracks(tracks)

        all_display = []
        for e in entries:
            sc_id = str(e.get("id", ""))
            if not sc_id:
                continue
            info = tracks.get(sc_id, {})
            all_display.append({
                "id": sc_id,
                "title": info.get("title") or e.get("title") or sc_id,
                "artist": info.get("artist") or e.get("uploader") or "",
                "status": info.get("status", "pending"),
            })
        # Треки из базы, которых уже нет в лайках (в т.ч. загруженные в ЯМ),
        # не должны пропадать из списка после скана.
        shown = {t["id"] for t in all_display}
        for sid, info in reversed(list(tracks.items())):
            if sid not in shown:
                all_display.append({
                    "id": sid,
                    "title": info.get("title", "") or sid,
                    "artist": info.get("artist", ""),
                    "status": info.get("status", "pending"),
                })

        _broadcast({
            "type": "scan_complete",
            "total": len(entries),
            "new_count": first_seen_count,
            "already": len(entries) - len(new_entries),
            "not_in_likes": not_in_likes_n,
            "all_tracks": all_display,
        })

    except Exception as e:
        _broadcast({"type": "error", "message": str(e)[:300]})
    finally:
        _set_task(None)


# ── Pipeline worker: download → upload ───────────────────────────────────

def _pipeline_worker(config: dict, do_upload: bool = True, track_ids: list | None = None) -> None:
    """Скачивает треки из локальной базы (tracks.csv) и, если нужно, грузит в ЯМ.

    Работает по базе, а не по результату последнего скана: после перезапуска
    приложения скачивание доступно сразу. track_ids=None — все «pending».
    """
    global _browser_cookies_ok
    ydl = None
    try:
        sc = config.get("soundcloud", {})
        out = config.get("output", {})

        output_dir = _music_dir(config)
        output_dir.mkdir(parents=True, exist_ok=True)
        audio_fmt = out.get("format", "mp3")
        quality = out.get("quality", "320")
        template = str(output_dir / "%(uploader)s - %(title)s.%(ext)s")

        existing_tracks = _load_tracks()
        if track_ids is None:
            selected = [sid for sid, t in existing_tracks.items() if t.get("status") == "pending"]
        else:
            selected = [str(tid) for tid in track_ids]
        unknown = [sid for sid in selected if sid not in existing_tracks]
        if unknown:
            _broadcast({"type": "log", "level": "warn",
                        "message": f"Нет в базе, пропущено: {len(unknown)} — обновите список лайков"})
        selected = [sid for sid in selected if sid in existing_tracks]
        if not selected:
            _broadcast({"type": "error", "message": "Нет выбранных треков"})
            return

        total = len(selected)
        processed = 0
        saved_count = 0
        errors: list[str] = []
        download_failures: list[str] = []
        last_filepath: list[str | None] = [None]
        last_meta: list[dict] = [{}]
        current_id: list[str | None] = [None]

        _sync_archive()

        _broadcast({"type": "dl_start", "total": total})

        last_emit = [0.0]

        def _short(s: str) -> str:
            return (s[:55] + "…") if len(s) > 55 else s

        def _hook(d: dict) -> None:
            # Именно DownloadCancelled: обычный DownloadError загрузчик HLS-фрагментов
            # глотает как сбой одного куска и идёт дальше — отмена не срабатывала.
            if _cancel.is_set():
                raise yt_dlp.utils.DownloadCancelled("Отменено")
            status = d.get("status")
            if status == "downloading":
                now = time.monotonic()
                if now - last_emit[0] < 0.3:
                    return  # не заливаем SSE событием на каждый чанк
                last_emit[0] = now
            elif status != "finished":
                return
            # «finished» — скачан исходник, дальше ffmpeg; трек ещё НЕ готов,
            # поэтому это тоже downloading (со stage=convert), а не track_done.
            _broadcast({
                "type": "downloading",
                "id": current_id[0],
                "title": _short(Path(d.get("filename", "")).stem),
                "stage": "convert" if status == "finished" else "download",
                "downloaded": processed,
                "saved": saved_count,
                "total": total,
            })

        def _pp_hook(d: dict) -> None:
            if d.get("status") == "started" and _cancel.is_set():
                raise yt_dlp.utils.DownloadCancelled("Отменено")  # не начинаем следующий шаг ffmpeg
            if d.get("status") == "finished":
                info = d.get("info_dict", {})
                last_filepath[0] = info.get("filepath") or info.get("filename")
                last_meta[0] = {"uploader": info.get("uploader", ""), "title": info.get("title", "")}

        class _Logger:
            _IGNORED = ("Deprecated Feature:",)

            def debug(self, msg: str) -> None: pass
            def info(self, msg: str) -> None: pass
            def warning(self, msg: str) -> None: pass
            def error(self, msg: str) -> None:
                if not any(p in msg for p in self._IGNORED):
                    errors.append(msg)

        dl_opts: dict = {
            "format": "bestaudio/best",
            "outtmpl": template,
            "restrictfilenames": False,
            "windowsfilenames": True,
            "ignoreerrors": True,
            "quiet": True,
            "no_color": True,
            "sleep_requests": sc.get("sleep_requests", 1.5),
            "download_archive": str(ARCHIVE_PATH),
            "postprocessors": [
                TITLE_EXT_CLEANUP_PP,
                {"key": "FFmpegExtractAudio", "preferredcodec": audio_fmt, "preferredquality": quality},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True,
            "embedthumbnail": True,
            "progress_hooks": [_hook],
            "postprocessor_hooks": [_pp_hook],
            "logger": _Logger(),
        }
        cookies_browser = sc.get("cookies_browser")
        if cookies_browser and _browser_cookies_ok is not False:
            dl_opts["cookiesfrombrowser"] = (cookies_browser,)

        def _fail(sc_id: str, message: str, title: str) -> None:
            download_failures.append(title)
            _broadcast({"type": "log", "id": sc_id, "level": "error", "message": message})

        def _done(sc_id: str, title: str, status: str) -> None:
            _broadcast({
                "type": "track_done",
                "id": sc_id,
                "title": _short(title),
                "status": status,
                "downloaded": processed,
                "saved": saved_count,
                "total": total,
            })

        ydl = yt_dlp.YoutubeDL(dl_opts)
        for sc_id in selected:
            if _cancel.is_set():
                break
            entry = existing_tracks[sc_id]
            title_hint = (entry.get("title") or sc_id)[:80]
            prev_status = entry.get("status")
            # Перекачка уже отправленного в ЯМ трека не должна сбрасывать «uploaded».
            new_status = "uploaded" if prev_status == "uploaded" else "downloaded"
            current_id[0] = sc_id

            existing_file = entry.get("file")
            if existing_file and Path(existing_file).exists():
                processed += 1
                saved_count += 1
                if prev_status != new_status:
                    _track_update(sc_id, status=new_status)
                _done(sc_id, title_hint, new_status)
                continue

            # Файла нет (например, удалён после загрузки в ЯМ), но id мог
            # остаться в archive.txt — уберём, иначе yt-dlp молча пропустит
            # трек, а UI покажет «Файл не появился после скачивания».
            _remove_from_archive(sc_id)

            url = entry.get("sc_url", "")
            if not url:
                processed += 1
                _fail(sc_id, f"Нет URL для трека: {title_hint}", title_hint)
                continue
            last_filepath[0] = None
            last_meta[0] = {}
            errors_before = len(errors)
            dl_err: str | None = None
            try:
                ydl.download([url])
            except Exception as e:
                if _cancel.is_set():
                    break  # отмена — не считаем её ошибкой скачивания
                if _is_cookie_db_error(str(e)) and "cookiesfrombrowser" in dl_opts:
                    # Браузер не найден — пересоздаём загрузчик без cookies
                    # и повторяем этот же трек.
                    _browser_cookies_ok = False
                    dl_opts.pop("cookiesfrombrowser", None)
                    ydl.close()
                    ydl = yt_dlp.YoutubeDL(dl_opts)
                    _broadcast({"type": "log", "level": "warn", "message":
                                "Cookies браузера недоступны — скачиваю без них"})
                    try:
                        ydl.download([url])
                    except Exception as e2:
                        dl_err = str(e2)[:280]
                else:
                    dl_err = str(e)[:280]
            if _cancel.is_set():
                break
            processed += 1
            fpath = last_filepath[0]
            meta = last_meta[0]
            if fpath and Path(fpath).exists():
                title = meta.get("title") or entry.get("title", "")
                _track_update(
                    sc_id,
                    status=new_status,
                    title=title,
                    artist=meta.get("uploader") or entry.get("artist", ""),
                    sc_url=url,
                    file=str(Path(fpath).resolve()),
                )
                saved_count += 1
                _done(sc_id, title or title_hint, new_status)
            else:
                reason = dl_err or next(iter(errors[errors_before:][-1:]), "")
                if "DRM protected" in reason:
                    _fail(sc_id, f"{title_hint}: трек защищён DRM (SoundCloud Go+) — "
                                 "скачать нельзя", title_hint)
                elif dl_err is not None:
                    _fail(sc_id, f"Ошибка скачивания ({title_hint}): {dl_err}", title_hint)
                else:
                    detail = f": {reason[:200]}" if reason else ""
                    _fail(sc_id, f"Файл не появился после скачивания ({title_hint}){detail}",
                          title_hint)

        if _cancel.is_set():
            _broadcast({"type": "cancelled", "downloaded": saved_count})
            return

        _broadcast({
            "type": "dl_complete",
            "downloaded": saved_count,
            "errors": len(errors) + len(download_failures),
            "failures": len(download_failures),
        })

        if do_upload:
            _upload_phase(config, saved_count, track_ids=selected)
        else:
            _broadcast({
                "type": "all_done",
                "downloaded": saved_count,
                "uploaded": 0,
                "failures": len(download_failures),
            })

    except Exception as e:
        _broadcast({"type": "error", "message": str(e)[:300]})
    finally:
        if ydl is not None:
            ydl.close()
        try:
            _cleanup_partials()
        except OSError:
            pass
        _set_task(None)


def _upload_phase(config: dict, dl_count: int = 0, track_ids: list | None = None) -> None:
    """Загрузка треков в ЯМ; пишет upload_archive.txt как CLI.

    Если передан track_ids — грузятся именно эти треки (даже уже отмеченные «uploaded»,
    выбор пользователя в UI имеет приоритет). Иначе — все со статусом «downloaded»
    (пакетная загрузка из левой панели, без привязки к выбору в списке).
    Локальные файлы удаляются только при yandex_music.delete_after_upload: true в config.yaml.
    """
    ym = config.get("yandex_music", {})
    playlist_url = ym.get("playlist_url", "")
    if not playlist_url:
        _broadcast({"type": "all_done", "downloaded": dl_count, "uploaded": 0, "level": "warn",
                     "message": "URL плейлиста ЯМ не задан — загрузка пропущена."})
        return

    try:
        from ym_uploader import (
            DEFAULT_UPLOAD_ARCHIVE,
            _create_session,
            _get_auth,
            _record_uploaded,
            _resolve_playlist_kind,
            _upload_one,
            get_playlist_tracks,
        )
    except ImportError:
        _broadcast({"type": "all_done", "downloaded": dl_count, "uploaded": 0, "level": "error",
                     "message": "Модуль ym_uploader не найден"})
        return

    tracks = _load_tracks()
    if track_ids is not None:
        selected = {str(tid) for tid in track_ids}
        to_upload = [
            (sid, info) for sid, info in tracks.items()
            if sid in selected and info.get("file") and Path(info["file"]).exists()
        ]
    else:
        to_upload = [
            (sid, info) for sid, info in tracks.items()
            if info.get("status") == "downloaded" and info.get("file") and Path(info["file"]).exists()
        ]

    if not to_upload:
        _broadcast({"type": "all_done", "downloaded": dl_count, "uploaded": 0, "level": "warn",
                     "message": "Нет треков для загрузки в ЯМ"})
        return

    _broadcast({"type": "upload_start", "total": len(to_upload), "skipped": 0})

    try:
        session = _create_session()
        auth = _get_auth(session)
        if not auth.get("logged"):
            _broadcast({"type": "all_done", "downloaded": dl_count, "uploaded": 0, "level": "error",
                         "message": auth.get("error", "Не авторизованы в ЯМ")})
            return
        token = auth["token"]
        uid = auth["uid"]
        kind = _resolve_playlist_kind(session, playlist_url, token, uid)
        if not kind:
            _broadcast({"type": "all_done", "downloaded": dl_count, "uploaded": 0, "level": "error",
                         "message": "Не удалось определить плейлист"})
            return
    except Exception as e:
        _broadcast({"type": "all_done", "downloaded": dl_count, "uploaded": 0, "level": "error",
                     "message": str(e)[:200]})
        return

    uploaded = 0
    upload_errors = 0
    upload_delay = ym.get("upload_delay", 3)
    del_after = bool(ym.get("delete_after_upload", False))
    accepted: list[str] = []  # приняты сервером, но ещё не подтверждены в плейлисте

    for sc_id, info in to_upload:
        if _cancel.is_set():
            break
        filepath = Path(info["file"])
        name = filepath.stem
        _broadcast({
            "type": "uploading",
            "id": sc_id,
            "title": (name[:50] + "…") if len(name) > 50 else name,
            "uploaded": uploaded,
            "total": len(to_upload),
        })
        try:
            res = _upload_one(session, uid, kind, filepath, token, cancel_event=_cancel)
            if res:
                uploaded += 1
                _record_uploaded(DEFAULT_UPLOAD_ARCHIVE, filepath)
                _track_update(sc_id, status="uploaded")
                if res == "accepted":
                    accepted.append(sc_id)
                    _broadcast({"type": "log", "id": sc_id, "level": "warn",
                                 "message": f"{name}: файл принят, ЯМ ещё обрабатывает — "
                                            "проверю плейлист после загрузки"})
                # Удаляем только при точном подтверждении по id трека: «grown» (плейлист
                # вырос — мог вырасти и за счёт прошлого дообработанного трека) и
                # «accepted» не гарантируют, что доехал именно этот файл.
                if del_after and res is True:
                    _delete_file_safe(str(filepath.resolve()))
                    _track_update(sc_id, file="")
                _broadcast({"type": "track_uploaded", "id": sc_id})
            else:
                upload_errors += 1
                _broadcast({"type": "log", "id": sc_id, "level": "error",
                             "message": f"{name}: не удалось загрузить"})
        except Exception as e:
            upload_errors += 1
            _broadcast({"type": "log", "id": sc_id, "level": "error", "message": f"{name}: {e!s:.100}"})
        if _cancel.wait(upload_delay):
            break  # «Остановить» прерывает и паузу между загрузками

    if _cancel.is_set():
        _broadcast({"type": "cancelled", "downloaded": dl_count, "uploaded": uploaded})
        return

    # ── Автопроверка: дожидаемся появления принятых треков в плейлисте ──
    if accepted:
        _broadcast({"type": "log", "level": "info",
                     "message": f"Проверяю плейлист: жду появления {len(accepted)} трек(ов)…"})
        tracks_db = _load_tracks()
        pending_ids = set(accepted)
        try:
            for _ in range(6):  # до ~1 минуты
                if _cancel.wait(10):
                    break
                pl_tracks = get_playlist_tracks(session, uid, kind, token)
                keys = {(_norm(t["artist"]), _norm(t["title"])) for t in pl_tracks}
                titles = {_norm(t["title"]) for t in pl_tracks if t["title"]}
                for sid in list(pending_ids):
                    info = tracks_db.get(sid, {})
                    k = (_norm(info.get("artist")), _norm(info.get("title")))
                    if k in keys or (k[1] and k[1] in titles):
                        pending_ids.discard(sid)
                        _broadcast({"type": "log", "id": sid, "level": "info",
                                     "message": f"{(info.get('title') or sid)[:50]}: "
                                                "появился в плейлисте ✓"})
                if not pending_ids:
                    break
        except Exception:
            pass  # сверка — не повод ронять итог загрузки
        if pending_ids:
            _broadcast({"type": "log", "level": "warn",
                         "message": f"Пока не видны в плейлисте: {len(pending_ids)} — "
                                    "нажмите «Сверить плейлист» через пару минут"})

    _broadcast({
        "type": "all_done",
        "downloaded": dl_count,
        "uploaded": uploaded,
        "upload_errors": upload_errors,
    })


# ── Upload-only worker ───────────────────────────────────────────────────

def _upload_only_worker(config: dict) -> None:
    try:
        _upload_phase(config)
    except Exception as e:
        _broadcast({"type": "error", "message": str(e)[:300]})
    finally:
        _set_task(None)


# ── Mark-all-uploaded worker ─────────────────────────────────────────────

def _mark_uploaded(track_ids: list[str] | None = None, *, mark_all_pending: bool = False) -> int:
    """Помечает выбранные (или все pending / downloaded / not_in_likes) как уже в ЯМ.

    Пути существующих файлов дописываются в upload_archive.txt, чтобы CLI
    (sc_downloader.py --upload) не залил их повторно.
    """
    marked_files: list[Path] = []
    with _db_lock:
        tracks = _load_tracks()
        if mark_all_pending:
            target = {
                sid for sid, inf in tracks.items()
                if inf.get("status") in ("pending", "downloaded", "not_in_likes")
            }
        elif track_ids is not None:
            target = {str(tid) for tid in track_ids}
        else:
            return 0
        count = 0
        for sid, info in tracks.items():
            if info.get("status") in ("pending", "downloaded", "not_in_likes"):
                if sid not in target:
                    continue
                info["status"] = "uploaded"
                count += 1
                fpath = info.get("file")
                if fpath and Path(fpath).exists():
                    marked_files.append(Path(fpath))
        _save_tracks(tracks)
    _sync_archive()
    if marked_files:
        try:
            from ym_uploader import DEFAULT_UPLOAD_ARCHIVE, _record_uploaded
            for fp in marked_files:
                _record_uploaded(DEFAULT_UPLOAD_ARCHIVE, fp)
        except ImportError:
            pass
    return count


# ── Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/status")
def api_status():
    cfg = _load_config()
    sc = cfg.get("soundcloud", {})
    ym = cfg.get("yandex_music", {})
    return jsonify({
        "username": _extract_username(sc.get("profile_url", "")),
        "playlist_url": ym.get("playlist_url", ""),
        "stats": _music_stats(),
        "active_task": _active_task,
    })


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json(silent=True) or {}
    cfg = _load_config()
    username = (data.get("username") or "").strip()
    if username:
        cfg.setdefault("soundcloud", {})["profile_url"] = _profile_url(username)
    playlist_url = data.get("playlist_url")
    if playlist_url is not None:
        cfg.setdefault("yandex_music", {})["playlist_url"] = str(playlist_url).strip()
    _save_config(cfg)
    return jsonify({"ok": True})


def _started(err: str | None):
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    return _started(_start_task("scan", _scan_worker, _load_config()))


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    do_upload = bool(data.get("upload", True))
    track_ids = data.get("track_ids")
    if track_ids is not None and not isinstance(track_ids, list):
        return jsonify({"error": "track_ids должен быть списком"}), 400
    return _started(_start_task("pipeline", _pipeline_worker, _load_config(), do_upload, track_ids))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    return _started(_start_task("upload", _upload_only_worker, _load_config()))


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Только сигнал отмены — active_task сбрасывает сам воркер в finally."""
    _cancel.set()
    return jsonify({"ok": True})


@app.route("/api/force-clear", methods=["POST"])
def api_force_clear():
    """Аварийный сброс «зависшего» active_task. Пока поток задачи жив — отказываем:
    иначе следующая задача стартует параллельно ещё работающему воркеру."""
    global _active_task
    _cancel.set()
    with _task_lock:
        if _worker is not None and _worker.is_alive():
            return jsonify({"ok": False, "alive": True,
                            "error": "Задача ещё останавливается"}), 409
        old = _active_task
        _active_task = None
    return jsonify({"ok": True, "cleared": old})


@app.route("/api/mark-uploaded", methods=["POST"])
def api_mark_uploaded():
    """Помечает треки как уже в ЯМ: по списку track_ids или все новые при mark_all_pending: true."""
    data = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids")
    mark_all_pending = bool(data.get("mark_all_pending"))
    # Работаем не выпуская _task_lock: иначе между проверкой _active_task
    # и записью CSV успел бы стартовать scan/pipeline (TOCTOU).
    with _task_lock:
        if _active_task:
            return jsonify({"error": "Дождитесь завершения текущей задачи"}), 409
        if mark_all_pending:
            count = _mark_uploaded(mark_all_pending=True)
        elif track_ids is not None and len(track_ids) > 0:
            count = _mark_uploaded(track_ids)
        else:
            return jsonify({"error": "Передайте track_ids или mark_all_pending: true"}), 400
    return jsonify({"ok": True, "marked": count})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    music_dir = _music_dir()
    music_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["open", str(music_dir)])
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"ok": True})


@app.route("/api/check-yandex")
def api_check_yandex():
    try:
        from ym_uploader import _create_session, _get_auth
        session = _create_session()
        auth = _get_auth(session)
        if auth.get("logged"):
            return jsonify({
                "authorized": True,
                "login": auth.get("login", "?"),
                "uid": auth.get("uid"),
            })
        return jsonify({
            "authorized": False,
            "error": auth.get("error", "Войдите в music.yandex.ru в Chrome"),
        })
    except Exception as e:
        err = str(e)
        if "404" in err:
            err = (
                "Старый API Яндекс Музыки недоступен (404). "
                "Обновите ym_uploader.py или перезапустите приложение."
            )
        return jsonify({"authorized": False, "error": err[:300]})


@app.route("/api/tracks")
def api_tracks():
    """Все треки из локальной базы — чтобы список был виден сразу, без скана SC.

    Порядок: последние добавленные сверху (в CSV новые дописываются в конец).
    """
    tracks = _load_tracks()
    out = [
        {
            "id": sid,
            "title": info.get("title", "") or sid,
            "artist": info.get("artist", ""),
            "status": info.get("status", "pending"),
        }
        for sid, info in reversed(list(tracks.items()))
    ]
    return jsonify({"tracks": out})


@app.route("/api/check-playlist", methods=["POST"])
def api_check_playlist():
    """Сверяет локальную базу (uploaded-треки) с реальным содержимым плейлиста ЯМ."""
    cfg = _load_config()
    playlist_url = cfg.get("yandex_music", {}).get("playlist_url", "")
    if not playlist_url:
        return jsonify({"error": "URL плейлиста не задан"}), 400
    try:
        from ym_uploader import (
            _create_session, _get_auth, _resolve_playlist_kind, get_playlist_tracks,
        )
        session = _create_session()
        auth = _get_auth(session)
        if not auth.get("logged"):
            return jsonify({"error": auth.get("error", "Не авторизованы в ЯМ")}), 401
        kind = _resolve_playlist_kind(session, playlist_url, auth["token"], auth["uid"])
        if not kind:
            return jsonify({"error": "Не удалось определить плейлист"}), 400
        pl_tracks = get_playlist_tracks(session, auth["uid"], kind, auth["token"])
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 502

    from collections import Counter
    pl_keys = [(_norm(t["artist"]), _norm(t["title"])) for t in pl_tracks]
    key_set = set(pl_keys)
    titles = {k[1] for k in pl_keys if k[1]}
    dup_count = sum(c - 1 for k, c in Counter(pl_keys).items() if c > 1 and k != ("", ""))
    processing = sum(1 for t in pl_tracks if not t["available"])

    tracks = _load_tracks()
    found = 0
    missing: list[dict] = []
    for sid, info in tracks.items():
        if info.get("status") != "uploaded":
            continue
        k = (_norm(info.get("artist")), _norm(info.get("title")))
        if k in key_set or (k[1] and k[1] in titles):
            found += 1
        else:
            missing.append({"id": sid, "title": info.get("title", ""),
                            "artist": info.get("artist", "")})
    return jsonify({
        "playlist_total": len(pl_tracks),
        "found": found,
        "missing_total": len(missing),
        "missing": missing[:30],
        "duplicates": dup_count,
        "processing": processing,
    })


@app.route("/api/progress")
def api_progress():
    q: queue.Queue = queue.Queue(maxsize=256)
    # Первое событие — текущее состояние: после сна/обрыва связи клиент
    # переподключается и может понять, что задача уже закончилась.
    q.put_nowait(json.dumps({"type": "hello", "active_task": _active_task}))
    with _clients_lock:
        _clients.append(q)

    def stream():
        try:
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except GeneratorExit:
            pass
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── HTML ─────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Музыкальный</title>
<style>
:root{
  --bg:#0a0a12;--surface:#12122a;--surface-2:#171736;--border:#1e1e44;
  --primary:#8b5cf6;--primary-h:#a78bfa;--primary-glow:rgba(139,92,246,.25);
  --ok:#10b981;--ok-bg:rgba(16,185,129,.14);
  --err:#f43f5e;--err-bg:rgba(244,63,94,.14);
  --warn:#f59e0b;--warn-bg:rgba(245,158,11,.14);
  --info:#38bdf8;--info-bg:rgba(56,189,248,.14);
  --text:#e2e8f0;--dim:#7b88a8;--faint:#4b5573;--input:#0d0d1f;
  --radius:12px;--radius-sm:8px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  background:var(--bg);color:var(--text);height:100vh;line-height:1.5;overflow:hidden}
::selection{background:var(--primary-glow)}

.shell{max-width:1180px;margin:0 auto;padding:16px 24px 12px;
  display:flex;flex-direction:column;gap:14px;height:100vh}

.topbar{display:flex;align-items:baseline;justify-content:center;gap:10px;flex-shrink:0}
.topbar h1{font-size:21px;font-weight:800;letter-spacing:-.01em;
  background:linear-gradient(135deg,#8b5cf6,#ec4899);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar span{font-size:12px;color:var(--dim)}

.layout{display:grid;grid-template-columns:296px 1fr;gap:14px;min-height:0;flex:1}

.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px;flex-shrink:0}
.rail{display:flex;flex-direction:column;gap:12px;overflow-y:auto;min-height:0}
.card h2{font-size:10.5px;font-weight:700;color:var(--dim);text-transform:uppercase;
  letter-spacing:.6px;display:flex;align-items:center;gap:7px;margin-bottom:11px}
.card h2 .dot{width:6px;height:6px;border-radius:50%;background:var(--primary);flex-shrink:0}

.field{margin-bottom:9px}
.field label{display:block;font-size:11px;color:var(--dim);margin-bottom:5px}
input[type=text],input[type=url]{width:100%;padding:9px 11px;
  background:var(--input);border:1px solid var(--border);border-radius:var(--radius-sm);
  color:var(--text);font-size:13px;outline:none;transition:border .15s,box-shadow .15s;
  font-family:inherit}
input:focus{border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-glow)}
input:disabled{opacity:.5}

.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;
  padding:9px 14px;border:none;border-radius:var(--radius-sm);font-size:13px;font-weight:600;
  cursor:pointer;transition:transform .12s,box-shadow .12s,background .12s;width:100%;
  font-family:inherit}
.btn:active:not(:disabled){transform:translateY(1px)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-p{background:var(--primary);color:#fff}
.btn-p:hover:not(:disabled){background:var(--primary-h);box-shadow:0 4px 14px var(--primary-glow)}
.btn-s{background:var(--surface-2);color:var(--text);border:1px solid var(--border)}
.btn-s:hover:not(:disabled){border-color:var(--primary)}
.btn-ghost{background:transparent;color:var(--dim);border:1px solid var(--border);font-size:12px}
.btn-ghost:hover:not(:disabled){color:var(--text);border-color:var(--faint)}
.btn-stop{background:transparent;color:var(--err);border:1px solid rgba(244,63,94,.35)}
.btn-stop:hover:not(:disabled){background:var(--err-bg)}
.btn-auto{width:auto}

.rowbtns{display:flex;gap:8px;margin-top:10px}

.statgrid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px;position:relative}
.stat{background:var(--surface-2);border-radius:var(--radius-sm);padding:9px 6px;text-align:center}
.stat b{display:block;font-size:17px;font-weight:800;font-variant-numeric:tabular-nums;color:var(--text)}
.stat span{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
.new-flag{position:absolute;top:-7px;right:-7px;background:var(--ok);color:#04140d;
  font-size:10px;font-weight:800;padding:2px 7px;border-radius:99px;display:none;
  box-shadow:0 2px 8px rgba(0,0,0,.3)}
.new-flag.on{display:block;animation:fadeIn .3s ease}

.syncline{font-size:11.5px;color:var(--dim);padding-top:9px;border-top:1px solid var(--border)}
.syncline b{color:var(--ok);font-weight:700}
.syncline.warn b{color:var(--warn)}

.authpill{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:99px;
  font-size:12px;font-weight:600;margin-bottom:11px}
.authpill.ok{background:var(--ok-bg);color:var(--ok)}
.authpill.fail{background:var(--err-bg);color:var(--err)}
.authpill.wait{background:var(--warn-bg);color:var(--warn)}

.hint{font-size:11px;color:var(--faint);margin-top:9px;line-height:1.45}

/* worklist */
.worklist{display:flex;flex-direction:column;gap:10px;min-height:0}

.toolbar{display:flex;align-items:center;gap:9px;flex-shrink:0;flex-wrap:wrap}
.toolbar h2{font-size:15px;font-weight:700;white-space:nowrap}
.toolbar .count{font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.search{flex:1;position:relative;min-width:120px}
.search input{padding:7px 11px 7px 30px;font-size:12.5px}
.search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);width:13px;height:13px;
  color:var(--faint);pointer-events:none}
.seg{display:flex;background:var(--surface-2);border:1px solid var(--border);border-radius:999px;
  padding:2px;flex-shrink:0}
.seg button{border:none;background:transparent;color:var(--dim);font-size:11px;font-weight:600;
  padding:6px 11px;border-radius:999px;cursor:pointer;font-family:inherit;white-space:nowrap}
.seg button.on{background:var(--primary);color:#fff}
.seg button:disabled{opacity:.5;cursor:not-allowed}

.strip{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:11px 14px;flex-shrink:0;display:none}
.strip.on{display:block;animation:fadeIn .25s ease}

.selbar .row1{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.selbar .txt{flex:1;min-width:200px;font-size:12.5px;color:var(--text)}
.selbar .txt b{font-variant-numeric:tabular-nums}
.selbar .warn-note{color:var(--warn);display:block;font-size:11.5px;margin-top:2px}
.selbar .actions{display:flex;gap:8px;flex-wrap:wrap}
.selbar .btn{width:auto;padding:9px 15px;font-size:12.5px}
.selbar.empty .txt{color:var(--dim)}

.activity-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px}
.activity-top .phase{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--text)}
.activity-top .cnt{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{width:100%;height:6px;background:var(--input);border-radius:3px;overflow:hidden}
.fill{height:100%;border-radius:3px;width:0%;transition:width .4s ease;
  background:linear-gradient(90deg,#7c3aed,#ec4899)}
.fill.ind{width:30%;animation:ind 1.2s ease-in-out infinite}
@keyframes ind{0%{transform:translateX(-100%)}100%{transform:translateX(400%)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
.activity-bottom{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:8px}
.activity-bottom .cur{font-size:12px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.banner{border-radius:var(--radius);padding:11px 14px;font-size:12.5px;flex-shrink:0;display:none}
.banner.on{display:block;animation:fadeIn .3s ease}
.banner.ok{background:var(--ok-bg);border:1px solid rgba(16,185,129,.25);color:var(--ok)}
.banner.cancel{background:var(--warn-bg);border:1px solid rgba(245,158,11,.25);color:var(--warn)}

.list-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden;display:flex;flex-direction:column;min-height:0;flex:1}
.list-head{display:grid;grid-template-columns:30px 1fr 118px 78px;gap:10px;align-items:center;
  padding:8px 14px;border-bottom:1px solid var(--border);background:var(--surface-2);flex-shrink:0}
.list-head span{font-size:9.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;font-weight:700}
.list-head .chk-all{width:15px;height:15px;accent-color:var(--primary);cursor:pointer}
.list-head .chk-all:disabled{cursor:not-allowed}

.list{flex:1;overflow-y:auto;min-height:0}
.empty-note{padding:32px 20px;text-align:center;color:var(--dim);font-size:12.5px}
.row{display:grid;grid-template-columns:30px 1fr 118px 78px;gap:10px;align-items:center;
  padding:9px 14px;border-bottom:1px solid var(--border);transition:background .12s}
.row:last-child{border-bottom:none}
.row:hover{background:var(--surface-2)}
.row.checked{background:var(--primary-glow)}
.row input[type=checkbox]{width:15px;height:15px;accent-color:var(--primary);cursor:pointer}
.row input[type=checkbox]:disabled{cursor:not-allowed}
.row .meta{min-width:0}
.row .title{font-size:12.5px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .artist{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.pill{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;border-radius:999px;
  font-size:10px;font-weight:700;white-space:nowrap;letter-spacing:.2px}
.pill.new{background:var(--primary-glow);color:var(--primary-h)}
.pill.disk{background:var(--info-bg);color:var(--info)}
.pill.ym{background:var(--ok-bg);color:var(--ok)}
.pill.busy{background:var(--warn-bg);color:var(--warn)}
.pill.err{background:var(--err-bg);color:var(--err)}
.pill .spin{width:8px;height:8px;border-radius:50%;border:1.5px solid currentColor;
  border-top-color:transparent;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

.skip{background:none;border:none;color:var(--faint);cursor:pointer;font-size:10.5px;
  padding:4px 6px;border-radius:6px;font-family:inherit;white-space:nowrap;justify-self:end}
.skip:hover:not(:disabled){color:var(--dim);background:var(--surface-2)}
.skip:disabled{cursor:not-allowed;opacity:.4}

.btn-more{width:100%;padding:8px;border:none;background:transparent;color:var(--primary);
  font-size:11.5px;cursor:pointer;border-top:1px solid var(--border);flex-shrink:0;font-family:inherit}
.btn-more:hover:not(:disabled){background:var(--surface-2)}

.drawer{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden;flex-shrink:0}
.drawer summary{list-style:none;cursor:pointer;padding:9px 14px;display:flex;align-items:center;
  gap:8px;font-size:11.5px;color:var(--dim);font-weight:600}
.drawer summary::-webkit-details-marker{display:none}
.drawer summary .chev{transition:transform .15s;width:10px;height:10px;flex-shrink:0}
.drawer[open] summary .chev{transform:rotate(90deg)}
.drawer summary .n{margin-left:auto;background:var(--surface-2);border-radius:999px;padding:2px 8px;
  font-size:10px;font-variant-numeric:tabular-nums}
.logbody{padding:0 14px 10px;font-family:ui-monospace,'SF Mono','Fira Code',monospace;font-size:11px;
  max-height:150px;overflow-y:auto}
.logline{display:flex;gap:8px;padding:2px 0;color:var(--dim)}
.logline time{color:var(--faint);flex-shrink:0}
.logline.ok{color:var(--ok)}
.logline.err{color:var(--err)}

.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(80px);
  background:var(--primary);color:#fff;padding:8px 18px;border-radius:8px;
  font-size:12.5px;font-weight:500;transition:transform .3s ease;z-index:99;pointer-events:none;
  box-shadow:0 8px 24px rgba(0,0,0,.35)}
.toast.show{transform:translateX(-50%) translateY(0)}

@media(max-width:880px){
  body{height:auto;overflow:auto}
  .shell{height:auto}
  .layout{grid-template-columns:1fr;max-width:560px;margin:0 auto}
  .rail{overflow-y:visible}
  .list{max-height:420px}
}
</style>
</head>
<body>
<div class="shell">

<div class="topbar">
  <h1>&#9835; Музыкальный</h1>
  <span>SoundCloud &rarr; Яндекс Музыка</span>
</div>

<div class="layout">

  <div class="rail">
    <div class="card">
      <h2><span class="dot"></span> Источник &mdash; SoundCloud</h2>
      <div class="field">
        <label for="sc">Имя пользователя</label>
        <input type="text" id="sc" placeholder="username" autocomplete="off" spellcheck="false">
      </div>
      <div class="statgrid">
        <span class="new-flag" id="s-new"></span>
        <div class="stat"><b id="s-total">&mdash;</b><span>Всего</span></div>
        <div class="stat"><b id="s-uploaded">&mdash;</b><span>В ЯМ</span></div>
        <div class="stat"><b id="s-size">&mdash;</b><span>На диске</span></div>
      </div>
      <div class="syncline" id="sync-line"></div>
      <div class="rowbtns">
        <button class="btn btn-p" id="btn-scan" onclick="doScan()" style="flex:1">&#128269; Проверить</button>
        <button class="btn btn-s btn-auto" onclick="openFolder()" title="Открыть папку">&#128194;</button>
      </div>
    </div>

    <div class="card">
      <h2><span class="dot" style="background:var(--ok)"></span> Назначение &mdash; Яндекс Музыка</h2>
      <div id="ym-auth"><div class="authpill wait">&#8635; Проверка&hellip;</div></div>
      <div class="field">
        <label for="pl">URL плейлиста</label>
        <input type="url" id="pl" placeholder="https://music.yandex.ru/playlists/..." autocomplete="off">
      </div>
      <div class="rowbtns">
        <button class="btn btn-s" onclick="checkYM()">Обновить статус</button>
        <button class="btn btn-s" onclick="checkPlaylist(this)" title="Сравнить локальную базу с плейлистом">Сверить плейлист</button>
        <button class="btn btn-s btn-auto" onclick="openPlaylist()" title="Открыть плейлист">&#8599;</button>
      </div>
      <button class="btn btn-s" id="btn-up" onclick="doUpload()" style="margin-top:8px">&#11014; Загрузить всё «На диске»</button>
      <div class="hint">Отправляет в плейлист все скачанные, но ещё не загруженные треки &mdash; не привязано к выбору в списке справа.</div>
    </div>
  </div>

  <div class="worklist">

    <div class="toolbar">
      <h2>Треки</h2>
      <span class="count" id="track-count">&mdash;</span>
      <div class="search">
        <svg viewBox="0 0 20 20" fill="none"><circle cx="9" cy="9" r="6.5" stroke="currentColor" stroke-width="1.6"/><path d="M18 18l-4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        <input type="text" id="track-filter" placeholder="Найти трек или исполнителя&hellip;" oninput="onFilter()">
      </div>
      <div class="seg" id="seg">
        <button class="on" data-f="all" onclick="setFilterMode('all')">Все</button>
        <button data-f="new" onclick="setFilterMode('new')">Новые</button>
        <button data-f="pending" onclick="setFilterMode('pending')">Не в ЯМ</button>
      </div>
    </div>

    <div class="strip selbar empty on" id="selbar">
      <div class="row1">
        <div class="txt" id="selbar-txt">Ничего не выбрано &mdash; отметь треки галочкой, чтобы скачать или отправить в Яндекс Музыку.</div>
        <div class="actions" id="selbar-actions" style="display:none">
          <button class="btn btn-ghost" id="btn-mark" onclick="doMarkUploaded()">&#10003; Уже в ЯМ</button>
          <button class="btn btn-s" id="btn-dl" onclick="doStart(false)">&#11015; Скачать</button>
          <button class="btn btn-p" id="btn-dlup" onclick="doStart(true)">&#11015;&#11014; Скачать и в ЯМ</button>
        </div>
      </div>
    </div>

    <div class="strip" id="activity">
      <div class="activity-top">
        <span class="phase" id="act-phase">&mdash;</span>
        <span class="cnt" id="act-cnt"></span>
      </div>
      <div class="bar"><div class="fill" id="act-fill"></div></div>
      <div class="activity-bottom">
        <span class="cur" id="act-cur"></span>
        <button class="btn btn-stop btn-auto" id="btn-cancel" onclick="doCancel()">&#9632; Остановить</button>
      </div>
    </div>

    <div class="banner ok" id="banner-ok"></div>
    <div class="banner cancel" id="banner-cancel"></div>

    <div class="list-card">
      <div class="list-head">
        <input type="checkbox" class="chk-all" id="chk-all" title="Выбрать/снять все по фильтру" onchange="toggleAll()">
        <span>Трек</span>
        <span>Статус</span>
        <span></span>
      </div>
      <div class="list" id="track-list"></div>
      <button class="btn-more" id="btn-more" onclick="showMore()" style="display:none">Показать ещё</button>
    </div>

    <details class="drawer" id="drawer">
      <summary>
        <svg class="chev" viewBox="0 0 20 20" fill="none"><path d="M7 4l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Журнал операции
        <span class="n" id="log-n">0</span>
      </summary>
      <div class="logbody" id="log"></div>
    </details>

  </div>
</div>
</div>

<div class="toast" id="toast"></div>

<script>
let sse=null,busy=false;
let allTracks=[];
const checked=new Set();
const liveStatus=new Map();
let filterMode='all',query='',visibleCount=0,logCount=0;
const PAGE=30;
const $=id=>document.getElementById(id);

const PILL={
  pending:{cls:'new',text:'Новый'},
  downloaded:{cls:'disk',text:'На диске'},
  uploaded:{cls:'ym',text:'В ЯМ'},
  not_in_likes:{cls:'disk',text:'Пропал из лайков'},
  dl_busy:{cls:'busy',text:'Скачивается'},
  ym_busy:{cls:'busy',text:'Отправка в ЯМ'},
  err:{cls:'err',text:'Ошибка'},
};

document.addEventListener('DOMContentLoaded',()=>{
  loadStatus();checkYM();connectSSE();loadTracks();
  function debounceSave(){if(_saveTimer)clearTimeout(_saveTimer);_saveTimer=setTimeout(()=>save(false),800)}
  $('sc').addEventListener('input',debounceSave);
  $('pl').addEventListener('input',debounceSave);
});

async function loadTracks(){
  // Список из локальной базы при старте — без скана SoundCloud.
  if(busy) return;
  try{
    const r=await fetch('/api/tracks');
    const d=await r.json();
    if(busy || allTracks.length) return; // скан успел отработать — не затираем
    if(d.tracks && d.tracks.length){allTracks=d.tracks;render()}
  }catch(e){console.error(e)}
}

async function loadTracksFresh(){
  // Перечитать базу после пропущенных событий (обрыв SSE) — статусы могли измениться.
  try{
    const d=await (await fetch('/api/tracks')).json();
    if(d.tracks){allTracks=d.tracks;liveStatus.clear();render()}
  }catch(e){console.error(e)}
}

async function loadStatus(){
  try{
    const r=await fetch('/api/status'),d=await r.json();
    $('sc').value=d.username||'';
    $('pl').value=d.playlist_url||'';
    showStats(d.stats);
    if(d.active_task){
      setBusy(true);
      showActivity(d.active_task==='scan'?'Сканирование SoundCloud':
        d.active_task==='upload'?'Загрузка в Яндекс Музыку':'Выполняется задача');
      indeterminate();
    }
  }catch(e){console.error(e)}
}
function showStats(s){
  if(!s)return;
  $('s-total').textContent=s.total;
  $('s-uploaded').textContent=s.uploaded;
  $('s-size').textContent=s.size_mb>0?(s.size_mb>=1024?(s.size_mb/1024).toFixed(1)+' ГБ':s.size_mb+' МБ'):'0';
  const sl=$('sync-line');
  if(s.total>0){
    if(s.pending>0||s.downloaded>0){
      let p=[];
      if(s.downloaded>0) p.push(s.downloaded+' на диске');
      if(s.pending>0) p.push(s.pending+' новых');
      sl.className='syncline warn';
      sl.innerHTML='<b>'+s.uploaded+'</b> в ЯМ &middot; '+p.join(' &middot; ');
    }else{
      sl.className='syncline';
      sl.innerHTML='&#10003; Все <b>'+s.total+'</b> в Яндекс Музыке';
    }
  }else{sl.textContent='Ещё не сканировали'}
}

let _saveTimer=null;
async function save(silent){
  if(_saveTimer){clearTimeout(_saveTimer);_saveTimer=null}
  try{
    await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:$('sc').value,playlist_url:$('pl').value})});
    if(!silent) toast('Сохранено');
  }catch(e){console.error(e)}
}
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}

async function checkYM(){
  const el=$('ym-auth');
  el.innerHTML='<div class="authpill wait">&#8635; Проверка&hellip;</div>';
  try{const r=await fetch('/api/check-yandex'),d=await r.json();
    el.innerHTML=d.authorized
      ?`<div class="authpill ok">&#10003; ${escapeHtml(d.login||'')}</div>`
      :`<div class="authpill fail">&#10007; ${escapeHtml(d.error||'Не авторизован')}</div>`;
  }catch(e){el.innerHTML='<div class="authpill fail">&#10007; Ошибка проверки</div>'}
}

async function checkPlaylist(btn){
  btn.disabled=true;const old=btn.textContent;btn.textContent='Сверяю…';
  try{
    const r=await fetch('/api/check-playlist',{method:'POST'});
    const d=await r.json();
    if(d.error){toast(d.error);return}
    let msg=`В плейлисте ${d.playlist_total} трек(ов). Найдено ${d.found} из ${d.found+d.missing_total} локальных «в ЯМ».`;
    if(d.processing)msg+=` Ещё обрабатывается: ${d.processing}.`;
    if(d.duplicates)msg+=` Дублей по названию: ${d.duplicates}.`;
    log(msg);
    if(d.missing_total){
      log(`Не найдено по названию (${d.missing_total}): `+d.missing.slice(0,10)
        .map(t=>(t.artist?t.artist+' — ':'')+t.title).join('; ')+(d.missing_total>10?' …':''), 'err');
    }
    toast(d.missing_total?`Не найдено: ${d.missing_total} (см. журнал)`:'Все локальные треки на месте');
    $('drawer').open=true;
  }catch(e){toast('Ошибка сверки')}
  finally{btn.disabled=false;btn.textContent=old}
}

function openPlaylist(){
  const url=$('pl').value.trim();
  if(url) window.open(url,'_blank');
  else toast('Введите URL плейлиста');
}

function connectSSE(){
  if(sse)sse.close();
  sse=new EventSource('/api/progress');
  sse.onmessage=e=>handleEvent(JSON.parse(e.data));
  sse.onerror=()=>setTimeout(connectSSE,3000);
}

function resetLog(){
  $('log').innerHTML='';logCount=0;$('log-n').textContent='0';$('drawer').open=false;
  $('banner-ok').classList.remove('on');$('banner-cancel').classList.remove('on');
}

function resetTracks(){
  allTracks=[];checked.clear();liveStatus.clear();
  filterMode='all';query='';visibleCount=PAGE;
  $('track-filter').value='';
  document.querySelectorAll('#seg button').forEach(b=>b.classList.toggle('on',b.dataset.f==='all'));
  $('s-new').classList.remove('on');
}

function visibleTracks(){
  return allTracks.filter(t=>{
    if(filterMode==='new' && t.status!=='pending') return false;
    if(filterMode==='pending' && t.status==='uploaded') return false;
    if(query && !(t.title+' '+t.artist).toLowerCase().includes(query)) return false;
    return true;
  });
}

function pillHtml(id,baseStatus){
  const live=liveStatus.get(id);
  const key=live||baseStatus;
  const p=PILL[key]||PILL.pending;
  const spin=(key==='dl_busy'||key==='ym_busy')?'<span class="spin"></span>':'';
  return `<span class="pill ${p.cls}">${spin}${p.text}</span>`;
}

function render(){
  const filtered=visibleTracks();
  const slice=filtered.slice(0,visibleCount);
  const list=$('track-list');

  if(!allTracks.length){
    list.innerHTML='<div class="empty-note">Пока пусто. Нажми «Проверить», чтобы получить список лайков с SoundCloud.</div>';
  }else if(!filtered.length){
    list.innerHTML='<div class="empty-note">Ничего не подходит под фильтр.</div>';
  }else{
    list.innerHTML=slice.map(t=>{
      const isChecked=checked.has(t.id);
      const canSkip=t.status!=='uploaded';
      return `<div class="row${isChecked?' checked':''}" data-id="${t.id}">
        <input type="checkbox" ${isChecked?'checked':''} data-chk="${t.id}" ${busy?'disabled':''}>
        <div class="meta">
          <div class="title">${escapeHtml(t.title)}</div>
          <div class="artist">${escapeHtml(t.artist||'')}</div>
        </div>
        <div class="pillwrap" data-pill="${t.id}">${pillHtml(t.id,t.status)}</div>
        <div>${canSkip?`<button class="skip" data-skip="${t.id}" ${busy?'disabled':''}>Уже в ЯМ</button>`:''}</div>
      </div>`;
    }).join('');
  }

  const newCount=allTracks.filter(t=>t.status==='pending').length;
  $('track-count').textContent=allTracks.length?(filtered.length+' из '+allTracks.length+' · '+newCount+' новых'):'';

  const more=$('btn-more');
  if(visibleCount<filtered.length){
    more.style.display='';
    more.textContent='Показать ещё '+Math.min(PAGE,filtered.length-visibleCount)+' из '+(filtered.length-visibleCount);
  }else{more.style.display='none'}

  $('chk-all').checked=filtered.length>0 && filtered.every(t=>checked.has(t.id));
  $('chk-all').disabled=busy || !filtered.length;

  renderSelbar();
}

function escapeHtml(s){
  return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderSelbar(){
  const sel=allTracks.filter(t=>checked.has(t.id));
  const bar=$('selbar'),txt=$('selbar-txt'),actions=$('selbar-actions');
  if(!sel.length){
    bar.classList.add('empty');actions.style.display='none';
    txt.textContent='Ничего не выбрано — отметь треки галочкой, чтобы скачать или отправить в Яндекс Музыку.';
    return;
  }
  bar.classList.remove('empty');actions.style.display='flex';
  const nNew=sel.filter(t=>t.status==='pending').length;
  const nDisk=sel.filter(t=>t.status==='downloaded').length;
  const nYm=sel.filter(t=>t.status==='uploaded').length;
  let parts=[];
  if(nNew) parts.push(nNew+' новых');
  if(nDisk) parts.push(nDisk+' на диске');
  if(nYm) parts.push(nYm+' уже в ЯМ');
  let html=`Выбрано <b>${sel.length}</b>: ${parts.join(', ')}.`;
  if(nYm) html+=`<span class="warn-note">&#9888; ${nYm===1?'Этот трек уже':'Эти треки уже'} в плейлисте — повторная отправка добавит дубль.</span>`;
  txt.innerHTML=html;
}

function setFilterMode(m){
  filterMode=m;visibleCount=PAGE;
  document.querySelectorAll('#seg button').forEach(b=>b.classList.toggle('on',b.dataset.f===m));
  render();
}

function onFilter(){
  query=$('track-filter').value.trim().toLowerCase();
  visibleCount=PAGE;
  render();
}

function showMore(){
  visibleCount=Math.min(visibleCount+PAGE,visibleTracks().length);
  render();
}

function toggleAll(){
  const filtered=visibleTracks();
  const allOn=filtered.length>0 && filtered.every(t=>checked.has(t.id));
  filtered.forEach(t=> allOn ? checked.delete(t.id) : checked.add(t.id));
  render();
}

$('track-list').addEventListener('change',e=>{
  if(e.target.matches('[data-chk]')){
    const id=e.target.dataset.chk;
    e.target.checked?checked.add(id):checked.delete(id);
    e.target.closest('.row').classList.toggle('checked',e.target.checked);
    renderSelbar();
    $('chk-all').checked=visibleTracks().every(t=>checked.has(t.id));
  }
});
$('track-list').addEventListener('click',e=>{
  if(e.target.matches('[data-skip]')) markOne(e.target.dataset.skip,e.target);
});

async function markOne(id,btn){
  if(busy)return;
  btn.disabled=true;
  try{
    const r=await fetch('/api/mark-uploaded',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({track_ids:[id]})});
    const d=await r.json();
    if(d.ok){
      const t=allTracks.find(x=>x.id===id);
      if(t) t.status='uploaded';
      checked.delete(id);
      render();toast('Отмечен');loadStatus();
    }else{toast(d.error||'Ошибка');btn.disabled=false}
  }catch(e){toast('Ошибка');btn.disabled=false}
}

function updatePill(id){
  const el=document.querySelector(`[data-pill="${id}"]`);
  const t=allTracks.find(x=>x.id===id);
  if(el && t) el.innerHTML=pillHtml(id,t.status);
}

function showActivity(phase){
  $('activity').classList.add('on');
  $('act-phase').textContent=phase;
  $('act-cnt').textContent='';$('act-cur').textContent='';
  $('btn-cancel').style.display='';
}
function hideActivity(){$('activity').classList.remove('on')}
function indeterminate(){$('act-fill').className='fill ind';$('act-fill').style.width='30%'}
function progressTo(done,total){
  $('act-fill').className='fill';
  $('act-fill').style.width=(total?Math.min(99,Math.round(done/total*100)):0)+'%';
  $('act-cnt').textContent=done+'/'+total;
}

function handleEvent(d){
  switch(d.type){
    case 'scanning':
      resetLog();resetTracks();render();
      showActivity('Сканирование SoundCloud');indeterminate();
      $('btn-cancel').style.display='none';
      setBusy(true);
      break;

    case 'scan_complete':
      hideActivity();
      allTracks=d.all_tracks||[];
      visibleCount=PAGE;
      if(d.new_count>0){$('s-new').textContent='+'+d.new_count+' новых';$('s-new').classList.add('on')}
      else{$('s-new').classList.remove('on')}
      if(!allTracks.length) toast('Треков не найдено');
      render();
      setBusy(false);
      break;

    case 'dl_start':
      showActivity('Скачивание с SoundCloud');
      progressTo(0,d.total);
      break;

    case 'downloading':
      if(d.id){liveStatus.set(d.id,'dl_busy');updatePill(d.id)}
      progressTo(d.downloaded,d.total);
      $('act-cur').textContent='♪ '+d.title;
      break;

    case 'track_done':{
      progressTo(d.downloaded,d.total);
      if(d.id){
        liveStatus.delete(d.id);
        const t=allTracks.find(x=>x.id===d.id);
        if(t) t.status=d.status||'downloaded';
        updatePill(d.id);
      }
      log('✓ '+d.title,'ok');
      break;
    }

    case 'dl_complete':
      $('act-cur').textContent='';
      break;

    case 'upload_start':
      showActivity('Загрузка в Яндекс Музыку');
      progressTo(0,d.total);
      break;

    case 'uploading':
      if(d.id){liveStatus.set(d.id,'ym_busy');updatePill(d.id)}
      progressTo(d.uploaded,d.total);
      $('act-cur').textContent='↑ '+d.title;
      break;

    case 'track_uploaded':
      if(d.id){
        liveStatus.delete(d.id);
        const t=allTracks.find(x=>x.id===d.id);
        if(t) t.status='uploaded';
        checked.delete(d.id);
        render();
      }
      break;

    case 'all_done':{
      hideActivity();
      $('s-new').classList.remove('on');
      let t='';
      if(d.downloaded>0) t+='Скачано: '+d.downloaded;
      if(d.failures) t+=(t?' · ':'')+'Ошибок скачивания: '+d.failures;
      if(d.uploaded>0) t+=(t?' · ':'')+'В ЯМ: '+d.uploaded;
      if(d.upload_errors) t+=(t?' · ':'')+'Ошибок загрузки: '+d.upload_errors;
      if(d.message) t+=(t?' · ':'')+d.message;
      const bad=d.level==='error'||d.level==='warn';
      if(d.level==='error') log(d.message,'err');
      $(bad?'banner-cancel':'banner-ok').textContent=t||'Готово';
      $(bad?'banner-cancel':'banner-ok').classList.add('on');
      setBusy(false);loadStatus();
      break;
    }

    case 'cancelled':{
      hideActivity();
      $('s-new').classList.remove('on');
      let t='Остановлено';
      if(d.downloaded) t+=' · Скачано: '+d.downloaded;
      if(d.uploaded) t+=' · Загружено: '+d.uploaded;
      $('banner-cancel').textContent=t;
      $('banner-cancel').classList.add('on');
      setBusy(false);loadStatus();
      break;
    }

    case 'error':
      log(d.message,'err');
      toast(d.message.length>60?d.message.slice(0,60)+'…':d.message);
      hideActivity();setBusy(false);loadStatus();
      break;
    case 'hello':
      // (Пере)подключение к SSE: если задача уже закончилась, пока связи не было, — снимаем «занято».
      if(!d.active_task && busy){hideActivity();setBusy(false);loadStatus();loadTracksFresh()}
      break;
    case 'log':
      log(d.message,d.level==='error'?'err':'');
      if(d.id && d.level==='error'){liveStatus.set(d.id,'err');updatePill(d.id)}
      break;
  }
}

function log(msg,cls){
  const body=$('log');
  const el=document.createElement('div');
  el.className='logline'+(cls?' '+cls:'');
  const t=new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  el.innerHTML=`<time>${t}</time><span>${escapeHtml(msg)}</span>`;
  body.appendChild(el);body.scrollTop=body.scrollHeight;
  logCount++;$('log-n').textContent=logCount;
  if(cls==='err') $('drawer').open=true;
}

function setBusy(v){
  busy=v;
  $('btn-scan').disabled=v;
  $('btn-up').disabled=v;
  $('track-filter').disabled=v;
  $('btn-more').disabled=v;
  document.querySelectorAll('#seg button').forEach(b=>b.disabled=v);
  $('selbar').classList.toggle('on',!v);
  render();
  if(!v) $('btn-cancel').style.display='none';
}

async function doScan(){
  if(busy)return;
  await save(true);
  if(!$('sc').value.trim()){toast('Введите имя SoundCloud');$('sc').focus();return}
  try{const r=await fetch('/api/scan',{method:'POST'});
    if(!r.ok){
      const e=await r.json();
      if(r.status===409){
        toast('Задача уже выполняется — нажмите «Остановить» для сброса');
        setBusy(true);showActivity(e.error||'Задача выполняется');indeterminate();
      }else{toast(e.error||'Ошибка')}
    }
  }catch(e){toast('Ошибка')}
}

async function doStart(upload){
  if(busy)return;
  const ids=[...checked];
  if(!ids.length){toast('Выберите треки');return}
  setBusy(true);resetLog();
  try{const r=await fetch('/api/start',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({upload:!!upload,track_ids:ids})});
    if(!r.ok){const e=await r.json();toast(e.error||'Ошибка');setBusy(false)}
  }catch(e){toast('Ошибка');setBusy(false)}
}

async function doCancel(){
  try{await fetch('/api/cancel',{method:'POST'})}catch(e){}
  $('act-cur').textContent='Останавливаем… (дожидаюсь конца текущего трека)';
  let tries=0;
  const poll=async()=>{
    try{
      const d=await (await fetch('/api/status')).json();
      if(!d.active_task){if(busy){hideActivity();setBusy(false);loadStatus()}return}
      // Сервер откажет (409), пока поток задачи жив, — это нормально, ждём дальше.
      if(++tries%5===0) await fetch('/api/force-clear',{method:'POST'});
    }catch(e){}
    setTimeout(poll,2000);
  };
  setTimeout(poll,2000);
}

async function doUpload(){
  if(busy)return;
  await save(true);
  if(!$('pl').value.trim()){toast('Введите URL плейлиста');$('pl').focus();return}
  setBusy(true);resetLog();
  showActivity('Загрузка в Яндекс Музыку');indeterminate();
  try{const r=await fetch('/api/upload',{method:'POST'});
    if(!r.ok){const e=await r.json();toast(e.error||'Ошибка');setBusy(false);hideActivity()}
  }catch(e){toast('Ошибка');setBusy(false);hideActivity()}
}

async function openFolder(){
  try{await fetch('/api/open-folder',{method:'POST'})}catch(e){toast('Ошибка')}
}

async function doMarkUploaded(){
  if(busy)return;
  const ids=[...checked];
  if(!ids.length){toast('Выберите треки');return}
  if(!confirm('Пометить '+ids.length+' треков как уже загруженные в ЯМ?'))return;
  try{
    const r=await fetch('/api/mark-uploaded',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({track_ids:ids})});
    const d=await r.json();
    if(d.ok){
      ids.forEach(id=>{const t=allTracks.find(x=>x.id===id);if(t)t.status='uploaded';checked.delete(id)});
      render();toast('Отмечено: '+d.marked);loadStatus();
    }else{toast(d.error||'Ошибка')}
  }catch(e){toast('Ошибка')}
}

render();
</script>
</body>
</html>"""

# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    _migrate_json_to_csv()
    _cleanup_stale_paths()
    _cleanup_partials()
    port = 5555
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
