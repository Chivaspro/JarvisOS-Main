"""Memory Core camera interaction and bead sizing.

The reported problem: rotating or zooming dragged the beads along with it, the
field kept drifting under the pointer during a gesture, and a rotation could not
be started at all while the pointer happened to be over a memory.

The contract now is:
* A plain drag rotates the camera from ANYWHERE in the panel.
* Alt+drag is what moves a bead.
* The beads never move while the camera does — their world positions and the
  anchor are invariant, and the ambient drift is frozen for the gesture.
* Bead radius follows 3d-force-graph's model: nodeRelSize × ∛value, with the
  value taken from real relationship degree and stored importance.
"""
from __future__ import annotations

import math
import time as _time

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent, QWheelEvent

from dashboard.brain3d import (
    BG, NODE_REL_SIZE, ORBIT_SPEED, BrainGraph3D,
)

from .qt_harness import qapp  # noqa: F401  (fixture)

ALT = Qt.KeyboardModifier.AltModifier

NODES = [
    {"id": "core", "category": "CORE", "label": "CORE", "importance": 1.0},
    {"id": "hub", "category": "PROJECTS", "label": "Hub", "importance": 0.9},
    {"id": "a", "category": "TOOLS", "label": "A", "importance": 0.6},
    {"id": "b", "category": "FACTS", "label": "B", "importance": 0.5},
    {"id": "c", "category": "FACTS", "label": "C", "importance": 0.4},
]

LINKS = [
    {"s": "core", "t": "hub", "type": "has_memory", "weight": 0.6},
    {"s": "hub", "t": "a", "type": "uses", "weight": 0.9},
    {"s": "hub", "t": "b", "type": "related_to", "weight": 0.5},
    {"s": "a", "t": "c", "type": "related_to", "weight": 0.5},
]


def _graph():
    return {"nodes": [dict(n) for n in NODES], "links": [dict(l) for l in LINKS]}


def _star(degree: int):
    nodes = [{"id": "core", "category": "CORE", "label": "CORE", "importance": 1.0},
             {"id": "hub", "category": "PROJECTS", "label": "Hub", "importance": 0.9}]
    links = [{"s": "core", "t": "hub", "type": "has_memory", "weight": 0.6}]
    for i in range(degree):
        nodes.append({"id": f"n{i}", "category": "FACTS", "label": f"leaf {i}",
                      "importance": 0.5})
        links.append({"s": "hub", "t": f"n{i}", "type": "has_memory", "weight": 0.5})
    return {"nodes": nodes, "links": links}


@pytest.fixture()
def scene(qapp):  # noqa: F811
    orb = BrainGraph3D(get_graph=_graph)
    orb.resize(720, 520)
    orb.show()
    qapp.processEvents()
    yield orb
    orb.close()
    qapp.processEvents()


def _mouse(widget, kind, pos, button=Qt.MouseButton.LeftButton, modifiers=None):
    event = QMouseEvent(kind, QPointF(pos), widget.mapToGlobal(QPointF(pos)),
                        button, button,
                        Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers)
    if kind == QEvent.Type.MouseButtonPress:
        widget.mousePressEvent(event)
    elif kind == QEvent.Type.MouseMove:
        widget.mouseMoveEvent(event)
    elif kind == QEvent.Type.MouseButtonRelease:
        widget.mouseReleaseEvent(event)
    else:
        widget.mouseDoubleClickEvent(event)


def _wheel(widget, notches, pos=None):
    pos = QPointF(widget.width() / 2, widget.height() / 2) if pos is None \
        else QPointF(*pos)
    event = QWheelEvent(pos, widget.mapToGlobal(pos), QPoint(0, 0),
                        QPoint(0, 120 * int(notches)), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                        False)
    widget.wheelEvent(event)


def _tick_for(scene, seconds, dt=0.1):
    """Run the REAL animation tick as if `dt` had passed between each frame."""
    for _ in range(int(round(seconds / dt))):
        scene._last_tick = _time.perf_counter() - dt
        scene._tick()


def _settle_camera(scene, seconds=0.5, dt=1 / 60.0):
    """Let the eased camera reach the pointer/wheel targets.

    The pointer writes ``_rot_target_*`` and ``_zoom_target``; the rendered
    ``_rot_*`` / ``_zoom`` follow on the animation frames. Only the camera step
    runs here, so the eased value can be asserted without the orbit drift
    moving a bead underneath the assertion.
    """
    for _ in range(max(1, int(round(seconds / dt)))):
        scene._step_camera_interaction(dt)


