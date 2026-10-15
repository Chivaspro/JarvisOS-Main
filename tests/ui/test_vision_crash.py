"""Vision-crash regression tests (offscreen, no physical camera).

The bug: a thin, low-quality detection (a pair of scissors held to the camera)
could carry a non-finite confidence / box, and ``_InlineVisionView.paintEvent``
formatted it with ``int(float(conf) * 100)`` OUTSIDE the per-entry guard. In
PyQt6 an exception raised inside a reimplemented ``paintEvent`` becomes
``qFatal`` + abort — the whole app dies, mid-scan, the instant a box is drawn.

These tests pin the two layers of the fix:

* The pure math helpers are non-finite safe.
* A view fed a NaN detection paints without raising (via ``grab()`` = a real
  Qt paint event), and the analyzer drops degenerate boxes before they ever
  reach the widget.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: E402,F401

from PyQt6.QtGui import QColor, QImage  # noqa: E402

# Reuse the ONE already-loaded copy of ui.py (the same module the other camera/
# vision tests import). Loading ui.py under another alias would create a second
# full widget module in the session and destabilise the fragile offscreen Qt
# process for unrelated tests.
from .test_camera_pipeline import ui  # noqa: E402

NAN = float("nan")
INF = float("inf")


# ── the pure math that used to raise ───────────────────────────────────────

def test_pct_is_nonfinite_and_none_safe():
    assert ui._pct(NAN) == 0
    assert ui._pct(INF) == 0
    assert ui._pct(-INF) == 0
    assert ui._pct(None) == 0
    assert ui._pct("nonsense") == 0
    assert ui._pct(0.75) == 75
    assert ui._pct(1.4) == 100      # clamped
    assert ui._pct(-0.2) == 0


def test_finite_box_rejects_degenerate():
    assert ui._finite_box((0.1, 0.2, 0.3, 0.4))
    assert not ui._finite_box((0.1, NAN, 0.3, 0.4))
    assert not ui._finite_box((INF, 0.2, 0.3, 0.4))
    assert not ui._finite_box((0.1, "x", 0.3, 0.4))
    assert not ui._finite_box((0.1, 0.2))            # too short


# ── the widget must survive a bad detection box ─────────────────────────────

def _view(qapp):
    view = ui._InlineVisionView()
    view.resize(240, 180)
    img = QImage(320, 240, QImage.Format.Format_RGB32)
    img.fill(QColor(20, 40, 60))
    view._frame = img
    view._loading = False
    view._object_detection = True
    return view


def test_paint_survives_nan_object_box(qapp):
    view = _view(qapp)
    # First box is the scissors case (NaN confidence, hairline width); the
    # second is a good box that must still get painted without aborting.
    view._object_labels = [
        (0.5, 0.5, 0.002, 0.4, "SCISSORS", NAN, 3),
        (0.4, 0.4, 0.1, 0.1, "CUP", 0.9, 1),
    ]
    view.grab()   # a real Qt paint event: previously qFatal, now must pass


def test_paint_survives_all_nan_boxes(qapp):
    view = _view(qapp)
    view._object_labels = [(0.5, 0.5, NAN, 0.4, "SCISSORS", 0.7, -1)]
    view.set_faces([(0.3, 0.3, NAN, 0.2)])   # malformed faces are dropped
    assert view._faces == []
    view.grab()


def test_set_faces_drops_malformed_entries(qapp):
    view = _view(qapp)
    view.set_faces([
        (0.1, 0.1, 0.2, 0.2),        # good
        (0.1, NAN, 0.2, 0.2),         # non-finite → dropped
        (0.1, 0.1),                    # too short → dropped
        ("x", "y", "z", "w"),          # non-numeric → dropped
        None,                          # junk → dropped
    ])
    assert view._faces == [(0.1, 0.1, 0.2, 0.2)]


# ── the analyzer must never forward a degenerate box to the UI ──────────────

class _Rect:
    def __init__(self, x, y, w, h):
        self.x, self.y, self.w, self.h = x, y, w, h


class _Det:
    def __init__(self, x, y, w, h, label, conf, tid=-1):
        self.rect = _Rect(x, y, w, h)
        self.label = label
        self.confidence = conf
        self.tracker_id = tid


class _FakeDetector:
    def __init__(self, dets):
        self._dets = dets

    def set_runtime_options(self, **_kw):
        pass

    def detect(self, _bgr):
        return self._dets


def test_run_objects_drops_nonfinite_detections():
    analyzer = ui._VisionAnalyzer()
    analyzer._detector = _FakeDetector([
        _Det(0.5, 0.5, NAN, 0.4, "scissors", NAN),   # degenerate → dropped
        _Det(0.4, 0.4, 0.1, 0.1, "cup", 0.9, 2),     # good → kept
    ])
    analyzer._detector_tried = True
    out = analyzer._run_objects(object())
    assert len(out) == 1
    assert out[0][4] == "CUP"
    assert all(ui._finite_box(row[:4]) for row in out)
