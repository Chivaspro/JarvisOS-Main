"""Approve / Confirm UI tests (offscreen).

Verifies what can be checked headlessly:

* The Approve card is LIGHT BLUE and no longer uses the warm/orange accent,
  without touching the shared global palette.
* The card is sized to its FULL content (fixed width, height that grows to fit
  the title + detail + buttons) so it can never be cropped into a square.
* The card is its OWN window — separate from the app window the startup logo is
  drawn in, always on top so the logo cannot cover it, and draggable anywhere.
  It used to be a child of the HUD surface and was reported as partly hidden
  behind the logo with no way to move it clear.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_confirm_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

LIGHT_BLUE = "#8ad2ff"


def rect_inside(inner, outer) -> bool:
    return (outer.left() <= inner.left() and inner.right() <= outer.right()
            and outer.top() <= inner.top() and inner.bottom() <= outer.bottom())


def send_mouse(widget, kind, local: QPointF, glob: QPointF,
               button=Qt.MouseButton.LeftButton):
    """Deliver a real mouse event so the drag handlers are exercised as used."""
    buttons = button if kind is not QEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    event = QMouseEvent(kind, local, glob, button, buttons,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def test_confirm_banner_is_light_blue_not_warm_accent(qapp):
    banner = ui.ConfirmBanner("Delete file", "This removes C:\\x.txt permanently.")
    css = banner.styleSheet()
    # The Approve UI now uses the HUD blue token (a light blue), not the warm accent.
    assert ui.C.PRI in css, "the Approve UI must use the light-blue primary token"
    # The old warm treatment must be gone.
    assert "rgba(255,107,0" not in css, "the orange hover fill is still present"
    assert "rgba(14, 3, 0" not in css, "the warm dark background is still present"
    assert ui.C.ACC not in css, "the global warm accent is still wired into the Approve UI"


def test_global_palette_not_recolored(qapp):
    # TASK 5 says do not change unrelated colors: the shared accent token itself
    # must be untouched even though the banner no longer uses it, and the blue
    # token is a distinct value (not the warm accent).
    assert ui.C.ACC, "global accent token must still exist"
    assert ui.C.PRI and str(ui.C.PRI).lower() != str(ui.C.ACC).lower()


def test_confirm_banner_sizes_to_full_content_not_square(qapp):
    container = QWidget()
    container.resize(1200, 800)
    banner = ui.ConfirmBanner("Approve code execution", "aider will modify several files.")
    banner.show_as_window(container)
    need = banner.sizeHint()

    # Fixed, non-square width; height always accommodates the content.
    assert banner.width() == banner._OW == 430
    assert banner.height() >= need.height(), "content taller than the card -> clipping"
    assert banner.height() != banner.width(), "the Approve UI is a square (clip symptom)"
    # Long detail must still be laid out (word-wrap keeps width constant, height grows).
    assert banner.height() > 60


def test_the_card_is_its_own_window_separate_from_the_logo(qapp):
    """The startup logo is raised inside the app window; this must not be."""
    container = QWidget()
    container.resize(1000, 700)
    banner = ui.ConfirmBanner("Confirm", "detail", parent=container)
    banner.show_as_window(container)
    assert banner.isWindow(), "the Approve UI must be its own window"
    flags = banner.windowFlags()
    assert flags & Qt.WindowType.WindowStaysOnTopHint, "the logo could cover it again"
    assert flags & Qt.WindowType.FramelessWindowHint
    banner.hide()


def test_the_card_opens_beside_the_app_not_over_its_centre(qapp):
    container = QWidget()
    container.resize(1000, 700)
    container.move(80, 60)
    container.show()
    banner = ui.ConfirmBanner("Confirm", "detail", parent=container)
    banner.show_as_window(container)

    area = container.screen().availableGeometry()
    geometry = banner.frameGeometry()
    # Fully on screen, never cropped by the desktop edge.
    assert rect_inside(geometry, area), f"{geometry} escapes {area}"
    # And clear of the app's centre, where the startup logo is drawn.
    assert not geometry.contains(container.frameGeometry().center()), \
        "the card is sitting on top of the logo again"
    banner.hide()


def test_the_card_can_be_dragged_anywhere(qapp):
    banner = ui.ConfirmBanner("Confirm", "drag me")
    banner.show_as_window()
    start = banner.pos()
    local = QPointF(banner.width() / 2, 20)          # an empty part of the card
    global_start = QPointF(banner.mapToGlobal(local.toPoint()))

    send_mouse(banner, QEvent.Type.MouseButtonPress, local, global_start)
    send_mouse(banner, QEvent.Type.MouseMove, local,
               QPointF(global_start.x() + 120, global_start.y() + 90))
    send_mouse(banner, QEvent.Type.MouseButtonRelease, local,
               QPointF(global_start.x() + 120, global_start.y() + 90))

    moved = banner.pos() - start
    assert (moved.x(), moved.y()) == (120, 90), f"drag moved the card by {moved}"
    banner.hide()


def test_an_unanswered_card_dismisses_itself(qapp):
    """A card that floats above every window must not stay there forever."""
    from security.confirm import TIMEOUT_SECONDS

    banner = ui.ConfirmBanner("Confirm", "nobody answers")
    assert banner._expiry.isSingleShot()
    assert banner._expiry.isActive()
    assert banner._expiry.interval() == int(max(5.0, TIMEOUT_SECONDS) * 1000)

    seen = []
    banner.answered.connect(seen.append)
    banner.show_as_window()
    banner._dismiss_unanswered()
    assert seen == [False], "an expired card must report itself as unanswered"


def test_the_buttons_still_answer_after_the_drag_support(qapp):
    from PyQt6.QtWidgets import QPushButton

    banner = ui.ConfirmBanner("Confirm", "detail")
    banner.show_as_window()
    seen = []
    banner.answered.connect(seen.append)
    buttons = {b.text().strip(): b for b in banner.findChildren(QPushButton)}
    buttons["▸  CONFIRM"].click()
    buttons["CANCEL"].click()
    assert seen == [True, False]
    banner.hide()


def test_dragging_stops_when_the_button_is_released(qapp):
    banner = ui.ConfirmBanner("Confirm", "drag me")
    banner.show_as_window()
    local = QPointF(banner.width() / 2, 20)
    global_start = QPointF(banner.mapToGlobal(local.toPoint()))
    send_mouse(banner, QEvent.Type.MouseButtonPress, local, global_start)
    send_mouse(banner, QEvent.Type.MouseButtonRelease, local, global_start)
    parked = banner.pos()

    # A plain move with no button held must not drag the card along.
    send_mouse(banner, QEvent.Type.MouseMove, local,
               QPointF(global_start.x() + 200, global_start.y() + 200))
    assert banner.pos() == parked
    banner.hide()


# ── the real window, exactly as the HUD shows it ─────────────────────────────

_REAL_CONFIG = {
    "assistant_name": "JARVIS", "user_name": "Operator",
    "gemini_api_key": "test-key", "os_system": "windows",
    "ui_x": 400, "ui_y": 300, "compact_size": 176,
    "reactor_opacity": 60, "reactor_stroke_opacity": 100,
    "voice_output_mode": "gemini_piper", "tts_engine": "onnx",
    "voice_name": "Charon", "tts_voice": "en-GB-RyanNeural",
    "voice_speed": 1.0, "voice_pitch": 1.0, "voice_volume": 1.0,
    "sapi_voice": "", "onnx_voice_model": "config/voices/jarvis-high.onnx",
    "onnx_voice_config": "config/voices/jarvis-high.json",
    "onnx_voice_speaker": "default", "onnx_execution_provider": "cpu",
    "elevenlabs_voice_id": "", "elevenlabs_model_id": "",
    "fish_audio_voice_id": "", "fish_audio_model_id": "",
    "features": {"remember_position": True, "drag_reactor": True},
}


@pytest.fixture()
def hud_window(qapp, tmp_path, monkeypatch):
    """A real MainWindow, so the confirm path runs exactly as it does live."""
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps(_REAL_CONFIG, indent=4), encoding="utf-8")
    monkeypatch.setattr(ui, "API_FILE", config_path)
    monkeypatch.setattr(ui, "_read_full_config", lambda: dict(_REAL_CONFIG))
    monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(_REAL_CONFIG))

    win = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    yield win
    win._shutdown_startup_sequence()
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_the_hud_shows_the_card_as_its_own_window(qapp, hud_window):
    """The production path: what the user actually gets when asked to confirm."""
    win = hud_window
    win._show_confirm_banner("Shut down the machine",
                             "This powers the PC off and cannot be undone from here.")
    card = win._confirm_overlay
    assert card is not None and card.isVisible(), "no confirmation appeared"
    assert card.isWindow(), "the card is still inside the logo's surface"
    assert card.parentWidget() is not getattr(win, "surface", None)
    # Owned by the app window, so it stays above it instead of behind the logo.
    assert card.parentWidget() is win
    assert card.height() >= card.sizeHint().height(), "content is cropped"

    area = card.screen().availableGeometry()
    assert rect_inside(card.frameGeometry(), area), "the card is off screen"
    assert not card.frameGeometry().contains(win.frameGeometry().center()), \
        "the card is sitting on the startup logo's centre"

    win.hide_confirm()
    qapp.processEvents()


def test_every_control_sits_inside_the_visible_card(qapp, hud_window):
    """The complaint was a partly hidden card: nothing may fall outside it."""
    win = hud_window
    win._show_confirm_banner("Shut down the machine",
                             "This powers the PC off and cannot be undone. " * 12)
    card = win._confirm_overlay
    frame = card.rect()
    for child in card.findChildren(QWidget):
        if child.isWindow() or not child.isVisible():
            continue
        top_left = child.mapTo(card, QPoint(0, 0))
        assert frame.contains(QRect(top_left, child.size())), \
            f"{child.__class__.__name__} {getattr(child, 'text', lambda: '')()!r} " \
            "is outside the card"
    win.hide_confirm()
    qapp.processEvents()


def test_the_next_confirmation_does_not_stack_on_the_last(qapp, hud_window):
    win = hud_window
    win._show_confirm_banner("First", "detail")
    first = win._confirm_overlay
    win._show_confirm_banner("Second", "detail")
    second = win._confirm_overlay
    assert second is not first
    assert not first.isVisible(), "two approval cards are on screen at once"
    win.hide_confirm()
    qapp.processEvents()
