"""Hit-graphics check (no server): build a clip with loud transient bursts,
confirm impacts are detected, and that _overlay_hits produces a valid output."""
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from media import FFMPEG, probe
from pipeline import RenderConfig, _overlay_hits
from replay import detect_impacts

SR = 16000


def burst_wav(path, seconds=12):
    n = int(seconds * SR)
    x = np.random.default_rng(3).normal(0, 0.02, n)     # quiet floor
    for t in (2.0, 5.0, 8.0, 10.5):                      # loud punches
        i = int(t * SR)
        d = int(0.15 * SR)
        x[i:i + d] += np.exp(-np.linspace(0, 6, d)) * 0.9
    x = np.clip(x, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32000).astype(np.int16).tobytes())


def main():
    T = Path(tempfile.mkdtemp(prefix="fs_hits_"))
    burst_wav(T / "a.wav", 12)
    clip = T / "clip.mp4"
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=12",
        "-i", str(T / "a.wav"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(clip),
    ], check=True)
    work = T / "work"; work.mkdir()

    impacts = detect_impacts(str(clip), str(work), 6, min_gap=2.5)
    print(f"  detected impacts: {[round(t,1) for t in impacts]}")
    assert len(impacts) >= 2, "no impacts detected — overlay would be a no-op"

    cfg = RenderConfig(out_w=640, out_h=360, hit_count=6, hit_text="BIG HIT!")
    dst = str(T / "fx.mp4")
    out = _overlay_hits(str(clip), dst, str(work), cfg, probe(str(clip)).duration)
    assert out == dst and Path(dst).exists(), "overlay output missing"
    print(f"  overlay rendered: {probe(dst).duration:.2f}s")
    print("PASS - hit graphics overlay renders on detected impacts")


if __name__ == "__main__":
    main()
