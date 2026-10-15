"""Small, dependency-light helpers shared across the vision package."""

from __future__ import annotations

import base64
import importlib.util
import io
import time
from typing import Optional, Sequence, Tuple

import numpy as np


# ── Packaging / availability ───────────────────────────────────────────────

def is_package_available(package: str) -> bool:
    try:
        return importlib.util.find_spec(package) is not None
    except Exception:
        return False


def cv2_available() -> bool:
    return is_package_available("cv2")


def mediapipe_available() -> bool:
    return is_package_available("mediapipe")


# ── Image encoding ─────────────────────────────────────────────────────────

def encode_jpeg(frame_bgr: np.ndarray, quality: int = 82,
                max_width: int = 1280) -> bytes:
    """Encode a BGR OpenCV frame to JPEG bytes.

    Keeps aspect ratio, caps the longest side at ``max_width`` and returns
    raw bytes (``image/jpeg``). Never raises — returns ``b""`` on failure.
    """
    try:
        import cv2
        if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return b""
        frame = frame_bgr
        if max_width > 0 and frame.shape[1] > max_width:
            scale = max_width / float(frame.shape[1])
            frame = cv2.resize(frame, (int(frame.shape[1] * scale),
                                       int(frame.shape[0] * scale)))
        quality = int(max(10, min(95, quality)))
        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else b""
    except Exception:
        return b""


def frame_to_png_b64(frame_bgr: np.ndarray, max_width: int = 1280) -> str:
    """Base64 data URL ready alternative used by generic HTTP vision APIs."""
    jpg = encode_jpeg(frame_bgr, quality=90, max_width=max_width)
    if not jpg:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")


def buffer_to_bgr(jpg_bytes: bytes) -> Optional[np.ndarray]:
    """Decode JPEG/PNG bytes to a BGR numpy frame or None."""
    try:
        import cv2
        arr = np.frombuffer(jpg_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def compress_if_needed(frame_bgr: np.ndarray, max_width: int = 1280) -> np.ndarray:
    try:
        import cv2
        if frame_bgr is None or max_width <= 0 or frame_bgr.shape[1] <= max_width:
            return frame_bgr
        scale = max_width / float(frame_bgr.shape[1])
        return cv2.resize(frame_bgr, (int(frame_bgr.shape[1] * scale),
                                      int(frame_bgr.shape[0] * scale)))
    except Exception:
        return frame_bgr


# ── Geometry / analysis helpers ────────────────────────────────────────────

def iou(a: Tuple[float, float, float, float],
        b: Tuple[float, float, float, float]) -> float:
    """Intersection-over-union of two (x, y, w, h) rects (any units)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / max(1e-9, union)


def rect_from_tensor(parts: Sequence[float]) -> Tuple[float, float, float, float]:
    parts = list(parts)
    if len(parts) >= 4:
        x1, y1, x2, y2 = parts[:4]
        return (x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1))
    return (0.0, 0.0, 0.0, 0.0)


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def position_label(cx: float) -> str:
    if cx < 0.38:
        return "left"
    if cx > 0.62:
        return "right"
    return "center"


def now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def monotonic() -> float:
    return time.monotonic()


def time_ms() -> float:
    return time.monotonic() * 1000.0


# ── Logging ────────────────────────────────────────────────────────────────

class VisionLogger:
    """Tiny namespaced logger; levels: debug/info/warn/error.

    Writes to stdout with a ``[VISION]`` prefix plus (when available) the
    JARVIS UI log channel through an optional callback set by the coordinator.
    """

    _LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

    def __init__(self, level: str = "info", sink=None):
        self.level = self._LEVELS.get((level or "info").lower(), 20)
        self.sink = sink

    def set_level(self, level: str) -> None:
        self.level = self._LEVELS.get((level or "info").lower(), 20)

    def _log(self, level_name: str, level: int, message: str) -> None:
        if level < self.level:
            return
        line = f"[{level_name.upper()}] {message}"
        try:
            print(f"[VISION] {line}")
        except Exception:
            pass
        sink = self.sink
        if sink is not None:
            try:
                sink(line)
            except Exception:
                pass

    def debug(self, msg: str) -> None:
        self._log("debug", 10, msg)

    def info(self, msg: str) -> None:
        self._log("info", 20, msg)

    def warn(self, msg: str) -> None:
        self._log("warn", 30, msg)

    def error(self, msg: str) -> None:
        self._log("error", 40, msg)


logger = VisionLogger()