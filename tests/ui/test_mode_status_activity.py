"""Offscreen checks of the chat panel's Gemini ⇄ UI TARS mode readout.

The mode is no longer a manual button on the chat panel: J.A.R.V.I.S. switches
it himself through the ``brain_mode`` tool, so "switch to UI TARS" / "back to
Gemini" is a spoken command. What the panel still owns — and what these tests
pin — is the ACTIVITY readout that names the live mode:

* the readout starts in GEMINI and names UI TARS once the mode changes,
* it accepts both booleans and the string states main.py forwards,
* it is styled and carries a tooltip,
* and no manual toggle button is left behind on the panel.

Constructing the whole ``MainWindow`` offscreen is unsafe in this sandbox (the
native vision/MediaPipe stack), so — exactly as ``test_laya_status_activity``
does — the rendering method is driven on a ``RefinedCommandWindow`` instance
that carries only the widgets it touches.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QLabel  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load the app module under an alias (tests/ui is itself a package named 'ui').
_spec = importlib.util.spec_from_file_location("ui_impl_mode_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


def _command_window():
    """A RefinedCommandWindow carrying only the widgets the mode methods touch."""
    cmd = ui.RefinedCommandWindow.__new__(ui.RefinedCommandWindow)
    cmd._mode_status_label = QLabel('')
    cmd._ui_tars_mode = False
    return cmd


def test_starts_in_gemini_mode(qapp):
    cmd = _command_window()
    cmd.set_mode_status(False)
    assert 'GEMINI' in cmd._mode_status_label.text()
    assert 'UI TARS' not in cmd._mode_status_label.text()


def test_ui_tars_mode_is_named_on_the_status_line(qapp):
    cmd = _command_window()
    cmd.set_mode_status(True)
    assert 'UI TARS' in cmd._mode_status_label.text()
    assert cmd._mode_status_state == 'ui_tars'
    assert cmd._ui_tars_mode is True


def test_string_states_are_accepted(qapp):
    cmd = _command_window()
    cmd.set_mode_status('UI TARS')
    assert cmd._ui_tars_mode is True
    cmd.set_mode_status('gemini')
    assert cmd._ui_tars_mode is False


def test_status_line_is_styled_and_has_a_tooltip(qapp):
    cmd = _command_window()
    cmd.set_mode_status(True)
    assert cmd._mode_status_label.styleSheet().strip() != ''
    assert 'UI TARS' in (cmd._mode_status_label.toolTip() or '')


def test_no_manual_mode_button_is_left_on_the_panel():
    """The switch is voice-only now: J.A.R.V.I.S. flips it through the
    brain_mode tool, so neither the widget class nor its click handler may
    remain on the chat panel."""
    assert not hasattr(ui, '_ModeToggleButton')
    assert not hasattr(ui.RefinedCommandWindow, '_toggle_ui_tars_mode')
    assert not hasattr(ui.RefinedCommandWindow, 'mode_btn')
    source = (ROOT / 'ui.py').read_text(encoding='utf-8')
    assert 'mode_btn' not in source
    assert '_toggle_ui_tars_mode' not in source
