"""Ninja Capture — two-fighter broadcast composite.

Lays out a collab match as a broadcast: the SPECTATOR gameplay view fills the frame as
the fixed background, and each fighter's camera sits as a picture-in-picture in a bottom
corner (fighter 1 = bottom-left, fighter 2 = bottom-right). Each corner feed can be a
single clip OR a pre-directed feed from autodirect.py (when a fighter has multiple angles,
their best angle is chosen per-moment before it ever reaches here).

Deliberately standalone — reuses media.py's ffmpeg helpers but does NOT touch the main
render pipeline. Recorded output (not live). The spectator clip is the spine: the output
runs its length; a corner cam that ends early simply drops out (its corner goes away).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from media import probe, run_ffmpeg


@dataclass
class Corner:
    """One fighter's corner feed."""
    path: str
    offset: float = 0.0   # spectatorTime - camTime (reserved for capture-sync; 0 = aligned)


def _cover(w: int, h: int) -> str:
    """Scale-to-fill WxH then crop the overflow (no letterboxing)."""
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def build_broadcast(
    spectator: str,
    cam_left: Optional[str],
    cam_right: Optional[str],
    out: str,
    *,
    w: int = 1920,
    h: int = 1080,
    cam_frac: float = 0.26,     # corner cam width as a fraction of the frame width
    margin_frac: float = 0.028,  # gap from the frame edges
    border: int = 4,
    border_color: str = "white",
    audio: str = "mix",         # "mix" (spectator + both cams) | "spectator"
    crf: int = 20,
    preset: str = "veryfast",
    on_log=None,
) -> str:
    """Composite the broadcast layout → `out` (mp4). Returns `out`.

    cam_left/cam_right may be None (that corner is simply omitted), so this also
    handles a 1-fighter or spectator-only case gracefully.
    """
    si = probe(spectator)
    out_dur = si.duration
    camw = int(w * cam_frac)
    margin = int(w * margin_frac)

    inputs = ["-i", spectator]
    corners = []  # (input_index, position) position in {"left","right"}
    idx = 1
    if cam_left:
        inputs += ["-i", cam_left]; corners.append((idx, "left")); idx += 1
    if cam_right:
        inputs += ["-i", cam_right]; corners.append((idx, "right")); idx += 1

    # Video graph: spectator background, then overlay each corner cam.
    parts = [f"[0:v]{_cover(w, h)},setsar=1[bg]"]
    last = "bg"
    for n, (i, pos) in enumerate(corners):
        # scale to corner width, add a border via pad
        inner = max(2, camw - 2 * border)
        parts.append(
            f"[{i}:v]scale={inner}:-2,pad=iw+{2*border}:ih+{2*border}:{border}:{border}:{border_color},setsar=1[c{n}]"
        )
        if pos == "left":
            xy = f"x={margin}:y=H-h-{margin}"
        else:
            xy = f"x=W-w-{margin}:y=H-h-{margin}"
        tag = "v" if n == len(corners) - 1 else f"t{n}"
        parts.append(f"[{last}][c{n}]overlay={xy}:eof_action=pass[{tag}]")
        last = tag
    if not corners:
        parts[-1] = parts[-1].replace("[bg]", "[v]")  # spectator-only → label the output [v]

    # Audio graph.
    a_inputs = [f"[{j}:a]" for j in range(idx)]
    if audio == "mix" and len(a_inputs) > 1:
        # spectator carries the match/commentary; fighters' mics mixed under it
        amix = "".join(a_inputs) + f"amix=inputs={len(a_inputs)}:duration=first:normalize=0[a]"
        parts.append(amix)
        amap = ["-map", "[a]"]
    else:
        amap = ["-map", "0:a?"]

    filt = ";".join(parts)
    args = [
        *inputs,
        "-filter_complex", filt,
        "-map", "[v]", *amap,
        "-t", f"{out_dur:.3f}",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart",
        out,
    ]
    run_ffmpeg(args, on_log=on_log)
    return out
