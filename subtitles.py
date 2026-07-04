"""Subtitle transcription + colored burn-in for FightSync.

Two jobs:
  • transcribe(wav) — faster-whisper, with the language FORCED (default 'en'). Auto language
    detection in current faster-whisper throws `max() arg is an empty sequence` on quiet/short
    audio, which previously got swallowed into an empty SRT → subtitles silently stopped
    burning. Forcing the language skips that path. Quiet facecam audio is boosted first.
  • build_ass(lines) — a libass/ASS file with PER-SPEAKER COLOURS. Your speech (facecam track)
    and the in-game COACH (gameplay track) get different colours; lines carry a `speaker` the
    user can override in the editor. ffmpeg burns the ASS with `subtitles=…`.

A "line" is {start, end, text, speaker}  (seconds on the composite/aligned timeline).
"""
import os
import re
import subprocess
import threading
from pathlib import Path

from media import FFMPEG

SR = 16000
# load each Whisper model ONCE per process and reuse it — re-loading on every call was slow
# and could stall; the cache + a lock make repeated/colliding transcriptions cheap and safe.
_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


def _get_model(model_name):
    with _MODEL_LOCK:
        m = _MODEL_CACHE.get(model_name)
        if m is None:
            from faster_whisper import WhisperModel
            m = WhisperModel(model_name, device="cpu", compute_type="int8")
            _MODEL_CACHE[model_name] = m
        return m


# default speaker colours (RGB hex). me = yellow, coach = cyan, ref = magenta/pink.
COLORS = {"me": "FFE24D", "coach": "53D8FF", "ref": "FF5FC4", "other": "FFFFFF"}

# The COACH (older Italian man) and the REFEREE are BOTH on the gameplay audio, so we can't tell
# them apart by track — we split them by what the REF characteristically says (boxing-referee
# commands). Distinctive multi-word phrases match anywhere; the short single-word commands
# ("Box!", "Break!") only count as the WHOLE short line, so the coach's "box him in" isn't taken.
_REF_PHRASES = re.compile(
    r"\b(seconds out|box on|stop boxing|break it up|come on box|"
    r"back to (your|the)( neutral)? corner|neutral corner|to your corner|"
    r"protect yourself|touch (gloves|hands|'?em up)|good clean fight|no holding|"
    r"watch the (head|low blows)|let'?s get it on|"
    r"penali[sz]|deduct|point off|eyes forward|warning|low blow|"
    r"round (one|two|three|four|five|six|seven|eight|nine|ten))\b", re.I)
_REF_ADDRESS = re.compile(r"^\W*(red|blue)\b[\s,!.]", re.I)   # the ref addresses a corner by colour
_REF_SHORT = re.compile(r"^\W*(box on|box|break|stop|fight|time)\W*$", re.I)


def label_ref(text):
    """Classify a GAMEPLAY-audio line as 'ref' (referee command/warning) or 'coach' (corner
    advice). The TotF referee addresses fighters by corner COLOUR and issues set commands."""
    t = (text or "").strip()
    if _REF_PHRASES.search(t):
        return "ref"
    if _REF_ADDRESS.match(t):
        return "ref"
    if len(t.split()) <= 3 and _REF_SHORT.search(t):
        return "ref"
    return "coach"


def _rgb_to_ass(hex_rgb: str) -> str:
    """'RRGGBB' → ASS primary colour '&H00BBGGRR' (opaque)."""
    h = (hex_rgb or "FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    return f"&H00{bb}{gg}{rr}".upper()


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); t -= h * 3600
    m = int(t // 60); t -= m * 60
    s = int(t); cs = int(round((t - s) * 100))
    if cs == 100:
        cs = 0; s += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def extract_audio(src: str, start: float, dur: float, out_wav: str, boost: bool = True) -> str:
    """Mono 16k wav for `dur` seconds from `start`. Quiet facecam speech is lifted with
    dynaudnorm so Whisper + VAD can hear it (the facecam mic runs ~20x quieter than game audio).
    NOTE: heavier processing (highpass/loudnorm/limiter/large gain) measurably HURT speech
    detection on quiet clips — these gentle settings are what actually let VAD find the voice."""
    af = ("dynaudnorm=g=11:m=18" if boost else "anull")
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{start}", "-i", src, "-t", f"{dur}",
         "-vn", "-af", af, "-ac", "1", "-ar", str(SR), out_wav], check=True)
    return out_wav


def _line_from_words(words, speaker, auto_ref):
    txt = "".join(w.word for w in words).strip()
    if not txt:
        return None
    st, en = float(words[0].start), float(words[-1].end)
    en = max(en, st + 0.7)                     # readable floor (still far tighter than before)
    sp = label_ref(txt) if auto_ref else speaker
    return {"start": st, "end": en, "text": txt, "speaker": sp}


def _segments_to_lines(segs, speaker, auto_ref):
    """Whisper segments → tight, well-timed caption lines via WORD timestamps: each caption
    starts at its first word and ends at its last word (no long trailing overhang), and a long
    run is split at natural pauses (>0.7s) or ~9 words so captions track speech as it's said."""
    lines = []
    for s in segs:
        words = [w for w in (getattr(s, "words", None) or []) if (w.word or "").strip()]
        if not words:                              # no word timing → fall back to the segment
            txt = (s.text or "").strip()
            if txt:
                sp = label_ref(txt) if auto_ref else speaker
                lines.append({"start": float(s.start), "end": float(s.end),
                              "text": txt, "speaker": sp})
            continue
        chunk = []
        for w in words:
            if chunk and (w.start - chunk[-1].end > 0.7 or len(chunk) >= 9):
                ln = _line_from_words(chunk, speaker, auto_ref)
                if ln:
                    lines.append(ln)
                chunk = []
            chunk.append(w)
        ln = _line_from_words(chunk, speaker, auto_ref)
        if ln:
            lines.append(ln)
    return lines


