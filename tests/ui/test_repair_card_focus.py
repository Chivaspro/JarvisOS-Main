"""Repair-card surfacing — offscreen, no MainWindow needed.

Regression for the HUD "window rapidly opens and closes" report: every
auto-heal event called `_set_panel_visible(repair_panel, True)`, which restarted
the fade from opacity 0, re-fit the geometry and called activateWindow() —
so a burst of events made the panel blink and stole keyboard focus while the
user was typing. `show_repair_card` must refresh a visible panel in place.
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

_ui_spec = importlib.util.spec_from_file_location("ui_impl_repair_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


class _Pane:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def set_event(self, kind, payload) -> None:
        self.events.append((kind, payload))


class _Panel:
    def __init__(self, visible: bool) -> None:
        self._visible = visible
        self.raised = 0

    def isVisible(self) -> bool:  # noqa: N802 - Qt naming
        return self._visible

    def raise_(self) -> None:
        self.raised += 1


class _Host:
    """Stands in for MainWindow: show_repair_card only touches these parts."""

    def __init__(self, visible: bool) -> None:
        self._repair_pane = _Pane()
        self._repair_panel = _Panel(visible)
        self.show_calls: list[bool] = []

    def _set_panel_visible(self, panel, visible=True) -> None:
        self.show_calls.append(bool(visible))


def test_visible_repair_panel_is_refreshed_not_reshown(qapp):
    host = _Host(visible=True)
    ui.MainWindow.show_repair_card(host, "repair_detected", {"message": "x"})
    assert host._repair_pane.events == [("repair_detected", {"message": "x"})]
    assert host.show_calls == [], "a visible panel must not be re-shown"
    assert host._repair_panel.raised == 1, "it should just come to the front"


def test_hidden_repair_panel_is_surfaced(qapp):
    host = _Host(visible=False)
    ui.MainWindow.show_repair_card(host, "repair_proposed", {})
    assert host.show_calls == [True], "a hidden panel must be shown once"


def test_repeated_events_do_not_restart_the_fade(qapp):
    host = _Host(visible=True)
    for _ in range(5):
        ui.MainWindow.show_repair_card(host, "repair_analyzed", {})
    assert host.show_calls == [], "no re-show on any repeated event"
    assert len(host._repair_pane.events) == 5, "content still refreshes"