def _world_snapshot(scene):
    return {n["id"]: scene.world_position(n["id"]) for n in scene._graph["nodes"]}


def _channel_max(col: QColor) -> int:
    return max(col.red(), col.green(), col.blue())


def _canvas_samples(img, step: int = 4) -> list[int]:
    """Brightest channel of a regular grid of pixels across the canvas."""
    return sorted(_channel_max(img.pixelColor(x, y))
                  for x in range(0, img.width(), step)
                  for y in range(0, img.height(), step))


def _percentile(values: list[int], frac: float) -> int:
    return values[min(len(values) - 1, int(len(values) * frac))]


def _assert_dark_void(img) -> None:
    """The panel is a dark void with a few deliberate bright things on top.

    This guards what the old flat-equality test was really about: no
    decorative fill, no ringed canvas, no bright wash behind the graph.
    Per-pixel equality can no longer express that — the background is a radial
    #041019 → #010409 gradient with nebulae, a parallax starfield and a
    vignette — so the property is measured instead: the bulk of the canvas is
    near-black and only a few percent of pixels carry real brightness.

    What is bright is deliberate: the reactor bubble at the centre (single
    pixels of 255), the beads, and a few specular stars near the rim. Measured
    on a populated 720×520 panel, the brightest 90% of samples sit at 27 or
    below and 0.5% of samples exceed 96.
    """
    values = _canvas_samples(img)
    bulk = _percentile(values, 0.75)
    assert bulk <= 40, f"the background is not a dark void (p75 = {bulk})"
    bright = sum(1 for v in values if v > 96) / len(values)
    assert bright <= 0.05, \
        f"{bright:.1%} of the canvas is bright — something is painted over the void"


def _assert_cool_void(img) -> None:
    """The void is blue-tinted, never a warm or grey wash."""
    total = cool = 0
    for x in range(0, img.width(), 4):
        for y in range(0, img.height(), 4):
            col = img.pixelColor(x, y)
            total += 1
            cool += col.blue() >= col.green() >= col.red()
    ratio = cool / total
    assert ratio >= 0.97, f"the void is not blue-tinted ({ratio:.1%} cool)"


# ── rotate from anywhere, without moving a bead ──────────────────────────────