def speech_regions(src: str, ss: float, dur: float, work: str, tag: str = "me",
                   threshold: float = 0.3) -> list:
    """VAD-detected speech windows (composite timeline) for a track — EVERY moment with a voice,
    including ones Whisper didn't transcribe. The editor uses these to show where you spoke and
    flag the gaps to fill in. Returns [{start,end}] in seconds; [] on any failure."""
    wav = str(Path(work) / f"vad_{tag}.wav")
    try:
        extract_audio(src, ss, dur, wav)
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        audio = decode_audio(wav, sampling_rate=SR)
        ts = get_speech_timestamps(audio, vad_options=VadOptions(
            threshold=threshold, min_speech_duration_ms=120,
            min_silence_duration_ms=200, speech_pad_ms=100))
        return [{"start": round(t["start"] / SR, 2), "end": round(t["end"] / SR, 2)} for t in ts]
    except Exception:  # noqa: BLE001
        return []


def transcribe(wav: str, model_name: str = "base", language: str = "en", speaker: str = "me",
               progress=None, timeout: float = 300.0, auto_ref: bool = False) -> list:
    """→ [{start,end,text,speaker}] on the wav's own timeline (0 = wav start). Never raises and
    never hangs: runs in a worker thread with a hard `timeout` — on any failure (no speech,
    model error) or a stall it returns []. With `auto_ref`, each line is labelled coach/ref by
    `label_ref()` (used for the gameplay track, which carries BOTH voices)."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    result = {"lines": [], "done": False}

    def _work():
        try:
            if progress:
                progress(f"Loading Whisper '{model_name}'…")
            model = _get_model(model_name)
            if progress:
                progress("Transcribing…")
            # word_timestamps → tight per-word timing (kills the "caption lingers" overhang);
            # a LOWER VAD threshold + small pad catches quiet/short speech the default missed.
            segs, _ = model.transcribe(
                wav, beam_size=5, language=(language or "en"),
                word_timestamps=True, condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters=dict(threshold=0.3, min_speech_duration_ms=120,
                                    min_silence_duration_ms=300, speech_pad_ms=120))
            result["lines"] = _segments_to_lines(segs, speaker, auto_ref)
            result["done"] = True
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"no usable speech ({type(e).__name__})")

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():                       # genuine stall — don't let it freeze the job forever
        if progress:
            progress("transcription timed out — skipping these captions")
        return []
    return result["lines"]


def transcribe_track(src: str, ss: float, dur: float, work: str, tag: str,
                     speaker: str, model_name: str = "base", language: str = "en",
                     progress=None, auto_ref: bool = False) -> list:
    """Extract a track's audio (composite-aligned: ss = gs/fs) and transcribe → lines whose
    times are already on the COMPOSITE timeline (wav start = composite 0). `auto_ref` splits
    coach vs referee for the gameplay track."""
    wav = str(Path(work) / f"sub_{tag}.wav")
    try:
        extract_audio(src, ss, dur, wav)
    except subprocess.CalledProcessError:
        return []
    return transcribe(wav, model_name, language, speaker, progress, auto_ref=auto_ref)


def build_ass(lines: list, ass_path: str, w: int = 1920, h: int = 1080,
              colors: dict = None, fontsize: int = 96, margin_v: int = 64) -> str:
    """Write a colored ASS file. `lines` = [{start,end,text,speaker}] on the video timeline.
    `fontsize` = target text height in px AT 1080p (scaled to the real output height)."""
    colors = {**COLORS, **(colors or {})}
    # one style per speaker present (fall back to 'other')
    speakers = sorted({(ln.get("speaker") or "other") for ln in lines} | {"me"})
    fs = max(20, int(round(fontsize * h / 1080)))   # scale font to output height
    outline = max(2, int(round(fs / 16)))           # thicker outline keeps big text readable
    mv = int(round(margin_v * h / 1080))
    styles = []
    for sp in speakers:
        col = _rgb_to_ass(colors.get(sp, colors["other"]))
        styles.append(
            f"Style: {sp},Arial,{fs},{col},&H000000FF,&H00101010,&H64000000,"
            f"-1,0,0,0,100,100,0,0,1,{outline},0,2,40,40,{mv},1")
    body = []
    for ln in sorted(lines, key=lambda x: x["start"]):
        txt = (ln.get("text") or "").replace("\n", "\\N").strip()
        if not txt:
            continue
        sp = ln.get("speaker") or "other"
        if sp not in speakers:
            sp = "other"
        body.append(f"Dialogue: 0,{_ass_time(ln['start'])},{_ass_time(ln['end'])},{sp},,0,0,0,,{txt}")
    ass = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\nWrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n" + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        + "\n".join(body) + "\n"
    ) % (w, h)
    Path(ass_path).write_text(ass, encoding="utf-8")
    return ass_path


def lines_to_srt(lines: list, srt_path: str) -> str:
    def ts(t):
        t = max(0.0, t); h = int(t // 3600); t -= h * 3600
        m = int(t // 60); t -= m * 60; s = int(t); ms = int((t - s) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = []
    for i, ln in enumerate(sorted(lines, key=lambda x: x["start"]), 1):
        out.append(f"{i}\n{ts(ln['start'])} --> {ts(ln['end'])}\n{(ln.get('text') or '').strip()}\n")
    Path(srt_path).write_text("\n".join(out), encoding="utf-8")
    return srt_path
