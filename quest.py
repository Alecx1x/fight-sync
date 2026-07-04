"""quest.py — auto-import Meta Quest gameplay recordings when it's plugged in.

The Quest connects over USB as an MTP device (NO drive letter), so its files are
only reachable through the Windows shell namespace via Shell.Application (pywin32),
not os/shutil. When the headset is connected this polls
  This PC\\Quest 2\\Internal shared storage\\Oculus\\VideoShots
and COPIES any new gameplay clips into FightSync's gameplay folder, named totfN
(via capture.py). It copies (never deletes) so originals stay on the headset;
already-imported source files are tracked in quest_imported.json so nothing copies
twice. MTP reports Size=0, so "copy finished" is judged by the copied file's size
settling on disk.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pythoncom
import win32com.client

import capture

ROOT = Path(__file__).parent
STAGING = ROOT / "quest_staging"
SEEN_FILE = ROOT / "quest_imported.json"
VS_PATH = ["Internal shared storage", "Oculus", "VideoShots"]
COPY_FLAGS = 1556          # 4|16|512|1024 = no progress dialog, yes-to-all, no UI

_state = {"connected": False, "videoshots": 0, "imported": 0,
          "last": None, "busy": False}
_import_lock = threading.Lock()


def _shell():
    return win32com.client.Dispatch("Shell.Application")


def _child(folder, name):
    for it in folder.Items():
        if it.IsFolder and it.Name.lower() == name.lower():
            return it.GetFolder
    return None


def _device(sh):
    """The Quest/Oculus MTP device under 'This PC', or None if not plugged in."""
    pc = sh.NameSpace(17)
    for it in pc.Items():
        if it.IsFolder:
            n = it.Name.lower()
            if "quest" in n or "oculus" in n:
                return it.GetFolder
    return None


def _videoshots(sh):
    dev = _device(sh)
    if dev is None:
        return None
    cur = dev
    for seg in VS_PATH:
        cur = _child(cur, seg)
        if cur is None:
            return None
    return cur


def _load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(s):
    try:
        SEEN_FILE.write_text(json.dumps(sorted(s)), encoding="utf-8")
    except Exception:
        pass


def _is_video(name):
    return Path(name).suffix.lower() in capture.VIDEO_EXTS


def _wait_stable(before, timeout=3600):
    """Wait for CopyHere (async) to drop a new file into STAGING and finish. Returns
    the staged Path once its size has settled, else None (timeout/failure)."""
    target = None
    last = -1
    stable = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        if target is None:
            try:
                new = set(os.listdir(STAGING)) - before
            except OSError:
                new = set()
            if new:
                target = STAGING / sorted(new)[0]
            continue
        if not target.exists():
            continue
        try:
            sz = target.stat().st_size
        except OSError:
            continue
        if sz > 0 and sz == last:
            stable += 1
            if stable >= 2:
                return target
        else:
            stable = 0
            last = sz
    return None


def import_new(dest_key="gameplay", log=lambda *_: None):
    """Copy any not-yet-seen VideoShots clips into the dest folder as totfN.
    Returns the list of new totf names. Single-flighted (lock) so the poller and a
    manual trigger can't double-import."""
    if not _import_lock.acquire(blocking=False):
        return []
    pythoncom.CoInitialize()
    imported = []
    try:
        sh = _shell()
        vs = _videoshots(sh)
        if vs is None:
            return imported
        STAGING.mkdir(exist_ok=True)
        seen = _load_seen()
        all_vids = [it for it in vs.Items() if not it.IsFolder and _is_video(it.Name)]
        _state["videoshots"] = len(all_vids)
        todo = [it for it in all_vids if it.Name not in seen]
        if not todo:
            return imported
        gameplay = capture.folder_of(dest_key)
        gameplay.mkdir(parents=True, exist_ok=True)
        staging_ns = sh.NameSpace(str(STAGING))
        _state["busy"] = True
        for it in sorted(todo, key=lambda x: x.Name):   # timestamped names → chronological
            src_name = it.Name
            before = set(os.listdir(STAGING))
            try:
                staging_ns.CopyHere(it, COPY_FLAGS)
            except Exception as e:
                log(f"[quest] copy start failed {src_name}: {e}")
                continue
            staged = _wait_stable(before)
            if staged is None or not staged.exists():
                for f in (set(os.listdir(STAGING)) - before):   # clean any partial
                    try:
                        (STAGING / f).unlink()
                    except OSError:
                        pass
                log(f"[quest] copy timed out/failed: {src_name}")
                continue
            ext = Path(src_name).suffix.lower() or ".mp4"
            with capture._lock:                  # shared totf numbering for the folder
                name = capture.next_name(gameplay, ext)
                dest = gameplay / name
                try:
                    shutil.move(str(staged), str(dest))   # fast rename, same volume
                except Exception as e:
                    log(f"[quest] move failed {src_name}: {e}")
                    try:
                        staged.unlink()
                    except OSError:
                        pass
                    continue
            seen.add(src_name)
            _save_seen(seen)
            imported.append(name)
            _state["last"] = name
            log(f"[quest] imported {src_name} -> {name}")
        _state["imported"] = len(seen)
        return imported
    except Exception as e:
        log(f"[quest] import error: {e}")
        return imported
    finally:
        _state["busy"] = False
        pythoncom.CoUninitialize()
        _import_lock.release()


def status_live():
    """Fresh connection + counts for the UI (does its own COM init)."""
    pythoncom.CoInitialize()
    try:
        sh = _shell()
        vs = _videoshots(sh)
        conn = _device(sh) is not None
        cnt = 0
        if vs is not None:
            cnt = sum(1 for it in vs.Items() if not it.IsFolder and _is_video(it.Name))
        seen = _load_seen()
        _state.update(connected=conn, videoshots=cnt, imported=len(seen))
        return {"connected": conn, "videoshots": cnt, "imported": len(seen),
                "last": _state.get("last"), "busy": _state.get("busy", False)}
    except Exception as e:
        return {"connected": False, "videoshots": 0, "imported": len(_load_seen()),
                "last": _state.get("last"), "busy": False, "error": str(e)}
    finally:
        pythoncom.CoUninitialize()


def trigger_import(dest_key="gameplay", log=lambda *_: None):
    """Kick an import in the background (so an HTTP call returns immediately)."""
    threading.Thread(target=import_new, args=(dest_key, log),
                     daemon=True, name="quest-import").start()


def _poller(dest_key, log, interval):
    while True:
        try:
            conn = False
            pythoncom.CoInitialize()
            try:
                conn = _device(_shell()) is not None
            finally:
                pythoncom.CoUninitialize()
            _state["connected"] = conn
            if conn and not _state.get("busy"):
                import_new(dest_key, log=log)
        except Exception as e:
            log(f"[quest] poll error: {e}")
        time.sleep(interval)


def start_poller(dest_key="gameplay", log=lambda *_: None, interval=12):
    t = threading.Thread(target=_poller, args=(dest_key, log, interval),
                         daemon=True, name="quest-poll")
    t.start()
    return t