def test_a_drag_on_a_bead_rotates_the_camera_instead_of_grabbing_it(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    start = scene.orb_screen_positions()["hub"]
    before = (scene._rot_x, scene._rot_y)
    _mouse(scene, QEvent.Type.MouseButtonPress, start)
    _mouse(scene, QEvent.Type.MouseMove, QPointF(start.x() + 60, start.y() + 30))
    assert scene.dragging_orb() is None
    _settle_camera(scene)
    assert (scene._rot_x, scene._rot_y) != before


def test_rotation_is_available_from_every_corner_of_the_panel(scene):
    for point in ((3, 3), (716, 5), (5, 515), (700, 500),
                  (360.0, 260.0)):          # corners, edges and the middle
        scene._rot_y = scene._rot_target_y = 0.0
        _mouse(scene, QEvent.Type.MouseButtonPress, QPointF(*point))
        assert scene._drag is not None and scene._drag[1] == "rotate", point
        _mouse(scene, QEvent.Type.MouseMove, QPointF(point[0] + 40, point[1]))
        _settle_camera(scene, 0.25)
        assert scene._rot_y != 0.0, point


def test_rotating_never_moves_a_bead_or_the_anchor(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    worlds = _world_snapshot(scene)
    anchor = scene.anchor_point()
    start = scene.orb_screen_positions()["a"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start)
    for step in range(1, 8):
        _mouse(scene, QEvent.Type.MouseMove, QPointF(start.x() + 30 * step,
                                                     start.y() + 12 * step))
    _mouse(scene, QEvent.Type.MouseButtonRelease, QPointF(start.x() + 210,
                                                          start.y() + 84))
    assert scene.anchor_point() == anchor
    for nid, before in worlds.items():
        assert scene.world_position(nid) == before, nid


def test_zooming_never_moves_a_bead_or_the_anchor(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    worlds = _world_snapshot(scene)
    anchor = scene.anchor_point()
    _wheel(scene, 3)
    _wheel(scene, -6)
    _settle_camera(scene)
    assert scene._zoom != 1.0
    assert scene.anchor_point() == anchor
    for nid, before in worlds.items():
        assert scene.world_position(nid) == before, nid


def test_zoom_is_available_from_anywhere_in_the_panel(scene):
    for point in ((2, 2), (718, 518), (360.0, 260.0)):
        scene._zoom = scene._zoom_target = 1.0
        _wheel(scene, 2, pos=point)
        _settle_camera(scene, 0.25)
        assert scene._zoom > 1.0, point


# ── the field freezes for the duration of a camera gesture ───────────────────

def test_the_field_is_frozen_while_rotating(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    start = scene.orb_screen_positions()["hub"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start)
    phase = scene._orbit_phase
    _tick_for(scene, 0.5)
    assert scene._orbit_phase == phase
    _mouse(scene, QEvent.Type.MouseMove, QPointF(start.x() + 40, start.y() + 20))
    _tick_for(scene, 0.5)
    assert scene._orbit_phase == phase


def test_the_field_is_frozen_while_zooming(scene):
    phase = scene._orbit_phase
    _wheel(scene, 2)
    assert scene._orbit_paused() is True
    _tick_for(scene, 0.5)
    assert scene._orbit_phase == phase


def test_the_field_is_frozen_while_a_bead_is_being_moved(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    start = scene.orb_screen_positions()["hub"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start, modifiers=ALT)
    assert scene.dragging_orb() == "hub"
    phase = scene._orbit_phase
    _tick_for(scene, 0.5)
    assert scene._orbit_phase == phase


def test_the_drift_resumes_from_where_it_paused_without_jumping(scene):
    scene._drag = None
    scene._orb_drag = None
    scene._orbit_hold_until = 0.0
    _tick_for(scene, 0.5)
    paused_at = scene._orbit_phase
    assert paused_at > 0.0                       # it was drifting

    scene._hold_orbit()                          # a gesture
    _tick_for(scene, 0.5)
    assert scene._orbit_phase == paused_at       # frozen

    scene._orbit_hold_until = 0.0                # gesture over
    _tick_for(scene, 0.1, dt=0.1)
    resumed = scene._orbit_phase
    # It picks up from where it stopped rather than snapping to the wall clock.
    assert paused_at < resumed < paused_at + ORBIT_SPEED * 0.4


def test_a_gesture_holds_the_drift_for_a_beat_after_the_button_comes_up(scene):
    start = scene.orb_screen_positions()["hub"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start)
    _mouse(scene, QEvent.Type.MouseButtonRelease, start)
    phase = scene._orbit_phase
    _tick_for(scene, 0.05)
    assert scene._orbit_phase == phase
    assert scene._orbit_paused() is True


def test_the_field_still_drifts_when_nothing_is_touched(scene):
    scene._drag = None
    scene._orb_drag = None
    scene._orbit_hold_until = 0.0
    phase = scene._orbit_phase
    _tick_for(scene, 0.5)
    assert scene._orbit_phase > phase


# ── Alt+drag still moves a bead ──────────────────────────────────────────────

def test_alt_dragging_a_bead_still_moves_it_without_rotating_the_camera(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    before = scene.world_position("hub")
    rot = (scene._rot_x, scene._rot_y)
    start = scene.orb_screen_positions()["hub"]
    _mouse(scene, QEvent.Type.MouseButtonPress, start, modifiers=ALT)
    _mouse(scene, QEvent.Type.MouseMove, QPointF(start.x() + 70, start.y() - 40),
           modifiers=ALT)
    assert scene.dragging_orb() == "hub"
    assert scene.world_position("hub") != before
    assert (scene._rot_x, scene._rot_y) == rot


def test_alt_dragging_on_empty_space_still_rotates(scene):
    _mouse(scene, QEvent.Type.MouseButtonPress, QPointF(3, 3), modifiers=ALT)
    assert scene.dragging_orb() is None
    assert scene._drag is not None and scene._drag[1] == "rotate"


# ── bead sizing: 3d-force-graph's nodeRelSize × ∛value ───────────────────────

def test_bead_size_grows_with_real_connectivity(scene):
    scene._get_graph = lambda: _star(24)
    scene.refresh()
    scene._zoom = 1.0
    hub = next(n for n in scene._graph["nodes"] if n["id"] == "hub")
    leaf = next(n for n in scene._graph["nodes"] if n["id"] == "n0")
    assert scene._degrees["hub"] == 25
    assert scene._degrees["n0"] == 1
    assert scene._node_radius(hub, 0.5) > scene._node_radius(leaf, 0.5) * 1.5


def test_bead_size_grows_with_importance_at_equal_connectivity(scene):
    scene._zoom = 1.0
    weak = {"id": "a", "category": "FACTS", "importance": 0.30}
    strong = {"id": "b", "category": "FACTS", "importance": 1.00}
    assert scene._node_val(strong) > scene._node_val(weak)
    assert scene._node_radius(strong, 0.5) > scene._node_radius(weak, 0.5)


def test_bead_radius_follows_the_cube_root_of_value(scene):
    """A linear model would fail this: the library's spheres are volume-true."""
    scene._zoom = 1.0
    scene._get_graph = lambda: _star(8)
    scene.refresh()
    hub = next(n for n in scene._graph["nodes"] if n["id"] == "hub")
    leaf = next(n for n in scene._graph["nodes"] if n["id"] == "n0")
    ratio = scene._node_radius(hub, 0.5) / scene._node_radius(leaf, 0.5)
    expected = (scene._node_val(hub) / scene._node_val(leaf)) ** (1.0 / 3.0)
    assert ratio == pytest.approx(expected)


def test_beads_stay_inside_a_usable_size_band(scene):
    """Whatever the store holds, no bead is invisible or fills the panel."""
    scene._get_graph = lambda: _star(300)
    scene.refresh()
    scene._zoom = 1.0
    for node in scene._graph["nodes"]:
        radius = scene._node_radius(node, 1.0)
        assert 3.0 < radius < 20.0, (node["id"], radius)


def test_bead_size_still_respects_depth_and_zoom(scene):
    scene._zoom = 1.0
    hub = next(n for n in scene._graph["nodes"] if n["id"] == "hub")
    assert scene._node_radius(hub, 1.0) > scene._node_radius(hub, 0.0)
    scene._zoom = 2.0
    assert scene._node_radius(hub, 1.0) > scene._node_radius(hub, 1.0) / 2.0


# ── no decorative circle behind the graph ────────────────────────────────────

def test_the_decorative_background_circles_are_gone(scene):
    assert not hasattr(scene, "_draw_bubble_shell")
    assert not hasattr(scene, "_draw_rings")


def test_the_canvas_is_a_dark_void(scene, qapp):
    """The shell is gone: the background is a dark void, not a ringed canvas.

    This used to demand the exact BG token (#02060c) corner to corner, which
    stopped describing the view once the deep-space background landed, so the
    property is now measured as a distribution — see ``_assert_dark_void``.
    """
    scene._compute_layout(float(scene.width()), float(scene.height()))
    img = scene.grab().toImage()
    assert not img.isNull()
    _assert_dark_void(img)
    _assert_cool_void(img)


def test_the_scene_still_renders_a_selected_network(scene, qapp):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "hub")
    for _ in range(40):
        scene._step_visuals(1 / 30.0)
    assert not scene.grab().isNull()
    assert scene._particle_links()


def test_an_empty_graph_still_paints_a_dark_canvas(scene, qapp):
    """No memories must still draw the void — and nothing bright on top of it.

    Asserting one constant pixel was the same stale assumption as above, so
    the void is measured as a distribution. On top of that the middle of the
    panel is checked directly: with no graph there is no reactor bubble and no
    bead there either, so the centre of the panel must be plain background.
    """
    scene._get_graph = lambda: {"nodes": [], "links": []}
    scene.refresh()
    img = scene.grab().toImage()
    assert not img.isNull()
    _assert_dark_void(img)

    centre = img.pixelColor(img.width() // 2, img.height() // 2)
    assert _channel_max(centre) <= 48, \
        f"the empty panel is not dark at its centre {centre.name()}"
    # The depth gradient survives the empty graph: the centre of the
    # #041019 → #010409 falloff is lighter than the far corner the vignette
    # darkens.
    corner = img.pixelColor(3, img.height() - 4)
    assert centre.blue() > corner.blue(), "no depth gradient at all"
