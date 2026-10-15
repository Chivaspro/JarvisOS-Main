"""Typing-indicator request lifecycle (TASK 2) — offscreen, no live model.

The dispatcher calls clear_chat_activity() after EVERY tool. The old code hid
the dots on each of those calls, so the indicator vanished in the gaps of a
multi-step request and could flicker. The fix defers the hide: a tool-finishing
clear schedules a short "settle", any new activity cancels it, and the dots only
fade once the request has genuinely gone quiet (which also guarantees they can
never stick).

These tests bind the real RefinedCommandWindow lifecycle methods onto a stub
with a recording chat surface, so the timing logic is exercised deterministically
without building the whole HUD.
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

from PyQt6.QtWidgets import QWidget  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_typing_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


class _RecChat:
    def __init__(self):
        self.shows = 0
        self.hides = 0

    def show_typing_indicator(self):
        self.shows += 1

    def hide_typing_indicator(self):
        self.hides += 1


class _FakeSend:
    """The send button's Stop state (the same control doubles as interrupt)."""

    def __init__(self):
        self.stop_mode = False

    def set_stop_mode(self, on):
        self.stop_mode = bool(on)


def _make_stub():
    st = QWidget()
    st.chat = _RecChat()
    st._doing_now_label = None
    st._typing_settle_timer = None
    st.send_btn = _FakeSend()
    st._busy = False
    src = ui.RefinedCommandWindow
    for name in ("set_chat_activity", "clear_chat_activity",
                 "_schedule_indicator_settle", "_cancel_indicator_settle",
                 "_settle_typing_indicator", "_set_busy", "is_busy"):
        setattr(st, name, src.__dict__[name].__get__(st, QWidget))
    return st


def test_activity_shows_indicator(qapp):
    st = _make_stub()
    st.set_chat_activity("Scanning document")
    assert st.chat.shows == 1
    assert st.chat.hides == 0


def test_tool_completion_does_not_prematurely_hide(qapp):
    """A tool finishing (clear) must NOT drop the dots immediately."""
    st = _make_stub()
    st.set_chat_activity("Reading")
    st.clear_chat_activity()          # tool #1 done, but turn continues
    assert st.chat.hides == 0, "indicator hid the moment one tool finished"
    assert st._typing_settle_timer is not None
    assert st._typing_settle_timer.isActive(), "settle should be scheduled, not fired"


def test_next_activity_cancels_pending_hide(qapp):
    """A second tool starting within the window keeps the dots lit continuously."""
    st = _make_stub()
    st.set_chat_activity("Reading")
    st.clear_chat_activity()          # schedules hide
    st.set_chat_activity("Searching")  # cancels it
    assert not st._typing_settle_timer.isActive(), "settle was not cancelled by new activity"
    assert st.chat.hides == 0


def test_indicator_hides_after_quiescence(qapp):
    """No further activity -> the settle fires -> dots disappear (never stuck)."""
    st = _make_stub()
    st.set_chat_activity("Working")
    st.clear_chat_activity()          # end of the last step
    st._typing_settle_timer.stop()
    st._typing_settle_timer.setInterval(120)
    st._typing_settle_timer.start()
    QTest.qWait(400)
    assert st.chat.hides == 1, "indicator never hid once the request went quiet"


def test_empty_activity_schedules_settle(qapp):
    """set_chat_activity('') routes through clear -> deferred, not instant hide."""
    st = _make_stub()
    st.set_chat_activity("Working")
    st.set_chat_activity("")          # treated as a clear
    assert st.chat.hides == 0, "empty activity hid the dots instantly instead of settling"
    assert st._typing_settle_timer.isActive()


def test_activity_arms_the_stop_button(qapp):
    """The dots and the Stop glyph are the same "J.A.R.V.I.S. is busy" state."""
    st = _make_stub()
    st.set_chat_activity("SEARCH · tariffs")
    assert st.is_busy() is True
    assert st.send_btn.stop_mode is True


def test_settle_disarms_the_stop_button(qapp):
    st = _make_stub()
    st.set_chat_activity("Working")
    st._settle_typing_indicator()
    assert st.is_busy() is False
    assert st.send_btn.stop_mode is False
    assert st.chat.hides == 1
