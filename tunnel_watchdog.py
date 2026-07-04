"""Tunnel watchdog: keeps the Cloudflare quick tunnel alive AND keeps
`current-url.txt` pointing at the live URL.

Starts cloudflared, captures the public URL, writes it to current-url.txt, and
monitors it. If cloudflared dies, or the public URL stops responding (drops /
HTTP 530 flakiness), it restarts the tunnel. Crucially, it re-reads the log on
every health check, so if the URL changes (or appears late, when the network is
slow), current-url.txt is updated as soon as the new URL shows up — it never
gets stuck on a dead URL.

Note: a quick-tunnel URL CHANGES on every restart — this keeps you online, but a
truly permanent URL needs a custom domain (named tunnel) or a stable-URL service.

Run:  .venv\\Scripts\\python tunnel_watchdog.py   (leave it running)
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CF = str(ROOT / "cloudflared.exe")
PORT = os.environ.get("FIGHTSYNC_PORT", "8765")
LOG = ROOT / "tunnel.log"
URL_FILE = ROOT / "current-url.txt"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

CHECK_EVERY = 15   # seconds between health checks
URL_WAIT = 90      # seconds to wait for a URL right after a (re)start
FAIL_LIMIT = 2     # consecutive unreachable checks before recycling the tunnel


def latest_log_url() -> str | None:
    """The most recent trycloudflare URL currently in the log, if any."""
    if not LOG.exists():
        return None
    found = URL_RE.findall(LOG.read_text(encoding="utf-8", errors="ignore"))
    return found[-1] if found else None


def saved_url() -> str | None:
    try:
        return URL_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def publish(url: str | None) -> None:
    """Write the live URL to current-url.txt, but only when it actually changes."""
    if url and url != saved_url():
        URL_FILE.write_text(url, encoding="utf-8")
        print(f"[watchdog] URL updated -> {url}", flush=True)


def reachable(url: str) -> bool:
    try:
        req = urllib.request.Request(url + "/login", headers={"User-Agent": "watchdog"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def spawn():
    """Start a fresh cloudflared. Returns (proc, url); url may be None if it's
    slow to come up — the monitor loop will pick it up once it appears."""
    try:
        LOG.write_text("", encoding="utf-8")
    except OSError:
        pass
    # CREATE_NO_WINDOW: the watchdog runs under console-less pythonw, so launching
    # console-mode cloudflared.exe would otherwise pop a NEW empty terminal window
    # every time the tunnel is (re)started — and quick tunnels recycle often. This
    # flag keeps cloudflared windowless. (0 on non-Windows.)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [CF, "tunnel", "--url", f"http://localhost:{PORT}", "--logfile", str(LOG)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=no_window)
    url = None
    waited = 0.0
    while waited < URL_WAIT:
        time.sleep(1.0)
        waited += 1.0
        if proc.poll() is not None:                  # died during startup
            print("[watchdog] cloudflared died during startup", flush=True)
            break
        url = latest_log_url()
        if url:
            publish(url)
            print(f"[watchdog] tunnel up: {url}", flush=True)
            break
    if not url:
        print("[watchdog] no URL yet — will keep watching the log", flush=True)
    return proc, url


def kill(proc) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    except Exception:
        pass


def main():
    proc, url = spawn()
    fails = 0
    while True:
        time.sleep(CHECK_EVERY)

        # 1) cloudflared exited -> restart it
        if proc.poll() is not None:
            print("[watchdog] cloudflared exited — restarting", flush=True)
            proc, url = spawn()
            fails = 0
            continue

        # 2) catch a URL that appeared late or changed without a full restart
        log_url = latest_log_url()
        if log_url and log_url != url:
            url = log_url
            publish(url)
            fails = 0

        # 3) no URL yet — keep waiting (nothing to health-check)
        if not url:
            continue

        # 4) health-check the live URL
        if reachable(url):
            publish(url)          # belt-and-suspenders: keep the file correct
            fails = 0
        else:
            fails += 1
            print(f"[watchdog] unreachable x{fails} ({url})", flush=True)
            if fails >= FAIL_LIMIT:                   # ~30s of failure -> recycle
                print("[watchdog] recycling tunnel", flush=True)
                kill(proc)
                time.sleep(2)
                proc, url = spawn()
                fails = 0


if __name__ == "__main__":
    main()
