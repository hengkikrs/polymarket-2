@echo off
title BTC Polymarket Bot
color 0A

echo ================================================
echo   BTC 5-Min Polymarket Bot
echo ================================================
echo.

:: Cek apakah Python tersedia
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan!
    echo Pastikan Python sudah terinstall dan ada di PATH.
    pause
    exit /b 1
)

:: Pindah ke folder tempat run.bat berada
cd /d "%~dp0"

:: Live guard: run.bat must never start real-money mode accidentally.
findstr /R /I "^MOCK_MODE=false" ".env" >nul 2>&1
if not errorlevel 1 (
    if not "%LIVE_TRADING_CONFIRM%"=="I_UNDERSTAND_THIS_IS_REAL_MONEY" (
        echo [BLOCKED] .env sets MOCK_MODE=false.
        echo Set LIVE_TRADING_CONFIRM=I_UNDERSTAND_THIS_IS_REAL_MONEY only after paper validation.
        pause
        exit /b 1
    )
)

echo [1/3] Menjalankan Dashboard  ^(http://localhost:5004^)...
start "Dashboard" cmd /k "title Dashboard ^| color 0B && python -m web.dashboard"
timeout /t 1 /nobreak >nul

echo [2/3] Menjalankan Tracker    ^(http://localhost:5005^)...
start "Tracker" cmd /k "title Tracker ^| color 0E && python -m analysis.tracker"
timeout /t 1 /nobreak >nul

echo [3/3] Menjalankan Bot (main)...
echo.
echo ================================================
echo   Dashboard : http://localhost:5004
echo   Tracker   : http://localhost:5005
echo   Bot log   : logs\bot.log
echo ================================================
echo.
echo Tekan Ctrl+C di window ini untuk menghentikan bot.
echo.

python main.py

echo.
echo Bot telah berhenti.
pause
