"""Manual cutout-cleanup for a fighter REEL (the PNG sequence used by the VS intro).

The user can't easily re-matte a moving clip by hand, so this gives a few SAFE, global
controls + a hand-painted mask that only ever REMOVE background or tighten edges — they can
never eat into the solid body unless the user paints there themselves. So limbs/joints stay.

Operations (applied to every frame, always re-derived from a pristine backup so it's
non-destructive and re-editable):
  • tighten  — alpha black-point: faint partial-alpha background haze → fully transparent,
               while the solid body (alpha 255) is untouched. Kills "background leak".
  • choke    — erode the matte a few px (pull the edge inside the fringe).
  • feather  — soften the edge.
  • mask     — a user-painted L mask (255 keep / 0 remove): a KEEP-ZONE polygon (everything
               outside → gone) + ERASE brush strokes (painted blobs → gone). Multiplies alpha.

`apply()` reads from `<reel>/orig/` (created on first run) and writes the cleaned frames back
to the reel root. `reset()` restores the originals.
"""
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def _frames(d):
    return sorted(str(p) for p in Path(d).glob("[0-9]*.png"))


def _ensure_backup(reel_dir):
    """Copy pristine frames into <reel>/orig/ once; return the orig dir."""
    orig = Path(reel_dir) / "orig"
    if not orig.exists() or not _frames(orig):
        orig.mkdir(exist_ok=True)
        for p in _frames(reel_dir):
            shutil.copy(p, orig / Path(p).name)
    return orig


def reset(reel_dir):
    orig = Path(reel_dir) / "orig"
    if orig.exists():
        for p in _frames(orig):
            shutil.copy(p, Path(reel_dir) / Path(p).name)
    return len(_frames(reel_dir))


def apply(reel_dir, tighten=0, choke=0, feather=0, mask_path=None, progress=None):
    """Re-derive every frame from the pristine backup with the cleanup applied.
    tighten 0..230 (alpha black-point), choke 0..8 px, feather 0..6 px, optional L mask."""
    reel_dir = str(reel_dir)
    orig = _ensure_backup(reel_dir)
    src = _frames(orig)
    if not src:
        raise RuntimeError("reel has no frames")

    mask = None
    if mask_path and os.path.exists(mask_path):
        mask = np.asarray(Image.open(mask_path).convert("L"), dtype=np.float32) / 255.0

    tighten = max(0, min(230, int(tighten)))
    choke = max(0, min(8, int(choke)))
    feather = max(0, min(6, int(feather)))
    kel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (choke * 2 + 1, choke * 2 + 1)) if choke else None

    n = len(src)
    for i, p in enumerate(src):
        im = Image.open(p).convert("RGBA")
        arr = np.asarray(im).copy()
        a = arr[..., 3].astype(np.float32)
        if tighten:                                   # black-point: haze → 0, body (255) stays
            a = np.clip((a - tighten) / max(1, 255 - tighten), 0, 1) * 255.0
        if kel is not None:                           # choke the edge inward
            a = cv2.erode(a.astype(np.uint8), kel, iterations=1).astype(np.float32)
        if feather:                                   # soften
            k = feather * 2 + 1
            a = cv2.GaussianBlur(a, (k, k), 0)
        if mask is not None:                          # user keep-zone + erase (REMOVE only)
            m = mask
            if m.shape[:2] != a.shape[:2]:
                m = cv2.resize(m, (a.shape[1], a.shape[0]))
            a = a * m
        arr[..., 3] = np.clip(a, 0, 255).astype(np.uint8)
        Image.fromarray(arr, "RGBA").save(os.path.join(reel_dir, Path(p).name))
        if progress and (i % 12 == 0 or i == n - 1):
            progress(int((i + 1) / n * 100))
    return n


if __name__ == "__main__":
    import sys
    print("frames:", apply(sys.argv[1], tighten=int(sys.argv[2]) if len(sys.argv) > 2 else 40))
