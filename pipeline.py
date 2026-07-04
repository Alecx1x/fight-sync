"""End-to-end render pipeline.

Stages (each reports progress through a callback):
  1. probe both inputs
  2. compute audio sync offset
  3. transcribe facecam speech -> subtitles (optional)
  4. composite: trim-to-sync + picture-in-picture + audio mix + burn subs
  5. generate intro / outro title cards (optional)
  6. concat segments -> final.mp4
"""
from __future__ import annotations

import os
import shutil
import subprocess
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from media import FFMPEG, probe, run_ffmpeg
from sync import SR, compute_sync
from replay import (assemble_body, build_replay_clip, detect_impacts,
                    parse_timestamps)

# drawtext on Windows chokes on the "C:" drive colon no matter how it's escaped.
# We copy a system font into the job's work dir and reference it by a bare
# relative name (ffmpeg runs with cwd=work), avoiding all path escaping.
_FONT_SOURCES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]
_FONT_LOCAL = "font.ttf"


def _ensure_font(work: str) -> str:
    dst = Path(work) / _FONT_LOCAL
    if not dst.exists():
        for src in _FONT_SOURCES:
            if Path(src).exists():
                shutil.copyfile(src, dst)
                break
    return _FONT_LOCAL

PROGRESS = Callable[[int, str], None]


@dataclass
class RenderConfig:
    out_w: int = 1920
    out_h: int = 1080
    fps: int = 30
    # layout
    layout: str = "pip"               # pip (picture-in-picture) | sbs (side-by-side)
    # which clip is the BIG/main video. False (default) = gameplay is the full frame
    # and facecam is the small corner; True = swap them (facecam main, gameplay corner).
    swap_pip: bool = False
    # picture-in-picture
    pip_scale: float = 0.26          # small-clip width as fraction of frame
    pip_position: str = "br"          # br, bl, tr, tl
    pip_margin: int = 40
    pip_border: int = 4
    pip_border_color: str = "white"
    # colour "punch-up" — make footage pop (contrast/saturation/gamma + light sharpen)
    color_punch: bool = False
    punch_strength: float = 1.0       # 0..2 (1 = a tasteful default)
    # audio
    audio_mode: str = "mix"           # mix | gameplay | facecam
    # music bed — mixed UNDER the final video (spans intro/rounds/outro), with
    # optional sidechain ducking (music dips under commentary) + fade in/out.
    music_path: Optional[str] = None
    music_volume: float = 0.18        # 0..1 bed level
    music_duck: bool = True
    # on-screen "BIG HIT" flash + border on the biggest detected impacts
    hit_flash: bool = False
    hit_count: int = 6
    hit_text: str = "BIG HIT!"
    # subtitles
    make_subtitles: bool = True
    burn_subtitles: bool = True
    whisper_model: str = field(
        default_factory=lambda: os.environ.get("FIGHTSYNC_WHISPER_MODEL", "base")
    )
    sub_language: str = "en"               # forced language (auto-detect crashes on quiet audio)
    subtitle_coach: bool = True            # also transcribe the gameplay track (coach + referee)
    sub_color_me: str = "FFE24D"           # your speech (facecam) — warm yellow
    sub_color_coach: str = "53D8FF"        # in-game coach (gameplay) — cyan
    sub_color_ref: str = "FF5FC4"          # in-game referee (gameplay) — magenta/pink
    sub_font_size: int = 96                # caption text height in px at 1080p (scaled to output)
    # per-round user-edited subtitles from the Subtitle editor: edited_subs[i] =
    # [{start,end,text,speaker}] on that round's composite timeline. When present for a round,
    # they are burned VERBATIM (no transcription).
    edited_subs: list = field(default_factory=list)
    # per-round {gs,fs} the edited subs were AUTHORED against — if the render's timeline differs
    # (you re-synced / trimmed since), each line is auto-shifted to match. Empty = no remap.
    edited_subs_tl: list = field(default_factory=list)
    # intro / outro
    intro: bool = True
    outro: bool = True
    # optional lower-third banner (handle/name) on the opening of the first round
    lower_third: str = ""
    title: str = "The Thrill of the Fight 2"
    intro_subtitle: str = ""
    outro_title: str = "Thanks for watching"
    outro_subtitle: str = "Like & Subscribe"
    intro_seconds: float = 2.6
    outro_seconds: float = 4.0
    # manual sync override from the preview nudge (seconds; + = gameplay leads).
    # None -> auto-detect / start-align as usual.
    manual_offset: Optional[float] = None
    # PER-ROUND manual sync (clapperboard round tabs): manual_offsets[i] = that
    # round's offset, or None -> auto-detect that round. Falls back to manual_offset
    # for round 0 when the list is empty (back-compat).
    manual_offsets: list = field(default_factory=list)
    # trim on the synced-composite timeline (seconds). Cut dead air before the
    # bell / after the action. trim_start=0 + trim_end=None -> keep the whole pair.
    trim_start: float = 0.0
    trim_end: Optional[float] = None
    # PER-ROUND composite trim: round_trims[i] = {"in","out"} (or None). Falls back
    # to trim_start/trim_end for round 0 when empty (back-compat).
    round_trims: list = field(default_factory=list)
    # multicam: extra facecam angles + manual angle cuts for the FIRST round.
    # multicam_angles[0] = primary facecam; each = {"path": str, "offset": float}
    # (offset = gameplayTime − angleTime, like manual_offset). multicam_cuts =
    # [{"t": float, "angle": int}] → "from composite-time t, show that angle".
    # Active only when ≥2 angles are supplied; audio stays on the primary angle.
    multicam_angles: list = field(default_factory=list)
    multicam_cuts: list = field(default_factory=list)
    # per-round multicam (Phase 4): a SECOND live cam per round + per-round cuts.
    #   cam_b_paths[i]  = round i's second-cam clip ("" → that round is a normal PiP)
    #   round_cuts[i]   = [{"t": float, "cam": 0|1}] — from composite-time t show cam A
    #                     (facecam, 0) or cam B (1) in the PiP; gameplay always the bg
    #   cam_a_offsets[i]/cam_b_offsets[i] = manual sync (gameplayTime − camTime);
    #                     None/missing → auto-sync that round/cam via compute_sync
    cam_b_paths: list = field(default_factory=list)
    round_cuts: list = field(default_factory=list)
    cam_a_offsets: list = field(default_factory=list)
    cam_b_offsets: list = field(default_factory=list)
    # SPECTATOR view (3rd PiP, opposite corner): per-round extracted clip + offset (gameplayTime−specTime)
    spectator_clips: list = field(default_factory=list)
    spectator_offsets: list = field(default_factory=list)
    spectator_audio: bool = False
    spectator_scale: float = 0.0     # 0 = a touch bigger than the facecam PiP (pip_scale*1.15)
    spectator_volume: float = 0.8    # loudness of the spectator audio when mixed in (0–1+)
    spectator_credit: str = ""       # source-credit block (VOD + channel links) → top of the description
    # round transitions between multiple clips
    transitions: bool = True
    transition_label: str = "ROUND"     # -> "ROUND 2", "ROUND 3", …
    transition_seconds: float = 2.4
    transition_style: str = "card"      # card (round card) | flash (white-flash cut)
    bell: bool = True
    # cinematic slow-mo replays
    replays: bool = False
    replay_times: str = ""            # "1:23, 4:05" or seconds, comma-separated
    auto_replays: int = 0             # auto-detect this many biggest hits
    replay_smooth: bool = False       # motion-interpolate (fluid but slower)
    whoosh_gain: float = 0.40
    impact_gain: float = 0.70
    # manual slow-motion: marked live in the Sync tab. Per-round list of
    # [{"start","end"}] windows (seconds, on that round's composite timeline);
    # the WHOLE frame slows (main view + PiP together) easing in/out.
    slowmo_regions: list = field(default_factory=list)   # slowmo_regions[round] = [{start,end}]
    slowmo_speed: float = 0.35
    # encode
    crf: int = 20
    preset: str = "medium"


_POS = {
    "br": "W-w-{m}:H-h-{m}",
    "bl": "{m}:H-h-{m}",
    "tr": "W-w-{m}:{m}",
    "tl": "{m}:{m}",
}
_OPP = {"br": "tl", "bl": "tr", "tr": "bl", "tl": "br"}   # opposite corner for the spectator PiP


