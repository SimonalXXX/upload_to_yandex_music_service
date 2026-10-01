#!/usr/bin/env python3
"""Музыкальный — локальный веб-сервер: лайки SoundCloud → плейлист Яндекс Музыки.

Интерфейс — templates/index.html + static/ (ES-модули без сборки).
Контракты: specs/openapi.yaml (REST), specs/sse-events.md (события /api/progress).
"""

from __future__ import annotations

import csv
import json
import mimetypes
import queue
import re
import subprocess
import threading
import time
import uuid
import webbrowser
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import yaml
import yt_dlp
from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from yt_dlp.postprocessor.metadataparser import MetadataParserPP

# ── Paths & constants ────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
TRACKS_PATH = BASE_DIR / "tracks.csv"
TRACKS_JSON_PATH = BASE_DIR / "tracks.json"
ARCHIVE_PATH = BASE_DIR / "archive.txt"
HISTORY_PATH = BASE_DIR / "history.json"
STATIC_DIR = BASE_DIR / "static"
PORT = 5555

CSV_FIELDS = ["sc_id", "title", "artist", "sc_url", "status", "file", "added", "error", "sent_at"]
# pending → downloaded → sent → uploaded; not_in_likes — пропал из лайков; unavailable — скачать нельзя.
# sent — ЯМ принял файл, но трек ещё не найден в плейлисте (specs/features/upload-confirmation.md).
STATUSES = ("pending", "downloaded", "sent", "uploaded", "not_in_likes", "unavailable")
SENT_TIMEOUT = 24 * 3600       # не появился в плейлисте за сутки → ЯМ не принял трек
SENT_CHECK_INTERVAL = 15 * 60  # фоновая проверка отправленных
# Короткое окно подтверждения при загрузке: ЯМ обрабатывает файл минутами, долгое
# ожидание только блокировало задачу; подтверждаем позже по плейлисту (_confirm_sent).
UPLOAD_VERIFY_RETRIES = 2
UPLOAD_VERIFY_DELAY = 3
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".flac"}
AUDIO_MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg", ".flac": "audio/flac"}
# Незавершённые скачивания yt-dlp (после отмены/сбоя) — подчищаем.
PARTIAL_GLOBS = ("*.part", "*.part-Frag*", "*.ytdl")
# Некоторые заливают трек с названием «… .mp3» — без этого получаются «….mp3.mp3».
TITLE_EXT_CLEANUP_PP = {
    "key": "MetadataParser",
    "when": "pre_process",
    "actions": [(MetadataParserPP.Actions.REPLACE, "title",
                 r"(?i)\.(?:mp3|wav|flac|m4a|aiff?|ogg|opus)$", "")],
}
# Ошибки, после которых повторять скачивание бессмысленно → статус unavailable.
UNAVAILABLE_PATTERNS = (
    ("DRM protected", "Защищён DRM (SoundCloud Go+) — скачать нельзя"),
    ("not available in your country", "Недоступен в вашей стране"),
    ("geo restriction", "Недоступен в вашей стране"),
    ("HTTP Error 404", "Удалён или скрыт автором"),
    ("HTTP Error 410", "Удалён автором"),
)

FORMATS = ("mp3", "m4a", "opus", "flac")
QUALITIES = ("128", "192", "256", "320")
COOKIE_BROWSERS = ("", "chrome", "safari", "firefox", "brave", "edge", "chromium", "opera", "vivaldi")
AUTOSYNC_INTERVALS = (1, 3, 6, 12, 24)
TASK_TITLES = {"scan": "Проверка лайков", "download": "Скачивание",
               "upload": "Загрузка в ЯМ", "sync": "Синхронизация"}

HISTORY_LIMIT = 50
HISTORY_EVENTS_LIMIT = 300
AUTH_CACHE_TTL = 600  # сек: не читать cookies Chrome на каждое открытие страницы

mimetypes.add_type("application/manifest+json", ".webmanifest")

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"), static_folder=str(STATIC_DIR))
# ES-модули импортируют друг друга без ?v= — пусть браузер всегда перепроверяет статику (304).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}


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
_history_lock = threading.Lock()
_active_task: str | None = None
_task_trigger: str | None = None
_task_started_at: float | None = None
# Поток текущей задачи: пока он жив, новую задачу не запускаем — иначе
# _cancel.clear() «оживит» недоостановленный воркер и два потока пишут в CSV.
_worker: threading.Thread | None = None
_cancel = threading.Event()
# Запись истории текущей задачи — в неё _broadcast складывает события.
_current_rec: dict | None = None
# None = ещё не проверяли; False = браузер/профиль не найден, cookies не используем
_browser_cookies_ok: bool | None = None
_auth_cache: dict | None = None
_last_report: dict | None = None


class TaskError(Exception):
    """Задача не может продолжаться; текст — для пользователя."""


class TaskCancelled(Exception):
    """Пользователь нажал «Остановить»; counts — что успели сделать."""

    def __init__(self, counts: dict | None = None):
        super().__init__("Отменено")
        self.counts = counts or {}


def _is_cookie_db_error(err: str) -> bool:
    return "cookies database" in err.lower()


def _norm(s: str | None) -> str:
    """Нормализация названия/артиста для сверки с плейлистом ЯМ."""
    return " ".join((s or "").lower().replace("ё", "е").split())


def _check_cancel(counts: dict | None = None) -> None:
    if _cancel.is_set():
        raise TaskCancelled(counts)


# ── Config & settings ────────────────────────────────────────────────────

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.replace(CONFIG_PATH)


def _music_dir(cfg: dict | None = None) -> Path:
    """Папка музыки. Относительный путь из конфига — от папки проекта, а не от cwd."""
    if cfg is None:
        cfg = _load_config()
    p = Path((cfg.get("output") or {}).get("directory", "./Music")).expanduser()
    return (p if p.is_absolute() else BASE_DIR / p).resolve()


def _profile_url(value: str) -> str:
    """Имя или любая ссылка на профиль SoundCloud → URL страницы лайков."""
    return f"https://soundcloud.com/{_sc_username(value)}/likes"


def _sc_username(value: str) -> str:
    v = (value or "").strip()
    m = re.search(r"soundcloud\.com/([^/?#\s]+)", v)
    return m.group(1) if m else v.strip("/@ ")


def _extract_username(profile_url: str) -> str:
    if not profile_url:
        return ""
    user = _sc_username(profile_url)
    return "" if user == "YOUR_USERNAME" else user


