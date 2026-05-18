@echo off
title TST2SK Setup
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python not found. Please install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python setup.py
pause
