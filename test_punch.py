"""Color punch-up render check (no server): render a synthetic pair with
color_punch on and confirm it completes with a valid, same-length output."""
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


def main():
    T = Path(tempfile.mkdtemp(prefix="fs_punch_"))
    build_clip(T / "g.mp4", "testsrc", 8)
    build_clip(T / "f.mp4", "testsrc2", 8)
    cfg = RenderConfig(out_w=640, out_h=360, fps=30, layout="sbs",
                       make_subtitles=False, burn_subtitles=False,
                       intro=False, outro=False, transitions=False,
                       replays=False, manual_offset=0.0,
                       color_punch=True, punch_strength=1.4)
    out = render(str(T / "g.mp4"), str(T / "f.mp4"), str(T / "out"),
                 str(T / "work"), cfg, lambda p, m: None)
    info = probe(out["final"])
    print(f"  rendered: {info.width}x{info.height}, {info.duration:.2f}s")
    assert info.duration >= 7.0, f"output too short: {info.duration}"
    assert info.width == 640 and info.height == 360
    print("PASS - color punch-up renders a valid output")


if __name__ == "__main__":
    main()
