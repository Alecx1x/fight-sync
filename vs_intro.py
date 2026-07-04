"""Animated VS fighter-intro for FightSync — the "tale of the tape" slam.

Two full-height STEEL corner panels (red corner left, blue corner right) meet at a
DIAGONAL symmetrical seam. They drift together slowly (magnetic pull) then SLAM,
sparks flying across the screen. Brushed metal, rivets, bevels. All of a fighter's
text is tinted to their corner colour. Rendered with Pillow, piped to ffmpeg.

render_vs_intro(out, left, right, ...)
  left/right = {"name","elo","height","style","logo"(png)/"logo_text"}
"""
import json
import math
import os
import random
import subprocess

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from media import FFMPEG

RED = (232, 26, 30)        # true, saturated red (was looking orange)
BLUE = (44, 104, 234)
BONE = (233, 228, 216)     # gritty off-white (newsprint / bone) instead of pure white
ASH = (150, 144, 134)      # muted label grey
BLOOD = (214, 31, 36)
BLOOD_DK = (124, 13, 16)
FONTS = [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf",
         r"C:\Windows\Fonts\Arial.ttf"]
LABEL_FONTS = [r"C:\Windows\Fonts\BAHNSCHRIFT.TTF", r"C:\Windows\Fonts\ariblk.ttf",
               r"C:\Windows\Fonts\impact.ttf"]


def _font(size, fonts=FONTS):
    for p in fonts:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _lfont(size):
    return _font(size, LABEL_FONTS)


def _magnet(t):            # slow magnetic creep → sudden slam
    return t ** 4


def _stext(d, xy, txt, font, fill, anchor="la", tr=4):
    """Text with letter-spacing (tracking `tr`) + drop shadow. anchor: la / ra / ma."""
    x, y = xy
    ws = [font.getlength(c) for c in txt]
    total = sum(ws) + tr * max(0, len(txt) - 1)
    sx = x if anchor[0] == "l" else (x - total if anchor[0] == "r" else x - total / 2)
    for c, w in zip(txt, ws):
        d.text((sx + 2, y + 3), c, font=font, fill=(0, 0, 0, 200))
        d.text((sx, y), c, font=font, fill=fill)
        sx += w + tr


def _concrete_base(w, h):
    """Dark, warm, blotchy CONCRETE — the gritty-fight-night backdrop (replaces brushed
    steel). Large-scale stains + fine grit, tinted warm (R>G>B) so it reads as grimy wall."""
    rng = np.random.RandomState(7)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = 24.0 + 9 * np.exp(-((yy / h - 0.22) ** 2) / 0.10)          # faint top light
    base += 13 * np.sin(xx / 190.0) * np.cos(yy / 150.0)             # broad stains
    base += 8 * np.sin(xx / 46.0 + 1.4) * np.sin(yy / 61.0)          # blotches
    base += (rng.rand(h, w) - 0.5) * 20                             # grit
    base = np.clip(base, 7, 86)
    img = np.stack([base * 1.14, base * 1.0, base * 0.84], 2)        # warm grime tint
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _rivet(d, x, y):
    """A worn dark bolt (was a shiny chrome rivet)."""
    d.ellipse((x - 8, y - 8, x + 8, y + 8), fill=(18, 16, 14, 255), outline=(8, 7, 6, 255))
    d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=(54, 49, 43, 255), outline=(28, 25, 22, 255))
    d.ellipse((x - 5, y - 6, x - 1, y - 2), fill=(120, 112, 100, 200))


def _draw_splatter(d, cx, cy, rng, scale=1.0):
    """Hand-thrown blood splatter around the seam centre — a few irregular blobs + flung
    droplets, dark-red base with brighter specks."""
    for _ in range(7):
        a = rng.uniform(0, 6.283)
        r = rng.uniform(20, 150) * scale
        bx, by = cx + math.cos(a) * r, cy + math.sin(a) * r
        rr = rng.uniform(8, 34) * scale
        d.ellipse((bx - rr, by - rr * rng.uniform(.7, 1.3), bx + rr, by + rr), fill=BLOOD_DK + (190,))
    for _ in range(26):                                              # flung droplets
        a = rng.uniform(0, 6.283)
        r = rng.uniform(30, 230) * scale
        bx, by = cx + math.cos(a) * r, cy + math.sin(a) * r
        rr = rng.uniform(1.5, 6) * scale
        col = BLOOD if rng.random() < .6 else BLOOD_DK
        d.ellipse((bx - rr, by - rr, bx + rr, by + rr), fill=col + (rng.randint(150, 230),))


