"""Cinematic slow-mo replays for FightSync.

Adapted from the Ringside VR OBS replay system. Around a punch-impact timestamp
we retime a short window of the composited video with ffmpeg `setpts`:

    ... 1.0x ...  [ease 0.5x]  [IMPACT 0.18x hold]  [ease 0.5x]  ... 1.0x ...

and layer in the synthesized whoosh + glove-impact SFX (the world goes quiet,
the whoosh swells, the punch connects). A "REPLAY" badge is burned in. The
finished replay clip is spliced into the main timeline just after the live punch.

Impacts can be supplied as timestamps or auto-detected from the mixed audio
(loudest transients = the biggest hits).
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from media import FFMPEG, probe, run_ffmpeg
from replay_sfx import ensure_assets, wav_duration

# Replay window geometry (seconds, on the live timeline, around the impact).
PRE = 1.4          # build-up kept before the impact
POST = 1.6         # footage kept after the impact (room for the slow-mo hold)
POST_LIVE = 0.6    # let the live punch land this long before cutting to replay

PROFILE = {
    "ease_in": 0.35,    # seconds of 0.5x just before impact
    "hold": 0.9,        # seconds of deep slow-mo from impact onward
    "ease_out": 0.5,    # seconds of 0.5x easing back up
    "slow_speed": 0.18,
    "ease_speed": 0.5,
}


# ── timestamp parsing ───────────────────────────────────────────────────────
def parse_timestamps(text: str) -> list[float]:
    """Parse 'mm:ss', 'h:mm:ss', or plain seconds, comma/space separated."""
    out: list[float] = []
    if not text:
        return out
    for tok in text.replace(";", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            if ":" in tok:
                parts = [float(p) for p in tok.split(":")]
                sec = 0.0
                for p in parts:
                    sec = sec * 60 + p
            else:
                sec = float(tok)
            out.append(sec)
        except ValueError:
            continue
    return sorted(out)


# ── auto impact detection ───────────────────────────────────────────────────
def detect_impacts(video_path: str, work: str, count: int,
                   min_gap: float = 6.0) -> list[float]:
    """Find the loudest transient onsets in the audio = the biggest hits.

    Returns up to `count` timestamps (seconds), spaced at least `min_gap` apart.
    """
    if count <= 0:
        return []
    sr = 8000
    wav = str(Path(work) / "impacts.wav")
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", video_path, "-vn", "-ac", "1", "-ar", str(sr),
         "-acodec", "pcm_s16le", wav],
        check=True,
    )
    with wave.open(wav, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    x = x.astype(np.float64)
    if len(x) < sr:
        return []

    # 20 ms energy envelope, then a positive "onset" = sharp rise in energy.
    hop = int(sr * 0.02)
    n = len(x) // hop
    env = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-9)
    env = np.log1p(env)
    onset = np.maximum(0.0, np.diff(env, prepend=env[0]))
    # smooth a touch so one punch is a single peak
    k = 3
    onset = np.convolve(onset, np.ones(k) / k, mode="same")

    gap_frames = int(min_gap / 0.02)
    order = np.argsort(onset)[::-1]
    chosen: list[int] = []
    for idx in order:
        if onset[idx] <= 0:
            break
        if all(abs(idx - c) >= gap_frames for c in chosen):
            chosen.append(int(idx))
        if len(chosen) >= count:
            break
    return sorted(round(i * 0.02, 2) for i in chosen)


# ── ramp schedule ───────────────────────────────────────────────────────────
def build_segments(duration: float, impact_at: float, profile: dict):
    """(start, end, speed) segments covering [0, duration], impact at `impact_at`."""
    p = profile
    impact = min(max(0.0, impact_at), duration)
    ease_in_start = max(0.0, impact - p["ease_in"])
    hold_end = min(duration, impact + p["hold"])
    ease_out_end = min(duration, hold_end + p["ease_out"])

    raw = [
        (0.0, ease_in_start, 1.0),
        (ease_in_start, impact, p["ease_speed"]),
        (impact, hold_end, p["slow_speed"]),
        (hold_end, ease_out_end, p["ease_speed"]),
        (ease_out_end, duration, 1.0),
    ]
    return [(s, e, spd) for (s, e, spd) in raw if e - s > 0.02]


def _impact_output_time(segments, slow_speed):
    """Where the impact lands on the OUTPUT (stretched) timeline, for SFX timing."""
    t = 0.0
    for s, e, spd in segments:
        if abs(spd - slow_speed) < 1e-6:
            return t
        t += (e - s) / spd
    return t * 0.5


def _video_chain(segments, offset, font, smooth, smooth_fps, ow, oh, fps):
    """Build the retimed video chain. `offset` shifts every trim onto main's
    absolute timeline (so we trim straight from main.mp4 — frame-accurate, no
    keyframe-seek error, and the impact lands exactly where it should)."""
    parts, labels = [], []
    for i, (s, e, spd) in enumerate(segments):
        lbl = f"rv{i}"
        chain = (f"[0:v]trim=start={offset + s:.3f}:end={offset + e:.3f},"
                 f"setpts=(PTS-STARTPTS)/{spd:.4f}")
        if smooth and spd < 1.0:
            chain += (f",minterpolate=fps={smooth_fps}:mi_mode=mci:"
                      f"mc_mode=aobmc:me_mode=bidir")
        chain += f"[{lbl}]"
        parts.append(chain)
        labels.append(f"[{lbl}]")
    concat = "".join(labels) + f"concat=n={len(segments)}:v=1:a=0[vcat]"
    # normalize + REPLAY badge so the clip concats cleanly with the rest
    badge = (
        f"[vcat]scale={ow}:{oh},setsar=1,fps={fps},format=yuv420p,"
        f"drawbox=x=36:y=34:w=190:h=52:color=0x000000@0.45:t=fill,"
        f"drawbox=x=36:y=34:w=8:h=52:color=0xE23B3B:t=fill,"
        f"drawtext=fontfile={font}:text='REPLAY':x=60:y=48:"
        f"fontcolor=white:fontsize=30[outv]"
    )
    return ";".join(parts + [concat, badge])


def build_replay_clip(main_path: str, impact_t: float, out_path: str,
                      work: str, font: str, ow: int, oh: int, fps: int,
                      enc: list[str], duration: float,
                      smooth: bool = False,
                      whoosh_gain: float = 0.40,
                      impact_gain: float = 0.70) -> float:
    """Render one slow-mo replay clip around `impact_t`. Returns its duration."""
    win_start = max(0.0, impact_t - PRE)
    win_end = min(duration, impact_t + POST)
    win_dur = win_end - win_start
    impact_at = impact_t - win_start

    segments = build_segments(win_dur, impact_at, PROFILE)
    fg = _video_chain(segments, win_start, font, smooth, 120, ow, oh, fps)

    whoosh, impact_wav = ensure_assets()
    whoosh_dur = wav_duration(whoosh)
    impact_out = _impact_output_time(segments, PROFILE["slow_speed"])
    w_ms = int(round(max(0.0, impact_out - whoosh_dur) * 1000))
    i_ms = int(round(impact_out * 1000))

    audio_fg = (
        f"[1:a]volume={whoosh_gain:.3f},adelay={w_ms}:all=1[wa];"
        f"[2:a]volume={impact_gain:.3f},adelay={i_ms}:all=1[ia];"
        f"[wa][ia]amix=inputs=2:normalize=0:duration=longest,"
        f"aresample=48000,aformat=channel_layouts=stereo[outa]"
    )

    # trim the window straight from main in the filtergraph (frame-accurate)
    run_ffmpeg([
        "-i", main_path, "-i", str(whoosh), "-i", str(impact_wav),
        "-filter_complex", fg + ";" + audio_fg,
        "-map", "[outv]", "-map", "[outa]",
        "-r", str(fps), *enc, out_path,
    ], cwd=work)
    return probe(out_path).duration


def assemble_body(main_path: str, out_dur: float, impacts: list[float],
                  replay_paths: list[str], out_path: str, work: str,
                  ow: int, oh: int, fps: int, enc: list[str]) -> None:
    """One pass: split main at each impact's cut point and weave the pre-rendered
    replay clips in between (live punch -> slow-mo replay -> resume live)."""
    cuts = []
    prev = -1.0
    for t in impacts:
        c = min(out_dur - 0.05, t + POST_LIVE)
        if c > prev + 0.2:
            cuts.append(c)
            prev = c

    inputs = ["-i", main_path]
    for rp in replay_paths:
        inputs += ["-i", rp]

    parts, order = [], []
    bounds = [0.0] + cuts + [out_dur]
    for j in range(len(bounds) - 1):
        a, b = bounds[j], bounds[j + 1]
        parts.append(
            f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,"
            f"scale={ow}:{oh},setsar=1,fps={fps},format=yuv420p[pv{j}]")
        parts.append(
            f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo[pa{j}]")
        order.append((f"pv{j}", f"pa{j}"))
        if j < len(replay_paths):              # weave the replay after this piece
            idx = j + 1
            parts.append(
                f"[{idx}:v]setpts=PTS-STARTPTS,scale={ow}:{oh},setsar=1,"
                f"fps={fps},format=yuv420p[xv{j}]")
            parts.append(
                f"[{idx}:a]asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=channel_layouts=stereo[xa{j}]")
            order.append((f"xv{j}", f"xa{j}"))

    cc = "".join(f"[{v}][{a}]" for v, a in order)
    cc += f"concat=n={len(order)}:v=1:a=1[bv][ba]"
    run_ffmpeg([
        *inputs, "-filter_complex", ";".join(parts) + ";" + cc,
        "-map", "[bv]", "-map", "[ba]", "-r", str(fps), *enc, out_path,
    ], cwd=work)
