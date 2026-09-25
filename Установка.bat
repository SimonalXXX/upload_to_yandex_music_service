@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
cls

echo.
echo   ♫  М У З Ы К А Л Ь Н Ы Й
echo   Автоматическая установка
echo.
echo   ─────────────────────────────────
echo.

set "PYTHON="

:: ── Ищем Python ──
echo   → Ищу Python…

where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        set "PYTHON=%%p"
        goto :check_python_ver
    )
)

:: Проверяем типичные места установки
for %%v in (313 312 311 310 39) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
        set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
        goto :check_python_ver
    )
)
for %%v in (313 312 311 310 39) do (
    if exist "C:\Python%%v\python.exe" (
        set "PYTHON=C:\Python%%v\python.exe"
        goto :check_python_ver
    )
)

:: Python не найден — устанавливаем
echo   Python не найден — устанавливаю…
echo.

where winget >nul 2>&1
if %errorlevel%==0 (
    echo   Устанавливаю Python через winget…
    winget install Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
    if !errorlevel!==0 (
        echo   Python установлен, ищу…
        timeout /t 3 /nobreak >nul

        :: Обновляем PATH из реестра
        for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%b"
        for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SPATH=%%b"
        set "PATH=!SPATH!;!UPATH!"

        where python >nul 2>&1
        if !errorlevel!==0 (
            for /f "delims=" %%p in ('where python 2^>nul') do (
                set "PYTHON=%%p"
                goto :check_python_ver
            )
        )

        for %%v in (313 312 311 310) do (
            if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
                set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
                goto :check_python_ver
            )
        )
    )
)

:: winget не помог — качаем установщик напрямую
echo   Скачиваю Python с python.org…
set "PY_INSTALLER=%TEMP%\python_install.exe"
powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile '%PY_INSTALLER%'" 2>nul

if exist "%PY_INSTALLER%" (
    echo   Запускаю установщик Python…
    "%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1
    timeout /t 5 /nobreak >nul
    del "%PY_INSTALLER%" 2>nul

    for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%b"
    for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SPATH=%%b"
    set "PATH=!SPATH!;!UPATH!"

    where python >nul 2>&1
    if !errorlevel!==0 (
        for /f "delims=" %%p in ('where python 2^>nul') do (
            set "PYTHON=%%p"
            goto :check_python_ver
        )
    )
    for %%v in (313 312 311 310) do (
        if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" (
            set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
            goto :check_python_ver
        )
    )
)

echo.
echo   ✗ Не удалось установить Python автоматически.
echo     Скачай вручную: https://www.python.org/downloads/
echo     При установке поставь галочку "Add Python to PATH"
echo     После установки запусти этот файл снова.
echo.
pause
exit /b 1

:check_python_ver
for /f "tokens=*" %%v in ('"!PYTHON!" --version 2^>^&1') do echo   ✓ %%v

:: Убедимся что директория Python в PATH текущей сессии
for %%d in ("!PYTHON!") do set "PYDIR=%%~dpd"
echo !PATH! | findstr /i /c:"!PYDIR!" >nul || set "PATH=!PYDIR!;!PYDIR!Scripts;!PATH!"

:: ── pip ──
echo.
echo   → Проверяю pip…
"!PYTHON!" -m pip --version >nul 2>&1
if %errorlevel%==0 (
    echo   ✓ pip доступен
) else (
    echo   Устанавливаю pip…
    "!PYTHON!" -m ensurepip --upgrade >nul 2>&1
    if !errorlevel! neq 0 (
        powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TEMP%\get-pip.py'" 2>nul
        "!PYTHON!" "%TEMP%\get-pip.py" >nul 2>&1
    )
    echo   ✓ pip установлен
)

:: ── ffmpeg ──
echo.
echo   → Проверяю ffmpeg…
where ffmpeg >nul 2>&1
if %errorlevel%==0 (
    echo   ✓ ffmpeg есть
    goto :packages
)

echo   ffmpeg не найден — устанавливаю…

where winget >nul 2>&1
if %errorlevel%==0 (
    winget install Gyan.FFmpeg --silent --accept-source-agreements --accept-package-agreements >nul 2>&1
    if !errorlevel!==0 (
        for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%b"
        for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SPATH=%%b"
        set "PATH=!SPATH!;!UPATH!"
        echo   ✓ ffmpeg установлен
        goto :packages
    )
)

:: Фоллбэк: качаем ffmpeg напрямую и кладём рядом с проектом
echo   Скачиваю ffmpeg…
set "FF_ZIP=%TEMP%\ffmpeg.zip"
set "FF_DIR=%~dp0ffmpeg"
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile '%FF_ZIP%'" 2>nul
if exist "%FF_ZIP%" (
    powershell -Command "Expand-Archive -Path '%FF_ZIP%' -DestinationPath '%TEMP%\ffmpeg_tmp' -Force" 2>nul
    if not exist "%FF_DIR%" mkdir "%FF_DIR%"
    for /r "%TEMP%\ffmpeg_tmp" %%f in (ffmpeg.exe ffprobe.exe) do (
        copy "%%f" "%FF_DIR%\" >nul 2>&1
    )
    del "%FF_ZIP%" 2>nul
    rd /s /q "%TEMP%\ffmpeg_tmp" 2>nul
    set "PATH=%FF_DIR%;!PATH!"
    echo   ✓ ffmpeg скачан в папку проекта
) else (
    echo   ⚠ Не удалось скачать ffmpeg — скачай вручную: https://ffmpeg.org
)

:packages
echo.
echo   → Устанавливаю компоненты программы…
"!PYTHON!" -m pip install -r requirements.txt --quiet >nul 2>&1
if %errorlevel%==0 (
    echo   ✓ Все компоненты установлены
) else (
    "!PYTHON!" -m pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo   ✗ Ошибка установки пакетов
        pause
        exit /b 1
    )
)

:: ── Конфиг ──
if not exist config.yaml (
    copy config.yaml.example config.yaml >nul
)

:: ── Готово — запускаем ──
echo.
echo   ─────────────────────────────────
echo.
echo   ✓ Всё установлено!
echo.
echo   Запускаю приложение…
echo   Закрой это окно чтобы остановить.
echo.

"!PYTHON!" web_app.py
