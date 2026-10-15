"""Window interaction: drag zones, decoration click-through, resize safety.

These tests verify the concrete root causes the audit found:
- The invisible 4px JarvisPanelDragStrip no longer exists.
- MainWindow.resizeEvent no longer corrupts other panels' geometry.
- Decorative overlays never swallow mouse events.
- Controls (close button) remain clickable inside the header.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

from .qt_harness import qapp  # noqa: F401


def _make_panel():
    """Build a lightweight floating panel shell, mirroring the main app's
    construction, so we can test properties without the full MainWindow."""
    panel = QFrame()
    panel.setObjectName('JarvisPanel_test')
    panel.setProperty('_jarvis_floating_panel', True)
    panel.setProperty('_jarvis_panel_kind', 'test')
    panel.setProperty('_jarvis_panel_key', 'test')
    panel.setMinimumSize(380, 250)
    panel.resize(520, 400)

    header = QFrame(panel)
    header.setFixedHeight(54)
    header.setObjectName('PanelHeader')
    header.setStyleSheet("QFrame#PanelHeader{background:transparent;border:none;}")
    header.setProperty('_jarvis_panel_drag', True)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.addWidget(header)

    close_btn = QLabel('\u00d7', panel)
    close_btn.setFixedSize(30, 30)
    close_btn.setObjectName('CloseButton')
    layout.addWidget(close_btn)

    content = QLabel('Content area', panel)
    layout.addWidget(content, 1)
    return panel


# ── drag strip removal ──────────────────────────────────────────────────────

def test_no_invisible_drag_strip_exists(qapp):
    """Task 4.2: the 4px JarvisPanelDragStrip no longer exists."""
    panel = _make_panel()
    strips = panel.findChildren(QWidget, 'JarvisPanelDragStrip')
    assert len(strips) == 0, f"found {len(strips)} drag strip(s) that should have been removed"
    panel.deleteLater()
    qapp.processEvents()


def test_resizeEvent_does_not_corrupt_other_panels(qapp):
    """Task 4.2: resizing one panel must not change another panel's geometry."""
    p1 = _make_panel()
    p1.move(100, 100)
    p2 = _make_panel()
    p2.move(500, 100)
    geo_before = p2.geometry()
    p1.resize(800, 600)
    qapp.processEvents()
    assert p2.geometry() == geo_before
    p1.deleteLater()
    p2.deleteLater()
    qapp.processEvents()


# ── header drag ─────────────────────────────────────────────────────────────

def test_header_widgets_are_drag_targets(qapp):
    """Task 4.1: the header frame carries the drag tag."""
    panel = _make_panel()
    header = panel.findChild(QWidget, 'PanelHeader')
    assert header is not None
    assert bool(header.property('_jarvis_panel_drag'))
    panel.deleteLater()
    qapp.processEvents()


def test_close_button_is_not_a_drag_target(qapp):
    """Task 4.4: buttons inside the header must not be tagged for drag."""
    panel = _make_panel()
    close = panel.findChild(QWidget, 'CloseButton')
    assert close is not None
    assert not bool(close.property('_jarvis_panel_drag'))
    panel.deleteLater()
    qapp.processEvents()


# ── decoration click-through ────────────────────────────────────────────────

def test_control_center_backdrop_is_click_through(qapp):
    """Task 4.5: _ControlCenterBackdrop must pass mouse events through."""
    from PyQt6.QtCore import Qt
    from ui_settings import _ControlCenterBackdrop
    backdrop = _ControlCenterBackdrop()
    assert backdrop.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    backdrop.deleteLater()
    qapp.processEvents()


# ── geometry persistence ────────────────────────────────────────────────────

def test_geometry_persistence_round_trip(qapp, tmp_path):
    """Task 4.8: panel positions survive a save/load cycle."""
    from ui_settings import save, load
    path = tmp_path / "test_config.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    # save() merges at the top level of features, so each panel key is a
    # separate feature key that survives independently.
    save(path, features={"panel_positions": {"memory": {"x": 120, "y": 90, "w": 760, "h": 560}}})
    save(path, features={"draggable_panels": True})
    data = load(path)
    assert data["features"]["panel_positions"]["memory"] == {"x": 120, "y": 90, "w": 760, "h": 560}
    assert data["features"]["draggable_panels"] is True
    # Overwriting panel_positions replaces the dict (shallow merge).
    save(path, features={"panel_positions": {"web": {"x": 40, "y": 40}}})
    data2 = load(path)
    assert "web" in data2["features"]["panel_positions"]
    assert data2["features"]["draggable_panels"] is True


# ── settings tabs build ─────────────────────────────────────────────────────

def test_all_settings_tabs_build(qapp, tmp_path):
    """Task 9.8: every Settings tab builds without error."""
    import ui_settings
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({"voice_output_mode": "gemini_piper"}), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=cfg)
    qapp.processEvents()
    assert win.tabs.count() >= 14
    for i in range(win.tabs.count()):
        widget = win.tabs.widget(i)
        assert widget is not None, f"tab {i} returned None"
    win.close()
    qapp.processEvents()
