@echo off
title TST2SK - Autonomous Troubleshooting Agent

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: Activate venv if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: Install dependencies (show errors if they fail)
echo Installing dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo.
    echo Warning: Some dependencies failed to install. The app may not work correctly.
    echo Try running: pip install -r requirements.txt
    echo.
)

cls
python main.py
pause
