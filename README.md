# FightSync 🥊

A local web app that automates your **Thrill of the Fight 2** YouTube workflow.
Drop in your VR gameplay capture and your facecam; FightSync:

1. **Auto-syncs** the two clips by their shared room audio (voice, punches) using
   audio-envelope cross-correlation — no clapboard or manual alignment needed.
2. **Composites** the facecam as a picture-in-picture overlay on the gameplay.
3. **Mixes** the audio (game + mic, or either one).
4. **Transcribes** your speech into subtitles with local Whisper (burned-in and/or `.srt`).
5. **Slow-mo replays** (optional): Fight-Night-style instant replays — a `setpts`
   speed-ramp into a deep slow-mo hold on the punch, with a synthesized whoosh +
   glove-impact SFX and a "REPLAY" badge, spliced in right after the live hit.
   Give it timestamps or let it **auto-detect the biggest hits** from the audio.
   (Ported from the Ringside VR OBS replay system.)
6. **Tops & tails** the video with a clean intro and outro title card.
7. Outputs a single, ready-to-upload **`final.mp4`** (+ `final.srt`).

Everything runs **offline on your PC**. Nothing is uploaded anywhere.

## Run it

Double-click **`start.bat`** (first run sets up the environment automatically),
or from a terminal:

```
.venv\Scripts\python app.py
```

Then open <http://127.0.0.1:8765>.

> Port 8765 is used because 8000 is taken by SillyTavern on this machine.
> Override with `set FIGHTSYNC_PORT=8780` before launching.

## Using it

- **Add multiple clips to each tile** — drop or pick several videos at once; they're
  kept in the order added and shown as a numbered list (× to remove). Gameplay and
  facecam pair up by position: gameplay #1 ↔ facecam #1, #2 ↔ #2, … Each pair is
  synced independently, then the segments are stitched together in order. Add the
  **same number** of clips to both tiles.
- **Sync runs automatically** as soon as each section has at least one clip — the *Sync*
  panel shows each pair's offset + a confidence (✓ solid / ⚠ weak / ⚠ no shared audio),
  and a **side-by-side synced preview player** appears so you can play/scrub the first
  pair and visually confirm alignment *before* rendering. The bottom **Render final
  video** button then does the full render with all graphics. (If clips don't share
  audio it no longer fails — it aligns them at the start and warns.)
- Sync locks onto **shared onsets** (claps, punch impacts) rather than overall loudness,
  so one or two shared key sounds are enough — a single clap at the start of a round
  gives a rock-solid lock even when the two tracks otherwise sound nothing alike.
- **Layout**: *Picture-in-picture* (facecam corner) or *Side-by-side* (both halves the
  same size). Side-by-side hides the corner/size controls.
- Between clips you get a **round transition** (Round transitions section): the video
  fades to black, a title (e.g. `ROUND 2`) flashes with a **boxing-bell ring**, then it
  fades into the next synced clip. Toggle the transition / bell and set the title word.
- **Drag-drop** each video onto its tile, **or** click *"use a file path instead"*
  and paste a full path (press Enter to add). For multi-GB captures, the path option is
  faster (no copy through the browser).
- **Record in the browser** — each tile has a record button:
  - Gameplay → **● Record screen** captures your screen + system audio. The
    **+ mic** checkbox (on by default) mixes your mic in too — keep it on, because
    auto-sync needs your voice present in *both* tracks.
  - Facecam → **● Record webcam** captures your camera + mic.
  Hit Stop and the clip auto-uploads and stages itself in that slot. Recording
  needs a secure context: open the app at **http://127.0.0.1:8765** in Chrome or
  Edge (the button disables itself otherwise). Recordings land in `recordings/`
  and are remuxed to MKV so ffmpeg always gets a valid duration.
- **Upload IRL footage from your phone** — on the facecam tile, tap **📱 Upload from
  library**. On iPhone this opens the Photos picker; the clip uploads with a progress
  bar and stages as facecam. iPhone HEVC/.mov and rotation are handled by ffmpeg.
- **Import a Meta Quest clip by link** — on the gameplay tile, click *"📎 paste a
  Meta Quest clip link"*, paste the share URL, and hit Import. It downloads via
  yt-dlp and stages as the gameplay footage. Note: personal Quest clips are often
  private/login-gated and won't download without auth — public/supported links work.

