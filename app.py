"""FightSync — local web app to auto-sync Thrill of the Fight 2 gameplay with
facecam, then composite, subtitle, and top-and-tail it for YouTube.

Run:  python app.py   then open http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
from http.cookies import SimpleCookie
import secrets as pysecrets
import shutil
import subprocess
import threading
import traceback
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

import channels as ch_mod
import youtube_upload as yt_mod
from annotations import render_overlay
from cropdetect import apply_crop, detect_crop
from media import FFMPEG, probe
from pipeline import RenderConfig, _enc, render_multi
from shorts import make_reel
from tracking import track_region

ROOT = Path(__file__).parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
REC_DIR = ROOT / "recordings"
REC_DIR.mkdir(exist_ok=True)
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
        authed = path in OPEN_PATHS
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


def _run_job(job_id: str, gameplays: list, facecams: list, cfg: RenderConfig):
    job_dir = JOBS_DIR / job_id
    work = job_dir / "work"
    out = job_dir / "out"
    try:
        _set(job_id, status="running")
        result = render_multi(gameplays, facecams, str(out), str(work), cfg,
                              _progress(job_id))
        _set(job_id, status="done", result=result, percent=100,
             message="Done.")
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e),
             traceback=traceback.format_exc())


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


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
    pip_position: str = Form("br"),
    pip_scale: float = Form(0.26),
    audio_mode: str = Form("mix"),
    make_subtitles: bool = Form(True),
    burn_subtitles: bool = Form(True),
    intro: bool = Form(True),
    outro: bool = Form(True),
    whisper_model: str = Form("base"),
    replays: bool = Form(False),
    replay_times: str = Form(""),
    auto_replays: int = Form(0),
    replay_smooth: bool = Form(False),
    gameplay_paths_json: str = Form(""),
    facecam_paths_json: str = Form(""),
    transitions: bool = Form(True),
    transition_label: str = Form("ROUND"),
    bell: bool = Form(True),
    manual_offset: str = Form(""),
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

    cfg = RenderConfig(
        title=title,
        intro_subtitle=intro_subtitle,
        layout=layout,
        pip_position=pip_position,
        pip_scale=pip_scale,
        audio_mode=audio_mode,
        make_subtitles=make_subtitles,
        burn_subtitles=burn_subtitles and make_subtitles,
        intro=intro,
        outro=outro,
        whisper_model=whisper_model,
        replays=replays,
        replay_times=replay_times,
        auto_replays=auto_replays,
        replay_smooth=replay_smooth,
        transitions=transitions,
        transition_label=transition_label,
        bell=bell,
        manual_offset=(float(manual_offset) if manual_offset.strip() else None),
    )

    JOBS[job_id] = {"status": "queued", "percent": 0,
                    "message": "Queued…", "result": None}
    threading.Thread(target=_run_job, args=(job_id, gs, fs, cfg),
                     daemon=True).start()
    return {"job_id": job_id}


def _run_import_job(job_id: str, url: str, name: str):
    try:
        _set(job_id, status="running", message="Downloading clip…")
        path = ch_mod.download_clip(
            url, REC_DIR, name,
            on_progress=lambda p: _set(job_id, percent=p,
                                       message=f"Downloading… {p}%"))
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
             facecam_paths_json: str = Form(...)):
    """Fast audio-only sync preview (no rendering): per pair, the offset and a
    confidence so you know alignment is solid before committing to a render."""
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
                s = compute_sync(gs[i], fs[i], str(work))
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


PROXY_DIR = REC_DIR / "proxies"
PROXY_DIR.mkdir(exist_ok=True)


def _proxy_path(src: str, seconds: float = 0) -> Path:
    p = Path(src)
    try:
        stamp = f"{p.resolve()}:{p.stat().st_mtime_ns}:{p.stat().st_size}:{seconds}"
    except OSError:
        stamp = f"{p}:{seconds}"
    key = hashlib.sha1(stamp.encode()).hexdigest()[:14]
    return PROXY_DIR / f"{key}.mp4"


def _run_proxy_job(job_id: str, src: str, seconds: float):
    try:
        dst = _proxy_path(src, seconds)
        if not dst.exists():
            _set(job_id, status="running", message="Optimizing preview…")
            vf = ("scale=854:480:force_original_aspect_ratio=decrease,"
                  "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30")
            # -g 15 + closed GOP = a keyframe ~every 0.5s, so the preview player
            # can seek/nudge anywhere without stalling at sparse keyframes (the
            # "freezes in the same spots" symptom). All-I would be even smoother
            # but bloats the file; 0.5s GOP is the sweet spot.
            tail = ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "30", "-g", "15", "-keyint_min", "15",
                    "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", str(dst)]
            dur = (["-t", str(seconds)] if seconds and seconds > 0 else [])
            base = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
            # GPU-accelerated decode (fast for HEVC); fall back to software.
            last = None
            for hw in (["-hwaccel", "auto"], []):
                r = subprocess.run(base + hw + dur + ["-i", src] + tail,
                                   capture_output=True, text=True)
                if r.returncode == 0:
                    break
                last = r
            else:
                raise RuntimeError(
                    "proxy failed:\n" + (last.stderr[-600:] if last else ""))
        _set(job_id, status="done", percent=100, message="ready",
             result={"path": str(dst)})
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", message=str(e),
             traceback=traceback.format_exc())


@app.post("/api/proxy")
def proxy(path: str = Form(...), seconds: float = Form(0)):
    """A small 480p H.264 proxy for smooth in-browser preview. `seconds`>0 only
    transcodes the first N seconds (much faster); 0 does the whole clip."""
    if not Path(path).exists():
        raise HTTPException(404, f"Not found: {path}")
    job_id = uuid.uuid4().hex[:12]
    dst = _proxy_path(path, seconds)
    if dst.exists():
        JOBS[job_id] = {"status": "done", "percent": 100, "message": "cached",
                        "result": {"path": str(dst)}}
        return {"job_id": job_id}
    JOBS[job_id] = {"status": "queued", "percent": 0, "message": "Queued…",
                    "result": None}
    threading.Thread(target=_run_proxy_job, args=(job_id, path, seconds),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/upload")
def upload(file: UploadFile, name: str = Form("facecam")):
    """Stage a picked video (e.g. iPhone library footage) as a source slot,
    preserving its real extension so ffmpeg handles iPhone HEVC/.mov natively.

    Deliberately a sync (`def`) endpoint: Starlette runs it in a threadpool, so
    the large file copy never blocks the event loop — otherwise a big upload
    freezes the whole server during the copy and the 100%->done response gets
    dropped over the tunnel (the 'stuck at 100%' bug)."""
    if name not in ("gameplay", "facecam"):
        name = "facecam"
    suffix = Path(file.filename or "").suffix.lower() or ".mp4"
    dst = REC_DIR / f"{name}-lib-{uuid.uuid4().hex[:8]}{suffix}"
    _save_upload(file, dst)
    return {"path": str(dst), "name": name}


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
    return (ROOT / "static" / "studio.html").read_text(encoding="utf-8")


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
    uvicorn.run(app, host=host, port=port, log_level="warning")
