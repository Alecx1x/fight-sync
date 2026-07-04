"""capture.py — organize fight captures into the OneDrive "Camera Roll" source
folders with simple, ordered names: totf1, totf2, totf3 …

Two jobs:
  * save_totf()  — used by FightSync's mobile upload to drop a phone clip or
    gameplay clip straight into the right PC folder as the next totfN.<ext>.
  * watch()      — a daemon thread that renames new Windows Camera app captures
    (WIN_*.mp4 dropped into "cam 2 PC") to the next totfN, so PC-webcam footage is
    auto-named too. It also undoes the system "Camera Roll" folder-name override if
    Windows ever re-stamps it, so the folder keeps showing its real name.

Numbering is PER FOLDER (next = highest existing totfN + 1), so capturing in order
gives cam 1 Phone\totf3, cam 2 PC\totf3, gameplay\totf3 for the same fight.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

_OD = os.environ.get("OneDrive") or r"C:\Users\socia\OneDrive"
BASE = Path(_OD) / "Pictures" / "Camera Roll"

# stable keys → (on-disk folder, human label for the UI)
FOLDERS = {
    "cam1": (BASE / "cam 1 Phone", "cam 1 Phone (phone footage)"),
    "cam2": (BASE / "cam 2 PC", "cam 2 PC (PC webcam)"),
    "gameplay": (BASE / "gameplay", "gameplay"),
}

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi"}
_TOTF_RE = re.compile(r"^totf(\d+)$", re.IGNORECASE)
_lock = threading.Lock()             # serialize number assignment across upload + watcher


def folder_of(key: str) -> Path:
    if key not in FOLDERS:
        raise ValueError(f"unknown capture target {key!r}")
    return FOLDERS[key][0]


def next_number(folder: Path) -> int:
    n = 0
    if folder.exists():
        for p in folder.iterdir():
            m = _TOTF_RE.match(p.stem)
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def next_name(folder: Path, ext: str) -> str:
    ext = ext if ext.startswith(".") else "." + ext
    return f"totf{next_number(folder)}{ext.lower()}"


def save_totf(key: str, fileobj, original_name: str) -> dict:
    """Stream an uploaded file into the target folder as the next totfN.<ext>.
    Reserves the number with an empty placeholder under the lock, then copies the
    bytes outside the lock so big phone uploads don't block other callers."""
    folder = folder_of(key)
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(original_name or "").suffix.lower() or ".mp4"
    with _lock:
        name = next_name(folder, ext)
        dst = folder / name
        dst.touch()                  # reserve so a concurrent call/watcher skips this number
    with dst.open("wb") as out:
        shutil.copyfileobj(fileobj, out, length=1024 * 1024)
    return {"name": name, "path": str(dst), "folder": str(folder)}


def folder_info() -> list:
    """For the UI: each target's label, current video count, and the next name."""
    out = []
    for key, (folder, label) in FOLDERS.items():
        cnt = 0
        if folder.exists():
            cnt = sum(1 for p in folder.iterdir()
                      if not p.is_dir() and p.suffix.lower() in VIDEO_EXTS)
        out.append({"key": key, "label": label, "folder": str(folder),
                    "count": cnt, "next": next_name(folder, ".mp4")})
    return out


# ── background watcher (cam 2 PC) ───────────────────────────────────────────
def _fix_name_override(folder: Path) -> None:
    """If Windows re-stamped the system 'Camera Roll' display name onto the folder
    (desktop.ini → windows.storage.dll resource), rewrite it to the folder's real
    name so Explorer keeps showing e.g. 'cam 2 PC'. Leaves a user's custom name
    desktop.ini untouched. Does NOT affect where the Camera app saves."""
    ini = folder / "desktop.ini"
    try:
        if not ini.exists():
            return
        txt = ini.read_text(encoding="utf-8", errors="ignore")
        if "windows.storage.dll" in txt.lower():
            ini.write_text(
                "[.ShellClassInfo]\nLocalizedResourceName=%s\n" % folder.name,
                encoding="utf-8")
    except Exception:
        pass


def _stable(p: Path, sizes: dict) -> bool:
    """True once a file's size has stopped growing between polls (done writing)."""
    try:
        sz = p.stat().st_size
    except OSError:
        return False
    prev = sizes.get(p.name)
    sizes[p.name] = sz
    return prev is not None and prev == sz and sz > 0


def watch(poll: float = 3.0, log=lambda *_: None) -> None:
    cam2 = folder_of("cam2")
    sizes: dict = {}
    while True:
        try:
            if cam2.exists():
                _fix_name_override(cam2)
                # candidate = a finished video not already named totfN
                cands = []
                for p in list(cam2.iterdir()):
                    if p.is_dir() or p.name.lower() == "desktop.ini":
                        continue
                    if p.suffix.lower() not in VIDEO_EXTS or _TOTF_RE.match(p.stem):
                        continue
                    if _stable(p, sizes):
                        cands.append(p)
                # earliest capture → lowest totf number
                cands.sort(key=lambda q: q.stat().st_mtime if q.exists() else 0)
                for p in cands:
                    with _lock:
                        dst = cam2 / next_name(cam2, p.suffix)
                        try:
                            p.rename(dst)
                            sizes.pop(p.name, None)
                            log(f"[capture] renamed {p.name} -> {dst.name}")
                        except OSError:
                            pass
        except Exception:
            pass
        time.sleep(poll)


def start(log=lambda *_: None) -> threading.Thread:
    t = threading.Thread(target=watch, kwargs={"log": log},
                         daemon=True, name="capture-watch")
    t.start()
    return t
