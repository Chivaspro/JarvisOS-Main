"""How the running HUD loads, applies and saves Layout Studio elements.

The Studio edits the *real* widget tree, so MainWindow has to agree with it on
three things: how ``config/ui_layout.json`` is read (v3 files included), what
gets created for a custom element (the engine widget that paints images,
effects and strokes), and what a save is allowed to overwrite.

These tests run against a real ``MainWindow`` with ``CONFIG_DIR`` redirected to
a temp directory, so nothing here touches the user's own layout file.
"""
from __future__ import annotations

import json

import pytest

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtGui import QColor, QPixmap

import ui_layout
from ui_layout_core import ElementWidget, widget_alive

from .test_image_view_panel import _write_png, camera, qapp, window  # noqa: F401  (fixtures)
from .test_image_view_panel import ui


LEGACY_LAYOUT = {
    # v3 element: kind / text / color / opacity — the shape the old editor wrote.
    "elements": [
        {"name": "LegacyLabel", "kind": "Text", "text": "HELLO", "color": "#ff8800",
         "x": 30, "y": 40, "w": 140, "h": 30, "opacity": 80, "visible": True},
    ],
    "widgets": {"Top Bar": {"id": "Top Bar", "stylesheet": "background:red;"}},
    "assets": {"logo": {"path": "logo.png", "name": "logo"}},
    "active_panel": "Main Surface",
}


def _layout_file():
    """The layout file MainWindow actually reads (``ui.CONFIG_DIR`` is patched)."""
    return ui.CONFIG_DIR / "ui_layout.json"


def _write_layout(payload) -> None:
    _layout_file().write_text(json.dumps(payload), encoding="utf-8")


def _read_layout():
    return json.loads(_layout_file().read_text(encoding="utf-8"))


# ── loading ──────────────────────────────────────────────────────────────────

def test_v3_layout_loads_into_real_engine_widgets(window):
    _write_layout(LEGACY_LAYOUT)
    window._load_layout_elements()

    widget = window._layout_widgets["LegacyLabel"]
    assert isinstance(widget, ElementWidget), "custom elements are engine widgets now"
    assert widget.element_type() == "Text"
    assert widget.spec["text"] == "HELLO"
    assert widget.spec["opacity"] == 80
    assert widget.geometry().getRect() == (30, 40, 140, 30)
    # ``isVisible`` is False while the whole HUD window is hidden; what matters
    # is that the element itself was asked to show and is not hidden.
    assert widget.spec["visible"] is True and not widget.isHidden()
    # The other layout sections survive the load.
    assert window._layout_assets == {"logo": {"path": "logo.png", "name": "logo"}}
    assert window._layout_widget_overrides == {"Top Bar": {"id": "Top Bar", "stylesheet": "background:red;"}}
    assert window._layout_active_panel == "Main Surface"


def test_image_element_renders_through_the_engine(window, tmp_path):
    png = _write_png(tmp_path / "background.png", 180, 120)
    spec = ui._normalize_element(
        {"name": "BackgroundImage", "type": "Image", "image": {"path": str(png)},
         "x": 10, "y": 12, "w": 180, "h": 120},
        "BackgroundImage",
    )
    window._layout_elements["BackgroundImage"] = spec
    window._apply_layout_elements()

    widget = window._layout_widgets["BackgroundImage"]
    assert widget.image_path() == str(png)
    canvas = QPixmap(widget.size())
    canvas.fill(QColor(0, 0, 0, 0))
    widget.render(canvas)                       # must paint, never raise


def test_deleting_an_element_removes_it_from_the_tree(window, qapp):
    window._layout_elements["Throwaway"] = ui._normalize_element(
        {"name": "Throwaway", "type": "Panel", "x": 4, "y": 4, "w": 60, "h": 40}, "Throwaway")
    window._apply_layout_elements()
    widget = window._layout_widgets["Throwaway"]
    assert widget_alive(widget)

    window._layout_elements.pop("Throwaway")
    window._apply_layout_elements()
    assert "Throwaway" not in window._layout_widgets
    assert widget.parentWidget() is None, "the removed element leaves the widget tree"
    # deleteLater is a queued request: deliver it explicitly (no running loop).
    qapp.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not widget_alive(widget)


