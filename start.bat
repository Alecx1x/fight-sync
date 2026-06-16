@echo off
REM FightSync launcher — starts the local web app and opens it in your browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup: creating virtual environment...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if "%FIGHTSYNC_PORT%"=="" set FIGHTSYNC_PORT=8765
start "" "http://127.0.0.1:%FIGHTSYNC_PORT%"
.venv\Scripts\python.exe app.py
