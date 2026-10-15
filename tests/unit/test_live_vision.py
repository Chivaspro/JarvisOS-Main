"""Live vision: motion gating, rate limiting, honest failures and shutdown.

The camera is never opened: a stub manager stands in for the shared capture, so
the tests exercise the layer that decides whether a frame is worth sending and
what is reported when it is not.
"""
from __future__ import annotations

import time

import pytest

from vision import live_stream


class _StubCamera:
    """Stands in for vision.camera.CameraManager."""

    def __init__(self, running: bool = True, jpeg: bytes = b"jpeg-bytes"):
        self._running = running
        self.subscribers: list = []
        self.started = 0
        self.stopped = 0
        self._jpeg = jpeg
        self.active_index = 0

    def running(self) -> bool:
        return self._running

    def start(self, on_frame=None, on_state=None, wait_s: float = 1.5) -> bool:
        self.started += 1
        self._running = True
        return True

    def stop(self, force: bool = False) -> None:
        self.stopped += 1
        self._running = False

    def subscribe(self, on_frame, on_state=None, max_fps: float = 30.0,
                  cooldown: float = 0.0) -> None:
        self.subscribers.append([on_frame, on_state, max_fps, 0.0])

    def unsubscribe(self, on_frame) -> None:
        self.subscribers = [s for s in self.subscribers if s[0] is not on_frame]

    def jpeg(self) -> bytes:
        return self._jpeg

    def push(self, jpeg: bytes) -> None:
        for entry in list(self.subscribers):
            if entry[0] is not None:
                entry[0](jpeg)


@pytest.fixture()
def camera(monkeypatch):
    stub = _StubCamera()
    import vision.camera as camera_module
    monkeypatch.setattr(camera_module, "get_camera_manager", lambda config=None: stub)
    return stub


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_a_frame_is_due_when_none_was_ever_sent():
    assert live_stream._due(0.0, 1.0) is True


def test_the_rate_limit_holds_a_second_frame_back():
    assert live_stream._due(time.monotonic(), 0.2) is False


def test_identical_thumbnails_are_not_a_change():
    import numpy as np
    frame = np.zeros((live_stream._COMPARE_SIZE, live_stream._COMPARE_SIZE),
                     dtype="float32")
    assert live_stream._difference(frame, frame) == 0.0


def test_a_missing_thumbnail_counts_as_a_change():
    assert live_stream._difference(None, None) == 255.0


# ── starting and stopping ────────────────────────────────────────────────────

def test_starting_subscribes_to_the_shared_camera(camera):
    stream = live_stream.LiveVisionStream(lambda jpeg: True)
    ok, message = stream.start()
    assert ok, message
    assert len(camera.subscribers) == 1
    assert stream.status()["running"] is True
    stream.stop("test over")
    assert camera.subscribers == []


def test_the_stream_opens_the_camera_only_when_nobody_else_has_it(monkeypatch):
    stub = _StubCamera(running=False)
    import vision.camera as camera_module
    monkeypatch.setattr(camera_module, "get_camera_manager", lambda config=None: stub)
    stream = live_stream.LiveVisionStream(lambda jpeg: True)
    assert stream.start()[0] is True
    assert stub.started == 1
    stream.stop("test over")
    # It opened the device, so it is the one that releases it.
    assert stub.stopped == 1


def test_a_camera_another_surface_uses_is_left_alone(camera):
    other = live_stream.LiveVisionStream(lambda jpeg: True)
    other.start()
    stream = live_stream.LiveVisionStream(lambda jpeg: True)
    stream.start()
    stream.stop("test over")
    assert camera.stopped == 0
    other.stop("test over")


def test_a_camera_that_does_not_start_is_reported(monkeypatch):
    class _Dead(_StubCamera):
        def running(self) -> bool:
            return False

        def start(self, on_frame=None, on_state=None, wait_s: float = 1.5):
            return False

    import vision.camera as camera_module
    monkeypatch.setattr(camera_module, "get_camera_manager", lambda config=None: _Dead())
    ok, message = live_stream.LiveVisionStream(lambda jpeg: True).start()
    assert ok is False
    assert "did not start" in message


def test_stopping_without_starting_says_so(camera):
    stream = live_stream.LiveVisionStream(lambda jpeg: True)
    assert "was not streaming" in stream.stop()


