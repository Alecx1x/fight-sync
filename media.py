"""ffmpeg / ffprobe discovery and media probing helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _winget_bin(name: str) -> Optional[str]:
    """Look for a winget-installed Gyan.FFmpeg binary."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if not base.exists():
        return None
    for exe in base.glob(f"Gyan.FFmpeg*/**/{name}.exe"):
        return str(exe)
    return None


def _discover(name: str) -> str:
    # 1) On PATH
    found = shutil.which(name)
    if found:
        return found
    # 2) Explicit env override
    env = os.environ.get(f"{name.upper()}_BINARY")
    if env and Path(env).exists():
        return env
    # 3) winget install location
    wg = _winget_bin(name)
    if wg:
        return wg
    # 4) imageio-ffmpeg fallback (ffmpeg only)
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    raise RuntimeError(
        f"Could not locate '{name}'. Install ffmpeg (winget install Gyan.FFmpeg) "
        f"or set {name.upper()}_BINARY to its full path."
    )


FFMPEG = _discover("ffmpeg")
try:
    FFPROBE = _discover("ffprobe")
except RuntimeError:
    # ffprobe usually sits next to ffmpeg
    cand = Path(FFMPEG).with_name("ffprobe.exe")
    FFPROBE = str(cand) if cand.exists() else FFMPEG


@dataclass
class MediaInfo:
    path: str
    duration: float          # seconds
    width: int
    height: int
    fps: float
    has_audio: bool

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 16 / 9


def probe(path: str) -> MediaInfo:
    """Return basic stream info for a media file."""
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True, timeout=60,
    ).stdout
    data = json.loads(out)

    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise ValueError(f"No video stream found in {path}")

    # fps can be "30000/1001"
    fps = 30.0
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1"
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else float(num)
    except Exception:
        pass

    duration = float(data["format"].get("duration") or v.get("duration") or 0.0)
    if duration <= 0:
        duration = _measure_duration(path, fps)

    return MediaInfo(
        path=path,
        duration=duration,
        width=int(v["width"]),
        height=int(v["height"]),
        fps=round(fps, 3),
        has_audio=a is not None,
    )


def _measure_duration(path: str, fps: float) -> float:
    """Fallback when the container has no duration (e.g. MediaRecorder webm):
    count video packets and divide by the frame rate."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-of", "json", path],
            capture_output=True, text=True, check=True, timeout=180,
        ).stdout
        pkts = int(json.loads(out)["streams"][0]["nb_read_packets"])
        if fps > 0 and pkts > 0:
            return pkts / fps
    except Exception:
        pass
    return 0.0


def waveform_peaks(path: str, max_points: int = 6000) -> dict:
    """Decode the audio to low-rate mono PCM and return per-bucket amplitude
    peaks (0..1, peak-normalised so a quiet facecam mic is still visible) plus
    the duration. Used by the visual sync aligner: a shared clap/punch shows as
    a spike on BOTH clips even when their mics are too different to cross-correlate.

    Returns {"duration": float, "peaks": [float...], "n": int}. `peaks` is empty
    if the file has no audio.
    """
    import numpy as np

    info = probe(path)
    dur = max(info.duration, 0.0)
    if not info.has_audio or dur <= 0:
        return {"duration": round(dur, 3), "peaks": [], "n": 0}

    # Keep the decoded buffer bounded (~<=4M samples) regardless of clip length.
    sr = 1600
    if sr * dur > 4_000_000:
        sr = max(400, int(4_000_000 / dur))

    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", path,
         "-map", "a:0", "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
    ).stdout
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size == 0:
        return {"duration": round(dur, 3), "peaks": [], "n": 0}

    # ~40 buckets/sec for clean transient localisation, capped at max_points.
    n = int(min(max_points, max(200, dur * 40)))
    n = min(n, samples.size)
    # Pad so the samples split evenly into n buckets, then take per-bucket max-abs.
    pad = (-samples.size) % n
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, dtype="<i2")])
    peaks = np.abs(samples.reshape(n, -1).astype(np.float32)).max(axis=1)
    top = float(peaks.max()) or 1.0
    peaks = (peaks / top)
    return {"duration": round(dur, 3),
            "peaks": [round(float(p), 3) for p in peaks], "n": n}


def run_ffmpeg(args: list[str], cwd: Optional[str] = None,
               on_log=None) -> None:
    """Run ffmpeg, streaming stderr. Raises on non-zero exit."""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-stats", *args]
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    tail = []
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line.rstrip())
        if len(tail) > 40:
            tail.pop(0)
        if on_log:
            on_log(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))