def _boxer(w, h, color):
    """A boxing-stance silhouette PLACEHOLDER (stands in for the real captured/uploaded
    photo) — dark figure in guard, with a soft corner-colour glow + rim so it reads."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((w * 0.12, h * 0.06, w * 0.88, h * 0.98), fill=color + (45,))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img, "RGBA")
    cx = w * 0.5
    dark = (20, 22, 26, 255)
    aw = int(0.11 * w)
    d.polygon([(cx - 0.05 * w, h * 0.55), (cx + 0.05 * w, h * 0.55),
               (cx + 0.21 * w, h * 0.99), (cx + 0.07 * w, h * 0.99)], fill=dark)   # leg
    d.polygon([(cx - 0.05 * w, h * 0.55), (cx + 0.05 * w, h * 0.55),
               (cx - 0.07 * w, h * 0.99), (cx - 0.21 * w, h * 0.99)], fill=dark)   # leg
    d.polygon([(cx - 0.30 * w, h * 0.30), (cx + 0.30 * w, h * 0.30),
               (cx + 0.17 * w, h * 0.60), (cx - 0.17 * w, h * 0.60)], fill=dark)   # torso
    d.line((cx - 0.26 * w, h * 0.33, cx - 0.16 * w, h * 0.21), fill=dark, width=aw)
    d.line((cx + 0.26 * w, h * 0.33, cx + 0.16 * w, h * 0.21), fill=dark, width=aw)
    for sx in (-0.16, 0.16):                                                        # gloves
        d.ellipse((cx + sx * w - 0.11 * w, h * 0.21 - 0.11 * w,
                   cx + sx * w + 0.11 * w, h * 0.21 + 0.11 * w), fill=dark)
    d.ellipse((cx - 0.12 * w, h * 0.05, cx + 0.12 * w, h * 0.05 + 0.22 * w), fill=dark)  # head
    return img


def _fighter_fig(box_w, box_h, color, fighter):
    """Real photo (cropped to fill) if `photo` given, else the silhouette placeholder."""
    photo = fighter.get("photo")
    if photo:
        try:
            im = Image.open(photo).convert("RGBA")
            sc = max(box_w / im.width, box_h / im.height)
            im = im.resize((int(im.width * sc), int(im.height * sc)))
            x = (im.width - box_w) // 2
            return im.crop((x, 0, x + box_w, box_h))
        except Exception:  # noqa: BLE001
            pass
    return _boxer(box_w, box_h, color)


def _load_reel(folder):
    """Load a cutout reel (PNG sequence + meta.json) → (frames, fps, n, pw, ph) or None."""
    import glob
    if not folder or not os.path.isdir(folder):
        return None
    try:
        meta = json.load(open(os.path.join(folder, "meta.json")))
    except (OSError, ValueError):
        meta = {"fps": 20, "pw": 300, "ph": 560}
    paths = sorted(glob.glob(os.path.join(folder, "[0-9]*.png")))
    if not paths:
        return None
    frames = [Image.open(p).convert("RGBA") for p in paths]
    return frames, meta.get("fps", 20), len(frames), meta.get("pw", frames[0].width), meta.get("ph", frames[0].height)


def _wedge_poly(W, H, side, d):
    cx = W // 2
    return ([(0, 0), (cx + d, 0), (cx - d, H), (0, H)] if side == "L"
            else [(cx + d, 0), (W, 0), (W, H), (cx - d, H)])


def _draw_fighter_text(img, W, H, color, side, fighter, fonts):
    """Draw a fighter's tag/name/stats/logo onto `img` (reused for the panel + the
    on-top text overlay when a reel is composited under it). Gritty palette: corner-colour
    tag, BONE name, ash labels, and worn corner-colour stat bars."""
    tag, big, lab, val = fonts
    td = ImageDraw.Draw(img, "RGBA")
    pad = 40
    left = side == "L"
    tx = pad if left else W - pad
    anc = "la" if left else "ra"
    _stext(td, (tx, 24), ("RED CORNER" if color == RED else "BLUE CORNER"), tag, color + (255,), anc, tr=6)
    _stext(td, (tx, 50), (fighter.get("name") or "FIGHTER").upper()[:13], big, BONE + (255,), anc, tr=2)
    rows = [("ELO", fighter.get("elo", "—"), .72), ("HEIGHT", fighter.get("height", "—"), .66),
            ("STYLE", fighter.get("style", "—"), None)]
    y = 170
    bw = 200
    for label, value, frac in rows:
        _stext(td, (tx, y), label, lab, ASH + (255,), anc, tr=4)
        _stext(td, (tx, y + 28), str(value)[:16], val, BONE + (255,), anc, tr=1)
        if frac:                                            # worn corner-colour rating bar
            by = y + 70
            x0 = tx if left else tx - bw
            td.rectangle((x0, by, x0 + bw, by + 9), fill=(255, 255, 255, 18), outline=(0, 0, 0, 150))
            fw = int(bw * frac)
            fx = x0 if left else x0 + bw - fw
            for sx in range(0, fw, 12):                     # hatched fill = grungy
                td.rectangle((fx + sx, by + 1, fx + sx + 8, by + 8), fill=color + (235,))
        y += 100
    logo = fighter.get("logo")
    if logo:
        try:
            lg = Image.open(logo).convert("RGBA"); lg.thumbnail((W // 2 - 80, 90))
            img.alpha_composite(lg, (pad if left else W - pad - lg.width, H - lg.height - 28))
        except Exception:  # noqa: BLE001
            pass
    else:
        _stext(td, (tx, H - 52), (fighter.get("logo_text") or "@yourchannel"), lab, ASH + (235,), anc, tr=2)


def _text_overlay(W, H, color, side, fighter, fonts, d):
    """Transparent text layer (clipped to the wedge) drawn ON TOP of the reel."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    _draw_fighter_text(img, W, H, color, side, fighter, fonts)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(_wedge_poly(W, H, side, d), fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0)); out.paste(img, (0, 0), mask)
    return out


