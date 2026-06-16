"""Region motion tracking for the Form Studio.

Given a video, a start time, a duration, and an initial region (normalized
bbox), track that region frame-by-frame with OpenCV CSRT and return a path of
normalized centers. The renderer then makes a marker ride that path so a zone
you outline "sticks" to your glove/head as it moves.

CSRT is accurate but can drift on fast/blurred motion; the editor offers manual
keyframe correction for those cases, and pose-anchoring (later) for body parts.
"""
from __future__ import annotations

import cv2

from media import probe


def _make_tracker():
    if hasattr(cv2, "TrackerCSRT_create"):
        return cv2.TrackerCSRT_create()
    return cv2.legacy.TrackerCSRT_create()


def track_region(video: str, start: float, dur: float,
                 bbox_norm: tuple[float, float, float, float],
                 max_points: int = 240) -> list[list[float]]:
    """Return [[t, cx, cy, w, h], ...] in normalized coords (0..1).

    cx,cy = region center; w,h = region size. The first entry is the seed.
    """
    info = probe(video)
    W, H = info.width, info.height
    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        return []
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000.0)
    ok, frame = cap.read()
    if not ok:
        cap.release()
        return []

    nx, ny, nw, nh = bbox_norm
    x, y = int(nx * W), int(ny * H)
    w, h = max(8, int(nw * W)), max(8, int(nh * H))
    tracker = _make_tracker()
    tracker.init(frame, (x, y, w, h))

    end = start + dur
    track = [[round(start, 3), (x + w / 2) / W, (y + h / 2) / H, w / W, h / H]]
    while len(track) < max_points:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t > end:
            break
        ok2, box = tracker.update(frame)
        if not ok2:
            break
        bx, by, bw, bh = box
        track.append([round(t, 3), (bx + bw / 2) / W, (by + bh / 2) / H,
                      bw / W, bh / H])
    cap.release()
    return track
