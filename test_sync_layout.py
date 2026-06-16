"""Test the split sync endpoint, side-by-side layout, and graceful no-shared-audio
fallback. Requires the server running for the /api/sync part.
"""
import json
from pathlib import Path

import numpy as np
import requests

from media import probe
from pipeline import RenderConfig, render_multi
import test_synthetic as ts

T = ts.T
T.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8765"


def synced_pair():
    sig = ts.make_signature()
    fac = sig + np.random.default_rng(1).normal(0, 0.03, len(sig))
    ts.write_wav(T / "fS.wav", fac)
    lead = np.random.default_rng(2).normal(0, 0.02, int(5.0 * ts.SR))
    game = np.concatenate([lead, sig * 0.8])
    ts.write_wav(T / "gS.wav", game)
    ts.build_clip(T / "gS.wav", T / "gS.mp4", "testsrc", len(game) / ts.SR)
    ts.build_clip(T / "fS.wav", T / "fS.mp4", "testsrc2", len(fac) / ts.SR)
    return str(T / "gS.mp4"), str(T / "fS.mp4")


def unshared_pair():
    n = int(8 * ts.SR)
    ts.write_wav(T / "gU.wav", np.random.default_rng(11).normal(0, 0.3, n))
    ts.write_wav(T / "fU.wav", np.random.default_rng(99).normal(0, 0.3, n))
    ts.build_clip(T / "gU.wav", T / "gU.mp4", "testsrc", 8)
    ts.build_clip(T / "fU.wav", T / "fU.mp4", "testsrc2", 8)
    return str(T / "gU.mp4"), str(T / "fU.mp4")


def main():
    gS, fS = synced_pair()
    gU, fU = unshared_pair()

    print("== /api/sync (fast preview) ==")
    s = requests.Session(); s.trust_env = False
    s.post(f"{BASE}/login", data={"password": open("fightsync-secret.txt").read().strip(), "next": "/"})
    d = s.post(f"{BASE}/api/sync", data={
        "gameplay_paths_json": json.dumps([gS, gU]),
        "facecam_paths_json": json.dumps([fS, fU])}).json()
    print(f"  shared pair : {d['pairs'][0]}")
    print(f"  unshared    : {d['pairs'][1]}")
    assert d["pairs"][0]["ok"] is True and abs(d["pairs"][0]["offset"] - 5.0) < 0.1
    assert d["pairs"][1]["ok"] is False        # no real shared audio
    print("  PASS - sync preview flags good vs weak alignment\n")

    print("== SIDE-BY-SIDE LAYOUT ==")
    cfg = RenderConfig(out_w=1280, out_h=720, layout="sbs", preset="ultrafast",
                       make_subtitles=False, intro=False, outro=False)
    out = render_multi([gS], [fS], str(T / "sbs"), str(T / "sbsw"), cfg,
                       lambda p, m: None)
    info = probe(out["final"])
    print(f"  side-by-side output: {info.width}x{info.height}")
    assert info.width == 1280 and info.height == 720
    print("  PASS - side-by-side rendered\n")

    print("== NO-SHARED-AUDIO FALLBACK (must not hard-fail) ==")
    cfg2 = RenderConfig(out_w=854, out_h=480, preset="ultrafast",
                        make_subtitles=False, intro=False, outro=False)
    out2 = render_multi([gU], [fU], str(T / "fb"), str(T / "fbw"), cfg2,
                        lambda p, m: None)
    assert Path(out2["final"]).exists() and probe(out2["final"]).duration > 1
    print(f"  rendered anyway: {probe(out2['final']).duration:.1f}s "
          f"(confidence {out2['confidence']:.2f})")
    print("  PASS - non-shared clips still produce a video (aligned at start)\n")
    print("SYNC + LAYOUT TESTS PASSED")


if __name__ == "__main__":
    main()
