"""iphone.py — import iPhone videos over USB WITHOUT the flaky File-Explorer MTP drag.

The iPhone connects as an MTP device (no drive letter), and dragging files in Explorer over
MTP silently drops them ("trusted the PC but nothing shows up"). This reads the iPhone's DCIM
through the Windows shell namespace (Shell.Application / pywin32) — the SAME reliable path the
Quest import uses — lists the most-recent videos, and copies the ones you PICK into FightSync's
Phone folder (cam 1 Phone) as totfN. Copies, never deletes; originals stay on the phone.

Unlike the Quest (whose VideoShots are all gameplay), the iPhone's camera roll is mixed, so this
does NOT auto-import — it surfaces recent videos and you tap the ones to bring over.
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
STAGING = ROOT / "iphone_staging"
COPY_FLAGS = 1556          # 4|16|512|1024 = no progress dialog, yes-to-all, no UI
DEST_KEY = "cam1"          # the "cam 1 Phone" folder
SCAN_LIMIT = 30            # most-recent videos surfaced to the picker

_import_lock = threading.Lock()
_state = {"connected": False, "busy": False, "last": None, "imported": 0}


def _shell():
    return win32com.client.Dispatch("Shell.Application")


def _child(folder, name):
    for it in folder.Items():
        if it.IsFolder and it.Name.lower() == name.lower():
            return it.GetFolder
    return None


def _device(sh):
    """The iPhone MTP device under 'This PC', or None if not plugged in/unlocked."""
    pc = sh.NameSpace(17)
    for it in pc.Items():
        if it.IsFolder:
            n = it.Name.lower()
            if "iphone" in n:
                return it.GetFolder
    return None


def _dcim(sh):
    """The iPhone's DCIM folder (under 'Internal Storage' on most iOS, else directly)."""
    dev = _device(sh)
    if dev is None:
        return None
    inner = _child(dev, "Internal Storage") or dev
    return _child(inner, "DCIM")


def _is_video(name):
    return Path(name).suffix.lower() in capture.VIDEO_EXTS


def _vinfo(it):
    name = it.Name
    disp = ""
    try:
        md = it.ModifyDate
        if md:
            disp = md.Format("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return {"name": name, "date": disp}


def _recent_videos(dcim, limit=SCAN_LIMIT):
    """Newest videos across DCIM/xxxAPPLE — scans highest folder first and stops early,
    so it's quick even on a camera roll with thousands of photos."""
    if dcim is None:
        return []
    subs = sorted((s for s in dcim.Items() if s.IsFolder), key=lambda s: s.Name, reverse=True)
    out = []
    for sub in subs:
        fol = sub.GetFolder
        vids = [it for it in fol.Items() if not it.IsFolder and _is_video(it.Name)]
        for it in sorted(vids, key=lambda x: x.Name, reverse=True):
            out.append(_vinfo(it))
            if len(out) >= limit:
                return out
    return out


def _wait_stable(before, timeout=3600):
    """Wait for the async CopyHere to drop a file into STAGING and settle (MTP reports Size=0,
    so we judge 'done' by the on-disk size holding steady)."""
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


def status_live():
    """Connection + the recent-video picker list for the UI (does its own COM init)."""
    pythoncom.CoInitialize()
    try:
        sh = _shell()
        dev = _device(sh)
        dcim = _dcim(sh) if dev is not None else None      # readable only when unlocked + trusted
        conn = dcim is not None
        vids = _recent_videos(dcim) if conn else []
        _state["connected"] = conn
        return {"connected": conn, "present": dev is not None, "count": len(vids), "videos": vids,
                "busy": _state.get("busy", False), "last": _state.get("last")}
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "count": 0, "videos": [], "busy": False, "error": str(e)}
    finally:
        pythoncom.CoUninitialize()


def import_selected(names, log=lambda *_: None):
    """Copy the picked videos (by name) into the Phone folder as totfN. Returns the new names."""
    if not _import_lock.acquire(blocking=False):
        return []
    pythoncom.CoInitialize()
    done = []
    want = set(names or [])
    try:
        if not want:
            return done
        sh = _shell()
        dcim = _dcim(sh)
        if dcim is None:
            return done
        STAGING.mkdir(exist_ok=True)
        dest = capture.folder_of(DEST_KEY)
        dest.mkdir(parents=True, exist_ok=True)
        staging_ns = sh.NameSpace(str(STAGING))
        _state["busy"] = True
        for sub in dcim.Items():
            if not sub.IsFolder or not want:
                continue
            fol = sub.GetFolder
            for it in fol.Items():
                if it.IsFolder or it.Name not in want:
                    continue
                want.discard(it.Name)
                before = set(os.listdir(STAGING))
                try:
                    staging_ns.CopyHere(it, COPY_FLAGS)
                except Exception as e:  # noqa: BLE001
                    log(f"[iphone] copy start failed {it.Name}: {e}")
                    continue
                staged = _wait_stable(before)
                if staged is None or not staged.exists():
                    for f in (set(os.listdir(STAGING)) - before):
                        try:
                            (STAGING / f).unlink()
                        except OSError:
                            pass
                    log(f"[iphone] copy timed out/failed: {it.Name}")
                    continue
                ext = Path(it.Name).suffix.lower() or ".mov"
                with capture._lock:
                    name = capture.next_name(dest, ext)
                    target = dest / name
                    try:
                        shutil.move(str(staged), str(target))
                    except Exception as e:  # noqa: BLE001
                        log(f"[iphone] move failed {it.Name}: {e}")
                        try:
                            staged.unlink()
                        except OSError:
                            pass
                        continue
                done.append(name)
                _state["last"] = name
                log(f"[iphone] imported {it.Name} -> {name}")
            if not want:
                break
        _state["imported"] = _state.get("imported", 0) + len(done)
        return done
    except Exception as e:  # noqa: BLE001
        log(f"[iphone] import error: {e}")
        return done
    finally:
        _state["busy"] = False
        pythoncom.CoUninitialize()
        _import_lock.release()
