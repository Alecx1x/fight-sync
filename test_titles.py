"""Title-card + lower-third render check (no server). The lower-third lives in
the composite filter_complex, so this mainly proves that quoting is valid."""
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
    T = Path(tempfile.mkdtemp(prefix="fs_titles_"))
    build_clip(T / "g.mp4", "testsrc", 8)
    build_clip(T / "f.mp4", "testsrc2", 8)
    cfg = RenderConfig(out_w=640, out_h=360, fps=30, layout="sbs",
                       make_subtitles=False, burn_subtitles=False,
                       intro=True, outro=True, transitions=False,
                       replays=False, manual_offset=0.0,
                       title="FIGHT NIGHT", intro_subtitle="Round 1",
                       lower_third="@AnpiBoxing")
    out = render(str(T / "g.mp4"), str(T / "f.mp4"), str(T / "out"),
                 str(T / "work"), cfg, lambda p, m: None)
    info = probe(out["final"])
    print(f"  rendered with intro+outro+lower-third: {info.duration:.2f}s")
    # 8s body + ~2.6 intro + ~4.0 outro
    assert info.duration >= 13.0, f"intro/outro missing? {info.duration}"
    print("PASS - upgraded title cards + lower-third render cleanly")


if __name__ == "__main__":
    main()
