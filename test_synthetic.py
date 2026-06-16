"""Self-test: build two synthetic clips that share an audio signature with a
known offset, then verify sync detection and the full render pipeline.

Run:  .venv\Scripts\python test_synthetic.py
"""
import subprocess
import wave
from pathlib import Path

import numpy as np

from media import FFMPEG, probe
from sync import compute_sync
from pipeline import RenderConfig, render

ROOT = Path(__file__).parent
T = ROOT / "_test"
T.mkdir(exist_ok=True)
SR = 16000
KNOWN_OFFSET = 5.0          # gameplay starts 5s before facecam's t=0


def write_wav(path, x, sr=SR):
    x = np.clip(x, -1, 1)
    pcm = (x * 32000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def make_signature(seconds=18):
    """Random decaying bursts = a unique shared 'voice/punch' pattern."""
    rng = np.random.default_rng(7)
    n = int(seconds * SR)
    x = np.zeros(n)
    for _ in range(22):
        start = rng.integers(0, n - SR)
        dur = int(rng.uniform(0.08, 0.4) * SR)
        env = np.exp(-np.linspace(0, 6, dur))
        x[start:start + dur] += rng.normal(0, 1, dur) * env
    return x / (np.max(np.abs(x)) + 1e-9)


def build_clip(wav, mp4, testsrc, seconds):
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{testsrc}=size=640x360:rate=30:duration={seconds}",
        "-i", str(wav),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(mp4),
    ], check=True)


def main():
    sig = make_signature()

    # facecam: signature from t=0, plus its own light noise (different mic)
    facecam_audio = sig + np.random.default_rng(1).normal(0, 0.03, len(sig))
    write_wav(T / "facecam.wav", facecam_audio)

    # gameplay: 5s of room noise, THEN the same signature (so gameplay leads)
    lead = np.random.default_rng(2).normal(0, 0.02, int(KNOWN_OFFSET * SR))
    gameplay_audio = np.concatenate([lead, sig * 0.8])
    write_wav(T / "gameplay.wav", gameplay_audio)

    g_secs = len(gameplay_audio) / SR
    f_secs = len(facecam_audio) / SR
    build_clip(T / "gameplay.wav", T / "gameplay.mp4", "testsrc", g_secs)
    build_clip(T / "facecam.wav", T / "facecam.mp4", "testsrc2", f_secs)

    print("== SYNC TEST ==")
    res = compute_sync(str(T / "gameplay.mp4"), str(T / "facecam.mp4"), str(T))
    print(f"  detected offset : {res.offset_seconds:+.3f}s "
          f"(expected {KNOWN_OFFSET:+.3f})")
    print(f"  confidence      : {res.confidence:.3f}")
    print(f"  trim gameplay   : {res.a_start:.3f}s   trim facecam: {res.b_start:.3f}s")
    err = abs(res.offset_seconds - KNOWN_OFFSET)
    assert err < 0.05, f"SYNC OFF by {err:.3f}s"
    print("  PASS — sync within 50 ms\n")

    print("== PIPELINE TEST (no subtitles) ==")
    cfg = RenderConfig(out_w=1280, out_h=720, fps=30,
                       make_subtitles=False, burn_subtitles=False,
                       intro=True, outro=True, preset="ultrafast")
    out = render(str(T / "gameplay.mp4"), str(T / "facecam.mp4"),
                 str(T / "out"), str(T / "work"), cfg,
                 lambda p, m: print(f"  [{p:3d}%] {m}"))
    info = probe(out["final"])
    print(f"  final: {out['final']}")
    print(f"  {info.width}x{info.height} @ {info.fps}fps, {info.duration:.1f}s")
    assert info.width == 1280 and info.height == 720
    # intro + synced body + outro should exceed the synced body alone
    assert info.duration > out["duration"], "intro/outro not added"
    print("  PASS - composite + intro/outro rendered\n")

    print("== REPLAY TEST (slow-mo + impact SFX) ==")
    cfg2 = RenderConfig(out_w=1280, out_h=720, fps=30,
                        make_subtitles=False, burn_subtitles=False,
                        intro=False, outro=False, preset="ultrafast",
                        replays=True, replay_times="8", auto_replays=1)
    out2 = render(str(T / "gameplay.mp4"), str(T / "facecam.mp4"),
                  str(T / "out2"), str(T / "work2"), cfg2,
                  lambda p, m: print(f"  [{p:3d}%] {m}"))
    info2 = probe(out2["final"])
    print(f"  replays placed: {out2['replays']}")
    print(f"  synced body {out2['duration']:.1f}s -> final {info2.duration:.1f}s "
          f"(+{info2.duration - out2['duration']:.1f}s of slow-mo)")
    assert out2["replays"], "no replays were produced"
    assert info2.duration > out2["duration"] + 3, "replays did not lengthen video"
    # confirm the impact SFX assets were synthesized
    assert (ROOT / "assets" / "impact.wav").exists()
    assert (ROOT / "assets" / "whoosh.wav").exists()
    print("  PASS - replays spliced in with SFX\n")

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
