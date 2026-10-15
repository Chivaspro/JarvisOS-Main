"""Repair card approval UI — offscreen.

Regression: a fault in a protected file (ui.py, main.py, ...) was skipped by
the analyzer ("found no first-party file"), so `repair_proposed` never fired,
the patch preview stayed at its placeholder and APPLY PATCH had nothing to
approve. A protected fault must now fill the preview and hold the proposal for
explicit approval.
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

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_repair_ui_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def _pane():
    pane = ui.RepairPane()
    # Keep the display logic under test deterministic: the real refresh
    # spawns a worker that re-renders from live recovery state.
    pane.refresh = lambda: None  # type: ignore[method-assign]
    return pane


def test_protected_proposal_fills_the_preview_and_pends(qapp):
    pane = _pane()
    pane.set_event('repair_proposed', {
        'parsed': {'file': str(ROOT / 'ui.py'), 'line': 10869,
                   'exception_type': 'TypeError'},
        'proposal': {'ok': True, 'risk': 'protected',
                     'explanation': 'bind the paint handler to the bubble',
                     'target_chunk': 'OLD CHUNK',
                     'replacement_chunk': 'NEW CHUNK'},
        'auto': False,
    })
    assert pane._pending, "an approval-required proposal must be held"
    body = pane._diff.toPlainText()
    assert 'OLD CHUNK' in body and 'NEW CHUNK' in body, \
        "the patch preview must show both sides of the diff"
    assert 'approval' in pane._state.text().lower()


def test_auto_applied_proposal_is_never_pending(qapp):
    pane = _pane()
    pane.set_event('repair_proposed', {
        'parsed': {'file': str(ROOT / 'actions' / 'x.py'), 'line': 3},
        'proposal': {'ok': True, 'risk': 'low', 'explanation': 'guard',
                     'target_chunk': 'a', 'replacement_chunk': 'b'},
        'auto': True,
    })
    assert pane._pending == {}, "an auto-applied patch needs no approval slot"
    assert 'applied' in pane._state.text().lower()


def test_detected_fault_shows_the_traceback(qapp):
    pane = _pane()
    pane.set_event('repair_detected', {
        'parsed': {'file': str(ROOT / 'ui.py'), 'line': 10869,
                   'exception_type': 'TypeError'},
        'traceback': 'Traceback (most recent call last): boom',
        'source': 'tool:x', 'request': '',
    })
    assert 'TypeError' in pane._state.text()
    assert 'Traceback' in pane._detail.text()
