#!/bin/bash
cd "$(dirname "$0")"
clear

purple='\033[1;35m'
green='\033[1;32m'
yellow='\033[1;33m'
red='\033[1;31m'
dim='\033[2m'
bold='\033[1m'
reset='\033[0m'

echo ""
echo -e "${purple}  ♫  М У З Ы К А Л Ь Н Ы Й${reset}"
echo -e "${dim}  Автоматическая установка${reset}"
echo ""
echo "  ─────────────────────────────────"

fail() { echo -e "\n${red}  ✗ $1${reset}\n"; read -p "  Нажми Enter…"; exit 1; }
ok()   { echo -e "  ${green}✓${reset} $1"; }
step() { echo -e "\n  ${yellow}→${reset} $1"; }

# ── Xcode Command Line Tools (нужны для Homebrew и компиляции) ──
step "Проверяю базовые инструменты…"
if xcode-select -p &>/dev/null; then
    ok "Xcode CLI tools есть"
else
    echo -e "  ${dim}Устанавливаю Xcode Command Line Tools…${reset}"
    echo -e "  ${dim}Может появиться системное окно — нажми «Установить»${reset}"
    xcode-select --install 2>/dev/null
    echo ""
    echo -e "  ${yellow}⏳ Жду завершения установки Xcode tools…${reset}"
    until xcode-select -p &>/dev/null; do sleep 5; done
    ok "Xcode CLI tools установлены"
fi

# ── Homebrew ──
step "Проверяю Homebrew…"
if command -v brew &>/dev/null; then
    ok "Homebrew есть"
else
    echo -e "  ${dim}Устанавливаю Homebrew (менеджер пакетов для macOS)…${reset}"
    echo -e "  ${dim}Может попросить пароль от компьютера — это нормально${reset}"
    echo ""
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || fail "Не удалось установить Homebrew"

    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile 2>/dev/null
    elif [[ -f /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    ok "Homebrew установлен"
fi

# ── Python 3 ──
step "Проверяю Python…"
if command -v python3 &>/dev/null; then
    ok "$(python3 --version 2>&1)"
else
    echo -e "  ${dim}Устанавливаю Python 3…${reset}"
    brew install python@3.12 || fail "Не удалось установить Python"
    ok "$(python3 --version 2>&1)"
fi

# ── ffmpeg ──
step "Проверяю ffmpeg…"
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg есть"
else
    echo -e "  ${dim}Устанавливаю ffmpeg (конвертация аудио)…${reset}"
    brew install ffmpeg || fail "Не удалось установить ffmpeg"
    ok "ffmpeg установлен"
fi

# ── Python-пакеты ──
step "Устанавливаю компоненты программы…"
python3 -m pip install -r requirements.txt --quiet --break-system-packages 2>/dev/null \
  || python3 -m pip install -r requirements.txt --quiet 2>/dev/null \
  || python3 -m pip install -r requirements.txt \
  || fail "Не удалось установить пакеты"
ok "Все компоненты установлены"

# ── Конфиг ──
if [[ ! -f config.yaml ]]; then
    cp config.yaml.example config.yaml
fi

# ── Готово ──
echo ""
echo "  ─────────────────────────────────"
echo ""
echo -e "  ${green}✓ Всё установлено!${reset}"
echo ""
echo -e "  ${bold}Запускаю приложение…${reset}"
echo ""

python3 web_app.py
