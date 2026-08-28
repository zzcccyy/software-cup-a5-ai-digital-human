@echo off
cd /d "%~dp0"
title Lingshan Backend - Port 8088

echo ========================================
echo   Lingshan AI Backend - Starting
echo ========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo Please install Python 3 and add it to PATH first.
    echo.
    pause
    exit /b 1
)

echo Starting backend service...
echo.
echo   Tourist:  http://localhost:8088/
echo   Admin:    http://localhost:8088/admin
echo.
echo Close this window or press Ctrl+C to stop.
echo ----------------------------------------

python backend\main.py

echo.
echo Backend stopped.
pause
