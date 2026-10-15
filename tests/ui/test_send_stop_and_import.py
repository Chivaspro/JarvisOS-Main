"""The send button doubles as Stop, and a dropped file is always shown.

Two related chat-panel guarantees:

* while a request is in flight the send control becomes a Stop (interrupt)
  button, and clicking it cancels the turn instead of sending;
* a file imported or dropped is rendered in the Chat Log (card) and shown above
  the input box, so the user can see the attach actually landed.

The window-level drop path used to call the bare file registrar, which only
touched the activity rail — a file dropped anywhere outside the chat log looked
like nothing had happened.
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

from PyQt6.QtGui import QPixmap  # noqa: E402
from PyQt6.QtWidgets import QWidget  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_spec = importlib.util.spec_from_file_location("ui_impl_send_stop_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)

_LIFECYCLE = (
    "set_chat_activity", "clear_chat_activity", "_schedule_indicator_settle",
    "_cancel_indicator_settle", "_settle_typing_indicator", "_set_busy",
    "is_busy", "_on_send_or_stop", "_request_interrupt",
    "_clear_busy_after_interrupt",
)


class _RecChat:
    def __init__(self):
        self.shows = 0
        self.hides = 0

    def show_typing_indicator(self):
        self.shows += 1

    def hide_typing_indicator(self):
        self.hides += 1

    def has_typing_indicator(self):
        return self.shows > self.hides


class _FakeSend:
    def __init__(self):
        self.stop_mode = False
        self.tooltips = []

    def set_stop_mode(self, on):
        self.stop_mode = bool(on)

    def is_stop_mode(self):
        return self.stop_mode


class _FakeHost:
    def __init__(self):
        self.interrupts = 0

    def _do_interrupt(self):
        self.interrupts += 1


def _stub():
    st = QWidget()
    st.chat = _RecChat()
    st._doing_now_label = None
    st._typing_settle_timer = None
    st.send_btn = _FakeSend()
    st._host = _FakeHost()
    st._busy = False
    st.sent = []
    st._send = lambda: st.sent.append(True)
    src = ui.RefinedCommandWindow
    for name in _LIFECYCLE:
        setattr(st, name, src.__dict__[name].__get__(st, QWidget))
    return st


# ── send button: Send ⇄ Stop ────────────────────────────────────────────────

def test_activity_arms_the_stop_glyph(qapp):
    st = _stub()
    st.set_chat_activity("SEARCH · tariffs")
    assert st.is_busy() is True
    assert st.send_btn.is_stop_mode() is True


def test_quiescence_returns_to_send(qapp):
    st = _stub()
    st.set_chat_activity("Working")
    st.clear_chat_activity()          # schedules the settle
    st._typing_settle_timer.stop()
    st._settle_typing_indicator()     # the real end-of-turn path
    assert st.is_busy() is False
    assert st.send_btn.is_stop_mode() is False


def test_click_sends_when_idle_and_stops_when_busy(qapp):
    st = _stub()
    st._on_send_or_stop()
    assert st.sent == [True]
    assert st._host.interrupts == 0

    st.set_chat_activity("AUTOMATE · notepad")
    st._on_send_or_stop()
    assert st.sent == [True], "a click while busy must not also send"
    assert st._host.interrupts == 1
    assert st.is_busy() is False, "interrupting must disarm the stop state"
    assert st.send_btn.is_stop_mode() is False


def test_interrupt_clears_the_dots(qapp):
    st = _stub()
    st.set_chat_activity("AUTOMATE · notepad")
    st._request_interrupt()
    assert st.chat.hides >= 1
    assert st._host.interrupts == 1


def test_keyboard_cancel_disarms_without_re_dispatching(qapp):
    """Escape (MainWindow._do_interrupt) must not recurse through the host."""
    st = _stub()
    st.set_chat_activity("SEARCH · tariffs")
    st._clear_busy_after_interrupt()
    assert st.is_busy() is False
    assert st.send_btn.is_stop_mode() is False
    assert st.chat.hides >= 1
    assert st._host.interrupts == 0, "the keyboard path must not dispatch twice"


def test_tool_work_lights_the_dots_and_the_stop_glyph(qapp):
    """set_doing is the per-tool signal: searching/automating must show too."""
    st = _stub()
    st._doing_rows = {"SEARCH": (object(), object())}
    st._doing_generation = {}
    st._doing_effects = {"SEARCH": object()}
    st._doing_animations = {}
    st._doing_pulse_animations = {}
    try:
        ui.RefinedCommandWindow.set_doing(st, "SEARCH", "tariffs")
    except Exception:
        pass          # the row plumbing is out of scope; the latch is not
    assert st.chat.shows >= 1, "tool activity did not show the indicator"
    assert st.is_busy() is True


def test_real_send_button_switches_glyph(qapp):
    btn = ui._SendIconButton(None, size=22)
    assert btn.is_stop_mode() is False
    assert 'Send' in btn.toolTip()
    btn.set_stop_mode(True)
    assert btn.is_stop_mode() is True
    assert 'Stop' in btn.toolTip()
    assert btn.accessibleName() == 'Stop'
    pm = QPixmap(btn.size()); pm.fill()
    btn.render(pm)                     # the stop glyph must paint
    assert not pm.isNull()
    btn.set_stop_mode(False)
    assert btn.accessibleName() == 'Send'


# ── dropped / imported files are shown ──────────────────────────────────────

def test_window_drop_routes_through_the_chat_import(qapp, tmp_path):
    st = QWidget()
    f = tmp_path / "report.txt"
    f.write_text("hello", encoding="utf-8")
    seen = {}

    class _Win:
        def import_file(self, p):
            seen["chat_import"] = p

    st._command_window = _Win()
    st._register_opened_file = lambda p: seen.setdefault("bare", p)
    st._import_into_chat = ui.MainWindow.__dict__["_import_into_chat"].__get__(st, QWidget)

    assert st._import_into_chat(str(f)) is True
    assert seen["chat_import"] == str(f)
    assert "bare" not in seen, "the bare registrar was used instead of the chat"


def test_window_drop_falls_back_when_the_panel_is_gone(qapp, tmp_path):
    st = QWidget()
    f = tmp_path / "notes.md"
    f.write_text("x", encoding="utf-8")
    seen = {}
    st._command_window = None
    st._register_opened_file = lambda p: seen.setdefault("bare", p)
    st._import_into_chat = ui.MainWindow.__dict__["_import_into_chat"].__get__(st, QWidget)

    assert st._import_into_chat(str(f)) is False
    assert "bare" not in seen          # the caller decides the fallback
