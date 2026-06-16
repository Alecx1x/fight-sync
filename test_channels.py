"""Validate auto-crop detection, crop apply, and the channels/live API.
Requires the server running on 127.0.0.1:8765 for the API portion.
"""
import subprocess
from pathlib import Path

import requests

from media import FFMPEG, probe
from cropdetect import detect_crop, apply_crop
from pipeline import _enc

T = Path(__file__).parent / "_test"
T.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8765"

# A synthetic "broadcast layout": static dark canvas + a static blue scoreboard
# bar on top, with a MOVING testsrc2 region inset at (320,180) sized 640x360.
GX, GY, GW, GH = 320, 180, 640, 360


def build_layout():
    out = T / "layout.mp4"
    fc = (f"[1:v]format=yuv420p[g];"
          f"[0:v][g]overlay={GX}:{GY}[o];"
          f"[o]drawbox=x=0:y=0:w=1280:h=48:color=blue:t=fill,"
          f"drawbox=x=0:y=668:w=1280:h=52:color=green:t=fill[v]")
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=0x202020:s=1280x720:d=6:r=30",
         "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=6",
         "-filter_complex", fc, "-map", "[v]", "-an", str(out)],
        check=True,
    )
    return str(out)


def main():
    print("== CROP DETECTION TEST ==")
    vid = build_layout()
    crop = detect_crop(vid, str(T))
    print(f"  expected ~ x={GX} y={GY} w={GW} h={GH}")
    print(f"  detected   x={crop['x']} y={crop['y']} w={crop['w']} h={crop['h']} "
          f"(auto={crop['auto']})")
    # tolerate small error from the low-res motion grid (~1 cell = ~7px x / ~7px y)
    assert abs(crop["x"] - GX) < 40 and abs(crop["y"] - GY) < 40, "crop origin off"
    assert abs(crop["w"] - GW) < 60 and abs(crop["h"] - GH) < 60, "crop size off"
    print("  PASS - found the moving gameplay region, ignored static bars\n")

    print("== CROP APPLY TEST ==")
    outc = T / "cropped.mp4"
    apply_crop(vid, crop, str(outc), _enc(20, "veryfast"))
    info = probe(str(outc))
    print(f"  cropped output: {info.width}x{info.height}")
    assert info.width == crop["w"] and info.height == crop["h"]
    print("  PASS - crop applied to exact rectangle\n")

    print("== CHANNELS / LIVE API TEST ==")
    s = requests.Session(); s.trust_env = False
    s.post(f"{BASE}/login", data={"password": open("fightsync-secret.txt").read().strip(), "next": "/"})
    # use a reliably-live 24/7 channel as a stand-in to exercise live detection
    r = s.post(f"{BASE}/api/channels", data={"url": "https://www.youtube.com/@LofiGirl"})
    assert r.status_code == 200, r.text
    ch = r.json()
    print(f"  added: {ch['name']}  ({ch['id']})")
    live = s.get(f"{BASE}/api/channels/{ch['id']}/live").json()
    safe_title = str(live.get("title")).encode("ascii", "replace").decode()
    print(f"  live check: live={live['live']} title={safe_title}")
    assert live["live"] is True, "expected LofiGirl to be live"
    # clean up the test channel
    s.delete(f"{BASE}/api/channels/{ch['id']}")
    print("  PASS - channel add + live detection via API\n")
    print("CHANNEL / CROP TESTS PASSED")


if __name__ == "__main__":
    main()