def _panel(W, H, color, side, fighter, fonts, d, no_figure=False, no_text=False):
    """Full W×H RGBA; this fighter's wedge (left/right of the diagonal) only — gritty
    concrete wall with a corner-colour grime wash, worn cracks/bolts, and a blood-red
    fracture down the seam."""
    cx = W // 2
    poly = _wedge_poly(W, H, side, d)
    tmp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    tmp.alpha_composite(_concrete_base(W, H))
    left = side == "L"
    # soft corner-colour wash bleeding from the seam outward
    wash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wash)
    if left:
        wd.polygon([(cx - 320, 0), (cx + d, 0), (cx - d, H), (cx - 520, H)], fill=color + (52,))
    else:
        wd.polygon([(cx + d, 0), (cx + 320, 0), (cx + 520, H), (cx - d, H)], fill=color + (46,))
    tmp.alpha_composite(wash.filter(ImageFilter.GaussianBlur(70)))
    td = ImageDraw.Draw(tmp, "RGBA")
    crng = random.Random(3 if left else 9)                  # worn hairline cracks
    for _ in range(5):
        x = crng.randint(40, W - 40); y = 0; pts = [(x, y)]
        for _s in range(6):
            x += crng.randint(-34, 34); y += crng.randint(80, 150); pts.append((x, y))
        td.line(pts, fill=(0, 0, 0, 80), width=2)
    # fighter portrait (real photo or silhouette placeholder), inner side toward the seam.
    # Skipped when a live REEL is supplied — the reel is composited per-frame instead.
    if not no_figure:
        fb_w, fb_h = 230, 520
        fx = 300 if left else W - 300 - fb_w
        tmp.alpha_composite(_fighter_fig(fb_w, fb_h, color, fighter), (fx, 150))
    if not no_text:
        _draw_fighter_text(tmp, W, H, color, side, fighter, fonts)
    for rx in (52, W - 52):                                 # a couple of worn bolts in the corners
        _rivet(td, rx, 30); _rivet(td, rx, H - 30)
    # clip everything to the wedge, then draw the blood fracture ON the seam
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(tmp, (0, 0), mask)
    ed = ImageDraw.Draw(out, "RGBA")
    top, bot = (cx + d, 0), (cx - d, H)
    for gw, col, ga in ((26, BLOOD_DK, 70), (14, (180, 20, 24), 140),
                        (6, BLOOD, 220), (2, (255, 150, 120), 255)):  # glow → hot core
        ed.line([top, bot], fill=col + (ga,), width=gw)
    ed.line((0, 0, W, 0), fill=(214, 210, 200, 70), width=2)         # worn top edge
    ed.line((0, H - 1, W, H - 1), fill=(0, 0, 0, 210), width=5)      # dark bottom
    ed.line(((0 if left else W - 2), 0, (0 if left else W - 2), H), fill=color + (170,), width=5)
    return out


