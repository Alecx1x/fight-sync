"""Cinematic between-rounds "highlight break" for FightSync.

Replaces the plain black-screen / white-text / bell round card. Two stitched
phases over one transition clip:

  A) HIGHLIGHTS — the round that just finished is auto-scanned for its biggest
     moments (knockdowns / hard combinations, via replay.detect_impacts on the
     mixed audio). Each becomes a slow-mo inset that fades in at a different
     screen corner, the insets cross-fading into one another over a dark
     backdrop, then assembling into the finished "page" (layout.png).

  B) SHATTER — a giant "ROUND N" title comes crashing in from behind the camera
     (huge → slams down to fit), hits with a white flash + a burst of sparks and
     screen-shake, and the assembled page SHATTERS into shards that fly outward,
     resolving to black so it fades cleanly into the next round's clips.

Phase A is real video built in one ffmpeg filtergraph. Phase B is a Pillow frame
sequence (reusing the vs_intro spark/flash technique) over the final montage
frame, with synthesized whoosh + glove-impact + boxing-bell SFX on the slam.

Public entry:
    render_round_break(dst, work, cfg, label, source_body, count=3) -> dst

`source_body` = the just-finished round's composited mp4 (the thing the highlights
are pulled from). Pass source_body=None for a placeholder demo (no real footage).
"""
from __future__ import annotations

import math
import os
import random
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from media import FFMPEG, probe, run_ffmpeg
from replay import detect_impacts

RED = (232, 26, 30)
ACCENT = "0xE23B3B"
BG = (12, 14, 20)

_IMPACT_FONTS = [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf",
                 r"C:\Windows\Fonts\Arial.ttf"]


def _enc(crf: int, preset: str) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
    ]


def _font(size: int):
    for p in _IMPACT_FONTS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ── highlight windows ────────────────────────────────────────────────────────
# each inset shows `REAL` seconds of footage slowed by SP -> VIS seconds on screen.
VIS = 1.5          # seconds an inset is visible
SP = 0.5           # slow-mo factor for the insets
REAL = VIS * SP    # seconds of real footage per inset
FADE = 0.35        # inset cross-fade in/out
STAG = 0.85        # stagger between successive insets (they overlap -> cross-fade)
TAIL = 0.30        # final beat where the assembled page snaps to full


def _highlight_windows(source: str, work: str, count: int) -> list[tuple[float, float]]:
    """Pick up to `count` (start, end) windows of real footage around the round's
    biggest hits. Falls back to evenly-spaced windows if nothing is detected."""
    dur = max(0.1, probe(source).duration)
    impacts = detect_impacts(source, work, count, min_gap=max(1.2, REAL * 1.4))
    if not impacts:
        # spread `count` windows across the clip (skip the very start/end)
        if dur <= REAL * 1.2:
            impacts = [dur / 2]
        else:
            span = dur - REAL
            impacts = [REAL / 2 + span * (k + 1) / (count + 1) for k in range(count)]
    wins = []
    for t in impacts[:count]:
        a = max(0.0, min(t - REAL * 0.4, dur - REAL))
        b = min(dur, a + REAL)
        if b - a > 0.15:
            wins.append((round(a, 3), round(b, 3)))
    return wins or [(0.0, min(REAL, dur))]


def _grab_thumb(source: str, t: float, dst: str, work: str) -> Optional[str]:
    try:
        run_ffmpeg(["-ss", f"{t:.3f}", "-i", source, "-frames:v", "1",
                    "-q:v", "2", dst], cwd=work)
        return dst if Path(dst).exists() else None
    except Exception:  # noqa: BLE001
        return None


