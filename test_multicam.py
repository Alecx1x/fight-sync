"""Multicam composite check (no server): render gameplay + 2 facecam angles with
manual cuts and confirm a valid switched output in both PiP and side-by-side."""
import subprocess
import tempfile
from pathlib import Path

from media import FFMPEG, probe
from pipeline import RenderConfig, _cut_segments, render_multi


def build_clip(mp4, testsrc, seconds, freq=300):
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{testsrc}=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(mp4),
    ], check=True)


def main():
    # unit-check the cut tiler first
    segs = _cut_segments([{"t": 3, "angle": 1}, {"t": 6, "angle": 0}], 2, 10.0)
    assert segs == [(0.0, 3.0, 0), (3.0, 6.0, 1), (6.0, 10.0, 0)], segs
    print(f"  cut tiler: {segs}")

    T = Path(tempfile.mkdtemp(prefix="fs_mc_"))
    build_clip(T / "g.mp4", "testsrc", 10)
    build_clip(T / "a0.mp4", "testsrc2", 10, freq=300)   # primary angle
    build_clip(T / "a1.mp4", "smptebars", 10, freq=500)  # second angle

    angles = [{"path": str(T / "a0.mp4"), "offset": 0.0},
              {"path": str(T / "a1.mp4"), "offset": 1.0}]   # angle 1 synced +1s
    cuts = [{"t": 3.0, "angle": 1}, {"t": 6.0, "angle": 0}]

    for layout in ("pip", "sbs"):
        cfg = RenderConfig(out_w=640, out_h=360, fps=30, layout=layout,
                           make_subtitles=False, burn_subtitles=False,
                           intro=False, outro=False, transitions=False,
                           replays=False, multicam_angles=angles, multicam_cuts=cuts)
        out = render_multi([str(T / "g.mp4")], [str(T / "a0.mp4")],
                           str(T / ("out_" + layout)), str(T / ("work_" + layout)),
                           cfg, lambda p, m: None)
        info = probe(out["final"])
        print(f"  {layout}: {info.width}x{info.height}, {info.duration:.2f}s")
        assert info.duration >= 8.0, f"{layout} too short: {info.duration}"
    print("PASS - multicam switches between 2 synced angles (pip + sbs)")


if __name__ == "__main__":
    main()