def render_vs_intro(out, left, right, left_color=RED, right_color=BLUE,
                    W=1280, H=720, fps=30, seconds=4.4, title="TALE OF THE TAPE",
                    left_reel=None, right_reel=None, outro_shatter=True):
    rnd = random.Random(7)
    fonts = (_font(22), _font(58), _lfont(23), _font(40))   # tag / name / label / value
    vs_f, ttl = _font(120), _font(30)
    d = 150
    cx = W // 2
    top, bot = (cx + d, 0), (cx - d, H)
    Lr = _load_reel(left_reel)
    Rr = _load_reel(right_reel)
    Lp = _panel(W, H, left_color, "L", left, fonts, d, no_figure=bool(Lr), no_text=bool(Lr))
    Rp = _panel(W, H, right_color, "R", right, fonts, d, no_figure=bool(Rr), no_text=bool(Rr))
    # when a reel plays, the text is drawn ON TOP of it (top layer)
    Lt = _text_overlay(W, H, left_color, "L", left, fonts, d) if Lr else None
    Rt = _text_overlay(W, H, right_color, "R", right, fonts, d) if Rr else None
    slam = int(1.15 * fps)                                  # longer magnetic approach
    # if there are reels, run ~8s of cycling highlights (the reel loops via %n to fill);
    # never cut a single pass short either.
    reel_dur = max((r[2] / r[1] for r in (Lr, Rr) if r), default=0)
    if reel_dur:
        seconds = max(seconds, 8.0, 1.15 + min(reel_dur, 6.8) + 0.4)
    nfr = int(seconds * fps)
    # crackle-and-shatter outro that transitions INTO round 1
    crackle_n = int(0.45 * fps) if outro_shatter else 0
    break_n = int(0.85 * fps) if outro_shatter else 0
    # Fill the WHOLE wedge with the cutout so the fighter is as BIG as the card allows.
    # The reel is scaled to the card HEIGHT and centred in its half; the ONLY things that
    # clip the body are the card's own diagonal seam (the wedge) and the screen edge — never
    # an inner box. So any cutoff lands on a real border, exactly as the user asked.
    def _prep_reel(reel, side):
        if not reel:
            return None
        frames, rfps, n, pw, ph = reel
        sc = H / ph                                          # fill the full card height
        nw = max(1, int(pw * sc))
        sframes = [fr.resize((nw, H)) for fr in frames]
        cxp = int(W * 0.225) if side == "L" else int(W * 0.775)   # centre of this fighter's half
        px = cxp - nw // 2
        wmask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(wmask).polygon(_wedge_poly(W, H, side, d), fill=255)
        return {"frames": sframes, "fps": rfps, "n": n, "px": px, "wmask": wmask}
    Lprep = _prep_reel(Lr, "L")
    Rprep = _prep_reel(Rr, "R")
    sparks = []

    def diag_pt(u):                                         # point along the seam, u in 0..1
        return (cx + d - 2 * d * u, H * u)

    base = Image.new("RGB", (W, H), (6, 7, 10))
    total = nfr + crackle_n + break_n
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
    sfx = None
    if outro_shatter:                                        # crackle whoosh + shatter impact + bell
        try:
            from replay_sfx import ensure_assets, ensure_bell
            whoosh, impact = ensure_assets()
            bell = ensure_bell()
            crackle_ms = int(nfr / fps * 1000)
            break_ms = int((nfr + crackle_n) / fps * 1000)
            cmd += ["-i", str(whoosh), "-i", str(impact), "-i", str(bell),
                    "-filter_complex",
                    f"[1:a]volume=0.5,adelay={crackle_ms}:all=1[wa];"
                    f"[2:a]volume=0.95,adelay={break_ms}:all=1[ia];"
                    f"[3:a]volume=0.7,adelay={break_ms}:all=1[ba];"
                    f"[wa][ia][ba]amix=inputs=3:normalize=0:duration=longest,"
                    f"aresample=48000,aformat=channel_layouts=stereo[a]",
                    "-map", "0:v", "-map", "[a]"]
            sfx = True
        except Exception:  # noqa: BLE001 — fall back to silent if SFX unavailable
            sfx = None
    if not sfx:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a"]
    cmd += ["-t", f"{total / fps:.3f}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "18", "-c:a", "aac", "-movflags", "+faststart", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    # ── gritty finishing assets (precomputed once) ──────────────────────────────
    splat = Image.new("RGBA", (W, H), (0, 0, 0, 0))         # blood over the seam centre
    _draw_splatter(ImageDraw.Draw(splat), cx, H // 2, random.Random(21), scale=1.15)
    grng = np.random.RandomState(12)                        # film-grain tiles (cycled)
    GRAIN_N = 10
    grain = [((grng.rand(H, W) - 0.5) * 30).astype(np.int16) for _ in range(GRAIN_N)]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)          # vignette (1 centre → ~.45 corners)
    rr = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    vign = np.clip(1.05 - 0.46 * np.clip(rr - 0.2, 0, 1.3) ** 1.6, 0.45, 1.05)[..., None].astype(np.float32)

    def _finish(img, fi):                                   # vignette + grain + worn frame
        a = np.asarray(img, dtype=np.float32) * vign
        a += grain[fi % GRAIN_N][..., None]
        np.clip(a, 0, 255, out=a)
        a = a.astype(np.uint8)
        a[:6, :] = 12; a[-6:, :] = 12; a[:, :6] = 12; a[:, -6:] = 12        # dark worn frame
        a[6:8, :] = BONE; a[-8:-6, :] = BONE; a[:, 6:8] = BONE; a[:, -8:-6] = BONE  # bone inner rule
        return a

    for f in range(nfr):
        im = base.copy()
        if f < slam:
            t = _magnet(f / max(1, slam))
            lx = int(-W + W * t); rx = int(W - W * t); shake = 0
        else:
            age = f - slam
            recoil = max(0, 10 - age) if age < 14 else 0
            lx, rx = -recoil, recoil; shake = int(max(0, 16 - age * 1.6) * (rnd.random() - 0.5))
            if age in (0, 4):                               # two spark bursts → linger longer
                for _ in range(90):
                    u = rnd.random(); px, py = diag_pt(u)
                    sparks.append({"x": px, "y": py, "vx": rnd.choice((-1, 1)) * rnd.uniform(8, 34),
                                   "vy": rnd.uniform(-16, 16), "life": rnd.randint(14, 34), "age": 0,
                                   "col": rnd.choice([(255, 240, 120), (255, 170, 40), (255, 90, 30), (255, 255, 255)])})
        im.paste(Lp, (lx, shake), Lp)
        im.paste(Rp, (rx, -shake), Rp)
        if Lprep:                                           # cycling cutout reel, fills the wedge
            rf = Lprep["frames"][int((f / fps) * Lprep["fps"]) % Lprep["n"]]
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            layer.paste(rf, (Lprep["px"], 0), rf)           # placed in panel-local coords
            layer.putalpha(ImageChops.multiply(layer.split()[3], Lprep["wmask"]))  # clip to seam
            im.paste(layer, (lx, shake), layer)             # then move with the sliding panel
        if Rprep:
            rf = Rprep["frames"][int((f / fps) * Rprep["fps"]) % Rprep["n"]]
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            layer.paste(rf, (Rprep["px"], 0), rf)
            layer.putalpha(ImageChops.multiply(layer.split()[3], Rprep["wmask"]))
            im.paste(layer, (rx, -shake), layer)
        if Lt:                                              # text ON TOP of the reel
            im.paste(Lt, (lx, shake), Lt)
        if Rt:
            im.paste(Rt, (rx, -shake), Rt)
        if f >= slam:
            age = f - slam
            sa = min(1.0, age / 6.0)                         # blood splatter fades in on impact
            if sa >= 1.0:
                im.paste(splat, (0, 0), splat)
            else:
                sp = splat.copy(); sp.putalpha(sp.split()[3].point(lambda p: int(p * sa)))
                im.paste(sp, (0, 0), sp)
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
            if age < 7:
                ov.paste((255, 248, 240, int(165 * (1 - age / 7))), (0, 0, W, H))
            for gw, col, ga in ((34, (180, 20, 24), 50), (18, BLOOD, 110),
                                (7, (255, 150, 120), 210), (2, (255, 240, 230), 255)):  # blood-hot seam
                od.line([top, bot], fill=col + (ga,), width=gw)
            for s in sparks:
                if s["age"] >= s["life"]:
                    continue
                a = s["age"]; x = s["x"] + s["vx"] * a; y = s["y"] + s["vy"] * a + 0.5 * a * a
                fade = int(255 * (1 - a / s["life"])); col = s["col"] + (fade,)
                od.line((x, y, x - s["vx"] * 0.6, y - s["vy"] * 0.6), fill=col, width=3)
                od.ellipse((x - 2, y - 2, x + 2, y + 2), fill=col)
                s["age"] += 1
            im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
            d2 = ImageDraw.Draw(im, "RGBA")
            if title:                                        # distressed header plate + blood underline
                tw = ttl.getlength(title)
                d2.rectangle((cx - tw / 2 - 26, 14, cx + tw / 2 + 26, 56), fill=(10, 8, 7, 232))
                d2.rectangle((cx - tw / 2 - 26, 54, cx + tw / 2 + 26, 57), fill=BLOOD + (255,))
                _stext(d2, (cx, 21), title, ttl, BONE + (255,), "ma", tr=5)
            rad = int(70 * min(1.0, age / 6))
            if rad > 4:
                for gw, ga in ((rad + 16, 60), (rad + 7, 120)):   # red glow ring behind the badge
                    d2.ellipse((cx - gw, H // 2 - gw, cx + gw, H // 2 + gw), outline=BLOOD + (ga,), width=6)
                d2.ellipse((cx - rad, H // 2 - rad, cx + rad, H // 2 + rad),
                           fill=(14, 11, 10, 255), outline=BONE + (255,), width=7)
                d2.ellipse((cx - rad + 9, H // 2 - rad + 9, cx + rad - 9, H // 2 + rad - 9),
                           outline=(0, 0, 0, 160), width=2)
                if rad > 36:
                    _stext(d2, (cx, H // 2 - vs_f.size // 2 + 4), "VS", vs_f, BONE + (255,), "ma", tr=2)
        proc.stdin.write(_finish(im, f).tobytes())

    # ── crackle + shatter outro → transition INTO round 1 ──────────────────────
    if outro_shatter:
        card = Image.fromarray(_finish(im, nfr - 1))  # settled card WITH the gritty finish baked in
        rnd2 = random.Random(99)
        cracks = []                                 # jagged fracture lines from the seam
        for _ in range(8):
            ang = rnd2.uniform(0, 2 * math.pi)
            x, y = float(cx), H / 2.0
            pts = [(x, y)]
            for _s in range(rnd2.randint(5, 9)):
                ang += rnd2.uniform(-0.55, 0.55)
                step = rnd2.uniform(45, 95)
                x += math.cos(ang) * step; y += math.sin(ang) * step
                pts.append((x, y))
            cracks.append(pts)
        for cf in range(crackle_n):                 # cracks creep outward + electric flicker
            fr = card.copy()
            dd = ImageDraw.Draw(fr, "RGBA")
            prog = (cf + 1) / max(1, crackle_n)
            flick = 255 if (cf % 3) else 150
            for pts in cracks:
                seg = pts[:max(2, int(len(pts) * prog))]
                for a, b in zip(seg, seg[1:]):
                    dd.line([a, b], fill=(235, 244, 255, flick), width=3)
                    dd.line([a, b], fill=(255, 255, 255, 200), width=1)
                tip = seg[-1]                        # hot spark at the advancing crack tip
                dd.ellipse((tip[0] - 3, tip[1] - 3, tip[0] + 3, tip[1] + 3),
                           fill=(255, 250, 210, 230))
            if cf % 4 == 0:                          # subtle electric white flash
                fr = Image.alpha_composite(fr.convert("RGBA"),
                                           Image.new("RGBA", (W, H), (255, 255, 255, 26))).convert("RGB")
            proc.stdin.write(np.asarray(fr, dtype=np.uint8).tobytes())
        # break apart: slice the card on a grid, fly the shards outward, reveal black
        GX, GY = 12, 7
        tw, th = W / GX, H / GY
        shards = []
        for gy in range(GY):
            for gx in range(GX):
                box = (int(gx * tw), int(gy * th), int((gx + 1) * tw), int((gy + 1) * th))
                bx, by = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                dx, dy = bx - W / 2, by - H / 2
                norm = max(1.0, math.hypot(dx, dy))
                spd = rnd2.uniform(8, 19)
                shards.append({"img": card.crop(box).convert("RGBA"), "pos": (box[0], box[1]),
                               "vx": dx / norm * spd + rnd2.uniform(-3, 3),
                               "vy": dy / norm * spd + rnd2.uniform(-3, 3) - 5,
                               "spin": rnd2.uniform(-11, 11), "delay": rnd2.randint(0, 4)})
        bsparks = []
        black = Image.new("RGB", (W, H), (6, 7, 10))
        for bf in range(break_n):
            fr = black.copy()
            for sh in shards:
                a2 = bf - sh["delay"]
                if a2 < 0:
                    fr.paste(sh["img"].convert("RGB"), sh["pos"]); continue
                fade = max(0.0, 1.0 - a2 / max(1, break_n - 2))
                if fade <= 0.02:
                    continue
                x = sh["pos"][0] + sh["vx"] * a2
                y = sh["pos"][1] + sh["vy"] * a2 + 0.6 * a2 * a2
                pc = sh["img"].rotate(sh["spin"] * a2, expand=True)
                if fade < 0.99:
                    pc.putalpha(pc.split()[3].point(lambda v: int(v * fade)))
                fr.paste(pc, (int(x), int(y)), pc)
            ovr = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ovr)
            if bf == 0:                              # spark burst at the shatter
                for _ in range(140):
                    a = rnd2.uniform(0, 2 * math.pi); sp = rnd2.uniform(10, 40)
                    bsparks.append({"x": W / 2, "y": H / 2, "vx": math.cos(a) * sp,
                                    "vy": math.sin(a) * sp, "life": rnd2.randint(12, 30), "age": 0,
                                    "col": rnd2.choice([(255, 240, 120), (255, 170, 40),
                                                        (255, 90, 30), (255, 255, 255)])})
            if bf < 5:                               # white slam flash
                ovr.paste((255, 255, 255, int(170 * (1 - bf / 5))), (0, 0, W, H))
            for s in bsparks:
                if s["age"] >= s["life"]:
                    continue
                aa = s["age"]; x = s["x"] + s["vx"] * aa; y = s["y"] + s["vy"] * aa + 0.5 * aa * aa
                fd = int(255 * (1 - aa / s["life"])); col = s["col"] + (fd,)
                od.line((x, y, x - s["vx"] * 0.5, y - s["vy"] * 0.5), fill=col, width=3)
                od.ellipse((x - 2, y - 2, x + 2, y + 2), fill=col); s["age"] += 1
            fr = Image.alpha_composite(fr.convert("RGBA"), ovr).convert("RGB")
            proc.stdin.write(np.asarray(fr, dtype=np.uint8).tobytes())

    proc.stdin.close(); proc.wait()
    return out


if __name__ == "__main__":
    render_vs_intro(
        "vs_demo.mp4",
        {"name": "You", "elo": "1450-1600", "height": "5'11\"", "style": "Pressure",
         "logo_text": "@yourchannel"},
        {"name": "Iron Mike", "elo": "1700+", "height": "5'10\"", "style": "Peek-a-boo",
         "logo_text": "THRILL OF THE FIGHT"})
    print("wrote vs_demo.mp4")
