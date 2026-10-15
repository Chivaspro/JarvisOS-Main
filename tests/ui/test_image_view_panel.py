"""Image View panel — the title-less surface JARVIS shows pictures in.

The brief asks for a panel exactly like the Vision surface but for images:

* no title, no header, no caption text on the panel — the picture is the panel;
* one exit control in the top-right corner drawn as a bare glyph, with no
  background and no stroke;
* AI-generated, researched and imported images all land here, while JARVIS
  still knows what the image is so it can describe it out loud.

The tests are split in two groups. The panel itself is exercised on a bare
``_ImageViewPanel`` (no HUD, so image-decoding failures can never disturb a
whole window), and the HUD-facing behaviour — routing, the layout-Studio target
list, position memory — runs against a real ``MainWindow``.
"""
from __future__ import annotations

import json

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QLabel, QPushButton

from .test_camera_pipeline import _pump, camera, qapp, ui  # noqa: F401  (fixtures)


CONFIG = {
    "assistant_name": "JARVIS",
    "user_name": "Operator",
    "gemini_api_key": "test-key",
    "os_system": "windows",
    "compact_size": 176,
    "reactor_opacity": 60,
    "voice_output_mode": "gemini_piper",
    "tts_engine": "onnx",
    "features": {"remember_position": True, "floating_windows": True},
}


def _png_pixmap(w: int = 64, h: int = 48, color=None) -> QPixmap:
    """A real bitmap, no file needed."""
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(color or QColor(0, 200, 255))
    return QPixmap.fromImage(image)


def _write_png(path, w: int = 64, h: int = 48, color=None):
    """A real PNG on disk (Qt encoder — no image library needed)."""
    assert _png_pixmap(w, h, color).save(str(path), "PNG")
    return path


@pytest.fixture()
def panel(qapp):  # noqa: F811
    """A bare Image View panel — no HUD involved."""
    view = ui._ImageViewPanel()
    yield view
    view.close()