## Remote access (password + Cloudflare tunnel)

The whole app is behind a **password gate** (`app.py` middleware): the first run
generates a password into `fightsync-secret.txt` (override with env
`FIGHTSYNC_PASSWORD`), shown on startup. Unauthenticated requests get a login page
(HTML) or `401` (API); a correct password sets a 30-day cookie.

To reach it from anywhere, run **`start-remote.bat`** — it starts the server and a
**Cloudflare quick tunnel** (`cloudflared.exe`) and prints a public
`https://<random>.trycloudflare.com` URL (also saved to `tunnel.log`). Open it on any
device, enter the password, done.

> The quick-tunnel URL **changes every restart** — a *dedicated* `fightsync.<domain>`
> URL needs a custom domain added to your Cloudflare account (the account currently
> has none). Add one and a named tunnel gives a permanent URL.

## Use it from your phone

The server binds all interfaces, so on the **same Wi-Fi** open the LAN URL printed
at startup (e.g. `http://192.168.18.4:8765`) on your phone/tablet. Uploads, the Meta
link import, renders, and the **Form Studio** (touch-enabled) all work on mobile.
Screen/webcam **recording** and channel capture do *not* — browsers block camera/
screen APIs outside a secure context (HTTPS/localhost), so those stay desktop-only.
Heads-up: binding to the LAN exposes the app (and `/api/media` file reads) to your
network with no password — fine on a trusted home Wi-Fi; don't do it on public Wi-Fi.
- Pick the facecam **corner**, **size**, and **audio** mode.
- Set the **intro title/subtitle** and toggle intro/outro/subtitles.
- Choose subtitle **accuracy** (bigger Whisper model = slower but better).
- Click **Sync & render**. Watch progress, then download the MP4 (and `.srt`).

The result panel shows the detected sync **offset** and a **confidence** score.
Low confidence (< ~0.2) usually means the two clips don't actually share audio —
double-check both recordings contain your voice / room sound.

## Auto-capture from live channels

The **"Auto-capture from live channels"** panel lets you grab gameplay straight
from a channel's live stream:

1. **Add a channel** by URL or handle (`youtube.com/@TheCleanLeague`, `@RingsideVR`).
2. The app polls each channel's live status with **yt-dlp** (no API key — it reads
   `youtube.com/@handle/live`). A red **LIVE** badge lights up when they're streaming,
   which enables that channel's **Capture** button.
3. Press **Capture** to start screen + audio recording (your mic is mixed in too),
   press again to **Stop**.
