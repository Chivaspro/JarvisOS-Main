"""Real-time hand tracking built on MediaPipe Hands.

Uses the bundled MediaPipe models (no downloads).  This module only reads
frames — it never opens a camera itself, sharing the singleton
:class:`vision.camera.CameraManager` used by the rest of JARVIS.
"""

from __future__ import annotations

import threading
from typing import List, Optional

from vision.config import VisionConfig
from vision.models import Hand, HandLand, Handedness, Rect
from vision.utils import is_package_available, logger

try:
    import cv2
    _CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _CV2 = False

try:
    import mediapipe as mp
    _MP = True
except Exception:  # pragma: no cover
    mp = None
    _MP = False


class HandTracker:
    """Detect hands + landmarks in a BGR frame. Never raises."""

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._hands = None
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not _MP:
            logger.warn("mediapipe missing — hand tracking unavailable")
            return
        try:
            self._hands = mp.solutions.hands.Hands(
                static_image_mode=False,
                max_num_hands=max(1, min(8, self._cfg.max_hands)),
                min_detection_confidence=0.4,
                min_tracking_confidence=0.4,
                model_complexity=1,
            )
        except Exception as exc:
            logger.warn(f"mediapipe hands failed: {exc}")
            self._hands = None

    @property
    def ready(self) -> bool:
        return self._hands is not None

    def detect(self, frame_bgr) -> List[Hand]:
        try:
            if not self.ready or frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
                return []
            with self._lock:
                rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                res = self._hands.process(rgb)
            return self._to_hands(res)
        except Exception as exc:
            logger.debug(f"hand detect error: {exc}")
            return []

    def _to_hands(self, res) -> List[Hand]:
        if res is None or not getattr(res, "multi_hand_landmarks", None):
            return []
        hands: List[Hand] = []
        for i, hms in enumerate(res.multi_hand_landmarks):
            lms = [HandLand(x=l.x, y=l.y, z=l.z, index=idx)
                   for idx, l in enumerate(hms.landmark)]
            htype = Handedness.NONE
            conf = 0.5
            try:
                cls = res.multi_handedness[i]
                if cls is not None:
                    htype = Handedness(str(cls.classification[0].label))
                    conf = float(cls.classification[0].score)
            except Exception:
                pass
            xs = [l.x for l in lms]
            ys = [l.y for l in lms]
            x0, y0 = min(xs), min(ys)
            w, h = max(xs) - x0, max(ys) - y0
            hands.append(Hand(handedness=htype, landmarks=lms,
                              rect=Rect(x=x0, y=y0, w=w, h=h), confidence=conf))
        # Sort hands left→right so "the left hand" is deterministic.
        hands.sort(key=lambda hand: hand.rect.center()[0])
        return hands

    @staticmethod
    def hand_position(hand: Hand) -> str:
        cx, cy = hand.rect.center()
        horiz = "left" if cx < 0.32 else ("right" if cx > 0.68 else "center")
        vert = "top" if cy < 0.4 else ("bottom" if cy > 0.65 else "middle")
        return f"{horiz} {vert}".strip()


__all__ = ["HandTracker"]