"""Music-bed render check (no server): render a synthetic pair with a music
track mixed under it (ducking on) and confirm a valid output with audio."""
import json
import subprocess
import tempfile
from pathlib import Path

from media import FFMPEG, FFPROBE, probe
from pipeline import RenderConfig, render


def build_clip(mp4, testsrc, seconds):
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"{testsrc}=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(mp4),
    ], check=True)


def build_music(mp3, seconds):
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "libmp3lame", str(mp3),
    ], check=True)


def has_audio(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_streams",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return len(json.loads(out).get("streams", [])) > 0


def main():
    T = Path(tempfile.mkdtemp(prefix="fs_music_"))
    build_clip(T / "g.mp4", "testsrc", 8)
    build_clip(T / "f.mp4", "testsrc2", 8)
    build_music(T / "bed.mp3", 4)            # shorter than video -> must loop to cover
    cfg = RenderConfig(out_w=640, out_h=360, fps=30, layout="sbs",
                       make_subtitles=False, burn_subtitles=False,
                       intro=False, outro=False, transitions=False,
                       replays=False, manual_offset=0.0,
                       music_path=str(T / "bed.mp3"), music_volume=0.2,
                       music_duck=True)
    out = render(str(T / "g.mp4"), str(T / "f.mp4"), str(T / "out"),
                 str(T / "work"), cfg, lambda p, m: None)
    info = probe(out["final"])
    print(f"  rendered: {info.duration:.2f}s, audio={has_audio(out['final'])}")
    assert info.duration >= 7.0, f"output too short: {info.duration}"
    assert has_audio(out["final"]), "final has no audio after music mix"
    print("PASS - music bed mixed under the render (looped + ducked)")


if __name__ == "__main__":
    main()
