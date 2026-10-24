@echo off
REM Windows launcher - double-click me
cd /d %~dp0
start "" http://localhost:8770
.venv\Scripts\python run.py serve --port 8770
pause
