"""FightSync — local web app to auto-sync Thrill of the Fight 2 gameplay with
facecam, then composite, subtitle, and top-and-tail it for YouTube.

Run:  python app.py   then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
from http.cookies import SimpleCookie
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

import capture as capture_mod
import channels as ch_mod
try:
    import quest as quest_mod          # Quest USB auto-import (needs pywin32)
except Exception:                       # pragma: no cover - keep app up if missing
    quest_mod = None
try:
    import iphone as iphone_mod        # iPhone USB import via the shell namespace (needs pywin32)
except Exception:                       # pragma: no cover - keep app up if missing
    iphone_mod = None
import youtube_upload as yt_mod
from annotations import render_overlay
from cropdetect import apply_crop, detect_crop
from media import FFMPEG, probe, run_ffmpeg, waveform_peaks
from pipeline import RenderConfig, _enc, render_multi
from shorts import make_reel
from tracking import track_region

ROOT = Path(__file__).parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
REC_DIR = ROOT / "recordings"
REC_DIR.mkdir(exist_ok=True)
LABELS_FILE = REC_DIR / "labels.json"
SESSIONS_DIR = ROOT / "sessions"      # saved synced-clip sets (clips + offsets + trims)
SESSIONS_DIR.mkdir(exist_ok=True)
FIGHTER_DIR = ROOT / "fighters"       # saved fighter photos/cutouts + the user's profile
FIGHTER_DIR.mkdir(exist_ok=True)
FIGHTER_PROFILE = FIGHTER_DIR / "profile.json"


def _load_labels() -> dict:
    """User-given names for saved clips, keyed by filename. Lets a clip keep the
    name the user typed at upload so the saved-uploads dropdown stays categorized
    across reloads (instead of falling back to the auto 'Library upload · …')."""
    try:
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _set_label(filename: str, label: str) -> None:
    labels = _load_labels()
    labels[filename] = label
    try:
        LABELS_FILE.write_text(json.dumps(labels), encoding="utf-8")
    except Exception:
        pass
PREV_DIR = ROOT / "previews"
PREV_DIR.mkdir(exist_ok=True)

app = FastAPI(title="FightSync")

# ── authentication (a password gate; required before any public tunnel) ──────
SECRET_FILE = ROOT / "fightsync-secret.txt"


def _load_password() -> str:
    env = os.environ.get("FIGHTSYNC_PASSWORD")
    if env:
        return env.strip()
    if SECRET_FILE.exists():
        pw = SECRET_FILE.read_text(encoding="utf-8").strip()
        if pw:
            return pw
    pw = pysecrets.token_urlsafe(9)
    SECRET_FILE.write_text(pw, encoding="utf-8")
    return pw


PASSWORD = _load_password()
AUTH_TOKEN = hmac.new(PASSWORD.encode(), b"fightsync-auth", hashlib.sha256).hexdigest()
COOKIE = "fs_auth"
OPEN_PATHS = {"/login", "/favicon.ico", "/favicon.svg"}

# Password gate on/off. Turned OFF by request — flip back on by setting the env
# var FIGHTSYNC_AUTH=1 (or change the default below to "1") and restarting.
# Heads-up: with this off and the tunnel up, anyone with the URL can reach the
# app (incl. /api/media file reads). The random tunnel URL is the only cover.
AUTH_ENABLED = os.environ.get("FIGHTSYNC_AUTH", "0") == "1"

_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<link rel=icon type=image/svg+xml href=/favicon.svg><title>FightSync</title>
<style>body{{margin:0;height:100vh;display:grid;place-items:center;background:#0b0e13;
color:#e7ecf3;font:15px/1.5 "Segoe UI",system-ui,sans-serif}}
.box{{background:#141a23;border:1px solid #26303f;border-radius:14px;padding:28px;width:300px;text-align:center}}
.logo{{font-size:34px}}h1{{font-size:19px;margin:6px 0 18px}}
input{{width:100%;background:#0d131b;border:1px solid #26303f;color:#e7ecf3;border-radius:9px;
padding:11px;font-size:15px;margin-bottom:12px}}
button{{width:100%;padding:12px;border:0;border-radius:9px;font-weight:700;color:#fff;cursor:pointer;
background:linear-gradient(145deg,#e23b3b,#b32a2a)}}.err{{color:#ff8d8d;font-size:13px;margin-bottom:10px}}</style>
</head><body><form class=box method=post action=/login>
<div class=logo>🥊</div><h1>FightSync</h1>{err}
<input type=password name=password placeholder=Password autofocus>
<input type=hidden name=next value="{next}"><button>Unlock</button></form></body></html>"""


