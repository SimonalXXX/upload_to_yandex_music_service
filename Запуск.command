#!/bin/bash
cd "$(dirname "$0")"

echo ""
echo -e "\033[1;35m  ♫  М У З Ы К А Л Ь Н Ы Й\033[0m"
echo -e "\033[2m  http://127.0.0.1:5555\033[0m"
echo ""

if ! command -v python3 &>/dev/null; then
    echo -e "\033[1;31m  ✗ Python не найден. Запусти сначала Установка.command\033[0m"
    echo ""; read -p "  Нажми Enter…"; exit 1
fi

if ! python3 -c "import flask" 2>/dev/null; then
    echo -e "\033[1;33m  Пакеты не установлены — доустанавливаю…\033[0m"
    python3 -m pip install -r requirements.txt --quiet --break-system-packages 2>/dev/null \
      || python3 -m pip install -r requirements.txt --quiet
fi

if [[ ! -f config.yaml ]]; then cp config.yaml.example config.yaml; fi

echo -e "\033[1;32m  ✓ Запускаю…\033[0m"
echo -e "\033[2m  Закрой это окно чтобы остановить\033[0m"
echo ""

python3 web_app.py
