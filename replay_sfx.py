"""Replay SFX synthesizer (ported from the Ringside VR replay system).

Procedurally generates the two sounds the slow-mo replay layers in — no licensed
audio assets needed:

  whoosh.wav  — a slow, airy "time-bending" whoosh that swells as the footage
                slows (brown noise through a downward-gliding lowpass + a soft
                downward pitch sweep, cresting right into the punch).
  impact.wav  — a glove-to-face hit: low thud body + sub for weight + a fast
                noise "slap" transient (skin contact).

Run directly to (re)generate:  python replay_sfx.py
Needs numpy.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
ASSETS = Path(__file__).with_name("assets")


def _write_wav(path, samples):
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _one_pole_lowpass(x, cutoff_hz):
    """One-pole lowpass with a per-sample (time-varying) cutoff array."""
    alpha = 1.0 - np.exp(-2.0 * np.pi * cutoff_hz / SR)
    y = np.empty_like(x)
    prev = 0.0
    for i in range(len(x)):
        prev += alpha[i] * (x[i] - prev)
        y[i] = prev
    return y


def make_whoosh(path, dur=1.30, seed=7):
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)

    white = rng.normal(0.0, 1.0, n)
    brown = np.cumsum(white)
    brown /= np.max(np.abs(brown)) + 1e-9

    cutoff = np.linspace(4000.0, 550.0, n)         # darkens over time
    body = _one_pole_lowpass(brown, cutoff)

    inst_freq = np.linspace(520.0, 150.0, n)        # downward sweep
    sweep = np.sin(2.0 * np.pi * np.cumsum(inst_freq) / SR)

    mix = 0.9 * body + 0.25 * sweep

    env = np.power(t / dur, 1.6)                     # crest near the end
    tail = int(n * 0.05)
    env[-tail:] *= np.linspace(1.0, 0.25, tail)
    fade_in = int(n * 0.08)
    env[:fade_in] *= np.linspace(0.0, 1.0, fade_in)

    out = mix * env
    out /= np.max(np.abs(out)) + 1e-9
    _write_wav(path, out * 0.9)
    return dur


def make_impact(path, dur=0.42, seed=11):
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    t = np.linspace(0.0, dur, n, endpoint=False)

    body = np.sin(2.0 * np.pi * 95.0 * t) * np.exp(-t * 24.0)
    sub = np.sin(2.0 * np.pi * 52.0 * t) * np.exp(-t * 17.0) * 0.8

    noise = rng.normal(0.0, 1.0, n) * np.exp(-t * 70.0)
    slap = np.diff(noise, prepend=0.0)               # crude highpass "skin"

    out = body + sub + 0.55 * slap
    out /= np.max(np.abs(out)) + 1e-9

    fade = int(n * 0.12)
    out[-fade:] *= np.linspace(1.0, 0.0, fade)
    _write_wav(path, out * 0.95)
    return dur


def make_bell(path, dur=1.9):
    """A boxing ring bell — three bright 'ding's with inharmonic bell partials."""
    n = int(SR * dur)
    out = np.zeros(n)
    partials = [(1.0, 1.0), (2.76, 0.55), (5.40, 0.40), (8.93, 0.25), (13.34, 0.15)]
    f0 = 600.0
    for st in (0.0, 0.18, 0.36):           # ding-ding-ding
        start = int(st * SR)
        tt = np.arange(n - start) / SR
        tone = np.zeros(len(tt))
        for ratio, amp in partials:
            tone += amp * np.sin(2 * np.pi * f0 * ratio * tt)
        env = np.exp(-tt * 6.5) * (1.0 - np.exp(-tt * 350.0))   # fast attack, ring-out
        out[start:] += tone * env
    out /= np.max(np.abs(out)) + 1e-9
    _write_wav(path, out * 0.92)
    return dur


def wav_duration(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def ensure_bell(regen=False):
    ASSETS.mkdir(exist_ok=True)
    bell = ASSETS / "bell.wav"
    if regen or not bell.exists():
        make_bell(bell)
    return bell


def ensure_assets(regen=False):
    """Make sure both WAVs exist; generate if missing. Returns their paths."""
    ASSETS.mkdir(exist_ok=True)
    whoosh = ASSETS / "whoosh.wav"
    impact = ASSETS / "impact.wav"
    if regen or not whoosh.exists():
        make_whoosh(whoosh)
    if regen or not impact.exists():
        make_impact(impact)
    return whoosh, impact


if __name__ == "__main__":
    w, i = ensure_assets(regen=True)
    print(f"[+] {w}  ({wav_duration(w):.2f}s)")
    print(f"[+] {i}  ({wav_duration(i):.2f}s)")
