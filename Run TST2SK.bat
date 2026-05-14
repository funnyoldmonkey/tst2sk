@echo off
title TST2SK - Autonomous Troubleshooting Agent
cls
pip install -r requirements.txt >nul 2>&1
cls
python main.py
pause
