"""Offscreen tests for the J.A.R.V.I.S. Layout Studio.

These cover the editor requirements end to end: the schema migration, the
element engine's real rendering (including the white-screen regression guard),
the full create → style → arrange → save → reopen workflow, script containment
and the safety paths for missing images and deleted widgets.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .qt_harness import qapp  # noqa: E402,F401  (pytest fixture)

from PyQt6.QtCore import QRect, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QFrame, QLabel, QMainWindow, QVBoxLayout, QWidget  # noqa: E402

import ui_layout  # noqa: E402
import ui_layout_core as core  # noqa: E402
from ui_layout import LayoutEditorDialog, load_layout, save_layout  # noqa: E402
from ui_layout_core import (  # noqa: E402
    EFFECT_KINDS,
    EFFECT_SCHEMA,
    ELEMENT_TYPES,
    ElementWidget,
    IMAGE_CACHE,
    default_spec,
    normalize_element,
)


# ---------------------------------------------------------------------------
# A minimal host that implements exactly the contract ui_layout.py needs
# (the same contract run_layout_editor.py and ui.py's MainWindow provide).
# ---------------------------------------------------------------------------
class StudioHost(QMainWindow):
    def __init__(self, config_dir: Path):
        super().__init__()
        self.CONFIG_DIR = config_dir
        self.resize(1200, 800)
        self.surface = QWidget()
        self.surface.setObjectName("Surface")
        self.surface.resize(1200, 800)
        self.setCentralWidget(self.surface)

        self._topbar = QFrame(self.surface)
        self._topbar.setObjectName("TopBar")
        self._topbar.setGeometry(0, 0, 1200, 56)
        row = QVBoxLayout(self._topbar)
        self.sample_label = QLabel("REACTOR")
        self.sample_label.setObjectName("ReactorLabel")
        row.addWidget(self.sample_label)

        content = QFrame(self.surface)
        content.setObjectName("ContentSurface")
        content.setGeometry(20, 70, 900, 640)
        self._system_layout_widgets = {
            "Content Surface": content,
            "Video Preview": QFrame(self.surface),
        }
        for widget in (self._topbar, self.sample_label, content, *self._system_layout_widgets.values()):
            widget.show()

        self._layout_elements: dict[str, dict] = {}
        self._layout_widgets: dict[str, ElementWidget] = {}
        self._layout_selected_widget = None
        self._layout_widget_overrides: dict = {}
        self.layout_log: list[str] = []
        self.save_count = 0

    # -- logging ---------------------------------------------------------
    def write_log(self, message: str) -> None:
        self.layout_log.append(str(message))

    # -- persistence (mirrors ui.py's MainWindow contract) ---------------
    def _load_layout_elements(self) -> None:
        data = load_layout(Path(self.CONFIG_DIR) / "ui_layout.json")
        self._layout_elements = {}
        for raw in data.get("elements") or []:
            if isinstance(raw, dict) and raw.get("name"):
                self._layout_elements[str(raw["name"])] = normalize_element(raw, str(raw["name"]))
        self._apply_layout_elements()

    def _save_layout_elements(self) -> None:
        self.save_count += 1
        for name, widget in list(self._layout_widgets.items()):
            element = self._layout_elements.get(name)
            if element is None or not core.widget_alive(widget):
                continue
            if bool(element.get("locked")):
                continue
            element.update({"x": widget.x(), "y": widget.y(),
                            "w": widget.width(), "h": widget.height(),
                            "visible": widget.isVisible()})
        save_layout(Path(self.CONFIG_DIR) / "ui_layout.json",
                    {"elements": list(self._layout_elements.values())})

    def _parent_for(self, element: dict) -> QWidget:
        wanted = str((element or {}).get("parent") or "")
        for name, widget in ui_layout._root_candidates(self):
            if name == wanted and core.widget_alive(widget):
                return widget
        return self.surface

    def _apply_layout_elements(self) -> None:
        for name, widget in list(self._layout_widgets.items()):
            if name not in self._layout_elements:
                try:
                    widget.setParent(None)
                    widget.deleteLater()
                except RuntimeError:
                    pass
                self._layout_widgets.pop(name, None)
        for name, raw in list(self._layout_elements.items()):
            spec = normalize_element(raw, name)
            self._layout_elements[name] = spec
            parent = self._parent_for(spec)
            widget = self._layout_widgets.get(name)
            if widget is not None and not core.widget_alive(widget):
                widget = None
            if widget is None:
                widget = ElementWidget(spec, parent)
                widget.setProperty("_jarvis_layout_custom", True)
                widget._layout_element_name = name
                self._layout_widgets[name] = widget
            elif widget.parentWidget() is not parent:
                widget.setParent(parent)
            widget.apply_spec(spec)
            widget.show()


def _png(path: Path, width: int = 320, height: int = 200, colour: str = "#00d4ff") -> Path:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    image.save(str(path))
    return path


@pytest.fixture()
def host(qapp, tmp_path: Path) -> StudioHost:  # noqa: F811
    (tmp_path / "ui_layout.json").write_text(
        json.dumps({"version": 3, "camera_window": {"x": 1, "y": 2, "w": 3, "h": 4}},
                   indent=2), encoding="utf-8")
    window = StudioHost(tmp_path)
    window.show()
    qapp.processEvents()
    yield window
    window.close()


@pytest.fixture()
def studio(qapp, host: StudioHost):  # noqa: F811
    editor = LayoutEditorDialog(host)
    editor.show()
    qapp.processEvents()
    yield editor
    editor.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_v3_element_migrates_into_the_v4_schema():
    legacy = {"name": "HUD_Button", "kind": "Button", "text": "GO", "color": "#ff8800",
              "x": 10, "y": 20, "w": 120, "h": 40, "opacity": 80, "visible": True,
              "stylesheet": "QPushButton{color:red;}", "html": "", "css": ""}
    spec = normalize_element(legacy, "HUD_Button")
    assert spec["name"] == "HUD_Button"
    assert spec["type"] == "Button"
    assert (spec["x"], spec["y"], spec["w"], spec["h"]) == (10, 20, 120, 40)
    assert spec["opacity"] == 80
    assert spec["text"] == "GO"
    assert spec["stylesheet"].startswith("QPushButton")
    assert spec["stroke"]["color"] == "#ff8800"
    assert spec["text_style"]["color"] == "#ff8800"
    # Every v4 block exists and is safe to render.
    for key in ("background", "image", "text_style", "stroke", "borders", "shadow",
                "glow", "effects", "states", "animation", "interaction", "script"):
        assert key in spec


def test_normalize_element_never_raises_on_garbage():
    for payload in (None, 5, "text", [], {"effects": "nope"}, {"image": 3, "states": []}):
        spec = normalize_element(payload, "X")
        assert spec["name"] == "X"
        assert isinstance(spec["effects"], list)


# ---------------------------------------------------------------------------
# Engine rendering (white-screen regression guard)
# ---------------------------------------------------------------------------
def _render(widget: ElementWidget) -> QPixmap:
    pixmap = QPixmap(widget.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    widget.render(painter)
    painter.end()
    return pixmap


def test_every_element_type_and_effect_renders_without_crashing(tmp_path: Path, qapp):  # noqa: F811
    image = _png(tmp_path / "art.png")
    for element_type in ELEMENT_TYPES:
        spec = default_spec(f"E_{element_type}", element_type)
        spec["image"].update({"path": str(image), "is_background": element_type in ("Image", "Panel"),
                              "fit": "Cover", "tint": "#ff00ff", "tint_strength": 30})
        spec["background"]["gradient"].update({"enabled": True, "stops": [[0.0, "#00d4ff"], [0.5, "#003344"], [1.0, "#000000"]]})
        spec["stroke"].update({"enabled": True, "width": 2.0})
        spec["glow"].update({"enabled": True, "radius": 20})
        spec["shadow"].update({"enabled": True})
        spec["text_style"]["stroke"].update({"enabled": True, "width": 2.0})
        spec["text_style"]["glow"].update({"enabled": True})
        spec["text_style"]["shadow"].update({"enabled": True})
        spec["borders"]["left"].update({"width": 3, "style": "Dashed"})
        spec["borders"]["top"].update({"width": 2, "style": "Double"})
        spec["rotation"] = 12.0
        spec["states"]["hover"].update({"enabled": True, "background": "#123456"})
        spec["effects"] = [{"kind": kind, "enabled": True,
                            "params": {key: value[-1] for key, value in EFFECT_SCHEMA[kind].items()}}
                           for kind in EFFECT_KINDS]
        spec["w"], spec["h"] = 240, 160
        widget = ElementWidget(spec)
        widget.apply_spec(spec)
        pixmap = _render(widget)
        assert not widget._paint_failed, element_type
        assert pixmap.width() == 240 and pixmap.height() == 160


def test_element_actually_paints_pixels(tmp_path: Path, qapp):  # noqa: F811
    spec = default_spec("Banner", "Panel")
    spec["background"].update({"color": "#00ff00", "opacity": 100, "gradient": {"enabled": False}})
    spec["w"], spec["h"] = 80, 60
    spec["stroke"]["enabled"] = False
    widget = ElementWidget(spec)
    widget.apply_spec(spec)
    image = _render(widget).toImage()
    # Somewhere in the middle the background colour must be present.
    assert image.pixelColor(40, 30).green() > 120
    assert image.pixelColor(40, 30).alpha() > 0


def test_missing_image_file_is_safe_and_reported(qapp):  # noqa: F811
    spec = default_spec("Ghost", "Image")
    spec["image"]["path"] = str(Path("does") / "not" / "exist.png")
    spec["image"]["is_background"] = True
    widget = ElementWidget(spec)
    widget.apply_spec(spec)
    _render(widget)
    assert not widget._paint_failed
    assert any(str(key).endswith("exist.png") for key in IMAGE_CACHE.decode_failures)


def test_large_image_is_decoded_once_and_scaled_from_cache(tmp_path: Path, qapp):  # noqa: F811
    big = _png(tmp_path / "big.png", 2400, 1600)
    IMAGE_CACHE.clear()
    started = time.time()
    first = IMAGE_CACHE.scaled(big, core.QSize(420, 280))
    second = IMAGE_CACHE.scaled(big, core.QSize(420, 280))
    elapsed = time.time() - started
    assert first is not None and second is not None
    assert first is second, "scaled previews must come from the cache"
    assert elapsed < 8.0

    spec = default_spec("Wallpaper", "Image")
    spec["image"].update({"path": str(big), "is_background": True, "fit": "Cover"})
    widget = ElementWidget(spec)
    widget.resize(400, 300)
    widget.apply_spec(spec)
    started = time.time()
    for _ in range(6):
        _render(widget)
    assert time.time() - started < 5.0
    assert not widget._paint_failed


# ---------------------------------------------------------------------------
# The Studio workflow (TXT section 60)
# ---------------------------------------------------------------------------
def test_studio_workflow_creates_styles_arranges_and_saves(studio, host, qapp, tmp_path):  # noqa: F811
    editor = studio
    assert editor.panel_picker.count() > 2
    editor._set_target_by_index(1)              # Main Surface / Surface
    qapp.processEvents()

    # Add element
    panel = editor.add_element("Panel")
    assert isinstance(panel, ElementWidget)
    assert "Panel" in panel.element_name()

    # Import image → real element with the asset registered
    png = _png(tmp_path / "logo.png")
    editor.import_image_files([str(png)])
    assert any(Path(path).name == "logo.png" for _name, path in editor.assets.items())
    image_element = editor.assets and editor.selection.primary()
    assert isinstance(image_element, ElementWidget)
    assert image_element.image_path() == str(png)

    # Set as background (fills its parent) and back to foreground
    editor.do_action("set_background", True)
    assert image_element.spec["image"]["is_background"] is True
    parent = image_element.parentWidget()
    assert image_element.width() == parent.width()
    editor.do_action("set_background", False)
    assert image_element.spec["image"]["is_background"] is False

    # Move / resize / opacity / stroke / glow / shadow / gradient / text
    editor.set_property(image_element, "x", 40, history=False)
    editor.set_property(image_element, "w", 260, history=False)
    editor.set_property(image_element, "opacity", 65, history=False)
    editor.set_property(image_element, "stroke.enabled", True, history=False)
    editor.set_property(image_element, "stroke.width", 3.0, history=False)
    editor.set_property(image_element, "glow.enabled", True, history=False)
    editor.set_property(image_element, "shadow.enabled", True, history=False)
    editor.set_property(image_element, "background.gradient.enabled", True, history=False)
    editor.set_property(image_element, "text", "HELLO", history=False)
    editor.set_property(image_element, "text_style.stroke.enabled", True, history=False)
    assert image_element.x() == 40 and image_element.width() == 260
    assert image_element.spec["stroke"]["width"] == 3.0
    assert image_element.graphicsEffect() is not None, "glow should occupy the effect slot"

    # Effect stack
    editor.select_widget(image_element)
    editor._add_effect("Brightness")
    editor._add_effect("Frosted")
    assert [e["kind"] for e in image_element.spec["effects"]] == ["Brightness", "Frosted"]
    editor._move_effect(0, 1)
    assert [e["kind"] for e in image_element.spec["effects"]] == ["Frosted", "Brightness"]
    editor._toggle_effect(0, False)
    assert image_element.spec["effects"][0]["enabled"] is False
    editor._remove_effect(0)

    # Presets are real property sets
    text = editor.add_element("Text")
    editor.select_widget(text)
    editor._apply_preset("text", "Warning Text")
    assert text.spec["text_style"]["stroke"]["enabled"] is True
    editor._apply_preset("hud", "Neon HUD")
    assert text.spec["glow"]["enabled"] is True

    # Copy / paste / duplicate with unique naming
    editor.select_widget(text)
    editor.copy_selection()
    before = len(host._layout_elements)
    editor.paste_clipboard()
    editor.duplicate_selection()
    names = list(host._layout_elements)
    assert len(names) == before + 2
    assert any(n.endswith("_Copy") for n in names)
    assert len(names) == len(set(names)), "names must stay unique"

    # Multi-select, align, distribute, group
    editor.select_widget(panel)
    editor.select_widget(image_element, additive=True)
    editor.select_widget(text, additive=True)
    assert editor.selection.count() == 3
    editor._align("left")
    editor._distribute("vertical")
    lefts = {w.x() for w in editor.selection.items()}
    assert len(lefts) == 1, "align left must line the selection up"
    editor._group(True)
    assert all(w.spec.get("group") for w in editor.selection.items())
    editor._group(False)
    assert not any(w.spec.get("group") for w in editor.selection.items())

    # Lock / hide / show / rename / z-order
    editor.do_action("lock", None)
    assert all(w.spec["locked"] for w in editor.selection.items())
    editor.do_action("unlock", None)
    editor.do_action("hide", None)
    assert all(not w.isVisible() for w in editor.selection.items())
    editor.do_action("show", None)
    editor.do_action("z_front", None)
    editor.do_action("z_back", None)
    editor.set_property(text, "object_name", "RenamedLabel")
    assert text.objectName() == "RenamedLabel"

    # Reparent into another panel
    target_panel = host._system_layout_widgets["Content Surface"]
    editor._reparent(text, target_panel)
    assert text.parentWidget() is target_panel

    # View code / QSS / script
    editor.select_widget(text)
    editor._show_code_tab()
    code = editor.code_editors["python"].text()
    assert "ElementWidget" in code and "HUD" not in code.splitlines()[0]
    editor.set_property(text, "stylesheet", "QLabel{color:#00ff00;}")
    qapp.processEvents()
    editor.code_editors["script"].set_text(
        "done = element.element_name()\nlog('script ran for', done)\n", "python")
    editor._save_code_tab("script")
    assert text.script_runner is not None
    assert editor.scripts.run(text, text.spec["script"]["code"], "test")[0] is True
    editor._disable_script()
    assert text.script_runner is None

    # Delete + undo + redo
    doomed_name = panel.element_name()
    editor.select_widget(panel)
    editor.delete_selected_element()
    assert doomed_name not in host._layout_elements
    editor.undo()
    assert doomed_name in host._layout_elements
    editor.redo()
    assert doomed_name not in host._layout_elements

    # Save, close, reopen
    editor.save_all()
    assert host.save_count > 0
    path = Path(host.CONFIG_DIR) / "ui_layout.json"
    assert path.exists()
    editor.close()
    qapp.processEvents()
    reopened = LayoutEditorDialog(host)
    reopened.show()
    qapp.processEvents()
    assert reopened.panel_picker.count() > 0
    assert any(Path(p).name == "logo.png" for _n, p in reopened.assets.items())
    reloaded = load_layout(path)
    assert reloaded["version"] >= 4
    assert reloaded["camera_window"] == {"x": 1, "y": 2, "w": 3, "h": 4}, \
        "unknown top-level keys must survive a Studio save"
    assert reloaded["assets"]
    reopened.close()


def test_studio_recovers_from_a_deleted_widget(studio, host, qapp):  # noqa: F811
    editor = studio
    element = editor.add_element("Panel")
    editor.select_widget(element)
    assert editor.selection.primary() is element
    # Simulate an element destroyed elsewhere (the crash this guards against).
    host._layout_widgets.pop(element.element_name(), None)
    element.setParent(None)
    from PyQt6 import sip
    sip.delete(element)
    assert not core.widget_alive(element)
    editor._selection_changed()                 # must not raise
    qapp.processEvents()
    assert editor.selection.primary() is None
    assert editor.properties._spec is None
    editor.viewport.mark_dirty()
    editor.viewport.repaint()                   # viewport must survive a dead target
    assert not editor.viewport._paint_failed
    editor.set_property(element, "x", 10)      # ignored, never raised
    editor.delete_selected_element()


def test_invalid_script_is_contained_and_reported(studio, host, qapp):  # noqa: F811
    editor = studio
    element = editor.add_element("Button")
    editor.select_widget(element)
    ok, message = editor.scripts.run(element, "import os\nos.system('echo hi')", "test")
    assert ok is False and "imports are not allowed" in message
    ok, message = editor.scripts.run(element, "def broken(:\n", "test")
    assert ok is False and "line" in message
    ok, message = editor.scripts.run(element, "raise RuntimeError('boom')", "test")
    assert ok is False and "boom" in message
    assert any("SCRIPT" in line for line in host.layout_log)


def test_malformed_qss_does_not_break_the_studio(studio, host, qapp):  # noqa: F811
    editor = studio
    element = editor.add_element("Panel")
    editor.select_widget(element)
    editor.set_property(element, "stylesheet", "QLabel { color: ; broken }")
    assert element.isVisible()
    assert not element._paint_failed


def test_syntax_highlighting_never_raises(studio, host, qapp):  # noqa: F811
    """A regex group that did not participate must not reach ``setFormat``.

    ``highlightBlock`` runs inside Qt's document layout: raising there escapes
    Python and aborts the process, and the QSS/JSON/Python tabs colour real HUD
    content like ``border-radius:10px;`` — where the decimal part of the number
    rule does not participate in the match.
    """
    editor = studio
    samples = {
        "qss": [
            "QFrame { border-radius:10px; }",
            "color:#00d4ff; background: rgba(0,20,30,0.5); padding: 12px;",
            "margin:0; font-size: 9pt;", "width: 100%;", "/* note */ a:b;", "", "{}",
        ],
        "json": ['{"x": 12, "name": "HUD", "ok": true, "maybe": null}', "", "[1,2,3]"],
        "python": [
            "def on_click(element): return 12.5",
            "class HUD_Button(object): pass",
            "# comment only", "if x == 10: y = 'text'", "",
        ],
        "html": ["<div class=\"hud\">text</div>", ""],
    }
    for key, texts in samples.items():
        code_editor = editor.code_editors[key]
        for text in texts:
            code_editor.set_text(text, code_editor.language, highlight=True)
            code_editor.ensure_highlighted()
            assert code_editor.text() == text
    qapp.processEvents()


def test_many_elements_stay_usable(studio, host, qapp):  # noqa: F811
    editor = studio
    created = [editor.add_element("Indicator") for _ in range(40)]
    assert len([w for w in created if core.widget_alive(w)]) == 40
    editor.set_selection([w for w in created if core.widget_alive(w)])
    assert editor.selection.count() == 40
    editor._align("top")
    editor._distribute("horizontal")
    editor._selection_changed()
    qapp.processEvents()
    assert editor.properties._subject is not None
    assert editor.explorer.topLevelItemCount() == 1
