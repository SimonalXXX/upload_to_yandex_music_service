#!/usr/bin/env python3
"""
Yandex Music Uploader — загружает треки в плейлист через HTTP API.
Авторизация: cookies из Chrome → OAuth (account/playlists) + cookies (upload).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import List, Optional, Set

import requests
from pycookiecheat import chrome_cookies
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

console = Console()

SUPPORTED_FORMATS = {".mp3", ".m4a", ".opus", ".flac", ".ogg", ".wav", ".aac"}
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_UPLOAD_ARCHIVE = BASE_DIR / "upload_archive.txt"
MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
}

API_BASE = "https://api.music.yandex.net"
API_RU_BASE = "https://api.music.yandex.ru"
UPLOAD_URL_PATH = "/loader/upload-url"
PASSPORT_TOKEN_URL = (
    "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid"
)
YM_OAUTH_CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
YM_OAUTH_CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"
WEB_CLIENT = "YandexMusicWebNext/1.0.0"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def collect_music_files(directory: Path) -> List[Path]:
    """Recursively collect all audio files from directory."""
    if not directory.exists():
        return []
    return sorted(
        f for f in directory.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_FORMATS
    )


def _load_upload_archive(path: Path) -> Set[str]:
    """Имена уже загруженных файлов.

    Сверяем по имени файла, а не по полному пути: пути в архиве (старые —
    абсолютные) ломаются при переносе папки проекта, а имя «артист - трек.mp3»
    в плоской папке Music и так уникально.
    """
    if not path.exists():
        return set()
    with open(path, encoding="utf-8") as f:
        return {Path(line.strip()).name for line in f if line.strip()}


def _record_uploaded(path: Path, filepath: Path) -> None:
    if filepath.name in _load_upload_archive(path):
        return
    p = filepath.resolve()
    entry = str(p.relative_to(BASE_DIR)) if p.is_relative_to(BASE_DIR) else str(p)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def _forget_uploaded(path: Path, names: Set[str]) -> int:
    """Убирает файлы (по имени) из архива загрузок — чтобы их можно было загрузить снова."""
    if not path.exists() or not names:
        return 0
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    keep = [ln for ln in lines if Path(ln).name not in names]
    path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return len(lines) - len(keep)


def _wait(seconds: float, cancel_event=None) -> None:
    """Пауза, которую прерывает «Остановить» (threading.Event) — тогда RuntimeError."""
    if cancel_event is None:
        time.sleep(seconds)
    elif cancel_event.wait(seconds):
        raise RuntimeError("Отменено")


def _chrome_cookie_files() -> List[Path]:
    """Файлы Cookies всех профилей Chrome (Default, Profile 1, …) на macOS."""
    base = Path.home() / "Library/Application Support/Google/Chrome"
    if not base.exists():
        return []
    files: List[Path] = []
    try:
        for prof in sorted(base.iterdir()):
            if prof.is_dir() and (prof.name == "Default" or prof.name.startswith("Profile")):
                ck = prof / "Cookies"
                if ck.exists():
                    files.append(ck)
    except PermissionError as e:
        raise RuntimeError(
            "macOS не даёт программе доступ к данным Chrome (Operation not permitted). "
            "Откройте: Системные настройки → Конфиденциальность и безопасность → "
            "Полный доступ к диску → включите «Терминал», затем перезапустите приложение."
        ) from e
    return files


def _create_session() -> requests.Session:
    """Cookies Яндекса из Chrome.

    pycookiecheat по умолчанию смотрит только профиль Default; если вход в
    Яндекс сделан в другом профиле Chrome (Profile 1, …) — перебираем все
    и берём тот, где есть сессия (cookie Session_id).
    """
    cookies: dict | None = None
    last_err: Exception | None = None
    try:
        cookies = chrome_cookies("https://music.yandex.ru")
    except Exception as e:
        last_err = e
    if not cookies or "Session_id" not in cookies:
        for ck_file in _chrome_cookie_files():
            try:
                c = chrome_cookies("https://music.yandex.ru", cookie_file=ck_file)
            except Exception as e:
                last_err = e
                continue
            if c and "Session_id" in c:
                cookies = c
                break
            if c and not cookies:
                cookies = c
    if not cookies:
        if last_err and (
            isinstance(last_err, PermissionError)
            or "Operation not permitted" in str(last_err)
        ):
            raise RuntimeError(
                "macOS не даёт доступ к cookies Chrome. Откройте: Системные "
                "настройки → Конфиденциальность и безопасность → Полный доступ "
                "к диску → включите «Терминал», затем перезапустите приложение."
            )
        raise RuntimeError(
            "Не удалось прочитать cookies Яндекса из Chrome"
            + (f": {last_err}" if last_err else "")
            + ". Проверьте, что Chrome установлен, и разрешите доступ к Связке "
            "ключей (нажмите «Разрешить всегда», если macOS спросит)."
        )
    if "Session_id" not in cookies:
        raise RuntimeError(
            "В Chrome нет активной сессии Яндекса (cookie Session_id). "
            "Откройте music.yandex.ru именно в Google Chrome, войдите в "
            "аккаунт и повторите проверку."
        )
    s = requests.Session()
    s.cookies.update(cookies)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "X-Retpath-Y": "https://music.yandex.ru",
        "Referer": "https://music.yandex.ru/",
    })
    return s


def _cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{k}={v}" for k, v in session.cookies.items())


def _fetch_oauth_token(session: requests.Session) -> str:
    r = requests.post(
        PASSPORT_TOKEN_URL,
        data={
            "client_id": YM_OAUTH_CLIENT_ID,
            "client_secret": YM_OAUTH_CLIENT_SECRET,
        },
        headers={
            "Ya-Client-Host": "music.yandex.ru",
            "Ya-Client-Cookie": _cookie_header(session),
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(
            "Не удалось получить OAuth-токен. "
            "Откройте music.yandex.ru в Chrome и войдите в аккаунт."
        )
    return token


def _api_headers(token: str, uid: Optional[int] = None) -> dict:
    """Заголовки OAuth API (api.music.yandex.net)."""
    headers = {
        "Authorization": f"OAuth {token}",
        "Accept": "application/json",
        "X-Yandex-Music-Client": WEB_CLIENT,
        "X-Yandex-Music-Without-Invocation-Info": "1",
    }
    if uid is not None:
        headers["X-Yandex-Music-Multi-Auth-User-Id"] = str(uid)
    return headers


def _web_headers(uid: int) -> dict:
    """Заголовки веб-клиента (api.music.yandex.ru, авторизация по cookies)."""
    return {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Yandex-Music-Client": WEB_CLIENT,
        "X-Yandex-Music-Without-Invocation-Info": "1",
        "X-Yandex-Music-Multi-Auth-User-Id": str(uid),
        "Referer": "https://music.yandex.ru/",
    }


def _parse_api_error(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:120]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        if data.get("message"):
            return str(data["message"])
    return str(data)[:120]


def _get_auth(session: requests.Session) -> dict:
    """Проверка авторизации через OAuth (старый handlers/auth отдаёт 404)."""
    token = _fetch_oauth_token(session)
    r = session.get(
        f"{API_BASE}/account/status",
        headers=_api_headers(token),
        timeout=30,
    )
    if r.status_code == 401:
        return {"logged": False, "error": "Сессия Chrome устарела — перелогиньтесь в Яндекс Музыке"}
    r.raise_for_status()
    payload = r.json()
    account = payload.get("result", {}).get("account") or payload.get("account") or {}
    uid = account.get("uid")
    if not uid:
        return {"logged": False, "error": "Не удалось получить данные аккаунта"}
    return {
        "logged": True,
        "login": account.get("login", "?"),
        "uid": uid,
        "token": token,
    }


def _playlist_uuid_from_url(playlist_url: str) -> Optional[str]:
    m = _UUID_RE.search(playlist_url)
    return m.group(0) if m else None


def _playlist_kind_from_url(playlist_url: str) -> Optional[str]:
    m = re.search(r"/playlists/(?:[\w-]+/)?(\d+)(?:[/?#]|$)", playlist_url)
    return m.group(1) if m else None


def _playlist_owner_from_url(playlist_url: str) -> Optional[str]:
    """Логин владельца из старых ссылок вида /users/<login>/playlists/<kind>."""
    m = re.search(r"/users/([^/?#]+)/playlists/", playlist_url)
    return m.group(1) if m else None


def _api_get_result(session: requests.Session, url: str, token: str, uid: int):
    r = session.get(url, headers=_api_headers(token, uid), timeout=30)
    if not r.ok:
        return None
    body = r.json()
    return body.get("result", body) if isinstance(body, dict) else body


def _resolve_playlist_kind(
    session: requests.Session,
    playlist_url: str,
    token: str,
    uid: int,
) -> Optional[str]:
    """Определяет numeric kind СВОЕГО плейлиста по ссылке (обычной или с UUID).

    Загрузка всегда идёт в «{uid}:{kind}» текущего аккаунта, поэтому ссылка на
    чужой плейлист молча отправила бы треки в ваш плейлист с тем же номером.
    Такие ссылки отклоняем (RuntimeError). Возвращает None, если плейлист не найден.
    """
    kind = _playlist_kind_from_url(playlist_url)
    playlist_uuid = _playlist_uuid_from_url(playlist_url)

    if kind is None and playlist_uuid:
        own = _api_get_result(session, f"{API_BASE}/users/{uid}/playlists/list", token, uid)
        for pl in own if isinstance(own, list) else []:
            if pl.get("playlistUuid") == playlist_uuid:
                kind = str(pl.get("kind"))
                break
        if kind is None:
            pl = _api_get_result(session, f"{API_BASE}/playlist/{playlist_uuid}", token, uid)
            if isinstance(pl, dict) and pl.get("kind") is not None:
                owner_uid = (pl.get("owner") or {}).get("uid", pl.get("uid"))
                if owner_uid is not None and str(owner_uid) != str(uid):
                    raise RuntimeError(
                        "Плейлист принадлежит другому пользователю — загружать можно "
                        "только в свой плейлист"
                    )
                kind = str(pl["kind"])
    if kind is None:
        return None

    # Проверяем, что uid:kind существует в НАШЕМ аккаунте и совпадает со ссылкой.
    pl = _api_get_result(session, f"{API_BASE}/users/{uid}/playlists/{kind}", token, uid)
    if not isinstance(pl, dict):
        raise RuntimeError(f"Плейлист {kind} не найден в вашем аккаунте Яндекс Музыки")
    url_owner = _playlist_owner_from_url(playlist_url)
    owner_login = (pl.get("owner") or {}).get("login")
    if url_owner and owner_login and url_owner.lower() != owner_login.lower():
        raise RuntimeError(
            f"Ссылка ведёт на плейлист пользователя {url_owner}, а вы вошли как "
            f"{owner_login} — загружать можно только в свой плейлист"
        )
    if playlist_uuid and pl.get("playlistUuid") and pl["playlistUuid"] != playlist_uuid:
        raise RuntimeError("Плейлист по ссылке не совпадает с найденным в аккаунте")
    return kind


def _get_upload_target(
    session: requests.Session,
    uid: int,
    kind: str,
    filename: str,
    token: str,
    retries: int = 3,
    cancel_event=None,
) -> dict:
    """Запрашивает post-target для загрузки (POST loader/upload-url на api.music.yandex.ru)."""
    del token  # загрузка идёт по cookies, OAuth нужен только для account/playlists
    params = {
        "uid": uid,
        "playlist-id": f"{uid}:{kind}",
        "path": filename,
    }
    headers = _web_headers(uid)

    last_error = ""
    for attempt in range(retries):
        r = session.post(
            f"{API_RU_BASE}{UPLOAD_URL_PATH}",
            params=params,
            headers=headers,
            timeout=30,
        )
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                last_error = "Ответ API не JSON (возможна капча SmartCaptcha)"
                _wait(3 * (attempt + 1), cancel_event)
                continue
            if isinstance(data, dict) and "post-target" in data:
                return data
            if isinstance(data, dict) and data.get("result") == "TOO_MANY_FILES":
                raise RuntimeError("Слишком много файлов в очереди загрузки Яндекс Музыки")
            last_error = f"Неожиданный ответ: {str(data)[:120]}"
        else:
            last_error = f"HTTP {r.status_code}: {_parse_api_error(r)}"
        _wait(3 * (attempt + 1), cancel_event)

    raise RuntimeError(
        "Не удалось получить URL загрузки. "
        f"Последняя ошибка: {last_error}. "
        "Убедитесь, что вы залогинены в music.yandex.ru в Chrome."
    )


def _upload_file(session: requests.Session, post_target: str, filepath: Path) -> bool:
    mime = MIME_MAP.get(filepath.suffix.lower(), "audio/mpeg")
    with open(filepath, "rb") as fh:
        r = session.post(
            post_target,
            files={"file": (filepath.name, fh, mime)},
            timeout=120,
        )
    return r.status_code in (200, 201)


def _playlist_track_count(
    session: requests.Session, uid: int, kind: str, token: str
) -> Optional[int]:
    """Число треков в плейлисте (для подтверждения загрузки по росту счётчика)."""
    try:
        r = session.get(
            f"{API_BASE}/users/{uid}/playlists/{kind}",
            headers=_api_headers(token, uid),
            timeout=30,
        )
        if r.ok:
            body = r.json()
            pl = body.get("result", body) if isinstance(body, dict) else {}
            if isinstance(pl, dict):
                if pl.get("trackCount") is not None:
                    return int(pl["trackCount"])
                if isinstance(pl.get("tracks"), list):
                    return len(pl["tracks"])
    except (requests.exceptions.RequestException, ValueError, TypeError):
        pass
    return None


def get_playlist_info(
    session: requests.Session, uid: int, kind: str, token: str
) -> dict:
    """Название, число треков и владелец плейлиста — для проверки ссылки в настройках."""
    pl = _api_get_result(session, f"{API_BASE}/users/{uid}/playlists/{kind}", token, uid)
    if not isinstance(pl, dict):
        raise RuntimeError(f"Плейлист {kind} не найден в вашем аккаунте Яндекс Музыки")
    count = pl.get("trackCount")
    if count is None and isinstance(pl.get("tracks"), list):
        count = len(pl["tracks"])
    return {
        "kind": str(kind),
        "title": pl.get("title") or "",
        "track_count": count,
        "owner": (pl.get("owner") or {}).get("login") or "",
    }


def get_playlist_tracks(
    session: requests.Session, uid: int, kind: str, token: str
) -> List[dict]:
    """Треки плейлиста: id, title, artist, available — для сверки с локальной базой."""
    r = session.get(
        f"{API_BASE}/users/{uid}/playlists/{kind}",
        headers=_api_headers(token, uid),
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    pl = body.get("result", body) if isinstance(body, dict) else {}
    out: List[dict] = []
    for item in pl.get("tracks", []) if isinstance(pl, dict) else []:
        tr = item.get("track", {}) or {}
        artists = ", ".join(
            a.get("name", "") for a in tr.get("artists", []) if isinstance(a, dict)
        )
        out.append({
            "id": str(tr.get("id", "")),
            "title": tr.get("title", "") or "",
            "artist": artists,
            "available": tr.get("state") == "playable" or bool(tr.get("available")),
        })
    return out


def _verify_uploaded(
    session: requests.Session,
    uid: int,
    kind: str,
    ugc_track_id: Optional[str],
    token: str,
    retries: int = 10,
    delay: float = 5,
    cancel_event=None,
    count_before: Optional[int] = None,
) -> Optional[str]:
    """Подтверждает по списку треков плейлиста, что трек реально доехал и проигрываем.

    Возвращает "id" — трек найден по ugc-track-id и проигрываем; "count" — трек по
    id не найден, но плейлист вырос (слабое подтверждение: рост мог дать и ранее
    принятый трек, дообработавшийся только сейчас); None — не подтверждено.

    Начальный POST на loader может вернуть 200/201 ещё до того, как асинхронная
    обработка файла на их стороне провалится — сам факт HTTP-успеха ничего не
    гарантирует (поймано на реальной загрузке: POST успешен, трек в плейлисте так
    и не появился). poll-result для перепроверки не годится — это long-polling
    эндпоинт на конкретной loader-ноде, и если нода нездорова, зависает или рвётся
    той же сетевой болезнью, что и сама заливка, без возможности уйти на другую ноду.
    Вместо этого сверяемся через users/{uid}/playlists/{kind} — тот же стабильный
    api.music.yandex.net, что и для auth/резолва плейлиста.
    """
    for attempt in range(retries):
        try:
            r = session.get(
                f"{API_BASE}/users/{uid}/playlists/{kind}",
                headers=_api_headers(token, uid),
                timeout=30,
            )
            if r.ok:
                body = r.json()
                pl = body.get("result", body) if isinstance(body, dict) else {}
                tracks = pl.get("tracks", []) if isinstance(pl, dict) else []
                for item in tracks:
                    tr = item.get("track", {})
                    # API может вернуть id числом, а ugc-track-id приходит строкой —
                    # сравниваем как строки, иначе загруженный трек «не находится»
                    # и повторные попытки плодят дубли в плейлисте.
                    if ugc_track_id is not None and str(ugc_track_id) in (
                        str(tr.get("id")), str(tr.get("realId"))
                    ):
                        playable = tr.get("state") == "playable" or bool(tr.get("available"))
                        return "id" if playable else None
                # ЯМ может присвоить треку ДРУГОЙ id после обработки — тогда
                # сверка по id не сработает никогда. Подстраховка: плейлист вырос.
                if count_before is not None:
                    current = pl.get("trackCount") if isinstance(pl, dict) else None
                    if current is None:
                        current = len(tracks)
                    if isinstance(current, int) and current > count_before:
                        return "count"
        except requests.exceptions.RequestException:
            pass
        if cancel_event is not None and cancel_event.is_set():
            return None
        if attempt < retries - 1:
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    return None
            else:
                time.sleep(delay)
    return None


def _upload_one(
    session: requests.Session,
    uid: int,
    kind: str,
    filepath: Path,
    token: str,
    retries: int = 3,
    cancel_event=None,
    verify_retries: int = 10,
    verify_delay: float = 5,
):
    """Запрашивает upload-target, заливает файл и подтверждает результат по плейлисту.

    Возвращает: True — подтверждено в плейлисте по id трека; "grown" (истинно) —
    по id не найден, но плейлист вырос (почти наверняка доехал, но не 100%);
    "accepted" (истинно) — файл принят сервером, но подтверждение не успело
    прийти (проверьте плейлист); исключение — загрузка не удалась.
    Удалять локальный файл безопасно только при True.

    Повторная попытка — ТОЛЬКО если сам POST не прошёл (сетевой сбой/не-2xx):
    post-target одноразовый, запрашиваем новый и повторяем. Если сервер файл
    принял (2xx), но подтверждение не получено — не ретраим (ЯМ может сменить id
    трека при обработке, и слепой повтор плодит дубли в плейлисте); подтверждаем
    по совпадению id ИЛИ по росту числа треков в плейлисте.

    cancel_event (опционально, threading.Event) — retries и ожидание внутри
    _verify_uploaded прерываются по нему, чтобы кнопка «Остановить» в вебе не
    зависала на несколько минут посреди повторных попыток одного трека.

    verify_retries × verify_delay — окно ожидания подтверждения. ЯМ обрабатывает
    файл минутами, поэтому веб берёт короткое окно и подтверждает позже по плейлисту.
    """
    last_error: Exception | None = None
    for attempt in range(retries):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Отменено")
        count_before = _playlist_track_count(session, uid, kind, token)
        target = _get_upload_target(
            session, uid, kind, filepath.name, token, cancel_event=cancel_event
        )
        ugc_id = target.get("ugc-track-id")
        http_ok = False
        try:
            http_ok = _upload_file(session, target["post-target"], filepath)
        except requests.exceptions.RequestException as e:
            last_error = e
        if ugc_id is None and count_before is None:
            if http_ok:
                return "accepted"  # проверить нечем — принят, но не подтверждён
        else:
            verified = _verify_uploaded(
                session, uid, kind, ugc_id, token,
                retries=verify_retries, delay=verify_delay,
                cancel_event=cancel_event, count_before=count_before,
            )
            if verified == "id":
                return True
            if verified == "count":
                return "grown"
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Отменено")
        if http_ok:
            # Сервер ПРИНЯЛ файл (HTTP 2xx), но подтверждения в плейлисте нет:
            # обработка у ЯМ может занимать дольше окна ожидания, а id трека —
            # измениться. Повторная заливка создала бы дубль, поэтому НИКОГДА
            # не ретраим принятый файл. Считаем успехом с оговоркой: вызывающий
            # код показывает предупреждение «проверьте плейлист».
            return "accepted"
        # Сюда попадаем только если сам POST не прошёл — повтор оправдан.
        if last_error is None:
            last_error = RuntimeError("Загрузка файла не удалась")
        if attempt < retries - 1:
            _wait(3 * (attempt + 1), cancel_event)
    raise last_error


def upload_to_yandex(
    playlist_url: str,
    files: List[Path],
    upload_delay: float = 1.5,
    archive_path: Optional[Path] = None,
    force: bool = False,
    **kwargs,
) -> dict:
    """Upload audio files to a Yandex Music playlist via HTTP API."""
    results: dict = {"uploaded": [], "skipped": [], "errors": []}
    archive = archive_path or DEFAULT_UPLOAD_ARCHIVE

    audio_files = [f for f in files if f.suffix.lower() in SUPPORTED_FORMATS]

    skipped = 0
    if not force:
        already = _load_upload_archive(archive)
        before = len(audio_files)
        audio_files = [f for f in audio_files if f.name not in already]
        skipped = before - len(audio_files)
        if skipped:
            results["skipped"] = [f"({skipped} ранее загруженных)"]

    if not audio_files:
        console.print("[green]Все треки уже загружены — нечего отправлять[/green]")
        return results

    console.print()
    console.print(
        Panel(
            f"[bold]Загрузка в Яндекс Музыку[/bold]\n"
            f"[dim]Плейлист:[/dim]  {playlist_url}\n"
            f"[dim]Новых файлов:[/dim]  {len(audio_files)}"
            + (f"  [dim](пропущено {skipped} уже загруженных)[/dim]" if skipped else "")
            + "\n[dim]Метод:[/dim]     OAuth API (cookies из Chrome)",
            border_style="magenta",
        )
    )
    console.print()

    try:
        session = _create_session()
    except Exception as e:
        console.print(f"[red]Не удалось извлечь cookies из Chrome:[/red] {e}")
        console.print("[dim]Убедитесь, что Chrome запущен и вы залогинены в Яндекс Музыке[/dim]")
        return results

    try:
        auth = _get_auth(session)
    except Exception as e:
        console.print(f"[red]Ошибка авторизации:[/red] {e}")
        return results

    if not auth.get("logged"):
        console.print(f"[red]{auth.get('error', 'Вы не залогинены в Яндекс Музыке в Chrome')}[/red]")
        return results

    token = auth["token"]
    uid = auth["uid"]
    console.print(
        f"  [green]Авторизация OK:[/green] {auth.get('login', '?')} (uid {uid})"
    )

    try:
        kind = _resolve_playlist_kind(session, playlist_url, token, uid)
    except Exception as e:
        console.print(f"[red]Плейлист:[/red] {e}")
        return results
    if not kind:
        console.print("[red]Не удалось определить ID плейлиста[/red]")
        return results

    console.print(f"  [green]Плейлист:[/green] kind={kind}")
    console.print()
    console.print("[bold blue]Загрузка треков...[/bold blue]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Подготовка...", total=len(audio_files))

        for filepath in audio_files:
            name = filepath.stem
            display = name[:45] + "..." if len(name) > 45 else name
            progress.update(task, description=display)

            try:
                ok = _upload_one(session, uid, kind, filepath, token)
                if ok:
                    results["uploaded"].append(filepath.name)
                    _record_uploaded(archive, filepath)
                    if ok == "accepted":
                        console.print(
                            f"  [yellow]⚠ {filepath.name}: принят, но в плейлисте "
                            f"пока не виден (обработка)[/yellow]"
                        )
                else:
                    results["errors"].append((filepath.name, "HTTP upload failed"))

            except Exception as e:
                results["errors"].append((filepath.name, str(e)[:100]))

            progress.advance(task)
            time.sleep(upload_delay)

    console.print()
    _print_summary(results)
    return results


def _print_summary(results: dict) -> None:
    table = Table(title="Результат загрузки в Яндекс Музыку", border_style="magenta")
    table.add_column("Параметр", style="bold")
    table.add_column("Значение")
    table.add_row("Загружено", f"[green]{len(results['uploaded'])}[/green]")
    if results.get("skipped"):
        table.add_row("Пропущено", f"[dim]{results['skipped'][0]}[/dim]")
    if results["errors"]:
        table.add_row("Ошибки", f"[red]{len(results['errors'])}[/red]")
    console.print(table)

    if results["uploaded"]:
        console.print("\n[bold green]Загруженные треки:[/bold green]")
        for i, name in enumerate(results["uploaded"][:20], 1):
            console.print(f"  {i}. {name}")
        if len(results["uploaded"]) > 20:
            console.print(f"  ... и ещё {len(results['uploaded']) - 20}")

    if results["errors"]:
        console.print("\n[bold red]Ошибки:[/bold red]")
        for name, err in results["errors"][:10]:
            console.print(f"  [red]x[/red] {name}: {err}")

    console.print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Загрузка треков в плейлист Яндекс Музыки")
    parser.add_argument("playlist_url", help="URL плейлиста Яндекс Музыки")
    parser.add_argument("--dir", "-d", default="./Music", help="Папка с музыкой")
    parser.add_argument("--files", "-f", nargs="+", help="Конкретные файлы")
    parser.add_argument("--delay", type=float, default=1.5, help="Задержка между загрузками (сек)")

    args = parser.parse_args()

    if args.files:
        targets = [Path(f) for f in args.files if Path(f).exists()]
    else:
        targets = collect_music_files(Path(args.dir))

    upload_to_yandex(playlist_url=args.playlist_url, files=targets, upload_delay=args.delay)