4. The recording is analyzed: motion-variance **auto-detects the gameplay viewport**
   and opens a confirm dialog with a draggable/resizable crop box. Accept it (saved as
   that channel's template for next time), tweak it, or keep the full frame.
5. The cropped gameplay drops into the **gameplay slot** — ready to sync with your
   facecam and run the full pipeline.

Recording needs a secure context (open at **http://127.0.0.1:8765** in Chrome/Edge).
Channel templates persist in `channels.json`. Only capture footage you have the
rights to use (e.g. leagues you compete in).

## Form Studio (coaching annotation editor)

Open **http://127.0.0.1:8765/studio** (or click *Form Studio →* in the header, or
*Annotate in Form Studio* on a finished render). It's a purpose-built editor for
boxing-form review — no general NLE needed:

- Load any synced/exported clip (path, `?path=`, or from a finished render).
- Scrub the timeline; pick a tool — **circle, zone, polygon, arrow, text** — in
  **red / green / yellow**, with a pulse toggle and a default marker length.
- Draw on the video: circle/zone drag, arrow tail→head, polygon click-to-close,
  text click. Markers carry a time window and fade in/out.
- **Motion tracking**: select a circle/zone/polygon and hit **◎ Track motion** —
  OpenCV CSRT follows that region for its duration so the marker *sticks to your
  glove/head as it moves*. (Drifts on very fast/blurred motion; pose-anchoring and
  keyframe correction are the planned robustness upgrades.)
- **Burn in & export** renders the animated overlays into a downloadable MP4
  (`annotations.py`: Pillow draws super-sampled RGBA frames piped into ffmpeg).
- Save/Load the annotation project (`studio_project.json`).

Planned next: in-browser **MediaPipe Pose** to read your movement and generate a
findings report (punches, guard-down, head-movement, stance) that you accept/reject
with buttons — accepted findings become pre-placed, joint-anchored markers to refine.

## Requirements

- **Python 3.10+** and **ffmpeg** (installed via `winget install Gyan.FFmpeg`).
  FightSync auto-detects ffmpeg on PATH, the winget install location, or via the
  `FFMPEG_BINARY` env var.
- Python deps in `requirements.txt` (FastAPI, numpy, faster-whisper, …).

## How sync works

Both recordings capture the same acoustic events. FightSync extracts a low-rate
mono signal from each, reduces it to a 100 Hz short-time **energy envelope**, and
cross-correlates the two envelopes via FFT. The lag at the correlation peak is the
time offset. Using the *envelope* (amplitude pattern) rather than raw samples makes
it robust to the two devices having very different mic frequency response.

## Configuration knobs (`pipeline.py` → `RenderConfig`)

| Field | Default | Meaning |
|-------|---------|---------|
| `out_w` / `out_h` / `fps` | 1920×1080 @ 30 | Output resolution / framerate |
| `pip_scale` | 0.26 | Facecam width as a fraction of frame |
| `pip_position` | `br` | `br` / `bl` / `tr` / `tl` |
| `audio_mode` | `mix` | `mix` / `gameplay` / `facecam` |
| `whisper_model` | `base` | `tiny`/`base`/`small`/`medium` |
| `replays` | `False` | enable cinematic slow-mo replays |
| `replay_times` | `""` | impact timestamps, e.g. `"1:23, 4:05"` |
| `auto_replays` | `0` | auto-detect this many biggest hits |
| `replay_smooth` | `False` | motion-interpolate the slow-mo (fluid, slower) |
| `whoosh_gain` / `impact_gain` | 0.40 / 0.70 | replay SFX levels |
| `crf` / `preset` | 20 / medium | x264 quality / speed |

### Slow-mo replays in detail

`replay.py` retimes a ~3 s window around each impact with ffmpeg `setpts`
(`1.0x → 0.5x ease → 0.18x impact hold → 0.5x ease → 1.0x`), trimming straight
from the composited video by absolute timestamp so the slow-mo is frame-accurate
on the punch. `replay_sfx.py` procedurally synthesizes the whoosh and glove-impact
sounds (no licensed audio) and times the whoosh to crest exactly at contact. Each
replay is spliced in after the live punch lands, so viewers see it live, then in
dramatic slow motion, then the fight resumes. Auto-detection finds the loudest
audio onsets (spaced ≥ 6 s apart) as the biggest hits.

## Test

`.venv\Scripts\python test_synthetic.py` builds two synthetic clips with a known
5-second offset and verifies sync accuracy + the full render pipeline.

## Project layout

```
app.py            FastAPI server (upload, recordings, jobs, progress, download)
pipeline.py       render: sync -> PiP -> audio -> subtitles -> replays -> intro/outro
sync.py           audio-envelope cross-correlation
replay.py         slow-mo replays: ramp, impact auto-detect, body assembly
replay_sfx.py     procedurally synthesized whoosh + impact SFX
channels.py       channel registry + yt-dlp live detection + crop templates
cropdetect.py     motion-variance gameplay-viewport detection + crop apply
annotations.py    coaching overlay renderer (circles/arrows/text/zones/polygons)
tracking.py       OpenCV CSRT region motion tracking for the Form Studio
media.py          ffmpeg/ffprobe discovery + probing (with duration fallback)
static/index.html the web UI (drag-drop, recorder, channels, cropper, progress)
static/studio.html the Form Studio annotation editor (canvas, tools, tracking)
```

## Ideas to extend

- Auto-generate a YouTube **title/description/chapters** from the transcript.
- Detect **knockdowns / round changes** (audio spikes) to auto-place chapter markers.
- A **highlights** pass that finds your biggest combos by audio energy.
- Per-corner facecam **shapes** (rounded, circle mask) and animated lower-thirds.
- Direct **YouTube upload** via the Data API once a clip is approved.