# ── geometry shared by montage + layout still ────────────────────────────────
def _slots(W: int, H: int):
    M = max(24, W // 32)
    IW = (int(W * 0.46) // 2) * 2
    IH = (int(IW * 9 / 16) // 2) * 2
    # TL, BR, TR, BL — successive insets land in different corners
    return M, IW, IH, [(M, M), (W - IW - M, H - IH - M), (W - IW - M, M), (M, H - IH - M)]


# ── the assembled "page" still (end of montage = start of shatter) ───────────
def _build_layout(W: int, H: int, wins: list, thumbs: list, label: str, dst: str):
    """Dark page with each highlight thumbnail framed in its corner + accents."""
    M, IW, IH, slots = _slots(W, H)
    img = Image.new("RGB", (W, H), BG)
    # subtle top sheen
    arr = np.asarray(img).astype(np.float32)
    yy = np.linspace(0, 1, H)[:, None]
    arr += (18 * np.exp(-((yy - 0.0) ** 2) / 0.08))[:, :, None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img, "RGBA")
    # broadcast accent rules top & bottom
    d.rectangle((0, 0, W, 6), fill=RED + (255,))
    d.rectangle((0, H - 6, W, H), fill=RED + (255,))
    lab_font = _font(max(26, H // 26))
    for k, (a, b) in enumerate(wins[:3]):
        sx, sy = slots[k]
        th = thumbs[k] if k < len(thumbs) else None
        if th and Path(th).exists():
            try:
                im = Image.open(th).convert("RGB")
                sc = max(IW / im.width, IH / im.height)
                im = im.resize((int(im.width * sc), int(im.height * sc)))
                ox = (im.width - IW) // 2
                img.paste(im.crop((ox, 0, ox + IW, IH)), (sx, sy))
            except Exception:  # noqa: BLE001
                d.rectangle((sx, sy, sx + IW, sy + IH), fill=(28, 30, 36, 255))
        else:
            d.rectangle((sx, sy, sx + IW, sy + IH), fill=(28, 30, 36, 255))
        # red frame + REPLAY tag
        d.rectangle((sx, sy, sx + IW - 1, sy + IH - 1), outline=RED + (255,), width=5)
        d.rectangle((sx, sy, sx + 150, sy + 30), fill=(0, 0, 0, 170))
        d.text((sx + 10, sy + 5), "REPLAY", font=lab_font, fill=(255, 255, 255, 255))
    # centre label chip
    big = _font(max(40, H // 12))
    tw = d.textlength(label.upper(), font=big)
    cx = W // 2
    d.rectangle((cx - tw / 2 - 26, H // 2 - H // 22, cx + tw / 2 + 26, H // 2 + H // 22),
                fill=(8, 9, 12, 210))
    d.text((cx, H // 2), label.upper(), font=big, fill=(238, 240, 246, 255), anchor="mm")
    img.save(dst)
    return dst


# ── phase A: the cross-fading corner-replay montage (real video, ffmpeg) ─────
def _build_montage(source: str, wins: list, layout_png: str, dst: str, work: str,
                   cfg, font: str) -> float:
    W, H, fps = cfg.out_w, cfg.out_h, cfg.fps
    M, IW, IH, slots = _slots(W, H)
    n = len(wins)
    mon_dur = (n - 1) * STAG + VIS + TAIL

    pan = (f"scale={IW}:{IH}:force_original_aspect_ratio=increase,"
           f"crop={IW}:{IH},setsar=1,fps={fps}")
    parts = [f"color=c=0x{BG[0]:02x}{BG[1]:02x}{BG[2]:02x}:s={W}x{H}:r={fps}[bgc]",
             f"[bgc]drawbox=x=0:y=0:w=iw:h=6:color={ACCENT}:t=fill,"
             f"drawbox=x=0:y=ih-6:w=iw:h=6:color={ACCENT}:t=fill[bg]"]
    overlays = []
    cur = "[bg]"
    for k, (a, b) in enumerate(wins):
        appear = k * STAG
        sx, sy = slots[k % len(slots)]
        parts.append(
            f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{SP:.3f},{pan},"
            f"format=yuva420p,"
            f"drawbox=x=0:y=0:w={IW}:h={IH}:color={ACCENT}:t=5,"
            f"drawbox=x=0:y=0:w=150:h=30:color=0x000000@0.55:t=fill,"
            f"drawtext=fontfile={font}:text='REPLAY':x=10:y=5:fontcolor=white:fontsize=22,"
            f"fade=t=in:st=0:d={FADE}:alpha=1,"
            f"fade=t=out:st={VIS - FADE:.3f}:d={FADE}:alpha=1,"
            f"setpts=PTS-STARTPTS+{appear:.3f}/TB[ins{k}]")
        nxt = f"[o{k}]"
        overlays.append(f"{cur}[ins{k}]overlay=x={sx}:y={sy}:eof_action=pass:"
                        f"enable='gte(t,{appear:.3f})'{nxt}")
        cur = nxt
    # assemble: the finished page fades to full over the final TAIL seconds
    parts.append(
        f"[1:v]scale={W}:{H},setsar=1,fps={fps},format=yuva420p,"
        f"fade=t=in:st=0:d={TAIL}:alpha=1,"
        f"setpts=PTS-STARTPTS+{mon_dur - TAIL:.3f}/TB[lay]")
    overlays.append(f"{cur}[lay]overlay=x=0:y=0:eof_action=pass:"
                    f"enable='gte(t,{mon_dur - TAIL:.3f})'[outv]")
    fg = ";".join(parts + overlays) + ";anullsrc=r=48000:cl=stereo[outa]"
    run_ffmpeg(["-i", source, "-loop", "1", "-t", f"{mon_dur:.3f}", "-i", layout_png,
                "-filter_complex", fg, "-map", "[outv]", "-map", "[outa]",
                "-t", f"{mon_dur:.3f}", "-r", str(fps), *_enc(cfg.crf, cfg.preset),
                dst], cwd=work)
    return mon_dur


# ── phase B: text-crash + shatter (Pillow frames -> ffmpeg, with SFX) ─────────
def _render_text(label: str, W: int, H: int) -> Image.Image:
    """Pre-render the big title as an RGBA image (white, heavy outline)."""
    txt = (label or "ROUND").upper()
    f = _font(max(80, H // 5))
    tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tmp)
    # shrink font until it fits ~0.82 W
    while d.textlength(txt, font=f) > W * 0.82 and f.size > 24:
        f = _font(f.size - 6)
    bb = d.textbbox((0, 0), txt, font=f)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    img = Image.new("RGBA", (tw + 40, th + 40), (0, 0, 0, 0))
    dd = ImageDraw.Draw(img)
    ox, oy = 20 - bb[0], 20 - bb[1]
    for dx in range(-6, 7, 2):                       # chunky outline
        for dy in range(-6, 7, 2):
            dd.text((ox + dx, oy + dy), txt, font=f, fill=(8, 8, 10, 255))
    dd.text((ox, oy), txt, font=f, fill=(255, 255, 255, 255))
    dd.text((ox, oy), txt, font=f, fill=(255, 80, 70, 60))  # faint red core
    return img


def _render_shatter(layout_png: str, label: str, dst: str, work: str, cfg) -> float:
    from replay_sfx import ensure_assets, ensure_bell
    W, H, fps = cfg.out_w, cfg.out_h, cfg.fps
    rnd = random.Random(13)
    layout = Image.open(layout_png).convert("RGB").resize((W, H))

    # pre-split the page into tiles that will fly apart
    GX, GY = 10, 6
    tw, th = W / GX, H / GY
    tiles = []
    for gy in range(GY):
        for gx in range(GX):
            box = (int(gx * tw), int(gy * th), int((gx + 1) * tw), int((gy + 1) * th))
            cx0, cy0 = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            dx, dy = cx0 - W / 2, cy0 - H / 2
            norm = max(1.0, math.hypot(dx, dy))
            spd = rnd.uniform(7, 17)
            tiles.append({
                "img": layout.crop(box).convert("RGBA"), "pos": (box[0], box[1]),
                "vx": dx / norm * spd + rnd.uniform(-3, 3),
                "vy": dy / norm * spd + rnd.uniform(-3, 3) - 5,
                "spin": rnd.uniform(-9, 9), "delay": rnd.randint(0, 3),
            })

    text_img = _render_text(label, W, H)
    approach = int(0.55 * fps)            # title flies in from behind the camera
    after = int(1.05 * fps)               # shatter resolves
    nfr = approach + after
    slam = approach
    black = Image.new("RGB", (W, H), (6, 7, 10))
    BIG = 7.0                             # title starts this many × oversize

    sparks = []
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
    whoosh = impact_wav = bell = None
    try:
        whoosh, impact_wav = ensure_assets()
        bell = ensure_bell()
    except Exception:  # noqa: BLE001
        pass
    if whoosh and impact_wav and bell:
        slam_ms = int(slam / fps * 1000)
        w_ms = max(0, slam_ms - 360)
        cmd += ["-i", str(whoosh), "-i", str(impact_wav), "-i", str(bell),
                "-filter_complex",
                f"[1:a]volume=0.45,adelay={w_ms}:all=1[wa];"
                f"[2:a]volume=0.95,adelay={slam_ms}:all=1[ia];"
                f"[3:a]volume=0.8,adelay={slam_ms}:all=1[ba];"
                f"[wa][ia][ba]amix=inputs=3:normalize=0:duration=longest,"
                f"aresample=48000,aformat=channel_layouts=stereo[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-map", "0:v", "-map", "1:a"]
    cmd += ["-t", f"{nfr / fps:.3f}", *_enc(cfg.crf, cfg.preset), dst]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    for f in range(nfr):
        if f < slam:
            # the page sits; the title rushes in from behind the camera
            frame = layout.copy()
            u = f / max(1, slam)
            ease = u ** 0.45                       # fast approach, eases to land
            scale = BIG + (1.0 - BIG) * ease
            alpha = int(70 + 185 * ease)
            base = frame.convert("RGBA")
            for ghost, ga in ((scale * 1.10, 0.35), (scale, 1.0)):  # cheap motion blur
                tw2 = max(1, int(text_img.width * ghost))
                th2 = max(1, int(text_img.height * ghost))
                ti = text_img.resize((tw2, th2))
                a = ti.split()[3].point(lambda v: int(v * (alpha / 255) * ga))
                ti.putalpha(a)
                base.alpha_composite(ti, ((W - tw2) // 2, (H - th2) // 2))
            frame = base.convert("RGB")
        else:
            age = f - slam
            shake = int(max(0, 26 - age * 3) * (rnd.random() - 0.5))
            frame = black.copy()
            # flying shards
            for tdat in tiles:
                a2 = age - tdat["delay"]
                if a2 < 0:
                    frame.paste(tdat["img"].convert("RGB"), tdat["pos"])
                    continue
                fade = max(0.0, 1.0 - a2 / (after - 2))
                if fade <= 0.02:
                    continue
                x = tdat["pos"][0] + tdat["vx"] * a2 + shake
                y = tdat["pos"][1] + tdat["vy"] * a2 + 0.6 * a2 * a2
                pc = tdat["img"].rotate(tdat["spin"] * a2, expand=True)
                if fade < 0.99:
                    al = pc.split()[3].point(lambda v: int(v * fade))
                    pc.putalpha(al)
                frame.paste(pc, (int(x), int(y)), pc)
            base = frame.convert("RGBA")
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            if age < 6:                              # white slam flash
                ov.paste((255, 255, 255, int(200 * (1 - age / 6))), (0, 0, W, H))
            if age == 0:                             # spark burst from centre
                for _ in range(130):
                    ang = rnd.uniform(0, 2 * math.pi)
                    sp = rnd.uniform(10, 40)
                    sparks.append({"x": W / 2, "y": H / 2,
                                   "vx": math.cos(ang) * sp, "vy": math.sin(ang) * sp,
                                   "life": rnd.randint(12, 30), "age": 0,
                                   "col": rnd.choice([(255, 240, 120), (255, 170, 40),
                                                      (255, 90, 30), (255, 255, 255)])})
            for s in sparks:
                if s["age"] >= s["life"]:
                    continue
                aa = s["age"]
                x = s["x"] + s["vx"] * aa
                y = s["y"] + s["vy"] * aa + 0.5 * aa * aa
                fd = int(255 * (1 - aa / s["life"]))
                col = s["col"] + (fd,)
                od.line((x, y, x - s["vx"] * 0.5, y - s["vy"] * 0.5), fill=col, width=3)
                od.ellipse((x - 2, y - 2, x + 2, y + 2), fill=col)
                s["age"] += 1
            # the title flares white at the hit, then recedes with the shards
            ts = 1.0 + min(0.12, age * 0.02)
            talpha = int(255 * max(0.0, 1.0 - age / 8))
            if talpha > 4:
                tw2 = max(1, int(text_img.width * ts))
                th2 = max(1, int(text_img.height * ts))
                ti = text_img.resize((tw2, th2))
                al = ti.split()[3].point(lambda v: int(v * (talpha / 255)))
                ti.putalpha(al)
                ov.alpha_composite(ti, ((W - tw2) // 2 + shake, (H - th2) // 2))
            frame = Image.alpha_composite(base, ov).convert("RGB")
        proc.stdin.write(np.asarray(frame, dtype=np.uint8).tobytes())
    proc.stdin.close()
    proc.wait()
    return nfr / fps


# ── orchestrator ─────────────────────────────────────────────────────────────
def render_round_break(dst: str, work: str, cfg, label: str,
                       source_body: Optional[str], count: int = 3) -> str:
    """Render one highlight-break transition clip to `dst`. `source_body` = the
    just-finished round's composited mp4 (highlights are pulled from it)."""
    from pipeline import _ensure_font
    work_p = Path(work).resolve()
    work = str(work_p)
    work_p.mkdir(parents=True, exist_ok=True)
    W, H = cfg.out_w, cfg.out_h
    font = _ensure_font(work)
    stem = Path(dst).stem

    have = bool(source_body and Path(source_body).exists())
    if have:
        source_body = str(Path(source_body).resolve())
    if have:
        wins = _highlight_windows(source_body, work, count)
        thumbs = []
        for k, (a, b) in enumerate(wins):
            thumbs.append(_grab_thumb(source_body, (a + b) / 2,
                                      str(work_p / f"{stem}_th{k}.png"), work))
    else:                                            # placeholder demo
        wins = [(0.0, REAL)] * 3
        thumbs = [None, None, None]

    layout_png = str(work_p / f"{stem}_layout.png")
    _build_layout(W, H, wins, thumbs, label, layout_png)

    mon = str(work_p / f"{stem}_montage.mp4")
    shat = str(work_p / f"{stem}_shatter.mp4")
    if have:
        _build_montage(source_body, wins, layout_png, mon, work, cfg, font)
    else:                                            # static page hold for the demo
        run_ffmpeg(["-loop", "1", "-t", "2.2", "-i", layout_png,
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-shortest", "-r", str(cfg.fps), *_enc(cfg.crf, cfg.preset), mon],
                   cwd=work)
    _render_shatter(layout_png, label, shat, work, cfg)

    # concat montage + shatter
    listf = work_p / f"{stem}_list.txt"
    listf.write_text(f"file '{Path(mon).name}'\nfile '{Path(shat).name}'\n",
                     encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listf),
                *_enc(cfg.crf, cfg.preset), dst], cwd=work)
    return dst


if __name__ == "__main__":
    import sys
    from dataclasses import dataclass

    @dataclass
    class _Cfg:
        out_w: int = 1280
        out_h: int = 720
        fps: int = 30
        crf: int = 20
        preset: str = "medium"

    src = str(Path(sys.argv[1]).resolve()) if len(sys.argv) > 1 else None
    work = str(Path("round_break_demo_work").resolve())
    Path(work).mkdir(exist_ok=True)
    out = str(Path("round_break_demo.mp4").resolve())
    render_round_break(out, work, _Cfg(), "ROUND 2", src)
    print("wrote", out)
