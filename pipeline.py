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
    # picture-in-picture
    pip_scale: float = 0.26          # facecam width as fraction of frame
    pip_position: str = "br"          # br, bl, tr, tl
    pip_margin: int = 40
    pip_border: int = 4
    pip_border_color: str = "white"
    # audio
    audio_mode: str = "mix"           # mix | gameplay | facecam
    # subtitles
    make_subtitles: bool = True
    burn_subtitles: bool = True
    whisper_model: str = field(
        default_factory=lambda: os.environ.get("FIGHTSYNC_WHISPER_MODEL", "base")
    )
    # intro / outro
    intro: bool = True
    outro: bool = True
    title: str = "The Thrill of the Fight 2"
    intro_subtitle: str = ""
    outro_title: str = "Thanks for watching"
    outro_subtitle: str = "Like & Subscribe"
    intro_seconds: float = 2.6
    outro_seconds: float = 4.0
    # manual sync override from the preview nudge (seconds; + = gameplay leads).
    # None -> auto-detect / start-align as usual.
    manual_offset: Optional[float] = None
    # round transitions between multiple clips
    transitions: bool = True
    transition_label: str = "ROUND"     # -> "ROUND 2", "ROUND 3", …
    transition_seconds: float = 2.4
    bell: bool = True
    # cinematic slow-mo replays
    replays: bool = False
    replay_times: str = ""            # "1:23, 4:05" or seconds, comma-separated
    auto_replays: int = 0             # auto-detect this many biggest hits
    replay_smooth: bool = False       # motion-interpolate (fluid but slower)
    whoosh_gain: float = 0.40
    impact_gain: float = 0.70
    # encode
    crf: int = 20
    preset: str = "medium"


_POS = {
    "br": "W-w-{m}:H-h-{m}",
    "bl": "{m}:H-h-{m}",
    "tr": "W-w-{m}:{m}",
    "tl": "{m}:{m}",
}


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
        f":fontcolor=white:fontsize=(h/11):x=(w-text_w)/2:y=(h/2)-text_h"
        f":alpha='{alpha}'"
    )
    small_dt = (
        f"drawtext=fontfile={font}:textfile='{small_f.name}'"
        f":fontcolor=0x9AA4B2:fontsize=(h/26):x=(w-text_w)/2:y=(h/2)+text_h*0.5"
        f":alpha='{alpha}'"
    )
    accent = (
        "drawbox=x=(w-iw*0.16)/2:y=(h/2)+ih*0.02:w=iw*0.16:h=4"
        ":color=0xE23B3B:t=fill:enable='gte(t,0.2)'"
    )
    vf = f"format=yuv420p,{big_dt},{small_dt},{accent}"

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
    vf = f"format=yuv420p,{dt},{accent}"

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


def _build_segment(gameplay: str, facecam: str, cfg: RenderConfig, work: str,
                   progress: PROGRESS, idx: int, lo: int, hi: int,
                   manual_times: list[float],
                   manual_offset: Optional[float] = None) -> dict:
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
    if mode == "manual":
        prog(0.3, f"{tag}: manual offset {manual_offset:+.2f}s, {out_dur:.0f}s")
    elif mode == "start":
        prog(0.3, f"{tag}: no shared-audio lock — aligned from the start "
                  f"({out_dur:.0f}s)")
    else:
        prog(0.3, f"{tag}: offset {s.offset_seconds:+.2f}s, {out_dur:.0f}s synced")

    make_subs = cfg.make_subtitles and f.has_audio
    burn_subs = cfg.burn_subtitles and make_subs
    srt_rel = f"subs_{idx}.srt"
    srt_path = str(work_p / srt_rel)
    if make_subs:
        sub_wav = str(work_p / "facecam_sync.wav")
        subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{s.b_start}", "-i", facecam, "-t", f"{out_dur}",
             "-vn", "-ac", "1", "-ar", str(SR), sub_wav],
            check=True,
        )
        _transcribe(sub_wav, srt_path, cfg.whisper_model,
                    lambda p, m: prog(0.3 + 0.2 * p / 100, f"{tag}: {m}"))
        if not Path(srt_path).exists() or Path(srt_path).stat().st_size == 0:
            make_subs = burn_subs = False
    else:
        make_subs = burn_subs = False

    prog(0.6, f"{tag}: compositing…")
    OW, OH = cfg.out_w, cfg.out_h

    if cfg.layout == "sbs":
        # side-by-side, both halves the same size
        half = (OW // 2) // 2 * 2
        chain = [
            f"[0:v]trim=start={gs},setpts=PTS-STARTPTS,"
            f"scale={half}:{OH}:force_original_aspect_ratio=decrease,"
            f"pad={half}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[L]",
            f"[1:v]trim=start={fs},setpts=PTS-STARTPTS,"
            f"scale={half}:{OH}:force_original_aspect_ratio=decrease,"
            f"pad={half}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[R]",
            "[L][R]hstack=inputs=2[vov]",
        ]
    else:
        pw = round(OW * cfg.pip_scale / 2) * 2
        bw = cfg.pip_border
        pos = _POS[cfg.pip_position].format(m=cfg.pip_margin)
        chain = [
            f"[0:v]trim=start={gs},setpts=PTS-STARTPTS,"
            f"scale={OW}:{OH}:force_original_aspect_ratio=decrease,"
            f"pad={OW}:{OH}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[bg]",
            f"[1:v]trim=start={fs},setpts=PTS-STARTPTS,scale={pw}:-2,"
            f"pad=iw+{2*bw}:ih+{2*bw}:{bw}:{bw}:color={cfg.pip_border_color}[pip]",
            f"[bg][pip]overlay={pos}:shortest=1[vov]",
        ]
    vlabel = "vov"
    if burn_subs:
        style = ("FontName=Arial,Fontsize=22,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
                 "MarginV=60")
        chain.append(f"[vov]subtitles={srt_rel}:force_style='{style}'[vsub]")
        vlabel = "vsub"

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

    body = main
    replay_times: list[float] = []
    if cfg.replays:
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
            "srt": srt_path if make_subs else None,
            "replays": [round(t, 1) for t in replay_times]}


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
    manual = parse_timestamps(cfg.replay_times) if n == 1 else []

    infos = []
    for i in range(n):
        lo, hi = 4 + int(70 * i / n), 4 + int(70 * (i + 1) / n)
        # a manual offset from the preview applies to the first (previewed) pair
        moff = cfg.manual_offset if i == 0 else None
        infos.append(_build_segment(gameplays[i], facecams[i], cfg, work,
                                    progress, i, lo, hi, manual, moff))

    FADE = 0.4
    do_trans = cfg.transitions and n > 1
    trans = []
    if do_trans:
        for i in range(1, n):
            progress(76, f"Bell transition → {cfg.transition_label} {i + 1}…")
            tp = str(work_p / f"trans_{i}.mp4")
            _transition_card(tp, work, cfg,
                             f"{cfg.transition_label} {i + 1}".strip(),
                             cfg.transition_seconds)
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
