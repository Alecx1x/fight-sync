"""Validate the in-browser recording path: a webm 'recording' uploaded via the
endpoint is remuxed, gets a valid duration, and renders as a gameplay source.
Requires the server running on 127.0.0.1:8765.
"""
import subprocess
import wave
from pathlib import Path

import numpy as np
import requests

from media import FFMPEG, probe
import test_synthetic as ts
from pipeline import RenderConfig, render

T = ts.T
T.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8765"


def build_clips():
    sig = ts.make_signature()
    fac = sig + np.random.default_rng(1).normal(0, 0.03, len(sig))
    ts.write_wav(T / "facecam.wav", fac)
    lead = np.random.default_rng(2).normal(0, 0.02, int(ts.KNOWN_OFFSET * ts.SR))
    game = np.concatenate([lead, sig * 0.8])
    ts.write_wav(T / "gameplay.wav", game)
    ts.build_clip(T / "gameplay.wav", T / "gameplay.mp4", "testsrc", len(game) / ts.SR)
    ts.build_clip(T / "facecam.wav", T / "facecam.mp4", "testsrc2", len(fac) / ts.SR)


def main():
    build_clips()

    # Encode gameplay to a webm container (what a browser MediaRecorder produces).
    webm = T / "gameplay.webm"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(T / "gameplay.mp4"),
         "-c:v", "libvpx", "-deadline", "realtime", "-b:v", "1M",
         "-c:a", "libopus", str(webm)],
        check=True,
    )
    print(f"== RECORDING UPLOAD TEST ==")
    print(f"  source webm: {probe(str(webm)).duration:.2f}s")

    s = requests.Session(); s.trust_env = False
    s.post(f"{BASE}/login", data={"password": open("fightsync-secret.txt").read().strip(), "next": "/"})
    with open(webm, "rb") as fh:
        r = s.post(f"{BASE}/api/upload_recording",
                   files={"file": ("gameplay-recording.webm", fh, "video/webm")},
                   data={"name": "gameplay"})
    assert r.status_code == 200, r.text
    staged = r.json()["path"]
    info = probe(staged)
    print(f"  staged as : {staged}")
    print(f"  remuxed   : {info.duration:.2f}s, {info.width}x{info.height} @ {info.fps}fps")
    assert info.duration > 0, "staged recording has no usable duration"
    print("  PASS - recording uploaded, remuxed, has valid duration\n")

    print("== RENDER FROM RECORDING ==")
    cfg = RenderConfig(out_w=1280, out_h=720, make_subtitles=False,
                       intro=False, outro=False, preset="ultrafast")
    out = render(staged, str(T / "facecam.mp4"),
                 str(T / "outrec"), str(T / "workrec"), cfg,
                 lambda p, m: print(f"  [{p:3d}%] {m}"))
    fin = probe(out["final"])
    print(f"  final {fin.width}x{fin.height}, {fin.duration:.1f}s, "
          f"sync offset {out['offset']:+.2f}s")
    assert Path(out["final"]).exists()
    assert abs(out["offset"] - ts.KNOWN_OFFSET) < 0.1, "sync wrong from webm source"
    print("  PASS - recorded webm synced + composited correctly\n")
    print("RECORDING PATH TESTS PASSED")


if __name__ == "__main__":
    main()
