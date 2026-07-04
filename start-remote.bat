@echo off
REM FightSync remote launcher (self-healing). Safe to run any number of times:
REM the PowerShell launcher starts the server on :8765 only if it's down, and
REM only recycles the Cloudflare tunnel if the tunnel is actually dead -- a
REM healthy tunnel keeps its current URL. The live URL is saved in current-url.txt.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-remote.ps1"
if not "%FIGHTSYNC_NOPAUSE%"=="1" pause
