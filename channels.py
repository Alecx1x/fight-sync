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
                  on_progress=None) -> str:
    """Download a video from a share link with yt-dlp; return the file path."""
    from pathlib import Path as _P
    dst = _P(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    holder = {}

    def hook(d):
        if d.get("status") == "downloading" and on_progress:
            tot = d.get("total_bytes") or d.get("total_bytes_estimate")
            if tot:
                on_progress(int(d.get("downloaded_bytes", 0) / tot * 100))
        elif d.get("status") == "finished":
            holder["file"] = d.get("filename")

    base = {
        "outtmpl": str(dst / f"{name}-meta-%(id)s.%(ext)s"),
        "format": "mp4/bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "progress_hooks": [hook],
        "http_headers": {"User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Edg/124.0")},
    }
    # Try plain first, then borrow Meta-login cookies from the user's browser so
    # login-gated Horizon clips become downloadable (no effect if the clip is public).
    attempts = [base]
    for browser in ("edge", "chrome"):
        attempts.append({**base, "cookiesfrombrowser": (browser,)})

    errors = []
    for opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            rd = (info.get("requested_downloads") or [{}])[0]
            return rd.get("filepath") or holder.get("file") or ydl.prepare_filename(info)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    raise errors[0]   # the plain attempt's error is usually the most informative
