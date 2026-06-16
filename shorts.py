"""Auto-cut a vertical 9:16 highlights / Shorts reel from a finished render.

Finds the biggest hits (loudest audio onsets — same detector the slow-mo replays
use), grabs a short window around each, re-frames them to 1080x1920 with a blurred
fill background (so nothing is cropped away), burns a small header label, and
concats them into one snappy reel ready for YouTube Shorts / Reels / TikTok.
"""
from __future__ import annotations

from pathlib import Path

from media import probe, run_ffmpeg
from replay import detect_impacts
from pipeline import _enc, _ensure_font

PRE = 1.3            # seconds kept before each impact
POST = 2.0           # seconds kept after each impact
MIN_GAP = 4.0        # don't pick two hits closer than this
MAX_TOTAL = 58.0     # cap the reel length (Shorts max is 60s)


def _windows(impacts: list[float], dur: float) -> list[tuple[float, float]]:
    """Chronological, non-overlapping [start,end] windows, capped at MAX_TOTAL."""
    wins: list[tuple[float, float]] = []
    total = 0.0
    for t in sorted(impacts):
        a = max(0.0, t - PRE)
        b = min(dur, t + POST)
        if b - a < 0.6:
            continue
        if wins and a <= wins[-1][1] + 0.05:   # overlaps previous -> extend it
            na, nb = wins[-1][0], max(wins[-1][1], b)
            total += nb - wins[-1][1]
            wins[-1] = (na, nb)
        else:
            wins.append((a, b))
            total += b - a
        if total >= MAX_TOTAL:
            break
    return wins


def make_reel(src: str, out_path: str, work: str, count: int = 16,
              label: str = "HIGHLIGHTS", ow: int = 1080, oh: int = 1920,
              fps: int = 30, crf: int = 20, preset: str = "veryfast",
              progress=None) -> dict:
    """Build the reel. Returns {final, clips, duration}."""
    def prog(p, m):
        if progress:
            progress(p, m)

    work_p = Path(work)
    work_p.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    dur = info.duration

    prog(10, "Finding the biggest hits…")
    impacts = detect_impacts(src, work, count, min_gap=MIN_GAP)
    wins = _windows(impacts, dur)
    if len(wins) < 2:
        # not enough punchy moments — fall back to a few evenly spaced grabs
        n = 6
        step = dur / (n + 1)
        wins = _windows([step * (i + 1) for i in range(n)], dur)

    font = _ensure_font(work)
    enc = _enc(crf, preset)
    if label:
        lf = work_p / "reel_label.txt"
        lf.write_text(label, encoding="utf-8")
        header = (
            f",drawbox=x=0:y=70:w={ow}:h=92:color=0x000000@0.35:t=fill,"
            f"drawtext=fontfile={font}:textfile='{lf.name}':fontcolor=white:"
            f"fontsize=46:x=(w-text_w)/2:y=92"
        )
    else:
        header = ""

    parts = []
    for i, (a, b) in enumerate(wins):
        prog(15 + int(70 * i / len(wins)), f"Clip {i + 1}/{len(wins)}…")
        seg = str(work_p / f"reel_{i}.mp4")
        vf = (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
            f"crop={ow}:{oh},boxblur=22:2,setsar=1[bgb];"
            f"[fg]scale={ow}:-2:force_original_aspect_ratio=decrease,setsar=1[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p,fps={fps}"
            f"{header}[v]"
        )
        run_ffmpeg([
            "-ss", f"{a:.3f}", "-i", src, "-t", f"{b - a:.3f}",
            "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
            "-r", str(fps), *enc, seg,
        ], cwd=work)
        parts.append(seg)

    prog(90, "Stitching the reel…")
    listf = work_p / "reel_list.txt"
    listf.write_text("".join(
        f"file '{Path(p).name}'\n" for p in parts), encoding="utf-8")
    run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", listf.name,
        "-c", "copy", "-movflags", "+faststart", out_path,
    ], cwd=work)

    prog(100, "Reel ready.")
    return {"final": out_path, "clips": len(parts),
            "duration": round(probe(out_path).duration, 1)}