def _settings(cfg: dict | None = None) -> dict:
    """Настройки в том виде, в каком их видит интерфейс (с умолчаниями)."""
    cfg = _load_config() if cfg is None else cfg
    sc = cfg.get("soundcloud") or {}
    out = cfg.get("output") or {}
    ym = cfg.get("yandex_music") or {}
    au = cfg.get("autosync") or {}
    username = _extract_username(sc.get("profile_url", ""))
    return {
        "soundcloud": {
            "username": username,
            "profile_url": _profile_url(username) if username else "",
            "max_tracks": int(sc.get("max_tracks") or 0),
            "sleep_requests": float(sc.get("sleep_requests", 1.5)),
            "cookies_browser": sc.get("cookies_browser") or "",
        },
        "output": {
            "directory": str(out.get("directory", "./Music")),
            "resolved_directory": str(_music_dir(cfg)),
            "format": str(out.get("format", "mp3")),
            "quality": str(out.get("quality", "320")),
        },
        "yandex_music": {
            "playlist_url": ym.get("playlist_url") or "",
            "upload_delay": float(ym.get("upload_delay", 3)),
            "delete_after_upload": bool(ym.get("delete_after_upload", False)),
        },
        "autosync": {
            "enabled": bool(au.get("enabled", False)),
            "interval_hours": int(au.get("interval_hours", 6)),
            "download": bool(au.get("download", True)),
            "upload": bool(au.get("upload", True)),
            "notify": bool(au.get("notify", True)),
        },
    }


def _apply_settings(cfg: dict, data: dict) -> dict:
    """Проверяет частичное обновление настроек и применяет его к cfg.
    Ошибка → ValueError(поле, текст): не записываем ничего."""

    def num(section: str, key: str, value, lo: float, hi: float, cast=float):
        try:
            v = cast(value)
        except (TypeError, ValueError):
            raise ValueError(f"{section}.{key}", "Нужно число") from None
        if not lo <= v <= hi:
            raise ValueError(f"{section}.{key}", f"Допустимо от {lo:g} до {hi:g}")
        return v

    def choice(section: str, key: str, value, allowed):
        v = str(value)
        if v not in allowed:
            raise ValueError(f"{section}.{key}", "Недопустимое значение")
        return v

    sc_in = data.get("soundcloud") or {}
    sc = cfg.setdefault("soundcloud", {})
    if "username" in sc_in:
        user = _sc_username(str(sc_in["username"] or ""))
        sc["profile_url"] = _profile_url(user) if user else ""
    if "max_tracks" in sc_in:
        sc["max_tracks"] = num("soundcloud", "max_tracks", sc_in["max_tracks"], 0, 100000, int)
    if "sleep_requests" in sc_in:
        sc["sleep_requests"] = num("soundcloud", "sleep_requests", sc_in["sleep_requests"], 0, 30)
    if "cookies_browser" in sc_in:
        cb = choice("soundcloud", "cookies_browser", sc_in["cookies_browser"] or "", COOKIE_BROWSERS)
        if cb:
            sc["cookies_browser"] = cb
        else:
            sc.pop("cookies_browser", None)

    out_in = data.get("output") or {}
    out = cfg.setdefault("output", {})
    if "directory" in out_in:
        d = str(out_in["directory"] or "").strip()
        if not d:
            raise ValueError("output.directory", "Укажите папку")
        out["directory"] = d
    if "format" in out_in:
        out["format"] = choice("output", "format", out_in["format"], FORMATS)
    if "quality" in out_in:
        out["quality"] = choice("output", "quality", out_in["quality"], QUALITIES)

    ym_in = data.get("yandex_music") or {}
    ym = cfg.setdefault("yandex_music", {})
    if "playlist_url" in ym_in:
        url = str(ym_in["playlist_url"] or "").strip()
        if url and not re.match(r"https://music\.yandex\.[a-z]{2,3}/", url):
            raise ValueError("yandex_music.playlist_url", "Нужна ссылка вида https://music.yandex.ru/…")
        ym["playlist_url"] = url
    if "upload_delay" in ym_in:
        ym["upload_delay"] = num("yandex_music", "upload_delay", ym_in["upload_delay"], 0, 60)
    if "delete_after_upload" in ym_in:
        ym["delete_after_upload"] = bool(ym_in["delete_after_upload"])

    au_in = data.get("autosync") or {}
    if au_in:
        au = cfg.setdefault("autosync", {})
        for key in ("enabled", "download", "upload", "notify"):
            if key in au_in:
                au[key] = bool(au_in[key])
        if "interval_hours" in au_in:
            v = num("autosync", "interval_hours", au_in["interval_hours"], 1, 24, int)
            if v not in AUTOSYNC_INTERVALS:
                raise ValueError("autosync.interval_hours", "Недопустимый интервал")
            au["interval_hours"] = v
    return cfg


# ── Files helpers ────────────────────────────────────────────────────────

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


# ── Tracks DB (CSV) ──────────────────────────────────────────────────────
# Key = SoundCloud numeric ID (str).
# Value = {title, artist, sc_url, status, file, added, error}

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
                    "title": row.get("title") or "",
                    "artist": row.get("artist") or "",
                    "sc_url": row.get("sc_url") or "",
                    "status": row.get("status") or "pending",
                    "file": _abs_file(row.get("file")),
                    "added": row.get("added") or "",
                    "error": row.get("error") or "",
                    "sent_at": row.get("sent_at") or "",
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


