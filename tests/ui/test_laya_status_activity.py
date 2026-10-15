"""Offscreen check of the ACTIVITY ``LAYA:`` runtime-status line.

The spec (ReferenceDashboard §12-14) requires a small, live indicator that
reflects whether Laya is *actually used for the current activity* — not whether
it is enabled in Settings — and that updates as the classifier decides.

Constructing the whole ``MainWindow`` offscreen crashes in this sandbox (the
native vision/MediaPipe stack, as ``test_camera_pipeline`` also warns), so the
rendering method is exercised directly on a ``RefinedCommandWindow`` instance
that carries a real ``QLabel`` — same code path the live signal drives, minus
the heavy constructor.
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
_spec = importlib.util.spec_from_file_location("ui_impl_laya_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


def _command_window_with_label():
    """A RefinedCommandWindow with only the label the status method touches."""
    cmd = ui.RefinedCommandWindow.__new__(ui.RefinedCommandWindow)
    cmd._laya_status_label = QLabel('')
    cmd._laya_status_state = 'idle'
    return cmd, cmd._laya_status_label


def test_starts_honest_when_disabled(qapp):
    """A fresh panel must NOT claim Laya is active just because it is enabled."""
    cmd, lbl = _command_window_with_label()
    cmd.set_laya_status('NOT USED', 'disabled in Settings')
    assert 'NOT USED' in lbl.text()
    assert 'ACTIVE' not in lbl.text()


def test_lifecycle_active_then_complete_then_not_used(qapp):
    cmd, lbl = _command_window_with_label()

    cmd.set_laya_status('ACTIVE', 'deciding task approach')
    assert 'ACTIVE' in lbl.text()
    assert 'deciding task approach' in lbl.text()
    assert cmd._laya_status_state == 'active'

    cmd.set_laya_status('COMPLETE', 'routed → Gemini · SEARCH')
    assert 'COMPLETE' in lbl.text()
    assert 'SEARCH' in lbl.text()

    cmd.set_laya_status('NOT USED', 'abstained — general AI handled this')
    assert 'NOT USED' in lbl.text()
    assert 'COMPLETE' not in lbl.text()


def test_unknown_state_degrades_to_not_used(qapp):
    """A bad state must never render a false 'ACTIVE'."""
    cmd, lbl = _command_window_with_label()
    cmd.set_laya_status('garbage', 'should be ignored')
    assert 'NOT USED' in lbl.text()
    assert 'ACTIVE' not in lbl.text()


def test_status_has_a_tooltip_and_style(qapp):
    """The role is surfaced for hover, and the line is styled (not plain text)."""
    cmd, lbl = _command_window_with_label()
    cmd.set_laya_status('ACTIVE', 'selecting next action')
    assert 'selecting next action' in (lbl.toolTip() or '')
    assert lbl.styleSheet().strip() != ''


def test_complete_line_carries_the_measured_routing_time(qapp):
    """The COMPLETE line reports the routing latency that was actually
    measured for this request (spec §4/§26), not a generic 'done'."""
    cmd, lbl = _command_window_with_label()
    cmd.set_laya_status('COMPLETE', 'routed → pc_automation · AUTOMATE · 1840ms')
    assert 'COMPLETE' in lbl.text()
    assert '1840ms' in lbl.text()
    assert '1840ms' in (lbl.toolTip() or '')


def test_not_used_states_the_real_reason(qapp):
    """'NOT USED' is not a shrug: the runtime's skip reason (still loading,
    busy, disabled) is carried on the line that the HUD shows."""
    cmd, lbl = _command_window_with_label()
    cmd.set_laya_status('NOT USED', 'decision model still loading on CPU')
    assert 'NOT USED' in lbl.text()
    assert 'still loading' in (lbl.toolTip() or '')

