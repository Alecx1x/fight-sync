"""Calibrate / verify onset-based sync: rich shared audio, a single shared clap
amid unrelated audio, and pure non-shared noise."""
import numpy as np

from sync import compute_sync
import test_synthetic as ts

T = ts.T
T.mkdir(exist_ok=True)
SR = ts.SR     # write_wav writes at this rate — build audio at the same rate


def build(name, audio):
    ts.write_wav(T / (name + ".wav"), audio / (np.max(np.abs(audio)) + 1e-9))
    ts.build_clip(T / (name + ".wav"), T / (name + ".mp4"), "testsrc", len(audio) / SR)
    return str(T / (name + ".mp4"))


def clap():
    n = int(SR * 0.12); t = np.arange(n) / SR
    return np.random.default_rng(7).normal(0, 1, n) * np.exp(-t * 40)


def case_shared():
    sig = ts.make_signature()
    fac = sig + np.random.default_rng(1).normal(0, 0.03, len(sig))
    game = np.concatenate([np.random.default_rng(2).normal(0, 0.02, int(5 * SR)), sig * 0.8])
    return build("csg", game), build("csf", fac), 5.0


def case_oneclap():
    n = int(8 * SR); c = clap()
    g = np.random.default_rng(5).normal(0, 0.05, n)     # game-ish background
    f = np.random.default_rng(6).normal(0, 0.05, n)     # independent room background
    g[int(4.0 * SR):int(4.0 * SR) + len(c)] += c * 3    # clap at 4.0s in gameplay
    f[int(1.0 * SR):int(1.0 * SR) + len(c)] += c * 3    # same clap at 1.0s in facecam
    return build("cog", g), build("cof", f), 3.0        # -> offset 3.0


def case_noise():
    n = int(8 * SR)
    return (build("cng", np.random.default_rng(11).normal(0, 0.3, n)),
            build("cnf", np.random.default_rng(99).normal(0, 0.3, n)), None)


def main():
    print("== ONSET SYNC CALIBRATION ==")
    rows = [("rich shared", case_shared()), ("one shared clap", case_oneclap()),
            ("non-shared noise", case_noise())]
    res = {}
    for label, (g, f, exp) in rows:
        r = compute_sync(g, f, str(T))
        res[label] = r
        ex = f"{exp:+.1f}" if exp is not None else "—"
        print(f"  {label:18s} offset={r.offset_seconds:+.2f} (exp {ex})  "
              f"psr={r.peak_psr:5.1f}  conf={r.confidence:.2f}")

    assert abs(res["rich shared"].offset_seconds - 5.0) < 0.1
    assert abs(res["one shared clap"].offset_seconds - 3.0) < 0.1, "one clap didn't lock"
    assert res["rich shared"].confidence >= 0.6
    assert res["one shared clap"].confidence >= 0.3, "one clap should read as a real match"
    assert res["non-shared noise"].confidence < 0.2, "noise should read as weak"
    print("  PASS - locks on a single shared clap; flags noise as weak\n")
    print("ONSET SYNC TEST PASSED")


if __name__ == "__main__":
    main()
