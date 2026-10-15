"""Figma export unit tests (Download as Figma feature).

Proves: the exporter never needs Qt to run, produces well-formed SVG,
preserves nesting/z-order/text/gradients, degrades unsupported element kinds
to a labelled placeholder instead of crashing or silently dropping them,
resists cyclic/malformed panel data, never overwrites a previous export, and
rejects genuinely invalid input with FigmaExportError instead of writing a
bad file.

The capture/colour cases cover the "imported SVG is incomplete" bug: a real
``QSvgGenerator`` document must become a balanced fragment (an unclosed nested
``<svg>`` made every design tool stop reading), and Qt's own paint vocabulary
(``rgba()``, ``qlineargradient``, per-corner radius, dashed borders) must reach
the SVG instead of collapsing to a hard-coded fallback colour.
"""
from __future__ import annotations

import sys
import xml.dom.minidom as minidom
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from figma_export import (  # noqa: E402
    FigmaExportError,
    export_panel_to_figma,
    normalize_runtime_svg_fragment,
    parse_qt_gradient,
    parse_qt_paint_value,
    qss_border,
    qss_paint,
    qss_radius,
    runtime_fragment_has_content,
    sanitize_filename,
    split_color,
    _build_tree,
)


# Verbatim shape of what ``QSvgGenerator`` hands back: an XML declaration, an
# ``<svg>`` root sized in millimetres, Qt's own ``<title>``/``<desc>`` chrome and
# a document-level ``</svg>``. Embedding this raw is what made every exported
# panel unreadable past the first captured widget.
QT_CAPTURE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<svg width="62.7944mm" height="17.6389mm"\n viewBox="0 0 178 50"\n'
    ' xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"\n'
    ' version="1.2" baseProfile="tiny">\n'
    '<title>QLabel</title>\n<desc>Generated with Qt</desc>\n'
    '<defs>\n<linearGradient id="gradient1" gradientUnits="objectBoundingBox">\n'
    '    <stop offset="0" stop-color="#00cfff" stop-opacity="1" />\n'
    '</linearGradient>\n</defs>\n'
    '<g fill="none" stroke="black" stroke-width="1">\n'
    '<g fill="url(#gradient1)">\n<path d="M 0 0 L 10 10 Z"/>\n</g>\n'
    '<image x="0" y="0" width="10" height="10" preserveAspectRatio="none"'
    ' xlink:href="data:image/png;base64,cG5n"/>\n'
    '<text x="0" y="9">JARVIS</text>\n'
    '</g>\n</svg>\n'
)


def _spec(name: str, kind: str = "Panel", parent: str = "", **overrides) -> dict:
    base = {
        "name": name, "type": kind, "kind": kind, "parent": parent,
        "x": 10, "y": 10, "w": 120, "h": 60, "z": 0,
        "visible": True, "enabled": True, "hidden": False,
        "opacity": 100, "rotation": 0.0, "rotation_center": [0.5, 0.5],
        "background": {"color": "#04121b", "opacity": 72,
                        "gradient": {"enabled": False, "kind": "linear", "angle": 90,
                                     "stops": [[0.0, "#00cfff"], [1.0, "#04121b"]]}},
        "image": {"path": "", "opacity": 100, "fit": "Contain"},
        "text": name,
        "text_style": {"color": "#e6f4ff", "opacity": 100, "family": "Exo 2", "size": 11,
                        "bold": False, "italic": False, "underline": False, "align": "center",
                        "v_align": "middle", "wrap": True, "letter_spacing": 0.0,
                        "stroke": {"enabled": False}, "shadow": {"enabled": False},
                        "glow": {"enabled": False}},
        "stroke": {"enabled": False, "color": "#00cfff", "width": 2.0, "opacity": 100},
        "borders": {"top": {"width": 0}, "right": {"width": 0}, "bottom": {"width": 0},
                     "left": {"width": 0}, "radius": {"tl": 8, "tr": 8, "br": 8, "bl": 8}},
        "shadow": {"enabled": False}, "glow": {"enabled": False},
    }
    base.update(overrides)
    return base