@pytest.fixture()
def window(qapp, tmp_path, monkeypatch, camera):  # noqa: F811
    """A real HUD window whose config lives in the test's temp directory.

    ``CONFIG_DIR`` is redirected too, so the shared ``config/ui_layout.json`` is
    never read or written by a test.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "api_keys.json"
    config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
    monkeypatch.setattr(ui, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(ui, "API_FILE", config_path)
    monkeypatch.setattr(ui, "_read_full_config", lambda: dict(CONFIG))
    monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(CONFIG))
    monkeypatch.setattr(ui._FeatheredCamView, "_save_geom", lambda self: None)
    monkeypatch.setattr(ui._FeatheredCamView, "_load_geom", lambda self: None)

    win = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    yield win
    try:
        # Same teardown as the production close: stop and join the camera
        # workers first, then take the floating surfaces (their own top-level
        # windows) down, then the window itself.
        win._shutdown_camera_threads(timeout=5.0)
        for name in ("_cam_live_lbl", "_cam_preview", "_image_view_panel"):
            child = getattr(win, name, None)
            if child is not None:
                child.close()
        qapp.processEvents()
        win.close()
    except Exception:
        pass
    qapp.processEvents()


# ── the panel itself ─────────────────────────────────────────────────────────

def test_panel_is_frameless_and_has_no_title_or_labels(panel, qapp):
    panel.show()
    qapp.processEvents()
    assert panel.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert panel.findChildren(QLabel) == [], "the Image View panel must not print a title"
    assert panel.findChildren(QPushButton) == [panel.exit_button]


def test_exit_control_is_a_bare_glyph_in_the_top_right(panel, qapp):
    panel.show()
    qapp.processEvents()
    btn = panel.exit_button
    sheet = btn.styleSheet().replace(" ", "").lower()
    assert btn.isFlat()
    assert "background:transparent" in sheet and "border:none" in sheet
    assert btn.text() == "", "the exit glyph is painted, not a text button"
    assert btn.x() + btn.width() >= panel.width() - 12
    assert btn.y() <= 12


def test_exit_control_paints_no_background_and_no_stroke(panel, qapp):
    panel.show()
    qapp.processEvents()
    btn = panel.exit_button
    pixmap = QPixmap(btn.width(), btn.height())
    pixmap.fill(QColor(0, 0, 0, 0))
    btn.render(pixmap)
    image = pixmap.toImage()
    for x, y in ((0, 0), (btn.width() - 1, 0), (0, btn.height() - 1),
                 (btn.width() - 1, btn.height() - 1)):
        assert image.pixelColor(x, y).alpha() == 0, "no frame may be painted around the glyph"
    painted = any(image.pixelColor(x, y).alpha() > 0
                  for x in range(btn.width()) for y in range(btn.height()))
    assert painted, "the × glyph itself must be drawn"


def test_exit_control_hides_the_panel(panel, qapp):
    panel.show()
    qapp.processEvents()
    assert panel.isVisible()
    panel.exit_button.click()
    qapp.processEvents()
    assert not panel.isVisible()


def test_undecodable_image_is_refused_and_the_last_frame_is_kept(panel, qapp, tmp_path):
    assert panel.set_image_bytes(b"", "empty") is False
    assert panel.set_pixmap(_png_pixmap(), "first image") is True
    before = panel._current_pixmap()

    assert panel.set_image_bytes(b"this is not an image", "junk") is False
    assert panel.set_image_path(tmp_path / "does_not_exist.png", "missing") is False
    assert panel.caption() == "first image", "a refused image must not replace the caption"
    assert panel._current_pixmap() is before


def test_painting_an_empty_panel_does_not_raise(panel):
    pixmap = QPixmap(240, 180)
    pixmap.fill(QColor(0, 0, 0, 0))
    panel.render(pixmap)                      # no image loaded: must still be safe
    assert not panel.has_image()


def test_scaled_preview_is_cached_per_size(panel):
    panel.set_pixmap(_png_pixmap(900, 700), "cached")
    panel.resize(640, 480)
    first = panel._current_pixmap()
    assert panel._current_pixmap() is first, "the scaled pixmap must be reused, not rebuilt"
    assert panel._scaled_for == panel.size()

    panel.resize(520, 400)
    second = panel._current_pixmap()
    assert second is not first
    assert panel._scaled_for == panel.size()


def test_caption_is_kept_for_the_tooltip_and_for_jarvis(panel):
    panel.set_pixmap(_png_pixmap(), "blue arc reactor render")
    assert panel.caption() == "blue arc reactor render"
    assert panel.toolTip() == "blue arc reactor render"


# ── the HUD uses it for every picture ────────────────────────────────────────

def test_show_image_path_displays_in_the_chat_panel_stage(window, qapp, tmp_path):
    """The picture is shown on the embedded Chat Panel Image stage.

    ``_image_view_panel`` is deliberately retained as a hidden cache/analysis
    surface (see ``show_image_view``) — nothing may open it as a separate
    top-level window any more, so asserting it is *visible* would encode the
    pre-redesign behaviour rather than the current contract.
    """
    png = _write_png(tmp_path / "generated.png")
    window.show_image_path(str(png), "AI generated image")
    _pump(qapp, 0.3)

    view = window._image_view_panel
    assert not view.isVisible(), "the legacy Image View must not open as a window"
    assert view.has_image(), "it still holds the picture for its own tools"
    assert view.caption() == "AI generated image"
    assert window.image_view_caption == "AI generated image"

    host = window._command_window
    assert host is not None and host.isVisible()
    assert host._vision_visual.currentWidget() is host._image_view_stage


def test_show_image_bytes_fills_both_surfaces(window, qapp, tmp_path):
    png = _write_png(tmp_path / "researched.png")
    window._show_image_bytes(png.read_bytes(), "researched image")
    qapp.processEvents()

    assert window._image_view_panel.has_image()
    # The analysis panel keeps its copy: its tools still work on the same picture.
    assert not window._image_label.pixmap().isNull()


def test_image_view_is_a_layout_studio_target(window):
    assert window._system_layout_widgets["Image View"] is window._image_view_panel
    names = [name for name, _widget in ui._layout_root_candidates(window)]
    assert "Image View" in names


def test_panel_keeps_its_own_remembered_position_key(window):
    view = window._image_view_panel
    assert view.property("_jarvis_panel_key") == "image_view"
    assert view.property("_jarvis_floating_panel") is True
