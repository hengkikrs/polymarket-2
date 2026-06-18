@echo off
title BTC Polymarket Bot [DEV MODE]
color 0E

echo ================================================
echo   BTC 5-Min Polymarket Bot - DEV MODE
echo   Auto-reload on file changes
echo ================================================
echo.

:: Cek Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan!
    pause
    exit /b 1
)

cd /d "%~dp0"

:: Install watchdog jika belum ada
python -c "import watchdog" >nul 2>&1
if errorlevel 1 (
    echo [DEV] Installing watchdog...
    pip install watchdog
    echo.
)

echo [DEV] Starting with auto-reload...
echo      Edit file .py atau .env di VSCode
echo      Bot akan otomatis restart!
echo.

python -m tools.run_dev

echo.
echo Dev mode stopped.
pause
