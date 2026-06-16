"""Automatic A/V sync by matching shared *onsets* (sharp sound starts).

Both recordings capture the same acoustic events (claps, punch impacts, voice
onsets). Instead of correlating the overall loudness curve — which gets diluted
when the two tracks also contain very different audio (game audio vs room audio)
— we reduce each track to an *onset-strength* signal: a sparse series of spikes
at moments where the sound suddenly gets louder. Cross-correlating those locks
onto the shared key sounds; one or two clear shared hits are enough to find the
single constant offset that keeps the whole clip aligned. Sustained, dissimilar
audio produces no matching onsets, so it doesn't pollute the match.

Confidence is a peak-to-sidelobe ratio: how far the winning alignment stands out
above all the other candidate lags. A lone clap that lines up gives a tall, lonely
peak (high confidence) even amid otherwise-unrelated audio.
"""
from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from media import FFMPEG

SR = 8000          # audio extraction sample rate (Hz)
HOP = 0.01         # 10 ms hop -> 100 Hz onset signal
ENV_RATE = int(1 / HOP)


@dataclass
class SyncResult:
    offset_seconds: float      # time in `a` (gameplay) matching t=0 in `b` (facecam)
    confidence: float          # 0..1, from the peak-to-sidelobe ratio
    a_start: float             # seconds to trim from start of a
    b_start: float             # seconds to trim from start of b
    peak_psr: float = 0.0      # raw peak-to-sidelobe ratio (diagnostic)


def _extract_mono(path: str, dst: str) -> np.ndarray:
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
         "-acodec", "pcm_s16le", dst],
        check=True,
    )
    with wave.open(dst, "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float64)


def _onset_env(x: np.ndarray) -> np.ndarray:
    """Onset-strength signal at 100 Hz: positive jumps in short-time log energy."""
    hop_n = int(SR * HOP)
    n = len(x) // hop_n
    if n < 2:
        return np.zeros(2)
    frames = x[: n * hop_n].reshape(n, hop_n)
    energy = np.sqrt((frames ** 2).mean(axis=1) + 1e-9)
    log_e = np.log1p(energy)
    onset = np.maximum(0.0, np.diff(log_e, prepend=log_e[0]))   # rectified flux
    # light smoothing so one hit is a single spike, not a sliver
    onset = np.convolve(onset, np.array([0.5, 1.0, 0.5]), mode="same")
    # de-mean a touch so constant background doesn't bias the correlation
    return onset - 0.25 * onset.mean()


def _xcorr_offset(oa: np.ndarray, ob: np.ndarray) -> tuple[int, float]:
    """Return (lag, psr). lag>0 means `a` started before `b`."""
    n = len(oa) + len(ob)
    nfft = 1 << (n - 1).bit_length()
    fa = np.fft.rfft(oa, nfft)
    fb = np.fft.rfft(ob, nfft)
    corr = np.fft.irfft(fa * np.conj(fb), nfft)
    corr = np.concatenate((corr[-(len(ob) - 1):], corr[: len(oa)]))
    lags = np.arange(-(len(ob) - 1), len(oa))

    peak_i = int(np.argmax(corr))
    lag = int(lags[peak_i])

    # peak-to-sidelobe ratio: z-score of the peak vs. all other lags
    w = 5
    mask = np.ones(len(corr), dtype=bool)
    mask[max(0, peak_i - w): peak_i + w + 1] = False
    bg = corr[mask]
    psr = float((corr[peak_i] - bg.mean()) / (bg.std() + 1e-9))
    return lag, psr


def compute_sync(a_path: str, b_path: str, work: str) -> SyncResult:
    """a = gameplay, b = facecam. Returns trim points to align them."""
    work_p = Path(work)
    xa = _extract_mono(a_path, str(work_p / "a.wav"))
    xb = _extract_mono(b_path, str(work_p / "b.wav"))
    oa = _onset_env(xa)
    ob = _onset_env(xb)

    lag, psr = _xcorr_offset(oa, ob)
    offset = lag / ENV_RATE

    # map peak-to-sidelobe ratio -> 0..1 confidence. A clean lock (one clear
    # shared transient) sits around psr 8-12; pure noise is ~3-5.
    conf = max(0.0, min(1.0, (psr - 5.0) / 18.0))

    return SyncResult(
        offset_seconds=offset,
        confidence=conf,
        a_start=max(offset, 0.0),
        b_start=max(-offset, 0.0),
        peak_psr=psr,
    )
