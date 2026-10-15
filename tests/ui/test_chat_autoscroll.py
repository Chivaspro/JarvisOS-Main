"""Chat log auto-scroll (TASK 4) — offscreen.

The old path scheduled a fresh timer on every append, and that timer scheduled
ANOTHER one — so a burst of inserts fired many competing "scroll to bottom"
calls (the reported jitter/jumping). The fix coalesces every request into a
single deferred scroll that runs after the layout has placed the newest row.

Verifies:
* A burst of appends queues exactly ONE pending scroll (no duplicate timers).
* After the event loop turn, the view is genuinely at the bottom.
* The newest message ends up in the visible range.
* Resizing does NOT force a scroll (manual scrolling is preserved).
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

pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_scroll_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def test_burst_of_appends_coalesces_to_one_scroll(qapp):
    log = ui._RoundedChatLog()
    log.resize(400, 260)

    fired = []
    real = ui._RoundedChatLog._scroll_bottom

    def spy(self):
        fired.append(1)
        return real(self)

    ui._RoundedChatLog._scroll_bottom = spy
    try:
        for i in range(6):
            log.append_message('J.A.R.V.I.S.', f'line {i} ' + 'x' * 60)
        # All six inserts share a single queued scroll.
        assert log._scroll_pending is True
        assert len(fired) == 0, "scroll must be deferred, not run synchronously"

        qapp.processEvents()
        # The deferred callback ran exactly once and cleared the pending flag.
        assert len(fired) == 1, f"expected one coalesced scroll, got {len(fired)}"
        assert log._scroll_pending is False
    finally:
        ui._RoundedChatLog._scroll_bottom = real


def test_view_rests_at_bottom_after_update(qapp):
    log = ui._RoundedChatLog()
    log.resize(400, 200)
    for i in range(10):
        log.append_message('J.A.R.V.I.S.', f'row {i} ' + 'y' * 80)
    qapp.processEvents()
    bar = log.verticalScrollBar()
    assert bar.value() == bar.maximum(), "the newest content is not visible"


def test_resize_does_not_force_scroll(qapp):
    log = ui._RoundedChatLog()
    log.resize(400, 200)
    for i in range(8):
        log.append_message('You', f'msg {i}')
    qapp.processEvents()
    # Simulate the user reading history: scroll up.
    bar = log.verticalScrollBar()
    bar.setValue(0)
    qapp.processEvents()
    log.resize(420, 300)
    qapp.processEvents()
    # A resize re-lays rows but must NOT yank the reader back to the bottom.
    assert bar.value() == 0, "resize forced a scroll, breaking manual reading"


def test_new_message_returns_view_to_bottom_after_scrolling_up(qapp):
    """The user requirement: every new message (sent or received) pins the
    view to the bottom, even if the reader had scrolled up."""
    log = ui._RoundedChatLog()
    log.resize(400, 200)
    for i in range(12):
        log.append_message('J.A.R.V.I.S.', f'row {i} ' + 'y' * 80)
    qapp.processEvents()
    bar = log.verticalScrollBar()
    bar.setValue(0)
    qapp.processEvents()
    assert bar.value() == 0, "precondition: reader is up in history"

    log.append_message('You', 'Hi Jarvis')
    qapp.processEvents()
    qapp.processEvents()   # covers the bounded late-layout recheck
    assert bar.value() == bar.maximum(), \
        "a new message must bring the chat log back to the bottom"

    log.append_message('J.A.R.V.I.S.', 'At your service. ' + 'z' * 120)
    qapp.processEvents()
    qapp.processEvents()
    assert bar.value() == bar.maximum(), \
        "a received message must keep the chat log at the bottom"

