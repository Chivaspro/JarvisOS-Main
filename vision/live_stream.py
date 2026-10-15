"""Continuous vision: the camera's frames into the Live session.

The Live model can already SEE a frame when one is handed to it — this module is
what keeps handing frames over while the user talks, so "what am I holding?" is
answered from what the camera is looking at *now* rather than from a snapshot
taken minutes ago.

Three deliberate properties:

* **The camera is not opened twice.** ``vision.camera.CameraManager`` owns the
  device (the preview panel, face recognition and the photo tools all share it).
  This stream subscribes to that manager instead of opening its own capture, and
  on the way out it unsubscribes without force-stopping a camera another surface
  is still using.
* **Frames are worth sending, or they are not sent.** Video costs bandwidth and
  tokens on every frame, so a frame is only forwarded when enough time has passed
  *and* the picture actually changed. The counters report how many frames were
  dropped as unchanged, which is what makes "the model is seeing live video"
  verifiable rather than assumed.
* **Nothing here can break a voice turn.** The sender is the caller's callable
  and every failure it raises is caught, counted and reported in ``status()`` —
  a Live session that refuses video must not take the conversation with it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ["LiveVisionStream", "stop_live_vision_streams"]

#: A frame is considered changed when the mean absolute difference between the
#: downsampled greyscale frames exceeds this (0-255 scale). Deliberately low:
#: a hand moving across the desk must register, sensor noise must not.
DEFAULT_CHANGE_THRESHOLD = 6.0

#: How small the comparison frame is. 32×32 is enough to notice a person
#: entering the shot and cheap enough to run on every candidate frame.
_COMPARE_SIZE = 32

#: Streams that are currently subscribed to the camera (for shutdown).
_STREAMS: list["LiveVisionStream"] = []
_STREAMS_LOCK = threading.Lock()


class LiveVisionStream:
    """Feeds JPEG frames from the shared camera into a Live session."""

    def __init__(
        self,
        send: Callable[[bytes], Any],
        *,
        max_fps: float = 1.0,
        quality: int = 70,
        change_threshold: float = DEFAULT_CHANGE_THRESHOLD,
        label: str = "live vision",
    ) -> None:
        #: ``send(jpeg_bytes)`` — returns False (or raises) when the frame could
        #: not be delivered. May return seconds taken, which is reported.
        self._send = send
        self.max_fps = max(0.2, float(max_fps))
        self.quality = max(20, min(95, int(quality)))
        self.change_threshold = float(change_threshold)
        self.label = label

        # One stable reference to the bound method: the camera manager compares
        # subscribers by identity, and ``self._on_frame`` returns a NEW bound
        # method on every attribute access — subscribing with one and
        # unsubscribing with another would silently leave the subscription (and
        # the camera) open forever.
        self._handler = self._on_frame
        self._lock = threading.RLock()
        self._last_sent_at = 0.0
        self._last_signature: Any = None
        self._frames_sent = 0
        self._frames_unchanged = 0
        self._frames_throttled = 0
        self._send_failures = 0
        self._last_error = ""
        self._last_send_ms = 0
        self._send_ms_total = 0.0
        self._started_at = 0.0
        self._running = False
        self._started_camera = False
        self._camera_index = -1

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> tuple[bool, str]:
        """Subscribe to the shared camera and begin forwarding frames."""
        with self._lock:
            if self._running:
                return True, f"{self.label} is already streaming."
        try:
            from vision.camera import get_camera_manager
            manager = get_camera_manager()
        except Exception as exc:
            return False, f"the camera layer is unavailable: {exc}"

        opened_here = False
        try:
            if not manager.running():
                opened_here = bool(manager.start(wait_s=2.5))
                if not opened_here and not manager.running():
                    return False, ("the camera did not start, so I have no picture "
                                   "to send.")
        except Exception as exc:
            return False, f"the camera could not be started: {exc}"

        try:
            manager.subscribe(self._handler, None, max_fps=self.max_fps)
        except Exception as exc:
            return False, f"the camera would not accept a frame subscriber: {exc}"

        try:
            index = int(getattr(manager, "active_index", -1))
        except Exception:
            index = -1
        with self._lock:
            self._running = True
            self._started_camera = opened_here
            self._started_at = time.time()
            # 0 is a real index (the first camera), so it is never folded into
            # the "auto" sentinel.
            self._camera_index = index
        _remember(self)
        return True, (f"{self.label} streaming from camera "
                      f"{self._camera_index if self._camera_index >= 0 else 'auto'}"
                      f" at up to {self.max_fps:.1f} frame(s) per second.")

    def stop(self, reason: str = "") -> str:
        """Detach from the camera. Never leaves a device open by itself."""
        with self._lock:
            was_running = self._running
            self._running = False
            started_camera = self._started_camera
            self._started_camera = False
        if not was_running:
            return f"{self.label} was not streaming."
        try:
            from vision.camera import get_camera_manager
            manager = get_camera_manager()
            manager.unsubscribe(self._handler)
            if started_camera and not _other_subscribers(manager):
                # We are the only frame consumer and we opened the device, so it
                # is ours to release; force=True is safe because nothing else
                # holds a subscription.
                manager.stop(force=True)
        except Exception:
            pass
        _forget(self)
        with self._lock:
            sent = self._frames_sent
            unchanged = self._frames_unchanged
            failures = self._send_failures
        detail = (f"{self.label} stopped after sending {sent} frame(s) "
                  f"({unchanged} unchanged, skipped).")
        if failures:
            detail += f" {failures} frame(s) could not be sent."
        if reason:
            detail += f" ({reason})"
        return detail

    # ── the frame path ────────────────────────────────────────────────────────

    def _on_frame(self, jpeg: bytes) -> None:
        """Called by the capture thread for each candidate frame."""
        with self._lock:
            if not self._running:
                return
        if not jpeg:
            return
        signature = self._signature(jpeg)
        with self._lock:
            if signature is not None and self._last_signature is not None:
                if _difference(signature, self._last_signature) < self.change_threshold:
                    self._frames_unchanged += 1
                    return
            if not _due(self._last_sent_at, self.max_fps):
                self._frames_throttled += 1
                return
            self._last_sent_at = time.monotonic()
            self._last_signature = signature
        self._deliver(jpeg)

    def send_once(self) -> tuple[bool, str]:
        """Send the current frame whatever the gate says (for a direct question)."""
        jpeg = b""
        try:
            from vision.camera import get_camera_manager
            jpeg = get_camera_manager().jpeg() or b""
        except Exception as exc:
            return False, f"the camera could not be read: {exc}"
        if not jpeg:
            return False, ("there is no frame from the camera yet — the preview may "
                           "still be starting.")
        if not self._deliver(jpeg):
            return False, self._last_error or "the frame could not be sent."
        return True, f"Sent one frame to the Live session ({len(jpeg) // 1024} KB)."

    def _deliver(self, jpeg: bytes) -> bool:
        started = time.monotonic()
        try:
            outcome = self._send(jpeg)
        except Exception as exc:
            outcome = False
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = ""
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with self._lock:
            self._last_send_ms = elapsed_ms
            self._send_ms_total += elapsed_ms
            if outcome is False:
                self._send_failures += 1
                self._last_error = detail or ("the Live session refused the frame "
                                              "(is it still connected?)")
                return False
            self._frames_sent += 1
            self._last_error = ""
        return True

    def _signature(self, jpeg: bytes):
        """A tiny greyscale thumbnail used for change detection (None without cv2)."""
        try:
            import cv2
            import numpy as np
        except Exception:
            return None
        try:
            buffer = np.frombuffer(jpeg, dtype=np.uint8)
            frame = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
            if frame is None:
                return None
            small = cv2.resize(frame, (_COMPARE_SIZE, _COMPARE_SIZE),
                               interpolation=cv2.INTER_AREA)
            return small.astype("float32")
        except Exception:
            return None

    # ── reporting ─────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            sent = self._frames_sent
            average = (self._send_ms_total / sent) if sent else 0.0
            return {
                "running": self._running,
                "camera_index": self._camera_index,
                "max_fps": self.max_fps,
                "seconds": round(time.time() - self._started_at, 1)
                if self._started_at else 0.0,
                "frames_sent": sent,
                "frames_unchanged": self._frames_unchanged,
                "frames_throttled": self._frames_throttled,
                "send_failures": self._send_failures,
                "last_send_ms": self._last_send_ms,
                "average_send_ms": int(average),
                "last_error": self._last_error,
            }

    def report(self) -> str:
        """One honest paragraph for the assistant and the HUD."""
        state = self.status()
        if not state["running"]:
            line = (f"{self.label} is not streaming. It sent "
                    f"{state['frames_sent']} frame(s) over {state['seconds']}s "
                    f"since it started.")
        else:
            line = (f"{self.label} is streaming from camera "
                    f"{state['camera_index'] if state['camera_index'] >= 0 else 'auto'}"
                    f": {state['frames_sent']} frame(s) delivered, "
                    f"{state['frames_unchanged']} skipped as unchanged, "
                    f"{state['frames_throttled']} skipped by the rate limit, "
                    f"average send {state['average_send_ms']} ms.")
        if state["send_failures"]:
            line += (f" {state['send_failures']} frame(s) failed"
                     + (f": {state['last_error']}" if state["last_error"] else "."))
        return line


# ── helpers (pure, so they can be reasoned about and tested directly) ────────

def _due(last_sent_at: float, max_fps: float) -> bool:
    if last_sent_at <= 0:
        return True
    return (time.monotonic() - last_sent_at) >= (1.0 / max(max_fps, 0.2))


def _difference(first, second) -> float:
    """Mean absolute difference between two thumbnails (0-255 scale)."""
    try:
        import numpy as np
        if first is None or second is None or first.shape != second.shape:
            return 255.0
        return float(np.abs(first - second).mean())
    except Exception:
        return 255.0


def _remember(stream: "LiveVisionStream") -> None:
    """Track live streams so shutdown can detach every camera subscriber."""
    with _STREAMS_LOCK:
        if stream not in _STREAMS:
            _STREAMS.append(stream)


def _forget(stream: "LiveVisionStream") -> None:
    with _STREAMS_LOCK:
        try:
            _STREAMS.remove(stream)
        except ValueError:
            pass


def stop_live_vision_streams(reason: str = "shutdown") -> int:
    """Stop every live-vision stream this process still owns.

    Called from the shutdown path before the camera is released: a stream that
    is still subscribed would otherwise keep the device open (CameraManager
    does not stop a camera another subscriber needs) and the process would
    linger on a camera it is no longer using.
    """
    with _STREAMS_LOCK:
        streams = list(_STREAMS)
    stopped = 0
    for stream in streams:
        try:
            if stream.status().get("running"):
                stream.stop(reason)
                stopped += 1
        except Exception:
            pass
    return stopped


def _other_subscribers(manager) -> bool:
    """True when another surface is still consuming frames."""
    try:
        subs = getattr(manager, "_subs", None)
        if not isinstance(subs, list):
            return False
        return any(entry and entry[0] is not None for entry in subs)
    except Exception:
        return False
