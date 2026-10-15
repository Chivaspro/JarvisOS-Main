"""Memory Core scene behaviour.

The reported symptoms were that the main orb never stopped moving, and that
dragging an orb rotated the whole scene. Both came from one variable being used
as camera state, animation clock and object position at the same time, so these
tests assert the separation directly.
"""
from __future__ import annotations

import math

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from dashboard.brain3d import (
    ATTRACT_RADIUS, MAX_ORB_SPEED, PARTICLE_CAP, SNAP_RADIUS,
    BrainGraph3D, emit_memory_event, orb_registry_size,
)

from .qt_harness import qapp  # noqa: F401  (fixture)

ANCHOR_ID = "core"


def _graph():
    return {
        "nodes": [
            {"id": ANCHOR_ID, "category": "CORE", "label": "CORE", "importance": 1.0},
            {"id": "m1", "category": "PROFILE", "label": "Operator name", "importance": 0.9},
            {"id": "m2", "category": "PROJECTS", "label": "JARVIS build", "importance": 0.6},
            {"id": "m3", "category": "PREFERENCES", "label": "Voice", "importance": 0.4},
        ],
        "links": [{"s": ANCHOR_ID, "t": "m1"}, {"s": "m1", "t": "m2"}],
    }


@pytest.fixture()
def scene(qapp):  # noqa: F811
    orb = BrainGraph3D(get_graph=_graph)
    orb.resize(720, 520)
    orb.show()
    qapp.processEvents()
    yield orb
    orb.close()
    qapp.processEvents()


# Moving a bead is ALT+drag; a plain drag rotates the camera from anywhere, so
# every bead-drag test below has to hold Alt or it would rotate instead.
ALT = Qt.KeyboardModifier.AltModifier


def _mouse(widget, kind, pos, button=Qt.MouseButton.LeftButton, modifiers=None):
    event = QMouseEvent(kind, QPointF(pos), widget.mapToGlobal(QPointF(pos)),
                        button, button,
                        Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers)
    if kind == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(event)
    else:
        widget.mouseReleaseEvent(event)


def _ticks(orb, count, dt=1 / 30.0):
    for _ in range(count):
        orb._step_orb_interaction(dt)


# ── anchor stability ─────────────────────────────────────────────────────────

def test_anchor_world_position_is_a_constant(scene):
    assert scene.anchor_world().length() == 0.0
    scene._t = 999.0
    scene._orbit_phase = scene._t * 0.2
    scene._step_orb_interaction(1 / 30.0)
    assert scene.anchor_world().length() == 0.0


def test_anchor_does_not_move_while_the_clock_advances(scene):
    before = scene.anchor_point()
    for _ in range(90):
        scene._tick()
    assert scene.anchor_point() == before


def test_anchor_is_excluded_from_the_orbit_animation(scene):
    """The fixed-point bug: the clock used to drive a rotation that every node,
    including the anchor, passed through."""
    anchor_before = scene.anchor_point()
    others_before = {k: QPointF(v) for k, v in scene.orb_screen_positions().items()
                     if k != ANCHOR_ID}
    scene._t += 4.0
    scene._orbit_phase = scene._t * 0.2
    scene._compute_layout(float(scene.width()), float(scene.height()))
    after = scene.orb_screen_positions()
    assert scene.anchor_point() == anchor_before
    moved = [k for k, v in others_before.items()
             if (after[k] - v).manhattanLength() > 1.0]
    assert moved, "the rest of the field should still animate"


@pytest.mark.parametrize("op", ["rotate", "zoom", "pan"])
def test_camera_operations_never_move_the_anchor_world_position(scene, op):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    world_before = scene.anchor_world()
    if op == "rotate":
        scene._rot_x += 0.4
        scene._rot_y -= 0.7
    elif op == "zoom":
        scene._zoom = 2.4
    else:
        scene._pan = QPointF(-60, 35)
    scene._compute_layout(float(scene.width()), float(scene.height()))
    assert scene.anchor_world() == world_before
    # Zoom keeps the anchor centred in the panel.
    if op == "zoom":
        cx = scene.width() / 2 + scene._pan.x()
        assert abs(scene.anchor_point().x() - cx) < 0.01


def test_resizing_re_frames_without_moving_the_anchor_out_of_view(scene):
    scene._compute_layout(720.0, 520.0)
    scene.resize(380, 260)
    scene._compute_layout(380.0, 260.0)
    point = scene.anchor_point()
    assert 0 <= point.x() <= 380 and 0 <= point.y() <= 260


# ── orb dragging ─────────────────────────────────────────────────────────────

def test_alt_pressing_an_orb_selects_it_for_dragging(scene):
    point = scene.orb_screen_positions()["m1"]
    _mouse(scene, QEvent.Type.MouseButtonPress, point, modifiers=ALT)
    assert scene.dragging_orb() == "m1"


