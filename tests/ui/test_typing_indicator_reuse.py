"""The chat typing indicator must exist in exactly one copy, and be seen.

The dots are what tells the user J.A.R.V.I.S. is working, so two properties
have to hold no matter how the panel is being used:

* there is never more than one typing row (the old code decided whether to
  reuse the row by asking ``isVisible()``, which is False whenever an ancestor
  is hidden — so activity set while the panel was tucked away created a *second*
  row and orphaned the first one; the strays are why the indicator could seem to
  appear late, stack up, or vanish on its own);
* a reply whose first text chunk lands in the same event-loop turn as the
  indicator does not hide it before a human could ever have seen it.

These drive the real ``_RoundedChatLog`` offscreen.
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

from PyQt6.QtTest import QTest  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_spec = importlib.util.spec_from_file_location("ui_impl_typing_dots_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


class _VisibleLog(ui._RoundedChatLog):
    """A chat log that reports itself on screen (the offscreen default never is)."""

    def isVisible(self) -> bool:      # noqa: N802 - Qt spelling
        return True


def _typing_rows(log):
    return [r for r in log._rows if isinstance(r, ui._TypingIndicatorRow)]


def test_showing_repeatedly_keeps_exactly_one_row(qapp):
    log = ui._RoundedChatLog()
    log.show_typing_indicator()
    log.show_typing_indicator()
    log.show_typing_indicator()
    assert len(_typing_rows(log)) == 1
    assert log.has_typing_indicator()


def test_activity_while_the_panel_is_hidden_does_not_duplicate(qapp):
    """Regression: a hidden parent makes isVisible() False for a shown row.

    The old implementation treated that as "there is no row" and added another.
    """
    log = ui._RoundedChatLog()
    log.hide()                        # parent hidden -> row.isVisible() is False
    for _ in range(4):
        log.show_typing_indicator()
    assert log._typing_row.isVisible() is False
    assert len(_typing_rows(log)) == 1, "the hidden row was duplicated"


def test_a_stray_row_is_purged(qapp):
    log = ui._RoundedChatLog()
    stray = ui._TypingIndicatorRow(log._page)
    log._insert_before_stretch(stray)          # simulate the old orphaning
    log.show_typing_indicator()
    assert len(_typing_rows(log)) == 1
    assert stray not in log._rows


def test_hide_removes_every_typing_row(qapp):
    log = ui._RoundedChatLog()
    log.show_typing_indicator()
    log._insert_before_stretch(ui._TypingIndicatorRow(log._page))
    log.hide_typing_indicator()
    assert _typing_rows(log) == []
    assert not log.has_typing_indicator()


def test_show_after_hide_recreates_one_row(qapp):
    log = ui._RoundedChatLog()
    log.show_typing_indicator()
    log.hide_typing_indicator()
    log.show_typing_indicator()
    assert len(_typing_rows(log)) == 1
    assert log.has_typing_indicator()


def test_clear_leaves_no_typing_row_behind(qapp):
    log = ui._RoundedChatLog()
    log.show_typing_indicator()
    log.clear()
    assert _typing_rows(log) == []


def test_a_flash_short_indicator_is_still_seen(qapp):
    """Hide is deferred while the row has not been on screen long enough."""
    log = _VisibleLog()
    log.show_typing_indicator()
    log.hide_typing_indicator()
    assert log.has_typing_indicator(), "the dots were removed the same instant"
    QTest.qWait(ui._RoundedChatLog._TYPING_MIN_VISIBLE_MS + 300)
    assert not log.has_typing_indicator()


def test_new_activity_cancels_a_deferred_hide(qapp):
    log = _VisibleLog()
    log.show_typing_indicator()
    log.hide_typing_indicator()        # deferred, not applied
    log.show_typing_indicator()        # a new step started
    QTest.qWait(ui._RoundedChatLog._TYPING_MIN_VISIBLE_MS + 300)
    assert log.has_typing_indicator()


def test_offscreen_hide_is_immediate(qapp):
    """Headless (and hidden-panel) behaviour stays deterministic."""
    log = ui._RoundedChatLog()
    log.show_typing_indicator()
    log.hide_typing_indicator()
    assert not log.has_typing_indicator()
