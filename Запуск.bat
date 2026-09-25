@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo   ♫  М У З Ы К А Л Ь Н Ы Й
echo   http://127.0.0.1:5555
echo.

:: Ищем Python
set "PYTHON="
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        set "PYTHON=%%p"
        goto :found
    )
)
for %%v in (313 312 311 310 39) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
        goto :found
    )
)
echo   ✗ Python не найден. Запусти сначала Установка.bat
echo.
pause
exit /b 1

:found
:: Добавим директорию Python в PATH сессии
for %%d in ("!PYTHON!") do set "PYDIR=%%~dpd"
set "PATH=!PYDIR!;!PYDIR!Scripts;!PATH!"

:: Добавим локальный ffmpeg если есть
if exist "%~dp0ffmpeg" set "PATH=%~dp0ffmpeg;!PATH!"

:: Проверяем пакеты, доустанавливаем если надо
"!PYTHON!" -c "import flask" >nul 2>&1
if !errorlevel! neq 0 (
    echo   Доустанавливаю пакеты…
    "!PYTHON!" -m pip install -r requirements.txt --quiet >nul 2>&1
)

if not exist config.yaml copy config.yaml.example config.yaml >nul 2>&1

echo   ✓ Запускаю…
echo   Закрой это окно чтобы остановить.
echo.

"!PYTHON!" web_app.py
