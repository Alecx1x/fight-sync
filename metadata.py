"""Build YouTube-ready title, description (with chapters) and tags from a
finished render — no external API, all derived from the render result + the
auto-generated subtitles.

YouTube chapter rules we respect so they actually activate:
  * the first chapter must start at 0:00
  * there must be at least 3 chapters
  * each chapter must be at least 10 seconds long
If those can't be met (e.g. one short clip) we still emit the timestamps as
plain text — harmless, YouTube just won't turn them into clickable chapters.
"""
from __future__ import annotations

import re
from pathlib import Path

MIN_CHAPTER_GAP = 10.0   # seconds — YouTube's minimum chapter length

# very small stop-word list for pulling keyword tags out of the transcript
_STOP = set("""
a an the and or but if then else of to in on at by for with from into over
this that these those it its is are was were be been being am as so not no yes
i you he she we they me him her them my your his our their mine yours ours
do does did done have has had will would shall should can could may might must
just like get got go going gonna want need know think really very much more
most some any all out up down off about here there what when where why how who
oh ok okay yeah yep nah uh um hmm gonna wanna lemme right left one two three
""".split())


def fmt_ts(t: float) -> str:
    """Seconds -> M:SS or H:MM:SS (YouTube chapter format)."""
    t = max(0, int(round(t)))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _valid_chapters(raw: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Force the first mark to 0:00 and drop any that fall within 10s of the
    previously kept one, so the survivors satisfy YouTube's chapter rules."""
    if not raw:
        return []
    out: list[tuple[float, str]] = [(0.0, raw[0][1])]
    for start, label in raw[1:]:
        if start - out[-1][0] >= MIN_CHAPTER_GAP:
            out.append((start, label))
        elif out[-1][1] == "Intro":
            # the real content starts right after a tiny intro card — let the
            # 0:00 chapter carry the content label instead of dropping it
            out[-1] = (out[-1][0], label)
    return out


def _srt_text(srt_paths: list[str]) -> str:
    """Flatten the spoken text out of one or more .srt files."""
    words: list[str] = []
    for p in srt_paths:
        if not p or not Path(p).exists():
            continue
        for line in Path(p).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.isdigit() or "-->" in line:
                continue
            words.append(line)
    return " ".join(words)


def _keywords(text: str, n: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for w in re.findall(r"[A-Za-z']{4,}", text.lower()):
        w = w.strip("'")
        if w in _STOP or len(w) < 4:
            continue
        counts[w] = counts.get(w, 0) + 1
    ranked = sorted(counts, key=lambda w: (-counts[w], w))
    return ranked[:n]


def build(cfg, result: dict, chapters_raw: list[tuple[float, str]],
          srt_paths: list[str]) -> dict:
    """Return {title, description, tags, chapters} for the finished video."""
    title = (cfg.title or "The Thrill of the Fight 2").strip()
    rounds = result.get("clips", 1)
    if rounds > 1 and "round" not in title.lower():
        title = f"{title} | {rounds} Rounds"

    chapters = _valid_chapters(chapters_raw)
    chapter_lines = [f"{fmt_ts(t)} {label}" for t, label in chapters]

    transcript = _srt_text(srt_paths)
    kw = _keywords(transcript)

    # base tag set + any standout words from what was actually said
    base_tags = [
        "Thrill of the Fight 2", "Thrill of the Fight", "VR Boxing", "VR Boxing Game",
        "Boxing", "Quest 3", "VR Fitness", "Boxing Workout", "VR Gameplay",
    ]
    tags = base_tags + [w for w in kw if w not in {t.lower() for t in base_tags}]
    tags = tags[:18]

    hashtags = "#ThrillOfTheFight2 #VRBoxing #Boxing #Quest3 #VRFitness"

    parts = [title, ""]
    credit = (getattr(cfg, "spectator_credit", "") or "").strip()
    if credit:                                   # source-credit (VOD + channel) goes at the very top
        parts.append(credit)
        parts.append("")
    if transcript:
        # a light, honest one-liner — no AI claims, just framing
        parts.append("Round-by-round VR boxing in Thrill of the Fight 2, "
                     "with live facecam reactions.")
        parts.append("")
    if len(chapter_lines) >= 3:
        parts.append("⏱️ Chapters")
        parts.extend(chapter_lines)
        parts.append("")
    parts.append("🥊 Recorded in Thrill of the Fight 2 (VR boxing).")
    parts.append("👊 Like & subscribe for more fights.")
    parts.append("")
    parts.append(hashtags)
    description = "\n".join(parts)

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "chapters": chapter_lines,
    }
