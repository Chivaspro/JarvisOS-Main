"""Webcam pipeline tests (offscreen, no physical camera).

These cover the contract the preview depends on:

* JPEG decode happens on the decoder thread, never on the UI thread.
* Frames are newest-wins: a frame that arrives while another is still being
  decoded is replaced, so the preview can never fall behind by a backlog and
  never paints an older frame after a newer one.
* Start / stop / restart leaves exactly one capture thread and one
  subscription, and closing the window releases both.
* The camera manager refuses to tear the shared stream down while another
  push consumer is still subscribed.

A fake camera manager is injected, so the tests run anywhere.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QBuffer, QIODevice  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

# ``tests/ui`` is a package named ``ui``, so load the app module under an alias.
_ui_spec = importlib.util.spec_from_file_location("ui_impl_camera_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

CAMERA_CONFIG = {
    "assistant_name": "JARVIS",
    "user_name": "Operator",
    "gemini_api_key": "test-key",
    "os_system": "windows",
    "compact_size": 176,
    "reactor_opacity": 60,
    "voice_output_mode": "gemini_piper",
    "tts_engine": "onnx",
    "voice_name": "Charon",
    "features": {"remember_position": True, "camera_access": True},
}


def _solid_jpeg(color: QColor, w: int = 320, h: int = 240) -> bytes:
    """A real JPEG of one flat colour (Qt encoder, no cv2)."""
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(color)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPEG")
    return bytes(buf.data())


def _make_jpeg(shade: int, w: int = 320, h: int = 240) -> bytes:
    """A real JPEG whose green channel encodes ``shade``."""
    return _solid_jpeg(QColor(10, shade, 60), w, h)


class _NullAnalyzer:
    """No-op stand-in for ``_VisionAnalyzer`` (see the window fixture)."""

    def start(self):
        pass

    def stop(self, timeout: float = 0.0):
        pass

    def submit(self, jpg):
        pass

    def set_wants(self, **kwargs):
        pass


class FakeCameraManager:
    """Stands in for the process-wide CameraManager."""

    def __init__(self, fps: float = 30.0):
        self.enabled = True
        self._fps = fps
        self._lock = threading.Lock()
        self._subs = []
        self._running = False
        self._thread = None
        self._session = None
        self.sent = 0
        self.started = 0
        self.stopped = 0
        self.forced_stops = 0

    # ── manager surface used by MainWindow._cam_loop ─────────────────────
    @property
    def running(self) -> bool:
        return self._running

    @property
    def active_index(self) -> int:
        return 0

    def subscribe(self, on_frame=None, on_state=None, max_fps=30.0, cooldown=0.0):
        # Same entry shape as the real CameraManager so the production code
        # sees an identical consumer list.
        with self._lock:
            self._subs = [s for s in self._subs if s[0] is not on_frame]
            self._subs.append([on_frame, on_state, float(max_fps), float(cooldown)])

    def unsubscribe(self, on_frame=None):
        with self._lock:
            self._subs = [s for s in self._subs if s[0] is not on_frame]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def start(self, on_frame=None, on_state=None) -> bool:
        if on_frame is not None:
            self.subscribe(on_frame, on_state)
        with self._lock:
            if self._running:
                return True
            self._running = True
            self._session = threading.Event()
            self.started += 1
            self._thread = threading.Thread(target=self._push_loop, args=(self._session,),
                                            daemon=True, name="fake-camera")
            self._thread.start()
        return True

    def stop(self, force: bool = False) -> None:
        with self._lock:
            session = self._session
        if session is not None:
            session.set()
        self._session = None
        self._running = False
        self.stopped += 1
        if force:
            self.forced_stops += 1

    def frame(self):
        return None

    def jpeg(self) -> bytes:
        return b""

    def take_photo(self):
        return None

    def reload_config(self, config=None) -> None:
        pass

    # ── synthetic capture ───────────────────────────────────────────────
    def _push_loop(self, session: threading.Event) -> None:
        interval = 1.0 / max(1.0, self._fps)
        while not session.is_set():
            self.sent += 1
            shade = 8 + (self.sent % 40) * 5
            jpg = _make_jpeg(shade)
            with self._lock:
                subs = [s[0] for s in self._subs]
            for fn in subs:
                try:
                    fn(jpg)
                except Exception:
                    pass
            if session.wait(interval):
                break


@pytest.fixture()
def camera(qapp, monkeypatch):
    fake = FakeCameraManager()
    import vision.camera as camera_mod
    monkeypatch.setattr(camera_mod, "get_camera_manager", lambda *a, **k: fake)
    yield fake
    # Stop the synthetic capture thread at the end of EVERY test that used it.
    # It was never stopped, so one 30 fps thread per test kept running for the
    # rest of the session: each pass encoded a 320x240 JPEG off the UI thread and
    # pushed it at a window whose Qt objects were already gone (the window
    # fixture tears down first). Queued frames plus Qt calls from a foreign
    # thread is a process-killing combination — the whole run used to die with
    # no traceback a few tests later, and the survivors showed up as a starved
    # 1.6 s animation that "never ended".
    fake.stop(force=True)
    thread = getattr(fake, "_thread", None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch, camera):
    """A real MainWindow whose camera never touches the physical device."""
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps(CAMERA_CONFIG), encoding="utf-8")
    monkeypatch.setattr(ui, "API_FILE", config_path)
    monkeypatch.setattr(ui, "_read_full_config", lambda: dict(CAMERA_CONFIG))
    monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(CAMERA_CONFIG))
    # Never write the shared layout file from a test.
    monkeypatch.setattr(ui._FeatheredCamView, "_save_geom", lambda self: None)
    monkeypatch.setattr(ui._FeatheredCamView, "_load_geom", lambda self: None)

    # The analysis worker loads MediaPipe/TFLite the first time it sees a frame,
    # and this environment crashes inside that native library's teardown at
    # process exit. These tests cover the camera frame pipeline and the Vision
    # drawer, not the analyser, so the worker is stubbed out at the class.
    monkeypatch.setattr(ui.RefinedCommandWindow, '_ensure_vision_analyzer',
                        lambda self: _NullAnalyzer())

    win = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    yield win
    try:
        # Same teardown as the real window close: stop AND join every camera
        # worker, so no surviving thread can outlive the Qt objects, then take
        # the floating surfaces (own top-level windows) down with it.
        win._shutdown_camera_threads(timeout=5.0)
        for name in ('_cam_live_lbl', '_cam_preview'):
            panel = getattr(win, name, None)
            if panel is not None:
                try:
                    panel.close()
                except Exception:
                    pass
        qapp.processEvents()
        win.close()
    except Exception:
        pass
    qapp.processEvents()


def _pump(qapp, seconds: float, step: float = 0.01) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(step)


def _threads_named(name: str):
    return [t for t in threading.enumerate() if t.name == name]


class _RecordingQImage:
    """``ui.QImage`` stand-in that records which thread decodes a JPEG."""

    Format = QImage.Format
    decode_threads: list[int] = []

    def __new__(cls, *args, **kwargs):
        return QImage(*args, **kwargs)

    @staticmethod
    def fromData(data, *args, **kwargs):
        _RecordingQImage.decode_threads.append(threading.get_ident())
        return QImage.fromData(data, *args, **kwargs)


# ── decode is off the UI thread ─────────────────────────────────────────────

def test_jpeg_decode_never_runs_on_the_ui_thread(qapp, window, monkeypatch):
    _RecordingQImage.decode_threads = []
    monkeypatch.setattr(ui, "QImage", _RecordingQImage)

    window.start_camera_stream()
    _pump(qapp, 0.6)

    assert _RecordingQImage.decode_threads, "the fake camera produced no frames"
    assert threading.get_ident() not in _RecordingQImage.decode_threads, \
        "JPEG decoding happened on the UI thread"


def test_frames_reach_the_floating_preview(qapp, window, camera):
    window.start_camera_stream()
    _pump(qapp, 0.8)

    lbl = window._cam_live_lbl
    assert camera.subscriber_count == 1, "the stream must have exactly one subscription"
    assert lbl._frame_n > 0, "no frame reached the preview"
    assert not lbl._img.isNull(), "the preview holds no decoded image"


def test_newest_frame_wins_and_stale_frames_are_dropped(qapp, window, camera, monkeypatch):
    """A backlog must collapse to the newest frame, not replay old ones."""
    real_from_data = QImage.fromData
    state = {"slow": False, "decoded": 0}

    def slow_from_data(data, *args, **kwargs):
        if state["slow"]:
            time.sleep(0.08)
        state["decoded"] += 1
        return real_from_data(data, *args, **kwargs)

    presented_shades: list[int] = []
    real_set_frame = ui._FeatheredCamView.set_frame_image

    def recording_set_frame(self, img):
        presented_shades.append(img.pixelColor(4, 4).green())
        return real_set_frame(self, img)

    monkeypatch.setattr(ui, "QImage", type("Q", (), {
        "Format": QImage.Format,
        "__new__": staticmethod(lambda cls, *a, **k: QImage(*a, **k)),
        "fromData": staticmethod(slow_from_data),
    }))
    monkeypatch.setattr(ui._FeatheredCamView, "set_frame_image", recording_set_frame)

    window.start_camera_stream()
    state["slow"] = True
    _pump(qapp, 1.2)
    state["slow"] = False
    _pump(qapp, 0.2)

    assert len(presented_shades) >= 3, f"too few frames presented: {presented_shades}"
    # Frames were skipped rather than decoded and shown one after another.
    assert state["decoded"] < camera.sent, \
        f"decoded {state['decoded']} of {camera.sent} pushed frames — nothing was coalesced"
    # Never paint an older frame after a newer one (JPEG shifts values a little).
    for older, newer in zip(presented_shades, presented_shades[1:]):
        assert newer >= older - 3, f"stale frame presented: {older} then {newer}"


def test_ui_thread_cost_per_frame_is_small(qapp, window, monkeypatch):
    samples: list[float] = []
    win = window
    presenter = ui.MainWindow._present_camera_frame

    def timed_presenter():
        t0 = time.perf_counter()
        presenter(win)
        samples.append(time.perf_counter() - t0)

    win._frame_decoder.decoded.disconnect()
    win._frame_decoder.decoded.connect(timed_presenter)

    win.start_camera_stream()
    _pump(qapp, 0.8)

    assert samples, "the presenter never ran"
    mean = sum(samples) / len(samples)
    assert mean < 0.020, f"UI thread spends {mean * 1000:.1f} ms per frame"
    assert max(samples) < 0.080, f"worst UI-thread frame took {max(samples) * 1000:.1f} ms"


# ── one-shot photos are their own pipeline ──────────────────────────────────

def test_one_shot_photo_survives_the_live_stream(qapp, window, camera):
    """A photo someone asked for must not be replaced by the next live frame."""
    window.start_camera_stream()
    _pump(qapp, 0.4)
    assert camera.subscriber_count == 1

    window._camera_sig.emit(_solid_jpeg(QColor(230, 20, 20)))
    _pump(qapp, 0.5)

    prev = window._cam_preview
    assert prev.isVisible(), "the one-shot photo preview never appeared"
    pix = prev._img_lbl.pixmap()
    assert pix is not None and not pix.isNull(), "the photo was never decoded"
    painted = pix.toImage().pixelColor(4, 4)
    assert painted.red() > 150 and painted.green() < 120, \
        f"live frames overwrote the photo: {painted.name()}"


def test_photo_decode_is_off_the_ui_thread(qapp, window, monkeypatch):
    threads: list[int] = []
    real_from_data = QImage.fromData

    def recording_from_data(data, *args, **kwargs):
        threads.append(threading.get_ident())
        return real_from_data(data, *args, **kwargs)

    monkeypatch.setattr(ui._CameraFrameDecoder, '_decode', staticmethod(recording_from_data))
    window._camera_sig.emit(_solid_jpeg(QColor(20, 220, 60)))
    _pump(qapp, 0.4)

    assert threads, "the photo was never decoded"
    assert threading.get_ident() not in threads, "the photo froze the UI thread"


# ── the Vision stage shares the same decoded frames ─────────────────────────

def test_vision_stage_consumes_shared_frames(qapp, window, camera):
    window._toggle_command_window()
    qapp.processEvents()
    cmd = window._command_window
    cmd._save_vision_options = lambda: None
    cmd._vision_btn.setChecked(True)          # arms Vision + starts the stream
    # Arming Vision starts the stream through the host; wait for the session so
    # a silent start failure reports itself instead of a zero frame count.
    deadline = time.monotonic() + 2.0
    while window._cam_session is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert window._cam_session is not None, "arming Vision did not start the camera stream"
    # Arming also spins up the analysis worker, whose first model load can hold
    # the GIL for a while on a loaded machine, so wait for a frame instead of
    # asserting on a fixed slice of wall clock.
    deadline = time.monotonic() + 6.0
    while cmd._camera_view._frame_count == 0 and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    _pump(qapp, 0.2)

    assert cmd._camera_view._frame_count > 0, (
        "the Vision stage received no frames: "
        f"inline={bool(getattr(window, '_chat_vision_inline', False))} "
        f"armed={cmd._vision_btn.isChecked()} session={window._cam_session is not None} "
        f"pending={window._cam_mailbox._jpg is not None} "
        f"in_flight={window._cam_mailbox._in_flight} "
        f"decoded={window._frame_decoder.sequence} "
        f"float_frames={window._cam_live_lbl._frame_n} pushed={camera.sent}"
    )
    assert not cmd._camera_view._frame.isNull()
    assert camera.subscriber_count == 1
    cmd._vision_btn.setChecked(False)
    _pump(qapp, 0.2)


# ── the scan sweep and the instant-open warm-up ────────────────────────────

def test_scan_pulse_sweeps_the_visible_surface(qapp, window, camera):
    window.start_camera_stream()
    _pump(qapp, 0.4)
    assert window._cam_live_lbl.isVisible()

    window.scan_pulse()
    _pump(qapp, 0.05)
    scan = window._cam_live_lbl._scan
    assert scan._tmr.isActive() and scan.isVisible(), "the scan line never ran"
    # A sweep is 36 ticks of a 30 ms timer (~1.1 s), but Qt coalesces timer
    # ticks when the rest of the suite keeps the process busy, so a single fixed
    # 1.6 s pump turned this into a coin flip. Wait for the sweep to stop on its
    # own — the deadline still fails if it never does.
    deadline = time.monotonic() + 8.0
    while scan._tmr.isActive() and time.monotonic() < deadline:
        _pump(qapp, 0.1)
    assert not scan._tmr.isActive(), "the single sweep never ended"


def test_vision_look_closes_its_own_surfaces(qapp, window, camera):
    window.start_camera_stream()
    _pump(qapp, 0.4)
    assert window._cam_live_lbl.isVisible()

    window.vision_scan_then_close(0.4)
    _pump(qapp, 0.3)
    assert window._cam_live_lbl._scan._tmr.isActive(), "no scan on a vision look"

    _pump(qapp, 0.6)
    assert not window._cam_live_lbl.isVisible(), "the panel lingered after the look"
    assert not camera.subscriber_count, "the preview kept its stream subscription"


def test_user_armed_stage_is_never_closed_by_a_look(qapp, window, camera):
    window._toggle_command_window()
    qapp.processEvents()
    cmd = window._command_window
    cmd._save_vision_options = lambda: None
    cmd._vision_btn.setChecked(True)
    deadline = time.monotonic() + 2.0
    while window._cam_session is None and time.monotonic() < deadline:
        qapp.processEvents(); time.sleep(0.01)

    window.vision_scan_then_close(0.3)
    _pump(qapp, 0.8)
    assert cmd._vision_btn.isChecked(), "a look turned Vision mode off"
    assert window._cam_session is not None, "a look stopped the user's stream"
    cmd._vision_btn.setChecked(False)
    _pump(qapp, 0.2)


def test_warm_camera_opens_the_device_early(qapp, window, camera):
    assert camera.started == 0
    window.warm_camera()
    deadline = time.monotonic() + 2.0
    while camera.started == 0 and time.monotonic() < deadline:
        qapp.processEvents(); time.sleep(0.01)

    assert camera.started == 1, "the camera was not warmed"
    # The keep-alive consumer is what stops a preview closing the device, so a
    # later open is instant instead of a fresh 8-20 s cold start.
    assert camera.subscriber_count == 1
    window.warm_camera()                     # idempotent
    assert camera.started == 1


# ── lifecycle: start / stop / restart / close ───────────────────────────────

def test_start_stop_restart_leaves_one_capture_thread(qapp, window, camera):
    window.start_camera_stream()
    _pump(qapp, 0.3)
    assert len(_threads_named("jarvis-camera")) == 1

    window.stop_camera_stream()
    deadline = time.monotonic() + 2.0
    while _threads_named("jarvis-camera") and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert not _threads_named("jarvis-camera"), "capture thread survived stop"

    window.start_camera_stream()               # restart immediately
    _pump(qapp, 0.5)
    assert len(_threads_named("jarvis-camera")) == 1, "restart leaked a second capture thread"
    assert camera.subscriber_count == 1, "restart duplicated the subscription"


def test_close_releases_capture_and_decode_threads(qapp, window, camera):
    window.start_camera_stream()
    _pump(qapp, 0.4)
    assert _threads_named("jarvis-frame-decode")

    window.close()
    _pump(qapp, 0.4)

    assert not _threads_named("jarvis-camera"), "capture thread survived window close"
    assert not _threads_named("jarvis-frame-decode"), "decode thread survived window close"
    assert camera.stopped > 0


# ── camera manager contracts (no device needed) ─────────────────────────────

def test_subscribe_replaces_a_duplicate_handler():
    from vision.camera import CameraManager

    mgr = CameraManager(config=object())
    handler = lambda jpg: None            # noqa: E731
    mgr.subscribe(handler)
    mgr.subscribe(handler)
    assert len(mgr._subs) == 1
    other = lambda jpg: None              # noqa: E731
    mgr.subscribe(other)
    assert len(mgr._subs) == 2
    mgr.unsubscribe(handler)
    assert len(mgr._subs) == 1


def test_stop_defers_while_a_consumer_is_subscribed():
    from vision.camera import CameraManager

    mgr = CameraManager(config=object())
    mgr.subscribe(lambda jpg: None)

    mgr.stop()                            # another surface still wants frames
    assert not mgr._stop_event.is_set(), "stop must defer while subscribers remain"

    mgr.stop(force=True)                  # explicit stop / shutdown outranks it
    assert mgr._stop_event.is_set()