# ── the frame path ───────────────────────────────────────────────────────────

def test_a_changed_frame_is_sent_and_the_unchanged_one_is_skipped(camera):
    import numpy as np
    sent: list[bytes] = []
    stream = live_stream.LiveVisionStream(sent.append, max_fps=30.0,
                                          change_threshold=1.0)
    stream.start()
    first = _jpeg(np.zeros((64, 64, 3), dtype="uint8"))
    second = _jpeg(np.full((64, 64, 3), 255, dtype="uint8"))
    camera.push(first)
    camera.push(first)
    # The third frame really is different; open the rate gate so the test is
    # about the change detector rather than about the clock.
    stream._last_sent_at = 0.0
    camera.push(second)
    stream.stop("test over")
    state = stream.status()
    assert len(sent) == 2
    assert state["frames_sent"] == 2
    assert state["frames_unchanged"] == 1


def test_the_rate_limit_drops_frames_it_cannot_send(camera):
    stream = live_stream.LiveVisionStream(lambda jpeg: True, max_fps=0.5)
    stream.start()
    # Force the gate closed by pretending a frame was just delivered.
    stream._last_sent_at = time.monotonic()
    stream._on_frame(b"anything")
    stream.stop("test over")
    assert stream.status()["frames_throttled"] >= 1


def test_a_send_that_fails_is_counted_and_explained(camera):
    def _boom(_jpeg):
        raise RuntimeError("the session is closed")

    stream = live_stream.LiveVisionStream(_boom, max_fps=30.0)
    stream.start()
    camera.push(_jpeg())
    stream.stop("test over")
    state = stream.status()
    assert state["send_failures"] == 1
    assert state["frames_sent"] == 0
    assert "session is closed" in state["last_error"]
    assert "1 frame(s) failed" in stream.report()
    assert "session is closed" in stream.report()


def test_a_sender_that_returns_false_is_a_failure_not_a_success(camera):
    stream = live_stream.LiveVisionStream(lambda jpeg: False, max_fps=30.0)
    stream.start()
    camera.push(_jpeg())
    stream.stop("test over")
    assert stream.status()["frames_sent"] == 0
    assert stream.status()["send_failures"] == 1


def test_the_report_contains_only_measured_numbers(camera):
    stream = live_stream.LiveVisionStream(lambda jpeg: True, max_fps=30.0)
    stream.start()
    camera.push(_jpeg())
    report = stream.report()
    stream.stop("test over")
    assert "delivered" in report
    assert "average send" in report
    assert "camera 0" in report


def test_send_once_uses_the_current_frame(camera):
    stream = live_stream.LiveVisionStream(lambda jpeg: True)
    ok, message = stream.send_once()
    assert ok, message
    assert "Sent one frame" in message
    assert stream.status()["frames_sent"] == 1


def test_send_once_without_a_frame_is_honest(monkeypatch):
    stub = _StubCamera(jpeg=b"")
    import vision.camera as camera_module
    monkeypatch.setattr(camera_module, "get_camera_manager", lambda config=None: stub)
    ok, message = live_stream.LiveVisionStream(lambda jpeg: True).send_once()
    assert ok is False
    assert "no frame" in message


# ── shutdown ─────────────────────────────────────────────────────────────────

def test_shutdown_stops_every_subscribed_stream(camera):
    first = live_stream.LiveVisionStream(lambda jpeg: True)
    second = live_stream.LiveVisionStream(lambda jpeg: True)
    first.start()
    second.start()
    stopped = live_stream.stop_live_vision_streams("test shutdown")
    assert stopped == 2
    assert camera.subscribers == []


def test_shutdown_with_nothing_running_is_harmless(camera):
    assert live_stream.stop_live_vision_streams("nothing to do") == 0


def _jpeg(shape_or_none=None):
    """A real JPEG so the change detector has something to decode."""
    import numpy as np
    try:
        import cv2
    except Exception:
        return b"not-a-real-jpeg"
    frame = np.zeros((48, 48, 3), dtype="uint8") if shape_or_none is None \
        else np.asarray(shape_or_none, dtype="uint8")
    ok, buffer = cv2.imencode(".jpg", frame)
    return buffer.tobytes() if ok else b""
