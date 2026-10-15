"""Background vision pipeline with per-stage frame-rate control.

The coordinator creates a :class:`VisionPipeline` when continuous vision is
enabled.  It subscribes to the shared :class:`vision.camera.CameraManager`
and runs each heavy stage in its own worker thread at its own throttled
rate (YOLO every N ms, faces every N ms, hands real-time, multimodal never
unless triggered).  The latest results are merged into a
:class:`VisionSnapshot`, published to subscribers.

If the camera is idle or continuous vision is off, the pipeline sleeps and
consumes zero CPU.

Nothing here touches Qt or the asyncio loop; results are pushed through
plain callbacks.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from vision.config import VisionConfig
from vision.models import Gesture, Hand, VisionSnapshot
from vision.utils import logger


class VisionPipeline:
    """Rate-limited multi-stage processor."""

    def __init__(self, config: VisionConfig, camera,
                 detectors: Dict[str, object],
                 snapshot_callback: Optional[Callable[[VisionSnapshot], None]] = None):
        self._cfg = config
        self._cam = camera
        self._detectors = detectors              # {"object":..,"face":..,"hand":..,"gesture":..,"ai":..}
        self._snapshot_cb = snapshot_callback
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

        self._objects: list = []
        self._persons: list = []
        self._hands: list = []
        self._gestures: list = []
        self._cached_snapshot = VisionSnapshot()
        self._lock = threading.Lock()
        self._last_frame_idx = -1

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._stop.is_set():
            self._stop = threading.Event()
        spec = [
            (self._loop_object, max(1, self._cfg.object_detection_fps)),
            (self._loop_face,   max(1, self._cfg.face_recognition_fps)),
            (self._loop_hand,   max(1, self._cfg.hand_tracking_fps)),
        ]
        for target, _ in spec:
            t = threading.Thread(target=target, daemon=True,
                                 name=target.__name__)
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            try:
                t.join(timeout=1.5)
            except Exception:
                pass
        self._threads.clear()

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    # ── shared helpers ──────────────────────────────────────────────────────

    def _latest_frame(self) -> Optional[np.ndarray]:
        frame = self._cam.frame()
        if frame is None:
            return None
        with self._lock:
            self._last_frame_idx = self._cam.frame_sequence
        return frame

    def _run_stage(self, stage_fn, result_slot: str) -> None:
        while not self._stop.is_set():
            frame = self._latest_frame()
            if frame is not None:
                try:
                    result = stage_fn(frame)
                    with self._lock:
                        getattr(self, result_slot).clear()
                        getattr(self, result_slot).extend(result or [])
                except Exception as exc:
                    logger.warn(f"pipeline stage {result_slot} error: {exc}")
            self._publish(force=False)
            # Capture _stop.wait with stage-appropriate pacing is done by caller.

    # ── stage loops (each throttles itself) ─────────────────────────────────

    def _loop_object(self) -> None:
        det = self._detectors.get("object")
        interval = 1.0 / max(1, self._cfg.object_detection_fps)
        while not self._stop.wait(interval):
            if not self._cfg.object_detection_enabled or det is None:
                continue
            frame = self._latest_frame()
            if frame is None:
                continue
            try:
                with self._lock:
                    self._objects = det.detect(frame) or []
            except Exception as exc:
                logger.warn(f"object stage error: {exc}")
            self._publish(force=True)

    def _loop_face(self) -> None:
        face = self._detectors.get("face")
        interval = 1.0 / max(1, self._cfg.face_recognition_fps)
        while not self._stop.wait(interval):
            if not self._cfg.face_detection_enabled or face is None:
                continue
            frame = self._latest_frame()
            if frame is None:
                continue
            try:
                if self._cfg.face_recognition_enabled:
                    faces = face.recognize(frame) or []
                else:
                    faces = face.detect_faces(frame) or []
            except Exception as exc:
                logger.warn(f"face stage error: {exc}")
                faces = []
            with self._lock:
                people = []
                for f in faces:
                    from vision.models import Person
                    people.append(Person(face=f, position_label=_pos(f)))
                self._persons = people
            self._publish(force=True)

    def _loop_hand(self) -> None:
        hand = self._detectors.get("hand")
        gesture = self._detectors.get("gesture")
        interval = 1.0 / max(1, self._cfg.hand_tracking_fps)
        while not self._stop.wait(interval):
            if hand is None:
                continue
            frame = self._latest_frame()
            if frame is None:
                continue
            try:
                hands: List[Hand] = hand.detect(frame) or []
            except Exception as exc:
                logger.warn(f"hand stage error: {exc}")
                hands = []
            gestures: List[Gesture] = []
            if hands and gesture is not None and self._cfg.gesture_recognition_enabled:
                gestures = [gesture.recognize(h) for h in hands]
            with self._lock:
                self._hands = hands
                self._gestures = gestures
                for p in self._persons:
                    p.hands = [h for h in hands
                               if p.face and p.face.rect.contains_point(
                                   *h.rect.center())]
            self._publish(force=True)

    # ── snapshot plumbing ───────────────────────────────────────────────────

    def _publish(self, force: bool) -> None:
        with self._lock:
            snap = VisionSnapshot(
                frame_idx=self._last_frame_idx,
                objects=list(self._objects),
                persons=list(self._persons),
                hands=list(self._hands),
                gestures=list(self._gestures),
            )
            self._cached_snapshot = snap
        if self._snapshot_cb is not None and (force or self._cfg.processing_fps <= 30):
            try:
                self._snapshot_cb(snap)
            except Exception as exc:
                logger.debug(f"snapshot consumer error: {exc}")

    def latest_snapshot(self) -> VisionSnapshot:
        with self._lock:
            return self._cached_snapshot


def _pos(face) -> str:
    try:
        cx = face.rect.center()[0]
        if cx < 0.38:
            return "left"
        if cx > 0.62:
            return "right"
        return "center"
    except Exception:
        return ""


__all__ = ["VisionPipeline"]