def test_malformed_layout_file_never_breaks_the_window(window):
    _layout_file().write_text("{not json at all", encoding="utf-8")
    window._load_layout_elements()              # must not raise
    assert window._layout_elements == {}
    _layout_file().write_text('{"elements": "nonsense"}', encoding="utf-8")
    window._load_layout_elements()
    assert window._layout_elements == {}


# ── saving ───────────────────────────────────────────────────────────────────

def test_save_keeps_widgets_assets_and_active_panel(window):
    _write_layout(LEGACY_LAYOUT)
    window._load_layout_elements()
    window._save_layout_elements()

    saved = _read_layout()
    assert saved["widgets"] == LEGACY_LAYOUT["widgets"], "runtime widget edits must survive a save"
    assert saved["assets"] == LEGACY_LAYOUT["assets"]
    assert saved["active_panel"] == "Main Surface"
    names = [element["name"] for element in saved["elements"]]
    assert names == ["LegacyLabel"]
    # The element is written back in the current schema, not the v3 shape.
    assert saved["elements"][0]["type"] == "Text"
    assert "stroke" in saved["elements"][0]


def test_save_records_the_live_geometry(window):
    window._layout_elements["Mover"] = ui._normalize_element(
        {"name": "Mover", "type": "Panel", "x": 1, "y": 2, "w": 60, "h": 40}, "Mover")
    window._apply_layout_elements()
    window._layout_widgets["Mover"].move(123, 45)
    window._save_layout_elements()

    saved = _read_layout()
    assert saved["elements"][0]["x"] == 123 and saved["elements"][0]["y"] == 45


def test_locked_element_keeps_its_saved_geometry(window):
    window._layout_elements["Locked"] = ui._normalize_element(
        {"name": "Locked", "type": "Panel", "x": 5, "y": 6, "w": 70, "h": 20,
         "locked": True}, "Locked")
    window._apply_layout_elements()
    window._layout_widgets["Locked"].move(300, 300)     # an accidental move
    window._save_layout_elements()

    saved = _read_layout()
    element = saved["elements"][0]
    assert (element["x"], element["y"]) == (5, 6), "a locked element keeps its stored position"
    assert element["locked"] is True


# ── runtime widget overrides ─────────────────────────────────────────────────

def test_saved_runtime_widget_edits_are_reapplied(window):
    """The Studio's real-widget edits must survive a restart of the HUD."""
    topbar = window._topbar
    topbar.setProperty("_jarvis_layout_touched", True)
    topbar.setStyleSheet("background: rgb(1, 2, 3);")
    overrides = ui_layout.capture_widget_overrides(window)
    assert overrides, "a touched runtime widget must be captured"

    topbar.setStyleSheet("background: rgb(9, 9, 9);")   # the app restyles it
    window._layout_widget_overrides = overrides
    window._apply_saved_widget_overrides()

    assert "1, 2, 3" in topbar.styleSheet()


def test_restoring_overrides_without_a_layout_is_a_no_op(window):
    window._layout_widget_overrides = {}
    window._apply_saved_widget_overrides()              # must not raise


# ── the Studio working on the real HUD ───────────────────────────────────────

def test_layout_studio_edits_the_real_hud_and_saves_it(window, qapp, tmp_path):
    """Open the Studio on the live window, add/import/save, and check the file."""
    studio = ui.LayoutEditorDialog(window)
    try:
        assert studio.panel_picker.count() > 1, "the Studio lists the real target panels"
        studio._set_target_by_index(1)
        qapp.processEvents()

        element = studio.add_element("Panel")
        assert isinstance(element, ElementWidget)
        name = element.element_name()
        assert window._layout_widgets.get(name) is element, \
            "the running HUD builds the element the Studio created"

        studio.select_widget(element)
        studio.set_property(element, "opacity", 70, history=False)
        studio.set_property(element, "stroke.enabled", True, history=False)
        assert element.spec["opacity"] == 70

        studio.save_all()
    finally:
        studio.close()
        qapp.processEvents()

    saved = _read_layout()
    names = [element["name"] for element in saved["elements"]]
    assert name in names, "the Studio's element reaches config/ui_layout.json"
    assert any(element["name"] == name and element["opacity"] == 70
               for element in saved["elements"])
    assert window._layout_elements[name]["name"] == name
    # The HUD keeps editing the same live widget the Studio edited.
    assert window._layout_widgets[name].spec["stroke"]["enabled"] is True
