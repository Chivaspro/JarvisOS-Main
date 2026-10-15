"""Vision control drawer (the toggles rail on the camera stage).

The rail used to be permanent chrome that ate a quarter of the stage and could
not be scrolled. The contract now is:

* It is collapsed on arm, so the camera owns the whole stage by default.
* A single icon-only handle in the picture's top-left corner opens and closes
  it, and the slide is animated (a running width animation, not a snap).
* The switches inside stay functional while the drawer is closed — hiding the
  rail must not disable a Vision feature.
* EXIT lives on the picture itself, so Vision mode can always be left with the
  drawer shut.
"""
from __future__ import annotations

from PyQt6.QtCore import QAbstractAnimation, Qt

from .test_camera_pipeline import (  # noqa: F401  (fixtures re-exported)
    _pump, camera, qapp, window,
)
from .test_camera_pipeline import ui

RAIL_WIDTH = ui.RefinedCommandWindow._VISION_RAIL_WIDTH


def _command_window(qapp, window):
    window.show()
    window._toggle_command_window()
    qapp.processEvents()
    cmd = window._command_window
    cmd._save_vision_options = lambda: None
    return cmd


def _arm_vision(qapp, window, cmd):
    """Arm Vision and let the stage take its real geometry."""
    cmd._vision_btn.setChecked(True)
    _pump(qapp, 0.4)
    assert cmd._vision_btn.isChecked()


# ── default state ───────────────────────────────────────────────────────────

def test_rail_is_collapsed_on_open(qapp, window):
    cmd = _command_window(qapp, window)
    scroll = cmd._vision_control_scroll

    assert scroll.maximumWidth() == 0, "the drawer must not reserve stage width"
    assert scroll.isHidden(), "the drawer must start hidden"
    assert not cmd._vision_rail_btn.isChecked()


def test_handle_is_an_icon_button_in_the_top_left(qapp, window, camera):
    cmd = _command_window(qapp, window)
    _arm_vision(qapp, window, cmd)
    _pump(qapp, 0.3)

    btn = cmd._vision_rail_btn
    assert btn.text() == "", "the handle must be an icon, not a caption"
    cam_rect = cmd._camera_view.geometry()
    btn_rect = btn.geometry()
    # Same cell as the camera surface, pinned to its top-left corner.
    assert abs(btn_rect.x() - cam_rect.x()) <= 12
    assert abs(btn_rect.y() - cam_rect.y()) <= 12


def test_exit_is_reachable_with_the_drawer_closed(qapp, window, camera):
    cmd = _command_window(qapp, window)
    _arm_vision(qapp, window, cmd)

    exit_btn = cmd._vision_exit_btn
    assert exit_btn.text() == ""
    assert cmd._vision_control_scroll.isHidden()
    exit_btn.click()
    _pump(qapp, 0.3)

    assert not cmd._vision_btn.isChecked(), "the corner EXIT must leave Vision mode"


# ── open / close ────────────────────────────────────────────────────────────

def test_handle_opens_and_closes_the_drawer(qapp, window):
    cmd = _command_window(qapp, window)
    scroll = cmd._vision_control_scroll
    btn = cmd._vision_rail_btn

    btn.click()
    _pump(qapp, 0.6)
    assert scroll.maximumWidth() == RAIL_WIDTH
    assert not scroll.isHidden(), "the drawer stayed hidden after the handle click"
    assert btn.isChecked(), "the handle must read as engaged while the drawer is open"

    btn.click()
    _pump(qapp, 0.6)
    assert scroll.maximumWidth() == 0
    assert scroll.isHidden()
    assert not btn.isChecked()


def test_drawer_slide_is_animated(qapp, window):
    cmd = _command_window(qapp, window)
    scroll = cmd._vision_control_scroll

    cmd._vision_rail_btn.click()
    anim = getattr(scroll, '_jarvis_width_animation', None)
    assert anim is not None, "opening the drawer did not animate"
    assert anim.state() == QAbstractAnimation.State.Running, "the slide never ran"
    assert anim.duration() > 0 and anim.endValue() == RAIL_WIDTH
    assert scroll.maximumWidth() < RAIL_WIDTH, "the drawer snapped open"
    _pump(qapp, 0.6)
    assert scroll.maximumWidth() == RAIL_WIDTH

    cmd._vision_rail_btn.click()
    assert getattr(scroll, '_jarvis_width_animation').state() == QAbstractAnimation.State.Running
    _pump(qapp, 0.6)
    assert scroll.maximumWidth() == 0 and scroll.isHidden(), "closing must end hidden"


def test_reaming_vision_resets_the_drawer(qapp, window, camera):
    cmd = _command_window(qapp, window)
    _arm_vision(qapp, window, cmd)
    cmd._vision_rail_btn.click()
    _pump(qapp, 0.5)
    assert cmd._vision_control_scroll.maximumWidth() == RAIL_WIDTH

    cmd._vision_btn.setChecked(False)
    _pump(qapp, 0.2)
    cmd._vision_btn.setChecked(True)
    _pump(qapp, 0.3)

    assert cmd._vision_control_scroll.maximumWidth() == 0
    assert cmd._vision_control_scroll.isHidden()
    assert not cmd._vision_rail_btn.isChecked()


# ── the switches still work ─────────────────────────────────────────────────

def test_switches_work_while_the_drawer_is_closed(qapp, window):
    cmd = _command_window(qapp, window)
    cam = cmd._camera_view
    assert cmd._vision_control_scroll.isHidden()

    cmd._vision_grid_overlay.setChecked(True)
    qapp.processEvents()
    assert cam._grid_overlay is True

    cmd._vision_invert.setChecked(True)
    qapp.processEvents()
    assert cam._invert is True

    cmd._vision_grid_overlay.setChecked(False)
    cmd._vision_invert.setChecked(False)
    qapp.processEvents()
    assert cam._grid_overlay is False and cam._invert is False


def test_drawer_is_scrollable(qapp, window, camera):
    cmd = _command_window(qapp, window)
    scroll = cmd._vision_control_scroll
    assert scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded

    _arm_vision(qapp, window, cmd)
    cmd._vision_rail_btn.click()
    _pump(qapp, 0.5)

    # Twelve switches plus the header are taller than the stage, so the rail
    # must offer a real scroll range instead of clipping silently.
    assert scroll.verticalScrollBar().maximum() > 0
