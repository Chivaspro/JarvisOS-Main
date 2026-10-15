"""The Layout Studio's real "Download as Figma" pipeline, run on live widgets.

``tests/unit/test_figma_export.py`` covers the Qt-free SVG writer. Everything
that broke the exported file lives on the Qt side of that seam, so this module
drives the actual Studio code — ``_figma_svg_capture``, ``_figma_runtime_visual``,
``_figma_runtime_specs`` and ``_export_target_as_figma`` — against a HUD-style
panel that has the same characteristics as the real Chat Panel:

* a ``qlineargradient`` + ``rgba()`` stylesheet background,
* a pixmap logo (raster, the thing that used to disappear),
* a custom ``paintEvent`` widget (the reactor-style artwork),
* a persisted custom element hosting an ordinary child widget,
* and a panel that is hidden on screen, which is the normal state while the
  Studio is open.

The assertions are the ones that decide whether Figma/Penpot show a complete
design: well-formed XML with exactly one ``<svg>`` root, a painted background,
one non-empty layer per exported element, and embedded assets.
"""
from __future__ import annotations

import os
import sys
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

import figma_export
import ui_layout
from ui_layout_core import widget_alive

from .qt_harness import qapp  # noqa: F401,E402  (pytest fixture)
from .test_layout_studio import StudioHost  # noqa: E402

SVG = "{http://www.w3.org/2000/svg}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

PANEL_SHEET = (
    "QFrame#HudPanel{"
    "background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
    "stop:0 rgba(4,18,27,255),stop:1 rgba(10,58,74,128));"
    "border:2px solid rgba(138,210,255,90);border-radius:16px;}"
    "QPushButton#SendButton{background:rgba(0,82,110,100);color:#8ad2ff;}"
)


class ReactorWidget(QWidget):
    """Stands in for the HUD's custom-painted art (the compact reactor)."""

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#00cfff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRect(4, 4, self.width() - 8, self.height() - 8))
        painter.end()


@pytest.fixture()
def host(qapp, tmp_path: Path):  # noqa: F811
    window = StudioHost(tmp_path)
    window.show()
    qapp.processEvents()
    yield window
    window.close()