def _track_update(sc_id: str, **fields) -> dict:
    with _db_lock:
        tracks = _load_tracks()
        entry = tracks.get(sc_id, {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        tracks[sc_id] = entry
        _save_tracks(tracks)
        return entry


def _track_view(sc_id: str, info: dict) -> dict:
    f = info.get("file")
    return {
        "id": sc_id,
        "title": info.get("title") or sc_id,
        "artist": info.get("artist") or "",
        "status": info.get("status") or "pending",
        "added": info.get("added") or "",
        "error": info.get("error") or "",
        "has_file": bool(f and Path(f).exists()),
        "sc_url": info.get("sc_url") or "",
        "sent_at": float(info["sent_at"]) if info.get("sent_at") else None,
    }


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
            if info.get("status") in ("downloaded", "sent", "uploaded"):
                existing.add(key)
            elif key in existing and info.get("status") in ("pending", "not_in_likes", "unavailable"):
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


def _music_stats() -> dict:
    tracks = _load_tracks()
    by_status = Counter(t.get("status") or "pending" for t in tracks.values())
    music_dir = _music_dir()
    size = 0
    local_files = 0
    if music_dir.exists():
        for f in music_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                size += f.stat().st_size
                local_files += 1
    return {
        "total": len(tracks),
        **{s: by_status.get(s, 0) for s in STATUSES},
        "not_in_ym": len(tracks) - by_status.get("uploaded", 0),
        "with_errors": sum(1 for t in tracks.values()
                           if t.get("error") and t.get("status") != "unavailable"),
        "local_files": local_files,
        "size_mb": round(size / (1024 * 1024), 1),
    }


# ── Events & history ─────────────────────────────────────────────────────
_SKIP_IN_HISTORY = {"downloading", "uploading", "ping", "hello", "scan_progress"}


def _broadcast(event: dict) -> None:
    rec = _current_rec
    if rec is not None and event.get("type") not in _SKIP_IN_HISTORY:
        if len(rec["events"]) < HISTORY_EVENTS_LIMIT:
            rec["events"].append({**event, "t": round(time.time(), 1)})
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


def _load_history() -> list[dict]:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            items = json.load(f)
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _append_history(rec: dict) -> None:
    with _history_lock:
        items = [rec, *_load_history()][:HISTORY_LIMIT]
        tmp = HISTORY_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        tmp.replace(HISTORY_PATH)


def _notify(title: str, text: str) -> None:
    """Уведомление macOS (Центр уведомлений) через osascript."""
    script = (f"display notification {json.dumps(text, ensure_ascii=False)} "
              f"with title {json.dumps(title, ensure_ascii=False)}")
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, check=False,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _notify_autosync(rec: dict) -> None:
    if not _settings()["autosync"]["notify"]:
        return
    s = rec.get("summary") or {}
    if rec["status"] == "error":
        _notify("Музыкальный", f"Автосинхронизация: ошибка — {s.get('message', '')[:120]}")
        return
    parts = []
    if s.get("new_count"):
        parts.append(f"новых лайков: {s['new_count']}")
    if s.get("downloaded"):
        parts.append(f"скачано: {s['downloaded']}")
    if s.get("uploaded"):
        parts.append(f"в ЯМ: {s['uploaded']}")
    errors = (s.get("failures") or 0) + (s.get("upload_errors") or 0)
    if errors:
        parts.append(f"ошибок: {errors}")
    if parts:
        _notify("Музыкальный", "Автосинхронизация — " + ", ".join(parts))


# ── Task runner ──────────────────────────────────────────────────────────

def _set_task(name: str | None) -> None:
    global _active_task, _task_trigger, _task_started_at
    with _task_lock:
        _active_task = name
        if name is None:
            _task_trigger = None
            _task_started_at = None


def _start_task(name: str, fn, *args, trigger: str = "manual") -> str | None:
    """Запускает фоновую задачу. Возвращает текст ошибки, если что-то уже выполняется
    (в т.ч. если прошлый воркер ещё не доостановился после «Остановить»)."""
    global _active_task, _task_trigger, _task_started_at, _worker
    with _task_lock:
        if _active_task:
            return f"Уже выполняется: {TASK_TITLES.get(_active_task, _active_task)}"
        if _worker is not None and _worker.is_alive():
            # active_task уже сброшен — воркер дописывает историю; ждём его недолго.
            _worker.join(timeout=3)
            if _worker.is_alive():
                return "Предыдущая задача ещё останавливается — подождите пару секунд"
        _active_task = name
        _task_trigger = trigger
        _task_started_at = time.time()
        _cancel.clear()
        _worker = threading.Thread(target=_run_task, args=(name, trigger, fn, args),
                                   daemon=True, name=f"task-{name}")
        _worker.start()
    return None


def _run_task(name: str, trigger: str, fn, args: tuple) -> None:
    """Обёртка задачи: события task_start/task_end, отмена, ошибки, запись в историю."""
    global _current_rec
    rec = {"id": uuid.uuid4().hex[:12], "task": name, "trigger": trigger,
           "started_at": time.time(), "finished_at": None,
           "status": "ok", "summary": {}, "events": []}
    _current_rec = rec
    _broadcast({"type": "task_start", "task": name, "trigger": trigger})
    try:
        summary = fn(*args) or {}
        rec["summary"] = summary
        if summary.get("level") in ("error", "warn"):
            rec["status"] = summary["level"]
    except TaskCancelled as c:
        rec["status"] = "cancelled"
        rec["summary"] = c.counts
        _broadcast({"type": "cancelled", **c.counts})
    except TaskError as e:
        rec["status"] = "error"
        rec["summary"] = {"message": str(e)}
        _broadcast({"type": "error", "message": str(e)})
    except Exception as e:
        msg = f"{e!s:.300}"
        rec["status"] = "error"
        rec["summary"] = {"message": msg}
        _broadcast({"type": "error", "message": msg})
    finally:
        try:
            _cleanup_partials()
        except OSError:
            pass
        rec["finished_at"] = time.time()
        _current_rec = None
        try:
            _append_history(rec)
        except OSError:
            pass
        _set_task(None)
        _broadcast({"type": "task_end", "task": name, "status": rec["status"],
                    "history_id": rec["id"]})
        if trigger == "auto":
            # Отдельным потоком: osascript не должен задерживать завершение задачи.
            threading.Thread(target=_notify_autosync, args=(rec,), daemon=True).start()


class _SilentLogger:
    def debug(self, msg: str) -> None: pass
    def info(self, msg: str) -> None: pass
    def warning(self, msg: str) -> None: pass
    def error(self, msg: str) -> None: pass


# ── SoundCloud: scan ─────────────────────────────────────────────────────

def _fetch_likes(sc: dict, url: str, max_tracks: int) -> list[dict]:
    """Лайки SoundCloud постранично (по 200): прогресс и отмена между страницами."""
    global _browser_cookies_ok
    opts: dict = {"quiet": True, "no_color": True, "skip_download": True,
                  "logger": _SilentLogger()}
    cookies_browser = sc.get("cookies_browser")
    if cookies_browser and _browser_cookies_ok is not False:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    def run() -> list[dict]:
        entries: list[dict] = []
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
            for e in (info or {}).get("entries") or []:
                _check_cancel()
                if not e or not e.get("id"):
                    continue
                entries.append(e)
                if len(entries) % 50 == 0:
                    _broadcast({"type": "scan_progress", "found": len(entries)})
                if max_tracks and len(entries) >= max_tracks:
                    break
        _broadcast({"type": "scan_progress", "found": len(entries)})
        return entries

    try:
        return run()
    except TaskCancelled:
        raise
    except Exception as e:
        # Браузер не установлен / профиль не найден — пробуем без cookies:
        # для публичных лайков они и не нужны.
        if _is_cookie_db_error(str(e)) and "cookiesfrombrowser" in opts:
            _browser_cookies_ok = False
            opts.pop("cookiesfrombrowser", None)
            _broadcast({"type": "log", "level": "warn", "message":
                        f"Cookies браузера «{cookies_browser}» недоступны — проверяю без них"})
            try:
                return run()
            except TaskCancelled:
                raise
            except Exception as e2:
                raise TaskError(f"Ошибка проверки лайков: {e2!s:.200}") from e2
        if "404" in str(e):
            raise TaskError("Профиль SoundCloud не найден — проверьте имя в настройках") from e
        raise TaskError(f"Ошибка проверки лайков: {e!s:.200}") from e


def _do_scan(config: dict) -> dict:
    sc = config.get("soundcloud") or {}
    username = _extract_username(sc.get("profile_url", ""))
    if not username:
        raise TaskError("Профиль SoundCloud не задан — укажите его в настройках")
    _broadcast({"type": "scanning"})
    max_tracks = int(sc.get("max_tracks") or 0)
    entries = _fetch_likes(sc, _profile_url(username), max_tracks)
    if not entries:
        # Пустой ответ без исключения (сбой SoundCloud, опечатка в имени) не должен
        # переводить все ожидающие треки в «пропал из лайков».
        raise TaskError("SoundCloud вернул пустой список лайков — статусы не изменены. "
                        "Проверьте имя пользователя и повторите.")

    with _db_lock:
        tracks = _load_tracks()
        new_count = 0
        known = 0
        for e in entries:
            sc_id = str(e.get("id"))
            prev = tracks.get(sc_id)
            sc_url = e.get("url") or e.get("webpage_url") or (prev or {}).get("sc_url", "")
            if prev and prev.get("status") in ("downloaded", "sent", "uploaded", "unavailable"):
                known += 1
                if not prev.get("title"):
                    prev["title"] = e.get("title") or ""
                if not prev.get("sc_url"):
                    prev["sc_url"] = sc_url
                continue
            if prev is None:
                new_count += 1
            else:
                known += 1
            # Плоский скан не отдаёт исполнителя — не затираем уже известного.
            tracks[sc_id] = {
                "title": e.get("title") or (prev or {}).get("title", ""),
                "artist": (prev or {}).get("artist", ""),
                "sc_url": sc_url,
                "status": "pending",
                "file": (prev or {}).get("file"),
                "added": (prev or {}).get("added") or time.strftime("%Y-%m-%d"),
                "error": (prev or {}).get("error", ""),
                "sent_at": "",
            }
        not_in_likes_n = 0
        # С лимитом max_tracks список неполный — «пропавшие» за лимитом не пропали.
        if not max_tracks:
            seen_ids = {str(e.get("id")) for e in entries}
            for sid, info in tracks.items():
                if info.get("status") == "pending" and sid not in seen_ids and not info.get("file"):
                    info["status"] = "not_in_likes"
                    not_in_likes_n += 1
        _save_tracks(tracks)

    summary = {"total": len(entries), "new_count": new_count,
               "already": known, "not_in_likes": not_in_likes_n}
    _broadcast({"type": "scan_complete", **summary})
    return summary


# ── SoundCloud: download ─────────────────────────────────────────────────

def _unavailable_reason(message: str) -> str | None:
    for pattern, reason in UNAVAILABLE_PATTERNS:
        if pattern.lower() in message.lower():
            return reason
    return None


def _do_download(config: dict, track_ids: list | None) -> dict:
    """Скачивает треки из локальной базы (tracks.csv) по sc_url.

    Работает по базе, а не по результату последнего скана: после перезапуска
    приложения скачивание доступно сразу. track_ids=None — все «pending».
    """
    global _browser_cookies_ok
    sc = config.get("soundcloud") or {}
    out = config.get("output") or {}

    output_dir = _music_dir(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_fmt = out.get("format", "mp3")
    quality = str(out.get("quality", "320"))
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
        raise TaskError("Нет выбранных треков")

    total = len(selected)
    processed = 0
    saved_count = 0
    errors: list[str] = []
    failures = 0
    last_filepath: list[str | None] = [None]
    last_meta: list[dict] = [{}]
    current_id: list[str | None] = [None]
    last_emit = [0.0]

    _sync_archive()
    # yt-dlp читает archive.txt в память при создании загрузчика, поэтому id треков
    # без файла (например, удалённых после загрузки в ЯМ) убираем заранее — иначе
    # он молча пропустит трек, и в UI будет «не скачан» без причины.
    for sid in selected:
        f = existing_tracks[sid].get("file")
        if not (f and Path(f).exists()):
            _remove_from_archive(sid)
    _broadcast({"type": "dl_start", "total": total})

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

    def _fail(sc_id: str, prev_status: str, title: str, problem: str, raw: str) -> None:
        """Ошибка по треку: сохраняем её; неустранимую — как статус unavailable."""
        nonlocal failures
        failures += 1
        reason = _unavailable_reason(raw)
        if reason and prev_status not in ("uploaded", "sent"):
            entry = _track_update(sc_id, status="unavailable", error=reason)
            message = f"{title}: {reason}"
        else:
            message = f"{title}: {problem}"
            entry = _track_update(sc_id, error=(reason or raw or problem)[:300])
        _broadcast({"type": "log", "id": sc_id, "level": "error", "message": message})
        _broadcast({"type": "track_status", "id": sc_id,
                    "status": entry.get("status"), "error": entry.get("error", "")})

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

    def _counts() -> dict:
        return {"downloaded": saved_count, "failures": failures}

    ydl = yt_dlp.YoutubeDL(dl_opts)
    try:
        for sc_id in selected:
            _check_cancel(_counts())
            entry = existing_tracks[sc_id]
            title_hint = (entry.get("title") or sc_id)[:80]
            prev_status = entry.get("status") or "pending"
            # Перекачка уже отправленного в ЯМ трека не должна сбрасывать «uploaded»/«sent».
            new_status = prev_status if prev_status in ("uploaded", "sent") else "downloaded"
            current_id[0] = sc_id

            existing_file = entry.get("file")
            if existing_file and Path(existing_file).exists():
                processed += 1
                saved_count += 1
                if prev_status != new_status or entry.get("error"):
                    _track_update(sc_id, status=new_status, error="")
                _done(sc_id, title_hint, new_status)
                continue

            url = entry.get("sc_url", "")
            if not url:
                processed += 1
                _fail(sc_id, prev_status, title_hint, "нет ссылки на SoundCloud", "")
                continue
            last_filepath[0] = None
            last_meta[0] = {}
            errors_before = len(errors)
            dl_err: str | None = None
            try:
                ydl.download([url])
            except Exception as e:
                _check_cancel(_counts())  # отмена — не ошибка скачивания
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
            _check_cancel(_counts())
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
                    error="",
                )
                saved_count += 1
                _done(sc_id, title or title_hint, new_status)
            else:
                raw = dl_err or next(iter(errors[errors_before:][-1:]), "")
                problem = f"не скачан: {raw[:200]}" if raw else "не скачан"
                _fail(sc_id, prev_status, title_hint, problem, raw)
    finally:
        ydl.close()

    _broadcast({"type": "dl_complete", "downloaded": saved_count,
                "errors": len(errors) + failures, "failures": failures})
    return {"downloaded": saved_count, "failures": failures, "selected": selected}


# ── Yandex Music ─────────────────────────────────────────────────────────

def _ym_connect(playlist_url: str | None = None) -> dict:
    """Сессия ЯМ по cookies Chrome (+ kind плейлиста). Ошибки → TaskError с понятным текстом."""
    global _auth_cache
    from ym_uploader import _create_session, _get_auth, _resolve_playlist_kind
    try:
        session = _create_session()
        auth = _get_auth(session)
    except Exception as e:
        raise TaskError(str(e)[:300]) from e
    if not auth.get("logged"):
        raise TaskError(auth.get("error") or "Не авторизованы в Яндекс Музыке")
    _auth_cache = {"at": time.monotonic(), "data": {
        "authorized": True, "login": auth.get("login", "?"), "uid": auth.get("uid")}}
    ctx = {"session": session, "uid": auth["uid"], "token": auth["token"],
           "login": auth.get("login", "")}
    if playlist_url is not None:
        try:
            kind = _resolve_playlist_kind(session, playlist_url, auth["token"], auth["uid"])
        except Exception as e:
            raise TaskError(str(e)[:300]) from e
        if not kind:
            raise TaskError("Не удалось определить плейлист по ссылке")
        ctx["kind"] = kind
    return ctx


def _check_auth(refresh: bool = False) -> dict:
    """Проверка авторизации ЯМ с кэшем: успешный результат живёт AUTH_CACHE_TTL секунд."""
    global _auth_cache
    if (not refresh and _auth_cache is not None
            and time.monotonic() - _auth_cache["at"] < AUTH_CACHE_TTL):
        return {**_auth_cache["data"], "cached": True}
    try:
        _ym_connect()
        return {**_auth_cache["data"], "cached": False}
    except TaskError as e:
        _auth_cache = None
        err = str(e)
        if "404" in err:
            err = "API Яндекс Музыки недоступно (404) — обновите приложение"
        return {"authorized": False, "error": err}


def _do_upload(config: dict, track_ids: list | None = None) -> dict:
    """Загрузка треков в ЯМ; пишет upload_archive.txt как CLI.

    track_ids — именно эти треки (даже уже «uploaded»/«sent»: выбор пользователя в UI имеет
    приоритет, UI предупреждает о дублях). Иначе — все со статусом «downloaded».
    Подтверждённый по id трек → «uploaded»; принятый, но ещё не найденный в плейлисте →
    «sent» (подтверждается позже в _confirm_sent). «Мягкие» отказы (нет плейлиста, нет
    авторизации) — через level/message, чтобы итог скачивания в той же задаче не терялся.
    """
    from ym_uploader import DEFAULT_UPLOAD_ARCHIVE, _record_uploaded, _upload_one, get_playlist_tracks

    ym = config.get("yandex_music") or {}
    playlist_url = ym.get("playlist_url", "")
    if not playlist_url:
        return {"uploaded": 0, "level": "warn",
                "message": "Плейлист ЯМ не задан — загрузка пропущена"}

    tracks = _load_tracks()
    if track_ids is not None:
        selected = {str(tid) for tid in track_ids}
        to_upload = [(sid, info) for sid, info in tracks.items()
                     if sid in selected and info.get("file") and Path(info["file"]).exists()]
        if not to_upload:
            return {"uploaded": 0, "level": "warn",
                    "message": "У выбранных треков нет файлов — сначала скачайте их"}
    else:
        to_upload = [(sid, info) for sid, info in tracks.items()
                     if info.get("status") == "downloaded"
                     and info.get("file") and Path(info["file"]).exists()]
        if not to_upload:
            return {"uploaded": 0, "level": "warn", "message": "Нет треков для загрузки в ЯМ"}

    _broadcast({"type": "upload_start", "total": len(to_upload), "skipped": 0})
    try:
        ctx = _ym_connect(playlist_url)
    except TaskError as e:
        return {"uploaded": 0, "level": "error", "message": str(e)}
    session, uid, token, kind = ctx["session"], ctx["uid"], ctx["token"], ctx["kind"]

    uploaded = 0      # подтверждены по id → «uploaded»
    sent_ids: list[str] = []  # приняты, ждут появления в плейлисте → «sent»
    upload_errors = 0
    upload_delay = float(ym.get("upload_delay", 3))
    del_after = bool(ym.get("delete_after_upload", False))

    def _counts() -> dict:
        return {"uploaded": uploaded, "sent": len(sent_ids)}

    for i, (sc_id, info) in enumerate(to_upload):
        _check_cancel(_counts())
        filepath = Path(info["file"])
        name = filepath.stem
        _broadcast({
            "type": "uploading",
            "id": sc_id,
            "title": (name[:50] + "…") if len(name) > 50 else name,
            "uploaded": i,
            "total": len(to_upload),
        })
        try:
            res = _upload_one(session, uid, kind, filepath, token, cancel_event=_cancel,
                              verify_retries=UPLOAD_VERIFY_RETRIES, verify_delay=UPLOAD_VERIFY_DELAY)
            if res:
                _record_uploaded(DEFAULT_UPLOAD_ARCHIVE, filepath)
                if res is True:
                    uploaded += 1
                    _track_update(sc_id, status="uploaded", error="", sent_at="")
                    # Удаляем только при точном подтверждении по id трека.
                    if del_after:
                        _delete_file_safe(str(filepath.resolve()))
                        _track_update(sc_id, file="")
                    _broadcast({"type": "track_uploaded", "id": sc_id})
                else:
                    # «accepted»/«grown»: ЯМ принял файл, но трек ещё не в плейлисте — обработка
                    # может молча не удаться, поэтому это не «В ЯМ», а «Ждёт ЯМ».
                    sent_ids.append(sc_id)
                    _track_update(sc_id, status="sent", error="", sent_at=str(int(time.time())))
                    _broadcast({"type": "track_status", "id": sc_id, "status": "sent", "error": ""})
                    _broadcast({"type": "log", "id": sc_id, "level": "info",
                                "message": f"{name}: принят, ЯМ обрабатывает — станет «В ЯМ», "
                                           "когда появится в плейлисте"})
            else:
                upload_errors += 1
                _track_update(sc_id, error="Не удалось загрузить в ЯМ")
                _broadcast({"type": "log", "id": sc_id, "level": "error",
                            "message": f"{name}: не удалось загрузить"})
        except Exception as e:
            _check_cancel(_counts())
            upload_errors += 1
            _track_update(sc_id, error=f"ЯМ: {e!s:.200}")
            _broadcast({"type": "log", "id": sc_id, "level": "error", "message": f"{name}: {e!s:.100}"})
        if _cancel.wait(upload_delay):
            break  # «Остановить» прерывает и паузу между загрузками
    _check_cancel(_counts())

    # Один запрос без ожидания: треки, отправленные в начале длинной загрузки, могли уже появиться.
    if sent_ids:
        try:
            _confirm_sent(get_playlist_tracks(session, uid, kind, token))
        except Exception:
            pass  # проверим позже — фоном или при сверке
    now_tracks = _load_tracks()
    confirmed_late = sum(1 for sid in sent_ids if now_tracks.get(sid, {}).get("status") == "uploaded")
    uploaded += confirmed_late
    sent = len(sent_ids) - confirmed_late

    result = {"uploaded": uploaded, "sent": sent, "upload_errors": upload_errors}
    if upload_errors:
        result["level"] = "warn"
    return result


def _pl_matcher(pl_tracks: list[dict]):
    """Функция «есть ли трек базы в плейлисте» — по исполнителю и названию, как в «Сверке»."""
    keys = {(_norm(t["artist"]), _norm(t["title"])) for t in pl_tracks}
    titles = {_norm(t["title"]) for t in pl_tracks if t["title"]}

    def found(info: dict) -> bool:
        k = (_norm(info.get("artist")), _norm(info.get("title")))
        return k in keys or bool(k[1] and k[1] in titles)
    return found


def _confirm_sent(pl_tracks: list[dict] | None = None) -> dict:
    """Проверка треков «Ждёт ЯМ» (sent) по плейлисту.

    Найден → «uploaded». Не найден дольше SENT_TIMEOUT → обратно «downloaded» (есть файл)
    или «pending» с ошибкой, файл убирается из upload_archive.txt — чтобы его можно было
    отправить снова. Иначе остаётся «sent». pl_tracks=None — плейлист загружается здесь.
    """
    from ym_uploader import DEFAULT_UPLOAD_ARCHIVE, _forget_uploaded, get_playlist_tracks
    if not any(t.get("status") == "sent" for t in _load_tracks().values()):
        return {"confirmed": 0, "expired": 0, "waiting": 0}
    if pl_tracks is None:
        ctx = _ym_connect((_load_config().get("yandex_music") or {}).get("playlist_url", ""))
        pl_tracks = get_playlist_tracks(ctx["session"], ctx["uid"], ctx["kind"], ctx["token"])
    found = _pl_matcher(pl_tracks)
    now = time.time()
    events: list[dict] = []
    forget: set[str] = set()
    confirmed = expired = waiting = 0
    with _db_lock:
        tracks = _load_tracks()
        for sid, info in tracks.items():
            if info.get("status") != "sent":
                continue
            if found(info):
                info.update(status="uploaded", sent_at="", error="")
                confirmed += 1
                events.append({"type": "track_uploaded", "id": sid})
                continue
            sent_at = float(info.get("sent_at") or 0) or now
            if not info.get("sent_at"):
                info["sent_at"] = str(int(now))
            if now - sent_at > SENT_TIMEOUT:
                fpath = info.get("file")
                has_file = bool(fpath and Path(fpath).exists())
                info.update(status="downloaded" if has_file else "pending", sent_at="",
                            error="ЯМ не принял трек: не появился в плейлисте за сутки")
                if fpath:
                    forget.add(Path(fpath).name)
                expired += 1
                events.append({"type": "track_status", "id": sid,
                               "status": info["status"], "error": info["error"]})
            else:
                waiting += 1
        if confirmed or expired or waiting:
            _save_tracks(tracks)
    if forget:
        _forget_uploaded(DEFAULT_UPLOAD_ARCHIVE, forget)
        _sync_archive()
    for ev in events:
        _broadcast(ev)
    if confirmed or expired:
        _broadcast({"type": "log", "level": "warn" if expired else "info",
                    "message": f"Проверка отправленных: подтверждено «В ЯМ» — {confirmed}"
                               + (f", не приняты ЯМ за сутки — {expired}" if expired else "")
                               + (f", ещё ждут — {waiting}" if waiting else "")})
    return {"confirmed": confirmed, "expired": expired, "waiting": waiting}


def _playlist_report(config: dict) -> dict:
    """Сверка треков «в ЯМ» из базы с плейлистом: ненайденные, дубли, в обработке.
    Заодно подтверждает «Ждёт ЯМ» (sent), которые уже появились (_confirm_sent)."""
    global _last_report
    from ym_uploader import get_playlist_info, get_playlist_tracks
    playlist_url = (config.get("yandex_music") or {}).get("playlist_url", "")
    if not playlist_url:
        raise TaskError("Плейлист ЯМ не задан — укажите ссылку в настройках")
    ctx = _ym_connect(playlist_url)
    try:
        pl_info = get_playlist_info(ctx["session"], ctx["uid"], ctx["kind"], ctx["token"])
        pl_tracks = get_playlist_tracks(ctx["session"], ctx["uid"], ctx["kind"], ctx["token"])
    except Exception as e:
        raise TaskError(f"Ошибка API Яндекс Музыки: {e!s:.200}") from e

    confirm = _confirm_sent(pl_tracks)

    pl_keys = [(_norm(t["artist"]), _norm(t["title"])) for t in pl_tracks]
    first_seen: dict[tuple, dict] = {}
    for key, t in zip(pl_keys, pl_tracks):
        first_seen.setdefault(key, t)
    dups = [
        {"artist": first_seen[k]["artist"], "title": first_seen[k]["title"], "count": c}
        for k, c in Counter(pl_keys).most_common() if c > 1 and k != ("", "")
    ]
    processing = [{"artist": t["artist"], "title": t["title"]}
                  for t in pl_tracks if not t["available"]]

    found_in_pl = _pl_matcher(pl_tracks)
    tracks = _load_tracks()
    found = 0
    missing: list[dict] = []
    waiting: list[dict] = []
    for sid, info in tracks.items():
        if info.get("status") == "sent":
            waiting.append(_track_view(sid, info))
        elif info.get("status") == "uploaded":
            if found_in_pl(info):
                found += 1
            else:
                missing.append(_track_view(sid, info))
    _last_report = {
        "checked_at": time.time(),
        "playlist": pl_info,
        "playlist_total": len(pl_tracks),
        "found": found,
        "missing_total": len(missing),
        "missing": missing,
        "duplicates": sum(d["count"] - 1 for d in dups),
        "duplicates_list": dups,
        "processing": len(processing),
        "processing_list": processing,
        "sent_waiting": waiting,
        "confirmed_now": confirm["confirmed"],
        "expired_now": confirm["expired"],
    }
    return _last_report


# ── Tasks ────────────────────────────────────────────────────────────────

def _task_scan(config: dict) -> dict:
    return _do_scan(config)


def _task_download(config: dict, track_ids: list | None, do_upload: bool) -> dict:
    dl = _do_download(config, track_ids)
    summary = {"downloaded": dl["downloaded"], "failures": dl["failures"]}
    if do_upload:
        try:
            summary.update(_do_upload(config, track_ids=dl["selected"]))
        except TaskCancelled as c:
            raise TaskCancelled({**summary, **c.counts}) from None
    _broadcast({"type": "all_done", "task": "download", **summary})
    return summary


def _task_upload(config: dict, track_ids: list | None) -> dict:
    summary = _do_upload(config, track_ids)
    _broadcast({"type": "all_done", "task": "upload", **summary})
    return summary


def _task_sync(config: dict, opts: dict | None = None) -> dict:
    """«Синхронизировать»: проверить лайки → скачать новые → загрузить в ЯМ → сверить."""
    opts = opts or {}
    summary: dict = {}
    _broadcast({"type": "sync_step", "step": "scan"})
    summary["new_count"] = _do_scan(config)["new_count"]

    if opts.get("download", True):
        _check_cancel(summary)
        pending = [sid for sid, t in _load_tracks().items() if t.get("status") == "pending"]
        if pending:
            _broadcast({"type": "sync_step", "step": "download"})
            try:
                dl = _do_download(config, pending)
            except TaskCancelled as c:
                raise TaskCancelled({**summary, **c.counts}) from None
            summary.update(downloaded=dl["downloaded"], failures=dl["failures"])

    playlist_url = (config.get("yandex_music") or {}).get("playlist_url", "")
    if opts.get("upload", True) and playlist_url:
        _check_cancel(summary)
        _broadcast({"type": "sync_step", "step": "upload"})
        try:
            up = _do_upload(config)
        except TaskCancelled as c:
            raise TaskCancelled({**summary, **c.counts}) from None
        if up.get("level") == "error" or up.get("uploaded") or up.get("sent") or up.get("upload_errors"):
            summary.update(up)
        _check_cancel(summary)
        if summary.get("level") != "error":
            _broadcast({"type": "sync_step", "step": "verify"})
            try:
                rep = _playlist_report(config)  # заодно подтверждает «Ждёт ЯМ»
                summary["missing"] = rep["missing_total"]
                summary["duplicates"] = rep["duplicates"]
                summary["sent"] = len(rep["sent_waiting"])
                if rep["confirmed_now"] and "uploaded" in summary:
                    summary["uploaded"] += rep["confirmed_now"]
            except TaskError as e:
                summary.update(level="warn", message=f"Сверка не выполнена: {e}")

    if summary.get("failures") and not summary.get("level"):
        summary["level"] = "warn"
    _broadcast({"type": "all_done", "task": "sync", **summary})
    return summary


# ── Autosync scheduler ───────────────────────────────────────────────────

def _last_auto_run() -> float | None:
    for rec in _load_history():
        if rec.get("trigger") == "auto":
            return rec.get("started_at")
    return None


def _autosync_state(settings: dict | None = None) -> dict:
    au = (settings or _settings())["autosync"]
    last = _last_auto_run()
    next_run = None
    if au["enabled"]:
        next_run = last + au["interval_hours"] * 3600 if last else time.time()
    return {**au, "last_run": last, "next_run": next_run}


def _scheduler_loop() -> None:
    """Раз в 30 с: если автосинхронизация включена и подошло время — запускаем sync.
    Занято другой задачей — попробуем на следующем тике.
    Раз в SENT_CHECK_INTERVAL, пока есть «Ждёт ЯМ» и нет задачи, — проверка отправленных."""
    last_sent_check = 0.0
    while True:
        time.sleep(30)
        try:
            if (not _active_task and time.time() - last_sent_check >= SENT_CHECK_INTERVAL
                    and any(t.get("status") == "sent" for t in _load_tracks().values())):
                last_sent_check = time.time()
                _confirm_sent()
        except Exception:
            pass  # нет авторизации ЯМ и т.п. — попробуем через интервал
        try:
            cfg = _load_config()
            s = _settings(cfg)
            state = _autosync_state(s)
            if not state["enabled"] or not s["soundcloud"]["username"] or _active_task:
                continue
            if state["next_run"] is not None and time.time() >= state["next_run"]:
                _start_task("sync", _task_sync, cfg,
                            {"download": state["download"], "upload": state["upload"]},
                            trigger="auto")
        except Exception:
            pass


# ── Mark / unmark ────────────────────────────────────────────────────────

def _mark_uploaded(track_ids: list[str] | None = None, *, mark_all_pending: bool = False) -> int:
    """Помечает выбранные (или все не загруженные) как уже в ЯМ.

    Пути существующих файлов дописываются в upload_archive.txt, чтобы CLI
    (sc_downloader.py --upload) не залил их повторно.
    """
    from ym_uploader import DEFAULT_UPLOAD_ARCHIVE, _record_uploaded
    movable = ("pending", "downloaded", "sent", "not_in_likes", "unavailable")
    marked_files: list[Path] = []
    with _db_lock:
        tracks = _load_tracks()
        if mark_all_pending:
            target = {sid for sid, inf in tracks.items() if inf.get("status") in movable}
        elif track_ids is not None:
            target = {str(tid) for tid in track_ids}
        else:
            return 0
        count = 0
        for sid, info in tracks.items():
            if sid not in target or info.get("status") not in movable:
                continue
            info["status"] = "uploaded"
            info["error"] = ""
            info["sent_at"] = ""
            count += 1
            fpath = info.get("file")
            if fpath and Path(fpath).exists():
                marked_files.append(Path(fpath))
        _save_tracks(tracks)
    _sync_archive()
    for fp in marked_files:
        _record_uploaded(DEFAULT_UPLOAD_ARCHIVE, fp)
    return count


def _unmark_uploaded(track_ids: list[str]) -> int:
    """Снимает отметку «В ЯМ» (или «Ждёт ЯМ»): есть файл → downloaded, нет → pending.
    Файл убирается из upload_archive.txt, чтобы его можно было загрузить снова."""
    from ym_uploader import DEFAULT_UPLOAD_ARCHIVE, _forget_uploaded
    names: set[str] = set()
    count = 0
    with _db_lock:
        tracks = _load_tracks()
        for sid in {str(t) for t in track_ids}:
            info = tracks.get(sid)
            if not info or info.get("status") not in ("uploaded", "sent"):
                continue
            fpath = info.get("file")
            has_file = bool(fpath and Path(fpath).exists())
            info["status"] = "downloaded" if has_file else "pending"
            info["sent_at"] = ""
            if fpath:
                names.add(Path(fpath).name)
            count += 1
        _save_tracks(tracks)
    _sync_archive()
    _forget_uploaded(DEFAULT_UPLOAD_ARCHIVE, names)
    return count


# ── SoundCloud profile check ─────────────────────────────────────────────

def _soundcloud_user(user: str) -> dict | None:
    """Профиль SoundCloud: имя и число лайков. None — профиль не найден.

    Сначала через внутренний API экстрактора yt-dlp (даёт likes_count); если он
    поменялся — упрощённая проверка существования через страницу лайков.
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "logger": _SilentLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ie = ydl.get_info_extractor("SoundcloudUser")
            ie.initialize()
            data = ie._call_api(ie._resolv_url(ie._BASE_URL + user), user, headers=ie._HEADERS)
            return {"display_name": data.get("username") or user,
                    "likes_count": data.get("likes_count") or data.get("public_favorites_count")}
        except Exception as e:
            if "404" in str(e):
                return None
        info = ydl.extract_info(f"https://soundcloud.com/{user}/likes",
                                download=False, process=False)
        title = (info or {}).get("title") or ""
        return {"display_name": re.sub(r"\s*\(Likes\)$", "", title) or user,
                "likes_count": None}


# ── Routes: pages ────────────────────────────────────────────────────────

def _asset_version() -> str:
    """Версия статики для сброса кэша браузера: время последнего изменения файлов."""
    try:
        return str(int(max(p.stat().st_mtime for p in STATIC_DIR.rglob("*") if p.is_file())))
    except ValueError:
        return "0"


@app.route("/")
def index():
    return render_template("index.html", v=_asset_version())


# ── Routes: state & settings ─────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    s = _settings()
    return jsonify({
        "username": s["soundcloud"]["username"],
        "configured": bool(s["soundcloud"]["username"]),
        "playlist_url": s["yandex_music"]["playlist_url"],
        "stats": _music_stats(),
        "active_task": _active_task,
        "task": ({"name": _active_task, "trigger": _task_trigger, "started_at": _task_started_at}
                 if _active_task else None),
        "autosync": _autosync_state(s),
    })


@app.route("/api/settings")
def api_settings_get():
    return jsonify(_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Ожидается JSON-объект"}), 400
    cfg = _load_config()
    try:
        _apply_settings(cfg, data)
    except ValueError as e:
        field, message = e.args if len(e.args) == 2 else ("", str(e))
        return jsonify({"error": message, "field": field}), 400
    _save_config(cfg)
    return jsonify({"ok": True, "settings": _settings(cfg)})


@app.route("/api/validate/soundcloud", methods=["POST"])
def api_validate_soundcloud():
    data = request.get_json(silent=True) or {}
    user = _sc_username(str(data.get("username") or ""))
    if not user:
        return jsonify({"ok": False, "error": "Введите имя или ссылку на профиль"})
    try:
        info = _soundcloud_user(user)
    except Exception as e:
        return jsonify({"ok": False, "error": f"SoundCloud недоступен: {e!s:.150}"}), 502
    if info is None:
        return jsonify({"ok": False, "error": f"Профиль «{user}» не найден на SoundCloud"})
    return jsonify({"ok": True, "username": user, "profile_url": _profile_url(user), **info})


@app.route("/api/validate/playlist", methods=["POST"])
def api_validate_playlist():
    from ym_uploader import get_playlist_info
    data = request.get_json(silent=True) or {}
    url = str(data.get("playlist_url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Вставьте ссылку на плейлист"})
    if "music.yandex." not in url:
        return jsonify({"ok": False, "error": "Это не ссылка на Яндекс Музыку"})
    try:
        ctx = _ym_connect(url)
        info = get_playlist_info(ctx["session"], ctx["uid"], ctx["kind"], ctx["token"])
    except TaskError as e:
        return jsonify({"ok": False, "error": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e!s:.200}"})
    return jsonify({"ok": True, **info})


@app.route("/api/check-yandex")
def api_check_yandex():
    return jsonify(_check_auth(refresh=request.args.get("refresh") == "1"))


# ── Routes: tracks ───────────────────────────────────────────────────────

@app.route("/api/tracks")
def api_tracks():
    """Все треки из локальной базы. Порядок: последние добавленные сверху."""
    tracks = _load_tracks()
    return jsonify({"tracks": [_track_view(sid, info)
                               for sid, info in reversed(list(tracks.items()))]})


def _track_file(sc_id: str) -> Path:
    info = _load_tracks().get(sc_id)
    f = info and info.get("file")
    if not f:
        abort(404)
    p = Path(f)
    if not p.exists() or p.suffix.lower() not in AUDIO_EXTS:
        abort(404)
    return p


@app.route("/api/tracks/<sc_id>/audio")
def api_track_audio(sc_id: str):
    p = _track_file(sc_id)
    return send_file(p, mimetype=AUDIO_MIME.get(p.suffix.lower()), conditional=True, max_age=0)


@app.route("/api/tracks/<sc_id>/reveal", methods=["POST"])
def api_track_reveal(sc_id: str):
    p = _track_file(sc_id)
    try:
        subprocess.Popen(["open", "-R", str(p)])
    except OSError as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"ok": True})


def _ids_from_request() -> list[str] | None:
    data = request.get_json(silent=True) or {}
    ids = data.get("track_ids")
    if ids is None:
        return None
    if not isinstance(ids, list):
        raise ValueError("track_ids должен быть списком")
    return [str(i) for i in ids]


@app.route("/api/mark-uploaded", methods=["POST"])
def api_mark_uploaded():
    """Помечает треки как уже в ЯМ: по списку track_ids или все при mark_all_pending: true."""
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
        elif isinstance(track_ids, list) and track_ids:
            count = _mark_uploaded(track_ids)
        else:
            return jsonify({"error": "Передайте track_ids или mark_all_pending: true"}), 400
    return jsonify({"ok": True, "marked": count})


@app.route("/api/unmark-uploaded", methods=["POST"])
def api_unmark_uploaded():
    try:
        ids = _ids_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not ids:
        return jsonify({"error": "Передайте track_ids"}), 400
    with _task_lock:
        if _active_task:
            return jsonify({"error": "Дождитесь завершения текущей задачи"}), 409
        count = _unmark_uploaded(ids)
    return jsonify({"ok": True, "unmarked": count})


# ── Routes: tasks ────────────────────────────────────────────────────────

def _started(err: str | None):
    if err:
        return jsonify({"error": err}), 409
    return jsonify({"ok": True})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    return _started(_start_task("scan", _task_scan, _load_config()))


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    try:
        ids = _ids_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    do_upload = bool(data.get("upload", True))
    return _started(_start_task("download", _task_download, _load_config(), ids, do_upload))


@app.route("/api/upload", methods=["POST"])
def api_upload():
    try:
        ids = _ids_from_request()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return _started(_start_task("upload", _task_upload, _load_config(), ids))


@app.route("/api/sync", methods=["POST"])
def api_sync():
    return _started(_start_task("sync", _task_sync, _load_config(), {}))


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Только сигнал отмены — active_task сбрасывает сам воркер, когда остановится."""
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


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    music_dir = _music_dir()
    music_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(["open", str(music_dir)])
    except OSError as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"ok": True})


