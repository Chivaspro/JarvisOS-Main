"""Floating-panel header drag — offscreen.

The repair "HUD WINDOW" (and every floating panel) advertises header drag via
the `_jarvis_panel_drag` property, but the header's title/subtitle labels
consumed the mouse press, so only the icon and the bar's empty gaps moved the
window. The labels are now transparent for mouse events and the press reaches
the bar, which MainWindow.eventFilter turns into a window move.
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
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QFrame, QLabel  # noqa: E402

# A real MainWindow with an isolated config and a mocked camera. The camera/
# qapp fixtures come from test_camera_pipeline, as in test_image_view_panel.
from .test_camera_pipeline import _pump, camera, qapp, ui  # noqa: F401,E402
from .test_image_view_panel import window  # noqa: F401,E402


def _header(panel) -> QFrame:
    bar = panel.findChild(QFrame, 'PanelHeader')
    assert bar is not None, "the panel must carry the shared header rail"
    return bar


def test_repair_panel_header_is_tagged_for_drag(window):
    panel = window._repair_panel
    assert panel is not None
    bar = _header(panel)
    assert bar.property('_jarvis_panel_drag') is True


def test_header_text_labels_do_not_swallow_the_press(window):
    bar = _header(window._repair_panel)
    labels = [lbl for lbl in bar.findChildren(QLabel) if lbl.text()]
    assert labels, "the header is expected to show its title text"
    for lbl in labels:
        assert lbl.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents), \
            f"{lbl.text()!r} would consume the drag press"


def _mouse(kind, local: QPoint, glob: QPoint, *, buttons, button=Qt.MouseButton.NoButton):
    return QMouseEvent(kind, QPointF(local), QPointF(glob), button, buttons,
                       Qt.KeyboardModifier.NoModifier)


def test_dragging_the_header_moves_the_panel(window, qapp):
    panel = window._repair_panel
    panel.show()
    qapp.processEvents()
    bar = _header(panel)
    start = panel.pos()

    press_global = bar.mapToGlobal(QPoint(4, 4))
    window.eventFilter(bar, _mouse(QEvent.Type.MouseButtonPress, QPoint(4, 4),
                                   press_global,
                                   buttons=Qt.MouseButton.LeftButton,
                                   button=Qt.MouseButton.LeftButton))
    moved_global = press_global + QPoint(48, 32)
    handled = window.eventFilter(bar, _mouse(QEvent.Type.MouseMove, QPoint(52, 36),
                                             moved_global,
                                             buttons=Qt.MouseButton.LeftButton))
    assert handled is True, "the move past the drag threshold must be consumed"
    assert panel.pos() != start, "the panel did not move with the drag"
    assert panel.pos() == start + QPoint(48, 32)

    window.eventFilter(bar, _mouse(QEvent.Type.MouseButtonRelease, QPoint(52, 36),
                                   moved_global,
                                   buttons=Qt.MouseButton.NoButton,
                                   button=Qt.MouseButton.LeftButton))
    panel.hide()
    qapp.processEvents()
