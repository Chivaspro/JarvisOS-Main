"""Vision Panel progress indicators must reflect REAL work (prompt §12-§15, §20).

Two guards this file pins down:

* The camera-loading and Vision-Scan indicators are a stage-driven percentage
  bar, not the old 3x3 square animation. The bar only ever reaches 100 % when
  the operation genuinely completed, and it terminates on failure/cancellation
  instead of sticking at an arbitrary number.
* Percentages come from the runtime's own stage tables, so a QTimer cannot
  invent progress and an old operation cannot move the current bar.
"""
from __future__ import annotations

import time

from .test_camera_pipeline import (  # noqa: F401
    _pump, camera, qapp, window,
)
from .test_camera_pipeline import ui


def _vision_stage(qapp, window):
    window._toggle_command_window()
    _pump(qapp, 0.2)
    cmd = window._command_window
    cmd._save_vision_options = lambda: None
    return cmd._camera_view


# ── the square loader is gone ───────────────────────────────────────────────

def test_loader_is_a_percentage_bar_not_a_square_animation(qapp, window):
    cam = _vision_stage(qapp, window)
    loader = cam._loader
    assert isinstance(loader, ui._VisionLoader)
    # A progress plate is wide and short; the old animation was a 96x96 square.
    assert loader.width() > 2 * loader.height(), "expected a bar, not a square"
    for needed in ('set_stage', 'finish', 'halt', 'begin', 'set_title'):
        assert callable(getattr(loader, needed)), f"progress API missing {needed}"
    # The old hand-authored square animation must not come back.
    src = (ui.__file__ and open(ui.__file__, encoding='utf-8').read()) or ''
    assert 'Three-by-three' not in src, "the 3x3 square loader is still in the tree"


# ── camera init progress is stage driven, never time driven ─────────────────

def test_camera_progress_follows_real_stages(qapp, window):
    cam = _vision_stage(qapp, window)
    cam.begin_loading()
    _pump(qapp, 0.05)
    assert cam._loader._value == 0, "loading must start at a real 0 %"

    seen = []
    for stage, pct in (('preparing', 10), ('discovering', 20), ('opening', 30),
                       ('configured', 50), ('streaming', 85)):
        cam.report_progress(stage, pct)
        _pump(qapp, 0.05)
        seen.append(cam._loader._value)

    assert seen == [10, 20, 30, 50, 85], f"stages did not map to real values: {seen}"
    assert all(later >= earlier for earlier, later in zip(seen, seen[1:])), \
        "progress jumped backwards"
    # 85 % is the honest ceiling before a frame exists: 100 % is earned, not shown.
    assert cam._loader._value < 100, "100 % shown before any frame arrived"


def test_camera_reaches_100_only_when_a_frame_lands(qapp, window):
    cam = _vision_stage(qapp, window)
    from PyQt6.QtGui import QImage
    cam.begin_loading()
    cam.report_progress('configured', 50)
    _pump(qapp, 0.05)
    assert cam._loading and cam._loader._value == 50

    img = QImage(64, 48, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0xFF102030)
    cam.set_frame_image(img)
    _pump(qapp, 0.05)

    assert cam._loader._value == 100, "a real frame must complete the indicator"
    assert not cam._loading
    assert cam._status == 'VISION CAMERA · LIVE'


def test_percentage_timer_cannot_invent_camera_progress(qapp, window):
    """A quiet pipeline stays quiet — no animation loop nudges the number."""
    cam = _vision_stage(qapp, window)
    cam.begin_loading()
    cam.report_progress('preparing', 10)
    _pump(qapp, 0.05)
    before = cam._loader._value
    _pump(qapp, 1.2)                      # several shimmer frames elapse
    assert cam._loader._value == before, \
        "the percentage moved without the runtime reporting a stage"


# ── Vision Scan progress ────────────────────────────────────────────────────