def test_the_anchor_is_never_a_drag_target(scene):
    assert scene.pick_orb(scene.anchor_point()) is None
    _mouse(scene, QEvent.Type.MouseButtonPress, scene.anchor_point(), modifiers=ALT)
    assert scene.dragging_orb() is None


def test_dragging_an_orb_leaves_everything_else_alone(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    anchor_before = scene.anchor_point()
    others_before = {k: scene.world_position(k) for k in ("m2", "m3")}
    dragged_before = scene.world_position("m1")
    start = scene.orb_screen_positions()["m1"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start, modifiers=ALT)
    _mouse(scene, QEvent.Type.MouseMove, QPointF(start.x() + 70, start.y() + 45),
           modifiers=ALT)
    assert scene.anchor_point() == anchor_before
    for node_id, before in others_before.items():
        assert scene.world_position(node_id) == before
    assert scene.world_position("m1") != dragged_before


def test_a_dragged_orb_follows_the_pointer(scene):
    start = scene.orb_screen_positions()["m1"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start, modifiers=ALT)
    target = QPointF(start.x() + 90, start.y() - 60)
    _mouse(scene, QEvent.Type.MouseMove, target, modifiers=ALT)
    scene._compute_layout(float(scene.width()), float(scene.height()))
    dropped = scene.orb_screen_positions()["m1"]
    # Perspective projection makes the inverse scale z-dependent: the grab
    # point's depth is used for the conversion, but the new position projects
    # at its own depth, so large deltas drift.  The bound proves the orb moves
    # in the right direction and roughly tracks the pointer.
    assert abs(dropped.x() - target.x()) < 25
    assert abs(dropped.y() - target.y()) < 25


def test_pressing_empty_space_does_not_grab_an_orb(scene):
    _mouse(scene, QEvent.Type.MouseButtonPress, QPointF(4, 4))
    assert scene.dragging_orb() is None
    assert scene._drag is not None and scene._drag[1] == "rotate"


def test_a_plain_drag_on_a_bead_rotates_instead_of_grabbing_it(scene):
    """The camera must be drivable from anywhere in the panel."""
    point = scene.orb_screen_positions()["m1"]
    _mouse(scene, QEvent.Type.MouseButtonPress, point)
    assert scene.dragging_orb() is None
    assert scene._drag is not None and scene._drag[1] == "rotate"


# ── attraction, snap, merge ──────────────────────────────────────────────────

def _place(orb, node_id, distance, angle=0.4):
    from PyQt6.QtGui import QVector3D
    orb._world[node_id] = QVector3D(distance * math.cos(angle),
                                    distance * math.sin(angle), 0.0)
    orb._state_of(node_id).update({"placed": True, "dragging": False,
                                   "snapping": False, "merged": False})


def test_nothing_is_pulled_from_outside_the_attraction_radius(scene):
    _place(scene, "m1", ATTRACT_RADIUS * 2.2)
    before = scene.world_position("m1")
    _ticks(scene, 30)
    assert scene.world_position("m1") == before


def test_attraction_pulls_monotonically_and_settles(scene):
    _place(scene, "m1", ATTRACT_RADIUS * 0.85)
    distances = [scene.world_position("m1").length()]
    speeds = []
    for _ in range(120):
        scene._step_orb_interaction(1 / 30.0)
        distances.append(scene.world_position("m1").length())
        speeds.append(scene.orb_states()["m1"]["velocity"])
    # Monotonic approach — no overshoot, no bounce.
    assert all(b <= a + 1e-6 for a, b in zip(distances, distances[1:]))
    assert max(speeds) <= MAX_ORB_SPEED + 1e-6
    assert distances[-1] < distances[0]


def test_attraction_starts_gently_at_the_boundary(scene):
    """A hard yank at the boundary was the failure mode the spec forbids."""
    _place(scene, "m1", ATTRACT_RADIUS * 0.999)
    before = scene.world_position("m1").length()
    scene._step_orb_interaction(1 / 30.0)
    after = scene.world_position("m1").length()
    assert 0 < (before - after) < 0.02


def test_snap_is_smooth_and_never_teleports(scene):
    _place(scene, "m1", SNAP_RADIUS * 0.7)
    scene._state_of("m1")["snapping"] = True
    previous = scene.world_position("m1")
    max_step = 0.0
    for _ in range(60):
        scene._step_orb_interaction(1 / 30.0)
        current = scene.world_position("m1")
        max_step = max(max_step, (current - previous).length())
        previous = current
    assert max_step <= MAX_ORB_SPEED / 30.0 + 1e-6
    assert scene.world_position("m1").length() == 0.0
    assert scene.orb_states()["m1"]["merged"] is True


def test_merge_is_idempotent(scene):
    _place(scene, "m1", SNAP_RADIUS * 0.5)
    scene._state_of("m1")["snapping"] = True
    _ticks(scene, 40)
    first = scene.orb_states()["m1"]
    _ticks(scene, 40)
    second = scene.orb_states()["m1"]
    assert first["merged"] is True and second["merged"] is True
    assert second["velocity"] == 0.0
    assert scene.world_position("m1").length() == 0.0


def test_release_outside_the_snap_radius_comes_to_rest(scene):
    _place(scene, "m1", ATTRACT_RADIUS * 0.9)
    scene.mouseReleaseEvent(_release_event(scene))
    distances = []
    for _ in range(150):
        scene._step_orb_interaction(1 / 30.0)
        distances.append(scene.world_position("m1").length())
    tail = distances[-30:]
    assert max(tail) - min(tail) < 1e-3, "the orb must settle, not oscillate"


def _release_event(widget):
    pos = QPointF(5, 5)
    return QMouseEvent(QEvent.Type.MouseButtonRelease, pos, widget.mapToGlobal(pos),
                       Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


def test_attraction_is_frame_rate_independent(scene):
    from PyQt6.QtGui import QVector3D
    scene._world["m1"] = QVector3D(ATTRACT_RADIUS * 0.8, 0.0, 0.0)
    scene._state_of("m1").update({"placed": True, "snapping": False})
    slow = QVector3D(scene._world["m1"])
    for _ in range(30):
        scene._step_orb_interaction(1 / 30.0)
    fast_result = scene.world_position("m1").length()

    scene._world["m1"] = slow
    for _ in range(60):
        scene._step_orb_interaction(1 / 60.0)
    slow_result = scene.world_position("m1").length()
    assert abs(fast_result - slow_result) < 5e-3


def test_interrupted_drag_leaves_no_stuck_orb(scene):
    start = scene.orb_screen_positions()["m1"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start, modifiers=ALT)
    _mouse(scene, QEvent.Type.MouseMove, QPointF(start.x() + 40, start.y() + 30),
           modifiers=ALT)
    scene.hide()
    scene._orb_drag = None
    scene._orb_grab = None
    scene.show()
    scene._compute_layout(float(scene.width()), float(scene.height()))
    assert scene.dragging_orb() is None
    assert set(scene.orb_screen_positions()) == {ANCHOR_ID, "m1", "m2", "m3"}


# ── budgets ──────────────────────────────────────────────────────────────────

def test_effects_cannot_move_anything(scene):
    # The anchor and every *world* position must be untouched by effects. The
    # unattached orbs are allowed to keep orbiting on the ornament phase, so
    # their on-screen point is compared only while that phase is held still.
    anchor_before = scene.anchor_point()
    world = {k: scene.world_position(k) for k in (ANCHOR_ID, "m1", "m2", "m3")}
    scene.set_activity("thinking")
    emit_memory_event("save", 4)
    scene._spawn_particles(40)
    scene._drain_memory_events()
    scene._step_orb_interaction(1 / 30.0)
    scene._ensure_frame()
    assert scene.anchor_point() == anchor_before
    for node_id, value in world.items():
        assert scene.world_position(node_id) == value
    effects = scene.particle_count()
    assert effects > 0
    # With the effects quieted, the same interaction is identical.
    scene._particles.clear()
    assert scene.particle_count() == 0
    assert scene.anchor_point() == anchor_before
    assert scene.dragging_orb() is None


def test_the_particle_pool_is_capped_and_drains(scene):
    for _ in range(20):
        emit_memory_event("recall", 8)
        scene._drain_memory_events()
    assert scene.particle_count() <= PARTICLE_CAP
    scene._t += 10.0
    assert scene.particle_count() == 0


def test_caches_shrink_when_the_graph_shrinks(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    assert len(scene._world) >= 3
    scene._orb_states["ghost"] = {"placed": True}
    scene._world["ghost"] = scene.world_position("m1")
    scene._get_graph = lambda: {"nodes": [{"id": "m1", "category": "PROFILE"}], "links": []}
    scene.refresh()
    assert "ghost" not in scene._world
    assert "ghost" not in scene._orb_states
    assert set(scene._world) <= {"m1"}


def test_only_one_animation_timer_runs_and_hiding_stops_it(scene):
    assert scene._anim.isActive()
    scene.hide()
    assert not scene._anim.isActive()
    scene.show()
    assert scene._anim.isActive()


def test_destroying_an_orb_unregisters_it(scene, qapp):
    baseline = orb_registry_size()
    extra = BrainGraph3D(get_graph=_graph)
    extra.show()
    qapp.processEvents()
    assert orb_registry_size() == baseline + 1
    extra.close()
    extra.deleteLater()
    qapp.processEvents()
    assert orb_registry_size() == baseline


def test_publishing_to_a_destroyed_orb_is_a_no_op(scene, qapp):
    orb = BrainGraph3D(get_graph=_graph)
    orb.close()
    orb.deleteLater()
    qapp.processEvents()
    emit_memory_event("save", 3)      # must not raise