@app.route("/api/check-playlist")
def api_check_playlist_last():
    """Последний результат сверки (от кнопки или синхронизации) — без обращения к ЯМ."""
    return jsonify({"report": _last_report})


@app.route("/api/check-playlist", methods=["POST"])
def api_check_playlist():
    """Сверяет локальную базу (uploaded-треки) с реальным содержимым плейлиста ЯМ."""
    cfg = _load_config()
    if not (cfg.get("yandex_music") or {}).get("playlist_url"):
        return jsonify({"error": "URL плейлиста не задан"}), 400
    try:
        return jsonify(_playlist_report(cfg))
    except TaskError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/history")
def api_history():
    items = [{k: v for k, v in rec.items() if k != "events"} | {"events_count": len(rec.get("events", []))}
             for rec in _load_history()]
    return jsonify({"items": items})


@app.route("/api/history/<rec_id>")
def api_history_item(rec_id: str):
    for rec in _load_history():
        if rec.get("id") == rec_id:
            return jsonify(rec)
    abort(404)


@app.route("/api/progress")
def api_progress():
    q: queue.Queue = queue.Queue(maxsize=256)
    # Первое событие — текущее состояние: после сна/обрыва связи клиент
    # переподключается и может понять, что задача уже закончилась.
    q.put_nowait(json.dumps({"type": "hello", "active_task": _active_task,
                             "trigger": _task_trigger}))
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


# ── Entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    _migrate_json_to_csv()
    _cleanup_stale_paths()
    _cleanup_partials()
    threading.Thread(target=_scheduler_loop, daemon=True, name="autosync").start()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