def test_vision_scan_reports_its_real_stages(qapp, window):
    cam = _vision_stage(qapp, window)
    cam.begin_vision_scan()
    _pump(qapp, 0.05)
    assert cam._scan_progress is True
    assert 'SCANNING' in cam._loader._title.upper()

    steps = (('capturing', 15), ('captured', 35), ('detecting', 55),
             ('interpreting', 70))
    values = []
    for stage, pct in steps:
        cam.report_progress(stage, pct)
        _pump(qapp, 0.05)
        values.append(cam._loader._value)
    assert values == [15, 35, 55, 70], f"scan stages mis-mapped: {values}"

    cam.report_progress('finalizing', 100)
    _pump(qapp, 0.05)
    assert cam._loader._value == 100, "a finished scan must show 100 %"
    _pump(qapp, 1.0)
    assert cam._loader.isHidden(), "the scan indicator never retired"
    assert cam._scan_progress is False


def test_scan_failure_terminates_the_indicator(qapp, window):
    cam = _vision_stage(qapp, window)
    cam.begin_vision_scan()
    cam.report_progress('capturing', 15)
    _pump(qapp, 0.05)
    cam.report_progress('failed', 0)
    _pump(qapp, 0.05)
    assert cam._loader._failed is True, "a failed scan must show a failed state"
    assert cam._loader._value != 100, "a failed scan must not claim 100 %"
    _pump(qapp, 1.8)
    assert cam._loader.isHidden(), "a failed scan left the bar stuck"


# ── lifecycle: a retired operation cannot move today's bar ──────────────────

def test_stale_progress_from_a_retired_operation_is_dropped(qapp, window):
    cam = _vision_stage(qapp, window)
    cam.begin_vision_scan()
    stale_gen = cam._progress_op
    cam.report_progress('detecting', 55)
    _pump(qapp, 0.05)
    assert cam._loader._value == 55

    # A newer operation supersedes the one that produced the stage above.
    cam.begin_vision_scan()
    _pump(qapp, 0.05)
    assert cam._progress_op != stale_gen
    cam._apply_camera_progress(f'{stale_gen}|interpreting', 70)
    _pump(qapp, 0.05)
    assert cam._loader._value == 0, \
        "a finished/cancelled operation drove the current indicator"


def test_closing_the_stream_retires_the_loading_indicator(qapp, window):
    cam = _vision_stage(qapp, window)
    cam.begin_loading()
    cam.report_progress('preparing', 10)
    _pump(qapp, 0.05)
    assert cam._loader._value == 10

    gen = cam._progress_op
    cam.end_stream()
    _pump(qapp, 0.05)
    assert not cam._loader._tmr.isActive(), "a closed panel kept animating its bar"
    assert cam._progress_op != gen, "closing must retire the open attempt"

    # The capture thread finishing its attempt late must be ignored.
    cam._apply_camera_progress(f'{gen}|configured', 50)
    _pump(qapp, 0.05)
    assert cam._loader._value == 10, "a retired attempt revived the indicator"



def test_loader_does_not_animate_while_hidden(qapp, window):
    cam = _vision_stage(qapp, window)
    loader = cam._loader
    loader.hide_progress()
    assert not loader._tmr.isActive(), \
        "the progress timer kept ticking on a hidden widget"
    loader.set_stage('opening', 30)
    assert loader._tmr.isActive()
    loader.hide_progress()
    assert not loader._tmr.isActive()


# ── the stage tables are the single source of truth ─────────────────────────

def test_progress_percentages_come_from_the_runtime_stage_tables(qapp, window):
    from vision.camera import CAMERA_STAGES, VISION_SCAN_STAGES, STAGE_PERCENT

    camera_pcts = [p for _s, p in CAMERA_STAGES]
    scan_pcts = [p for _s, p in VISION_SCAN_STAGES]
    assert camera_pcts == sorted(camera_pcts), "camera stages must advance monotonically"
    assert scan_pcts == sorted(scan_pcts), "scan stages must advance monotonically"
    assert camera_pcts[-1] == 100 and scan_pcts[-1] == 100
    assert STAGE_PERCENT['preparing'] == 10
    assert STAGE_PERCENT['capturing'] == 15

    # The widget refuses to display 100 % from a stage alone; only finish()
    # (driven by real completion) is allowed to.
    cam = _vision_stage(qapp, window)
    cam.begin_loading()
    cam.report_progress('ready', 100)
    _pump(qapp, 0.05)
    assert cam._loader._value < 100, "a stage callback alone must not claim completion"


