#!/bin/bash
cd "$(dirname "$0")"

# ffmpeg из Homebrew должен быть в PATH, даже если профиль shell его не добавил.
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x "$b" ]] && eval "$("$b" shellenv)" && break
done

echo ""
echo -e "\033[1;35m  ♫  М У З Ы К А Л Ь Н Ы Й\033[0m"
echo -e "\033[2m  http://127.0.0.1:5555\033[0m"
echo ""

# Уже запущено — просто открываем вкладку, второй сервер на том же порту не поднимется.
if lsof -nP -iTCP:5555 -sTCP:LISTEN &>/dev/null; then
    echo -e "\033[1;32m  ✓ Уже запущено — открываю в браузере\033[0m"
    open "http://127.0.0.1:5555"
    exit 0
fi

if [[ ! -x .venv/bin/python ]]; then
    echo -e "\033[1;33m  Окружение не установлено — запускаю установку…\033[0m"
    exec bash "./Установка.command"
fi

if ! .venv/bin/python -c "import flask, yt_dlp, pycookiecheat" 2>/dev/null; then
    echo -e "\033[1;33m  Пакеты не установлены — доустанавливаю…\033[0m"
    .venv/bin/python -m pip install -r requirements.txt --quiet
fi

if [[ ! -f config.yaml ]]; then cp config.yaml.example config.yaml; fi

echo -e "\033[1;32m  ✓ Запускаю…\033[0m"
echo -e "\033[2m  Закрой это окно чтобы остановить\033[0m"
echo ""

exec .venv/bin/python web_app.py