class _AuthMiddleware:
    """Pure-ASGI auth gate. (A BaseHTTPMiddleware here would buffer responses and
    break Range requests, which makes videos non-seekable in /api/media.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not AUTH_ENABLED or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        # /overlay is loaded by OBS as a browser source (no login cookie) — keep it open.
        # /ninja/* is the WebRTC phone-camera capture — the phone browser has no login cookie either.
        authed = (path in OPEN_PATHS or path == "/overlay" or path.startswith("/api/overlay")
                  or path.startswith("/ninja") or path.startswith("/api/ninja"))
        if not authed and "cookie" in headers:
            ck = SimpleCookie()
            ck.load(headers["cookie"])
            authed = COOKIE in ck and ck[COOKIE].value == AUTH_TOKEN
        if authed:
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "GET" and "text/html" in headers.get("accept", ""):
            resp = RedirectResponse(f"/login?next={path}", status_code=302)
        else:
            resp = PlainTextResponse("Unauthorized", status_code=401)
        await resp(scope, receive, send)


app.add_middleware(_AuthMiddleware)


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/"):
    return _LOGIN_HTML.format(err="", next=next or "/")


@app.post("/login")
def login_submit(request: Request, password: str = Form(...), next: str = Form("/")):
    if hmac.compare_digest(password, PASSWORD):
        # mark the cookie Secure when we're actually behind HTTPS (the tunnel sets
        # X-Forwarded-Proto), but not on plain-http LAN access, so both work.
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        secure = proto == "https" or request.url.scheme == "https"
        resp = RedirectResponse(next or "/", status_code=302)
        resp.set_cookie(COOKIE, AUTH_TOKEN, httponly=True, samesite="lax",
                        secure=secure, max_age=60 * 60 * 24 * 30)
        return resp
    return HTMLResponse(
        _LOGIN_HTML.format(err='<div class=err>Wrong password</div>', next=next or "/"),
        status_code=401)


# in-memory job registry (single-user local tool)
JOBS: dict[str, dict] = {}


def _set(job_id: str, **kw):
    JOBS[job_id].update(kw)


def _progress(job_id: str):
    def cb(pct: int, msg: str):
        _set(job_id, percent=pct, message=msg)
    return cb


def _save_upload(upload: UploadFile, dst: Path):
    with dst.open("wb") as out:
        shutil.copyfileobj(upload.file, out, length=1024 * 1024)


def _resolve_input(job_dir: Path, name: str,
                   upload: Optional[UploadFile], path: Optional[str]) -> str:
    """Accept either an uploaded file or a local path on disk."""
    if path:
        p = Path(path.strip().strip('"'))
        if not p.exists():
            raise HTTPException(400, f"{name} path does not exist: {p}")
        return str(p)
    if upload and upload.filename:
        suffix = Path(upload.filename).suffix or ".mp4"
        dst = job_dir / f"{name}{suffix}"
        _save_upload(upload, dst)
        return str(dst)
    raise HTTPException(400, f"No {name} file or path provided.")


def _run_job(job_id: str, gameplays: list, facecams: list, cfg: RenderConfig,
             vs_cfg: dict = None, cold_open: bool = True):
    job_dir = JOBS_DIR / job_id
    work = job_dir / "work"
    out = job_dir / "out"
    try:
        _set(job_id, status="running")
        result = render_multi(gameplays, facecams, str(out), str(work), cfg,
                              _progress(job_id))
        result.setdefault("download_name", _safe_name(_project_name(), "fightsync_final") + ".mp4")
        body = result.get("final")                  # the composite, before any intro/hook
        if vs_cfg:                                  # auto-prepend the VS fighter intro
            try:
                result["final"] = _prepend_vs_intro(result["final"], vs_cfg,
                                                    work, job_id)
            except Exception:  # noqa: BLE001 — never fail the whole render over the intro
                traceback.print_exc()
        if cold_open and body and result.get("final"):   # COLD-OPEN hook: best moment up front
            try:
                result["final"] = _prepend_hook(result["final"], body, work, job_id)
            except Exception:  # noqa: BLE001 — never fail the render over the hook
                traceback.print_exc()
        if result.get("final") and (Path(result["final"]).parent / "thumbnail.jpg").exists():
            result["thumbnail"] = True              # auto-thumbnail from the tale-of-the-tape card
        _set(job_id, status="done", result=result, percent=100, message="Done.")
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e),
             traceback=traceback.format_exc())


def _render_vs(out, cfg, lc, rc, lr, rr, fps, work):
    """Render the VS intro to `out`. Prefers the cinematic BROWSER scene (html_render,
    headless Chromium); falls back to the Pillow renderer (vs_intro) if that fails."""
    try:
        import html_render
        html_render.render_vs_intro(out, cfg.get("left", {}), cfg.get("right", {}), work,
                                    left_reel=lr, right_reel=rr, W=1280, H=720, fps=int(round(fps)),
                                    style=cfg.get("style", "cinematic"))
        return out
    except Exception:  # noqa: BLE001 — Chromium missing / scene error → Pillow fallback
        import traceback as _tb
        print("[vs] browser render failed, falling back to Pillow:\n", _tb.format_exc())
        import vs_intro
        vs_intro.render_vs_intro(out, cfg.get("left", {}), cfg.get("right", {}),
                                 left_color=lc, right_color=rc, W=1280, H=720, fps=fps,
                                 title=cfg.get("title", "TALE OF THE TAPE"),
                                 left_reel=lr, right_reel=rr)
        return out


def _make_thumbnail(intro_src: str, out_jpg: str):
    """Auto-thumbnail: a settled, slightly zoomed-in (readable) frame of the tale-of-the-tape card."""
    try:
        idur = probe(intro_src).duration or 3.0
    except Exception:  # noqa: BLE001
        idur = 3.0
    t = max(0.1, idur * 0.72)                     # during the full-card hold (after the build-in settles)
    z = 0.94                                       # GENTLE zoom — tightens without clipping the names/stats
    vf = (f"crop=iw*{z}:ih*{z}:(iw-iw*{z})/2:(ih-ih*{z})/2,"
          f"scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,format=yuvj420p")
    run_ffmpeg(["-ss", f"{t:.2f}", "-i", intro_src, "-frames:v", "1", "-vf", vf, "-q:v", "2", out_jpg])


def _prepend_vs_intro(final_path: str, vs_cfg: dict, work, job_id):
    """Render the VS intro at the final clip's size/fps and concat it in front."""
    import vs_intro
    _set(job_id, message="Adding the VS intro…")
    info = probe(final_path)
    W = int(info.width or 1280); H = int(info.height or 720)
    fps = round(info.fps or 30, 3)
    W -= W % 2; H -= H % 2
    intro = str(Path(work) / "vs_intro.mp4")
    lc = _hex_rgb(vs_cfg.get("left_color"), vs_intro.RED)
    rc = _hex_rgb(vs_cfg.get("right_color"), vs_intro.BLUE)
    lr, rr = _reel_dir(vs_cfg.get("left_reel")), _reel_dir(vs_cfg.get("right_reel"))
    # Render the cinematic BROWSER scene at 1280x720 (the same size the preview uses); the
    # concat below upscales to the final W×H (both 16:9). Falls back to the Pillow renderer
    # if headless Chromium is unavailable.
    _render_vs(intro, vs_cfg, lc, rc, lr, rr, fps, str(Path(work) / "vs_html"))
    try:                                          # auto-thumbnail from the tale-of-the-tape card
        _make_thumbnail(intro, str(Path(final_path).parent / "thumbnail.jpg"))
    except Exception:  # noqa: BLE001
        pass
    out = str(Path(final_path).with_name("final_vs.mp4"))
    # concat (scale-safe) so the intro and the body share format
    if info.has_audio:
        run_ffmpeg(["-i", intro, "-i", final_path, "-filter_complex",
                    f"[0:v]scale={W}:{H},setsar=1,fps={fps}[v0];"
                    f"[1:v]scale={W}:{H},setsar=1,fps={fps}[v1];"
                    "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];"
                    "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[a1];"
                    "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "19", "-c:a", "aac", "-movflags", "+faststart", out])
    else:
        run_ffmpeg(["-i", intro, "-i", final_path, "-filter_complex",
                    f"[0:v]scale={W}:{H},setsar=1,fps={fps}[v0];"
                    f"[1:v]scale={W}:{H},setsar=1,fps={fps}[v1];"
                    "[v0][v1]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "19", "-movflags", "+faststart", out])
    return out


def _find_peak_window(src: str, total: float, clip_len: float = 4.0) -> float:
    """Center time of the most action-packed ~clip_len window, by audio energy (game SFX/grunts ≈ action)."""
    try:
        import numpy as np
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", src,
                            "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, timeout=180)
        a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
        sr, win = 8000, max(1, int(8000 * clip_len))
        if a.size < win + sr:
            raise ValueError("no/short audio")
        e = np.convolve(a * a, np.ones(win, dtype=np.float32), mode="valid")  # windowed energy envelope
        lo, hi = int(len(e) * 0.06), max(int(len(e) * 0.06) + 1, int(len(e) * 0.94))   # skip the ends
        c = (lo + int(np.argmax(e[lo:hi])) + win / 2.0) / sr
    except Exception:  # noqa: BLE001
        c = total * 0.4                                                       # fallback: ~40% in
    return max(clip_len / 2, min(max(clip_len / 2, total - clip_len / 2), c))


def _prepend_hook(final_path: str, body: str, work, job_id, label: str = "COMING UP"):
    """Cut the most action-packed ~4s out of the body, brand it as a cold-open teaser (white flash-in +
    a 'COMING UP' banner), and concat it in FRONT of the finished video — fights the early drop-off."""
    bdur = probe(body).duration or 0.0
    clip_len = 4.0
    if bdur < clip_len + 1.5:
        return final_path                                                     # too short to tease
    _set(job_id, message="Adding the cold-open hook…")
    info = probe(final_path)
    W = int(info.width or 1280); H = int(info.height or 720); fps = round(info.fps or 30, 3)
    W -= W % 2; H -= H % 2
    c = _find_peak_window(body, bdur, clip_len)
    start = max(0.0, min(c - clip_len / 2, bdur - clip_len))
    from pipeline import _ensure_font
    font = _ensure_font(str(work))                                            # 'font.ttf' (relative → cwd=work)
    hook = str(Path(work) / "hook.mp4")
    vf = (f"fps={fps},scale={W}:{H},setsar=1,"
          f"fade=t=in:st=0:d=0.2:color=white,fade=t=out:st={clip_len - 0.25:.2f}:d=0.25,"
          f"drawbox=x=0:y=0:w=iw:h=ih*0.15:color=black@0.55:t=fill,"
          f"drawtext=fontfile={font}:text='{label}':fontcolor=white:fontsize=h/13:"
          f"x=(w-text_w)/2:y=h*0.04:borderw=3:bordercolor=black")
    common = ["-ss", f"{start:.2f}", "-t", f"{clip_len}", "-i", body, "-vf", vf,
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", "-movflags", "+faststart"]
    run_ffmpeg(common + (["-c:a", "aac", hook] if info.has_audio else ["-an", hook]), cwd=str(work))
    out = str(Path(final_path).with_name("final_hook.mp4"))
    if info.has_audio:
        run_ffmpeg(["-i", hook, "-i", final_path, "-filter_complex",
                    f"[0:v]scale={W}:{H},setsar=1,fps={fps}[v0];[1:v]scale={W}:{H},setsar=1,fps={fps}[v1];"
                    "[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];"
                    "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[a1];"
                    "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "19", "-c:a", "aac", "-movflags", "+faststart", out])
    else:
        run_ffmpeg(["-i", hook, "-i", final_path, "-filter_complex",
                    f"[0:v]scale={W}:{H},setsar=1,fps={fps}[v0];[1:v]scale={W}:{H},setsar=1,fps={fps}[v1];"
                    "[v0][v1]concat=n=2:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19",
                    "-movflags", "+faststart", out])
    return out


def _page(name: str):
    """Serve an HTML page with no-store so a browser reload ALWAYS gets the latest UI
    (no stale-cache 'I don't see the new button' surprises)."""
    return HTMLResponse((ROOT / "static" / name).read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/", response_class=HTMLResponse)
def index():
    return _page("index.html")


@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    return FileResponse(str(ROOT / "static" / "favicon.svg"),
                        media_type="image/svg+xml")


@app.post("/api/render")
async def start_render(
    gameplay: Optional[UploadFile] = None,
    facecam: Optional[UploadFile] = None,
    gameplay_path: Optional[str] = Form(None),
    facecam_path: Optional[str] = Form(None),
    title: str = Form("The Thrill of the Fight 2"),
    intro_subtitle: str = Form(""),
    layout: str = Form("pip"),
    swap_pip: bool = Form(False),
    pip_position: str = Form("br"),
    pip_scale: float = Form(0.26),
    audio_mode: str = Form("mix"),
    color_punch: bool = Form(False),
    punch_strength: float = Form(1.0),
    music_path: str = Form(""),
    music_volume: float = Form(0.18),
    music_duck: bool = Form(True),
    hit_flash: bool = Form(False),
    hit_count: int = Form(6),
    hit_text: str = Form("BIG HIT!"),
    make_subtitles: bool = Form(True),
    burn_subtitles: bool = Form(True),
    subtitle_coach: bool = Form(True),
    sub_color_me: str = Form("FFE24D"),
    sub_color_coach: str = Form("53D8FF"),
    sub_color_ref: str = Form("FF5FC4"),
    sub_font_size: int = Form(96),
    edited_subs_json: str = Form(""),
    intro: bool = Form(True),
    outro: bool = Form(True),
    lower_third: str = Form(""),
    whisper_model: str = Form("base"),
    replays: bool = Form(False),
    replay_times: str = Form(""),
    auto_replays: int = Form(0),
    replay_smooth: bool = Form(False),
    slowmo_regions_json: str = Form("[]"),
    slowmo_speed: float = Form(0.35),
    manual_offsets_json: str = Form("[]"),
    round_trims_json: str = Form("[]"),
    gameplay_paths_json: str = Form(""),
    facecam_paths_json: str = Form(""),
    transitions: bool = Form(True),
    transition_label: str = Form("ROUND"),
    transition_style: str = Form("card"),
    bell: bool = Form(True),
    manual_offset: str = Form(""),
    trim_start: str = Form(""),
    trim_end: str = Form(""),
    multicam_angles_json: str = Form(""),
    multicam_cuts_json: str = Form(""),
    cam_b_paths_json: str = Form(""),
    round_cuts_json: str = Form(""),
    cam_a_offsets_json: str = Form(""),
    cam_b_offsets_json: str = Form(""),
    vs_intro_json: str = Form(""),
    spectator_clips_json: str = Form(""),
    spectator_offsets_json: str = Form(""),
    spectator_audio: int = Form(0),
    spectator_scale: float = Form(0),
    spectator_volume: float = Form(0.8),
    spectator_src: str = Form(""),
    cold_open: int = Form(1),
):
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    (job_dir / "in").mkdir(parents=True, exist_ok=True)

    def _resolve_list(name, json_str, upload, path):
        if json_str.strip():
            paths = json.loads(json_str)
            for p in paths:
                if not Path(p).exists():
                    raise HTTPException(400, f"{name} not found: {p}")
            if paths:
                return paths
        return [_resolve_input(job_dir / "in", name, upload, path)]

    gs = _resolve_list("gameplay", gameplay_paths_json, gameplay, gameplay_path)
    fs = _resolve_list("facecam", facecam_paths_json, facecam, facecam_path)
    if len(gs) != len(fs):
        raise HTTPException(400, f"Got {len(gs)} gameplay clip(s) but {len(fs)} "
                                 "facecam clip(s) — add the same number to each.")

    mc_angles = json.loads(multicam_angles_json) if multicam_angles_json.strip() else []
    mc_cuts = json.loads(multicam_cuts_json) if multicam_cuts_json.strip() else []
    for a in mc_angles:
        if not Path(a.get("path", "")).exists():
            raise HTTPException(400, f"camera angle not found: {a.get('path')}")

    # per-round multicam: a second cam per round + per-round cuts/offsets
    cam_b = json.loads(cam_b_paths_json) if cam_b_paths_json.strip() else []
    r_cuts = json.loads(round_cuts_json) if round_cuts_json.strip() else []
    a_offs = json.loads(cam_a_offsets_json) if cam_a_offsets_json.strip() else []
    b_offs = json.loads(cam_b_offsets_json) if cam_b_offsets_json.strip() else []
    spec_clips = json.loads(spectator_clips_json) if spectator_clips_json.strip() else []
    spec_offs = json.loads(spectator_offsets_json) if spectator_offsets_json.strip() else []
    for p in cam_b:
        if p and not Path(p).exists():
            raise HTTPException(400, f"second cam clip not found: {p}")
    # A sent spectator clip that's missing (e.g. the browser cached an OLD/deleted path in localStorage)
    # → fall back to the saved session's spec_clip/offset for that round, so a stale client path doesn't
    # silently drop the PiP. Truly-absent clips just blank out (render still succeeds).
    try:
        _saved = json.loads((SESSIONS_DIR / "latest.json").read_text(encoding="utf-8")).get("rounds") or []
    except (ValueError, OSError):
        _saved = []
    for i, cp in enumerate(spec_clips):
        if not cp or Path(cp).exists():
            continue                                     # no spectator wanted, or the sent path is valid
        sr = _saved[i] if i < len(_saved) else {}
        # 1) saved session's clip → 2) the most-recent extracted clip ON DISK for this round. This makes
        # the PiP survive a stale path in the browser AND a clobbered session — as long as the file exists.
        sp = sr.get("spec_clip")
        if not (sp and Path(sp).exists()):
            disk = sorted(REC_DIR.glob(f"spectator-r{i + 1}-*.mp4"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            sp = str(disk[0]) if disk else ""
        spec_clips[i] = sp if (sp and Path(sp).exists()) else ""
        if spec_clips[i]:
            if i >= len(spec_offs):
                spec_offs += [None] * (i + 1 - len(spec_offs))
            if spec_offs[i] is None:
                spec_offs[i] = sr.get("spec_offset")

    # manual slow-mo: per-round list of [{start,end}] windows marked in the Sync tab
    try:
        slowmo_regions = json.loads(slowmo_regions_json) if slowmo_regions_json.strip() else []
        if not isinstance(slowmo_regions, list):
            slowmo_regions = []
    except (ValueError, TypeError):
        slowmo_regions = []

    # per-round manual sync offsets + composite trims (clapperboard round tabs)
    def _safe_list(s):
        try:
            v = json.loads(s) if s.strip() else []
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []
    manual_offsets = _safe_list(manual_offsets_json)
    round_trims = _safe_list(round_trims_json)

    cfg = RenderConfig(
        title=title,
        intro_subtitle=intro_subtitle,
        layout=layout,
        swap_pip=swap_pip,
        pip_position=pip_position,
        pip_scale=pip_scale,
        audio_mode=audio_mode,
        color_punch=color_punch,
        punch_strength=punch_strength,
        music_path=(music_path.strip() or None),
        music_volume=music_volume,
        music_duck=music_duck,
        hit_flash=hit_flash,
        hit_count=hit_count,
        hit_text=hit_text,
        make_subtitles=make_subtitles,
        burn_subtitles=burn_subtitles and make_subtitles,
        subtitle_coach=subtitle_coach,
        sub_color_me=(sub_color_me or "FFE24D").lstrip("#"),
        sub_color_coach=(sub_color_coach or "53D8FF").lstrip("#"),
        sub_color_ref=(sub_color_ref or "FF5FC4").lstrip("#"),
        sub_font_size=max(24, min(220, int(sub_font_size or 96))),
        edited_subs=_parse_edited_subs(edited_subs_json),
        edited_subs_tl=(_subs_meta("latest") if edited_subs_json else []),
        intro=intro,
        outro=outro,
        lower_third=lower_third,
        whisper_model=whisper_model,
        replays=replays,
        replay_times=replay_times,
        auto_replays=auto_replays,
        replay_smooth=replay_smooth,
        slowmo_regions=slowmo_regions,
        slowmo_speed=slowmo_speed,
        transitions=transitions,
        transition_label=transition_label,
        transition_style=transition_style,
        bell=bell,
        manual_offset=(float(manual_offset) if manual_offset.strip() else None),
        manual_offsets=manual_offsets,
        trim_start=(float(trim_start) if trim_start.strip() else 0.0),
        trim_end=(float(trim_end) if trim_end.strip() else None),
        round_trims=round_trims,
        multicam_angles=mc_angles,
        multicam_cuts=mc_cuts,
        cam_b_paths=cam_b,
        round_cuts=r_cuts,
        cam_a_offsets=a_offs,
        cam_b_offsets=b_offs,
        spectator_clips=spec_clips,
        spectator_offsets=spec_offs,
        spectator_audio=bool(spectator_audio),
        spectator_scale=float(spectator_scale or 0),
        spectator_volume=float(spectator_volume if spectator_volume is not None else 0.8),
        spectator_credit=(_vod_credit(spectator_src) if any(spec_clips) else ""),
    )

    vs_cfg = None
    if vs_intro_json:
        try:
            vc = json.loads(vs_intro_json)
            if vc.get("enabled"):
                vs_cfg = vc
        except (ValueError, TypeError):
            pass

    JOBS[job_id] = {"status": "queued", "percent": 0,
                    "message": "Queued…", "result": None}
    threading.Thread(target=_run_job, args=(job_id, gs, fs, cfg, vs_cfg, bool(cold_open)),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_import_job(job_id: str, url: str, name: str):
    try:
        _set(job_id, status="running", message="Downloading clip…")
        path = ch_mod.download_clip(
            url, REC_DIR, name,
            on_progress=lambda p: _set(job_id, percent=p),
            on_status=lambda m: _set(job_id, message=m))
        _set(job_id, status="done", percent=100, message="Done.",
             result={"final": path, "name": name})
    except Exception as e:  # noqa: BLE001
        import re as _re
        msg = _re.sub(r"\x1b\[[0-9;]*m", "", str(e))   # strip ANSI colour codes
        _set(job_id, status="error",
             message=f"Couldn't import that link: {msg}",
             traceback=traceback.format_exc())


@app.post("/api/import_url")
def import_url(url: str = Form(...), name: str = Form("gameplay")):
    """Import a video from a share link (e.g. a Meta Quest clip) into a slot."""
    if not url.strip():
        raise HTTPException(400, "Empty URL")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0,
                    "message": "Queued…", "result": None}
    threading.Thread(target=_run_import_job,
                     args=(job_id, url.strip(), name), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/sync")
def api_sync(gameplay_paths_json: str = Form(...),
             facecam_paths_json: str = Form(...),
             g_start: float = Form(0.0), g_end: float = Form(0.0),
             f_start: float = Form(0.0), f_end: float = Form(0.0)):
    """Fast audio-only sync preview (no rendering): per pair, the offset and a
    confidence so you know alignment is solid before committing to a render.

    `g_start/g_end`/`f_start/f_end` (seconds) trim each clip's ends for the FIRST
    pair only, so auto-sync correlates just the clean window the user kept (junk
    ends won't throw off the lock). 0 = no trim. The offset is returned on the
    full-clip timeline, so it's used by the render verbatim."""
    from sync import compute_sync
    gs = json.loads(gameplay_paths_json)
    fs = json.loads(facecam_paths_json)
    n = min(len(gs), len(fs))
    work = JOBS_DIR / ("sync-" + uuid.uuid4().hex[:8])
    work.mkdir(parents=True, exist_ok=True)
    pairs = []
    try:
        for i in range(n):
            try:
                if not (Path(gs[i]).exists() and Path(fs[i]).exists()):
                    pairs.append({"error": "file missing"})
                    continue
                # per-clip end trims apply to the previewed (first) pair only
                ai, ao = (g_start, (g_end or None)) if i == 0 else (0.0, None)
                bi, bo = (f_start, (f_end or None)) if i == 0 else (0.0, None)
                s = compute_sync(gs[i], fs[i], str(work),
                                 a_in=ai, a_out=ao, b_in=bi, b_out=bo)
                g, f = probe(gs[i]), probe(fs[i])
                out_dur = min(g.duration - s.a_start, f.duration - s.b_start)
                pairs.append({
                    "offset": round(s.offset_seconds, 2),
                    "confidence": round(s.confidence, 2),
                    "out_dur": round(max(0.0, out_dur), 1),
                    "ok": out_dur > 0.3 and s.confidence >= 0.20,
                    "shares_audio": out_dur > 0.3,
                })
            except Exception as e:  # noqa: BLE001
                pairs.append({"error": str(e)})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"pairs": pairs, "gameplay": len(gs), "facecam": len(fs),
            "counts_match": len(gs) == len(fs)}


@app.get("/api/sync/geometry")
def sync_geometry(session: str = "latest"):
    """Per-round timeline geometry for the Sync-tab trim UI: the UNtrimmed composite length
    (out_dur), the facecam-start offset (fs) so the preview can seek, the facecam path (for a
    proxy), and the current saved composite trim. Anchors EXACTLY like the render/editor."""
    p = SESSIONS_DIR / ("latest.json" if session in ("", "latest") else f"{session}.json")
    rounds = []
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            d = {}
        for i, r in enumerate(d.get("rounds") or []):
            gp, fc = r.get("gameplay"), r.get("facecam")
            if not gp or not fc or not Path(gp).exists() or not Path(fc).exists():
                continue
            cam2 = r.get("cam2")
            if cam2 and Path(cam2).exists():
                cA = float(r.get("camA_offset") or 0.0)
                cB = float(r.get("camB_offset") or 0.0)
                gs = max(0.0, cA, cB)
                fs = max(0.0, gs - cA)
            else:
                off = float(r.get("offset") or 0.0)
                gs, fs = max(off, 0.0), max(-off, 0.0)
            try:
                gd = probe(gp).duration or 0.0
                fd = probe(fc).duration or 0.0
            except Exception:  # noqa: BLE001
                gd = fd = 0.0
            out_dur = max(0.0, min(gd - gs, fd - fs))   # UNtrimmed composite length
            rounds.append({"round": r.get("round", i + 1), "facecam": fc,
                           "gs": round(gs, 3), "fs": round(fs, 3),
                           "out_dur": round(out_dur, 2), "trim": r.get("trim")})
    return {"rounds": rounds}


PROXY_DIR = REC_DIR / "proxies"
PROXY_DIR.mkdir(exist_ok=True)


def _proxy_path(src: str, seconds: float = 0, pad_to: float = 0, start: float = 0) -> Path:
    p = Path(src)
    # bump the version tag to invalidate all cached proxies when the encode changes
    # (v2 = forced 8-bit yuv420p, so 10-bit iPhone HEVC sources become browser-playable)
    ver = "v3"
    pad = f":pad{round(pad_to, 1)}" if pad_to else ""
    st = f":s{round(start, 2)}" if start else ""        # trimmed-window proxy (starts at `start`)
    try:
        stamp = f"{ver}:{p.resolve()}:{p.stat().st_mtime_ns}:{p.stat().st_size}:{seconds}{pad}{st}"
    except OSError:
        stamp = f"{ver}:{p}:{seconds}{pad}{st}"
    key = hashlib.sha1(stamp.encode()).hexdigest()[:14]
    return PROXY_DIR / f"{key}.mp4"


def _run_ffmpeg_progress(cmd: list, total: float, job_id: str):
    """Run ffmpeg parsing `-progress` on stdout → live percent on the job. ffmpeg
    emits out_time_us (microseconds) every chunk; pct = elapsed / total."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    err: list = []
    et = threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True)
    et.start()
    last = -1
    for line in proc.stdout:                       # drains the progress pipe
        line = line.strip()
        if line.startswith(("out_time_us=", "out_time_ms=")):
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            secs = us / 1e6
            if total and total > 0:
                pct = max(1, min(99, int(secs / total * 100)))
                if pct != last:
                    last = pct
                    _set(job_id, status="running", percent=pct,
                         message=f"Optimizing preview… {pct}%")
            else:
                # no known duration — show elapsed encoded time so it isn't a dead 0%
                cur = int(secs)
                if cur != last:
                    last = cur
                    _set(job_id, status="running", percent=0,
                         message=f"Optimizing preview… {cur}s done")
    proc.wait()
    et.join(timeout=1)
    return proc.returncode, "".join(err)


def _run_proxy_job(job_id: str, src: str, seconds: float, pad_to: float = 0, start: float = 0):
    try:
        dst = _proxy_path(src, seconds, pad_to, start)
        if not dst.exists():
            _set(job_id, status="running", percent=0, message="Optimizing preview…")
            try:
                total = probe(src).duration or 0.0
            except Exception:
                total = 0.0
            if seconds and seconds > 0:
                total = min(total, seconds) if total else seconds
            # if this clip is SHORTER than its pair, pad the tail with black so both
            # clips are the same length and the sync tools don't break on the gap
            # (e.g. a phone cam ended early while gameplay kept recording).
            padding = bool(pad_to) and total > 0 and pad_to > total + 0.3 and not (seconds and seconds > 0)
            scale = ("scale=854:480:force_original_aspect_ratio=decrease,"
                     "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30")
            if padding:
                scale += f",tpad=stop_mode=add:stop_duration={pad_to - total:.3f}:color=black"
                total = pad_to                       # progress bar spans the padded length
            # -g 15 + closed GOP = keyframe ~every 0.5s so the preview can seek/nudge
            # anywhere without stalling. -pix_fmt yuv420p forces 8-bit (10-bit iPhone
            # HEVC would otherwise become H.264 High 10 — unplayable in browsers/black).
            vf = ["-vf", scale]
            common = ["-pix_fmt", "yuv420p", "-g", "15", "-keyint_min", "15",
                      "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                      str(dst)]
            nvenc_tail = vf + ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "28"] + common
            x264_tail = vf + ["-c:v", "libx264", "-preset", "ultrafast",
                              "-crf", "30", "-sc_threshold", "0"] + common
            dur = (["-t", str(seconds)] if seconds and seconds > 0 else [])
            ss = (["-ss", f"{start:.3f}"] if start and start > 0.05 else [])   # seek to the kept-window start
            base = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-progress", "pipe:1", "-nostats"]
            # 1) full GPU (NVDEC decode + NVENC encode) — fastest for big HEVC phone
            #    clips; 2) GPU/auto decode + libx264; 3) all software. First that works.
            # tpad is a CPU filter, so when padding skip the GPU attempt.
            attempts = ([([], x264_tail)] if padding else
                        [(["-hwaccel", "cuda"], nvenc_tail),
                         (["-hwaccel", "auto"], x264_tail),
                         ([], x264_tail)])
            last_err = None
            for hw, tail in attempts:
                rc, err = _run_ffmpeg_progress(base + hw + ss + dur + ["-i", src] + tail,
                                               total, job_id)
                if rc == 0:
                    break
                last_err = err
                try:
                    dst.unlink()                   # drop any partial before retrying
                except OSError:
                    pass
            else:
                raise RuntimeError("proxy failed:\n" + (last_err or "")[-600:])
        _set(job_id, status="done", percent=100, message="ready",
             result={"path": str(dst)})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e),
             traceback=traceback.format_exc())


@app.post("/api/proxy")
def proxy(path: str = Form(...), seconds: float = Form(0), pad_match: str = Form(""),
          start: float = Form(0)):
    """A small 480p H.264 proxy for smooth in-browser preview. `seconds`>0 only transcodes that many
    seconds; `start`>0 begins at that offset → together they make a PHYSICALLY-TRIMMED preview (the
    kept window only, so the dead air isn't on the timeline at all). `pad_match` = comma-separated
    sibling clip paths; if any is longer, this clip's proxy is padded with black so they sync cleanly."""
    if not Path(path).exists():
        raise HTTPException(404, f"Not found: {path}")
    pad_to = 0.0
    for sib in [s for s in pad_match.split(",") if s.strip()]:
        if Path(sib).exists():
            try:
                pad_to = max(pad_to, probe(sib).duration or 0.0)
            except Exception:  # noqa: BLE001
                pass
    job_id = uuid.uuid4().hex[:12]
    dst = _proxy_path(path, seconds, pad_to, start)
    if dst.exists():
        JOBS[job_id] = {"status": "done", "percent": 100, "message": "cached",
                        "result": {"path": str(dst)}}
        return {"job_id": job_id}
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…",
                    "result": None}
    threading.Thread(target=_run_proxy_job, args=(job_id, path, seconds, pad_to, start),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/session/save")
def session_save(manifest: str = Form(...), name: str = Form("")):
    """Persist a SYNCED clip set (clip paths + per-round offsets/trims/cuts) so the user
    never has to re-upload+re-sync. Also mirrors to sessions/latest.json (always the most
    recent synced set, easy to find)."""
    try:
        data = json.loads(manifest)
    except (ValueError, TypeError):
        raise HTTPException(400, "bad manifest JSON")
    sid = data.get("id") or uuid.uuid4().hex[:8]
    data["id"] = sid
    data["name"] = (name or data.get("name") or "session").strip()[:80]
    data["saved"] = datetime.datetime.now().isoformat(timespec="seconds")
    blob = json.dumps(data, indent=2)
    (SESSIONS_DIR / f"{sid}.json").write_text(blob, encoding="utf-8")
    (SESSIONS_DIR / "latest.json").write_text(blob, encoding="utf-8")
    return {"ok": True, "id": sid, "saved": data["saved"]}


@app.get("/api/session/list")
def session_list():
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        # skip the latest mirror + sidecars (subtitle/studio/project) — only real manifests
        if (p.name in ("latest.json", "current_project.json")
                or p.name.endswith((".subs.json", ".subs.meta.json", ".studio.json"))):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or "rounds" not in d:   # not a session manifest → ignore
                continue
            out.append({"id": d.get("id", p.stem), "name": d.get("name", p.stem),
                        "saved": d.get("saved", ""), "rounds": len(d.get("rounds", []))})
        except (ValueError, OSError, AttributeError, TypeError):
            pass
    return {"sessions": out}


# ── Project = the full video you're working on. "New project" wipes the working state so a new
# video never inherits the last one's clips/sync/subtitles, and names the final file. ──
CURRENT_PROJECT = SESSIONS_DIR / "current_project.json"


def _project_name() -> str:
    if CURRENT_PROJECT.exists():
        try:
            return (json.loads(CURRENT_PROJECT.read_text(encoding="utf-8")).get("name") or "").strip()
        except (ValueError, OSError):
            pass
    return ""


def _safe_name(s, default="fightsync"):
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", (s or "")).strip().replace(" ", "_")
    return s[:60] or default


@app.get("/api/project/current")
def project_current():
    return {"name": _project_name()}


@app.post("/api/project/rename")
def project_rename(name: str = Form(...)):
    CURRENT_PROJECT.write_text(json.dumps({"name": name.strip()}), encoding="utf-8")
    return {"ok": True, "name": name.strip()}


@app.post("/api/project/new")
def project_new(name: str = Form("")):
    name = (name or "").strip() or "Untitled project"
    # archive the current working set (+ its subtitles) as a saved session so nothing is lost
    latest = SESSIONS_DIR / "latest.json"
    if latest.exists():
        try:
            d = json.loads(latest.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("rounds"):
                old = _project_name() or "project"
                sid = _safe_name(old, uuid.uuid4().hex[:8])
                d["id"], d["name"] = sid, old
                (SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(d), encoding="utf-8")
                for suf in (".subs.json", ".subs.meta.json", ".studio.json"):
                    src = SESSIONS_DIR / f"latest{suf}"
                    if src.exists():
                        (SESSIONS_DIR / f"{sid}{suf}").write_text(
                            src.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    # wipe the working state → a clean default project
    for f in ("latest.json", "latest.subs.json", "latest.subs.meta.json", "latest.studio.json"):
        try:
            (SESSIONS_DIR / f).unlink()
        except FileNotFoundError:
            pass
    CURRENT_PROJECT.write_text(json.dumps(
        {"name": name, "started": datetime.datetime.now().isoformat(timespec="seconds")}),
        encoding="utf-8")
    return {"ok": True, "name": name}


@app.post("/api/session/touch")
def session_touch():
    """Bump the latest session's saved-time so editing subtitles/VS-intro in a side editor
    registers as progress (keeps the Resume entry current)."""
    p = SESSIONS_DIR / "latest.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["saved"] = datetime.datetime.now().isoformat(timespec="seconds")
            blob = json.dumps(d, indent=2)
            p.write_text(blob, encoding="utf-8")
            sid = d.get("id")
            if sid:
                (SESSIONS_DIR / f"{sid}.json").write_text(blob, encoding="utf-8")
        except (ValueError, OSError):
            pass
    return {"ok": True}


@app.get("/api/session/get/{sid}")
def session_get(sid: str):
    p = SESSIONS_DIR / (f"{sid}.json" if sid != "latest" else "latest.json")
    if not p.exists():
        raise HTTPException(404, "session not found")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(500, "session unreadable")


# ── Subtitle editor (timeline edit + per-speaker colours) ───────────────────
def _parse_edited_subs(s):
    """JSON → per-round list of {start,end,text,speaker} (the editor's output)."""
    if not s:
        return []
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return []
    out = []
    for round_lines in (data or []):
        rl = []
        for ln in (round_lines or []):
            try:
                rl.append({"start": float(ln["start"]), "end": float(ln["end"]),
                           "text": str(ln.get("text", "")),
                           "speaker": (ln.get("speaker") or "me")})
            except (KeyError, ValueError, TypeError):
                continue
        out.append(rl)
    return out


def _subs_path(sid):
    return SESSIONS_DIR / f"{'latest' if sid in ('', 'latest') else sid}.subs.json"


def _load_subs(sid="latest"):
    p = _subs_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return []


def _session_rounds(sid="latest"):
    p = SESSIONS_DIR / (f"{sid}.json" if sid != "latest" else "latest.json")
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    rounds = []
    for i, r in enumerate(d.get("rounds") or []):
        gp, fc = r.get("gameplay"), r.get("facecam")
        if not gp or not fc or not Path(gp).exists() or not Path(fc).exists():
            continue
        cam2 = r.get("cam2")
        is_multicam = bool(cam2 and Path(cam2).exists())
        if is_multicam:
            # MATCH _build_multicam_segment EXACTLY: gameplay is anchored at the max of the cam
            # offsets (camA=webcam, camB=phone), and the facecam(webcam) starts gs-camA in.
            # The editor MUST use this same timeline or every caption is mistimed.
            cA = float(r.get("camA_offset") or 0.0)
            cB = float(r.get("camB_offset") or 0.0)
            gs = max(0.0, cA, cB)
            fs = max(0.0, gs - cA)
        else:
            off = float(r.get("offset") or 0.0)
            gs, fs = max(off, 0.0), max(-off, 0.0)
        try:
            gd = probe(gp).duration or 0.0
            fd = probe(fc).duration or 0.0
        except Exception:  # noqa: BLE001
            gd = fd = 0.0
        out_dur = max(0.0, min(gd - gs, fd - fs))
        # apply the round's composite TRIM exactly like the render does (else trimmed dead air
        # shifts every caption by the trim amount on the final).
        tr = r.get("trim") or {}
        ta = max(0.0, float(tr.get("in", 0) or 0.0))
        tb = out_dur if not tr.get("out") else min(float(tr.get("out") or out_dur), out_dur)
        ta = min(ta, max(0.0, tb - 0.3))
        if ta > 0.0 or tb < out_dur - 1e-3:
            gs += ta; fs += ta; out_dur = max(0.3, tb - ta)
        rounds.append({"round": r.get("round", i + 1), "gameplay": gp, "facecam": fc,
                       "gs": round(gs, 3), "fs": round(fs, 3), "out_dur": round(out_dur, 2)})
    return rounds


@app.get("/subtitles", response_class=HTMLResponse)
def subtitles_page():
    return _page("subtitles.html")


def _remap_subs(subs, meta, cur):
    """Shift each round's lines from the timeline they were AUTHORED on (meta[i]={gs,fs}) to the
    CURRENT timeline (cur[i]={gs,fs}), per speaker — so the editor shows captions where they'll
    actually land. me by (auth_fs−cur_fs), coach/ref by (auth_gs−cur_gs). Mirrors the render remap."""
    if not meta:
        return subs
    out = []
    for i, rnd in enumerate(subs):
        m = meta[i] if i < len(meta) else None
        c = cur[i] if i < len(cur) else None
        if not (m and c and rnd):
            out.append(rnd or [])
            continue
        try:
            dfs = float(m["fs"]) - float(c["fs"])
            dgs = float(m["gs"]) - float(c["gs"])
        except (KeyError, TypeError, ValueError):
            out.append(rnd)
            continue
        if abs(dfs) < 0.02 and abs(dgs) < 0.02:
            out.append(rnd)
            continue
        nr = []
        for ln in rnd:
            sh = dfs if ln.get("speaker") == "me" else dgs
            nl = dict(ln)
            nl["start"] = round(max(0.0, float(ln.get("start", 0.0)) + sh), 2)
            nl["end"] = round(max(0.0, float(ln.get("end", 0.0)) + sh), 2)
            nr.append(nl)
        out.append(nr)
    return out


@app.get("/api/subtitles/rounds")
def subtitles_rounds(session: str = "latest"):
    rounds = _session_rounds(session)
    # show captions on the CURRENT timeline (auto-shift if you've re-synced/trimmed since editing)
    subs = _remap_subs(_load_subs(session), _subs_meta(session), rounds)
    return {"rounds": rounds, "subs": subs}


@app.get("/api/subtitles/current")
def subtitles_current():
    return {"subs": _load_subs("latest")}


def _subs_meta_path(sid):
    return SESSIONS_DIR / f"{'latest' if sid in ('', 'latest') else sid}.subs.meta.json"


def _subs_meta(sid="latest"):
    """Per-round {gs,fs} the saved subtitles were AUTHORED against — lets the render auto-remap
    them if the sync/trim changed since (so re-syncing never silently mistimes captions)."""
    p = _subs_meta_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return []


@app.post("/api/subtitles/save")
def subtitles_save(subs: str = Form(...), session: str = Form("latest")):
    data = _parse_edited_subs(subs)
    blob = json.dumps(data)
    _subs_path(session).write_text(blob, encoding="utf-8")
    (SESSIONS_DIR / "latest.subs.json").write_text(blob, encoding="utf-8")
    # stamp the timeline these subs were authored on, so the render can re-align them later
    tl = [{"gs": r["gs"], "fs": r["fs"]} for r in _session_rounds(session)]
    meta = json.dumps(tl)
    _subs_meta_path(session).write_text(meta, encoding="utf-8")
    (SESSIONS_DIR / "latest.subs.meta.json").write_text(meta, encoding="utf-8")
    return {"ok": True, "rounds": len(data)}


def _run_subs_auto_job(job_id, gameplay, facecam, gs, fs, out_dur, coach, model):
    work = JOBS_DIR / job_id / "work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        import subtitles as sm
        _set(job_id, status="running", percent=20, message="Transcribing your speech…")
        lines = sm.transcribe_track(facecam, fs, out_dur, str(work), "me", "me", model, "en",
                                    progress=lambda m: _set(job_id, percent=35,
                                                            message=f"Your speech — {m}"))
        if coach:
            _set(job_id, percent=60, message="Transcribing the coach + ref…")
            lines += sm.transcribe_track(gameplay, gs, out_dur, str(work), "coach", "coach", model, "en",
                                         progress=lambda m: _set(job_id, percent=75,
                                                                 message=f"Coach/ref — {m}"),
                                         auto_ref=True)
        lines.sort(key=lambda x: x["start"])
        # every moment YOU spoke (VAD) — the editor flags the ones with no caption as fill-in gaps
        _set(job_id, percent=90, message="Scanning for moments you spoke…")
        speech_me = sm.speech_regions(facecam, fs, out_dur, str(work), "me")
        _set(job_id, status="done", percent=100, message=f"{len(lines)} lines found.",
             result={"lines": lines, "speech_me": speech_me})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/subtitles/auto")
def subtitles_auto(gameplay: str = Form(...), facecam: str = Form(...),
                   gs: float = Form(0.0), fs: float = Form(0.0), out_dur: float = Form(...),
                   coach: bool = Form(True), model: str = Form("base")):
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_subs_auto_job,
                     args=(job_id, gameplay, facecam, gs, fs, out_dur, coach, model),
                     daemon=True).start()
    return {"job": job_id}


# ── VS fighter-intro card ───────────────────────────────────────────────────
def _hex_rgb(s, default):
    try:
        s = (s or "").lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)) if len(s) == 6 else default
    except (ValueError, TypeError):
        return default


def _reel_dir(p):
    """A reel dir path, or None — accepts only an existing dir holding cut-out frames."""
    try:
        return str(p) if p and Path(p).is_dir() and any(Path(p).glob("*.png")) else None
    except (TypeError, OSError):
        return None


def _run_vs_intro_job(job_id: str, cfg: dict):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    out = str(job_dir / "vs_intro.mp4")
    try:
        _set(job_id, status="running", percent=15, message="Rendering cinematic VS intro…")
        import vs_intro                                  # for colour defaults + Pillow fallback
        lc = _hex_rgb(cfg.get("left_color"), vs_intro.RED)
        rc = _hex_rgb(cfg.get("right_color"), vs_intro.BLUE)
        lr, rr = _reel_dir(cfg.get("left_reel")), _reel_dir(cfg.get("right_reel"))
        _render_vs(out, cfg, lc, rc, lr, rr, 30, str(job_dir / "vs_html"))
        _set(job_id, status="done", percent=100, message="VS intro ready.",
             result={"path": out})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


def _run_round_break_job(job_id: str, cfg: dict):
    job_dir = JOBS_DIR / job_id
    work = job_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = str(job_dir / "round_break.mp4")
    try:
        _set(job_id, status="running", percent=20, message="Finding the round's best hits…")
        import round_break
        from pipeline import RenderConfig
        rc = RenderConfig()                              # defaults (1080p/30) for the preview
        src = (cfg.get("source") or "").strip()
        src = src if src and Path(src).exists() else None
        _set(job_id, percent=55, message="Building the highlight break…")
        round_break.render_round_break(out, str(work), rc,
                                       cfg.get("label", "ROUND 2"), src)
        _set(job_id, status="done", percent=100, message="Highlight break ready.",
             result={"path": out})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/round_break")
def round_break_render(config: str = Form(...)):
    """Preview the cinematic between-rounds highlight break from a source clip."""
    try:
        cfg = json.loads(config)
    except (ValueError, TypeError):
        raise HTTPException(400, "bad config")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_round_break_job, args=(job_id, cfg), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/vs_intro")
def vs_intro_render(config: str = Form(...)):
    """Generate the animated VS fighter-intro from the configured fighters/colors/photos."""
    try:
        cfg = json.loads(config)
    except (ValueError, TypeError):
        raise HTTPException(400, "bad config")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_vs_intro_job, args=(job_id, cfg), daemon=True).start()
    return {"job_id": job_id}


def _run_grab_job(job_id: str, clip: str, seconds: float, who: str = "opponent"):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    out = str(FIGHTER_DIR / ("me.png" if who == "me" else f"opponent-{job_id}.png"))
    try:
        _set(job_id, status="running", percent=10,
             message=("Scanning your footage…" if who == "me"
                      else "Scanning gameplay for the opponent…"))
        cmd = [str(FORMCOACH_PY), "grab_fighter.py", clip, out,
               str(seconds if seconds and seconds > 0 else 0)]
        with open(job_dir / "grab.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            ok = False
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=int(line.split()[1]), message="Finding the opponent…")
                    except ValueError:
                        pass
                elif line.startswith("GRABBED"):
                    ok = True
            proc.wait()
        if not ok or not Path(out).exists():
            raise RuntimeError("no clean full-body frame found (try a longer window)")
        if who == "me":                              # persist as the user's profile photo
            try:
                prof = (json.loads(FIGHTER_PROFILE.read_text(encoding="utf-8"))
                        if FIGHTER_PROFILE.exists() else {})
                prof["photo"] = out
                FIGHTER_PROFILE.write_text(json.dumps(prof, indent=2), encoding="utf-8")
            except (ValueError, OSError):
                pass
        _set(job_id, status="done", percent=100,
             message=("Captured you." if who == "me" else "Opponent captured."),
             result={"path": out})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/fighter/grab")
def fighter_grab(path: str = Form(...), seconds: float = Form(0), who: str = Form("opponent")):
    """Auto-capture a fighter's best full-body frame from a clip and cut them out —
    the opponent from gameplay, or YOU from your webcam/phone footage (who='me')."""
    if not Path(path).exists():
        raise HTTPException(400, f"Clip not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    who = "me" if who == "me" else "opponent"
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_grab_job, args=(job_id, path, seconds, who), daemon=True).start()
    return {"job_id": job_id}


RVM_DIR = Path(r"C:\Users\socia\locate-anything")     # reuse its torch-CUDA venv for matting
RVM_PY = RVM_DIR / ".venv" / "Scripts" / "python.exe"


def _rvm_matte(clip: str, job_dir, job_id) -> str:
    """Run Robust Video Matting (GPU) over the clip → a clean, temporally-stable alpha
    matte video. Returns its path, or "" if RVM isn't available / fails (grab_clips then
    falls back to MediaPipe segmentation)."""
    if not RVM_PY.exists() or not (RVM_DIR / "rvm_matte.py").exists():
        return ""
    from media import FFMPEG
    matte = str(Path(job_dir) / "matte.mp4")
    try:
        _set(job_id, percent=8, message="Matting you out cleanly on the GPU…")
        cmd = [str(RVM_PY), "rvm_matte.py", clip, matte, str(FFMPEG)]
        ok = False
        with open(Path(job_dir) / "rvm.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(RVM_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=8 + int(int(line.split()[1]) * 0.30),
                             message="Matting you out cleanly on the GPU…")
                    except ValueError:
                        pass
                elif line.startswith("MATTE "):
                    ok = True
            proc.wait()
        return matte if (ok and Path(matte).exists()) else ""
    except Exception:  # noqa: BLE001 — fall back to MediaPipe
        return ""


def _run_grab_clips_job(job_id: str, clip: str, who: str = "opponent"):
    """Build a CYCLING highlight reel (several short cut-out clips) for a corner — the
    moving alternative to a single static photo."""
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    reel_dir = FIGHTER_DIR / ("me_reel" if who == "me" else "opp_reel")
    try:
        _set(job_id, status="running", percent=8,
             message=("Scanning your footage…" if who == "me"
                      else "Scanning gameplay for the opponent…"))
        if reel_dir.exists():                        # clear stale frames so `n` is correct
            shutil.rmtree(reel_dir, ignore_errors=True)
        reel_dir.mkdir(parents=True, exist_ok=True)
        # RVM matte first (GPU): a clean, temporally-stable alpha that beats MediaPipe seg.
        matte = _rvm_matte(clip, job_dir, job_id)
        # 4 clips × 1.8s = ~7.2s of footage → cycles to fill the ~8s intro
        cmd = [str(FORMCOACH_PY), "grab_clips.py", clip, str(reel_dir), "4", "1.8", "640", "700"]
        if matte:
            cmd.append(matte)
        ok = False
        with open(job_dir / "grabclips.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=int(line.split()[1]),
                             message="Cutting out the highlight clips…")
                    except ValueError:
                        pass
                elif line.startswith("REEL"):
                    ok = True
            proc.wait()
        meta = reel_dir / "meta.json"
        if not ok or not meta.exists():
            raise RuntimeError("couldn't build a reel (no clean frames found — try a longer clip)")
        n = int(json.loads(meta.read_text(encoding="utf-8")).get("n", 0))
        if n < 1:
            raise RuntimeError("no cut-out frames were produced")
        if who == "me":                              # persist the user's reel for next time
            try:
                prof = (json.loads(FIGHTER_PROFILE.read_text(encoding="utf-8"))
                        if FIGHTER_PROFILE.exists() else {})
                prof["reel"] = str(reel_dir)
                FIGHTER_PROFILE.write_text(json.dumps(prof, indent=2), encoding="utf-8")
            except (ValueError, OSError):
                pass
        _set(job_id, status="done", percent=100,
             message=("Your highlight reel is ready." if who == "me"
                      else "Opponent reel ready."),
             result={"path": str(reel_dir), "frames": n})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/fighter/grab_clips")
def fighter_grab_clips(path: str = Form(...), who: str = Form("opponent")):
    """Auto-build a cycling highlight REEL (several cut-out clips) for a corner of the VS
    intro — the opponent from gameplay, or YOU from your webcam/phone footage (who='me')."""
    if not Path(path).exists():
        raise HTTPException(400, f"Clip not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    who = "me" if who == "me" else "opponent"
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_grab_clips_job, args=(job_id, path, who), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/fighter/photo")
def fighter_photo(file: UploadFile, cutout: int = Form(1)):
    """Upload + persist the user's own fighter photo (optionally background-removed)."""
    suffix = Path(file.filename or "me.png").suffix.lower() or ".png"
    raw = FIGHTER_DIR / f"me_raw{suffix}"
    with open(raw, "wb") as f:
        shutil.copyfileobj(file.file, f)
    out = FIGHTER_DIR / "me.png"
    if cutout and FORMCOACH_PY.exists():
        try:
            subprocess.run([str(FORMCOACH_PY), "cutout.py", str(raw), str(out)],
                           cwd=str(FORMCOACH_DIR), timeout=120, check=False)
        except (subprocess.SubprocessError, OSError):
            pass
    if not out.exists():
        shutil.copyfile(raw, out)
    return {"path": str(out)}


@app.get("/api/fighter/profile")
def fighter_profile_get():
    if FIGHTER_PROFILE.exists():
        try:
            return json.loads(FIGHTER_PROFILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


@app.post("/api/fighter/profile")
def fighter_profile_save(profile: str = Form(...)):
    """Persist the user's fighter card (name/elo/height/style/photo/colors) across videos."""
    try:
        data = json.loads(profile)
    except (ValueError, TypeError):
        raise HTTPException(400, "bad profile")
    FIGHTER_PROFILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"ok": True}


# ── reusable fighter CHARACTER SHEETS (saved corner: info + cutout, droppable on any video) ──
SHEETS_DIR = FIGHTER_DIR / "sheets"
SHEETS_DIR.mkdir(exist_ok=True)


def _slug(s, default="fighter"):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or default


@app.get("/api/fighter/sheets")
def fighter_sheets():
    """All saved character sheets, newest first."""
    out = []
    for d in sorted([p for p in SHEETS_DIR.glob("*") if p.is_dir()],
                    key=lambda p: p.stat().st_mtime, reverse=True):
        j = d / "sheet.json"
        if j.exists():
            try:
                out.append(json.loads(j.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
    return {"sheets": out}


@app.post("/api/fighter/sheet/save")
def fighter_sheet_save(name: str = Form(""), elo: str = Form(""), height: str = Form(""),
                       style: str = Form(""), color: str = Form("#e81a1e"),
                       corner: str = Form("red"), reel: str = Form(""), photo: str = Form("")):
    """Save a corner's card + cutout as a reusable character. The reel is COPIED into the
    sheet's own dir so future grabs (which overwrite me_reel/opp_reel) never disturb it.
    The display name is derived from the card info so it's recognisable in the library."""
    import shutil
    parts = [p.strip() for p in (name, elo, style) if (p or "").strip()]
    display = " · ".join(parts) or "Fighter"
    base = _slug(f"{name}-{elo}-{style}")
    sid, n = base, 2
    while (SHEETS_DIR / sid).exists():
        sid = f"{base}-{n}"; n += 1
    sdir = SHEETS_DIR / sid
    sdir.mkdir(parents=True)
    sheet = {"id": sid, "display": display, "name": name, "elo": elo, "height": height,
             "style": style, "color": color, "corner": corner, "reel": "", "photo": "",
             "created": datetime.datetime.now().isoformat(timespec="seconds")}
    rd = _reel_dir(reel)
    if rd:
        shutil.copytree(rd, sdir / "reel")
        sheet["reel"] = str(sdir / "reel")
    elif photo and Path(photo).exists():
        dst = sdir / ("photo" + (Path(photo).suffix or ".png"))
        shutil.copyfile(photo, dst)
        sheet["photo"] = str(dst)
    else:
        shutil.rmtree(sdir, ignore_errors=True)
        raise HTTPException(400, "grab a cutout (🎯) or upload a photo first")
    (sdir / "sheet.json").write_text(json.dumps(sheet, indent=2), encoding="utf-8")
    return sheet


@app.post("/api/fighter/sheet/delete")
def fighter_sheet_delete(id: str = Form(...)):
    import shutil
    d = SHEETS_DIR / _slug(id)
    if d.is_dir() and d.parent == SHEETS_DIR:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@app.post("/api/upload")
def upload(file: UploadFile, name: str = Form("facecam"),
           label: str = Form("")):
    """Stage a picked video (e.g. iPhone library footage) as a source slot,
    preserving its real extension so ffmpeg handles iPhone HEVC/.mov natively.

    Deliberately a sync (`def`) endpoint: Starlette runs it in a threadpool, so
    the large file copy never blocks the event loop — otherwise a big upload
    freezes the whole server during the copy and the 100%->done response gets
    dropped over the tunnel (the 'stuck at 100%' bug)."""
    if name not in ("gameplay", "facecam", "cam2"):
        name = "facecam"
    suffix = Path(file.filename or "").suffix.lower() or ".mp4"
    dst = REC_DIR / f"{name}-lib-{uuid.uuid4().hex[:8]}{suffix}"
    _save_upload(file, dst)
    if label.strip():
        _set_label(dst.name, label.strip())
    return {"path": str(dst), "name": name}


@app.post("/api/upload_music")
def upload_music(file: UploadFile):
    """Stage a music-bed audio track (mp3/wav/m4a/…)."""
    suffix = Path(file.filename or "").suffix.lower() or ".mp3"
    dst = REC_DIR / f"music-{uuid.uuid4().hex[:8]}{suffix}"
    _save_upload(file, dst)
    return {"path": str(dst), "name": file.filename or dst.name}


@app.post("/api/upload_recording")
def upload_recording(file: UploadFile, name: str = Form("gameplay")):
    """Accept an in-browser screen/webcam recording (webm) and stage it as a
    source. Remuxes to MKV because MediaRecorder webm often lacks the duration
    metadata ffmpeg needs for trimming/syncing; falls back to the raw file."""
    if name not in ("gameplay", "facecam"):
        name = "gameplay"
    uid = uuid.uuid4().hex[:8]
    raw = REC_DIR / f"{name}-{uid}.webm"
    _save_upload(file, raw)

    fixed = REC_DIR / f"{name}-{uid}.mkv"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-fflags", "+genpts", "-i", str(raw), "-c", "copy", str(fixed)],
            check=True,
        )
        raw.unlink(missing_ok=True)
        path = fixed
    except Exception:
        path = raw  # remux failed — use the raw recording

    return {"path": str(path), "name": name}


# ── capture: send phone/gameplay/webcam clips into the OneDrive source folders ──
@app.get("/api/capture/folders")
def capture_folders():
    """The three PC source folders (cam 1 Phone / cam 2 PC / gameplay) with their
    current video count and the next totfN name each upload would get."""
    return {"folders": capture_mod.folder_info()}


@app.post("/api/capture/upload")
def capture_upload(file: UploadFile, target: str = Form(...)):
    """Save an uploaded clip straight into a PC source folder as the next
    totfN.<ext>. Sync `def` (threadpool) so a big phone upload never blocks the
    event loop (same reason as /api/upload — avoids the 'stuck at 100%' bug)."""
    if target not in capture_mod.FOLDERS:
        raise HTTPException(400, f"unknown target: {target}")
    if not (file and file.filename):
        raise HTTPException(400, "no file")
    try:
        return capture_mod.save_totf(target, file.file, file.filename)
    except Exception as e:
        raise HTTPException(500, f"save failed: {e}")


# ── Quest: auto-import gameplay VideoShots over USB into the gameplay folder ──
@app.get("/api/quest/status")
def quest_status():
    if quest_mod is None:
        return {"available": False, "connected": False}
    s = quest_mod.status_live()
    s["available"] = True
    return s


@app.post("/api/quest/import")
def quest_import():
    """Manually kick a Quest import now (the poller also auto-imports on plug-in)."""
    if quest_mod is None:
        raise HTTPException(503, "Quest import unavailable (pywin32 not installed)")
    quest_mod.trigger_import("gameplay", log=lambda m: print("  " + str(m)))
    return {"started": True}


@app.get("/api/iphone/status")
def iphone_status():
    """iPhone plugged in? + the recent-video picker list (read via the shell namespace)."""
    if iphone_mod is None:
        return {"available": False, "connected": False, "videos": []}
    s = iphone_mod.status_live()
    s["available"] = True
    return s


def _run_iphone_import(job_id: str, names: list):
    try:
        _set(job_id, status="running", percent=8, message=f"Importing {len(names)} video(s) from iPhone…")
        done = iphone_mod.import_selected(names, log=lambda m: print("  " + str(m)))
        _set(job_id, status="done", percent=100,
             message=f"Imported {len(done)} clip(s) to the Phone folder.", result={"imported": done})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/iphone/import")
def iphone_import(names: str = Form(...)):
    """Import the picked iPhone videos into the Phone folder (background job → poll /api/status)."""
    if iphone_mod is None:
        raise HTTPException(503, "iPhone import unavailable (pywin32 not installed)")
    try:
        sel = json.loads(names)
    except ValueError:
        sel = [n for n in names.splitlines() if n.strip()]
    if not sel:
        raise HTTPException(400, "No videos selected.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_iphone_import, args=(job_id, sel), daemon=True).start()
    return {"job_id": job_id}


_WAVEFORM_CACHE: dict[str, dict] = {}


@app.get("/api/waveform")
def waveform(path: str):
    """Per-bucket audio peaks for the visual sync aligner. Cached by path+mtime
    so each clip is decoded only once."""
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, "Clip not found")
    try:
        key = f"{path}:{p.stat().st_mtime_ns}"
    except OSError:
        key = path
    cached = _WAVEFORM_CACHE.get(key)
    if cached is None:
        cached = waveform_peaks(path)
        _WAVEFORM_CACHE[key] = cached
    return cached


@app.get("/api/library")
def library():
    """List previously-uploaded / imported / recorded source clips so they can be
    re-used from a dropdown instead of re-uploading."""
    from datetime import datetime as _dt
    exts = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
    names = _load_labels()
    items = []
    for f in REC_DIR.iterdir():
        if f.is_dir() or f.suffix.lower() not in exts:
            continue
        n = f.name.lower()
        kind = ("cam2" if n.startswith("cam2")
                else "facecam" if n.startswith("facecam")
                else "gameplay" if n.startswith("gameplay") else "")
        custom = names.get(f.name, "").strip()
        if custom:
            src = custom
        elif "lib" in n:
            src = "Library upload"
        elif "meta" in n:
            src = "Meta clip"
        elif n.startswith(("gameplay-", "facecam-")):
            src = "Recording"
        else:
            src = f.stem[:24]
        try:
            st = f.stat()
            when = _dt.fromtimestamp(st.st_mtime).strftime("%b %d, %I:%M %p")
            label = f"{src} · {st.st_size / 1e6:.0f} MB · {when}"
            items.append({"path": str(f), "name": f.name, "kind": kind,
                          "title": custom or src,
                          "size": st.st_size, "mtime": st.st_mtime, "label": label})
        except OSError:
            continue
    items.sort(key=lambda x: -x["mtime"])
    return {"items": items}


@app.post("/api/rename")
def rename_clip(path: str = Form(...), label: str = Form(...)):
    """Persist a user-given name for a saved clip so it survives reloads and
    shows in the saved-uploads dropdown."""
    p = Path(path)
    if not p.exists() or p.parent != REC_DIR:
        raise HTTPException(404, "Clip not found")
    _set_label(p.name, label.strip())
    return {"ok": True, "name": p.name, "label": label.strip()}


@app.post("/api/library/delete")
def library_delete(path: str = Form(...)):
    """Remove a saved clip from the library dropdown. Safer than a hard delete:
    the file is MOVED into recordings/archive (which the library doesn't scan),
    so it leaves the dropdown but is still recoverable on disk."""
    p = Path(path)
    if p.parent != REC_DIR:
        raise HTTPException(400, "Clip not in recordings")
    if not p.exists():
        return {"ok": True}                      # already gone — nothing to do
    arch = REC_DIR / "archive"
    arch.mkdir(exist_ok=True)
    dest = arch / p.name
    if dest.exists():
        dest = arch / f"{p.stem}-{uuid.uuid4().hex[:6]}{p.suffix}"
    try:
        shutil.move(str(p), str(dest))
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return {"ok": True, "archived": dest.name}


# ── channels + auto-capture ─────────────────────────────────────────────────
@app.get("/api/channels")
def list_channels():
    return {"channels": ch_mod.load()}


@app.post("/api/channels")
def add_channel(url: str = Form(...)):
    if not url.strip():
        raise HTTPException(400, "Empty channel URL")
    return ch_mod.add(url)


@app.delete("/api/channels/{cid}")
def del_channel(cid: str):
    ch_mod.remove(cid)
    return {"ok": True}


@app.get("/api/channels/{cid}/live")
def channel_live(cid: str):
    c = ch_mod.get(cid)
    if not c:
        raise HTTPException(404, "Unknown channel")
    res = ch_mod.check_live(c["url"])
    return {**res, "channel_id": cid, "has_crop": bool(c.get("crop"))}


@app.post("/api/channels/{cid}/crop")
def save_channel_crop(cid: str, x: int = Form(...), y: int = Form(...),
                      w: int = Form(...), h: int = Form(...),
                      frame_w: int = Form(...), frame_h: int = Form(...)):
    crop = {"x": x, "y": y, "w": w, "h": h,
            "frame_w": frame_w, "frame_h": frame_h}
    ch_mod.set_crop(cid, crop)
    return {"ok": True, "crop": crop}


@app.post("/api/detect_crop")
def detect_crop_ep(path: str = Form(...), channel_id: str = Form("")):
    if not Path(path).exists():
        raise HTTPException(400, f"Recording not found: {path}")
    crop = None
    if channel_id:
        c = ch_mod.get(channel_id)
        if c and c.get("crop"):
            crop = {**c["crop"], "auto": False}
    if crop is None:
        crop = detect_crop(path, str(REC_DIR))

    uid = uuid.uuid4().hex[:8]
    prev = PREV_DIR / f"{uid}.png"
    info = probe(path)
    t = (info.duration or 2) * 0.5
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", path,
         "-frames:v", "1", "-vf", "scale=640:-2", str(prev)],
        check=True,
    )
    return {"crop": crop, "preview": f"/api/preview/{uid}.png",
            "frame_w": crop["frame_w"], "frame_h": crop["frame_h"]}


@app.get("/api/preview/{fname}")
def get_preview(fname: str):
    p = PREV_DIR / fname
    if not p.exists():
        raise HTTPException(404, "No preview")
    return FileResponse(p, media_type="image/png")


@app.post("/api/apply_crop")
def apply_crop_ep(path: str = Form(...), x: int = Form(...), y: int = Form(...),
                  w: int = Form(...), h: int = Form(...)):
    if not Path(path).exists():
        raise HTTPException(400, f"Recording not found: {path}")
    out = REC_DIR / f"gameplay-cropped-{uuid.uuid4().hex[:8]}.mp4"
    apply_crop(path, {"x": x, "y": y, "w": w, "h": h},
               str(out), _enc(20, "veryfast"))
    return {"path": str(out)}


# ── form studio (annotation editor) ─────────────────────────────────────────
STUDIO_PROJECT = ROOT / "studio_project.json"


@app.get("/studio", response_class=HTMLResponse)
def studio_page():
    return _page("studio.html")


@app.get("/cleanup", response_class=HTMLResponse)
def cleanup_page():
    return _page("cleanup.html")


@app.get("/label", response_class=HTMLResponse)
def label_page():
    return _page("label.html")


def _labels_path(clip: str) -> Path:
    d = SESSIONS_DIR / "labels"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(clip.encode("utf-8")).hexdigest()[:14] + ".json")


@app.get("/api/labels")
def labels_get(clip: str):
    p = _labels_path(clip)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"clip": clip, "labels": []}


@app.post("/api/labels/save")
def labels_save(clip: str = Form(...), labels_json: str = Form(...)):
    """Ground-truth punch labels for a clip: [{t,type,hand}] — used to tune/train the punch detector."""
    try:
        labels = json.loads(labels_json)
        assert isinstance(labels, list)
    except (ValueError, AssertionError):
        raise HTTPException(400, "labels_json must be a JSON array")
    p = _labels_path(clip)
    p.write_text(json.dumps({"clip": clip, "labels": labels,
                             "saved": datetime.datetime.now().isoformat(timespec="seconds")}, indent=1),
                 encoding="utf-8")
    return {"ok": True, "count": len(labels)}


def _cleanup_reel_dir(reel: str) -> Path:
    return FIGHTER_DIR / ("me_reel" if reel == "me" else "opp_reel")


@app.get("/api/cleanup/reels")
def cleanup_reels():
    """Each available reel: frame count, frame size, and the dir (for /api/media frame URLs)."""
    out = []
    for key, label in (("me", "You (red corner)"), ("opp", "Opponent (blue corner)")):
        d = _cleanup_reel_dir(key)
        frames = sorted(d.glob("[0-9]*.png")) if d.is_dir() else []
        if not frames:
            continue
        meta = {}
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            pass
        out.append({"reel": key, "label": label, "dir": str(d), "n": len(frames),
                    "pw": meta.get("pw"), "ph": meta.get("ph"),
                    "frames": [str(p) for p in frames]})
    return {"reels": out}


@app.post("/api/cleanup/apply")
def cleanup_apply(reel: str = Form(...), tighten: int = Form(0), choke: int = Form(0),
                  feather: int = Form(0), mask: Optional[UploadFile] = None):
    """Re-derive the reel frames from the pristine backup with the cleanup applied."""
    import cleanup_reel
    d = _cleanup_reel_dir(reel)
    if not d.is_dir():
        raise HTTPException(404, "reel not found")
    mask_path = None
    if mask is not None:
        mask_path = str(d / "_cleanup_mask.png")
        with open(mask_path, "wb") as f:
            f.write(mask.file.read())
    n = cleanup_reel.apply(str(d), tighten=tighten, choke=choke, feather=feather, mask_path=mask_path)
    return {"ok": True, "frames": n}


@app.post("/api/cleanup/reset")
def cleanup_reset(reel: str = Form(...)):
    import cleanup_reel
    d = _cleanup_reel_dir(reel)
    if not d.is_dir():
        raise HTTPException(404, "reel not found")
    return {"ok": True, "frames": cleanup_reel.reset(str(d))}


@app.get("/api/media")
def media(path: str, request: Request):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"Not found: {path}")
    size = p.stat().st_size
    ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(str(p), media_type=ctype,
                            headers={"Accept-Ranges": "bytes"})

    # serve a byte range (206) so the browser can seek/scrub the video
    m = re.match(r"bytes=(\d+)-(\d*)", rng)
    start = int(m.group(1)) if m else 0
    end = int(m.group(2)) if (m and m.group(2)) else size - 1
    end = min(end, size - 1)
    length = max(0, end - start + 1)

    def stream():
        with open(p, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(stream(), status_code=206, headers=headers,
                             media_type=ctype)


@app.post("/api/studio/track")
def studio_track(path: str = Form(...), start: float = Form(...),
                 dur: float = Form(...), nx: float = Form(...),
                 ny: float = Form(...), nw: float = Form(...),
                 nh: float = Form(...)):
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    track = track_region(path, start, dur, (nx, ny, nw, nh))
    return {"track": track}


@app.post("/api/studio/project")
def studio_save(path: str = Form(...), annotations: str = Form(...)):
    STUDIO_PROJECT.write_text(
        json.dumps({"path": path, "annotations": json.loads(annotations)}, indent=2),
        encoding="utf-8")
    return {"ok": True}


@app.get("/api/studio/project")
def studio_load():
    if STUDIO_PROJECT.exists():
        return json.loads(STUDIO_PROJECT.read_text(encoding="utf-8"))
    return {"path": "", "annotations": []}


def _run_studio_job(job_id: str, base: str, anns: list):
    out_dir = JOBS_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = str(out_dir / "annotated.mp4")
    try:
        _set(job_id, status="running", message="Rendering overlays…")
        render_overlay(base, anns, out, _enc(20, "medium"),
                       on_progress=lambda p: _set(job_id, percent=p,
                                                  message=f"Rendering overlays… {p}%"))
        _set(job_id, status="done", percent=100, message="Done.",
             result={"final": out})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e),
             traceback=traceback.format_exc())


# ── pose / form-coach tracking (accurate body tracking, replaces CSRT shapes) ──
FORMCOACH_DIR = Path(r"C:\Users\socia\form-coach")
FORMCOACH_PY = FORMCOACH_DIR / ".venv" / "Scripts" / "python.exe"


def _run_pose_job(job_id: str, src: str, seconds: float = 0, quality: str = "lite",
                  highlights: bool = True, stance: str = "auto"):
    out_dir = JOBS_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = str(out_dir / "pose_raw.mp4")
    out = str(out_dir / "form_coach.mp4")
    try:
        _set(job_id, status="running", percent=5,
             message="Tracking your body + form cues…")
        # args: in, out, max_seconds (0=all), quality (lite|full), highlights (1|0),
        #       stance (auto|orthodox|southpaw)
        cmd = [str(FORMCOACH_PY), "pose_coach.py", src, raw,
               str(seconds if seconds and seconds > 0 else 0), quality,
               "1" if highlights else "0", stance]
        # PROGRESS/summary come on stdout; MediaPipe's chatty logs (incl. harmless
        # "clearcut" telemetry spam) go to a log FILE so they never reach the user.
        log_path = out_dir / "pose.log"
        summary = ""
        with open(log_path, "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        p = int(line.split()[1])
                        _set(job_id, percent=5 + int(p * 0.78),
                             message=f"Tracking your body + form cues… {p}%")
                    except ValueError:
                        pass
                elif line.startswith("frames="):
                    summary = line
            proc.wait()
        if proc.returncode != 0 or not Path(raw).exists():
            noise = ("clearcut", "Source Location Trace", "GL version", "gl_context",
                     "feedback tensor", "landmark_projection", "TensorFlow", "Using NORM_RECT")
            errtxt = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            real = [ln for ln in errtxt.splitlines() if ln.strip() and not any(n in ln for n in noise)]
            raise RuntimeError("pose engine failed:\n" + "\n".join(real[-10:] or ["(see pose.log)"]))
        _set(job_id, percent=85, message="Finalizing video…")
        # remux to a browser-playable H.264 file + the clip's original audio
        run_ffmpeg(["-i", raw, "-i", src, "-map", "0:v:0", "-map", "1:a:0?",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
                    "-c:a", "aac", "-movflags", "+faststart", out])
        _set(job_id, status="done", percent=100, message="Done.",
             result={"final": out, "summary": summary})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/pose")
def studio_pose(path: str = Form(...), seconds: float = Form(0),
                quality: str = Form("lite"), highlights: int = Form(1),
                stance: str = Form("auto")):
    """Accurate body tracking + form cues (MediaPipe pose) on a clip — the reliable
    replacement for the CSRT shape tracker. Runs the form-coach engine in its own
    venv so FightSync's deps are untouched. `seconds`>0 analyses only the first N s.
    `highlights`=1 inserts the freeze + green/red body-part showcase on key moments."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0,
                    "message": "Queued…", "result": None}
    stance = stance if stance in ("auto", "orthodox", "southpaw") else "auto"
    threading.Thread(target=_run_pose_job,
                     args=(job_id, path, seconds, quality, bool(highlights), stance),
                     daemon=True).start()
    return {"job_id": job_id}


# ── GPU body tracking (SOTA YOLO11-pose on the RTX 2080 — far smoother/more accurate) ──
def _run_gpu_pose_job(job_id: str, src: str, seconds: float = 0, model: str = "yolo11m-pose.pt",
                      cues: bool = True, start: float = 0.0, kpts_b: str = "", off_b: float = 0.0):
    out_dir = JOBS_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = str(out_dir / "gpu_pose.mp4")
    try:
        _set(job_id, status="running", percent=4, message="GPU tracking your body (RTX 2080)…",
             preview=out + ".preview.jpg")
        # args: …, smooth, kpts_in(- = none), cues(1|0), start, kpts_b(2nd cam = multi-view), off_b
        cmd = [str(RVM_PY), "gpu_pose.py", src, out, str(FFMPEG),
               str(seconds if seconds and seconds > 0 else 0), model, "1", "-",
               "1" if cues else "0", str(round(float(start or 0.0), 2)),
               (kpts_b or "-"), str(round(float(off_b or 0.0), 2))]
        log_path = out_dir / "gpu_pose.log"
        with open(log_path, "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(RVM_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        p = int(line.split()[1])
                        _set(job_id, percent=4 + int(p * 0.92),
                             message=f"GPU tracking your body… {p}%")
                    except ValueError:
                        pass
            proc.wait()
        if proc.returncode != 0 or not Path(out).exists():
            errtxt = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            real = [ln for ln in errtxt.splitlines() if ln.strip()][-8:]
            raise RuntimeError("GPU pose failed:\n" + "\n".join(real or ["(see gpu_pose.log)"]))
        _set(job_id, status="done", percent=100, message="Done.",
             result={"final": out, "kpts": out + ".kpts.json"})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/gpu_pose")
def studio_gpu_pose(path: str = Form(...), seconds: float = Form(0),
                    quality: str = Form("balanced"), cues: int = Form(1), start: float = Form(0)):
    """SOTA GPU body tracking (YOLO11-pose on the RTX 2080) — much smoother + joint-accurate than
    the CPU MediaPipe path. `cues`=1 adds freeze-frame good/bad-form showcases on real punches."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if not RVM_PY.exists() or not (RVM_DIR / "gpu_pose.py").exists():
        raise HTTPException(500, "GPU tracker isn't set up (locate-anything venv / gpu_pose.py missing).")
    model = {"fast": "yolo11n-pose.pt", "balanced": "yolo11m-pose.pt",
             "accurate": "yolo11x-pose.pt"}.get(quality, "yolo11m-pose.pt")
    if quality == "mine":
        bp = FINETUNE_DIR / "best_pose.pt"
        model = str(bp) if bp.exists() else "yolo11m-pose.pt"
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_gpu_pose_job,
                     args=(job_id, path, seconds, model, bool(cues), float(start or 0)),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_multiview_pose_job(job_id: str, round_num: int, model: str, cues: bool):
    """MULTI-VIEW punch typing: pose the phone angle (cam B) first, then pose the webcam (cam A)
    WITH cam B's synced keypoints so the learned classifier fuses BOTH angles at each punch —
    the clearer view decides, so a hook that's foreshortened head-on is caught from the side."""
    try:
        _set(job_id, status="running", percent=2, message="Reading the synced cams…")
        saved = json.loads((SESSIONS_DIR / "latest.json").read_text(encoding="utf-8"))
        rounds = saved.get("rounds") or []
        r = rounds[round_num] if 0 <= round_num < len(rounds) else {}
        cam_a, cam_b = r.get("facecam"), r.get("cam2")
        if not (cam_a and Path(cam_a).exists()):
            raise RuntimeError("No webcam (cam A) found for this round.")
        if not (cam_b and Path(cam_b).exists()):
            raise RuntimeError("This round has no phone cam (cam B) — multi-view needs both angles.")
        # offset convention = gameplayTime − camTime, so camB_time = camA_time + (offA − offB)
        off_b = float(r.get("camA_offset") or 0.0) - float(r.get("camB_offset") or 0.0)
        # 1) pose cam B → its kpts.json (no overlay/cues needed; we only want its keypoints)
        _set(job_id, percent=6, message="Tracking the phone angle (cam B)…")
        b_dir = JOBS_DIR / job_id / "camb"; b_dir.mkdir(parents=True, exist_ok=True)
        b_out = str(b_dir / "camb.mp4")
        subprocess.run([str(RVM_PY), "gpu_pose.py", cam_b, b_out, str(FFMPEG), "0", model, "1", "-", "0", "0"],
                       cwd=str(RVM_DIR), capture_output=True, text=True)
        kpts_b = b_out + ".kpts.json"
        if not Path(kpts_b).exists():
            kpts_b = ""                              # cam B pose failed → degrade to single view
        # 2) pose cam A WITH cam B's keypoints → fused jab/hook/uppercut
        _run_gpu_pose_job(job_id, cam_a, 0, model, bool(cues), 0.0, kpts_b, off_b)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/multiview_pose")
def studio_multiview_pose(round: int = Form(...), quality: str = Form("balanced"), cues: int = Form(1)):
    """Two-angle GPU tracking: fuses webcam + phone at each punch for far better jab/hook/uppercut
    accuracy (sidesteps the single-camera depth ambiguity). Needs a round with BOTH cams synced."""
    if not RVM_PY.exists() or not (RVM_DIR / "gpu_pose.py").exists():
        raise HTTPException(500, "GPU tracker isn't set up (locate-anything venv / gpu_pose.py missing).")
    model = {"fast": "yolo11n-pose.pt", "balanced": "yolo11m-pose.pt",
             "accurate": "yolo11x-pose.pt"}.get(quality, "yolo11m-pose.pt")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_multiview_pose_job, args=(job_id, int(round), model, bool(cues)),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_directed_pose_job(job_id: str, round_num: int, model: str, cues: bool):
    """Build the SYNCED + DIRECTED feed for a round (full-screen cut between webcam + phone at the
    director's switch points, on the trimmed timeline) and GPU-track THAT — so the skeleton follows
    whichever camera the final edit shows. Reuses export_director_cut + the normal GPU pose job."""
    out_dir = JOBS_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    feed = str(out_dir / "directed_feed.mp4")
    work = str(JOBS_DIR / job_id / "feedwork")
    try:
        _set(job_id, status="running", percent=3, message="Building the directed feed (webcam + phone cut)…")
        d = json.loads((SESSIONS_DIR / "latest.json").read_text(encoding="utf-8"))
        rd = None
        for i, r in enumerate(d.get("rounds") or []):
            if isinstance(r, dict) and r.get("round", i + 1) == round_num:
                rd = r
                break
        if rd is None:
            raise RuntimeError(f"round {round_num} isn't in the current session")
        cam_a, cam_b = rd.get("facecam"), rd.get("cam2")
        if not cam_a or not Path(cam_a).exists():
            raise RuntimeError("this round has no webcam clip")
        if not cam_b or not Path(cam_b).exists():
            raise RuntimeError("the directed view needs BOTH the webcam and the phone cam")
        off_a = float(rd.get("camA_offset") or 0.0)
        off_b = float(rd.get("camB_offset") or 0.0)
        gs = max(0.0, off_a, off_b)
        fs_a = max(0.0, gs - off_a)
        gp = rd.get("gameplay")
        gd = probe(gp).duration if (gp and Path(gp).exists()) else 1e9
        out_dur = max(0.3, min(gd - gs, probe(cam_a).duration - fs_a))
        tr = rd.get("trim") or {}
        t_in = float(tr.get("in") or 0.0)
        t_out = min(float(tr["out"]) if tr.get("out") is not None else out_dur, out_dur)
        # stored cuts are TRIMMED-COMPOSITE time → export_director_cut wants GAMEPLAY time
        gcuts = [{"t": gs + t_in + float(c.get("t") or 0.0), "cam": int(c.get("cam") or 0)}
                 for c in (rd.get("cuts") or [])]
        import pipeline
        pipeline.export_director_cut(cam_a, cam_b, off_a, off_b, gcuts, feed, work)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", percent=100, message=f"directed feed failed: {str(e)[:200]}")
        return
    # GPU-track ONLY the kept window [t_in, t_out] of the directed feed
    _run_gpu_pose_job(job_id, feed, max(0.3, t_out - t_in), model, bool(cues), float(t_in))


@app.post("/api/studio/directed_pose")
def studio_directed_pose(round: int = Form(...), quality: str = Form("balanced"), cues: int = Form(1)):
    """GPU-track the synced + directed view (webcam ⇄ phone cut) for a round — its own dropdown option."""
    if not RVM_PY.exists() or not (RVM_DIR / "gpu_pose.py").exists():
        raise HTTPException(500, "GPU tracker isn't set up (locate-anything venv / gpu_pose.py missing).")
    model = {"fast": "yolo11n-pose.pt", "balanced": "yolo11m-pose.pt",
             "accurate": "yolo11x-pose.pt"}.get(quality, "yolo11m-pose.pt")
    if quality == "mine":
        bp = FINETUNE_DIR / "best_pose.pt"
        model = str(bp) if bp.exists() else "yolo11m-pose.pt"
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_directed_pose_job, args=(job_id, int(round), model, bool(cues)),
                     daemon=True).start()
    return {"job_id": job_id}


# ── SPECTATOR view: pull a round's section out of a long VOD (URL via yt-dlp, or a local file) ──
def _parse_ts(s: str) -> float:
    """'1:23:45', '1:23:45.5', '83:45', '85.5', or bare seconds → float seconds."""
    s = str(s).strip()
    if not s:
        return 0.0
    try:
        sec = 0.0
        for part in s.split(":"):
            sec = sec * 60 + float(part)
        return sec
    except ValueError:
        return 0.0


def _run_spectator_extract_job(job_id: str, src: str, is_url: bool, start: str, end: str, round_num: int):
    try:
        a, b = _parse_ts(start), _parse_ts(end)
        if b <= a + 0.2:
            raise RuntimeError("the end time must be after the start time")
        out = str(REC_DIR / f"spectator-r{round_num}-{uuid.uuid4().hex[:8]}.mp4")
        if is_url:
            # yt-dlp's first cold request to YouTube often gets throttled/403'd while it warms up the
            # player token → auto-retry a few times (the 2nd try usually sails through) + yt-dlp's own
            # network retries, so the user never sees the flaky first attempt.
            cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist",
                   "--download-sections", f"*{a:.2f}-{b:.2f}", "--force-keyframes-at-cuts",
                   "-f", "mp4/bestvideo*+bestaudio/best", "--ffmpeg-location", str(FFMPEG),
                   "--retries", "10", "--fragment-retries", "10", "--extractor-retries", "3",
                   "-o", out, src]
            ok, last = False, ""
            for attempt in range(3):
                _set(job_id, status="running", percent=10 + attempt * 6,
                     message=(f"Downloading {a:.0f}s–{b:.0f}s from the VOD…" if attempt == 0
                              else f"First try hiccuped — retrying ({attempt + 1}/3)…"))
                try:
                    Path(out).unlink(missing_ok=True)        # clear any partial from a failed attempt
                except OSError:
                    pass
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
                if r.returncode == 0 and Path(out).exists():
                    ok = True
                    break
                last = (r.stderr or r.stdout or "")[-400:]
            if not ok:
                raise RuntimeError("download failed after 3 tries — private/DRM link? Use a file instead.\n" + last)
        else:
            if not Path(src).exists():
                raise RuntimeError(f"file not found: {src}")
            _set(job_id, status="running", percent=10, message=f"Trimming {a:.0f}s–{b:.0f}s out of the file…")
            cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", f"{a:.2f}", "-i", src, "-t", f"{b - a:.2f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-c:a", "aac", "-movflags", "+faststart", out]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
            if r.returncode != 0 or not Path(out).exists():
                raise RuntimeError("trim failed:\n" + (r.stderr or "")[-400:])
        try:
            dur = probe(out).duration or 0.0
        except Exception:  # noqa: BLE001
            dur = 0.0
        _set(job_id, status="done", percent=100, message=f"Got a {dur:.0f}s spectator clip.",
             result={"clip": out, "duration": round(dur, 2)})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", percent=100, message=str(e)[:400])


_VOD_CREDIT_CACHE: dict = {}


def _vod_credit(url: str) -> str:
    """A friendly source-credit block (VOD + channel links) for a spectator VOD URL — channel name/url +
    title pulled via yt-dlp (cached; graceful if it fails). '' if not a URL or the lookup fails."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    if url in _VOD_CREDIT_CACHE:
        return _VOD_CREDIT_CACHE[url]
    channel = uploader_url = channel_url = title = ""
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--skip-download", "--no-warnings",
                            "--print", "%(channel)s", "--print", "%(uploader_url)s",
                            "--print", "%(channel_url)s", "--print", "%(title)s", url],
                           capture_output=True, text=True, timeout=90)
        ln = [("" if x.strip() == "NA" else x.strip()) for x in (r.stdout or "").splitlines()]
        channel = ln[0] if len(ln) > 0 else ""
        uploader_url = ln[1] if len(ln) > 1 else ""
        channel_url = ln[2] if len(ln) > 2 else ""
        title = ln[3] if len(ln) > 3 else ""
    except Exception:  # noqa: BLE001
        pass
    if not channel:
        _VOD_CREDIT_CACHE[url] = ""
        return ""
    chan_link = uploader_url or channel_url or url
    title_part = f" — {title}" if title else ""
    credit = (f"🥊 This bout was part of a live event hosted by {channel} — big shoutout to them for "
              f"putting on such a great show.\n\n"
              f"▶ Full event VOD{title_part}: {url}\n"
              f"📺 {channel} (tournaments, live streams & more): {chan_link}\n\n"
              f"If you're into competitive VR boxing, do yourself a favor and go give them a follow — "
              f"they run some of the best Thrill of the Fight action out there.")
    _VOD_CREDIT_CACHE[url] = credit
    return credit


@app.post("/api/spectator/extract")
def spectator_extract(src: str = Form(...), is_url: int = Form(0),
                      start: str = Form("0"), end: str = Form(...), round: int = Form(1)):
    """Cut one round's section [start,end] out of a spectator VOD (URL→yt-dlp section download, or a
    local file→ffmpeg trim) → a small clip the user then syncs + drops in as the opposite-corner PiP."""
    src = str(src).strip()
    if not src:
        raise HTTPException(400, "give a file path or a URL")
    # auto-detect a link even if the File/URL toggle was left on "file"
    as_url = bool(is_url) or src.lower().startswith(("http://", "https://", "www."))
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_spectator_extract_job,
                     args=(job_id, src, as_url, start, end, int(round)),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_gpu_redraw_job(job_id: str, src: str, kpts_path: str):
    out_dir = JOBS_DIR / job_id / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = str(out_dir / "gpu_corrected.mp4")
    try:
        _set(job_id, status="running", percent=8, message="Baking your joint fixes…")
        cmd = [str(RVM_PY), "gpu_pose.py", src, out, str(FFMPEG), "0", "yolo11m-pose.pt", "1", kpts_path]
        log_path = out_dir / "gpu_redraw.log"
        with open(log_path, "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(RVM_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=8 + int(int(line.split()[1]) * 0.9),
                             message=f"Baking your joint fixes… {line.split()[1]}%")
                    except ValueError:
                        pass
            proc.wait()
        if proc.returncode != 0 or not Path(out).exists():
            raise RuntimeError("redraw failed (see gpu_redraw.log)")
        _set(job_id, status="done", percent=100, message="Done.",
             result={"final": out, "kpts": out + ".kpts.json"})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


FINETUNE_DIR = RVM_DIR / "finetune_data"   # corrected frames accumulate here as YOLO-pose labels


def _export_finetune_data(src: str, corrected: dict, corr_idx: list) -> int:
    """Save the user-CORRECTED frames as YOLO-pose training data (image + label). These are the
    only frames with ground-truth fixes, so they're the signal for fine-tuning. Reads the clip
    SEQUENTIALLY (frame-accurate) — never seeks."""
    import cv2
    frames = corrected.get("frames") or []
    W = corrected.get("w") or 1
    H = corrected.get("h") or 1
    idxset = {int(i) for i in (corr_idx or [])}
    if not idxset:
        return 0
    (FINETUNE_DIR / "images").mkdir(parents=True, exist_ok=True)
    (FINETUNE_DIR / "labels").mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(src)
    stem = Path(src).stem
    f = 0
    saved = 0
    maxi = max(idxset)
    while f <= maxi:
        ok, frame = cap.read()
        if not ok:
            break
        if f in idxset and f < len(frames) and frames[f]:
            kp = frames[f]
            vis = [p for p in kp if p[2] > 0.2]
            if vis:
                xs = [p[0] for p in vis]; ys = [p[1] for p in vis]
                cx = ((min(xs) + max(xs)) / 2) / W; cy = ((min(ys) + max(ys)) / 2) / H
                bw = min(1.0, (max(xs) - min(xs)) / W * 1.3); bh = min(1.0, (max(ys) - min(ys)) / H * 1.3)
                lab = f"0 {cx:.5f} {cy:.5f} {bw:.5f} {bh:.5f} " + " ".join(
                    f"{p[0] / W:.5f} {p[1] / H:.5f} {2 if p[2] > 0.2 else 0}" for p in kp)
                cv2.imwrite(str(FINETUNE_DIR / "images" / f"{stem}_{f:06d}.jpg"), frame)
                (FINETUNE_DIR / "labels" / f"{stem}_{f:06d}.txt").write_text(lab)
                saved += 1
        f += 1
    cap.release()
    return saved


@app.post("/api/studio/gpu_redraw")
def studio_gpu_redraw(path: str = Form(...), corr_frames: str = Form("[]"),
                      kpts: UploadFile = None):
    """Re-render the GPU skeleton from user-CORRECTED keypoints (the 'Fix joints' editor) — no GPU
    inference, just redraws + recomputes stats. Also banks the corrected frames as training labels."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if kpts is None:
        raise HTTPException(400, "no corrected keypoints provided")
    job_id = uuid.uuid4().hex[:12]
    kp_path = str(JOBS_DIR / f"{job_id}_corr.kpts.json")
    blob = kpts.file.read()
    with open(kp_path, "wb") as f:
        f.write(blob)
    # bank the user's fixes as fine-tune training data (best effort, never blocks the re-render)
    try:
        corr = json.loads(corr_frames) if corr_frames.strip() else []
        if corr:
            _export_finetune_data(path, json.loads(blob.decode("utf-8")), corr)
    except Exception:  # noqa: BLE001
        pass
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_gpu_redraw_job, args=(job_id, path, kp_path), daemon=True).start()
    return {"job_id": job_id}


def _run_gpu_finetune_job(job_id: str):
    out_dir = JOBS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _set(job_id, status="running", percent=5,
             message="Fine-tuning on your fixes (this can take a while)…")
        cmd = [str(RVM_PY), "gpu_finetune.py", str(FINETUNE_DIR), "yolo11m-pose.pt", "40"]
        log_path = out_dir / "finetune.log"
        with open(log_path, "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(RVM_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS"):
                    _set(job_id, percent=30, message=line[8:].strip()[:120])
            proc.wait()
        best = FINETUNE_DIR / "best_pose.pt"
        if proc.returncode != 0 or not best.exists():
            errtxt = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            real = [ln for ln in errtxt.splitlines() if ln.strip()][-6:]
            raise RuntimeError("fine-tune failed:\n" + "\n".join(real or ["(see finetune.log)"]))
        _set(job_id, status="done", percent=100,
             message="Done — your fine-tuned model is ready (pick 'my model' in GPU tracking).",
             result={"model": str(best)})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.get("/api/studio/finetune_status")
def finetune_status():
    img_dir = FINETUNE_DIR / "images"
    frames = len(list(img_dir.glob("*.jpg"))) if img_dir.exists() else 0
    return {"frames": frames, "has_model": (FINETUNE_DIR / "best_pose.pt").exists()}


@app.post("/api/studio/gpu_finetune")
def studio_gpu_finetune():
    img_dir = FINETUNE_DIR / "images"
    n = len(list(img_dir.glob("*.jpg"))) if img_dir.exists() else 0
    if n < 12:
        raise HTTPException(400, f"Only {n} corrected frames banked — fix joints on more frames "
                                 f"first (need ~12+ for a fine-tune).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_gpu_finetune_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id}


# ── Form Studio: load/save by round (dropdown instead of pasting a path) ──
STUDIO_PROGRESS = SESSIONS_DIR / "latest.studio.json"


def _studio_progress() -> dict:
    if STUDIO_PROGRESS.exists():
        try:
            return json.loads(STUDIO_PROGRESS.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


@app.get("/api/studio/rounds")
def studio_round_list():
    """Rounds from the latest synced session — so Form Studio picks a clip from a DROPDOWN, not a
    pasted path. Also returns which rounds have saved Studio progress (a tracked result to reopen)."""
    geo = {g["round"]: g for g in _session_rounds("latest")}   # trimmed gs/fs/out_dur per round
    p = SESSIONS_DIR / "latest.json"
    rounds = []
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            d = {}
        for i, r in enumerate(d.get("rounds") or []):
            if not isinstance(r, dict):
                continue
            rn = r.get("round", i + 1)
            g = geo.get(rn, {})
            rounds.append({"round": rn,
                           "gameplay": r.get("gameplay") or "",
                           "facecam": r.get("facecam") or "",
                           "cam2": r.get("cam2") or "",
                           # the trimmed window (skip dead air) for each clip the GPU tracker can read
                           "fs": g.get("fs", 0.0), "gs": g.get("gs", 0.0),
                           "out_dur": g.get("out_dur", 0.0)})
    return {"rounds": rounds, "progress": _studio_progress()}


@app.post("/api/studio/save_progress")
def studio_save_progress(round: int = Form(...), clip: str = Form(""),
                         tracked: str = Form(""), kpts: str = Form("")):
    """Remember a round's tracked result so it can be reopened from the dropdown without re-tracking."""
    data = _studio_progress()
    data[str(round)] = {"clip": clip, "tracked": tracked, "kpts": kpts}
    STUDIO_PROGRESS.write_text(json.dumps(data), encoding="utf-8")
    return {"ok": True, "rounds": len(data)}


# ── LIVE streaming — Phase 0: drive OBS (scene switch, configure platform, go live) ──
import obs_live  # noqa: E402

LIVE_CFG = ROOT / "live_keys.json"   # stream keys + OBS websocket password (keep out of git)


def _live_cfg() -> dict:
    if LIVE_CFG.exists():
        try:
            return json.loads(LIVE_CFG.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


# ── Phase 4: simulcast — a local ffmpeg RTMP relay OBS publishes to, fanning ONE stream out to
# Twitch + YouTube (-c copy, no re-encode). OBS streams to one target natively; this is the fan-out. ──
RELAY = {"running": False, "port": 19350, "platforms": []}
_relay_proc = None


def _relay_targets(cfg) -> list:
    keys = cfg.get("keys", {})
    out = []
    for plat in ("twitch", "youtube"):
        if keys.get(plat):
            out.append((plat, obs_live.RTMP[plat] + "/" + keys[plat]))
    return out


def _start_relay() -> str | None:
    """Launch the fan-out relay; returns the local ingest URL OBS should publish to (or None if
    fewer than 2 platforms have keys — simulcast needs both)."""
    global _relay_proc
    cfg = _live_cfg()
    targets = _relay_targets(cfg)
    if len(targets) < 2:
        return None
    port = RELAY["port"]
    ingest = f"rtmp://127.0.0.1:{port}/live/fs"
    tee = "|".join(f"[f=flv:onfail=ignore]{url}" for _, url in targets)
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-listen", "1",
           "-f", "flv", "-i", ingest, "-c", "copy", "-f", "tee", "-map", "0:v?", "-map", "0:a?", tee]
    _relay_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    RELAY["running"], RELAY["platforms"] = True, [p for p, _ in targets]
    return ingest


def _stop_relay():
    global _relay_proc
    RELAY["running"], RELAY["platforms"] = False, []
    if _relay_proc:
        try:
            _relay_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        _relay_proc = None


@app.get("/live", response_class=HTMLResponse)
def live_page():
    return _page("live.html")


@app.get("/overlay", response_class=HTMLResponse)
def overlay_page():
    return _page("overlay.html")


# ── Ninja Capture ──────────────────────────────────────────────────────────────
# A self-contained VDO.Ninja-style phone-as-wireless-camera section. A phone on the
# same WiFi opens /ninja/cam → its camera streams here over WebRTC → "Record all"
# captures every connected camera at once (synced), and each clip is saved into
# recordings/ (remuxed to mp4) so it drops straight into the library + rounds like
# any other clip. Deliberately isolated: new routes/WS only, nothing existing touched.
_ninja_rooms: dict = {}      # room -> {peer_id: WebSocket}
_ninja_roles: dict = {}      # room -> {peer_id: "receiver"|"sender"}


@app.get("/youtube-setup", response_class=HTMLResponse)
def youtube_setup_page():
    return _page("youtube-setup.html")


@app.get("/ninja", response_class=HTMLResponse)
def ninja_page():
    return _page("ninja.html")


@app.get("/ninja/cam", response_class=HTMLResponse)
def ninja_cam_page():
    return _page("ninja-cam.html")


@app.get("/api/ninja/lan")
def ninja_lan():
    """The PC's LAN host:port so the receiver can build a phone-reachable join link/QR
    (window.location may be 127.0.0.1, which a phone can't use)."""
    return {"host": f"{_lan_ip()}:8765"}


@app.post("/api/ninja/save")
async def ninja_save(file: UploadFile, label: str = Form("cam")):
    """Save a WebRTC-recorded clip into recordings/ (remuxed to mp4 for pipeline
    compatibility) + register its name so it shows in the library dropdowns."""
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_-]+", "", (label or "cam"))[:24] or "cam"
    uid = uuid.uuid4().hex[:8]
    raw = REC_DIR / f"{safe}-ninja-{uid}.webm"
    raw.write_bytes(await file.read())
    out = REC_DIR / f"{safe}-ninja-{uid}.mp4"
    final = raw
    try:
        run_ffmpeg(["-y", "-i", str(raw), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(out)])
        if out.exists() and out.stat().st_size > 0:
            raw.unlink(missing_ok=True)
            final = out
    except Exception:
        final = raw   # keep the webm if the remux fails — still importable
    _set_label(final.name, f"{label} (ninja)")
    return {"ok": True, "path": str(final), "name": final.name}


@app.websocket("/ws/ninja")
async def ws_ninja(ws: WebSocket):
    """WebRTC signaling relay for Ninja Capture: forwards SDP offers/answers + ICE
    candidates between peers in a room and tracks presence. WS scope bypasses the HTTP
    auth middleware (same as /ws/overlay), so the phone connects with no login cookie."""
    await ws.accept()
    room = me = None
    try:
        while True:
            m = await ws.receive_json()
            if m.get("type") == "join":
                room = str(m.get("room") or "fs")
                me = str(m.get("id") or "")
                role = m.get("role") or "sender"
                _ninja_rooms.setdefault(room, {})[me] = ws
                _ninja_roles.setdefault(room, {})[me] = role
                await ws.send_json({"type": "peers", "peers": [
                    {"id": k, "role": _ninja_roles[room].get(k, "sender")}
                    for k in _ninja_rooms[room] if k != me]})
                for k, w in list(_ninja_rooms[room].items()):
                    if k != me:
                        try:
                            await w.send_json({"type": "peer-joined", "id": me, "role": role})
                        except Exception:
                            pass
            else:
                to = m.get("to")
                if room and to and to in _ninja_rooms.get(room, {}):
                    m["from"] = me
                    try:
                        await _ninja_rooms[room][to].send_json(m)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        if room and me and room in _ninja_rooms:
            _ninja_rooms[room].pop(me, None)
            _ninja_roles.get(room, {}).pop(me, None)
            for k, w in list(_ninja_rooms.get(room, {}).items()):
                try:
                    await w.send_json({"type": "peer-left", "id": me})
                except Exception:
                    pass
            if not _ninja_rooms[room]:
                _ninja_rooms.pop(room, None)
                _ninja_roles.pop(room, None)


def _autodirect_cuts(cam_a: str, cam_b: str, off_a: float, off_b: float, log_path) -> list:
    """Run autodirect.py (form-coach venv) synchronously → the camera-switch cut list.
    Same engine the Form Studio director uses, called inline for a fighter's two angles."""
    cmd = [str(FORMCOACH_PY), "autodirect.py", cam_a, cam_b, str(off_a), str(off_b), "0"]
    cuts = None
    with open(log_path, "w", encoding="utf-8", errors="replace") as ef:
        proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                stderr=ef, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("CUTS "):
                cuts = json.loads(line[len("CUTS "):])
        proc.wait()
    if proc.returncode != 0 or cuts is None:
        raise RuntimeError("auto-direct failed (see log)")
    return cuts


def _fighter_feed(paths, auto: bool, job_id: str, tag: str):
    """Resolve one fighter's corner feed → a single clip path, or (2+ angles + auto-direct)
    a directed single-feed that cuts to their clearer angle. Angles assumed start-aligned
    (offset 0) for now — capture-sync offsets arrive with the capture phase."""
    paths = [p for p in (paths or []) if p and Path(p).exists()]
    if not paths:
        return None
    if len(paths) == 1 or not auto:
        return paths[0]
    a, b = paths[0], paths[1]
    jd = JOBS_DIR / job_id
    (jd / "out").mkdir(parents=True, exist_ok=True)
    cuts = _autodirect_cuts(a, b, 0.0, 0.0, jd / f"autodirect_{tag}.log")
    feed = str(jd / "out" / f"{tag}_directed.mp4")
    import pipeline
    pipeline.export_director_cut(a, b, 0.0, 0.0, cuts, feed, str(jd / f"work_{tag}"))
    return feed


def _run_ninja_broadcast(job_id, spectator, f1, f2, auto1, auto2):
    jd = JOBS_DIR / job_id
    (jd / "out").mkdir(parents=True, exist_ok=True)
    try:
        _set(job_id, status="running", percent=10, message="Preparing fighter 1's camera…")
        left = _fighter_feed(f1, auto1, job_id, "f1")
        _set(job_id, percent=45, message="Preparing fighter 2's camera…")
        right = _fighter_feed(f2, auto2, job_id, "f2")
        _set(job_id, percent=70, message="Compositing the broadcast layout…")
        import ninja_broadcast
        out = str(jd / "out" / "broadcast.mp4")
        ninja_broadcast.build_broadcast(spectator, left, right, out)
        _set(job_id, status="done", percent=100, message="Broadcast ready.",
             result={"final": out, "download_name": "fightsync-broadcast.mp4"})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/ninja/broadcast")
def ninja_broadcast_start(spectator: str = Form(...), f1_json: str = Form("[]"),
                          f2_json: str = Form("[]"), auto1: int = Form(0), auto2: int = Form(0)):
    """Build a two-fighter broadcast: spectator gameplay full-frame + each fighter's cam in a
    bottom corner. A fighter with 2+ angles + auto-direct gets their clearer angle picked per-moment."""
    if not Path(spectator).exists():
        raise HTTPException(400, "spectator clip not found")
    try:
        f1 = json.loads(f1_json); f2 = json.loads(f2_json)
        assert isinstance(f1, list) and isinstance(f2, list)
    except Exception:
        raise HTTPException(400, "bad fighter clip lists")
    if (bool(auto1) and len([p for p in f1 if p]) >= 2) or (bool(auto2) and len([p for p in f2 if p]) >= 2):
        if not FORMCOACH_PY.exists():
            raise HTTPException(500, "Auto-direct needs the pose engine (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_ninja_broadcast,
                     args=(job_id, spectator, f1, f2, bool(auto1), bool(auto2)),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/live/status")
async def live_status():
    cfg = _live_cfg()
    keys = cfg.get("keys", {})
    base = {"keys": {k: bool(keys.get(k)) for k in ("youtube", "twitch")},
            "platform": cfg.get("platform", "twitch"),
            "has_obs_pw": bool(cfg.get("obs_password"))}
    base["can_simulcast"] = bool(keys.get("twitch")) and bool(keys.get("youtube"))
    base["relay"] = {"running": RELAY["running"], "platforms": RELAY["platforms"]}
    try:
        s = await obs_live.status(password=cfg.get("obs_password", ""))
        s.update(base)
        return s
    except obs_live.OBSError as e:
        return {"connected": False, "error": str(e), **base}


@app.post("/api/live/keys")
def live_save_keys(platform: str = Form("twitch"), youtube: str = Form(None),
                   twitch: str = Form(None), obs_password: str = Form(None)):
    cfg = _live_cfg()
    cfg["platform"] = platform if platform in obs_live.RTMP else "twitch"
    keys = cfg.setdefault("keys", {})
    if youtube is not None:
        keys["youtube"] = youtube.strip()
    if twitch is not None:
        keys["twitch"] = twitch.strip()
    if obs_password is not None:
        cfg["obs_password"] = obs_password.strip()
    LIVE_CFG.write_text(json.dumps(cfg), encoding="utf-8")
    return {"ok": True}


@app.post("/api/live/scene")
async def live_scene(name: str = Form(...)):
    cfg = _live_cfg()
    try:
        await obs_live.set_scene(name, password=cfg.get("obs_password", ""))
        return {"ok": True}
    except obs_live.OBSError as e:
        raise HTTPException(400, str(e))


@app.post("/api/live/go")
async def live_go(simulcast: int = Form(0)):
    cfg = _live_cfg()
    pw = cfg.get("obs_password", "")
    try:
        if simulcast:
            ingest = _start_relay()
            if not ingest:
                raise obs_live.OBSError("Simulcast needs BOTH a Twitch and a YouTube stream key saved.")
            await asyncio.sleep(0.8)            # let ffmpeg start listening before OBS connects
            server, key = ingest.rsplit("/", 1)
            await obs_live.call(("SetStreamServiceSettings", {
                "streamServiceType": "rtmp_custom",
                "streamServiceSettings": {"server": server, "key": key}}), password=pw)
            await obs_live.start_stream(password=pw)
            return {"ok": True, "platform": "simulcast", "platforms": RELAY["platforms"]}
        plat = cfg.get("platform", "twitch")
        await obs_live.configure(plat, cfg.get("keys", {}).get(plat, ""), password=pw)
        await obs_live.start_stream(password=pw)
        return {"ok": True, "platform": plat}
    except obs_live.OBSError as e:
        _stop_relay()
        raise HTTPException(400, str(e))


@app.post("/api/live/stop")
async def live_stop():
    cfg = _live_cfg()
    try:
        await obs_live.stop_stream(password=cfg.get("obs_password", ""))
    except obs_live.OBSError as e:
        _stop_relay()
        raise HTTPException(400, str(e))
    _stop_relay()
    return {"ok": True}


# The live overlay's data feed (OBS browser source polls this). Phases 1–3 fill it: round/clock,
# live skeleton keypoints, between-round graphic cues. Phase 0 = a working placeholder.
OVERLAY_STATE = {"live": False, "phase": "", "round": 1, "clock": "", "label": "", "card": "", "kpts": None}


@app.get("/api/overlay/state")
def overlay_state():
    return OVERLAY_STATE


# ── Phase 2: LIVE body tracking — stream YOLO11-pose keypoints to the overlay in real time ──
# A subprocess runs live_pose.py in the CUDA venv (reads the OBS Virtual Camera) and prints
# normalized keypoints; this thread ingests them; /ws/overlay broadcasts them to the browser source.
LIVE = {"seq": 0, "kpts": None, "punches": 0, "guard": "", "running": False}
_live_proc = None


def _run_live_pose(source: str, model: str, imgsz: int):
    global _live_proc
    cmd = [str(RVM_PY), "live_pose.py", str(source), model, str(imgsz)]
    try:
        _live_proc = subprocess.Popen(cmd, cwd=str(RVM_DIR), stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except Exception:  # noqa: BLE001
        LIVE["running"] = False
        return
    LIVE["running"] = True
    for line in _live_proc.stdout:
        if not LIVE["running"]:
            break
        if line.startswith("KPTS "):
            try:
                d = json.loads(line[5:])
                LIVE["kpts"] = d.get("kpts")
                LIVE["punches"] = d.get("punches", 0)
                LIVE["guard"] = d.get("guard", "")
                LIVE["seq"] += 1
            except ValueError:
                pass
    LIVE["running"] = False
    LIVE["kpts"] = None


@app.post("/api/live/track/start")
def live_track_start(source: str = Form(""), model: str = Form("yolo11n-pose.pt"),
                     imgsz: int = Form(640)):
    if LIVE["running"]:
        return {"ok": True, "already": True}
    cfg = _live_cfg()
    src = source or cfg.get("vcam_source", "0")   # OBS Virtual Camera device index (default 0)
    if source:                                     # remember the chosen source
        cfg["vcam_source"] = source
        LIVE_CFG.write_text(json.dumps(cfg), encoding="utf-8")
    LIVE["seq"] = 0
    threading.Thread(target=_run_live_pose, args=(src, model, int(imgsz)), daemon=True).start()
    return {"ok": True}


@app.post("/api/live/track/stop")
def live_track_stop():
    global _live_proc
    LIVE["running"] = False
    if _live_proc:
        try:
            _live_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    LIVE["kpts"] = None
    return {"ok": True}


@app.get("/api/live/track/status")
def live_track_status():
    return {"running": LIVE["running"], "punches": LIVE["punches"], "guard": LIVE["guard"]}


@app.websocket("/ws/overlay")
async def ws_overlay(ws: WebSocket):
    """Pushes live keypoints + stats to the /overlay browser source at ~30 fps. Auth-exempt
    (WebSocket scope bypasses the HTTP auth middleware), same as /overlay itself."""
    await ws.accept()
    last = -1
    try:
        while True:
            if LIVE["seq"] != last:
                last = LIVE["seq"]
                await ws.send_json({"kpts": LIVE["kpts"], "punches": LIVE["punches"],
                                    "guard": LIVE["guard"], "running": LIVE["running"]})
            await asyncio.sleep(1 / 30)
    except Exception:  # noqa: BLE001 — client (OBS) disconnected
        pass


# ── Phase 3: autonomous round controller — runs the round clock, between-round cards + OBS scene
# switches on its own. Backbone is the (reliable) timed clock; one "Start match" press, then hands-off.
MATCH = {"running": False, "phase": "idle", "round": 0, "rounds": 3,
         "round_len": 180, "rest_len": 60, "clock": "", "live_scene": "", "between_scene": ""}
_match_stop = threading.Event()
_match_next = threading.Event()
_match_pause = threading.Event()

# Live auto-director: rotate OBS scenes (camera angles) on its own during the fight — a real-time
# version of the offline multicam director. Yields to the match controller during rest periods.
DIRECTOR = {"running": False, "scenes": [], "interval": 10, "current": ""}
_director_stop = threading.Event()
_director_next = threading.Event()


def _fmt_clock(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def _obs_scene_sync(name):
    if not name:
        return
    try:
        asyncio.run(obs_live.call(("SetCurrentProgramScene", {"sceneName": name}),
                                  password=_live_cfg().get("obs_password", ""), timeout=2.5))
    except Exception:  # noqa: BLE001 — OBS not connected → graphics still run
        pass


def _ov(**kw):
    OVERLAY_STATE.update(kw)


def _match_countdown(seconds):
    """Tick a phase down 1s at a time; honor pause/next/stop. Returns why it ended."""
    rem = int(seconds)
    while rem > 0:
        if _match_stop.is_set():
            return "stop"
        if _match_next.is_set():
            _match_next.clear()
            return "next"
        if _match_pause.is_set():
            time.sleep(0.2)
            continue
        MATCH["clock"] = _fmt_clock(rem)
        _ov(clock=MATCH["clock"])
        time.sleep(1.0)
        rem -= 1
    return "done"


def _match_loop():
    _ov(live=True)
    n = MATCH["rounds"]
    for rnd in range(1, n + 1):
        if _match_stop.is_set():
            break
        MATCH["round"], MATCH["phase"] = rnd, "round"
        _ov(phase="round", round=rnd, card=f"ROUND {rnd}")
        if not DIRECTOR["running"]:            # if the auto-director runs, it owns live scenes
            _obs_scene_sync(MATCH["live_scene"])
        time.sleep(2.2)                        # hold the "ROUND N" announce card
        _ov(card="")                           # …then clear → fight
        if _match_countdown(MATCH["round_len"]) == "stop":
            break
        if rnd < n:                            # rest period + between-round graphic
            MATCH["phase"] = "rest"
            _ov(phase="rest", card=f"END OF ROUND {rnd}")
            _obs_scene_sync(MATCH["between_scene"])
            time.sleep(2.0)
            _ov(card="REST")
            r = _match_countdown(MATCH["rest_len"])
            _ov(card="")
            if r == "stop":
                break
    MATCH["phase"], MATCH["running"] = "done", False
    if not _match_stop.is_set():
        _ov(phase="done", card="FINAL BELL", clock="")
        _obs_scene_sync(MATCH["live_scene"])
        time.sleep(4.0)
    _ov(card="", clock="", phase="", live=False)


@app.post("/api/live/match/start")
def match_start(rounds: int = Form(3), round_len: int = Form(180), rest_len: int = Form(60),
                live_scene: str = Form(""), between_scene: str = Form("")):
    if MATCH["running"]:
        return {"ok": True, "already": True}
    MATCH.update(rounds=max(1, rounds), round_len=max(5, round_len), rest_len=max(0, rest_len),
                 live_scene=live_scene.strip(), between_scene=between_scene.strip(),
                 running=True, round=0, phase="round")
    cfg = _live_cfg()
    cfg["match"] = {"rounds": rounds, "round_len": round_len, "rest_len": rest_len,
                    "live_scene": live_scene.strip(), "between_scene": between_scene.strip()}
    LIVE_CFG.write_text(json.dumps(cfg), encoding="utf-8")
    _match_stop.clear(); _match_next.clear(); _match_pause.clear()
    threading.Thread(target=_match_loop, daemon=True).start()
    return {"ok": True}


@app.post("/api/live/match/next")
def match_next():
    _match_next.set()
    return {"ok": True}


@app.post("/api/live/match/pause")
def match_pause():
    if _match_pause.is_set():
        _match_pause.clear()
        return {"ok": True, "paused": False}
    _match_pause.set()
    return {"ok": True, "paused": True}


@app.post("/api/live/match/stop")
def match_stop_ep():
    _match_stop.set()
    MATCH["running"], MATCH["phase"] = False, "idle"
    _ov(card="", clock="", phase="", live=False)
    return {"ok": True}


@app.get("/api/live/match/status")
def match_status():
    cfg = _live_cfg().get("match", {})
    return {**{k: MATCH[k] for k in ("running", "phase", "round", "rounds", "clock")},
            "paused": _match_pause.is_set(), "saved": cfg}


def _director_loop():
    DIRECTOR["running"] = True
    i = 0
    while not _director_stop.is_set():
        scenes = list(DIRECTOR["scenes"])
        # nothing to do, or the match's rest/end owns the scene → idle this tick
        if not scenes or (MATCH["running"] and MATCH["phase"] in ("rest", "done")):
            time.sleep(0.4)
            continue
        scn = scenes[i % len(scenes)]
        DIRECTOR["current"] = scn
        _obs_scene_sync(scn)
        i += 1
        waited = 0.0
        while waited < DIRECTOR["interval"] and not _director_stop.is_set():
            if _director_next.is_set():
                _director_next.clear()
                break
            if MATCH["running"] and MATCH["phase"] in ("rest", "done"):
                break
            time.sleep(0.3)
            waited += 0.3
    DIRECTOR["running"], DIRECTOR["current"] = False, ""


@app.post("/api/live/director/start")
def director_start(scenes: str = Form(""), interval: int = Form(10)):
    sc = [s.strip() for s in scenes.split("|") if s.strip()]
    if not sc:
        raise HTTPException(400, "Pick at least one camera scene to rotate through.")
    DIRECTOR.update(scenes=sc, interval=max(2, interval))
    cfg = _live_cfg()
    cfg["director"] = {"scenes": sc, "interval": interval}
    LIVE_CFG.write_text(json.dumps(cfg), encoding="utf-8")
    if DIRECTOR["running"]:
        return {"ok": True, "already": True}
    _director_stop.clear(); _director_next.clear()
    threading.Thread(target=_director_loop, daemon=True).start()
    return {"ok": True}


@app.post("/api/live/director/stop")
def director_stop_ep():
    _director_stop.set()
    DIRECTOR["running"] = False
    return {"ok": True}


@app.post("/api/live/director/cut")
def director_cut():
    _director_next.set()
    return {"ok": True}


@app.get("/api/live/director/status")
def director_status():
    return {"running": DIRECTOR["running"], "current": DIRECTOR["current"],
            "interval": DIRECTOR["interval"], "saved": _live_cfg().get("director", {})}


def _run_calibrate_job(job_id: str, src: str, seconds: float = 0):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        _set(job_id, status="running", percent=5, message="Measuring your proportions…")
        profile_path = FORMCOACH_DIR / "profile.json"
        cmd = [str(FORMCOACH_PY), "calibrate.py", src, str(profile_path),
               str(seconds if seconds and seconds > 0 else 0)]
        prof = None
        with open(job_dir / "calib.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        p = int(line.split()[1])
                        _set(job_id, percent=5 + int(p * 0.9), message=f"Measuring you… {p}%")
                    except ValueError:
                        pass
                elif line.startswith("PROFILE "):
                    prof = json.loads(line[len("PROFILE "):])
            proc.wait()
        if proc.returncode != 0 or prof is None:
            raise RuntimeError("calibration failed (see calib.log)")
        _set(job_id, status="done", percent=100, message="Calibrated to you.",
             result={"profile": prof})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/calibrate")
def studio_calibrate(path: str = Form(...), seconds: float = Form(0)):
    """Measure the user's personal baselines (guard height, stance, reach) from a clip
    and save them as form-coach/profile.json — pose tracking then judges cues vs them."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_calibrate_job, args=(job_id, path, seconds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/studio/profile")
def studio_profile():
    p = FORMCOACH_DIR / "profile.json"
    if p.exists():
        try:
            return {"profile": json.loads(p.read_text(encoding="utf-8"))}
        except (ValueError, OSError):
            pass
    return {"profile": None}


def _run_refs_job(job_id: str, src: str, seconds: float = 0):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        _set(job_id, status="running", percent=5, message="Finding your best punches…")
        reps_path = FORMCOACH_DIR / "reps.json"
        cmd = [str(FORMCOACH_PY), "refs.py", src, str(reps_path),
               str(seconds if seconds and seconds > 0 else 0)]
        reps = None
        with open(job_dir / "refs.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        p = int(line.split()[1])
                        _set(job_id, percent=5 + int(p * 0.9), message=f"Finding your best punches… {p}%")
                    except ValueError:
                        pass
                elif line.startswith("REPS "):
                    reps = json.loads(line[len("REPS "):])
            proc.wait()
        if proc.returncode != 0 or reps is None:
            raise RuntimeError("reference capture failed (see refs.log)")
        _set(job_id, status="done", percent=100, message="Saved your best punches.",
             result={"reps": reps})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/refs")
def studio_refs(path: str = Form(...), seconds: float = Form(0)):
    """Capture the user's best punches (peak extension per type) from a clip → reps.json;
    pose_coach then grades future punches '% of best'."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_refs_job, args=(job_id, path, seconds), daemon=True).start()
    return {"job_id": job_id}


def _run_candidates_job(job_id: str, src: str, seconds: float = 0):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        _set(job_id, status="running", percent=5, message="Finding arm-extensions…")
        cmd = [str(FORMCOACH_PY), "candidates.py", src,
               str(seconds if seconds and seconds > 0 else 0)]
        cands = None
        with open(job_dir / "cand.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=int(line.split()[1]), message="Finding arm-extensions…")
                    except ValueError:
                        pass
                elif line.startswith("PUNCHCANDIDATES "):
                    cands = json.loads(line[len("PUNCHCANDIDATES "):])
            proc.wait()
        if proc.returncode != 0 or cands is None:
            raise RuntimeError("candidate scan failed (see cand.log)")
        _set(job_id, status="done", percent=100,
             message=f"Found {len(cands)} candidates.", result={"candidates": cands})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/punch_candidates")
def studio_punch_candidates(path: str = Form(...), seconds: float = Form(0)):
    """Find every arm-extension candidate (with features) so the user can label which
    are real punches — the training step for the personal punch detector."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_candidates_job, args=(job_id, path, seconds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/save_punch_profile")
def studio_save_punch_profile(min_ext: float = Form(...), min_speed: float = Form(...),
                              n: int = Form(0)):
    """Save the learned per-user punch thresholds (from the labeled candidates)."""
    prof = {"min_ext": round(min_ext, 3), "min_speed": round(min_speed, 2), "n": n}
    (FORMCOACH_DIR / "punch_profile.json").write_text(json.dumps(prof), encoding="utf-8")
    return {"ok": True, "profile": prof}


def _run_posedata_job(job_id: str, src: str, seconds: float = 0):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    out_json = job_dir / "posedata.json"
    try:
        _set(job_id, status="running", percent=5, message="Reading the skeleton for editing…")
        cmd = [str(FORMCOACH_PY), "posedata.py", src, str(out_json),
               str(seconds if seconds and seconds > 0 else 0)]
        with open(job_dir / "posedata.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=int(line.split()[1]), message="Reading the skeleton…")
                    except ValueError:
                        pass
            proc.wait()
        if proc.returncode != 0 or not out_json.exists():
            raise RuntimeError("pose-data export failed (see posedata.log)")
        _set(job_id, status="done", percent=100, message="Ready to edit.",
             result={"data_path": str(out_json)})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/studio/pose_data")
def studio_pose_data(path: str = Form(...), seconds: float = Form(0)):
    """Export editable per-frame pose data for the skeleton editor (drag-to-correct)."""
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_posedata_job, args=(job_id, path, seconds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/save_corrections")
def studio_save_corrections(corrections: str = Form(...)):
    """Save the user's hand-dragged joint corrections {frameIdx: {jointIdx: [x,y]}}
    (normalized). pose_coach applies them on the next render."""
    try:
        json.loads(corrections)
    except (ValueError, TypeError):
        raise HTTPException(400, "Bad corrections JSON")
    (FORMCOACH_DIR / "corrections.json").write_text(corrections, encoding="utf-8")
    return {"ok": True}


@app.get("/api/studio/corrections")
def studio_get_corrections():
    p = FORMCOACH_DIR / "corrections.json"
    if p.exists():
        try:
            return {"corrections": json.loads(p.read_text(encoding="utf-8"))}
        except (ValueError, OSError):
            pass
    return {"corrections": {}}


@app.get("/api/studio/reps")
def studio_reps():
    p = FORMCOACH_DIR / "reps.json"
    if p.exists():
        try:
            return {"reps": json.loads(p.read_text(encoding="utf-8"))}
        except (ValueError, OSError):
            pass
    return {"reps": None}


def _run_autodirect_job(job_id, cam_a, cam_b, off_a, off_b, seconds):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        _set(job_id, status="running", percent=5,
             message="Watching both cameras to pick the clearer angle…")
        cmd = [str(FORMCOACH_PY), "autodirect.py", cam_a, cam_b,
               str(off_a), str(off_b), str(seconds if seconds and seconds > 0 else 0)]
        cuts = None
        with open(job_dir / "autodirect.log", "w", encoding="utf-8", errors="replace") as ef:
            proc = subprocess.Popen(cmd, cwd=str(FORMCOACH_DIR), stdout=subprocess.PIPE,
                                    stderr=ef, text=True, bufsize=1)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        _set(job_id, percent=int(line.split()[1]),
                             message="Watching both cameras…")
                    except ValueError:
                        pass
                elif line.startswith("CUTS "):
                    cuts = json.loads(line[len("CUTS "):])
            proc.wait()
        if proc.returncode != 0 or cuts is None:
            raise RuntimeError("auto-direct failed (see autodirect.log)")
        _set(job_id, status="done", percent=100,
             message=f"Picked {len(cuts)} angle switch(es).", result={"cuts": cuts})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


def _run_export_director_job(job_id, rounds):
    job_dir = JOBS_DIR / job_id
    work = job_dir / "work"
    out = job_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    try:
        n = len(rounds)
        _set(job_id, status="running", percent=15,
             message=(f"Building your shareable angle — {n} round(s)"
                      + (", black between rounds…" if n > 1 else "…")))
        from pipeline import export_director_multi
        outp = str(out / "my_angle.mp4")
        export_director_multi(rounds, outp, str(work))
        _set(job_id, status="done", percent=100, message="Your shareable angle is ready.",
             result={"final": outp, "download_name": "my-fight-angle.mp4"})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e), traceback=traceback.format_exc())


@app.post("/api/export_director")
def export_director(rounds_json: str = Form(...)):
    """Export a clean FULL-SCREEN single-feed that cuts between the two camera angles at
    the director's switch points — no gameplay, no PiP — across ALL rounds, with 3s of
    black between rounds so the recipient sees where each round ends."""
    try:
        rounds = json.loads(rounds_json)
        if not isinstance(rounds, list) or not rounds:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(400, "no synced rounds provided")
    for rd in rounds:
        for k in ("cam_a", "cam_b"):
            if not Path(rd.get(k, "")).exists():
                raise HTTPException(400, f"Clip not found: {rd.get(k)}")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_export_director_job, args=(job_id, rounds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/autodirect")
def studio_autodirect(cam_a: str = Form(...), cam_b: str = Form(...),
                      off_a: float = Form(0), off_b: float = Form(0),
                      seconds: float = Form(0)):
    """Pose-score both camera angles and auto-pick the clearer view of the body →
    a camera-switch cut list (gameplay timeline). Powers the director's Auto-direct."""
    for p in (cam_a, cam_b):
        if not Path(p).exists():
            raise HTTPException(400, f"Camera clip not found: {p}")
    if not FORMCOACH_PY.exists():
        raise HTTPException(500, "Pose engine isn't set up (form-coach venv missing).")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…", "result": None}
    threading.Thread(target=_run_autodirect_job,
                     args=(job_id, cam_a, cam_b, off_a, off_b, seconds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/studio/render")
def studio_render(path: str = Form(...), annotations: str = Form(...)):
    if not Path(path).exists():
        raise HTTPException(400, f"Video not found: {path}")
    anns = json.loads(annotations)
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "percent": 0,
                    "message": "Queued…", "result": None}
    threading.Thread(target=_run_studio_job, args=(job_id, path, anns),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return JSONResponse({k: v for k, v in job.items() if k != "traceback"})


@app.get("/api/download/{job_id}")
def download(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    final = job["result"]["final"]
    name = job["result"].get("download_name", "fightsync_final.mp4")
    return FileResponse(final, filename=name, media_type="video/mp4")


@app.get("/api/subtitles/{job_id}")
def subtitles(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    srt = job["result"].get("subtitles")
    if not srt:
        raise HTTPException(404, "No subtitles produced")
    return FileResponse(srt, filename="fightsync_final.srt",
                        media_type="text/plain")


@app.get("/api/metadata/{job_id}")
def metadata_txt(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    final = job["result"].get("final")
    txt = Path(final).parent / "youtube.txt" if final else None
    if not txt or not txt.exists():
        raise HTTPException(404, "No metadata produced")
    return FileResponse(str(txt), filename="fightsync_youtube.txt",
                        media_type="text/plain")


@app.get("/api/thumbnail/{job_id}")
def thumbnail_jpg(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Not ready")
    final = (job.get("result") or {}).get("final")
    jpg = Path(final).parent / "thumbnail.jpg" if final else None
    if not jpg or not jpg.exists():
        raise HTTPException(404, "No thumbnail produced")
    return FileResponse(str(jpg), filename="fightsync_thumbnail.jpg", media_type="image/jpeg")


# ── Shorts / highlights reel ────────────────────────────────────────────────
def _run_reel_job(reel_id: str, src: str, label: str):
    job_dir = JOBS_DIR / reel_id
    work = job_dir / "work"
    out = job_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    try:
        _set(reel_id, status="running")
        res = make_reel(src, str(out / "short.mp4"), str(work),
                        label=label, progress=_progress(reel_id))
        res["download_name"] = "fightsync_short.mp4"
        _set(reel_id, status="done", result=res, percent=100, message="Reel ready.")
    except Exception as e:  # noqa: BLE001
        _set(reel_id, status="error", message=str(e),
             traceback=traceback.format_exc())


@app.post("/api/shorts/{job_id}")
def make_shorts(job_id: str, label: str = Form("HIGHLIGHTS")):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Render not finished")
    src = job["result"].get("final")
    if not src or not Path(src).exists():
        raise HTTPException(404, "Source video missing")
    reel_id = uuid.uuid4().hex[:12]
    JOBS[reel_id] = {"status": "queued", "percent": 0,
                     "message": "Queued…", "result": None}
    threading.Thread(target=_run_reel_job, args=(reel_id, src, label),
                     daemon=True).start()
    return {"job_id": reel_id}


# ── YouTube upload ──────────────────────────────────────────────────────────
@app.get("/api/youtube/state")
def youtube_state():
    return yt_mod.state()


def _run_upload_job(up_id: str, src: str, meta: dict, privacy: str):
    try:
        _set(up_id, status="running")
        res = yt_mod.upload(src, meta.get("title", ""), meta.get("description", ""),
                            meta.get("tags", []), privacy, _progress(up_id))
        _set(up_id, status="done", result=res, percent=100, message="Uploaded.")
    except Exception as e:  # noqa: BLE001
        _set(up_id, status="error", message=str(e),
             traceback=traceback.format_exc())


@app.post("/api/youtube/upload/{job_id}")
def youtube_upload(job_id: str, privacy: str = Form("unlisted")):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Render not finished")
    res = job["result"]
    src = res.get("final")
    if not src or not Path(src).exists():
        raise HTTPException(404, "Video missing")
    meta = res.get("metadata") or {"title": "Thrill of the Fight 2"}
    up_id = uuid.uuid4().hex[:12]
    JOBS[up_id] = {"status": "queued", "percent": 0,
                   "message": "Queued…", "result": None}
    threading.Thread(target=_run_upload_job, args=(up_id, src, meta, privacy),
                     daemon=True).start()
    return {"job_id": up_id}


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("FIGHTSYNC_PORT", "8765"))
    # bind all interfaces so phones/tablets on the same Wi-Fi can reach it
    host = os.environ.get("FIGHTSYNC_HOST", "0.0.0.0")
    ip = _lan_ip()
    gate = f"password: {PASSWORD}" if AUTH_ENABLED else "no password (auth OFF)"
    print(f"\n  FightSync running   ({gate})")
    print(f"    this PC      ->  http://127.0.0.1:{port}")
    print(f"    phone/tablet ->  http://{ip}:{port}   (same Wi-Fi)\n")
    try:
        capture_mod.start(log=lambda m: print("  " + str(m)))
        print("  capture: new 'cam 2 PC' videos auto-named totfN; mobile can upload to all 3 folders")
    except Exception as e:
        print(f"  capture watcher not started: {e}")
    if quest_mod is not None:
        try:
            quest_mod.start_poller("gameplay", log=lambda m: print("  " + str(m)))
            print("  quest: plug in the headset and new VideoShots auto-copy to 'gameplay' as totfN\n")
        except Exception as e:
            print(f"  quest poller not started: {e}\n")
    else:
        print("  quest: import disabled (pywin32 not available)\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
