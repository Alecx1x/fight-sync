# CLAUDE.md — FightSync

Guidance for Claude Code (and agents) working in this repo. **Read this first.**

**Harness:** driven inside Terminal Bridge. @../terminal-bridge/CLAUDE.md

## What this is
**FightSync** — a local web app that automates the user's **Thrill of the Fight 2** (VR
boxing) YouTube workflow. You drop in **gameplay** capture + **facecam**, it **auto-syncs**
them by shared audio, composites (picture-in-picture or side-by-side), mixes audio,
auto-subtitles (Whisper), can add **slow-mo replays** + a **bell round-card transition**
between clips, and tops & tails with intro/outro → a ready-to-upload `final.mp4`.
It also has a **Form Studio** (coaching annotation editor) and live-channel auto-capture.

- **Stack:** Python 3.10 + FastAPI/uvicorn + **ffmpeg** (the heavy lifter). Vanilla JS UI,
  no build step (single HTML files served as-is).
- **Platform:** Windows 11, **PowerShell** (no `&&`; use `;`). Everything runs in `.venv`.
- ffmpeg installed via `winget install Gyan.FFmpeg` — NOT on PATH; `media.py` auto-finds it
  under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\...\bin`.

## Run
- **`start.bat`** — local only → http://127.0.0.1:8765
- **`start-remote.bat`** — server + `tunnel_watchdog.py` (Cloudflare quick tunnel that
  self-restarts on drop/HTTP-530; writes the live URL to `current-url.txt`).
- Manual: `.venv\Scripts\python app.py` (binds `0.0.0.0:8765`; prints localhost + LAN URL).
- **Port 8765** on purpose — **port 8000 is taken by SillyTavern** on this machine.
- **Password gate**: password in `fightsync-secret.txt` (env `FIGHTSYNC_PASSWORD`), shown
  on startup. All paths gated except `/login`, `/favicon.*`. Cookie `fs_auth` (HMAC),
  30-day, Secure when behind HTTPS.

## Editing workflow
- **`static/index.html` / `static/studio.html` go live on a browser reload** — the routes
  read the file on each request, no caching concern, **no restart needed**.
- **Any `.py` change requires restarting the server** (kill python running `app.py`,
  relaunch). Restarting drops in-memory jobs but not files.
- **Do NOT open a browser tab to "show" changes — the user refreshes their own tab.**
  Verify with headless Playwright (`channel="chrome", headless=True`) or `curl`/`requests`.

## Architecture & key files
- **`app.py`** — FastAPI server: pure-ASGI auth middleware, all endpoints, the in-memory
  `JOBS` registry, background job runners, range-aware `/api/media`, tunnel/LAN bind.
- **`pipeline.py`** — the render. `RenderConfig` (all options). `render_multi(gameplays[],
  facecams[], …)` pairs clips by index, builds each segment, weaves bell transitions +
  intro/outro in one final concat. `render()` = single-pair wrapper. `_build_segment`
  (sync→composite PiP/side-by-side→subtitles→replays), `_transition_card` (bell round
  card), `_title_card` (intro/outro). `_enc()` = shared x264/aac encode args.
- **`sync.py`** — `compute_sync()`: **onset-based** audio alignment (correlates rectified
  log-energy flux, i.e. shared transients/claps), confidence = peak-to-sidelobe ratio.
- **`metadata.py`** — `build()`: from the render result + the generated subtitles, makes a
  **YouTube title / description / tags** and **chapters**. `render_multi` walks the final
  clip timeline to time each chapter (Intro / Round N at the bell card / Outro), enforces
  YouTube's rules (first at 0:00, ≥3 chapters, ≥10s apart) and writes `out/youtube.txt`.
- **`shorts.py`** — `make_reel()`: auto-cuts a vertical **9:16 highlights/Shorts reel** from a
  finished render. Reuses `detect_impacts` for the biggest hits, windows them (≤58s total),
  re-frames each to 1080×1920 with a blurred-fill background + header label, concats (demuxer
  copy). Run as a post-render job from the result card (`/api/shorts/{job_id}`).
- **`youtube_upload.py`** + **`youtube_auth.py`** — one-click **upload to YouTube** (Data API
  v3). `youtube_auth.py` is a **one-time terminal step** (browser consent → saves
  `yt_token.json`); the web app then uploads with the saved token via `/api/youtube/upload`.
  Needs the user's `client_secret.json` (Desktop-app OAuth client). NO Google creds are
  bundled — `state()` reports configured/authorized so the UI shows setup steps until ready.
- **`media.py`** — `FFMPEG`/`FFPROBE` discovery, `probe()` (with packet-count duration
  fallback for MediaRecorder webm), `run_ffmpeg()`.
- **`replay.py`** + **`replay_sfx.py`** — Fight-Night slow-mo replays (ffmpeg `setpts`
  ramp) + procedurally synthesized whoosh / glove-impact / boxing-**bell** SFX.
- **`cropdetect.py`** — motion-variance detection of the gameplay viewport (auto-crop).
- **`channels.py`** — yt-dlp channel live-detection (no API key) + `download_clip()`
  (Meta/Horizon import; tries cookies-from-browser for login-gated clips).
- **`tracking.py`** — OpenCV CSRT region tracking for the Form Studio.
- **`annotations.py`** — coaching overlay renderer (circles/arrows/text/zones/polygons;
  Pillow super-sampled RGBA frames piped into ffmpeg overlay; per-frame track offsets).
- **`static/index.html`** — main UI (multi-clip tiles, sync panel, side-by-side preview
  player, composite/extras/replays/transitions options, channels, recorder).
- **`static/studio.html`** — Form Studio annotation editor (canvas over `<video>`).
- **`tunnel_watchdog.py`** — keeps the quick tunnel alive; `current-url.txt` = live URL.

## Render pipeline (per clip pair)
sync (onset) → composite (PiP `[bg][pip]overlay` **or** side-by-side `hstack`, both
halves equal) + audio mix → optional subtitles (faster-whisper) → optional slow-mo
replays → segment. Then: intro card + (segment + bell transition)… + outro → one concat.

## Endpoints (all behind the password gate)
`/login`, `/`, `/studio`, `/favicon.svg`. `POST /api/sync` (fast audio-only preview),
`/api/render` (multi-clip job; `gameplay_paths_json`/`facecam_paths_json` arrays),
`/api/upload`, `/api/upload_recording` (webm→mkv remux), `/api/import_url` (yt-dlp),
`/api/proxy` (480p H.264 preview proxy, `seconds` window), `/api/media` (range/206),
`/api/status/{id}` (carries `result.metadata` = YouTube title/description/tags/chapters),
`/api/download/{id}` (honors `result.download_name`), `/api/subtitles/{id}`,
`/api/metadata/{id}` (downloads `youtube.txt`), `/api/shorts/{id}` (start reel job),
`/api/youtube/state`, `/api/youtube/upload/{id}` (start upload job),
channels (`/api/channels…`, `/api/channels/{id}/live`),
crop (`/api/detect_crop`,`/api/apply_crop`), studio (`/api/studio/track|render|project`).

## GOTCHAS (hard-won — don't re-learn these)
- **Inline-JS temporal dead zone**: index.html/studio.html run init + animation loops
  immediately, so any `const`/`let` referenced at load must be **declared above** the code
  that uses it. This bit us 3×. `node --check` passes syntax but NOT TDZ — test in a real
  (headless) browser.
- **Auth middleware MUST be pure-ASGI** (`_AuthMiddleware`), never `BaseHTTPMiddleware` —
  the latter buffers responses and **breaks HTTP Range**, making every video un-seekable.
  And **Starlette 0.37 `FileResponse` has no range support**, so `/api/media` implements
  Range/206 **manually**. If video scrubbing breaks, look here first.
- **ffmpeg `drawtext` on Windows** can't handle the `C:` drive colon no matter how escaped
  → copy a system font into the job work dir and reference it by **bare relative name**
  (`_ensure_font`).
- **Browser preview can't decode iPhone HEVC/4K** (stutters ~2fps) → the preview plays a
  cached **480p H.264 proxy** (`/api/proxy`, first ~45s, `-hwaccel auto` + software
  fallback). The **render always uses the full-quality originals** — proxy is preview-only.
- **Sync is onset/transient-based**: a single shared clap locks it; overall loudness does
  not. **Low confidence (<0.20) = no trustworthy lock → the detected offset is garbage and
  MUST be ignored** (`_build_segment` start-aligns instead; using it misaligns synchronous
  clips by tens of seconds — this was a real bug). `cfg.manual_offset` (from the preview
  nudge) overrides everything. iPhone-facecam-mic vs Meta-headset-audio often share almost
  no sound (facecam RMS ~20× quieter) → expect no lock; that's why the manual nudge exists.
- **Preview proxies must be FULL length + frequent keyframes**: the preview is two `<video>`s
  slaved by playbackRate. A 45s proxy froze everything past 45s; sparse keyframes made the
  follower hard-seeks stall "in the same spots". `/api/proxy` now does the whole clip at
  `-g 15 -keyint_min 15 -sc_threshold 0` (keyframe ~every 0.5s) and the UI requests both
  sources at `seconds=0`. Render still uses the originals — proxy is preview-only.
- **Multiple clips pair by index** (gameplay[i] ↔ facecam[i]); render requires equal counts.
- **Tunnel is a flaky free quick tunnel** — URL changes on every restart (watchdog handles
  drops). Account has **no custom domain**, so no permanent named-tunnel URL is possible.
  Current URL is always in `current-url.txt`.
- **Windows cp1252 console**: test scripts printing emoji / `→` / `…` crash on
  `print` — `.encode("ascii","replace")` them. (Pipeline messages with these chars are
  fine in the UTF-8 web UI.)

## Tests (`.venv\Scripts\python test_*.py`)
`test_synthetic` (sync+render+replays, no server) · `test_multi` (multi-clip+bell) ·
`test_sync_onset` (onset lock on one clap) · `test_sync_layout` (sync preview, side-by-side,
no-shared-audio fallback) · `test_tracking`/`test_tracked_overlay` (CSRT) · `test_annotations`
(overlay) · `test_recording`/`test_import`/`test_channels`/`test_studio`/`test_multi_api`
(need the server running). **Tests that hit the API must `POST /login` first** (auth gate);
build synthetic clips at `test_synthetic.SR` (16000), not `sync.SR` (8000).

## Conventions
- PowerShell: `;` not `&&`; venv python is `.\.venv\Scripts\python.exe`.
- The user is **novice-friendly** — plain language, guided UX, no jargon dumps.
- Don't auto-commit; this isn't a git repo unless the user sets one up.
- Deps in `requirements.txt` (fastapi, uvicorn, numpy, faster-whisper, yt-dlp, pillow,
  opencv-contrib-python, requests; playwright is dev/test-only).
