"""Gesture recognition computed purely from MediaPipe hand landmarks.

Classifier logic lives here so it can be unit-tested without a camera or
GPU.  Supported gestures:

    open_palm, fist, point, thumbs_up, thumbs_down, peace,
    ok, pinch, stop (+ swipe direction estimated from hand trajectory)

Gestures are individually toggleable.  Mapping a gesture to a computer
**action** (keyboard shortcuts, confirm, cancel…) is intentionally left to
the coordinator/plugin layer — never here — so nothing dangerous can fire
without explicit confirmation.

Implementation notes
--------------------
* Finger states (extended/folded) derive from the ``tip_y < pip_y`` rule
  for the four fingers and the ``thumb_tip`` relative to the index MCP for
  the thumb, which is far more robust than raw y comparisons.
* A short trajectory buffer converts finger counts into a swipe direction.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

from vision.config import VisionConfig
from vision.models import Gesture, GestureKind, Hand, HandLand, Handedness

# Landmark indices (MediaPipe Hands / Hand Landmarker).
_TIP = 8        # index finger tip
_PIP = 6
_MID = 12       # middle finger tip
_MID_PIP = 10
_RING = 16
_RING_PIP = 14
_PINKY = 20
_PINKY_PIP = 18
_THUMB_TIP = 4
_INDEX_MCP = 5


def _l(hand: Hand, idx: int) -> Optional[HandLand]:
    if hand is None or not hand.landmarks or idx >= len(hand.landmarks):
        return None
    return hand.landmarks[idx]


def _dist(a: HandLand, b: HandLand) -> float:
    dx, dy = a.x - b.x, a.y - b.y
    return math.hypot(dx, dy)


def _finger_extended(hand: Hand, tip: int, pip: int,
                     is_thumb: bool = False) -> bool:
    t = _l(hand, tip)
    p = _l(hand, pip)
    if t is None or p is None:
        return False
    if is_thumb:
        # Thumb is folded / extended relative to the index MCP, which works
        # for most natural poses without handedness calibration.
        mcp = _l(hand, _INDEX_MCP)
        if mcp is None:
            return False
        return (t.x - mcp.x) > 0.02 or abs(t.y - mcp.y) > 0.03
    return t.y < p.y


def _count_extended(hand: Hand) -> int:
    return sum([
        _finger_extended(hand, 8, 6),
        _finger_extended(hand, 12, 10),
        _finger_extended(hand, 16, 14),
        _finger_extended(hand, 20, 18),
    ])


def _thumb_state(hand: Hand) -> Tuple[bool, bool]:
    """Return (thumb_extended, thumb_touches_index)."""
    t = _l(hand, 4)
    i_mcp = _l(hand, 5)
    i_tip = _l(hand, 8)
    if t is None or i_mcp is None or i_tip is None:
        return False, False
    extended = (t.x - i_mcp.x) > 0.02 or abs(t.y - i_mcp.y) > 0.03
    touching = _dist(t, i_tip) < 0.055 and _dist(t, i_mcp) < 0.16
    return extended, touching


class GestureRecognizer:
    """Classify hands into gestures. Re-entrant-safe; no state except the
    optional swipe trajectory."""

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._enabled = set()
        for g in list(GestureKind):
            key = f"gesture_{g.value}"
            if getattr(cfg.as_dict, "get", lambda *_: True)(key, True):
                self._enabled.add(g.value)
        self._trajectory: List[Tuple[float, float]] = []

    def recognize(self, hand: Hand) -> Gesture:
        if hand is None or not hand.landmarks:
            return Gesture(GestureKind.UNKNOWN, 0.0, Handedness.NONE)
        ext_count = _count_extended(hand)
        thumb_ext, thumb_touch = _thumb_state(hand)

        kind = GestureKind.UNKNOWN
        conf = 0.0

        # Thumb-only gestures first (hand orientation independent enough).
        if thumb_ext and ext_count == 0:
            # thumbs up: thumb clearly above index MCP in frame coords.
            t = _l(hand, 4); mcp = _l(hand, 5)
            if t is not None and mcp is not None:
                if t.y < mcp.y - 0.04:
                    kind, conf = GestureKind.THUMBS_UP, 0.9
                elif t.y > mcp.y + 0.04:
                    kind, conf = GestureKind.THUMBS_DOWN, 0.9
        elif not thumb_ext and ext_count == 0:
            kind, conf = GestureKind.FIST, 0.92
        elif ext_count == 1 and not _finger_extended(hand, 20, 18) \
                and not _finger_extended(hand, 16, 14):
            kind, conf = GestureKind.POINT, 0.85
        elif ext_count == 2 and _finger_extended(hand, 8, 6) \
                and _finger_extended(hand, 12, 10) \
                and not _finger_extended(hand, 16, 14) \
                and not _finger_extended(hand, 20, 18):
            # peace / V
            sep = 0.0
            a, b = _l(hand, 8), _l(hand, 12)
            if a and b:
                sep = _dist(a, b)
            kind, conf = GestureKind.PEACE, max(0.55, 1.0 - sep * 6)
        elif ext_count == 1 and not thumb_ext and thumb_touch:
            kind, conf = GestureKind.OK, 0.8
        elif ext_count == 4:
            kind, conf = GestureKind.OPEN_PALM, 0.9
        elif ext_count == 5:
            kind, conf = GestureKind.OPEN_PALM, 0.9

        if thumb_touch and ext_count <= 1 and kind == GestureKind.UNKNOWN:
            kind, conf = GestureKind.PINCH, 0.78

        if kind == GestureKind.UNKNOWN and ext_count == 0 and thumb_ext \
                and _l(hand, 5) is None:
            kind, conf = GestureKind.FIST, 0.5

        gesture = Gesture(kind=kind, confidence=conf,
                          hand=hand.handedness,
                          metadata={"extended": ext_count,
                                    "thumb_extended": thumb_ext})
        # Attach swipe info when meaningful.
        if ext_count == 4 or ext_count == 5:
            swipe = self._swipe(hand)
            if swipe:
                gesture.metadata["swipe"] = swipe
        if kind.value not in self._enabled:
            return Gesture(GestureKind.UNKNOWN, conf, hand.handedness,
                           {"source": kind.value})
        return gesture

    # ── swipe estimation from wrist trajectory ─────────────────────────────

    def _swipe(self, hand: Hand) -> Optional[str]:
        w = hand.wrist()
        if w is None:
            return None
        pt = (w.x, w.y)
        self._trajectory.append(pt)
        # keep ~6 samples
        if len(self._trajectory) < 4:
            return None
        self._trajectory = self._trajectory[-6:]
        dx = self._trajectory[-1][0] - self._trajectory[0][0]
        dy = self._trajectory[-1][1] - self._trajectory[0][1]
        if max(abs(dx), abs(dy)) < 0.12:
            return None
        if abs(dx) > abs(dy):
            return "right" if dx > 0 else "left"
        return "up" if dy > 0 else "down"

    def reset_trajectory(self) -> None:
        self._trajectory.clear()

    @staticmethod
    def describe(gesture: Gesture) -> str:
        names = {
            GestureKind.OPEN_PALM: "an open palm",
            GestureKind.FIST: "a closed fist",
            GestureKind.POINT: "a pointing finger",
            GestureKind.THUMBS_UP: "a thumbs up",
            GestureKind.THUMBS_DOWN: "a thumbs down",
            GestureKind.PEACE: "a peace sign",
            GestureKind.OK: "an OK sign",
            GestureKind.PINCH: "a pinch",
            GestureKind.STOP: "a stop hand",
            GestureKind.UNKNOWN: "an unclassified hand pose",
        }
        base = names.get(gesture.kind, "an unknown gesture")
        h = gesture.hand.value if gesture.hand else "Left"
        if gesture.metadata.get("swipe"):
            return f"{h.lower()} hand sweeping {gesture.metadata['swipe']}"
        return f"the {h} hand forming {base}"


__all__ = ["GestureRecognizer", "_count_extended", "_finger_extended"]