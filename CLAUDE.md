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
- **`start-remote.bat`** → **`start-remote.ps1`** — **self-healing & idempotent** (safe to
  run any number of times): starts the server on :8765 only if it's down, and only recycles
  the Cloudflare tunnel (`tunnel_watchdog.py`) if the tunnel is actually dead — a healthy
  tunnel keeps its current URL. The watchdog self-restarts on drop/HTTP-530 and writes the
  live URL to `current-url.txt`. The Project Tracker "🚀 Use" button calls this (with
  `FIGHTSYNC_NOPAUSE=1`) and health-checks before opening, so the button brings FightSync
  back online no matter what state it's in.
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
  (sync→**composite-trim**→composite PiP/side-by-side→subtitles→replays), `_transition_card`
  (bell round card), `_title_card` (intro/outro). `_enc()` = shared x264/aac encode args.
  **Composite trim**: `cfg.trim_start`/`trim_end` (seconds on the aligned-overlap timeline,
  applied to pair 0 only — like `manual_offset`) shift `gs`/`fs` by `a` and shorten `out_dur`
  AFTER alignment, so video+audio+subtitle-window+replay-bounds all follow. The pipeline clamps
  `trim_end` to its own `out_dur`, so a UI value derived from preview-proxy durations can't
  overrun. (Subtitle extraction now keys off `fs`, not the auto `s.b_start` — fixes a latent
  manual-mode subtitle drift too.)
  **Which clip is the big one:** `cfg.swap_pip` (False default). `_build_segment` picks
  `big_in/small_in` from `[0:v]`(gameplay)/`[1:v]`(facecam): default = gameplay big +
  facecam corner; `swap_pip=True` = facecam big + gameplay corner (each keeps its own
  trim gs/fs). Applies to BOTH pip (bg/overlay) and sbs (L/R). UI: Composite → "Main
  (big) video" seg (`#mainSeg`, data-v 0/1 → `state.swap_pip` → `swap_pip` form field).
  The "Small clip corner/size" controls (`posSeg`/`sizeSeg`) act on whichever is the
  small one. (NOT yet wired into `_build_multicam_segment` — multicam stays gameplay-big.)
  `swap_pip` is verified correct end-to-end (HTTP render: swap=0 → gameplay big, swap=1
  → facecam big). If a user reports the toggle "inverted", the real cause is their clips
  loaded into the OPPOSITE upload boxes → `#swapZones` button (above the capture section)
  swaps `state.gameplay`↔`state.facecam` (whole lists, keeps pairs) + resets `lastSyncKey`
  so the aligner reloads. FastAPI Form bool parses "0"→False/"1"→True correctly (tested).
  **Polish/effects (Phase 2)** — all opt-in via `RenderConfig`:
  • `color_punch`/`punch_strength` → `eq`+`unsharp` on the composited video BEFORE subtitles
    (`vlabel` chain; don't shadow `s` from `compute_sync`!).
  • `music_path`/`music_volume`/`music_duck` → `_mix_music()` runs a FINAL pass on the
    concatenated `final.mp4` (so it spans intro/rounds/outro): `-stream_loop -1` the track,
    fade in/out, `sidechaincompress` (key = commentary via `asplit`) so music ducks under talk,
    `amix`, `-c:v copy`. Staged by `POST /api/upload_music`.
  • `hit_flash`/`hit_count`/`hit_text` → `_overlay_hits()` runs `detect_impacts` on the segment
    `main` then a `-vf` pass with `drawtext`+`drawbox` flashes at each hit (before replays, so
    replays show the graphic too).
  • `lower_third` → a banner on round 0's opening, appended into the composite `chain`
    (idx==0). **drawbox `w`/`h` = the BOX's own dims — use `iw`/`ih` for frame-relative pos**
    (this bit us); `drawtext` `w`/`h` ARE the frame. Commas inside quoted `enable=`/`alpha=`
    exprs are fine in filter_complex.
  • `transition_style` `card`|`flash` (flash = white `fade` open/close on the round card);
    title + transition cards share top/bottom accent `rules` + `vignette` for a cohesive look.
  **PER-ROUND multicam director (Phase 4)** — gameplay always the main screen; a SECOND live
    cam per round, switchable in the PiP at user-placed cut points, on EVERY round. RenderConfig:
    `cam_b_paths[i]` (round i's 2nd-cam clip; "" → normal PiP), `round_cuts[i]=[{t,cam}]` (cam
    0=facecam/A, 1=cam B), `cam_a_offsets[i]`/`cam_b_offsets[i]` (manual sync; None → auto via
    `compute_sync`). `render_multi` routes any round with a `cam_b_paths[i]` to
    `_build_multicam_segment(gameplay, angles, cuts, …)` — now takes angles/cuts as PARAMS (was
    cfg-globals), angles `[{path:facecam[i],offset:offA},{path:camB[i],offset:offB}]`, cuts
    mapped cam→angle. Legacy round-0 `multicam_angles` path kept as `elif`. **UI:** Step 1 has
    THREE upload zones — gameplay, **Webcam** (`dropF`/`facecam` = cam A), **Phone** (`dropCam2`/
    `cam2` = cam B, optional); `.grid2` is `auto-fit minmax(225px,1fr)` so they wrap.
    `swapRound`/`toggleRound` operate on all three tracks so reorder/skip keep rounds aligned. The
    legacy "Extra camera angles" (`#multicamSection`) is hidden (director supersedes it).
    `#directorSection` (Step 2) reads tracks via `enabledItems('gameplay'|'facecam'|'cam2')`;
    `state.roundCuts[i]`, `state.camAOffsets[]`/
    `camBOffsets[]`. `loadDirector(round)` auto-syncs each cam (`/api/sync` → `syncPair`), loads
    3 proxies (`dvG/dvA/dvB`), `dirPlay`/`dirTick` play all 3 in lockstep (gentle resync), per-cam
    ±0.1s nudge, `placeCut(cam)` drops a switch at the paused master time. `go` handler sends
    `cam_b_paths_json`/`round_cuts_json`/`cam_a_offsets_json`/`cam_b_offsets_json` when `state.cam2`
    is non-empty. Verified end-to-end (R1 PiP=camA, R2 PiP=camB over their own gameplay).
    **Decoder-limit fix:** when `enabledItems('cam2')` is non-empty, `checkSync` HIDES the
    single-pair clapperboard (`#previewSection`/`#syncSection`) AND releases its two `<video>`s
    (`removeAttribute('src')`+`load()`). Otherwise clapperboard(2) + director(3) = 5 simultaneous
    video elements blow past the browser's decode cap and the paused clapperboard ones go BLACK.
    In multicam mode the director IS the sync tool (auto-sync + play-all + ±0.1s nudge per cam),
    so the clapperboard is redundant anyway. `lastSyncKey=""` so the board reloads if cam2 is removed.
  **Legacy multicam (round-0 angles)** — `multicam_angles` (`[{path,offset}]`, index 0
    = primary facecam) + `multicam_cuts` (`[{t,angle}]`). When ≥2 angles, `render_multi` routes
    pair 0 to `_build_multicam_segment` (others stay normal). Alignment: each angle synced
    independently, so anchor `gs = max(angle offsets)` and each angle `start = gs − offset`
    (≥0); `out_dur = min(all)`; trim applies on top. `_cut_segments` tiles `[0,out_dur)` into
    (start,end,angle) spans (angle 0 fills gaps); each shown angle is one overlay with
    `enable='between(t,a,b)+…'` windows (PiP corner, or right half for sbs via `pad` base +
    `overlay=half:0`). Audio always = primary angle (no mic switching). No auto replays/subtitles
    in multicam v1. UI: `state.angles`/`state.cuts`; "Extra camera angles" section uploads each
    angle. **Adding an angle auto-calls `syncAngle(newIndex)`** so it loads into the player
    immediately (the old flow left it sitting in the list as "not synced" until you found the
    tiny "🔊 Sync" button — looked like "the angle isn't showing"). iPhone **`.mov` is HEVC**
    which browsers can't decode, so the angle (like the main clips) plays via `loadAligner`'s
    480p H.264 **proxy** (`ensureProxy`) — verified the proxy handles 10-bit `hvc1` + rotation
    metadata; expect a few seconds' delay while it builds. `syncAngle(i)` reuses the waveform
    aligner with `pv.angleSync` (offset stored back via
    `commitOffset`), `#btnDoneAngle` returns to main; cuts added at the verify `pv.master`
    playhead via `#cutButtons`. Render sends `multicam_angles_json`/`multicam_cuts_json`.
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
  **Manual slow-mo INSTANT-REPLAY BOX (Sync tab)** — the user's chosen design (sketch): for each
  marked window the main keeps playing **LIVE at 100%**, and a **top-right two-up inset** (gameplay |
  cam, equal 16:9 halves) of that window plays **slowed** (20–50%), appearing right after the
  knockdown. `pipeline.build_slowmo_replays(main, gameplay, cam, gs, fs, out_dur, regions, …)`:
  per region it trims gameplay `[gs+a,gs+b]` + cam `[fs+a,fs+b]`, slows each via `setpts/spd`,
  `hstack`s them into a red-bordered "REPLAY n%" box (~40% frame width, muted), then a final pass
  overlays each box on `main` PTS-shifted to the knockdown (`overlay=…:eof_action=pass`) — extending
  the main with `tpad=clone`+`apad` if a replay would run past the end (last frame held). Called in
  `_build_segment` (uses the facecam) and `_build_multicam_segment` (uses the primary angle `angs[0]`).
  `RenderConfig.slowmo_regions` is PER-ROUND (`slowmo_regions[i]` = composite-timeline `[{start,end,speed}]`)
  + `slowmo_speed` default; staged via `slowmo_regions_json`. (`apply_slowmo`/`_atempo_chain` — the older
  whole-frame retime — are still defined + tested but NO LONGER wired into the render.)
  **UI:** a 🐢 toggle (`[data-slow="pv"]` clapperboard verify, `[data-slow="dir"]` director) — tap once
  to arm (sets preview `<video>` `playbackRate` so you SEE it slow live + button `.armed` red-pulse
  "Slowing… tap to END"), tap again to close a `{start,end,speed}` window into `state.slowmoRounds[round]`
  (clapperboard→round 0 via `pv.master−pv.trimIn`; director→`dir.round` via `dir.master`). `resetSlow(ctx)`
  re-renders the removable chip list on `enterVerify`/`loadDirector`. Verified end-to-end: render
  extends 10s→11.7s and the red box is detectable in the top-right at the replay moment.
  **Result player is range-aware:** the done-screen `#preview` (and reel `#reelPreview`) play through
  `/api/media?path=<res.final>` (manual 206/Range), NOT `/api/download/{id}` (plain `FileResponse`, no
  Range → couldn't seek backward). Keep result playback on `/api/media`; `/api/download` is for the save button.
- **`cropdetect.py`** — motion-variance detection of the gameplay viewport (auto-crop).
- **`channels.py`** — yt-dlp channel live-detection (no API key) + `download_clip()`
  (Meta/Horizon import; tries cookies-from-browser for login-gated clips).
  **Meta-import gotchas (fixed):** (1) Meta's fbcdn CDN often omits Content-Length, so the
  old progress hook (percent only) NEVER fired → the import sat at a dead 0% with no name. Now
  `download_clip(..., on_progress, on_status)`: `on_status(msg)` always fires ("Downloading… N MB",
  "Merging audio + video…", "trying your edge login…") even when total is unknown; `_run_import_job`
  maps on_progress→percent, on_status→message. (2) The `outtmpl` used `%(id)s`, but for a DIRECT
  fbcdn link the "id" is the URL path and its `?_nc_cat=…` query string leaked into the filename
  (`…mp4？_nc_cat=…fbcdn.mp4` garbage) — now a clean `{name}-meta-{uuid10}.%(ext)s`. (3) Added
  `socket_timeout=30`+retries so a stalled CDN can't hang forever; the `cookiesfrombrowser` fallback
  (only hit when the public download fails) can still stall on a RUNNING browser's app-bound-encrypted
  cookie DB — the status now tells the user to close that browser. Proxy progress (`_run_ffmpeg_progress`)
  also shows "…Ns done" when a clip has no probe-able duration instead of a dead 0%.
  (Pre-existing `recordings/gameplay-meta-…？_nc_cat…fbcdn.mp4` files are old garbled-name imports —
  harmless, removable via the dropdown ✕.)
  **Horizon SHARE PAGES can't be imported (by design):** a `horizon.meta.com/shares/…` URL is an
  auth-gated SPA — the real video URL is fetched by the page's JS/GraphQL only after Meta login, and
  is NOT in the server HTML (confirmed: no `og:video`/DASH/`.mp4`; the lone `representation_id` is a
  player config flag; `facebookexternalhit` UA gets `og:title`+`og:image` thumbnail but no video). So
  no server-side resolver works. `download_clip` detects these URLs and raises a guide-the-user error
  ("open in browser → right-click video → Copy video address → paste the …fbcdn.net/…mp4 direct link").
  The links that DO import are those direct signed fbcdn `.mp4` URLs (time-limited but cookieless). UI
  hint + placeholder updated to say "direct video link, not a share page". A logged-in headless-browser
  resolver was considered but is unreliable (Edge app-bound cookie encryption + profile lock).
- **`tracking.py`** — OpenCV CSRT region tracking for the Form Studio.
- **`annotations.py`** — coaching overlay renderer (circles/arrows/text/zones/polygons;
  Pillow super-sampled RGBA frames piped into ffmpeg overlay; per-frame track offsets).
- **`static/index.html`** — main UI (multi-clip tiles, sync panel, side-by-side preview
  player, composite/extras/replays/transitions options, channels, recorder).
  **STEP WIZARD (layout):** the page is a 4-step wizard — `.wizstep#wstep1..4`
  (only `.on` is shown): **1 Add clips** (upload zones + swap + capture/Quest + channels),
  **2 Sync** (syncSection + previewSection clapperboard + multicam), **3 Style** (Composite +
  YouTube extras + transitions + replays), **4 Export** (#go + #prog + #done). `#wizNav`
  chips + `#wizFoot` Back/Next drive `goStep(n)`; advancing past step 1 is gated on having a
  gameplay AND facecam clip (`wizReady()`), message in `#wizMsg`. Sections keep their own
  JS-driven `display` toggles INSIDE their step (the wizstep container just gates the whole
  group) — so e.g. previewSection still auto-shows when clips load, becoming visible once you
  reach step 2. When adding/moving sections, keep them inside the right `.wizstep` wrapper.
  TODO from user feedback: richer intro/outro card builder (headers/subheaders/images), and a
  better tracker (keyframe-assisted, see tracking.py) — layout was done first.
- **`static/studio.html`** — Form Studio annotation editor (canvas over `<video>`).
  **🤸 AI body tracking (form-coach):** the old CSRT shape tracker (`tracking.py`/`/api/studio/track`)
  is unreliable; the Studio now has an **Auto-track body + form cues** button (`#poseBtn`) →
  `POST /api/studio/pose` → `_run_pose_job` shells out to a SEPARATE env
  `C:\Users\socia\form-coach\.venv` running `pose_coach.py` (MediaPipe **Tasks** PoseLandmarker —
  note: this build's `mp.solutions` is gone, must use `mediapipe.tasks`; needs `pose_landmarker.task`
  in that folder). It draws the 33-pt skeleton + first-pass cues (GUARD up/down, PUNCH detect+type
  STRAIGHT/HOOK, TURN/rotation %, HEAD slip + STANCE) and a punch count, then `run_ffmpeg` remuxes
  to browser-H.264 + the **original audio** (`-map 1:a:0?`); result loads back into the Studio player
  + downloadable. Runs ~real-time on CPU (no GPU needed). 100% body-track even with the VR headset.
  Kept in its own venv so FightSync's deps are untouched. Cues are tunable first-pass heuristics
  (2D single-cam); next steps: use MediaPipe 3D landmarks for rotation/head, pin
  annotations to joints (replace CSRT), point LocateAnything at the gameplay for the opponent.
  **Jitter+speed:** all 33 joints are **One-Euro filtered** (`OneEuro`, mincutoff 1.2/beta 0.3) →
  steady skeleton, punches still snap; processes at ≤720p (faster-than-realtime on lite). Model via
  `quality` arg/Studio checkbox: **lite** (`pose_landmarker.task`, default fast) vs **full**
  (`pose_landmarker_full.task`, "high accuracy", ~2-3× slower). `_run_pose_job(…, seconds, quality)`;
  pose_coach args = `in out max_seconds quality`. Engine logs (clearcut telemetry) silenced via
  `GLOG_minloglevel=3` + routed to `pose.log` (filtered from error msgs).
  **3D world-model analysis (accuracy pass, user feedback):** punch extension, elbow angle (straight
  vs hook), and stance now use MediaPipe `pose_world_landmarks` (`w3()`, metric 3D) instead of 2D image
  coords — a punch toward/away from camera is no longer foreshortened into a miss, and 2D jitter isn't
  mistaken for a punch (cut phantom punches 34→22 on the test clip). 2D `raw()` kept ONLY for the
  showcase highlight pixels. The SKELETON still draws in 2D. STRAIGHT/HOOK now by 3D `ea>158` alone
  (dropped the noisy 2D trajectory `align`). Stance EMA uses world-z (heavier 0.97/0.03).
  **Occlusion (no-freeze):** the old hard-HOLD froze occluded joints in place ("stuck point" — user
  disliked). Now a low-confidence joint is EASED `0.72*prev+0.28*new` toward the model (`prevP`) — keeps
  moving, won't snap to a wild guess or freeze; drawn AMBER. (Back arm/leg from ONE camera is a hard
  limit — 3D estimates it but can't truly see it.)
  **Conservative faults:** "guard down" only when the OTHER hand is CLEARLY below the shoulders
  (`>sh_y+0.16H`) AND confidently tracked (`conf>=VIS_TH`) at the punch — a tracking glitch on the guard
  hand mid-punch no longer triggers a bogus correction. "short" only if 3D ext<0.78. candidates.py +
  refs.py also switched to world landmarks so the TRAINER stays consistent with detection.
  **PER-BODY CALIBRATION (`calibrate.py` → `profile.json` in form-coach):** "📏 Calibrate to me" in the
  Studio → `POST /api/studio/calibrate` runs calibrate.py over a clip of the user boxing and saves
  *their* baselines (all ÷ shoulder-width): `guard_dy` (how high they hold guard), `stance` (foot
  width), `reach`. ROBUST: drops frames where shoulders foreshortened (turned sideways → shw→0 blows
  up ratios) by keeping shw within [0.62,1.5]× median; clamps reach to plausible; stance falls back to
  1.30 default if <8 ankle-visible frames (this webcam framing rarely shows legs). pose_coach loads
  profile.json and judges GUARD vs `guard_dy+0.18` and shows STANCE as ×(their normal) + "calibrated to
  you" badge. `GET /api/studio/profile` returns it. "Train it on my movements" = NOT the pose model
  (pre-trained) — it's this coaching layer (calibration done; next: save best reps as references,
  per-session trends, optional good/bad classifier on labeled clips).
  **AUTO-DIRECT (`autodirect.py`):** multicam director's **🎯 Auto-direct** button (`#autoDirectBtn`)
  → `POST /api/studio/autodirect(cam_a,cam_b,off_a,off_b,seconds)` runs pose on BOTH angles, scores
  each frame's view quality = mean `visibility` of key joints (nose/shoulders/elbows/wrists/hips),
  projects both onto the GAMEPLAY timeline (camTime+offX), and emits switch cuts to the clearer angle
  with hysteresis (`MARGIN=0.06`, `MIN_HOLD=1.2s`) so it doesn't flicker. Frontend sets
  `state.roundCuts[round]=cuts` (cam 0=A/webcam, 1=B/phone) → editable in the director (remove ✕ /
  add more). Requires both cams SYNCED first (`camAOffsets`/`camBOffsets` non-null). Runs pose on the
  full clips (slow on long rounds — background job w/ progress; lite model). Verified: with Cam A = an
  empty crop and Cam B = the person, it correctly cuts to cam 1.
  **PER-PUNCH GRADING + REFERENCES:** every detected punch is tracked to its PEAK (`pending`/
  `finalize_punch`) and graded — extension % (≈1.0 = full lockout), type (STRAIGHT/HOOK by elbow
  angle), and FAULTS: "short" (<0.82), "not straight" (straight w/ elbow<158°), "guard down" (the
  OTHER hand wasn't up at the punch). Flash shows e.g. "STRAIGHT 92% - guard down  88% of best";
  summary line adds `avg-ext` + a `faults: guard down x7,…` tally. **"🥊 Save best punches"**
  (`#refsBtn` → `POST /api/studio/refs` → `refs.py` → `reps.json` = 85th-pctile peak ext per type);
  pose_coach loads it and appends "% of best". `GET /api/studio/reps`. **CRITICAL detection fix:**
  punch detection reads the RAW wrist (`lm` direct), NOT the One-Euro-smoothed `P` — smoothing
  dampens the peak and dropped most punches (2 vs 21). Skeleton still draws smoothed; detection is
  raw, so pose_coach + refs.py now agree exactly (both 21 on a 15s clip). Occlusion HOLD also gated
  to `low_streak>=HOLD_AFTER(4)` frames so a fast punch's motion-blur dip isn't mistaken for occlusion.
  **PUNCH TRAINER (supervised, `candidates.py` → `punch_profile.json`):** raw detection over-counts
  (feints/reaches). Studio **🥊 Train punch detector** → 🥊 Find punches (`POST /api/studio/punch_candidates`
  → candidates.py lists every arm-extension w/ features `{t,side,ext,speed}`; speed=`(ext-prev_ext)*fps`
  = how fast it snapped out) → user ▶-jumps + UNCHECKS non-punches → **Learn** computes `min_ext`/
  `min_speed` = just below the weakest KEPT punch → `POST /api/studio/save_punch_profile`. pose_coach +
  candidates load `punch_profile.json` and `finalize_punch` discards events below those thresholds
  (defaults min_ext 0.82 / min_speed 0). Verified: training cut 21→5 punches. **Form Studio cleanup:**
  the legacy shape/marker/CSRT tools (`tracking.py`, `/api/studio/track`, burn) were ineffective — their
  cards are now `display:none` (IDs kept as hidden stubs so old JS hooks don't crash) and the draw
  canvas `#ov` is `pointer-events:none`. Studio is now pose/coaching-only: Auto-track + Calibrate +
  Save-best-punches + Train.
  **SKELETON EDITOR (rotoscoping, `posedata.py` → `corrections.json`):** "✏️ Edit skeleton" →
  `POST /api/studio/pose_data` (posedata.py writes per-frame `{fps,w,h,conn,frames:[[[x,y,vis]*33]]}`
  to the job dir; frontend fetches via `/api/media?path=`). The editor draws the skeleton on `#ov`
  FROM INSIDE the existing RAF `draw()` loop via the `window.editPaint` hook — the loop clears the
  canvas every frame, so a one-shot draw gets wiped (this was THE gotcha). Step frames w/ transport
  ⟨ ⟩, DRAG a joint (`pointerdown` grabs nearest ≤0.05; `pointermove` writes
  `edit.corr[frameIdx][jointIdx]=[x,y]` normalized; loop repaints; yellow=fixed). `💾 Save` →
  `POST /api/studio/save_corrections` → `corrections.json`; `GET /api/studio/corrections` reloads.
  pose_coach loads it and `gx/gy` override the landmark for `corr[str(f-1)]` (index aligns: posedata
  appends per read-frame 0-based, pose_coach `f` is pre-incremented so `f-1`); corrected joints forced
  vis=1. Honest framing GIVEN TO USER: this is MANUAL correction (fixes THIS clip + builds labeled
  data), NOT retraining MediaPipe (that needs thousands of labels + GPU). Keep segments short (`sec`
  box) — one JSON record/frame.
  **FORM SHOWCASE (broadcast freeze, pose_coach `show()`):** on a notable graded punch the render
  FREEZES ~0.9s on the punch's PEAK frame and pulses a translucent GREEN (good form) / RED (fix)
  overlay on the exact tracked body part — clean punch → punching arm green "GOOD FORM: TYPE NN%";
  fault → the offending part red ("FIX: GUARD DOWN" = the dropped OTHER wrist+elbow, "BENT ARM" =
  punching elbow, "SHORT PUNCH" = punching arm). `pending` stores `frame.copy()`+`P` at the peak;
  `finalize_punch` triggers `show()` (cooldown `SHOWCASE_GAP=fps*2.5` so it's not constant; good gated
  to ext≥`GOOD_EXT 0.92`). Freezes lengthen the output (15.6s vs 12s input — expected). Toggle:
  pose_coach 5th arg `1|0`, `/api/studio/pose` `highlights` Form param, Studio `#poseHi` checkbox
  (on by default). Verified visually (green arm on clean straights, red arm on guard-down).
  **Showcase v2 (user feedback fixes):** (1) highlight now drawn from RAW per-frame joints (`pend["raw"]`)
  on a CLEAN frame (`clean=frame.copy()` captured pre-draw) — NOT the lagging smoothed skeleton, so it
  lands on the real body. (2) DISTINCT OUTLINE via a limb mask + `findContours` → white(5px)+colour(2px)
  contour around a translucent fill (`fr[mask>0]=blended`). (3) Better STRAIGHT/HOOK: requires elbow
  `ea>160` AND radial travel (`align=cos(rad,travel)>0.55`, `w0`=start wrist) — a sweeping/bent punch =
  HOOK. (4) "GOOD" is CONSERVATIVE: only a clean STRAIGHT w/ ext≥0.92 + guard up (never labels a hook
  "good form" — 2D can't grade hook quality). Faults reduced to guard-down + (straight-only) short.
  Still heuristic 2D — type/grade can err; future: extend the punch TRAINER to label TYPE, or hook-specific
  (rotation/bent-at-impact) grading.
  **STANCE → JAB vs STRAIGHT (pose_coach 6th arg `STANCE` auto|orthodox|southpaw):** a jab and a cross
  are the same motion from different hands, so the label needs the LEAD hand. orthodox=LEFT lead,
  southpaw=RIGHT lead. `disp` = lead-hand STRAIGHT→"JAB", rear-hand STRAIGHT→"STRAIGHT", else HOOK
  (used in flash/showcase/summary; `type` stays STRAIGHT/HOOK for reps/good-form). AUTO reads stance from
  MediaPipe `lm.z` depth (lead side is bladed toward cam → smaller z; shoulders + ankles-if-visible,
  EMA `stance_ema` 0.96/0.04 so it's stable but can switch mid-clip). HUD shows ORTHODOX/SOUTHPAW; the
  foot-width readout renamed STANCE→FEET to avoid the clash. `/api/studio/pose` `stance` Form param,
  Studio `#poseStance` dropdown (auto default). Verified: same L straight = JAB(orthodox)/STRAIGHT(southpaw).
  Honest caveat to user: z-stance from one 2D cam isn't perfect (camera not always at the opponent's
  angle) — HUD shows the call; set the dropdown manually if auto is wrong (most boxers don't switch).
  (LocateAnything-3B model was DELETED 2026-06-26 — never used by FightSync, it's an object
  detector not a pose tool. The `C:\Users\socia\locate-anything` FOLDER stays: its CUDA `.venv` +
  YOLO11-pose weights + scripts (rvm_matte.py, gpu_pose.py, gpu_finetune.py) power cutouts + GPU
  tracking. See the GPU body-tracker note below.)
- **`tunnel_watchdog.py`** — keeps the quick tunnel alive; `current-url.txt` = live URL.

## Render pipeline (per clip pair)
sync (onset) → composite (PiP `[bg][pip]overlay` **or** side-by-side `hstack`, both
halves equal) + audio mix → optional subtitles (faster-whisper) → optional slow-mo
replays → segment. Then: intro card + (segment + bell transition)… + outro → one concat.

## Endpoints (all behind the password gate)
`/login`, `/`, `/studio`, `/favicon.svg`. `POST /api/sync` (fast audio-only preview),
`/api/render` (multi-clip job; `gameplay_paths_json`/`facecam_paths_json` arrays),
`/api/upload` (accepts optional `label` → persisted in `recordings/labels.json`),
`/api/upload_recording` (webm→mkv remux), `/api/upload_music` (music-bed track),
`/api/rename` (`path`+`label` → persist a user-given clip name in `labels.json`),
`/api/capture/folders` (the 3 OneDrive source folders + count + next totfN name),
`/api/capture/upload` (`target`=cam1|cam2|gameplay + `file` → save into that folder as next totfN.<ext>; sync def/threadpool),
`/api/quest/status` (Quest USB connected? videoshots count, imported count, last), `/api/quest/import` (kick a background import now),
`/api/library` (each item carries `title` = the user's name, falling back to auto-derived),
`/api/import_url` (yt-dlp),
`/api/proxy` (480p H.264 preview proxy, `seconds` window), `/api/media` (range/206),
`/api/waveform?path=` (per-bucket audio peaks for the sound-wave sync aligner; cached by path+mtime),
`/api/status/{id}` (carries `result.metadata` = YouTube title/description/tags/chapters),
`/api/download/{id}` (honors `result.download_name`), `/api/subtitles/{id}`,
`/api/metadata/{id}` (downloads `youtube.txt`), `/api/shorts/{id}` (start reel job),
`/api/youtube/state`, `/api/youtube/upload/{id}` (start upload job),
channels (`/api/channels…`, `/api/channels/{id}/live`),
crop (`/api/detect_crop`,`/api/apply_crop`), studio (`/api/studio/track|render|project`).

## Capture organizer (`capture.py`)
Sorts fight clips into the user's OneDrive **Camera Roll** source folders with
ordered names `totf1, totf2, …` (TOTF = Thrill of the Fight; per-folder numbering =
highest existing `totfN` + 1, so capturing in order aligns the same fight across
folders). Base = `%OneDrive%\Pictures\Camera Roll`; targets `cam 1 Phone` (cam1,
phone), `cam 2 PC` (cam2, PC webcam), `gameplay`.
- **Mobile upload:** `#captureSection` (top of the page, above sync) — tap a folder
  tile, choose a video → `POST /api/capture/upload` → `save_totf()` reserves the
  number with a `touch()` under a lock, then streams bytes. Lets the user push phone
  clips to the PC over the tunnel with no cable. `loadCapture()` shows each folder's
  count + next name and refreshes after each upload.
- **Camera-app auto-naming:** the Windows Camera app hard-codes `WIN_<timestamp>.mp4`
  and has NO folder/name setting — so a daemon thread (`capture.watch()`, started in
  `app.py` `__main__`) polls `cam 2 PC` every 3s and renames new finished videos
  (size-stable, not already `totfN`, sorted by mtime) to the next `totfN`. It also
  re-pins the folder name: if Windows re-stamps the system "Camera Roll" display name
  (`desktop.ini` → `windows.storage.dll`), it rewrites `LocalizedResourceName` to the
  real folder name. Saving location itself is the registry known-folder redirect (see
  the Camera Roll memory), independent of `desktop.ini`.
- Watcher only runs while the server runs (fine — start-remote keeps it up); it
  batch-renames any pre-existing `WIN_*` on startup. Scoped to `cam 2 PC` only;
  cam1/gameplay get `totfN` names directly from the upload endpoint.

## Quest USB auto-import (`quest.py`, needs pywin32)
The Quest connects over USB as an **MTP device — NO drive letter**, so its files are
ONLY reachable via the Windows shell namespace (`Shell.Application` through pywin32),
never `os`/`shutil`/`Path`. Also **MTP reports `Size=0`**, so you can't gauge a
file's size before copying. Mechanism:
- Navigate `This PC`(`NameSpace(17)`) → a child whose name contains "quest"/"oculus"
  → `Internal shared storage` → `Oculus` → `VideoShots` (each step = match child by
  name, `.GetFolder`). `_device()`/`_videoshots()`.
- Copy with `stagingFolder.CopyHere(item, 1556)` (4|16|512|1024 = no dialog/yes-to-all)
  into `quest_staging/`. CopyHere is **async** → `_wait_stable()` polls the staged
  file until its on-disk size settles (since MTP Size=0 pre-copy). Then `shutil.move`
  it into the gameplay folder as the next `totfN` (under `capture._lock`). COPIES,
  never deletes — originals stay on the headset.
- Already-imported source names tracked in `quest_imported.json` so nothing copies
  twice. A daemon poller (`start_poller`, every 12s) checks for the device and imports
  new VideoShots when plugged in; `/api/quest/import` triggers it manually.
- **COM threading:** every entry point that touches the shell (`status_live`,
  `import_new`, the poller loop) does its own `pythoncom.CoInitialize()`/`CoUninitialize()`
  — FastAPI sync endpoints run in threadpool threads that aren't COM-inited otherwise.
  `import quest` is wrapped in try/except in app.py so a missing pywin32 just disables
  the feature (`quest_mod=None`) instead of killing the server.
- UI: `#questRow` in the capture section shows connected/▪count and an "Import now"
  button; polled every 8s so plugging in is noticed without a refresh.

## VS fighter-intro (motion graphics, `vs_intro.py`)
Animated "tale of the tape": two full-height STEEL corner panels (red left / blue right) meet at a
DIAGONAL symmetrical seam, drift together slowly (magnetic, `_magnet`=t⁴) then SLAM with a white
flash + screen-shake + sparks flying along the seam (two bursts, linger ~1s) + a VS badge. Rendered
frame-by-frame with Pillow → ffmpeg (silent `anullsrc` track so it concats cleanly). Brushed-steel
base (numpy), plate seams, rivets, bevels; all of a fighter's text is their corner colour with
letter-spacing (`_stext` tracking). Each fighter `{name,elo,height,style,logo_text|logo, photo}`;
`photo` (RGBA) crops-to-fill its slot (`_fighter_fig`), else a boxer-silhouette placeholder
(`_boxer`). **Fighter auto-capture** (`grab_fighter.py`, form-coach): samples ~1 frame/s across the WHOLE clip,
scores each (pose visibility × size × IN-FRAME, full-body a bonus not a gate so webcam/phone
upper-body works), cuts the best one out with the MediaPipe **segmentation mask**. **Crop comes from
the MASK extent (squeeze 3D→2D first), NOT the landmark box** — the landmark box topped out at the
nose and chopped the head; mask extent + extra TOP padding keeps the full head/hair, and frames whose
mask touches the top edge are penalised (head-cutoff avoidance). `/api/fighter/grab` `who`=opponent
(from gameplay) | me (from webcam/facecam or phone/cam2 → saved as `me.png` + into `profile.json`).
**Cycling reels (`grab_clips.py`):** instead of a static photo, each corner can play a HIGHLIGHT REEL
— finds N (~4) best spread-out ~2.5s windows (pass 1 scores frames; pass 2 cuts the fighter out per
frame, locks the crop on the window's 1st frame, cover-fits to the panel slot, fades each clip in→out,
skips frames where the person left the crop) → writes an RGBA PNG sequence + `meta.json {fps,n,pw,ph}`
to a reel dir. `vs_intro.render_vs_intro(..., left_reel=dir, right_reel=dir)`: `_load_reel`, `_panel(...,
no_figure=True)` (skip the static figure), HOLD auto-extends to the longest reel, and each render frame
pastes the cycling cutout `frames[int(t*reel_fps)%n]` into the panel slot (REEL_FX_L=180, moves with the
panel's slide). ENGINE DONE + demo'd (you-from-webcam + opponent-from-gameplay both cycling). Reel slot FILLS the card
(cover-fit 470×700, feet-anchored) and is CLIPPED to the steel wedge (`ImageChops.multiply` with a
precomputed slot mask) so any cutoff lands on the card border (unnoticeable); the text is a separate
overlay drawn ON TOP. **Clean cutout (`clean_alpha` in grab_clips/grab_fighter/cutout):** mask>0.5 →
`MORPH_CLOSE`(7) fill holes → `erode`(3,×2) trim the ~2px anti-aliased background fringe → `GaussianBlur`(3)
soft edge — kills the background bleed without cutting real body parts (replaced the soft `mask*1.15`). TODO: the
in-app wiring — a `/api/fighter/grab_clips` endpoint, reel passthrough in `/api/vs_intro` + render config,
and a Style-tab "video reel" toggle per fighter (currently the app uses the static photo path). **User photo** upload+bg-cutout (`cutout.py`) →
`/api/fighter/photo`, persisted in `fighters/` + `fighters/profile.json` (`/api/fighter/profile`
get/save, reused across videos). **Render:** `/api/vs_intro` previews the clip; the main render
auto-prepends it when enabled — go-handler sends `vs_intro_json`, `_run_job(...,vs_cfg)` →
`_prepend_vs_intro()` renders at the final's W/H/fps and `concat`s (scale+audio-safe) in front.
Studio... no — **Style tab** `#vsIntroSection`: on/off, both corners' fields, colour pickers, my-photo
upload, opponent grab button, preview. Colours via `_hex_rgb`.

## Projects (`sessions/current_project.json`)
The "latest" working state (latest.json + .subs/.subs.meta/.studio) is GLOBAL — it used to bleed
across videos. A **project** = the full video you're on. Header has a project-name input (`#projName`)
+ **🆕 New** (`#newProjBtn`). `GET/POST /api/project/current|rename|new`. **`new`** ARCHIVES the current
latest.* as a named saved session (so the old video is recoverable via Resume), then WIPES
latest.json/.subs.json/.subs.meta.json/.studio.json → clean defaults, and stores the new name; the
client also clears `localStorage fs_ui` + reloads. The project name drives `localStorage fs_fightLabel`
(clip-name prefix) and the final render's `download_name` (`_run_job` → `_safe_name(_project_name())+".mp4"`).
session_list skips current_project.json + .subs.meta.json. Presets = a LATER ask (user wants to get
comfortable first). NOTE: if a user reports "old captions/clips from a previous video," they just need to
hit 🆕 New (or it means latest.* still holds the prior project).

## Saved synced sessions (`sessions/`)
Once clips are synced, the whole set persists so the user never re-uploads+re-syncs (and so the
real footage + offsets are reachable without redoing it). `buildSession()` (index.html) assembles a
manifest — per-round `{gameplay,facecam,cam2 paths, offset, camA/camB_offset, trim, cuts}` + the raw
`tracks` (with enabled flags) + the per-round arrays — and `POST /api/session/save` writes
`sessions/<id>.json` AND mirrors `sessions/latest.json` (always the newest synced set, easy to read).
**Auto-saves** (debounced 1.5s) on every `commitOffset` (clapperboard + director cam sync) via
`maybeAutoSave()`; manual **💾 Save synced session** / **📂 Load** in `#syncSection` (`sessSaveBtn`/
`sessList`/`sessLoadBtn`). `restoreSession()` repopulates `state.{gameplay,facecam,cam2}` + the
offset/trim/cut arrays + resets `lastSyncKey` so the clapperboard reloads — synced, no re-work.
`/api/session/list` (newest first by mtime), `/api/session/get/{id|latest}`. Clips themselves live in
`recordings/` (already persistent); the manifest just records paths + sync data. **For two-cam fusion:
read `sessions/latest.json` to get the webcam+phone paths + camB offset without the user re-uploading.**

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
- **Different-length clips (short phone cam) — black-pad the proxy for sync:** when one clip ends
  early (e.g. a phone cam ended while gameplay kept recording), the length mismatch broke the sync
  tools (nothing to scrub/align in the gap). Fix: `/api/proxy` takes `pad_match` (comma-sep sibling
  paths); the server probes them and if any is longer, the proxy is built with
  `tpad=stop_mode=add:stop_duration=…:color=black` (CPU filter → software path only when padding) so
  the SHORTER clip's proxy gets a real black tail to the longer length. `_proxy_path` cache key
  includes `pad_to`. Client: `ensureProxy(path,onProgress,padMatch)`; `loadAligner` pads each of the
  pair to the other (`loadOne(...,sibling)`), `loadDirector` pads each of the 3 angles to the trio's
  max. **Preview/sync ONLY** — the render still uses the original clips (no black in the final video);
  out_dur is still min-overlap, the padding just gives the UI a full timeline to manipulate.
- **Browser preview can't decode iPhone HEVC/4K** (stutters ~2fps) → the preview plays a
  cached **480p H.264 proxy** (`/api/proxy`, `-hwaccel auto` + software fallback). The
  **render always uses the full-quality originals** — proxy is preview-only.
  - **10-bit gotcha (was the iPhone-angle "black screen, plays in VLC" + "play both ends
    instantly" bug):** iPhone HEVC is **10-bit**, and libx264 preserves source bit depth, so
    the proxy came out **H.264 High 10 / yuv420p10le — which NO browser can decode** (black
    video, ~0 duration). Fix: proxy forces **`-pix_fmt yuv420p`** (8-bit). The proxy cache key
    (`_proxy_path`) has a **version tag** (`ver="v2"`) — bump it whenever the proxy encode
    changes so stale cached proxies on disk are invalidated and regenerated.
  - **Independent per-clip loading:** `loadAligner` loads pvG and pvF **separately** (each
    `ensureProxy().then(set src)`), NOT `Promise.all` — otherwise one slow proxy (a big 4K
    phone clip) blacks out BOTH screens while it builds ("both black"). Each slot primes +
    enables when its own proxy is ready; lock unlocks when both (`rdy.G && rdy.F`). Clears old
    `src` first so a still-loading slot shows blank, not the previous clip's frame.
  - **GPU proxy + progress:** `_run_proxy_job` tries 3 encode paths in order — (1) full GPU
    `-hwaccel cuda` NVDEC decode + `h264_nvenc` encode (≈10× faster: a 30s 1080p HEVC proxies
    in ~2.5s vs many seconds software), (2) `-hwaccel auto` decode + libx264, (3) all software.
    `_run_ffmpeg_progress` parses ffmpeg `-progress pipe:1` (`out_time_us`/duration) into a live
    `percent` on the job; `ensureProxy(path, onProgress)` polls it and `loadAligner` drives a
    per-slot bar (`#proxG`/`#proxF`, `setProx()`). nvenc only kicks in where it works (NVIDIA);
    elsewhere the loop falls through. Bump `_proxy_path` `ver` when the encode changes (now v3).
- **Sync is MANUAL by default now** (user request — auto audio-sync was unreliable for their
  footage: iPhone-facecam-mic vs Meta-headset-audio share almost no correlatable sound, facecam
  RMS ~20× quieter, so it never locked). The UI no longer calls `/api/sync`; the preview starts
  start-aligned (offset 0) and the user sets the offset with click-to-select + nudge controls.
  The frontend ALWAYS sends `manual_offset` for pair 0, so `_build_segment` uses it verbatim.
  Auto onset-sync (`compute_sync`) still exists and runs for pairs 2+ in multi-clip renders,
  with the low-confidence(<0.20)→start-align guard (using the raw low-conf offset misaligns
  synchronous clips by tens of seconds — that was a real bug). `/api/sync` endpoint is unused
  by the UI but kept. Offset convention: `gs=max(off,0), fs=max(-off,0)`; facecam-later→off−,
  gameplay-later→off+.
- **Preview = CLAPPERBOARD sync** (user-chosen UX after several iterations). Two paused 480p
  proxies (`#pvG`/`#pvF`, `/api/proxy` full-length, cached) side by side, each with its OWN scrub
  bar + frame-step buttons (`data-stepg`/`data-stepf`; |step|<0.5 ⇒ frame = step/fps, else seconds).
  MARK phase: line up the same real instant (a punch). `#btnLock` switches to VERIFY phase
  (`.markonly`↔`.verifyonly`). **VIDEO POSITIONS ARE THE SOURCE OF TRUTH.** Most people sync by
  just **play/pausing each clip** to the same frame — NOT by scrubbing or dragging the wave. So
  both commit buttons (`#btnLock` and the wave's `#waveUse`) call **`commitAlign()`**, which
  `pauseBoth()` then sets `pv.offset = gT − fT` from the live video `currentTime`s — UNLESS your
  last action was a sound-wave drag (`pv.lastAlign==="wave"`), in which case it trusts the wave
  offset. `markCouple()` also keeps `pv.offset` live as you go, listening on **`seeked` AND
  `pause`** (play→pause changes `currentTime` via playback and fires `pause`, NOT `seeked` — that
  was THE bug: the old coupling only watched `seeked`, so play/pause lining-up never registered
  and the commit saved 0). `markCouple` is gated to `pv.phase==="mark"` && `pv.coupleArmed` (false
  in `loadAligner`, true in `ready()`) so load-time events don't clobber a wave drag; it sets
  `pv.lastAlign="video"`, the wave drag sets `"wave"`, `loadAligner` resets to `null`.
  **`#waveUse` must NOT early-return when `pv.wfG` is null** (clips with no audio have no
  waveform) — people line up with the videos, so it always commits. Sign note: dragging the
  facecam strip is `+offset`, the gameplay strip `−offset`.
  VERIFY phase: one `#masterSeek` bar drives BOTH at the locked offset (`gs=max(off,0),
  fs=max(-off,0)`) so you scrub-check alignment; `#btnRemark` returns to MARK. NO playback, NO
  rebuild, NO simultaneous dual playback anywhere (all the prior jitter sources are gone — seeking
  paused frames is smooth). Render gets `manual_offset=pv.offset`.
  - **Sound-wave aligner** (`#waveCanvas`, MARK phase — the FASTEST sync method): two stacked
    waveform strips (🎮 gameplay top, 🤳 facecam bottom) from `GET /api/waveform` (per-bucket
    peaks via `media.waveform_peaks`, peak-normalised PER CLIP so the ~20× quieter facecam mic is
    still visible). A shared clap/landed punch spikes on BOTH even when the mics are too different
    to cross-correlate — so the user DRAGS the facecam lane until its spike sits under a gameplay
    spike. Math: facecam time `tf` is drawn at gameplay-time `tf+offset`, so dragging right by Δpx
    does `pv.offset += Δpx/pps`; dragging the top lane pans the view (`pv.wview`); a zoom slider
    sets `pps = wbasePps*wzoom`.
    **Dead-zone bug (fixed):** the top (gameplay) lane used to be "pan" and the bottom (facecam)
    lane "offset". But at default zoom the whole clip is visible so `waveClampView` pins `wview`
    to 0 → panning does NOTHING → dragging the top strip to line up did nothing, so `#waveUse`
    committed offset 0 ("goes back to default"). Now: drag EITHER strip to set the offset
    (`mode==="offset"` facecam `+dx`, `mode==="goffset"` gameplay `−dx`); top-strip PAN only
    engages once zoomed in (`pv.wzoom>1.01`). `#waveUse` rounds `pv.offset` and calls the shared `enterVerify()`
    (so verify fine-tune + Play-both confirm it). Loaded by `loadWaveforms()` from `loadAligner()`;
    all wave state lives on `pv.w*` and draw/drag guard on `pv.wfG`. Wave code is a hoisted-fn +
    `wireWave()` IIFE block placed AFTER `pv` is declared (TDZ). Canvas needs `touch-action:none`.
  - **Optional auto-sync** (`#btnAuto`, MARK phase): POSTs `/api/sync` (the kept onset-sync
    endpoint) on the ORIGINAL paths, sets `pv.offset` to the detected value, and `enterVerify()`s
    with a confidence note (honest "probably wrong" warning when `!ok`). From VERIFY you can
    **fine-tune** (`[data-fine]` ±0.1/±1s buttons → adjust `pv.offset`, re-seek, no re-mark) or
    `#btnRemark` to mark by hand. `enterVerify()` is shared by lock + auto.
  - **Auto-sync ALL rounds at once** (`#btnAutoAll`, top of `#syncSection` — NOT buried in the
    clapperboard, per user request 2026-06-26): one `/api/sync` call with the FULL enabled gameplay/
    facecam arrays → stores each `pairs[i].offset` into `state.roundOffsets[i]`, `renderRoundTabs()`
    (✓ marks), `maybeAutoSave()`, and if the clapperboard is open `enterVerify()`s the current round.
    Reports "N solid, K low-confidence (round … — check by hand)". The per-round `#btnAuto` stays as a
    single-round refinement. (User's footage audio barely correlates → most rounds come back low-conf;
    that's expected — the button flags them instead of committing a bad lock.)
  - **Per-clip TRIM bars** (MARK phase, `#gTrimBar`/`#fTrimBar` canvases under each clip): drag the
    green start/end handles to cut each clip's dead air. Replaced the old confusing `[data-trimset]`
    "⟦ start/end ⟧" buttons (which only fed auto-sync — useless in the manual default). Now they
    drive the FINAL OUTPUT: `applyClipTrims()` folds both clips' `[a,b]` windows through the sync
    offset into the composite trim via the pure `compositeTrim(gFull,fFull,gA,gB,fA,fB,off)`
    (gameplay=gs+t/facecam=fs+t, output = intersection of kept windows; `gs=max(off,0)`,
    `fs=max(-off,0)`; `ts=max(0,gA-gs,fA-fs)`, `te=min(outDur,gB-gs,fB-fs)`) → sets `pv.trimIn/Out`
    + `state.roundTrims[pv.round]` → existing render path. `enterVerify` recomputes (guarded: only if
    per-clip trims set, so a verify-strip-only trim isn't clobbered). State still `pv.gTrim`/`pv.fTrim`
    `{a,b}` (b=null → full), readout `updSyncTrim(clip)`. Bars draw on `loadedmetadata` +
    IntersectionObserver (visible) + a `loadAligner` setTimeout. `window._compositeTrim` = test hook.
    (`#btnAuto` auto-sync still sends `g_start/g_end/f_start/f_end` from the same `pv.gTrim/fTrim`.)
  - **Play-both** (`#pvPlayBoth`, VERIFY phase): plays BOTH proxies together at `pv.offset`
    (`playBoth`/`pauseBoth`/`bothTick`). Gentle resync only (`bothTick` seeks the follower if
    drift>0.15s — NOT per-frame playbackRate, which was the old jitter). Fine-tune buttons work
    WHILE playing (the next tick re-seeks the follower to the new offset, so you watch the facecam
    shift live). Scrubbing the master bar / Re-mark call `pauseBoth()` first. Two simultaneous
    480p decodes may be a little choppy on weak mobile — acceptable for a pre-render check.
  - **Trim strip** (`#trimCanvas`, VERIFY phase): cut dead air on the composite timeline
    `0..pv.dur`. Draws the gameplay waveform (`pv.wfG`) windowed to the aligned region `[gs,
    gs+dur]`; two green edge handles set `pv.trimIn`/`pv.trimOut` (the nearest handle to the
    click is grabbed); the `#masterSeek` playhead is mirrored on it, and ⟦/⟧ buttons snap a
    handle to the playhead. Render only sends `trim_start`/`trim_end` when a handle actually
    moved (`initTrim()` resets to full each `enterVerify()`). `drawTrim()` is also called from
    `masterSeek`/`bothTick` so the playhead tracks. Hoisted fns + a `wireTrim()` IIFE (after `pv`).
  - **Dedicated per-clip move sliders** (`#seekG`/`#seekF`, MARK phase, above each clip's frame-step
    row): each scrubs ONLY its own video (`wireClipSeek(vidId,sliderId)` — input→`seekVid` that one +
    `pauseBoth`; video→slider value guarded by a `dragging` flag to avoid feedback). The native
    `controls` stay (for play); these are the clear "line up each clip independently" control the user
    asked for. They fire `seeked`→`markCouple` so the offset stays live. (VERIFY's `#masterSeek` still
    drives BOTH at the locked offset — that's intentional, for confirming the lineup.)
  - **Black-frame fix (critical):** a paused `<video>` that has NEVER played shows a BLACK frame
    on many mobile browsers even after seeking. So (a) the two videos carry native `controls` in
    MARK phase (the native scrubber reliably paints frames as you drag, every device), and (b)
    `prime(v)` does a muted `play()→pause()` on load to "activate" them so programmatic seeks
    (frame-step buttons, master bar) actually paint. In VERIFY phase `controls` is toggled OFF so
    the master bar is the sole driver. Don't remove the priming — without it the preview is black. History of rejected designs:
  dual-HEVC-follower (jittery) → single combined `/api/preview` clip (smooth but no live feedback)
  → dual-proxy live-nudge aligner (jittery on mobile playback) → clapperboard (current). The
  single-file `/api/preview` + `build_preview_clip` approach was removed (clapperboard won).
  Keep horizontal-overflow guards (mobile zoom-out): body
  `overflow-x:hidden`, `.pvcol video{min-width:0;max-height:40vh}`.
- **Mobile: prevent horizontal overflow** (it makes the browser zoom out → "everything tiny, top
  section huge"). `body{overflow-x:hidden}`, preview videos `flex:1;min-width:0;max-height:42vh`,
  `#pvPlayer{max-height:55vh}`, `.pvnudge` wraps. Verified docW==winW at 390px.
- **PER-ROUND manual sync (clapperboard round tabs)** — multi-round gameplay+webcam used to only
  let you sync round 1 (clapperboard previewed pair 0; rounds 2+ fell back to the unreliable
  onset auto-sync). Now `#roundTabs` (`.dirtabs`, shown when >1 enabled pair) lets you pick each
  round; `loadRound(r)` loads that pair, restoring `state.roundOffsets[r]` (✓ on synced tabs).
  `commitOffset` saves the on-screen round's offset; `saveRoundState()` (called on tab-switch +
  at render) also stashes that round's verify-phase trim into `state.roundTrims[r]` ({in,out}),
  and `initTrim` restores it. Render payload sends `manual_offsets_json` (per-round offset, null →
  auto that round) + `round_trims_json`; `RenderConfig.manual_offsets`/`round_trims` (per-round),
  consumed in `render_multi` via `_round_off` with round-0 fallback to the legacy
  `manual_offset`/`trim_start`/`trim_end`. Verified end-to-end (round1 offset −1.5 shortened its
  segment to 6.5s; round0 trim [1,6]→5.0s; UI stores+sends `[1.2,-0.8]`). NOTE: the segment
  `offset` field in the result is the AUTO-detected value (diagnostic), NOT the applied manual
  offset — check durations to confirm a manual offset took.
- **Render robustness:** `render_multi` emits `progress(2,"Preparing…")` immediately so a slow
  first round doesn't sit at "Queued… 0%" (a real-4K render spends real time in probe+sync+composite
  BEFORE the first per-segment progress — that looked like a 0% hang). `probe()`/`_measure_duration`
  now have `timeout=60`/`180` so a pathological clip errors cleanly instead of hanging the job.
  Heads-up: restarting the server (for a .py change) KILLS any in-flight render (in-memory `JOBS` +
  daemon thread) — its half-built `main_*.mp4` stay in `jobs/<id>/work` with no `out/final.mp4`.
- **Multiple clips pair by index** (gameplay[i] ↔ facecam[i]) = a "round"; render requires equal
  ENABLED counts. Each clip item has ↑/↓ (reorder) + a ✓/🚫 skip toggle + ✎/×. Reorder (`swapRound`)
  and skip (`toggleRound`) act on the WHOLE round (both lists' index i together) so pairs stay
  intact; `×` still removes a single clip (to fix a mismatch). `enabledItems(name)` (= `enabled
  !==false`) is the source of truth: `checkSync` previews the first ENABLED pair and the render
  payload (`go` handler) filters to enabled, so the manual offset/trim on "pair 0" always matches
  the first enabled round. `addItem` sets `enabled:true`. `window._state` is exposed for tests.
- **Tunnel is a flaky free quick tunnel** — URL changes on every restart (watchdog handles
  drops). Account has **no custom domain**, so no permanent named-tunnel URL is possible.
  Current URL is always in `current-url.txt`.
- **Empty-terminal popups (fixed):** `tunnel_watchdog.py` runs under console-less `pythonw`,
  so spawning console-mode `cloudflared.exe` made Windows allocate a **new empty terminal
  window** on every (re)start — and quick tunnels recycle often, so windows kept appearing.
  Fix: `subprocess.Popen(..., creationflags=subprocess.CREATE_NO_WINDOW)` in `spawn()`. ALSO
  watch for **duplicate watchdogs** (e.g. one started with venv python + one with system
  python) — each runs its own cloudflared and doubles the churn; keep exactly ONE. Note the
  venv `pythonw.exe` is a launcher **stub**: stub + child interpreter = ONE logical process
  (same PID-pair pattern as `python.exe`), NOT a duplicate.
- **Naming clips on upload:** `addFiles()` `prompt()`s for a name per file (default `Round N
  <filename>`) and sends it as `label` to `/api/upload`, which persists it in
  `recordings/labels.json` (filename→name). `/api/library` reads that file so saved-upload
  dropdowns show the user's names across reloads; the ✎ rename also POSTs `/api/rename` to
  update `labels.json`. Deleting clips: the videos live in `recordings/` (+ `proxies/` and
  `previews/` caches) — `labels.json` is just a sidecar and can be left or cleared.
- **Windows cp1252 console**: test scripts printing emoji / `→` / `…` crash on
  `print` — `.encode("ascii","replace")` them. (Pipeline messages with these chars are
  fine in the UTF-8 web UI.)

## Tests (`.venv\Scripts\python test_*.py`)
`test_synthetic` (sync+render+replays, no server) · `test_multi` (multi-clip+bell) ·
`test_sync_onset` (onset lock on one clap) · `test_sync_layout` (sync preview, side-by-side,
no-shared-audio fallback) · `test_tracking`/`test_tracked_overlay` (CSRT) · `test_annotations`
(overlay) · `test_recording`/`test_import`/`test_channels`/`test_studio`/`test_multi_api`
(need the server running). Phase-1/2 additions (no server): `test_waveform` (peak extractor) ·
`test_trim` (composite trim shortens output) · `test_punch` (colour punch) · `test_music`
(music bed loop+duck) · `test_hits` (impact overlay) · `test_titles` (title cards + lower-third) ·
`test_multicam` (multi-angle switch composite + cut tiler) · `test_slowmo` (legacy whole-frame
retime) · `test_replaybox` (top-right two-up instant-replay box overlays + extends the body).
Headless UI (server running):
`test_waveform_ui` (sound-wave sync + trim handles), `test_arrange_ui` (round reorder + skip),
`test_upload_ui` (Choose-file button), `test_multicam_ui` (add angle, per-angle sync, cuts).
**Tests that hit the API must `POST /login` first**
(auth gate); build synthetic clips at `test_synthetic.SR` (16000), not `sync.SR` (8000).

## Conventions
- PowerShell: `;` not `&&`; venv python is `.\.venv\Scripts\python.exe`.
- The user is **novice-friendly** — plain language, guided UX, no jargon dumps.
- Don't auto-commit; this isn't a git repo unless the user sets one up.
- Deps in `requirements.txt` (fastapi, uvicorn, numpy, faster-whisper, yt-dlp, pillow,
  opencv-contrib-python, requests; playwright is dev/test-only).
