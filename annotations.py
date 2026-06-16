"""Annotation overlay renderer — the "form corrector" drawing layer.

Burns animated, translucent coaching markers into a video: red/green circles,
arrows, text callouts, and zone outlines, each with a time window and a
fade-in/out (+ optional pulse). Markers are stored in resolution-independent
normalized coordinates (0..1 of the base frame) so they survive any output size.

Rendering: for every frame we draw the active markers on a transparent RGBA
canvas (super-sampled for smooth edges) and pipe the raw frames straight into
ffmpeg, which overlays them onto the base video in a single pass.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from media import FFMPEG, probe

SS = 2  # super-sampling factor for smooth (anti-aliased) shapes

_COLORS = {
    "red": (231, 59, 59),
    "green": (61, 220, 132),
    "yellow": (247, 201, 72),
    "white": (240, 244, 250),
}

_FONT_SRCS = [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
              r"C:\Windows\Fonts\arial.ttf"]


def _rgb(c: str):
    if isinstance(c, str) and c.startswith("#") and len(c) == 7:
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    return _COLORS.get(c, _COLORS["red"])


def _font(px: int) -> ImageFont.FreeTypeFont:
    for src in _FONT_SRCS:
        if Path(src).exists():
            return ImageFont.truetype(src, max(8, px))
    return ImageFont.load_default()


def _alpha_env(t: float, start: float, end: float, fade: float = 0.25) -> float:
    """Fade in over `fade` s, hold, fade out — clamped 0..1."""
    if t < start or t > end:
        return 0.0
    return max(0.0, min(1.0, min((t - start) / fade, (end - t) / fade, 1.0)))


def _interp_track(track: list, t: float):
    """Interpolate [t,cx,cy,w,h] track at time t -> (cx, cy, w, h)."""
    if t <= track[0][0]:
        p = track[0]
        return p[1], p[2], p[3], p[4]
    if t >= track[-1][0]:
        p = track[-1]
        return p[1], p[2], p[3], p[4]
    for i in range(1, len(track)):
        if track[i][0] >= t:
            a0, b0 = track[i - 1], track[i]
            f = (t - a0[0]) / (b0[0] - a0[0]) if b0[0] != a0[0] else 0.0
            return tuple(a0[k] + f * (b0[k] - a0[k]) for k in (1, 2, 3, 4))
    p = track[-1]
    return p[1], p[2], p[3], p[4]


def _track_transform(a: dict, t: float):
    """Return (ox, oy, scale) that a track imposes on the marker at time t."""
    tr = a.get("track")
    if not tr:
        return 0.0, 0.0, 1.0
    cx, cy, w, h = _interp_track(tr, t)
    bx, by, bw = tr[0][1], tr[0][2], tr[0][3]
    return cx - bx, cy - by, (w / bw if bw else 1.0)


def _draw(d: ImageDraw.ImageDraw, a: dict, t: float, W: int, H: int):
    env = _alpha_env(t, a["start"], a["end"])
    if env <= 0:
        return
    col = _rgb(a.get("color", "red"))
    th = int(a.get("thickness", 4) * SS)
    pulse = 1.0
    if a.get("pulse", True):
        pulse = 1.0 + 0.07 * math.sin(t * 2 * math.pi * 1.8)

    ox, oy, sc = _track_transform(a, t)   # follow tracked motion
    fill_a, line_a = int(50 * env), int(230 * env)

    typ = a.get("type", "circle")
    if typ == "circle":
        cx, cy = (a["nx"] + ox) * W, (a["ny"] + oy) * H
        r = a["nr"] * H * pulse * sc
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  fill=col + (fill_a,), outline=col + (line_a,), width=th)
    elif typ == "zone":
        cx0, cy0 = a["nx"] + a["nw"] / 2, a["ny"] + a["nh"] / 2
        hw, hh = a["nw"] / 2 * sc, a["nh"] / 2 * sc
        x0, y0 = (cx0 + ox - hw) * W, (cy0 + oy - hh) * H
        x1, y1 = (cx0 + ox + hw) * W, (cy0 + oy + hh) * H
        rad = int(min(x1 - x0, y1 - y0) * 0.12)
        d.rounded_rectangle([x0, y0, x1, y1], radius=rad,
                            fill=col + (int(40 * env),),
                            outline=col + (line_a,), width=th)
    elif typ == "poly":
        pts = [((px + ox) * W, (py + oy) * H) for px, py in a["points"]]
        if len(pts) >= 2:
            d.polygon(pts, fill=col + (fill_a,), outline=col + (line_a,), width=th)
    elif typ == "arrow":
        x0, y0 = (a["nx"] + ox) * W, (a["ny"] + oy) * H
        x1, y1 = (a["nx2"] + ox) * W, (a["ny2"] + oy) * H
        d.line([x0, y0, x1, y1], fill=col + (int(235 * env),), width=th)
        ang = math.atan2(y1 - y0, x1 - x0)
        head = max(14 * SS, th * 3)
        for s in (+1, -1):
            hx = x1 - head * math.cos(ang - s * 0.5)
            hy = y1 - head * math.sin(ang - s * 0.5)
            d.line([x1, y1, hx, hy], fill=col + (int(235 * env),), width=th)

    label = a.get("label") or (a.get("text") if typ == "text" else None)
    if label:
        if typ == "text":
            tx, ty = (a["nx"] + ox) * W, (a["ny"] + oy) * H
            size = int(a.get("size", 0.05) * H * SS)
        else:  # caption under a circle/zone
            tx = (a.get("nx", 0) + a.get("nw", 0) / 2 + ox) * W if typ == "zone" \
                else (a["nx"] + ox) * W
            ty = (a["ny"] + oy) * H + a.get("nr", 0.0) * H * pulse + 6 * SS
            size = int(0.038 * H * SS)
        fnt = _font(size)
        ink = col if typ != "text" else _COLORS["white"]
        d.text((tx, ty), label, font=fnt, fill=ink + (int(245 * env),),
               anchor="ma", stroke_width=max(2, SS),
               stroke_fill=(0, 0, 0, int(220 * env)))


def render_overlay(base: str, annotations: list[dict], out_path: str,
                   enc: list[str], on_progress=None) -> str:
    info = probe(base)
    W, H, fps = info.width, info.height, info.fps or 30
    dur = info.duration or 0
    total = max(1, int(round(dur * fps)))
    sw, sh = W * SS, H * SS

    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", base,
        "-f", "rawvideo", "-pixel_format", "rgba",
        "-video_size", f"{W}x{H}", "-framerate", f"{fps}", "-i", "-",
        "-filter_complex", "[0:v][1:v]overlay=0:0:shortest=1[v]",
        "-map", "[v]", "-map", "0:a?", *enc, out_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for f in range(total):
            t = f / fps
            img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
            active = [a for a in annotations if a["start"] <= t <= a["end"]]
            if active:
                d = ImageDraw.Draw(img, "RGBA")
                for a in active:
                    _draw(d, a, t, sw, sh)
                img = img.resize((W, H), Image.LANCZOS)
            else:
                img = img.resize((W, H), Image.NEAREST)
            proc.stdin.write(img.tobytes())
            if on_progress and f % 30 == 0:
                on_progress(int(100 * f / total))
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"overlay render failed (exit {proc.returncode})")
    return out_path