def _spectator_filters(chain, vlabel, cfg, idx, gs, spec_in):
    """Append the SPECTATOR clip as a PiP in the corner OPPOSITE the facecam. `spec_in` = its ffmpeg
    input index (caller must add '-i <clip>'). Returns (new_vlabel, want_audio, spec_start, has_audio)."""
    clip = cfg.spectator_clips[idx] if idx < len(cfg.spectator_clips) else ""
    if not clip:
        return vlabel, False, 0.0, False
    off = float(cfg.spectator_offsets[idx]) if (idx < len(cfg.spectator_offsets)
                                                and cfg.spectator_offsets[idx] is not None) else 0.0
    spec_start = max(0.0, gs - off)
    scale = cfg.spectator_scale if cfg.spectator_scale > 0 else cfg.pip_scale * 1.15
    pw = round(cfg.out_w * scale / 2) * 2
    bw = cfg.pip_border
    pos = _POS[_OPP.get(cfg.pip_position, "tl")].format(m=cfg.pip_margin)
    chain.append(f"[{spec_in}:v]trim=start={spec_start:.3f},setpts=PTS-STARTPTS,"
                 f"scale={pw}:-2,pad=iw+{2 * bw}:ih+{2 * bw}:{bw}:{bw}:"
                 f"color={cfg.pip_border_color}[spv]")
    chain.append(f"[{vlabel}][spv]overlay={pos}:eof_action=pass[vsp]")
    has_audio = False
    if cfg.spectator_audio:
        try:
            has_audio = probe(clip).has_audio
        except Exception:  # noqa: BLE001
            has_audio = False
    return "vsp", (cfg.spectator_audio and has_audio), spec_start, has_audio


def _enc(crf: int, preset: str) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
    ]


def _title_card(dst: str, work: str, cfg: RenderConfig, dur: float,
                big: str, small: str) -> None:
    """Render a clean dark title card with fade in/out and silent audio."""
    # write text to files so we never have to escape user content
    big_f = Path(work) / (Path(dst).stem + "_big.txt")
    small_f = Path(work) / (Path(dst).stem + "_small.txt")
    big_f.write_text(big or " ", encoding="utf-8")
    small_f.write_text(small or " ", encoding="utf-8")

    font = _ensure_font(work)
    fin, fout = 0.5, 0.5
    alpha = (f"if(lt(t,{fin}),t/{fin},"
             f"if(gt(t,{dur}-{fout}),({dur}-t)/{fout},1))")

    big_dt = (
        f"drawtext=fontfile={font}:textfile='{big_f.name}'"
        f":fontcolor=white:fontsize=(h/10):borderw=2:bordercolor=black@0.5"
        f":x=(w-text_w)/2:y=(h/2)-text_h:alpha='{alpha}'"
    )
    small_dt = (
        f"drawtext=fontfile={font}:textfile='{small_f.name}'"
        f":fontcolor=0x9AA4B2:fontsize=(h/26):x=(w-text_w)/2:y=(h/2)+text_h*0.5"
        f":alpha='{alpha}'"
    )
    accent = (
        "drawbox=x=(w-iw*0.16)/2:y=(h/2)+ih*0.02:w=iw*0.16:h=6"
        ":color=0xE23B3B:t=fill:enable='gte(t,0.2)'"
    )
    # thin broadcast-style accent rules top & bottom + a vignette for depth
    rules = ("drawbox=x=0:y=0:w=iw:h=6:color=0xE23B3B:t=fill,"
             "drawbox=x=0:y=ih-6:w=iw:h=6:color=0xE23B3B:t=fill")
    vf = f"format=yuv420p,vignette=PI/5,{big_dt},{small_dt},{accent},{rules}"

    run_ffmpeg([
        "-f", "lavfi", "-i",
        f"color=c=0x0E1116:s={cfg.out_w}x{cfg.out_h}:d={dur}:r={cfg.fps}",
        "-f", "lavfi", "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", vf, "-t", f"{dur}",
        *_enc(cfg.crf, cfg.preset), dst,
    ], cwd=work)


