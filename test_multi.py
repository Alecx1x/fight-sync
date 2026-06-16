"""Multi-clip test: two (gameplay, facecam) pairs, each synced, joined by a bell
round-card transition, in selection order.
"""
import subprocess
import wave
from pathlib import Path

import numpy as np

from media import FFMPEG, probe
from pipeline import RenderConfig, render_multi
import test_synthetic as ts

T = ts.T
T.mkdir(exist_ok=True)


def make_pair(idx, offset, seconds=12, seed=0):
    """Build a (gameplay, facecam) pair sharing a unique audio signature, with a
    known lead offset on the gameplay side."""
    rng = np.random.default_rng(100 + idx)
    n = int(seconds * ts.SR)
    sig = np.zeros(n)
    for _ in range(16):
        st = rng.integers(0, n - ts.SR)
        d = int(rng.uniform(0.08, 0.35) * ts.SR)
        env = np.exp(-np.linspace(0, 6, d))
        sig[st:st + d] += rng.normal(0, 1, d) * env
    sig /= np.max(np.abs(sig)) + 1e-9

    fac = sig + np.random.default_rng(200 + idx).normal(0, 0.03, n)
    ts.write_wav(T / f"f{idx}.wav", fac)
    lead = np.random.default_rng(300 + idx).normal(0, 0.02, int(offset * ts.SR))
    game = np.concatenate([lead, sig * 0.8])
    ts.write_wav(T / f"g{idx}.wav", game)

    gsec, fsec = len(game) / ts.SR, n / ts.SR
    ts.build_clip(T / f"g{idx}.wav", T / f"g{idx}.mp4", "testsrc", gsec)
    ts.build_clip(T / f"f{idx}.wav", T / f"f{idx}.mp4", "testsrc2", fsec)
    return str(T / f"g{idx}.mp4"), str(T / f"f{idx}.mp4")


def main():
    print("== MULTI-CLIP TEST (2 pairs + bell transition) ==")
    g0, f0 = make_pair(0, 5.0)
    g1, f1 = make_pair(1, 3.0)

    cfg = RenderConfig(out_w=1280, out_h=720, fps=30, preset="ultrafast",
                       make_subtitles=False, burn_subtitles=False,
                       intro=False, outro=False,
                       transitions=True, transition_label="ROUND",
                       transition_seconds=2.0, bell=True)
    out = render_multi([g0, g1], [f0, f1], str(T / "outm"), str(T / "workm"),
                       cfg, lambda p, m: print(f"  [{p:3d}%] "
                                               + m.encode("ascii", "replace").decode()))
    info = probe(out["final"])
    body_total = sum(s["duration"] for s in out["segments"])
    print(f"  clips: {out['clips']}, per-clip offsets: "
          f"{[s['offset'] for s in out['segments']]}")
    print(f"  synced footage total {body_total:.1f}s + ~2.0s bell -> "
          f"final {info.duration:.1f}s")

    assert out["clips"] == 2
    # each pair synced to its known offset
    assert abs(out["segments"][0]["offset"] - 5.0) < 0.1, "pair 0 sync off"
    assert abs(out["segments"][1]["offset"] - 3.0) < 0.1, "pair 1 sync off"
    # final = seg0 + transition + seg1, so longer than the footage alone
    assert info.duration > body_total + 1.0, "transition not inserted"
    assert (Path(__file__).parent / "assets" / "bell.wav").exists()
    print("  PASS - two pairs synced independently, joined with bell transition\n")
    print("MULTI-CLIP TEST PASSED")


if __name__ == "__main__":
    main()
