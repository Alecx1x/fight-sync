"""Render an animated HTML scene to MP4 by capturing it frame-by-frame with headless
Chromium (Playwright), then encoding with ffmpeg.

This is FightSync's "motion-graphics in the browser" engine. A scene is a self-contained
HTML file (authored in Claude Design, kept in `web_intro/`) that exposes two globals:

    window.SCENE_DURATION   // total length in ms
    window.seekTo(ms)       // position EVERY animated element for time `ms` (deterministic)

Because motion is driven by `seekTo` rather than wall-clock CSS animation, capture is
perfectly frame-accurate and reproducible — step ms in 1/fps increments, screenshot each.
Browser-rendered art (gradients, blend modes, SVG filters, real fonts) looks far cleaner
than hand-drawn Pillow frames.

`render_scene()` captures → encodes, optionally muxing SFX. Inputs/paths are absolute so it
is independent of cwd.
"""
import json
import os
import subprocess
from pathlib import Path

from media import FFMPEG


def capture_frames(url, out_dir, W, H, fps, duration_ms=None, settle_ms=250, init_script=None):
    """Drive headless Chromium over the scene and write out_dir/NNNNN.png per frame.
    Returns (n_frames, duration_ms). Requires window.seekTo(ms); reads window.SCENE_DURATION
    when duration_ms is None. `init_script` (JS) runs BEFORE the page's own scripts — used to
    set window.CAPTURE and inject window.MANIFEST."""
    from playwright.sync_api import sync_playwright
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--force-color-profile=srgb", "--hide-scrollbars",
            "--disable-lcd-text", "--allow-file-access-from-files"])
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        if init_script:
            page.add_init_script(init_script)
        page.goto(url, wait_until="networkidle")
        # wait for web fonts + any scene-declared readiness (images preloaded, etc.)
        try:
            page.evaluate("async () => { if (document.fonts) await document.fonts.ready; "
                          "if (window.sceneReady) await window.sceneReady(); }")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(settle_ms)
        if duration_ms is None:
            duration_ms = page.evaluate("() => window.SCENE_DURATION") or 4000
        clip = {"x": 0, "y": 0, "width": W, "height": H}
        n = int(round(duration_ms / 1000.0 * fps))
        for f in range(n):
            t = f / fps * 1000.0
            page.evaluate("(t) => window.seekTo(t)", t)
            page.screenshot(path=os.path.join(out_dir, f"{f:05d}.png"), clip=clip, animations="disabled")
        browser.close()
    return n, duration_ms