def test_camera_manager_reports_stages_as_init_advances(tmp_path):
    """The manager emits each stage exactly once, from real progress."""
    from vision.camera import CameraManager

    mgr = CameraManager(config=object())
    events = []
    mgr.set_progress_callback(lambda stage, pct: events.append((stage, pct)))

    mgr._report_stage('preparing')
    mgr._report_stage('preparing')          # duplicate must be ignored
    mgr._report_stage('opening')
    mgr._report_stage('not_a_real_stage')
    assert [s for s, _p in events] == ['preparing', 'opening'], events

    # A fresh attempt re-arms the bookkeeping so a retry can report again.
    mgr._progress_done = set()
    mgr._report_stage('preparing')
    assert [s for s, _p in events][-1] == 'preparing'

    mgr._report_failure('device_unavailable')
    assert events[-1][0] == 'failed:device_unavailable'
    mgr.set_progress_callback(None)
    mgr._report_stage('opening')            # detached: nothing raised, nothing sent
    assert events[-1][0] == 'failed:device_unavailable'


def test_camera_index_cache_is_used_without_a_discovery_sweep(monkeypatch, tmp_path):
    """Trusting a cached index is what removes the multi-second cold open."""
    import json
    import vision.camera as cam_mod

    cache = tmp_path / "camera_probe_cache.json"
    cache.write_text(json.dumps({"camera_index": 3}), encoding="utf-8")
    monkeypatch.setattr(cam_mod, "_INDEX_CACHE_PATH", str(cache))

    probed = []
    monkeypatch.setattr(
        cam_mod, "auto_detect_camera_index",
        lambda *a, **k: probed.append(1) or 0)

    class _Cfg:
        camera_index = "auto"
        as_dict = {"camera_index": "auto"}

        @staticmethod
        def effective_camera_index():
            return -1

    mgr = cam_mod.CameraManager(config=_Cfg())
    assert mgr._resolve_camera_index() == 3, "the cached index must be trusted"
    assert not probed, "a full discovery sweep ran even though a cached index existed"


def test_auto_detect_records_a_dark_camera_instead_of_discarding_it(monkeypatch):
    """A covered lens is still a camera; the old probe threw that answer away."""
    import vision.camera as cam_mod

    class _FakeNp:
        @staticmethod
        def mean(_frame):
            return 2.0          # below the 8.0 "lit" threshold → dark

    class _Frame:
        size = 12345            # read_frame() rejects empty buffers

    class _Cap:
        def isOpened(self):
            return True

        def read(self):
            return True, _Frame()

        def release(self):
            pass

    class _OnlyOneDevice:
        """Only index 2 is a real camera, so discovery must find and keep it."""

        def __call__(self, index, *_a, **_k):
            return _Cap() if index == 2 else _Dead()

    class _Dead:
        def isOpened(self):
            return False

        def read(self):
            return False, None

        def release(self):
            pass

    monkeypatch.setattr(cam_mod, "_CV2", True)
    monkeypatch.setattr(cam_mod, "np", _FakeNp)
    monkeypatch.setattr(cam_mod.cv2, "VideoCapture", _OnlyOneDevice())
    # Force a real discovery sweep (no cached answer to lean on).
    monkeypatch.setattr(cam_mod, "_load_index_cache", lambda: None)
    written = {}
    monkeypatch.setattr(cam_mod, "_save_index_cache",
                        lambda idx: written.setdefault("i", idx))


    usable, lit = cam_mod.probe_camera_result(2, quick=True)
    assert usable is True and lit is False
    assert cam_mod.auto_detect_camera_index(None) == 2
    assert written.get("i") == 2, "the discovered index was not cached for next boot"

