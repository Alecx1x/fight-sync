"""Verify CSRT region tracking follows a moving target."""
import subprocess
from pathlib import Path

import numpy as np  # noqa: F401  (also confirms numpy still imports after cv2 install)

from media import FFMPEG
from tracking import track_region

T = Path(__file__).parent / "_test"
T.mkdir(exist_ok=True)


def main():
    # red 60x60 square moving left->right across a dark 640x360 frame
    vid = T / "moving.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x101418:s=640x360:d=4:r=30",
         "-f", "lavfi", "-i", "color=c=red:s=60x60:d=4:r=30",
         "-filter_complex", "[0][1]overlay=x='40+t*120':y=150",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid)],
        check=True,
    )

    print("== TRACKING TEST ==")
    # seed on the square at t=0.2: it's near x=40+0.2*120=64 -> center ~ (94,180)
    seed = (64 / 640, 150 / 360, 60 / 640, 60 / 360)
    track = track_region(str(vid), 0.2, 2.5, seed)
    print(f"  track points: {len(track)}")
    assert len(track) > 20, "tracker produced too few points"
    cx0, cx1 = track[0][1], track[-1][1]
    print(f"  start center x={cx0:.3f}  ->  end center x={cx1:.3f}")
    # square moves right ~120 px/s over ~2.5s = ~300px = ~0.47 of width
    assert cx1 - cx0 > 0.25, f"track did not follow the moving target (dx={cx1-cx0:.3f})"
    # y should stay roughly constant
    assert abs(track[-1][2] - track[0][2]) < 0.1, "y drifted unexpectedly"
    print("  PASS - tracker followed the moving region\n")
    print("TRACKING TEST PASSED")


if __name__ == "__main__":
    main()
