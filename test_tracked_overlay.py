"""End-to-end: track a moving target, attach the track to a marker, render, and
confirm the marker followed the motion (it's over the target at the END, and no
longer where it started).
"""
import subprocess
from pathlib import Path

from PIL import Image

from media import FFMPEG, probe
from tracking import track_region
from annotations import render_overlay
from pipeline import _enc

T = Path(__file__).parent / "_test"
T.mkdir(exist_ok=True)


def is_green(px):
    r, g, b = px
    return g > 150 and r < 130 and b < 175


def main():
    vid = T / "moving.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x101418:s=640x360:d=4:r=30",
         "-f", "lavfi", "-i", "color=c=red:s=60x60:d=4:r=30",
         "-filter_complex", "[0][1]overlay=x='40+t*120':y=150",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid)],
        check=True,
    )

    print("== TRACKED OVERLAY TEST ==")
    seed = (64 / 640, 150 / 360, 60 / 640, 60 / 360)
    track = track_region(str(vid), 0.2, 2.5, seed)
    c0 = track[0]            # [t,cx,cy,w,h] at start
    ann = {"type": "circle", "color": "green", "nx": c0[1], "ny": c0[2],
           "nr": 0.11, "start": 0.2, "end": 2.7, "pulse": False, "track": track}

    out = str(T / "tracked.mp4")
    render_overlay(str(vid), [ann], out, _enc(20, "veryfast"))
    assert probe(out).duration > 2

    # frame near the end: square center has moved to ~x=0.57
    end_cx = track[-1][1]
    frame = T / "end.png"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", "2.4", "-i", out,
                    "-frames:v", "1", str(frame)], check=True)
    im = Image.open(frame).convert("RGB")
    W, H = im.size
    cy = int(track[-1][2] * H)

    def green_near(cx_norm):
        cx = int(cx_norm * W)
        for x in range(max(0, cx - 90), min(W, cx + 90)):
            if is_green(im.getpixel((x, cy))):
                return True
        return False

    at_end = green_near(end_cx)
    at_start = green_near(track[0][1])
    print(f"  end center x={end_cx:.3f}: green ring present = {at_end}")
    print(f"  original start x={track[0][1]:.3f}: green ring present = {at_start}")
    assert at_end, "marker did not follow target to its end position"
    assert not at_start, "marker stayed at start instead of following"
    print("  PASS - marker followed the moving target\n")
    print("TRACKED OVERLAY TEST PASSED")


if __name__ == "__main__":
    main()