@pytest.fixture()
def hud_panel(qapp, host, tmp_path: Path):  # noqa: F811
    """A live HUD panel with a raster logo, custom paint and styled controls."""
    panel = QFrame(host.surface)
    panel.setObjectName("HudPanel")
    panel.setStyleSheet(PANEL_SHEET)
    panel.setGeometry(40, 40, 420, 300)

    layout = QVBoxLayout(panel)
    logo = QLabel(panel)
    logo.setObjectName("LogoIcon")
    image = QImage(48, 48, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ff00aa"))
    logo.setPixmap(QPixmap.fromImage(image))
    layout.addWidget(logo)

    reactor = ReactorWidget(panel)
    reactor.setObjectName("Reactor")
    reactor.setFixedSize(64, 64)
    layout.addWidget(reactor)

    send = QPushButton("SEND", panel)
    send.setObjectName("SendButton")
    layout.addWidget(send)

    # A persisted custom element that hosts an ordinary child widget: the child
    # used to be dropped entirely because it lived under an engine widget.
    spec = ui_layout.default_spec("HUD Card One", "HUD Card")
    spec.update({"parent": "HudPanel", "x": 250, "y": 180, "w": 150, "h": 90, "z": 5})
    host._layout_elements["HUD Card One"] = spec
    card = ui_layout.ElementWidget(spec, panel)
    card.setProperty("_jarvis_layout_custom", True)
    card._layout_element_name = "HUD Card One"
    card.setGeometry(250, 180, 150, 90)
    host._layout_widgets["HUD Card One"] = card
    card.show()
    nested = QLabel("NESTED", card)
    nested.setObjectName("NestedLabel")
    nested.setGeometry(10, 10, 120, 24)
    nested.show()
    panel.show()
    qapp.processEvents()
    for _ in range(10):
        qapp.processEvents()
    yield panel
    panel.deleteLater()


@pytest.fixture()
def studio(qapp, host, hud_panel, tmp_path, monkeypatch):  # noqa: F811
    editor = ui_layout.LayoutEditorDialog(host)
    editor.show()
    qapp.processEvents()
    editor._target_map = [("HudPanel", hud_panel)]
    editor._current_target_name = "HudPanel"
    out = tmp_path / "downloads"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(figma_export, "downloads_directory", lambda: out)

    def _no_dialog(*args, **kwargs):
        raise AssertionError(f"export reported a failure: {args[2] if len(args) > 2 else args}")

    monkeypatch.setattr(ui_layout.QMessageBox, "warning", staticmethod(_no_dialog))
    monkeypatch.setattr(ui_layout.QMessageBox, "critical", staticmethod(_no_dialog))
    yield editor
    editor.close()


def _export(studio, qapp) -> tuple[Path, str]:
    out = figma_export.downloads_directory()
    before = set(out.glob("*.svg"))
    studio._export_target_as_figma()
    qapp.processEvents()
    fresh = sorted(set(out.glob("*.svg")) - before)
    assert fresh, "Download as Figma wrote no file at all"
    path = fresh[-1]
    return path, path.read_text(encoding="utf-8")


def _text(studio, qapp) -> str:
    return _export(studio, qapp)[1]


# ── the document must survive a strict importer ─────────────────────────────
def test_export_is_well_formed_with_a_single_svg_root(studio, hud_panel, qapp):
    text = _text(studio, qapp)
    assert text.count("<svg") == 1, "a nested/unclosed <svg> stops importers"
    assert text.count("</svg>") == 1
    assert text.count("<?xml") == 1
    minidom.parseString(text)          # the exported file's own bytes
    root = ET.fromstring(text)
    assert root.tag == f"{SVG}svg"


def test_captures_enter_as_fragments_not_documents(qapp, hud_panel):
    logo = hud_panel.findChild(QLabel, "LogoIcon")
    fragment = ui_layout._figma_svg_capture(logo, include_children=False)
    assert fragment, "the pixmap logo produced no capture at all"
    assert "<svg" not in fragment and "<?xml" not in fragment and "</svg" not in fragment
    assert ui_layout.runtime_fragment_has_content(fragment)


# ── every element must be present, painted, and correctly sized ─────────────
def test_every_exported_element_gets_its_own_non_empty_layer(studio, hud_panel, qapp):
    elements = studio._figma_export_elements(hud_panel, "HudPanel")
    assert elements, "the Studio exported no elements from a populated panel"
    text = _text(studio, qapp)
    root = ET.fromstring(text)

    missing = [name for name in elements if f'id="{name}"' not in text]
    assert not missing, f"layers dropped from the export: {missing[:8]}"

    by_id = {group.get("id"): group for group in root.iter(f"{SVG}g")}
    empty = [name for name in elements if name in by_id and not len(list(by_id[name]))]
    assert not empty, f"blank layers exported: {empty[:8]}"


def test_widget_names_survive_normalisation(studio, hud_panel):
    """Custom-painted and raster widgets must keep their capture payloads."""
    elements = studio._figma_export_elements(hud_panel, "HudPanel")
    captured = [name for name, spec in elements.items()
                if spec.get("_runtime_svg") or spec.get("_runtime_image")]
    assert captured, "no widget kept a runtime capture (they used to be dropped)"
    assert "LogoIcon" in captured or "Reactor" in captured


def test_child_of_a_custom_element_is_nested_not_lost(studio, hud_panel):
    elements = studio._figma_export_elements(hud_panel, "HudPanel")
    nested = elements.get("NestedLabel")
    assert nested is not None, "a widget hosted by a custom element vanished"
    assert nested["parent"] == "HUD Card One"
    # The card itself comes from the persisted spec exactly once: it must not
    # also be re-captured as a runtime layer, or its art would be drawn twice.
    assert [name for name in elements if name.startswith("HUD Card One")] == ["HUD Card One"]


def test_canvas_matches_the_panel_and_is_not_clipped(studio, hud_panel, qapp):
    text = _text(studio, qapp)
    root = ET.fromstring(text)
    assert root.get("width") == f"{hud_panel.width()}px"
    assert root.get("height") == f"{hud_panel.height()}px"
    box = [float(v) for v in root.get("viewBox").split()]
    assert box[2] == float(hud_panel.width()) and box[3] == float(hud_panel.height())


# ── backgrounds, borders, gradients, assets ─────────────────────────────────
def test_panel_background_keeps_gradient_and_alpha(studio, hud_panel, qapp):
    text = _text(studio, qapp)
    assert "linearGradient" in text, "the panel's gradient background was dropped"
    assert "#0a3a4a" in text, "the gradient's own colour disappeared"
    assert 'stop-opacity="0.502"' in text, "rgba() stop alpha was flattened to opaque"
    root = ET.fromstring(text)
    canvas = [el for el in root.iter(f"{SVG}rect")
              if float(el.get("width") or 0) >= hud_panel.width()]
    assert canvas and any((el.get("fill") or "none") != "none" for el in canvas), \
        "nothing paints the canvas — the export would look blank"


def test_styled_child_keeps_its_own_rule(studio, hud_panel):
    """The cascade is resolved per widget, so each layer gets its own paint."""
    elements = studio._figma_export_elements(hud_panel, "HudPanel")
    send = elements["SendButton"]["background"]
    assert send["color"] == "#00526e", "the button lost its own rgba() background"
    assert 35 <= send["opacity"] <= 45, "rgba(0,82,110,100) is ~39% opaque"
    assert not send.get("gradient", {}).get("enabled"), \
        "the panel's gradient must not be copied onto its children"


def test_embedded_assets_render_without_external_files(studio, hud_panel, qapp):
    text = _text(studio, qapp)
    root = ET.fromstring(text)
    images = list(root.iter(f"{SVG}image"))
    assert images, "the panel's raster art produced no <image> layer"
    for element in images:
        href = element.get("href") or ""
        xlink = element.get(XLINK_HREF) or ""
        assert href.startswith("data:image/") and xlink.startswith("data:image/"), \
            f"asset is neither embedded nor dual-spelled: {dict(element.attrib)}"
        assert float(element.get("width") or 0) > 0 and float(element.get("height") or 0) > 0


# ── the Studio's own state must not leak into the file ──────────────────────
def test_a_hidden_panel_still_exports_completely(studio, hud_panel, qapp):
    """Editing happens with the HUD closed: 'visible' is relative to the panel."""
    hud_panel.hide()
    qapp.processEvents()
    elements = studio._figma_export_elements(hud_panel, "HudPanel")
    assert elements, "a closed panel exported nothing but a background"
    assert "LogoIcon" in elements and "SendButton" in elements
    text = _text(studio, qapp)
    assert 'id="LogoIcon"' in text and 'id="SendButton"' in text


def test_repeat_exports_are_separate_files_and_stay_valid(studio, hud_panel, qapp):
    first_path, first = _export(studio, qapp)
    second_path, second = _export(studio, qapp)
    assert first_path != second_path, "an earlier export was overwritten"
    for text in (first, second):
        minidom.parseString(text)


def test_export_refuses_a_panel_that_was_never_laid_out(studio, qapp, monkeypatch):
    """A 0x0 panel must be reported, not exported as a blank one-pixel canvas."""
    unbuilt = QFrame(studio.main.surface)
    unbuilt.setObjectName("UnbuiltPanel")
    unbuilt.setFixedSize(0, 0)
    studio._target_map = [("UnbuiltPanel", unbuilt)]
    studio._current_target_name = "UnbuiltPanel"
    warned = {}

    def _warn(parent, title, text, *args, **kwargs):
        warned["text"] = text

    monkeypatch.setattr(ui_layout.QMessageBox, "warning", staticmethod(_warn))
    studio._export_target_as_figma()
    assert "no size" in str(warned.get("text", "")).lower(), \
        f"expected an honest refusal, got: {warned}"
    unbuilt.deleteLater()