def test_export_is_well_formed_and_preserves_structure(tmp_path):
    elements = {
        "Header": _spec("Header", "Text", parent="Content Surface", text="Hello there"),
        "Action Button": _spec("Action Button", "Button", parent="Content Surface", x=10, y=60,
                                background={"color": "#0a3a4a", "opacity": 100,
                                            "gradient": {"enabled": True, "kind": "linear", "angle": 45,
                                                         "stops": [[0.0, "#00cfff"], [1.0, "#023142"]]}}),
        "Nested Icon": _spec("Nested Icon", "Panel", parent="Action Button", w=16, h=16),
    }
    path = export_panel_to_figma("Content Surface", (400, 300), elements,
                                  panel_background="#04121b", output_dir=tmp_path)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    minidom.parseString(text)  # raises on malformed XML
    for name in ("Header", "Action Button", "Nested Icon"):
        assert f'id="{name}"' in text
    assert "<linearGradient" in text
    # nesting: Nested Icon must be emitted inside Action Button's <g>, not as a sibling
    assert text.index('id="Action Button"') < text.index('id="Nested Icon"') < text.rindex("</g>")


def test_populated_export_with_unmatched_parents_never_becomes_blank(tmp_path):
    elements = {
        "Button": _spec("Button", "Button", parent="Other Panel", x=15, y=20),
        "Label": _spec("Label", "Text", parent="Other Panel", x=20, y=60, text="Visible"),
    }
    path = export_panel_to_figma("Chat Panel", (300, 200), elements, output_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert 'id="Button"' in text
    assert 'id="Label"' in text
    assert '>Visible<' in text




def test_runtime_svg_and_image_layers_are_embedded_without_nested_xml(tmp_path):
    elements = {
        "Vector Runtime": _spec("Vector Runtime", "Panel", parent="Panel",
                                 _runtime_svg='<rect width="20" height="10" fill="#00cfff"/>'),
        "Web Surface": _spec("Web Surface", "WebView", parent="Panel",
                              _runtime_image={"data": "cG5n", "mime": "image/png",
                                              "width": 40, "height": 20}),
    }
    path = export_panel_to_figma("Panel", (100, 80), elements, output_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.count('<?xml') == 1
    assert text.count('<svg ') == 1
    assert '<rect width="20" height="10" fill="#00cfff"/>' in text
    assert 'data:image/png;base64,cG5n' in text
    minidom.parseString(text)


def test_unsupported_kind_degrades_to_placeholder_not_a_crash(tmp_path):
    elements = {"Broken": _spec("Broken", "WebView", parent="Panel A")}
    path = export_panel_to_figma("Panel A", (100, 100), elements, output_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "not convertible" in text
    minidom.parseString(text)


def test_cyclic_parent_chain_does_not_hang_or_crash():
    elements = {"A": _spec("A", parent="B"), "B": _spec("B", parent="A")}
    tree = _build_tree(elements, "A")
    assert isinstance(tree, list)  # terminates; does not raise or infinite-loop


def test_malformed_element_entries_are_skipped_not_fatal(tmp_path):
    elements = {"junk": "not a dict", "ok": _spec("ok", parent="Root")}
    path = export_panel_to_figma("Root", (50, 50), elements, output_dir=tmp_path)
    assert path.exists()
    assert 'id="ok"' in path.read_text(encoding="utf-8")


def test_no_panel_selected_raises_figma_export_error(tmp_path):
    with pytest.raises(FigmaExportError):
        export_panel_to_figma("", (100, 100), {}, output_dir=tmp_path)


def test_invalid_dimensions_raise_figma_export_error(tmp_path):
    with pytest.raises(FigmaExportError):
        export_panel_to_figma("Panel", ("nope", "nope"), {}, output_dir=tmp_path)


def test_repeat_export_never_overwrites_previous_file(tmp_path):
    first = export_panel_to_figma("Dup", (10, 10), {}, output_dir=tmp_path)
    second = export_panel_to_figma("Dup", (10, 10), {}, output_dir=tmp_path)
    assert first != second
    assert first.exists() and second.exists()


def test_sanitize_filename_strips_illegal_characters():
    assert sanitize_filename('Weird:/Name*?"<>|') == "WeirdName"
    assert sanitize_filename("   ") == "Panel"


# ── runtime capture normalisation (the "incomplete SVG" root cause) ──────────

def test_real_qt_capture_normalises_to_a_balanced_fragment():
    fragment = normalize_runtime_svg_fragment(QT_CAPTURE)
    assert "<?xml" not in fragment
    assert "<svg" not in fragment and "</svg" not in fragment
    assert "<title>" not in fragment and "<desc>" not in fragment
    # the actual artwork survives untouched
    assert "<path" in fragment and "<text" in fragment and "gradient1" in fragment
    assert runtime_fragment_has_content(fragment)


def test_export_with_real_captures_is_well_formed(tmp_path):
    import xml.dom.minidom as md
    elements = {
        "Logo": _spec("Logo", "Image", parent="Panel", _runtime_svg=QT_CAPTURE),
        "Reactor": _spec("Reactor", "Panel", parent="Panel", x=40, _runtime_svg=QT_CAPTURE),
        "Title": _spec("Title", "Text", parent="Panel", y=60, _runtime_svg=QT_CAPTURE),
    }
    path = export_panel_to_figma("Panel", (300, 200), elements, output_dir=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.count("<svg") == 1 and text.count("</svg>") == 1
    assert text.count("<?xml") == 1
    md.parseString(text)          # raises on any unbalanced tag
    assert text.count("<path") >= 3, "every captured widget must still paint"


def test_capture_that_painted_nothing_degrades_instead_of_staying_blank(tmp_path):
    """Qt emits an empty ``<g>`` scaffold for widgets it cannot vectorise."""
    scaffold = ('<?xml version="1.0"?>\n<svg viewBox="0 0 20 20">\n'
                '<g fill="none"><g fill="none"></g></g>\n</svg>\n')
    assert not runtime_fragment_has_content(normalize_runtime_svg_fragment(scaffold))
    elements = {"Ghost": _spec("Ghost", "Panel", parent="Panel", _runtime_svg=scaffold)}
    text = export_panel_to_figma("Panel", (100, 100), elements,
                                 output_dir=tmp_path).read_text(encoding="utf-8")
    minidom.parseString(text)
    assert 'id="Ghost"' in text
    assert text.count("<path") >= 1, "the element must fall back to an editable shape"


def test_images_are_spelled_for_both_figma_and_penpot(tmp_path):
    elements = {"Snap": _spec("Snap", "WebView", parent="Panel",
                              _runtime_image={"data": "cG5n", "mime": "image/png",
                                              "width": 30, "height": 20})}
    text = export_panel_to_figma("Panel", (100, 100), elements,
                                 output_dir=tmp_path).read_text(encoding="utf-8")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    image = next(el for el in root.iter() if el.tag.endswith("}image"))
    assert image.get("href") == "data:image/png;base64,cG5n"
    assert image.get("{http://www.w3.org/1999/xlink}href") == "data:image/png;base64,cG5n"
    assert image.get("width") == "30.00", "the embedded asset keeps its real size"


def test_qt_capture_image_hrefs_are_dual_spelled():
    """Attribute-level check: ``xlink:href`` alone would satisfy a substring test."""
    import xml.etree.ElementTree as ET
    fragment = normalize_runtime_svg_fragment(QT_CAPTURE)
    element = ET.fromstring(f"<g xmlns='http://www.w3.org/2000/svg' "
                            f"xmlns:xlink='http://www.w3.org/1999/xlink'>{fragment}</g>")
    image = next(el for el in element.iter() if el.tag.endswith("}image"))
    assert image.get("href") == "data:image/png;base64,cG5n"
    assert image.get("{http://www.w3.org/1999/xlink}href") == "data:image/png;base64,cG5n"


# ── unreachable elements are recovered, not dropped ───────────────────────
def test_orphaned_parent_chains_are_still_exported(tmp_path):
    elements = {
        "Known": _spec("Known", "Panel", parent="Panel"),
        "Orphan": _spec("Orphan", "Text", parent="Deleted Container", text="KEEP ME"),
    }
    text = export_panel_to_figma("Panel", (100, 100), elements,
                                 output_dir=tmp_path).read_text(encoding="utf-8")
    assert 'id="Orphan"' in text and "KEEP ME" in text


def test_build_tree_recovers_every_unreachable_element():
    elements = {"A": _spec("A", parent="Missing"), "B": _spec("B", parent="A")}
    tree = _build_tree(elements, "Root")

    def names(nodes):
        found = set()
        for node in nodes:
            found.add(node["_name"])
            found |= names(node.get("_children") or [])
        return found

    # A is re-homed on the canvas and B keeps its real place underneath it.
    assert names(tree) == {"A", "B"}
    assert [node["_name"] for node in tree] == ["A"]


# ── Qt stylesheet -> SVG paint translation (backgrounds/opacity/gradients) ──
PANEL_SHEET = (
    "QPushButton{background:rgba(0,82,110,100);color:#ff0000;}\n"
    "QFrame#Panel{background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
    "stop:0 rgba(4,18,27,255),stop:1 rgba(10,58,74,128));"
    "border:2px dashed rgba(138,210,255,90);border-radius:14px;}\n"
)


def test_css_paint_prefers_the_rule_that_addresses_the_widget():
    paint = qss_paint(PANEL_SHEET, {"QFrame", "Panel"})
    assert paint and paint["color"] == "#04121b"
    assert paint["gradient"]["enabled"] is True
    # The button's own red text must not be read as the panel's paint.
    assert "#ff0000" not in str(paint)


def test_gradient_keeps_direction_and_per_stop_alpha():
    gradient = qss_paint(PANEL_SHEET, {"QFrame", "Panel"})["gradient"]
    assert gradient["coords"] == [0.0, 0.0, 1.0, 1.0]
    assert gradient["stops"][0][2] == 1.0
    assert 0.4 < gradient["stops"][1][2] < 0.6, "rgba(10,58,74,128) is ~50% opaque"


def test_radius_and_border_keep_what_qt_painted():
    assert qss_radius(PANEL_SHEET, {"QFrame", "Panel"}) == {"tl": 14.0, "tr": 14.0,
                                                            "br": 14.0, "bl": 14.0}
    edge = qss_border(PANEL_SHEET, {"QFrame", "Panel"})["edges"][""]
    assert edge == ("#8ad2ff", 2.0, 90 / 255, "Dashed")


def test_split_color_accepts_every_form_this_project_writes():
    assert split_color("#0a3a4a") == ("#0a3a4a", 1.0)
    assert split_color("#0af") == ("#00aaff", 1.0)
    assert split_color("#04121bff") == ("#04121b", 1.0)
    assert split_color("rgba(2,14,22,250)")[0] == "#020e16"
    assert abs(split_color("rgba(2,14,22,250)")[1] - 250 / 255) < 1e-6
    assert split_color("rgb(255,0,170)") == ("#ff00aa", 1.0)
    assert split_color("transparent") == ("#000000", 0.0)
    assert parse_qt_paint_value("none") is None
    assert parse_qt_gradient("rgba(0,0,0,1)") is None


def test_background_alpha_reaches_the_svg(tmp_path):
    elements = {"Card": _spec("Card", "Panel", parent="Panel",
                              background={"color": "#020e16", "opacity": 100,
                                          "gradient": {"enabled": False}})}
    text = export_panel_to_figma("Panel", (100, 100), elements,
                                 output_dir=tmp_path).read_text(encoding="utf-8")
    assert 'fill="#020e16" fill-opacity="1.000"' in text


def test_panel_background_can_be_a_parsed_qt_paint(tmp_path):
    paint = qss_paint(PANEL_SHEET, {"QFrame", "Panel"})
    text = export_panel_to_figma("Panel", (100, 100), {}, panel_background=paint,
                                 output_dir=tmp_path).read_text(encoding="utf-8")
    assert "<linearGradient" in text
    assert 'stop-opacity="0.502"' in text, "the rgba() stop keeps its transparency"
    minidom.parseString(text)


def test_captured_layer_keeps_its_glow_and_shadow(tmp_path):
    """Qt graphics effects don't survive ``render()``, so the group re-applies them."""
    elements = {"Reactor": _spec(
        "Reactor", "Panel", parent="Panel", _runtime_svg=QT_CAPTURE,
        glow={"enabled": True, "color": "#00cfff", "radius": 12, "opacity": 80,
              "strength": 60},
        shadow={"enabled": True, "color": "#000000", "opacity": 50, "blur": 8,
                "dx": 0, "dy": 3})}
    text = export_panel_to_figma("Panel", (200, 120), elements,
                                 output_dir=tmp_path).read_text(encoding="utf-8")
    minidom.parseString(text)
    assert 'filter="url(#' in text, "the captured layer lost its effect"
    assert text.count("<feDropShadow") == 2
    assert 'flood-color="#00cfff"' in text
