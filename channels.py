"""Channel registry + live detection for auto-capture.

Channels are registered by URL/handle. We use yt-dlp to check whether a channel
is currently live (no API key needed — it resolves youtube.com/@handle/live and
reads is_live). Live status is cached briefly so the UI can poll cheaply.

Each channel can also store a crop template (the gameplay viewport rectangle for
that channel's broadcast layout), used to strip the bordering overlays.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp

ROOT = Path(__file__).parent
STORE = ROOT / "channels.json"

_LIVE_TTL = 45.0           # seconds to cache a live-status result
_live_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


# ── persistence ─────────────────────────────────────────────────────────────
def load() -> list[dict]:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save(channels: list[dict]) -> None:
    STORE.write_text(json.dumps(channels, indent=2), encoding="utf-8")


def _handle_from_url(url: str) -> str:
    m = re.search(r"@([A-Za-z0-9_.-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/(?:c/|channel/|user/)?([A-Za-z0-9_.-]+)", url)
    return m.group(1) if m else url


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if url.startswith("@"):
        return f"https://www.youtube.com/{url}"
    if not url.startswith("http"):
        return f"https://www.youtube.com/@{url}"
    return url


def resolve_name(url: str) -> str:
    """Best-effort friendly channel name; falls back to the handle."""
    try:
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": True, "playlist_items": "0"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("channel") or info.get("title") or _handle_from_url(url)
    except Exception:
        return _handle_from_url(url)


def add(url: str) -> dict:
    url = _normalize_url(url)
    channels = load()
    for c in channels:
        if c["url"].rstrip("/") == url.rstrip("/"):
            return c                      # already registered
    ch = {"id": uuid.uuid4().hex[:8], "url": url,
          "name": resolve_name(url), "crop": None}
    channels.append(ch)
    save(channels)
    return ch


def remove(cid: str) -> None:
    save([c for c in load() if c["id"] != cid])


def get(cid: str) -> Optional[dict]:
    return next((c for c in load() if c["id"] == cid), None)


def set_crop(cid: str, crop: Optional[dict]) -> Optional[dict]:
    channels = load()
    for c in channels:
        if c["id"] == cid:
            c["crop"] = crop
            save(channels)
            return c
    return None


# ── live detection ──────────────────────────────────────────────────────────
def check_live(url: str, use_cache: bool = True) -> dict:
    """Return {'live': bool, 'id': str|None, 'title': str|None}."""
    key = url.rstrip("/")
    now = time.time()
    if use_cache:
        with _lock:
            hit = _live_cache.get(key)
            if hit and now - hit[0] < _LIVE_TTL:
                return hit[1]

    live_url = key if key.endswith("/live") else key + "/live"
    result = {"live": False, "id": None, "title": None}
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(live_url, download=False)
        if info.get("is_live"):
            result = {"live": True, "id": info.get("id"),
                      "title": info.get("title")}
    except Exception:
        pass  # not live / unavailable

    with _lock:
        _live_cache[key] = (now, result)
    return result


# ── clip download (e.g. a Meta Quest share link) ────────────────────────────
def download_clip(url: str, dst_dir, name: str = "gameplay",
                  on_progress=None, on_status=None) -> str:
    """Download a video from a share link with yt-dlp; return the file path.

    `on_progress(pct)` fires only when the server reports a total size (Meta's CDN
    often doesn't); `on_status(msg)` always fires with a human message (downloaded
    MB, merging, retrying) so the UI never just sits at a dead 0%."""
    from pathlib import Path as _P
    dst = _P(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    holder = {}

    def say(msg):
        if on_status:
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001
                pass

    def hook(d):
        st = d.get("status")
        if st == "downloading":
            dl = d.get("downloaded_bytes", 0) or 0
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            if tot and on_progress:
                on_progress(int(dl / tot * 100))
            mb = dl / 1e6
            say(f"Downloading… {mb:.0f} MB"
                + (f" of {tot/1e6:.0f} MB" if tot else " (size unknown)"))
        elif st == "finished":
            holder["file"] = d.get("filename")
            say("Merging audio + video…")          # ffmpeg postprocess, no % here

    # Clean, unique output name — DON'T template on %(id)s: for a direct fbcdn CDN
    # link the "id" is the URL path and its ?query string leaks into the filename
    # (that's the ...mp4？_nc_cat=...fbcdn.mp4 garbage). A short uuid keeps it tidy.
    base = {
        "outtmpl": str(dst / f"{name}-meta-{uuid.uuid4().hex[:10]}.%(ext)s"),
        "format": "mp4/bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "progress_hooks": [hook],
        "socket_timeout": 30,                       # don't hang forever on a stalled CDN
        "retries": 3, "fragment_retries": 3,
        "http_headers": {"User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0")},
    }
    # Try plain first (public clips). Only if that fails do we borrow browser
    # cookies for login-gated Horizon clips — but reading a RUNNING browser's
    # cookie DB can be slow/blocked (app-bound encryption), so we announce it.
    attempts = [("", base)]
    for browser in ("edge", "chrome"):
        attempts.append((browser, {**base, "cookiesfrombrowser": (browser,)}))

    errors = []
    for browser, opts in attempts:
        try:
            if browser:
                say(f"Public download failed — trying your {browser} login "
                    f"(close {browser} if this hangs)…")
            else:
                say("Starting download…")
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            rd = (info.get("requested_downloads") or [{}])[0]
            return rd.get("filepath") or holder.get("file") or ydl.prepare_filename(info)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    # A Meta "share page" (horizon.meta.com/shares/…) only plays the video after you
    # sign in — the real video URL is fetched by the page's JavaScript and is NOT in
    # the HTML, so no downloader can see it. Guide the user to the DIRECT video link.
    low = url.lower()
    if "horizon.meta.com" in low or "/shares/" in low or "meta.com/" in low:
        raise RuntimeError(
            "That's a Meta SHARE PAGE — it needs your Meta login to play, so the "
            "downloader can't reach the actual video. Fix: open the link in your "
            "browser, RIGHT-CLICK the playing video → 'Copy video address', and paste "
            "THAT instead (a long https://…fbcdn.net/…mp4 link — that kind imports "
            "fine). If 'Copy video address' gives a 'blob:' link, press F12 → Network "
            "→ Media, replay the clip, and copy the .mp4 request's URL.")
    raise errors[0]   # the plain attempt's error is usually the most informative