def _transcribe(audio_wav: str, srt_path: str, model_name: str,
                progress: PROGRESS) -> None:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel

    def ts(t: float) -> str:
        h = int(t // 3600); t -= h * 3600
        m = int(t // 60); t -= m * 60
        s = int(t); ms = int((t - s) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    progress(35, f"Loading Whisper model '{model_name}' (first run downloads it)…")
    lines: list[str] = []
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        progress(45, "Transcribing speech…")
        segments, _ = model.transcribe(audio_wav, vad_filter=True,
                                       beam_size=5, language=None)
        i = 1
        for seg in segments:           # generator: work happens here
            text = seg.text.strip()
            if not text:
                continue
            lines.append(f"{i}\n{ts(seg.start)} --> {ts(seg.end)}\n{text}\n")
            i += 1
    except Exception as e:             # noqa: BLE001
        # e.g. no detectable speech in this stretch -> just skip subtitles
        progress(48, f"No usable speech detected ({type(e).__name__}); "
                     f"skipping subtitles.")
        lines = []

    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")


def _transition_card(dst: str, work: str, cfg: RenderConfig, text: str,
                     dur: float) -> None:
    """A round-card transition: black screen, white text fading in/out, with a
    boxing-bell ring. Segments fade to/from black around it for a clean blend."""
    from replay_sfx import ensure_bell
    font = _ensure_font(work)
    tf = Path(work) / (Path(dst).stem + "_t.txt")
    tf.write_text(text or " ", encoding="utf-8")
    fin, fout = 0.45, 0.55
    alpha = (f"if(lt(t,{fin}),t/{fin},"
             f"if(gt(t,{dur}-{fout}),({dur}-t)/{fout},1))")
    dt = (f"drawtext=fontfile={font}:textfile='{tf.name}':fontcolor=white"
          f":fontsize=(h/8):x=(w-text_w)/2:y=(h-text_h)/2:alpha='{alpha}'")
    accent = ("drawbox=x=(w-iw*0.10)/2:y=(h/2)+ih*0.10:w=iw*0.10:h=4"
              ":color=0xE23B3B:t=fill")
    rules = ("drawbox=x=0:y=0:w=iw:h=6:color=0xE23B3B:t=fill,"
             "drawbox=x=0:y=ih-6:w=iw:h=6:color=0xE23B3B:t=fill")
    vf = f"format=yuv420p,{dt},{accent},{rules}"
    if (cfg.transition_style or "card").lower() == "flash":
        # punchier: a quick white flash opening and closing the card
        fl = min(0.2, dur / 4)
        vf += (f",fade=t=in:st=0:d={fl:.2f}:color=white"
               f",fade=t=out:st={max(0, dur - fl):.2f}:d={fl:.2f}:color=white")

    if cfg.bell:
        bell = ensure_bell()
        afilter = ("[1:a]volume=0.9,adelay=120:all=1,aresample=48000,"
                   "aformat=channel_layouts=stereo,apad[a]")
        inputs = ["-f", "lavfi", "-i",
                  f"color=c=black:s={cfg.out_w}x{cfg.out_h}:d={dur}:r={cfg.fps}",
                  "-i", str(bell)]
    else:
        afilter = "anullsrc=channel_layout=stereo:sample_rate=48000[a]"
        inputs = ["-f", "lavfi", "-i",
                  f"color=c=black:s={cfg.out_w}x{cfg.out_h}:d={dur}:r={cfg.fps}"]

    run_ffmpeg([
        *inputs, "-filter_complex", f"[0:v]{vf}[v];{afilter}",
        "-map", "[v]", "-map", "[a]", "-t", f"{dur}",
        *_enc(cfg.crf, cfg.preset), dst,
    ], cwd=work)


def _overlay_hits(src: str, dst: str, work: str, cfg: RenderConfig,
                  dur: float) -> str:
    """Flash a 'BIG HIT' label + a coloured border on the biggest detected
    impacts. Returns dst, or src unchanged if no impacts were found."""
    impacts = detect_impacts(src, work, cfg.hit_count, min_gap=2.5)
    hits: list[float] = []
    for t in sorted(impacts):
        if 0.3 < t < dur - 0.5 and all(abs(t - v) >= 1.5 for v in hits):
            hits.append(t)
    if not hits:
        return src
    font = _ensure_font(work)
    tf = Path(work) / f"hittext_{Path(src).stem}.txt"
    tf.write_text((cfg.hit_text or "BIG HIT!").strip(), encoding="utf-8")
    fs = max(40, cfg.out_h // 11)
    bt = max(8, cfg.out_w // 120)
    parts = []
    for t in hits:
        parts.append(
            f"drawtext=fontfile={font}:textfile='{tf.name}':fontcolor=white:"
            f"fontsize={fs}:borderw=6:bordercolor=black@0.9:box=1:boxcolor=red@0.55:"
            f"boxborderw=20:x=(w-text_w)/2:y=h*0.12:"
            f"enable='between(t,{t:.2f},{t + 0.7:.2f})'")
        parts.append(
            f"drawbox=x=0:y=0:w=iw:h=ih:color=yellow@0.85:t={bt}:"
            f"enable='between(t,{t:.2f},{t + 0.18:.2f})'")
    run_ffmpeg([
        "-i", src, "-vf", ",".join(parts),
        *_enc(cfg.crf, cfg.preset), "-c:a", "copy", dst,
    ], cwd=work)
    return dst


def _cut_segments(cuts: list, n_angles: int, out_dur: float) -> list:
    """Turn [{t, angle}] switch points into ordered (start, end, angle) spans that
    tile [0, out_dur). Angle 0 (primary) fills the start and any gaps."""
    pts = sorted(cuts, key=lambda c: float(c.get("t", 0) or 0))
    segs, cur_ang, cur_start = [], 0, 0.0
    for c in pts:
        t = max(0.0, min(float(c.get("t", 0) or 0), out_dur))
        ang = int(c.get("angle", 0))
        if not (0 <= ang < n_angles):
            ang = 0
        if t > cur_start + 0.01:
            segs.append((cur_start, t, cur_ang))
            cur_start = t
        cur_ang = ang
    if cur_start < out_dur - 0.01:
        segs.append((cur_start, out_dur, cur_ang))
    return segs or [(0.0, out_dur, 0)]


def _round_off(lst: list, i: int):
    """Per-round offset override, or None to auto-sync that round."""
    if i < len(lst):
        v = lst[i]
        return None if v is None else float(v)
    return None


def _remap_edited(lines, cfg, idx, render_fs, render_gs, prog, tag):
    """Self-correcting subtitles: if the edited subs were authored on a DIFFERENT timeline (you
    re-synced or trimmed since saving them), shift each line back onto the speech — 'me' lines by
    (authoring_fs − render_fs), coach/ref by (authoring_gs − render_gs). No stamp (old subs) → no-op."""
    tl = cfg.edited_subs_tl[idx] if (cfg.edited_subs_tl and idx < len(cfg.edited_subs_tl)) else None
    if not tl:
        return
    try:
        dfs = float(tl.get("fs", render_fs)) - render_fs
        dgs = float(tl.get("gs", render_gs)) - render_gs
    except (TypeError, ValueError, AttributeError):
        return
    if abs(dfs) < 0.02 and abs(dgs) < 0.02:
        return
    for ln in lines:
        sh = dfs if ln.get("speaker") == "me" else dgs
        ln["start"] = max(0.0, float(ln.get("start", 0.0)) + sh)
        ln["end"] = max(0.0, float(ln.get("end", 0.0)) + sh)
    prog(0.3, f"{tag}: re-aligned subtitles to current sync (you {dfs:+.1f}s, coach {dgs:+.1f}s)")


def _build_multicam_segment(gameplay: str, angles: list, cuts: list,
                            cfg: RenderConfig, work: str,
                            progress: PROGRESS, idx: int, lo: int, hi: int,
                            trim_start: float = 0.0,
                            trim_end: Optional[float] = None) -> dict:
    """Composite gameplay with MULTIPLE facecam angles, switching between them at
    the user's cuts. `angles` = [{"path","offset"}] (index 0 = primary), `cuts` =
    [{"t","angle"}]. Each angle independently synced (its own offset); audio stays
    on the primary angle. Same return shape as _build_segment (no auto
    replays/subtitles in multicam)."""
    work_p = Path(work)

    def prog(frac, msg):
        progress(int(lo + (hi - lo) * frac), msg)

    tag = f"Clip {idx + 1}"
    g = probe(gameplay)
    angs = [{"path": a["path"], "off": float(a.get("offset", 0.0)),
             "info": probe(a["path"])} for a in angles]
    n = len(angs)
    prog(0.1, f"{tag}: multicam ({n} angles)…")

    # GAMEPLAY IS THE SPINE: it plays its FULL duration; each angle is overlaid only where it actually
    # has footage — it APPEARS when its camera started (off>0) and DISAPPEARS when it ran out. So a cam
    # that started late never pushes the round's start in, and a dead cam never cuts the round short.
    gs = 0.0                                          # gameplay anchor = its own start
    for a in angs:
        a["start"] = max(0.0, gs - a["off"])          # trim INTO the cam (0 if it started after gameplay)
        a["appear"] = max(0.0, a["off"] - gs)         # composite time the cam first has footage
        a["avail"] = max(0.0, a["info"].duration - a["start"])   # seconds of cam footage from there
    out_dur = g.duration - gs                          # the FULL gameplay defines the round length
    if out_dur <= 0.3:
        raise RuntimeError(f"Clip {idx + 1}: the gameplay is empty or too short.")

    ta = max(0.0, float(trim_start or 0.0))
    tb = out_dur if (trim_end is None or trim_end <= 0) else min(float(trim_end), out_dur)
    ta = min(ta, max(0.0, tb - 0.3))
    if ta > 0.0 or tb < out_dur - 1e-3:
        gs += ta                                       # trim shifts the gameplay anchor in
        for a in angs:
            a["start"] = max(0.0, gs - a["off"])
            a["appear"] = max(0.0, a["off"] - gs)
            a["avail"] = max(0.0, a["info"].duration - a["start"])
        out_dur = max(0.3, tb - ta)

    # ── subtitles (multicam): primary angle (webcam) = you, gameplay = coach/ref. Edited subs
    # from the editor win verbatim. Burned via the same coloured ASS as the single-cam path.
    import subtitles as subs_mod
    make_subs = cfg.make_subtitles
    burn_subs = cfg.burn_subtitles and make_subs
    ass_rel = f"subs_{idx}.ass"
    ass_path = str(work_p / ass_rel)
    sub_lines = []
    edited = cfg.edited_subs[idx] if (cfg.edited_subs and idx < len(cfg.edited_subs)) else None
    if make_subs and edited:
        sub_lines = [dict(ln) for ln in edited if (ln.get("text") or "").strip()]
        _remap_edited(sub_lines, cfg, idx, render_fs=angs[0]["start"], render_gs=gs, prog=prog, tag=tag)
        prog(0.32, f"{tag}: using your edited subtitles ({len(sub_lines)} lines)")
    elif make_subs:
        if angs[0]["info"].has_audio:
            prog(0.3, f"{tag}: transcribing your speech…")
            me_lines = subs_mod.transcribe_track(angs[0]["path"], angs[0]["start"], out_dur, work,
                                                 f"{idx}_me", "me", cfg.whisper_model, cfg.sub_language,
                                                 lambda m: prog(0.33, f"{tag}: {m}"))
            _ap0 = angs[0]["appear"]                    # webcam appears late → shift its subs to match
            if _ap0 > 0.001:
                for ln in me_lines:
                    ln["start"] = ln.get("start", 0) + _ap0
                    ln["end"] = ln.get("end", 0) + _ap0
            sub_lines += me_lines
        if cfg.subtitle_coach and g.has_audio:
            prog(0.35, f"{tag}: transcribing the coach + ref…")
            sub_lines += subs_mod.transcribe_track(gameplay, gs, out_dur, work, f"{idx}_coach", "coach",
                                                   cfg.whisper_model, cfg.sub_language,
                                                   lambda m: prog(0.37, f"{tag}: {m}"), auto_ref=True)
    if make_subs and sub_lines:
        subs_mod.build_ass(sub_lines, ass_path, w=cfg.out_w, h=cfg.out_h,
                           colors={"me": cfg.sub_color_me, "coach": cfg.sub_color_coach,
                                   "ref": cfg.sub_color_ref}, fontsize=cfg.sub_font_size)
        subs_mod.lines_to_srt(sub_lines, str(work_p / f"subs_{idx}.srt"))
    else:
        burn_subs = False

    segs = _cut_segments(cuts, n, out_dur)
    # Clip every span to its angle's AVAILABILITY window [appear, appear+avail): a cam shows only where
    # it has footage. Where a cut-to angle isn't available (started late / died), the webcam (angle 0)
    # covers it IF it has footage there; otherwise the gameplay just shows full-screen (no PiP).
    def _avwin(k):
        return (angs[k]["appear"], angs[k]["appear"] + angs[k]["avail"])
    out_segs = []
    for (s, e, ang) in segs:
        lo, hi = _avwin(ang)
        a, b = max(s, lo), min(e, hi)
        if b > a + 0.05:
            out_segs.append((a, b, ang))
        if ang != 0:                                   # cover the gaps (before it appeared / after it died) with the webcam
            lo0, hi0 = _avwin(0)
            for (us, ue) in ((s, min(e, lo)), (max(s, hi), e)):
                a0, b0 = max(us, lo0), min(ue, hi0)
                if b0 > a0 + 0.05:
                    out_segs.append((a0, b0, 0))
    segs = out_segs
    prog(0.4, f"{tag}: {len(segs)} angle span(s), {out_dur:.0f}s (gameplay full)")
    windows: dict = {}
    for (s, e, ang) in segs:
        windows.setdefault(ang, []).append((s, e))

    def _enable(k):
        return "+".join(f"between(t,{s:.3f},{e:.3f})" for (s, e) in windows[k])

    OW, OH = cfg.out_w, cfg.out_h
    chain = []
    if cfg.layout == "sbs":
        half = (OW // 2) // 2 * 2
        chain.append(f"[0:v]trim=start={gs},setpts=PTS-STARTPTS,"
                     f"scale={half}:{OH}:force_original_aspect_ratio=decrease,"
                     f"pad={half}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[L]")
        chain.append(f"[L]pad={OW}:{OH}:0:0:color=black[base]")
        prev = "base"
        for k in sorted(windows):
            _ap = angs[k]["appear"]
            _pts = f"PTS-STARTPTS+{_ap:.3f}/TB" if _ap > 0.001 else "PTS-STARTPTS"
            chain.append(f"[{k + 1}:v]trim=start={angs[k]['start']},setpts={_pts},"
                         f"scale={half}:{OH}:force_original_aspect_ratio=decrease,"
                         f"pad={half}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[r{k}]")
            chain.append(f"[{prev}][r{k}]overlay={half}:0:enable='{_enable(k)}'[s{k}]")
            prev = f"s{k}"
        vlabel = prev
    else:                                    # pip
        pw = round(OW * cfg.pip_scale / 2) * 2
        bw = cfg.pip_border
        pos = _POS[cfg.pip_position].format(m=cfg.pip_margin)
        chain.append(f"[0:v]trim=start={gs},setpts=PTS-STARTPTS,"
                     f"scale={OW}:{OH}:force_original_aspect_ratio=decrease,"
                     f"pad={OW}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[bg]")
        prev = "bg"
        for k in sorted(windows):
            _ap = angs[k]["appear"]
            _pts = f"PTS-STARTPTS+{_ap:.3f}/TB" if _ap > 0.001 else "PTS-STARTPTS"
            chain.append(f"[{k + 1}:v]trim=start={angs[k]['start']},setpts={_pts},"
                         f"scale={pw}:-2,pad=iw+{2 * bw}:ih+{2 * bw}:{bw}:{bw}:"
                         f"color={cfg.pip_border_color}[pip{k}]")
            chain.append(f"[{prev}][pip{k}]overlay={pos}:enable='{_enable(k)}'[s{k}]")
            prev = f"s{k}"
        vlabel = prev

    # SPECTATOR view PiP (opposite corner) — composited before colour/subs so those apply on top
    _spec_clip = cfg.spectator_clips[idx] if idx < len(cfg.spectator_clips) else ""
    _spec_in = 1 + len(angs)
    vlabel, _spec_audio, _spec_start, _ = _spectator_filters(chain, vlabel, cfg, idx, gs, _spec_in)

    if cfg.color_punch:
        ps = max(0.0, min(2.0, cfg.punch_strength))
        con, sat, gam = 1.0 + 0.12 * ps, 1.0 + 0.20 * ps, 1.0 - 0.03 * ps
        chain.append(f"[{vlabel}]eq=contrast={con:.3f}:saturation={sat:.3f}:gamma={gam:.3f},"
                     f"unsharp=5:5:{0.5 * ps:.2f}:5:5:0.0[vcol]")
        vlabel = "vcol"
    if cfg.lower_third.strip():
        font = _ensure_font(work)
        lt = work_p / "lower_third.txt"
        lt.write_text(cfg.lower_third.strip(), encoding="utf-8")
        a0, b0 = 0.6, 4.8
        la = f"if(lt(t-{a0},0.3),(t-{a0})/0.3,if(gt(t,{b0}-0.4),({b0}-t)/0.4,1))"
        chain.append(
            f"[{vlabel}]"
            f"drawbox=x=0:y=ih*0.80:w=iw*0.44:h=ih*0.12:color=black@0.55:t=fill:enable='between(t,{a0},{b0})',"
            f"drawbox=x=0:y=ih*0.80:w=12:h=ih*0.12:color=0xE23B3B:t=fill:enable='between(t,{a0},{b0})',"
            f"drawtext=fontfile={font}:textfile='{lt.name}':fontcolor=white:fontsize=h/22:"
            f"x=36:y=h*0.80+(h*0.12-text_h)/2:enable='between(t,{a0},{b0})':alpha='{la}'[vlt]")
        vlabel = "vlt"
    if burn_subs:                                # coloured ASS carries its own styles
        chain.append(f"[{vlabel}]subtitles={ass_rel}[vsub]")
        vlabel = "vsub"

    have_g, have_f = g.has_audio, angs[0]["info"].has_audio
    a0s = angs[0]["start"]
    a0ap = angs[0]["appear"]                          # webcam started late → delay its audio so it lines up
    _fdly = f",adelay={int(a0ap * 1000)}|{int(a0ap * 1000)}" if a0ap > 0.001 else ""
    if cfg.audio_mode == "gameplay" or not have_f:
        chain.append(f"[0:a]atrim=start={gs},asetpts=PTS-STARTPTS,aresample=48000[aout]")
    elif cfg.audio_mode == "facecam" or not have_g:
        chain.append(f"[1:a]atrim=start={a0s},asetpts=PTS-STARTPTS,aresample=48000{_fdly}[aout]")
    else:
        chain.append(f"[0:a]atrim=start={gs},asetpts=PTS-STARTPTS,aresample=48000[ag]")
        chain.append(f"[1:a]atrim=start={a0s},asetpts=PTS-STARTPTS,aresample=48000{_fdly}[af]")
        chain.append("[ag][af]amix=inputs=2:duration=first:normalize=0,"
                     "dynaudnorm=f=200[aout]")

    _amap = "[aout]"
    if _spec_audio:                                  # mix the spectator clip's own audio in (toggle + volume)
        chain.append(f"[{_spec_in}:a]atrim=start={_spec_start:.3f},asetpts=PTS-STARTPTS,"
                     f"aresample=48000,volume={max(0.0, cfg.spectator_volume):.2f}[asp]")
        chain.append("[aout][asp]amix=inputs=2:duration=first:normalize=0[aoutf]")
        _amap = "[aoutf]"

    inputs = ["-i", gameplay]
    for a in angs:
        inputs += ["-i", a["path"]]
    if _spec_clip:
        inputs += ["-i", _spec_clip]
    main = str(work_p / f"main_{idx}.mp4")
    prog(0.65, f"{tag}: compositing multicam…")
    run_ffmpeg([
        *inputs, "-filter_complex", ";".join(chain),
        "-map", f"[{vlabel}]", "-map", _amap,
        "-r", str(cfg.fps), "-t", f"{out_dur}",
        *_enc(cfg.crf, cfg.preset), main,
    ], cwd=work)
    body = main
    # manual slow-mo instant-replay box (uses the primary cam for the 2-up here)
    my_slow = cfg.slowmo_regions[idx] if idx < len(cfg.slowmo_regions) else None
    if my_slow:
        prog(0.85, f"{tag}: slow-mo replay box…")
        sb = str(work_p / f"slowbody_{idx}.mp4")
        out_dur = build_slowmo_replays(main, gameplay, angs[0]["path"], gs,
                                       angs[0]["start"], out_dur, my_slow, work, sb,
                                       cfg, idx, default_speed=cfg.slowmo_speed)
        body = sb
    return {"body": body, "out_dur": out_dur, "body_dur": probe(body).duration,
            "offset": angs[0]["off"], "confidence": 1.0,
            "srt": (str(work_p / f"subs_{idx}.srt") if (make_subs and sub_lines) else None),
            "replays": []}


def _build_segment(gameplay: str, facecam: str, cfg: RenderConfig, work: str,
                   progress: PROGRESS, idx: int, lo: int, hi: int,
                   manual_times: list[float],
                   manual_offset: Optional[float] = None,
                   trim_start: float = 0.0,
                   trim_end: Optional[float] = None) -> dict:
    """Sync one (gameplay, facecam) pair, composite the PiP, optionally subtitle
    and add slow-mo replays. Returns the segment body clip + sync info."""
    work_p = Path(work)

    def prog(frac, msg):
        progress(int(lo + (hi - lo) * frac), msg)

    tag = f"Clip {idx + 1}"
    prog(0.05, f"{tag}: inspecting…")
    g = probe(gameplay)
    f = probe(facecam)

    prog(0.18, f"{tag}: auto-syncing…")
    s = compute_sync(gameplay, facecam, work)
    if manual_offset is not None:
        # user fine-tuned the alignment in the preview — trust it absolutely
        off = manual_offset
        gs, fs = max(off, 0.0), max(-off, 0.0)
        mode = "manual"
    elif s.confidence < 0.20:
        # no trustworthy audio lock — the detected offset is unreliable, so DON'T
        # use it (it would misalign synchronous clips). Align from the start.
        gs, fs = 0.0, 0.0
        mode = "start"
    else:
        gs, fs = s.a_start, s.b_start
        mode = "auto"
    out_dur = min(g.duration - gs, f.duration - fs)
    if out_dur <= 0.3:                       # overlap collapsed — fall back to start
        gs, fs = 0.0, 0.0
        out_dur = min(g.duration, f.duration)
        mode = "start"
    if out_dur <= 0.3:
        raise RuntimeError(f"Clip {idx + 1}: a video is empty or too short.")

    # user trim on the composite timeline: keep [a, b] (seconds into the overlap).
    # Shift both clip starts by a and shorten out_dur; everything downstream
    # (video, audio, subtitle window, replay bounds) reads gs/fs/out_dur.
    a = max(0.0, float(trim_start or 0.0))
    b = out_dur if (trim_end is None or trim_end <= 0) else min(float(trim_end), out_dur)
    a = min(a, max(0.0, b - 0.3))                 # keep at least 0.3s
    if a > 0.0 or b < out_dur - 1e-3:
        gs += a
        fs += a
        out_dur = max(0.3, b - a)
        prog(0.3, f"{tag}: trimmed to {out_dur:.0f}s (kept {a:.1f}s–{b:.1f}s)")

    if mode == "manual":
        prog(0.3, f"{tag}: manual offset {manual_offset:+.2f}s, {out_dur:.0f}s")
    elif mode == "start":
        prog(0.3, f"{tag}: no shared-audio lock — aligned from the start "
                  f"({out_dur:.0f}s)")
    else:
        prog(0.3, f"{tag}: offset {s.offset_seconds:+.2f}s, {out_dur:.0f}s synced")

    # ── subtitles: per-speaker COLOURED captions (your facecam = you, gameplay = the in-game
    # coach), burned via a libass ASS file. User-edited subs (from the editor) win verbatim.
    import subtitles as subs_mod
    make_subs = cfg.make_subtitles
    burn_subs = cfg.burn_subtitles and make_subs
    ass_rel = f"subs_{idx}.ass"
    ass_path = str(work_p / ass_rel)
    lines = []
    edited = cfg.edited_subs[idx] if (cfg.edited_subs and idx < len(cfg.edited_subs)) else None
    if make_subs and edited:                              # user-edited → burn exactly as given
        lines = [dict(ln) for ln in edited if (ln.get("text") or "").strip()]
        _remap_edited(lines, cfg, idx, render_fs=fs, render_gs=gs, prog=prog, tag=tag)
        prog(0.5, f"{tag}: using your edited subtitles ({len(lines)} lines)")
    elif make_subs:                                       # auto-transcribe both tracks
        if f.has_audio:
            prog(0.35, f"{tag}: transcribing your speech…")
            lines += subs_mod.transcribe_track(facecam, fs, out_dur, work, f"{idx}_me", "me",
                                               cfg.whisper_model, cfg.sub_language,
                                               lambda m: prog(0.42, f"{tag}: {m}"))
        if cfg.subtitle_coach and g.has_audio:
            prog(0.48, f"{tag}: transcribing the coach + ref…")
            lines += subs_mod.transcribe_track(gameplay, gs, out_dur, work, f"{idx}_coach", "coach",
                                               cfg.whisper_model, cfg.sub_language,
                                               lambda m: prog(0.54, f"{tag}: {m}"), auto_ref=True)
    if make_subs and lines:
        subs_mod.build_ass(lines, ass_path, w=cfg.out_w, h=cfg.out_h,
                           colors={"me": cfg.sub_color_me, "coach": cfg.sub_color_coach,
                                   "ref": cfg.sub_color_ref}, fontsize=cfg.sub_font_size)
        subs_mod.lines_to_srt(lines, str(work_p / f"subs_{idx}.srt"))   # sidecar .srt for download
    else:
        make_subs = burn_subs = False

    prog(0.6, f"{tag}: compositing…")
    OW, OH = cfg.out_w, cfg.out_h

    # [0:v]=gameplay, [1:v]=facecam. The BIG/main clip is gameplay by default;
    # swap_pip makes facecam the main and gameplay the small one. Each clip keeps
    # its own sync trim (gs for gameplay, fs for facecam).
    if cfg.swap_pip:
        big_in, big_trim, small_in, small_trim = "1:v", fs, "0:v", gs
    else:
        big_in, big_trim, small_in, small_trim = "0:v", gs, "1:v", fs

    if cfg.layout == "sbs":
        # side-by-side, both halves the same size
        half = (OW // 2) // 2 * 2
        chain = [
            f"[{big_in}]trim=start={big_trim},setpts=PTS-STARTPTS,"
            f"scale={half}:{OH}:force_original_aspect_ratio=decrease,"
            f"pad={half}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[L]",
            f"[{small_in}]trim=start={small_trim},setpts=PTS-STARTPTS,"
            f"scale={half}:{OH}:force_original_aspect_ratio=decrease,"
            f"pad={half}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[R]",
            "[L][R]hstack=inputs=2[vov]",
        ]
    else:
        pw = round(OW * cfg.pip_scale / 2) * 2
        bw = cfg.pip_border
        pos = _POS[cfg.pip_position].format(m=cfg.pip_margin)
        chain = [
            f"[{big_in}]trim=start={big_trim},setpts=PTS-STARTPTS,"
            f"scale={OW}:{OH}:force_original_aspect_ratio=decrease,"
            f"pad={OW}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[bg]",
            f"[{small_in}]trim=start={small_trim},setpts=PTS-STARTPTS,scale={pw}:-2,"
            f"pad=iw+{2*bw}:ih+{2*bw}:{bw}:{bw}:color={cfg.pip_border_color}[pip]",
            f"[bg][pip]overlay={pos}:shortest=1[vov]",
        ]
    vlabel = "vov"
    if cfg.color_punch:
        # tasteful pop, scaled by strength; applied BEFORE subtitles so text stays clean
        ps = max(0.0, min(2.0, cfg.punch_strength))
        con, sat, gam = 1.0 + 0.12 * ps, 1.0 + 0.20 * ps, 1.0 - 0.03 * ps
        chain.append(f"[{vlabel}]eq=contrast={con:.3f}:saturation={sat:.3f}:gamma={gam:.3f},"
                     f"unsharp=5:5:{0.5 * ps:.2f}:5:5:0.0[vcol]")
        vlabel = "vcol"
    if burn_subs:
        # ASS carries its own per-speaker colours/styles, so no force_style needed.
        chain.append(f"[{vlabel}]subtitles={ass_rel}[vsub]")
        vlabel = "vsub"

    if cfg.lower_third.strip() and idx == 0:
        font = _ensure_font(work)
        lt = work_p / "lower_third.txt"
        lt.write_text(cfg.lower_third.strip(), encoding="utf-8")
        a, b = 0.6, 4.8
        la = f"if(lt(t-{a},0.3),(t-{a})/0.3,if(gt(t,{b}-0.4),({b}-t)/0.4,1))"
        chain.append(
            f"[{vlabel}]"
            f"drawbox=x=0:y=ih*0.80:w=iw*0.44:h=ih*0.12:color=black@0.55:t=fill:enable='between(t,{a},{b})',"
            f"drawbox=x=0:y=ih*0.80:w=12:h=ih*0.12:color=0xE23B3B:t=fill:enable='between(t,{a},{b})',"
            f"drawtext=fontfile={font}:textfile='{lt.name}':fontcolor=white:fontsize=h/22:"
            f"x=36:y=h*0.80+(h*0.12-text_h)/2:enable='between(t,{a},{b})':alpha='{la}'"
            f"[vlt]")
        vlabel = "vlt"

    have_g, have_f = g.has_audio, f.has_audio
    mode = cfg.audio_mode
    if mode == "gameplay" or not have_f:
        chain.append(f"[0:a]atrim=start={gs},asetpts=PTS-STARTPTS,aresample=48000[aout]")
    elif mode == "facecam" or not have_g:
        chain.append(f"[1:a]atrim=start={fs},asetpts=PTS-STARTPTS,aresample=48000[aout]")
    else:
        chain.append(f"[0:a]atrim=start={gs},asetpts=PTS-STARTPTS,aresample=48000[ag]")
        chain.append(f"[1:a]atrim=start={fs},asetpts=PTS-STARTPTS,aresample=48000[af]")
        chain.append("[ag][af]amix=inputs=2:duration=shortest:normalize=0,"
                     "dynaudnorm=f=200[aout]")

    main = str(work_p / f"main_{idx}.mp4")
    run_ffmpeg([
        "-i", gameplay, "-i", facecam,
        "-filter_complex", ";".join(chain),
        "-map", f"[{vlabel}]", "-map", "[aout]",
        "-r", str(cfg.fps), "-t", f"{out_dur}",
        *_enc(cfg.crf, cfg.preset), main,
    ], cwd=work)

    if cfg.hit_flash:
        prog(0.74, f"{tag}: marking big hits…")
        main = _overlay_hits(main, str(work_p / f"mainfx_{idx}.mp4"), work, cfg, out_dur)

    body = main
    replay_times: list[float] = []
    # manual slow-mo: top-right two-up instant-replay box for this round's marked
    # windows (the main keeps playing live underneath). Supersedes the legacy
    # full-screen replay when present.
    my_slow = cfg.slowmo_regions[idx] if idx < len(cfg.slowmo_regions) else None
    if my_slow:
        prog(0.82, f"{tag}: slow-mo replay box…")
        sb = str(work_p / f"slowbody_{idx}.mp4")
        out_dur = build_slowmo_replays(main, gameplay, facecam, gs, fs, out_dur,
                                       my_slow, work, sb, cfg, idx,
                                       default_speed=cfg.slowmo_speed)
        body = sb
    elif cfg.replays:
        impacts = list(manual_times)
        if cfg.auto_replays > 0:
            prog(0.78, f"{tag}: finding big hits…")
            impacts += detect_impacts(main, work, cfg.auto_replays)
        valid: list[float] = []
        for t in sorted(impacts):
            if 0.3 < t < out_dur - 0.3 and all(abs(t - v) >= 3.0 for v in valid):
                valid.append(t)
        replay_times = valid
        if replay_times:
            font = _ensure_font(work)
            enc = _enc(cfg.crf, cfg.preset)
            paths = []
            for k, t in enumerate(replay_times):
                rp = str(work_p / f"replay_{idx}_{k}.mp4")
                build_replay_clip(main, t, rp, work, font, cfg.out_w, cfg.out_h,
                                  cfg.fps, enc, out_dur, smooth=cfg.replay_smooth,
                                  whoosh_gain=cfg.whoosh_gain,
                                  impact_gain=cfg.impact_gain)
                paths.append(rp)
            body = str(work_p / f"body_{idx}.mp4")
            assemble_body(main, out_dur, replay_times, paths, body, work,
                          cfg.out_w, cfg.out_h, cfg.fps, enc)

    return {"body": body, "out_dur": out_dur,
            "body_dur": probe(body).duration,
            "offset": s.offset_seconds, "confidence": s.confidence,
            "srt": (str(work_p / f"subs_{idx}.srt") if make_subs else None),
            "replays": [round(t, 1) for t in replay_times]}


def _mix_music(final: str, cfg: RenderConfig, work: str, out_p: Path) -> str:
    """Mix the music bed UNDER the finished video's audio. Music is looped to
    cover the full length, faded in/out, and (if music_duck) sidechain-compressed
    so it dips whenever the commentary/game audio is loud. Returns the new path."""
    dur = probe(final).duration
    vol = max(0.0, min(1.0, cfg.music_volume))
    fout = max(0.0, dur - 2.0)
    music_pre = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,"
                 f"volume={vol:.3f},afade=t=in:st=0:d=1.0,"
                 f"afade=t=out:st={fout:.3f}:d=2.0")
    if cfg.music_duck:
        chain = (
            "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[v1][v2];"
            f"{music_pre}[mus];"
            "[mus][v2]sidechaincompress=threshold=0.04:ratio=8:attack=15:release=350[mduck];"
            "[v1][mduck]amix=inputs=2:duration=first:normalize=0[aout]"
        )
    else:
        chain = (
            "[0:a]aresample=48000,aformat=channel_layouts=stereo[v1];"
            f"{music_pre}[mus];"
            "[v1][mus]amix=inputs=2:duration=first:normalize=0[aout]"
        )
    dst = str(out_p / "final_music.mp4")
    run_ffmpeg([
        "-i", final, "-stream_loop", "-1", "-i", cfg.music_path,
        "-filter_complex", chain,
        "-map", "0:v", "-c:v", "copy", "-map", "[aout]",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{dur}", "-shortest",
        "-movflags", "+faststart", dst,
    ], cwd=work)
    shutil.move(dst, final)
    return final


def _atempo_chain(spd: float) -> str:
    """ffmpeg `atempo` only accepts 0.5..2.0 per stage, so chain stages to reach
    an arbitrary tempo factor (audio plays at `spd`× speed, matching the video)."""
    spd = max(0.02, min(100.0, spd))
    parts, t = [], spd
    while t < 0.5 - 1e-6:
        parts.append("atempo=0.5"); t /= 0.5
    while t > 2.0 + 1e-6:
        parts.append("atempo=2.0"); t /= 2.0
    parts.append(f"atempo={t:.4f}")
    return ",".join(parts)


def apply_slowmo(src: str, regions: list, work: str, dst: str,
                 ow: int, oh: int, fps: int, enc: list,
                 slow_speed: float = 0.35, ease: float = 0.45) -> float:
    """Retime `src` so each {start,end} region (seconds, on src's timeline) plays
    in slow motion — easing in and out — while everything else stays full speed.
    Video AND audio slow together, and because it's the already-composited frame,
    the main view and the PiP corner slow in perfect sync. Returns the new
    duration (copies src through unchanged if no usable region)."""
    D = probe(src).duration
    regs = []
    for r in regions:
        if isinstance(r, dict):
            a, b = float(r.get("start", 0)), float(r.get("end", 0))
            spd = float(r.get("speed", slow_speed))
        else:
            a, b, spd = float(r[0]), float(r[1]), slow_speed
        a, b = max(0.0, min(a, b)), min(D, max(a, b))
        if b - a > 0.15:
            regs.append((a, b, max(0.05, min(0.95, spd))))
    regs.sort()
    merged: list = []
    for a, b, spd in regs:                          # collapse overlaps/touching
        if merged and a < merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b), min(merged[-1][2], spd))
        else:
            merged.append((a, b, spd))
    if not merged:
        shutil.copyfile(src, dst); return D

    pieces, cur = [], 0.0
    for a, b, slow in merged:
        mid = (1.0 + slow) / 2.0                    # one quick ramp step each side
        if a > cur + 0.02:
            pieces.append((cur, a, 1.0))
        e = min(ease, (b - a) / 3.0)
        pieces.append((a, a + e, mid))             # ease in
        pieces.append((a + e, b - e, slow))        # deep slow-mo
        pieces.append((b - e, b, mid))             # ease out
        cur = b
    if cur < D - 0.02:
        pieces.append((cur, D, 1.0))
    pieces = [(s, e, spd) for (s, e, spd) in pieces if e - s > 0.02]

    parts = []
    for k, (s, e, spd) in enumerate(pieces):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=(PTS-STARTPTS)/{spd:.4f},"
            f"scale={ow}:{oh},setsar=1,fps={fps},format=yuv420p[sv{k}]")
        parts.append(
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,"
            f"{_atempo_chain(spd)},aresample=48000,"
            f"aformat=channel_layouts=stereo[sa{k}]")
    cc = "".join(f"[sv{k}][sa{k}]" for k in range(len(pieces)))
    cc += f"concat=n={len(pieces)}:v=1:a=1[v][a]"
    run_ffmpeg([
        "-i", src, "-filter_complex", ";".join(parts) + ";" + cc,
        "-map", "[v]", "-map", "[a]", "-r", str(fps), *enc, dst,
    ], cwd=work)
    return probe(dst).duration


def build_slowmo_replays(main: str, gameplay: str, cam: str, gs: float, fs: float,
                         out_dur: float, regions: list, work: str, dst: str,
                         cfg: RenderConfig, idx: int,
                         default_speed: float = 0.35) -> float:
    """Broadcast-style instant replay. For each marked region [start,end] on the
    composite timeline (the knockdown window), build a TOP-RIGHT inset box that is a
    two-up — gameplay | cam, equal halves — of that window, SLOWED to the region
    speed, and overlay it on `main` starting at the knockdown. The main keeps playing
    live at 100% underneath (its last frame is held if a replay runs past the end).
    `gs`/`fs` map composite time t -> gameplay time gs+t and cam time fs+t.
    Returns the new body duration."""
    OW, OH = cfg.out_w, cfg.out_h
    fps = cfg.fps
    font = _ensure_font(work)
    enc = _enc(cfg.crf, cfg.preset)

    regs = []
    for r in regions:
        a = float(r.get("start", 0)) if isinstance(r, dict) else float(r[0])
        b = float(r.get("end", 0)) if isinstance(r, dict) else float(r[1])
        spd = float(r.get("speed", default_speed)) if isinstance(r, dict) else default_speed
        spd = max(0.05, min(0.95, spd))
        a, b = max(0.0, min(a, b)), min(out_dur, max(a, b))
        if b - a > 0.2:
            regs.append((a, b, spd))
    regs.sort()
    if not regs:
        shutil.copyfile(main, dst)
        return probe(main).duration

    # top-right box: ~40% of frame width, two equal 16:9 panels
    margin = max(8, OW // 60)
    panel_w = (int(OW * 0.20) // 2) * 2
    panel_h = (int(panel_w * OH / OW) // 2) * 2
    box_w, box_h = panel_w * 2, panel_h
    box_x, box_y = OW - box_w - margin, margin

    boxes = []                                   # (path, appear_at, dur)
    for k, (a, b, spd) in enumerate(regs):
        bp = str(Path(work) / f"slowbox_{idx}_{k}.mp4")
        pan = (f"scale={panel_w}:{panel_h}:force_original_aspect_ratio=increase,"
               f"crop={panel_w}:{panel_h},setsar=1,fps={fps}")
        vfg = (
            f"[0:v]trim=start={gs + a:.3f}:end={gs + b:.3f},"
            f"setpts=(PTS-STARTPTS)/{spd:.4f},{pan}[gp];"
            f"[1:v]trim=start={fs + a:.3f}:end={fs + b:.3f},"
            f"setpts=(PTS-STARTPTS)/{spd:.4f},{pan}[cm];"
            f"[gp][cm]hstack=inputs=2[bx];"
            f"[bx]drawbox=x=0:y=0:w={box_w}:h={box_h}:color=0xE23B3B:t=4,"
            f"drawbox=x={panel_w - 1}:y=0:w=2:h={box_h}:color=0xE23B3B:t=fill,"
            f"drawbox=x=0:y=0:w={box_w}:h=24:color=0x000000@0.55:t=fill,"
            f"drawtext=fontfile={font}:text='REPLAY {int(round(spd * 100))}%':"
            f"x=8:y=4:fontcolor=white:fontsize=17,format=yuv420p[outv]"
        )
        run_ffmpeg(["-i", gameplay, "-i", cam, "-filter_complex", vfg,
                    "-map", "[outv]", "-an", "-r", str(fps), *enc, bp], cwd=work)
        boxes.append((bp, b, probe(bp).duration))

    # the main must run long enough for the last replay to finish
    base_dur = probe(main).duration
    need = max(bt + bd for (_, bt, bd) in boxes)
    ext = max(0.0, need - base_dur + 0.05)

    inputs = ["-i", main]
    for bp, _, _ in boxes:
        inputs += ["-i", bp]
    parts, cur, amap = [], "[0:v]", "0:a"
    if ext > 0.05:                               # freeze the last frame + pad silence
        parts.append(f"[0:v]tpad=stop_mode=clone:stop_duration={ext:.3f}[mv]")
        parts.append(f"[0:a]apad=pad_dur={ext:.3f}[ma]")
        cur, amap = "[mv]", "[ma]"
    for k, (bp, bt, bd) in enumerate(boxes):
        parts.append(f"[{k + 1}:v]setpts=PTS-STARTPTS+{bt:.3f}/TB[bs{k}]")
        nxt = f"[ov{k}]"
        parts.append(f"{cur}[bs{k}]overlay=x={box_x}:y={box_y}:"
                     f"eof_action=pass:shortest=0{nxt}")
        cur = nxt
    run_ffmpeg([*inputs, "-filter_complex", ";".join(parts),
                "-map", cur, "-map", amap, "-r", str(fps), *enc, dst], cwd=work)
    return probe(dst).duration


def export_director_cut(cam_a: str, cam_b: str, off_a: float, off_b: float,
                        cuts: list, out: str, work: str,
                        out_w: int = 1920, out_h: int = 1080, fps: int = 30,
                        crf: int = 20, preset: str = "medium") -> str:
    """Standalone FULL-SCREEN single-feed export that cuts between two camera angles
    (no gameplay, no PiP) at the director's switch points — a clean multicam edit to
    hand to another creator. `cuts` = [{"t": gameplay_seconds, "cam": 0|1}] (0=Cam A,
    1=Cam B); each cam's time = gameplayTime − offX. Spans the aligned overlap of both
    cams (full clip, aligned start). Audio stays on Cam A the whole way for continuity."""
    work_p = Path(work)
    work_p.mkdir(parents=True, exist_ok=True)
    ia, ib = probe(cam_a), probe(cam_b)
    t0 = max(off_a, off_b, 0.0)                     # both cams present from here (gameplay time)
    t1 = min(off_a + ia.duration, off_b + ib.duration)
    if t1 - t0 < 0.3:
        raise RuntimeError("the two cameras barely overlap in time — re-check the sync")
    # tile [t0, t1] into (start, end, cam) spans from the switch points (Cam A is default)
    pts = sorted(({"t": float(c["t"]), "cam": (1 if int(c.get("cam", 0)) else 0)}
                  for c in (cuts or [])), key=lambda c: c["t"])
    spans, cur_cam, cur = [], 0, t0
    for c in pts:
        ct = min(max(c["t"], t0), t1)
        if ct > cur + 0.05:
            spans.append((cur, ct, cur_cam)); cur = ct
        cur_cam = c["cam"]
    if cur < t1 - 0.05:
        spans.append((cur, t1, cur_cam))
    if not spans:
        spans = [(t0, t1, 0)]

    fit = (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
           f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p")
    parts, labels = [], []
    for i, (s, e, cam) in enumerate(spans):
        src, off = (0, off_a) if cam == 0 else (1, off_b)
        cs, ce = max(0.0, s - off), max(0.0, e - off)
        parts.append(f"[{src}:v]trim=start={cs:.3f}:end={ce:.3f},setpts=PTS-STARTPTS,{fit}[v{i}]")
        labels.append(f"[v{i}]")
    parts.append("".join(labels) + f"concat=n={len(spans)}:v=1:a=0[outv]")
    # consistent audio across the whole aligned window (Cam A, else Cam B, else silent)
    if ia.has_audio:
        parts.append(f"[0:a]atrim=start={max(0.0, t0 - off_a):.3f}:end={max(0.0, t1 - off_a):.3f},"
                     f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[outa]")
        amap = "[outa]"
    elif ib.has_audio:
        parts.append(f"[1:a]atrim=start={max(0.0, t0 - off_b):.3f}:end={max(0.0, t1 - off_b):.3f},"
                     f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo[outa]")
        amap = "[outa]"
    else:
        parts.append("anullsrc=r=48000:cl=stereo[outa]")
        amap = "[outa]"
    run_ffmpeg(["-i", cam_a, "-i", cam_b, "-filter_complex", ";".join(parts),
                "-map", "[outv]", "-map", amap, "-t", f"{t1 - t0:.3f}",
                "-r", str(fps), *_enc(crf, preset), out], cwd=work)
    return out


def export_director_multi(rounds: list, out: str, work: str,
                          out_w: int = 1920, out_h: int = 1080, fps: int = 30,
                          crf: int = 20, preset: str = "medium",
                          gap_seconds: float = 3.0) -> str:
    """Build the shareable single-feed for EACH round (cut between the two cams) and join
    them with `gap_seconds` of BLACK between rounds, so the recipient sees where each round
    ends. `rounds` = [{cam_a, cam_b, off_a, off_b, cuts}, ...]."""
    work_p = Path(work)
    work_p.mkdir(parents=True, exist_ok=True)
    enc = _enc(crf, preset)
    round_paths = []
    for i, rd in enumerate(rounds):
        rp = str(work_p / f"dround_{i}.mp4")
        export_director_cut(rd["cam_a"], rd["cam_b"], float(rd.get("off_a", 0) or 0),
                            float(rd.get("off_b", 0) or 0), rd.get("cuts", []), rp, work,
                            out_w, out_h, fps, crf, preset)
        round_paths.append(rp)
    if not round_paths:
        raise RuntimeError("no synced rounds to export")
    if len(round_paths) == 1:
        shutil.copyfile(round_paths[0], out)
        return out
    # one black separator clip, encoded to match the rounds so the concat is seamless
    black = str(work_p / "round_gap.mp4")
    run_ffmpeg(["-f", "lavfi", "-i",
                f"color=c=black:s={out_w}x{out_h}:d={gap_seconds}:r={fps}",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-shortest", *enc, black], cwd=work)
    seq = []
    for i, rp in enumerate(round_paths):
        if i > 0:
            seq.append(black)          # black BETWEEN rounds (not before the first)
        seq.append(rp)
    listf = work_p / "dconcat.txt"
    listf.write_text("".join(f"file '{Path(p).name}'\n" for p in seq), encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listf), *enc, out], cwd=work)
    return out


def render_multi(gameplays: list[str], facecams: list[str], out_dir: str,
                 work: str, cfg: RenderConfig, progress: PROGRESS) -> dict:
    """Sync N (gameplay, facecam) pairs in order and stitch them together with
    bell round-card transitions, intro and outro."""
    work_p = Path(work)
    out_p = Path(out_dir)
    work_p.mkdir(parents=True, exist_ok=True)
    out_p.mkdir(parents=True, exist_ok=True)

    n = min(len(gameplays), len(facecams))
    if n == 0:
        raise RuntimeError("No clips provided.")
    progress(2, f"Preparing {n} round(s)…")   # show life immediately (probe/sync can take a bit)
    manual = parse_timestamps(cfg.replay_times) if n == 1 else []

    infos = []
    for i in range(n):
        lo, hi = 4 + int(70 * i / n), 4 + int(70 * (i + 1) / n)
        # PER-ROUND manual sync from the clapperboard round tabs; fall back to the
        # legacy single-pair values for round 0 (None for a round -> auto-detect it).
        moff = _round_off(cfg.manual_offsets, i)
        if moff is None and i == 0:
            moff = cfg.manual_offset
        rt = cfg.round_trims[i] if i < len(cfg.round_trims) else None
        if rt:
            tstart = float(rt.get("in", 0.0) or 0.0)
            tend = float(rt.get("out")) if rt.get("out") else None
        elif i == 0 and not cfg.round_trims:
            tstart, tend = cfg.trim_start, cfg.trim_end
        else:
            tstart, tend = 0.0, None
        camb = cfg.cam_b_paths[i] if i < len(cfg.cam_b_paths) else ""
        if camb:
            # PER-ROUND multicam: gameplay (always bg) + cam A (facecam) + cam B,
            # PiP switches between the two cams at this round's cuts.
            offA = _round_off(cfg.cam_a_offsets, i)
            if offA is None:
                offA = (cfg.manual_offset if (i == 0 and cfg.manual_offset is not None)
                        else compute_sync(gameplays[i], facecams[i], work).offset_seconds)
            offB = _round_off(cfg.cam_b_offsets, i)
            if offB is None:
                offB = compute_sync(gameplays[i], camb, work).offset_seconds
            angles = [{"path": facecams[i], "offset": offA},
                      {"path": camb, "offset": offB}]
            rcuts = cfg.round_cuts[i] if i < len(cfg.round_cuts) else []
            cuts = [{"t": float(c["t"]), "angle": int(c.get("cam", c.get("angle", 0)))}
                    for c in rcuts]
            infos.append(_build_multicam_segment(gameplays[i], angles, cuts, cfg, work,
                                                 progress, i, lo, hi,
                                                 trim_start=tstart, trim_end=tend))
        elif i == 0 and len(cfg.multicam_angles) >= 2:
            # legacy round-0-only multicam (single global angle set)
            infos.append(_build_multicam_segment(gameplays[i], cfg.multicam_angles,
                                                 cfg.multicam_cuts, cfg, work, progress,
                                                 i, lo, hi, trim_start=tstart, trim_end=tend))
        else:
            infos.append(_build_segment(gameplays[i], facecams[i], cfg, work,
                                        progress, i, lo, hi, manual, moff,
                                        trim_start=tstart, trim_end=tend))

    FADE = 0.4
    do_trans = cfg.transitions and n > 1
    highlight_trans = (cfg.transition_style or "card").lower() == "highlight"
    trans = []
    if do_trans:
        for i in range(1, n):
            tp = str(work_p / f"trans_{i}.mp4")
            label = f"{cfg.transition_label} {i + 1}".strip()
            if highlight_trans:
                # cinematic highlight break, pulled from the round that just finished
                progress(76, f"Highlight break → {label}…")
                try:
                    import round_break
                    round_break.render_round_break(
                        tp, work, cfg, label, infos[i - 1]["body"])
                except Exception:  # noqa: BLE001 — never let a transition kill the render
                    traceback.print_exc()
                    _transition_card(tp, work, cfg, label, cfg.transition_seconds)
            else:
                progress(76, f"Bell transition → {label}…")
                _transition_card(tp, work, cfg, label, cfg.transition_seconds)
            trans.append(tp)

    # ordered clip list: intro? + seg0 + (trans + seg)* + outro?
    clips: list = []   # (path, kind) where kind == "plain" or ("seg", i)
    marks: list = []   # (index into clips, chapter label) — start of each chapter
    round_word = (cfg.transition_label or "Round").strip().title() or "Round"
    if cfg.intro:
        progress(80, "Intro card…")
        ip = str(work_p / "intro.mp4")
        _title_card(ip, work, cfg, cfg.intro_seconds, cfg.title, cfg.intro_subtitle)
        clips.append((ip, "plain"))
        marks.append((len(clips) - 1, "Intro"))
    for i in range(n):
        if i > 0 and do_trans:
            clips.append((trans[i - 1], "plain"))
            marks.append((len(clips) - 1, f"{round_word} {i + 1}"))
        clips.append((infos[i]["body"], ("seg", i)))
        if i == 0 or not do_trans:
            marks.append((len(clips) - 1, f"{round_word} {i + 1}"))
    if cfg.outro:
        progress(86, "Outro card…")
        op = str(work_p / "outro.mp4")
        _title_card(op, work, cfg, cfg.outro_seconds, cfg.outro_title, cfg.outro_subtitle)
        clips.append((op, "plain"))
        marks.append((len(clips) - 1, "Outro"))

    final = str(out_p / "final.mp4")
    if len(clips) == 1:
        shutil.copyfile(clips[0][0], final)
    else:
        progress(92, "Stitching clips + transitions…")
        inputs, parts, order = [], [], []
        for j, (path, kind) in enumerate(clips):
            inputs += ["-i", path]
            v = (f"[{j}:v]scale={cfg.out_w}:{cfg.out_h},setsar=1,"
                 f"fps={cfg.fps},format=yuv420p")
            a = f"[{j}:a]aresample=48000,aformat=channel_layouts=stereo"
            if isinstance(kind, tuple) and do_trans:
                i = kind[1]
                d = infos[i]["body_dur"]
                if i > 0:
                    v += f",fade=t=in:st=0:d={FADE}"
                    a += f",afade=t=in:st=0:d={FADE}"
                if i < n - 1:
                    v += f",fade=t=out:st={max(0, d - FADE):.3f}:d={FADE}"
                    a += f",afade=t=out:st={max(0, d - FADE):.3f}:d={FADE}"
            parts.append(v + f"[v{j}]")
            parts.append(a + f"[a{j}]")
            order.append(j)
        cc = "".join(f"[v{j}][a{j}]" for j in order)
        cc += f"concat=n={len(order)}:v=1:a=1[v][a]"
        run_ffmpeg([
            *inputs, "-filter_complex", ";".join(parts) + ";" + cc,
            "-map", "[v]", "-map", "[a]",
            *_enc(cfg.crf, cfg.preset), final,
        ], cwd=work)

    if cfg.music_path and Path(cfg.music_path).exists():
        progress(95, "Adding music bed…")
        final = _mix_music(final, cfg, work, out_p)

    # cumulative start time of every clip on the final timeline -> chapter marks
    def _clip_dur(path, kind):
        if isinstance(kind, tuple):
            return infos[kind[1]]["body_dur"]
        return probe(path).duration

    starts, t = [], 0.0
    for path, kind in clips:
        starts.append(t)
        t += _clip_dur(path, kind)
    chapters_raw = [(starts[ci], label) for ci, label in marks]

    result = {
        "final": final,
        "duration": round(sum(x["out_dur"] for x in infos), 1),
        "offset": infos[0]["offset"], "confidence": infos[0]["confidence"],
        "clips": n,
        "segments": [{"offset": round(x["offset"], 2),
                      "confidence": round(x["confidence"], 2),
                      "duration": round(x["out_dur"], 1)} for x in infos],
        "replays": [t for x in infos for t in x["replays"]],
    }
    if n == 1 and infos[0]["srt"] and Path(infos[0]["srt"]).exists():
        published = str(out_p / "final.srt")
        shutil.copyfile(infos[0]["srt"], published)
        result["subtitles"] = published

    # YouTube title / description (with chapters) / tags
    try:
        import metadata as meta_mod
        srt_paths = [x["srt"] for x in infos if x.get("srt")]
        meta = meta_mod.build(cfg, result, chapters_raw, srt_paths)
        (out_p / "youtube.txt").write_text(
            f"TITLE\n{meta['title']}\n\nDESCRIPTION\n{meta['description']}\n\n"
            f"TAGS\n{', '.join(meta['tags'])}\n", encoding="utf-8")
        result["metadata"] = meta
    except Exception as e:  # noqa: BLE001  — metadata is a nicety, never fatal
        progress(99, f"(Skipped YouTube metadata: {type(e).__name__})")

    progress(100, "Done.")
    return result


def render(gameplay: str, facecam: str, out_dir: str, work: str,
           cfg: RenderConfig, progress: PROGRESS) -> dict:
    """Single-pair render (backward-compatible wrapper around render_multi)."""
    return render_multi([gameplay], [facecam], out_dir, work, cfg, progress)
