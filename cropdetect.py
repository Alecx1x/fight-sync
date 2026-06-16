"""Auto-detect the gameplay viewport in a broadcast layout.

The in-game view is in constant motion; scoreboards, names, logos and borders are
static. We sample frames, compute per-pixel temporal variance, and take the
largest connected high-variance region as the gameplay rectangle. The user
confirms/adjusts it once; the result is saved as that channel's crop template.
"""
from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path

import numpy as np

from media import FFMPEG, probe

SW, SH = 192, 108          # low-res sampling grid for motion analysis


def _grab_gray(video: str, t: float) -> np.ndarray | None:
    out = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", video,
         "-frames:v", "1", "-vf", f"scale={SW}:{SH}", "-pix_fmt", "gray",
         "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    if len(out) < SW * SH:
        return None
    return np.frombuffer(out[: SW * SH], dtype=np.uint8).reshape(SH, SW).astype(np.float32)


def _largest_component(mask: np.ndarray):
    """Return (minr, minc, maxr, maxc, area) of the largest 4-connected blob."""
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    best, best_area = None, 0
    for i in range(h):
        for j in range(w):
            if mask[i, j] and not visited[i, j]:
                q = deque([(i, j)])
                visited[i, j] = True
                minr = maxr = i
                minc = maxc = j
                area = 0
                while q:
                    r, c = q.popleft()
                    area += 1
                    minr, maxr = min(minr, r), max(maxr, r)
                    minc, maxc = min(minc, c), max(maxc, c)
                    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < h and 0 <= nc < w and mask[nr, nc] and not visited[nr, nc]:
                            visited[nr, nc] = True
                            q.append((nr, nc))
                if area > best_area:
                    best_area, best = area, (minr, minc, maxr, maxc)
    return best, best_area


def _even(n: int) -> int:
    return int(n) - (int(n) % 2)


def detect_crop(video: str, work: str, n: int = 22) -> dict:
    info = probe(video)
    fw, fh = info.width, info.height
    dur = info.duration or 0

    full = {"x": 0, "y": 0, "w": fw, "h": fh,
            "frame_w": fw, "frame_h": fh, "auto": False}

    if dur <= 0:
        return full
    times = np.linspace(0.05 * dur, 0.95 * dur, n)
    frames = [f for f in (_grab_gray(video, float(t)) for t in times) if f is not None]
    if len(frames) < 4:
        return full

    stack = np.stack(frames, axis=0)
    var_map = stack.std(axis=0)                      # (SH, SW)

    thr = max(float(var_map.mean()) * 1.15,
              float(np.percentile(var_map, 65)))
    mask = var_map > thr
    if mask.sum() < 0.02 * SW * SH:                  # almost nothing moved
        return full

    comp, area = _largest_component(mask)
    if comp is None or area < 0.03 * SW * SH:
        return full
    minr, minc, maxr, maxc = comp

    # map small-grid bbox -> original resolution (fractional, aspect-agnostic)
    x = _even(minc / SW * fw)
    y = _even(minr / SH * fh)
    w = _even((maxc - minc + 1) / SW * fw)
    h = _even((maxr - minr + 1) / SH * fh)
    w = min(w, fw - x)
    h = min(h, fh - y)

    # if it basically fills the frame, treat as no crop needed
    if w >= 0.95 * fw and h >= 0.95 * fh:
        return full

    return {"x": x, "y": y, "w": w, "h": h,
            "frame_w": fw, "frame_h": fh, "auto": True}


def preview_frame(video: str, crop: dict, out_png: str) -> None:
    """Grab a representative frame with the proposed crop drawn on it."""
    info = probe(video)
    t = (info.duration or 2) * 0.5
    vf = (f"drawbox=x={crop['x']}:y={crop['y']}:w={crop['w']}:h={crop['h']}:"
          f"color=red@0.9:t=5")
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video,
         "-frames:v", "1", "-vf", vf, out_png],
        check=True,
    )


def apply_crop(video: str, crop: dict, out_path: str, enc: list[str]) -> str:
    """Crop the video to the gameplay rectangle (re-encode video, keep audio)."""
    vf = f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", video, "-vf", vf, *enc, out_path],
        check=True,
    )
    return out_path
