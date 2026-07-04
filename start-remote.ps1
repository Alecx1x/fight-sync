# FightSync self-healing launcher.
#
# Idempotent: safe to run any number of times. It brings the stack back to a
# healthy state and never double-launches:
#   * Starts the FastAPI server on :8765 only if it isn't already listening.
#   * (Re)starts the Cloudflare tunnel watchdog ONLY if the tunnel is actually
#     down (no watchdog running, or the current public URL is unreachable).
#     A healthy tunnel is left untouched so its public URL stays stable.
#
# The public URL (it changes whenever the tunnel restarts) is always written to
# current-url.txt by the watchdog.

$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$port    = if ($env:FIGHTSYNC_PORT) { $env:FIGHTSYNC_PORT } else { "8765" }
$py      = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pyw     = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
$log     = Join-Path $PSScriptRoot "tunnel.log"
$urlFile = Join-Path $PSScriptRoot "current-url.txt"

# ---- first-time setup -------------------------------------------------------
if (-not (Test-Path $py)) {
  Write-Host "First-time setup: creating the virtual environment..."
  python -m venv .venv
  & $py -m pip install -r requirements.txt
}

function Port-Listening($p) {
  return [bool](Get-NetTCPConnection -State Listen -LocalPort ([int]$p) -ErrorAction SilentlyContinue)
}
function Url-Reachable($u) {
  if (-not $u) { return $false }
  try {
    $r = Invoke-WebRequest -Uri ($u.TrimEnd('/') + "/login") -TimeoutSec 12 -UseBasicParsing -ErrorAction Stop
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
  } catch { return $false }
}

# ---- 1) ensure the app server is up ----------------------------------------
if (Port-Listening $port) {
  Write-Host "FightSync server already listening on :$port - leaving it."
} else {
  Write-Host "Starting FightSync server on :$port ..."
  Start-Process -FilePath $py -ArgumentList "app.py" -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
  for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 1; if (Port-Listening $port) { break } }
  if (Port-Listening $port) { Write-Host "Server is up." } else { Write-Host "WARNING: server did not come up on :$port - check for errors." }
}

# ---- 2) ensure the tunnel watchdog is healthy ------------------------------
# A FightSync watchdog is a python process whose command line runs tunnel_watchdog.py.
$watchdog = Get-CimInstance Win32_Process -Filter "name='python.exe' OR name='pythonw.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*tunnel_watchdog.py*" }

$currentUrl = (Get-Content $urlFile -ErrorAction SilentlyContinue | Select-Object -First 1)
$tunnelOk   = ($watchdog -and (Url-Reachable $currentUrl))

if ($tunnelOk) {
  Write-Host "Tunnel healthy: $currentUrl  (watchdog already running - not disturbing it)."
} else {
  Write-Host "Tunnel is down or has no watchdog - recovering it..."
  # Kill any stale FightSync watchdog and its cloudflared child (scoped to :$port so other tunnels are untouched).
  if ($watchdog) { $watchdog | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
  Get-CimInstance Win32_Process -Filter "name='cloudflared.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*localhost:$port*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 1
  $runner = if (Test-Path $pyw) { $pyw } else { $py }
  Start-Process -FilePath $runner -ArgumentList "tunnel_watchdog.py" -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
  Write-Host "Watchdog (re)started; it will publish the live URL to current-url.txt shortly."
}

# ---- 3) surface the public URL ---------------------------------------------
Write-Host ""
Write-Host "============================================================"
$final = $null
for ($i = 0; $i -lt 60; $i++) {
  $final = (Get-Content $urlFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($final -and (Url-Reachable $final)) { break }
  Start-Sleep -Seconds 1
}
if ($final) {
  Write-Host "  Public URL:  $final"
} else {
  Write-Host "  Public URL:  (still resolving - watch current-url.txt)"
}
Write-Host "  Local UI:    http://127.0.0.1:$port"
Write-Host "  Password is in fightsync-secret.txt"
Write-Host "  The watchdog keeps the tunnel alive and updates current-url.txt"
Write-Host "  (the URL changes whenever the tunnel restarts)."
Write-Host "============================================================"
