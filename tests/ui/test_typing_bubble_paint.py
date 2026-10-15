"""Typing-bubble paint — offscreen.

Regression for the auto-heal fault captured at ui.py:10869:

    TypeError: paintEvent(self, a0: QPaintEvent|None): first argument of
    unbound method must have type 'QFrame'

`_TypingIndicatorRow` bound its painter with
`self._paint_bubble.__get__(self._bubble, QFrame)`, which is a no-op on an
already-bound method: the handler stayed bound to the ROW (a QWidget), so
every repaint called `QFrame.paintEvent(row, event)` and raised — one captured
fault per frame while the dots were visible. The handler must be bound to the
bubble itself and the decorative paint must never escape into the sentry.
"""
from __future__ import annotations

import importlib.util
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

from PyQt6.QtGui import QPixmap  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_bubble_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def _typing_bubble(qapp):
    log = ui._RoundedChatLog()
    log.resize(420, 220)
    log.show_typing_indicator()
    qapp.processEvents()
    row = log._typing_row
    assert row is not None
    # The log must stay referenced: a top-level widget without a Python
    # reference is garbage-collected together with its children.
    return log, row, row._bubble


def test_paint_handler_is_bound_to_the_bubble_itself(qapp):
    _log, _row, bubble = _typing_bubble(qapp)
    handler = bubble.paintEvent
    assert getattr(handler, '__self__', None) is bubble, \
        "the paint handler must be bound to the bubble, not the row"


def test_repainting_the_typing_bubble_raises_nothing(qapp):
    _log, _row, bubble = _typing_bubble(qapp)
    seen: list[BaseException] = []
    old_hook = sys.excepthook
    sys.excepthook = lambda kind, value, tb: seen.append(value)
    try:
        for _ in range(3):
            bubble.render(QPixmap(bubble.size()))
        qapp.processEvents()
    finally:
        sys.excepthook = old_hook
    assert not seen, f"paint raised into the exception hook: {seen!r}"


def test_paint_still_uses_the_real_type_check_path(qapp):
    """A QFrame receives the base paint call without a TypeError."""
    _log, _row, bubble = _typing_bubble(qapp)
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QPaintEvent
    from PyQt6.QtWidgets import QFrame
    try:
        QFrame.paintEvent(bubble, QPaintEvent(QRect(0, 0, 10, 10)))
    except TypeError as exc:
        pytest.fail(f"QFrame.paintEvent(bubble) type check failed: {exc}")
