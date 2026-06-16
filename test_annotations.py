"""Verify the annotation overlay renderer burns markers into a video correctly,
by sampling pixels of a rendered frame.
"""
import subprocess
from pathlib import Path

from PIL import Image

from media import FFMPEG, probe
from annotations import render_overlay
from pipeline import _enc

T = Path(__file__).parent / "_test"
T.mkdir(exist_ok=True)


def build_base():
    out = T / "base.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x101418:s=640x360:d=3:r=30",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
         "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(out)],
        check=True,
    )
    return str(out)


def main():
    base = build_base()
    anns = [
        {"type": "circle", "color": "red", "nx": .5, "ny": .5, "nr": .18,
         "start": 0.0, "end": 3.0, "label": "GUARD DOWN", "pulse": True},
        {"type": "circle", "color": "green", "nx": .2, "ny": .25, "nr": .1,
         "start": 0.0, "end": 3.0, "label": "good slip"},
        {"type": "arrow", "color": "green", "nx": .7, "ny": .8,
         "nx2": .85, "ny2": .6, "start": 0.0, "end": 3.0},
        {"type": "zone", "color": "yellow", "nx": .6, "ny": .15,
         "nw": .25, "nh": .18, "start": 0.0, "end": 3.0},
        {"type": "text", "color": "white", "nx": .5, "ny": .9,
         "text": "ROTATE HIPS", "size": .06, "start": 0.0, "end": 3.0},
    ]
    print("== OVERLAY RENDER TEST ==")
    out = str(T / "annotated.mp4")
    render_overlay(base, anns, out, _enc(20, "veryfast"),
                   on_progress=lambda p: None)
    info = probe(out)
    print(f"  rendered {info.width}x{info.height}, {info.duration:.1f}s")
    assert info.width == 640 and info.height == 360

    # sample a mid frame
    frame = T / "frame.png"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", "1.5", "-i", out,
         "-frames:v", "1", str(frame)], check=True)
    im = Image.open(frame).convert("RGB")

    cx, cy = int(.5 * 640), int(.5 * 360)           # on the red circle's ring
    # scan a small horizontal band at circle center height for a red ring pixel
    found_red = False
    for x in range(cx - 130, cx + 130):
        r, g, b = im.getpixel((x, cy))
        if r > 150 and g < 110 and b < 110:
            found_red = True
            break
    empty = im.getpixel((20, 340))                   # bottom-left, no marker there
    print(f"  red ring present near center: {found_red}")
    print(f"  empty corner pixel (should be dark bg): {empty}")
    assert found_red, "red circle not found in rendered frame"
    assert empty[0] < 60 and empty[1] < 60 and empty[2] < 60, "overlay leaked into empty area"
    print("  PASS - markers burned in, empty areas untouched\n")
    print("ANNOTATION RENDER TEST PASSED")


if __name__ == "__main__":
    main()
