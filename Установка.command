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

PY_FORMULA="python@3.14"

echo ""
echo -e "${purple}  ♫  М У З Ы К А Л Ь Н Ы Й${reset}"
echo -e "${dim}  Автоматическая установка${reset}"
echo ""
echo "  ─────────────────────────────────"

fail() { echo -e "\n${red}  ✗ $1${reset}\n"; read -p "  Нажми Enter…"; exit 1; }
ok()   { echo -e "  ${green}✓${reset} $1"; }
step() { echo -e "\n  ${yellow}→${reset} $1"; }

# ── Xcode Command Line Tools (нужны для Homebrew) ──
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
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [[ -x "$b" ]] && eval "$("$b" shellenv)" && break
done
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

# ── Python ──
# Системный /usr/bin/python3 (3.9) не подходит: свежий yt-dlp требует 3.10+.
step "Проверяю Python…"
if ! brew list --formula "$PY_FORMULA" &>/dev/null; then
    echo -e "  ${dim}Устанавливаю ${PY_FORMULA}…${reset}"
    brew install "$PY_FORMULA" || fail "Не удалось установить Python"
fi
PY="$(brew --prefix "$PY_FORMULA")/bin/python3.14"
[[ -x "$PY" ]] || fail "Python не найден: $PY"
ok "$("$PY" --version 2>&1)"

# ── ffmpeg ──
step "Проверяю ffmpeg…"
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg есть"
else
    echo -e "  ${dim}Устанавливаю ffmpeg (конвертация аудио)…${reset}"
    brew install ffmpeg || fail "Не удалось установить ffmpeg"
    ok "ffmpeg установлен"
fi

# ── Окружение и пакеты ──
step "Устанавливаю компоненты программы…"
# Пересоздаём .venv, если его нет или он собран на другой версии Python.
if [[ ! -x .venv/bin/python ]] || \
   [[ "$(.venv/bin/python -c 'import sys;print(sys.version_info[:2])' 2>/dev/null)" != \
      "$("$PY" -c 'import sys;print(sys.version_info[:2])')" ]]; then
    rm -rf .venv
    "$PY" -m venv .venv || fail "Не удалось создать окружение .venv"
fi
.venv/bin/python -m pip install --upgrade pip --quiet || fail "Не удалось обновить pip"
.venv/bin/python -m pip install --upgrade -r requirements.txt --quiet \
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

exec .venv/bin/python web_app.py
