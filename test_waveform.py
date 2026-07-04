"""Quick check of waveform_peaks on real clips (no server needed)."""
import glob
import os
import time

from media import waveform_peaks

pats = ["recordings/facecam-lib-*.mov", "recordings/gameplay-meta-*.mp4*"]
for pat in pats:
    hits = sorted(glob.glob(pat))
    if not hits:
        print(f"(no match for {pat})")
        continue
    f = hits[0]
    t = time.time()
    r = waveform_peaks(f)
    pk = r["peaks"]
    name = os.path.basename(f)[:34]
    mx = max(pk) if pk else 0
    loud = sum(1 for p in pk if p > 0.3)
    print(f"{name:36s} dur={r['duration']:8.1f}s pts={r['n']:5d} "
          f"max={mx:.2f} loud_buckets={loud:5d} took={time.time()-t:.2f}s")
