@echo off
REM FightSync remote launcher: starts the server + a Cloudflare quick tunnel.
REM The public URL is printed below and saved in tunnel.log. It CHANGES each run.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First-time setup: creating the virtual environment...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if "%FIGHTSYNC_PORT%"=="" set FIGHTSYNC_PORT=8765
del /q tunnel.log 2>nul

start "FightSync server" /min .venv\Scripts\python.exe app.py
timeout /t 5 /nobreak >nul
REM watchdog owns cloudflared and auto-restarts it if the tunnel drops
start "FightSync tunnel" /min .venv\Scripts\python.exe tunnel_watchdog.py

echo Waiting for the public URL...
timeout /t 12 /nobreak >nul
echo.
echo ==== Your FightSync public URL ====
type current-url.txt
echo.
echo ===================================
echo Password is in fightsync-secret.txt
echo The watchdog auto-recovers the tunnel; the current URL is always in
echo current-url.txt (note: it changes whenever the tunnel restarts).
echo.
pause
