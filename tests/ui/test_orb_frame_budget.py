"""The 3D orb's frame budget: 60 fps when it matters, ~24 fps when it does not.

The orb never fully freezes — a cinematic auto-orbit (0.035 rad/s) plus the
ambient bead drift always run — so the animation tick used to repaint at 60 fps
forever, which is a permanently busy CPU for a scene whose only motion is
slower than a clock hand. These tests pin both halves of the fix: the idle
scene drops to the cinematic rate, and anything genuinely moving (a one-pixel
drag, a bead snapping home, particles, an event flare, the camera easing)
promotes it straight back to 60 fps.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

from dashboard.brain3d import (  # noqa: E402
    FRAME_MS,
    IDLE_FRAME_MS,
    BrainGraph3D,
)


@pytest.fixture(scope="module")
def scene():
    app = QApplication.instance() or QApplication([])
    orb = BrainGraph3D()
    yield orb
    orb.deleteLater()


def test_idle_scene_runs_at_the_cinematic_rate(scene):
    """No interaction, nothing in motion → do not burn 60 fps on it."""
    scene._drag = None
    scene._orb_drag = None
    scene._cam_target = None
    scene._particles.clear()
    for _ in range(5):
        scene._tick()
    assert scene._interactive_now() is False
    assert scene._anim.interval() == IDLE_FRAME_MS


def test_dragging_promotes_the_tick_back_to_60fps(scene):
    scene._tick()
    assert scene._anim.interval() == IDLE_FRAME_MS
    scene._drag = ("pos", "rotate")
    try:
        scene._tick()
        assert scene._interactive_now() is True
        assert scene._anim.interval() == FRAME_MS
    finally:
        scene._drag = None


def test_moving_beads_keep_the_full_rate(scene):
    scene._drag = None
    scene._spawn_particles(4)
    try:
        scene._tick()
        assert scene._interactive_now() is True
        assert scene._anim.interval() == FRAME_MS
    finally:
        scene._particles.clear()


def test_a_flare_keeps_the_full_rate_until_it_expires(scene):
    """A memory event flashes the orb — that flash must not stutter."""
    import time as _time
    scene._flare_until = _time.perf_counter() + 5.0
    try:
        scene._tick()
        assert scene._interactive_now() is True
    finally:
        scene._flare_until = 0.0
    scene._tick()
    scene._tick()
    assert scene._anim.interval() == IDLE_FRAME_MS


def test_camera_easing_counts_as_motion(scene):
    scene._drag = None
    scene._particles.clear()
    scene._zoom_target = scene._zoom + 0.5      # a real, visible zoom to ease into
    try:
        scene._tick()
        assert scene._interactive_now() is True
    finally:
        scene._zoom_target = scene._zoom


def test_the_slow_idle_orbit_alone_never_counts_as_interaction(scene):
    """The ambient drift must not defeat the whole optimisation.

    The idle orbit leaves a steady-state camera lag of
    IDLE_ORBIT_SPEED * CAMERA_INTERACTION_TAU (~0.0023 rad). If the threshold
    were below that, the orb would be "interacting" with itself forever.
    """
    scene._drag = None
    scene._orb_drag = None
    scene._cam_target = None
    scene._particles.clear()
    # Replay the idle drift exactly as the real tick does it: advance the orbit
    # target by speed * dt, then let the camera ease after it. After a second of
    # this the settle lag is the steady state the threshold must tolerate.
    dt = 1.0 / 60.0
    for _ in range(120):
        scene._dt = dt
        scene._rot_target_y += 0.035 * dt
        scene._step_camera_interaction(dt)
    assert abs(scene._rot_target_y - scene._rot_y) < 0.05
    assert scene._interactive_now() is False
    scene._tick()
    assert scene._anim.interval() == IDLE_FRAME_MS


def test_start_animating_resumes_at_the_current_budget(scene):
    scene._particles.clear()
    scene._tick()
    idle_interval = scene._anim.interval()
    scene.stop_animating()
    assert not scene._anim.isActive()
    scene.start_animating()
    assert scene._anim.isActive()
    assert scene._anim.interval() == idle_interval
    scene.stop_animating()
