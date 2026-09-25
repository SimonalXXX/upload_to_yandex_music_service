#!/bin/bash
# Диагностика авторизации Яндекс Музыки — двойной клик для запуска
cd "$(dirname "$0")"
clear
echo ""
echo "  ♫  Диагностика авторизации Яндекс Музыки"
echo "  ─────────────────────────────────────────"
echo ""

PY=.venv/bin/python
if [[ ! -x "$PY" ]]; then
    echo "  ✗ Окружение .venv не найдено — сначала запустите Установка.command"
    echo ""; read -p "  Нажмите Enter, чтобы закрыть…"; exit 1
fi

"$PY" - 2>&1 <<'PYEOF' | tee "диагностика_отчет.txt"
# -*- coding: utf-8 -*-
import sys
from pathlib import Path

OK, FAIL, WARN = "  ✓", "  ✗", "  ⚠"

# ── 1. Chrome установлен? ──
base = Path.home() / "Library/Application Support/Google/Chrome"
if not base.exists():
    print(f"{FAIL} Папка Chrome не найдена: {base}")
    print("    Установите Google Chrome и войдите в music.yandex.ru")
    sys.exit(1)
print(f"{OK} Папка Chrome найдена")

try:
    profiles = [p for p in sorted(base.iterdir())
                if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile"))]
except PermissionError:
    print(f"{FAIL} macOS блокирует доступ к данным Chrome (Operation not permitted).")
    print("    Решение: Системные настройки → Конфиденциальность и безопасность →")
    print("    Полный доступ к диску → включите «Терминал» (ползунок).")
    print("    После этого закройте Терминал полностью (Cmd+Q) и запустите диагностику снова.")
    sys.exit(1)
with_cookies = [p for p in profiles if (p / "Cookies").exists()]
print(f"{OK} Профили Chrome: {', '.join(p.name for p in profiles) or 'нет'}")
if not with_cookies:
    print(f"{FAIL} Ни в одном профиле нет файла Cookies — запустите Chrome хотя бы раз")
    sys.exit(1)

# ── 2. Чтение cookies (может спросить доступ к Связке ключей!) ──
try:
    from pycookiecheat import chrome_cookies
except ImportError:
    print(f"{FAIL} Пакет pycookiecheat не установлен — запустите Установка.command")
    sys.exit(1)

print()
print("  Если появится окно про Связку ключей — нажмите «Разрешить всегда»")
print()

found = None
for prof in with_cookies:
    try:
        c = chrome_cookies("https://music.yandex.ru", cookie_file=prof / "Cookies")
    except Exception as e:
        print(f"{WARN} {prof.name}: не удалось прочитать cookies ({str(e)[:120]})")
        continue
    has_session = "Session_id" in c
    mark = OK if has_session else WARN
    print(f"{mark} {prof.name}: cookies Яндекса: {len(c)}, сессия (Session_id): {'да' if has_session else 'НЕТ'}")
    if has_session and found is None:
        found = prof

if found is None:
    print()
    print(f"{FAIL} Ни в одном профиле Chrome нет активной сессии Яндекса.")
    print("    Откройте music.yandex.ru в Chrome, войдите в аккаунт и запустите диагностику снова.")
    sys.exit(1)

# ── 3. Полная проверка через ym_uploader ──
print()
try:
    from ym_uploader import _create_session, _get_auth, _resolve_playlist_kind
    session = _create_session()
    auth = _get_auth(session)
    if not auth.get("logged"):
        print(f"{FAIL} Авторизация не прошла: {auth.get('error')}")
        sys.exit(1)
    print(f"{OK} Авторизация OK: {auth.get('login')} (uid {auth.get('uid')})")
except Exception as e:
    print(f"{FAIL} Ошибка авторизации: {str(e)[:300]}")
    sys.exit(1)

# ── 4. Плейлист из config.yaml ──
try:
    import yaml
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
    url = (cfg.get("yandex_music") or {}).get("playlist_url", "")
    if not url:
        print(f"{WARN} playlist_url не задан в config.yaml — пропускаю проверку плейлиста")
    else:
        kind = _resolve_playlist_kind(session, url, auth["token"], auth["uid"])
        if kind:
            print(f"{OK} Плейлист найден: kind={kind}")
        else:
            print(f"{FAIL} Не удалось определить плейлист по URL: {url}")
            print("    Проверьте, что это ВАШ плейлист и URL скопирован целиком.")
            sys.exit(1)
except Exception as e:
    print(f"{FAIL} Ошибка проверки плейлиста: {str(e)[:300]}")
    sys.exit(1)

print()
print("  ─────────────────────────────────────────")
print("  ✓ Всё в порядке — загрузка в ЯМ должна работать.")
print("    Перезапустите приложение (Запуск.command) и нажмите «Проверить авторизацию».")
PYEOF

echo ""
read -p "  Нажмите Enter, чтобы закрыть…"