def encode(frames_dir, out, fps, sfx_cmd=None, crf=18):
    """Encode NNNNN.png at `fps` → out. `sfx_cmd` is an optional (inputs, filter, maps) tuple
    appended for an audio track; otherwise a silent stereo track is added so it concats cleanly."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(fps), "-i", os.path.join(frames_dir, "%05d.png")]
    if sfx_cmd:
        inputs, filt, maps = sfx_cmd
        cmd += inputs + ["-filter_complex", filt] + maps
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", str(crf),
            "-c:a", "aac", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    return out


def render_scene(scene_url, out, work, W=1280, H=720, fps=30, duration_ms=None,
                 sfx_cmd=None, crf=18):
    """Full path: capture the scene at scene_url into work/frames, then encode to `out`."""
    frames = os.path.join(work, "frames")
    n, dur = capture_frames(scene_url, frames, W, H, fps, duration_ms)
    if n <= 0:
        raise RuntimeError("scene produced no frames")
    encode(frames, out, fps, sfx_cmd, crf)
    return out


SCENE_DIR = Path(__file__).resolve().parent / "web_intro"
SCENE = SCENE_DIR / "tale_of_the_tape.html"
# selectable tale-of-the-tape styles (UI passes the key; default = cinematic)
STYLES = {
    "cinematic": "tale_of_the_tape.html",
    "neon": "style_neon.html",
    "gold": "style_gold.html",
    "comic": "style_comic.html",
}


def scene_path(style):
    return SCENE_DIR / STYLES.get(style or "cinematic", STYLES["cinematic"])


def _reel_manifest(reel_dir, fps_default=20):
    """Read a cutout reel dir (NNNNN.png + meta.json) → dict for the scene, or None."""
    if not reel_dir or not os.path.isdir(reel_dir):
        return None
    import glob
    paths = sorted(glob.glob(os.path.join(reel_dir, "[0-9]*.png")))
    if not paths:
        return None
    meta = {}
    try:
        meta = json.load(open(os.path.join(reel_dir, "meta.json")))
    except (OSError, ValueError):
        pass
    return {"frames": [Path(p).resolve().as_uri() for p in paths],
            "fps": meta.get("fps", fps_default),
            "pw": meta.get("pw"), "ph": meta.get("ph")}


def _elo_frac(elo):
    """Map an ELO-ish string to a 0..1 bar fraction (decorative)."""
    import re
    m = re.search(r"\d+", str(elo or ""))
    if not m:
        return 0.7
    return max(0.25, min(0.96, (int(m.group()) - 1100) / 800.0))


def _side(fighter, corner):
    stats = [["ELO", fighter.get("elo", "—"), _elo_frac(fighter.get("elo"))],
             ["HEIGHT", fighter.get("height", "—"), 0.68],
             ["STYLE", fighter.get("style", "—"), None]]
    return {"name": fighter.get("name") or "FIGHTER", "corner": corner,
            "handle": fighter.get("logo_text") or "@yourchannel", "stats": stats}


def render_vs_intro(out, left, right, work, left_reel=None, right_reel=None,
                    W=1280, H=720, fps=30, seconds=None, sfx=True, crf=18, style="cinematic"):
    """Render the cinematic browser-based tale-of-the-tape to `out`. Mirrors
    vs_intro.render_vs_intro's role: fighters + optional cutout reels in, MP4 out.
    `style` selects the scene (cinematic/neon/gold/comic). `work` is a scratch dir."""
    os.makedirs(work, exist_ok=True)
    lm = _reel_manifest(left_reel)
    rm = _reel_manifest(right_reel)
    reel_dur = max((len(m["frames"]) / m["fps"] for m in (lm, rm) if m), default=0)
    if seconds is None:
        seconds = max(8.0, 1.15 + min(reel_dur, 6.8) + 1.8) if reel_dur else 8.0
    dur_ms = int(seconds * 1000)
    manifest = {"durationMs": dur_ms,
                "left": {**_side(left, "RED CORNER"), **(lm or {"frames": [], "fps": 20})},
                "right": {**_side(right, "BLUE CORNER"), **(rm or {"frames": [], "fps": 20})}}
    init = "window.CAPTURE=true; window.MANIFEST=" + json.dumps(manifest) + ";"
    frames = os.path.join(work, "frames")
    n, _ = capture_frames(scene_path(style).as_uri(), frames, W, H, fps, dur_ms, init_script=init)
    if n <= 0:
        raise RuntimeError("vs-intro scene produced no frames")

    sfx_cmd = None
    if sfx:
        try:
            from replay_sfx import ensure_assets, ensure_bell
            whoosh, impact = ensure_assets()
            bell = ensure_bell()
            slam = 1000; outro = max(0, dur_ms - 1300)
            inputs = ["-i", str(whoosh), "-i", str(impact), "-i", str(bell), "-i", str(impact)]
            filt = (f"[1:a]volume=0.5,adelay=300:all=1[wa];"
                    f"[2:a]volume=0.95,adelay={slam}:all=1[ia];"
                    f"[3:a]volume=0.7,adelay={slam}:all=1[ba];"
                    f"[4:a]volume=0.85,adelay={outro}:all=1[oa];"
                    f"[wa][ia][ba][oa]amix=inputs=4:normalize=0:duration=longest,"
                    f"aresample=48000,aformat=channel_layouts=stereo[a]")
            maps = ["-map", "0:v", "-map", "[a]", "-t", f"{dur_ms / 1000:.3f}"]
            sfx_cmd = (inputs, filt, maps)
        except Exception:  # noqa: BLE001 — silent fallback
            sfx_cmd = None
    encode(frames, out, fps, sfx_cmd, crf)
    return out


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1]).resolve().as_uri()
    work = sys.argv[2] if len(sys.argv) > 2 else "jobs/html_demo"
    os.makedirs(work, exist_ok=True)
    render_scene(src, os.path.join(work, "scene.mp4"), work)
    print("wrote", os.path.join(work, "scene.mp4"))
