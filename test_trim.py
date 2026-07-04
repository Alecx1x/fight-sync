"""Composite-trim check (no server): render a synthetic pair full vs. trimmed
and confirm the trimmed body matches the kept [a,b] window."""
import subprocess
import tempfile
from pathlib import Path

from media import FFMPEG, probe
from pipeline import RenderConfig, render


def build_clip(mp4, testsrc, seconds):
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{testsrc}=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(mp4),
    ], check=True)


def render_dur(T, cfg, tag):
    out = render(str(T / "g.mp4"), str(T / "f.mp4"),
                 str(T / ("out_" + tag)), str(T / ("work_" + tag)), cfg,
                 lambda p, m: None)
    return probe(out["final"]).duration, out


def main():
    T = Path(tempfile.mkdtemp(prefix="fs_trim_"))
    build_clip(T / "g.mp4", "testsrc", 12)
    build_clip(T / "f.mp4", "testsrc2", 12)

    base = dict(out_w=640, out_h=360, fps=30, layout="sbs",
                make_subtitles=False, burn_subtitles=False,
                intro=False, outro=False, transitions=False,
                replays=False, manual_offset=0.0)

    full_dur, _ = render_dur(T, RenderConfig(**base), "full")
    trim_dur, _ = render_dur(T, RenderConfig(**base, trim_start=2.0, trim_end=7.0),
                             "trim")

    print(f"  full  render: {full_dur:.2f}s (expect ~12)")
    print(f"  trim  [2,7]:  {trim_dur:.2f}s (expect ~5)")
    assert 11.0 <= full_dur <= 12.5, f"full duration off: {full_dur}"
    assert 4.4 <= trim_dur <= 5.6, f"trim duration off: {trim_dur}"
    print("PASS - composite trim shortens the render to the kept window")


if __name__ == "__main__":
    main()
