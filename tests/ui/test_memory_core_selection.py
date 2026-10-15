"""Memory Core selection behaviour (spec §1-§32).

The Memory Core must read as a calm grey network until a memory is picked, and
then as a coloured neighbourhood joined by blue relationship lines carrying
travelling particles. These tests assert the state model that produces that
look, the fact that every highlight comes from a real relationship, the particle
budget, the camera behaviour and the detail card.

Nothing here asserts pixel output: the scene's appearance is *derived* from
explicit state, so the state is what is worth pinning down.
"""
from __future__ import annotations

import math

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from dashboard.brain3d import (
    CONNECTION_PARTICLE_CAP, CONNECTION_PARTICLE_SPEED, MUTED_SATURATION,
    SECONDARY_DEGREE_MAX_LINKS, BrainGraph3D, LinkState, NodeState,
)

from .qt_harness import qapp  # noqa: F401  (fixture)

# A real little network: Vision Panel uses Cua and Gemini, is related to Head
# Tracking, hangs off the core, and one memory (Far Fact) sits outside the
# neighbourhood entirely.
LINKS = [
    {"s": "core", "t": "v", "type": "has_memory", "weight": 0.6},     # 0
    {"s": "v", "t": "cua", "type": "uses", "weight": 0.9},            # 1
    {"s": "v", "t": "gem", "type": "uses", "weight": 0.8},            # 2
    {"s": "v", "t": "head", "type": "related_to", "weight": 0.5},     # 3
    {"s": "core", "t": "far", "type": "has_memory", "weight": 0.6},   # 4
]

V_NEIGHBOURS = {"core", "cua", "gem", "head"}
V_LINKS = {0, 1, 2, 3}


def _graph():
    return {
        "nodes": [
            {"id": "core", "category": "CORE", "label": "CORE", "importance": 1.0},
            {"id": "v", "category": "PROJECTS", "label": "Vision Panel",
             "importance": 0.9, "confidence": 0.95,
             "content": "Jarvis visual tracking panel.",
             "created_at": 1_700_000_000.0, "updated_at": 1_700_100_000.0},
            {"id": "cua", "category": "TOOLS", "label": "Cua", "importance": 0.8},
            {"id": "gem", "category": "TECHNOLOGY", "label": "Gemini", "importance": 0.7},
            {"id": "head", "category": "TASKS", "label": "Head Tracking", "importance": 0.5},
            {"id": "far", "category": "FACT", "label": "Unrelated fact", "importance": 0.4},
        ],
        "links": [dict(link) for link in LINKS],
    }


def _star_graph(degree: int):
    """A core, one hub memory and `degree` leaves — for the budget and pixel
    tests, where the neighbourhood has to be bigger than the core."""
    nodes = [{"id": "core", "category": "CORE", "label": "CORE", "importance": 1.0},
             {"id": "hub", "category": "PROJECTS", "label": "Hub", "importance": 0.9}]
    links = [{"s": "core", "t": "hub", "type": "has_memory", "weight": 0.6}]
    for i in range(degree):
        nodes.append({"id": f"n{i}", "category": "FACTS", "label": f"leaf {i}",
                      "importance": 0.5})
        links.append({"s": "hub", "t": f"n{i}", "type": "has_memory",
                      "weight": 0.1 + 0.8 * (i / max(1, degree - 1))})
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


# Alt+drag moves a bead; a plain drag rotates the camera from anywhere.
ALT = Qt.KeyboardModifier.AltModifier


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


def _settle(orb, seconds=1.0, dt=1 / 30.0):
    """Advance the cross-fade without advancing the wall clock."""
    for _ in range(int(seconds / dt)):
        orb._step_visuals(dt)


def _select(orb, node_id="v"):
    orb._compute_layout(float(orb.width()), float(orb.height()))
    orb._set_selection("node", node_id)
    _settle(orb)


def _node_of(orb, node_id):
    for node in orb._graph["nodes"]:
        if node.get("id") == node_id:
            return node
    raise AssertionError(f"{node_id} not in graph")


# ── idle state: a calm grey field (§1, §7, §29) ──────────────────────────────

def test_nothing_selected_leaves_every_memory_muted(scene):
    for node in scene._graph["nodes"]:
        if node["category"] == "CORE":
            continue
        assert scene._node_state(node) == NodeState.DEFAULT
        assert scene.node_visual(node["id"])["sat"] == pytest.approx(MUTED_SATURATION)


def test_no_relationship_is_active_or_emitting_while_idle(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    for row in scene._link_rows:
        assert scene._link_state(row["i"]) == LinkState.DEFAULT
        assert scene.link_visual(row["i"]) < 0.3
    assert scene._particle_links() == []


def test_the_idle_graph_still_draws_every_node_and_link(scene):
    """Muting must not hide anything — the field stays readable (§1, §6)."""
    scene._compute_layout(float(scene.width()), float(scene.height()))
    assert len(scene._lay) == len(scene._graph["nodes"])
    assert {row["i"] for row in scene._link_rows} == {0, 1, 2, 3, 4}
    assert all(scene.node_visual(n["id"])["bright"] > 0.0
               for n in scene._graph["nodes"] if n["category"] != "CORE")


# ── selecting a memory colours its real neighbourhood (§3-§6) ────────────────

def test_selecting_a_memory_colours_exactly_its_real_neighbourhood(scene):
    _select(scene)
    assert scene.selected_node_id() == "v"
    assert scene.connected_node_ids() == V_NEIGHBOURS
    assert scene.active_link_ids() == V_LINKS


def test_every_highlighted_node_shares_a_real_relationship_with_the_focus(scene):
    """The look must come from real data — no relationship is invented."""
    _select(scene)
    real = set()
    for link in scene._graph["links"]:
        if link["s"] == "v":
            real.add(link["t"])
        elif link["t"] == "v":
            real.add(link["s"])
    assert scene.connected_node_ids() == real


def test_the_selected_memory_keeps_its_category_colour_at_full_strength(scene):
    _select(scene)
    vis = scene.node_visual("v")
    assert vis["sat"] == pytest.approx(1.0, abs=0.02)
    assert vis["bright"] == pytest.approx(1.0, abs=0.02)
    assert vis["glow"] == pytest.approx(1.0, abs=0.02)
    assert scene._node_state(_node_of(scene, "v")) == NodeState.SELECTED


def test_direct_neighbours_keep_their_own_category_colour(scene):
    _select(scene)
    for nid in ("cua", "gem", "head", "core"):
        vis = scene.node_visual(nid)
        assert vis["sat"] == pytest.approx(1.0, abs=0.03), nid
        assert scene._node_state(_node_of(scene, nid)) == NodeState.CONNECTED, nid


def test_unrelated_memories_go_grey_instead_of_disappearing(scene):
    scene.set_secondary_degree(False)          # isolate level 3 for the assert
    _select(scene)
    far = _node_of(scene, "far")
    assert scene._node_state(far) == NodeState.MUTED
    vis = scene.node_visual("far")
    assert vis["sat"] == pytest.approx(MUTED_SATURATION, abs=0.02)
    assert vis["bright"] < 0.5
    assert vis["bright"] > 0.0                 # muted, never hidden


def test_second_degree_highlighting_is_optional_and_off_when_crowded(scene):
    _select(scene)
    # Small network: the one-hop-further memory reads as related (§5, §21).
    assert scene._node_state(_node_of(scene, "far")) == NodeState.SECONDARY
    scene.set_secondary_degree(False)
    assert scene._node_state(_node_of(scene, "far")) == NodeState.MUTED
    scene.set_secondary_degree(True)
    assert scene._node_state(_node_of(scene, "far")) == NodeState.SECONDARY


def test_a_crowded_selection_drops_second_degree_by_itself(scene):
    scene._get_graph = lambda: _star_graph(SECONDARY_DEGREE_MAX_LINKS + 5)
    scene.refresh()
    scene._set_selection("node", "hub")
    assert scene.secondary_node_ids() == set()


# ── transitions are eased, not snapped (§17) ─────────────────────────────────

def test_a_selection_cross_fades_instead_of_switching(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    before = scene.node_visual("v")["sat"]
    scene._set_selection("node", "v")
    scene._step_visuals(1 / 30.0)
    mid = scene.node_visual("v")["sat"]
    assert before < mid < 1.0            # moving toward colour, not snapped
    _settle(scene)
    assert scene.node_visual("v")["sat"] == pytest.approx(1.0, abs=0.02)


def test_a_transition_lands_inside_the_expected_window(scene):
    """§17 asks for 200-500 ms. The ease must reach ~95% inside that band."""
    scene._set_selection("node", "v")
    for _ in range(int(0.45 * 30)):       # 450 ms
        scene._step_visuals(1 / 30.0)
    assert scene.node_visual("v")["sat"] > 0.95


def test_line_brightness_fades_rather_than_popping(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "v")
    scene._step_visuals(1 / 30.0)
    amp = scene.link_visual(1)
    assert 0.2 < amp < 1.0
    _settle(scene)
    assert scene.link_visual(1) > 0.95


def test_only_the_selected_networks_lines_light_up(scene):
    _select(scene)
    assert scene.link_visual(1) > 0.9
    # core→far reaches the neighbourhood only through the core, so it is never
    # one of the selection's own lines (§8, §28).
    assert 4 not in scene.active_link_ids()
    assert scene.link_visual(4) < scene.link_visual(1)
    scene.set_secondary_degree(False)
    _settle(scene)
    assert scene.link_visual(4) < 0.35


def test_deselect_fades_the_network_back_to_grey(scene):
    _select(scene)
    scene.clear_selection()
    assert scene.selected_node_id() is None
    assert scene.active_link_ids() == set()
    scene._step_visuals(1 / 30.0)
    assert scene.node_visual("v")["sat"] < 1.0
    _settle(scene)
    assert scene.node_visual("v")["sat"] == pytest.approx(MUTED_SATURATION, abs=0.02)


# ── animated particles (§9-§14, §23-§25) ─────────────────────────────────────

def test_particles_run_only_inside_the_selected_network(scene):
    _select(scene)
    ids = {row["i"] for row in scene._particle_links()}
    assert ids and ids <= scene.active_link_ids()
    assert 4 not in ids


def test_a_particle_travels_from_the_relationships_source_to_its_target(scene):
    """Direction comes from the data: p0 is the `s` end, p1 the `t` end."""
    scene._compute_layout(float(scene.width()), float(scene.height()))
    rows = {row["i"]: row for row in scene._link_rows}
    for i, link in enumerate(scene._graph["links"]):
        row = rows[i]
        assert row["p0"] == scene._lay_ids[link["s"]][2]
        assert row["p1"] == scene._lay_ids[link["t"]][2]


def test_reversing_a_relationship_reverses_its_particle(scene):
    graph = _graph()
    graph["links"] = [{"s": "cua", "t": "v", "type": "uses", "weight": 1.0}]
    scene._get_graph = lambda: {k: list(v) for k, v in graph.items()}
    scene.refresh()
    scene._compute_layout(float(scene.width()), float(scene.height()))
    row = scene._link_rows[0]
    assert row["p0"] == scene._lay_ids["cua"][2]
    assert row["p1"] == scene._lay_ids["v"][2]


def test_a_directional_relationship_does_not_animate_backwards(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    assert all(not row["two_way"] for row in scene._link_rows)


def test_a_two_way_relationship_is_animated_both_ways_when_the_data_says_so(scene):
    graph = _graph()
    graph["links"] = [{"s": "v", "t": "cua", "type": "syncs_with", "weight": 1.0,
                       "bidirectional": True}]
    scene._get_graph = lambda: {k: list(v) for k, v in graph.items()}
    scene.refresh()
    scene._compute_layout(float(scene.width()), float(scene.height()))
    assert scene._link_rows[0]["two_way"] is True


def test_the_particle_loop_fades_at_both_ends(scene):
    assert scene._particle_fade(0.0) == pytest.approx(0.0)
    assert scene._particle_fade(1.0) == pytest.approx(0.0)
    assert scene._particle_fade(0.5) == pytest.approx(1.0)
    # Monotone on the way in, so there is no visible pop at the crossing.
    values = [scene._particle_fade(u / 20.0) for u in range(0, 11)]
    assert values == sorted(values)


def test_particles_are_capped_and_the_strongest_relationships_win(scene):
    scene._get_graph = lambda: _star_graph(CONNECTION_PARTICLE_CAP * 3)
    scene.refresh()
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "hub")
    _settle(scene)
    pool = scene._particle_links()
    assert len(pool) <= CONNECTION_PARTICLE_CAP
    weights = [row["weight"] for row in scene._link_rows]
    assert max(weights) in [row["weight"] for row in pool]


def test_particle_speed_is_configurable(scene):
    assert scene._particle_speed == pytest.approx(CONNECTION_PARTICLE_SPEED)
    scene.set_particle_speed(1.75)
    assert scene._particle_speed == pytest.approx(1.75)
    scene.set_particle_speed(999)
    assert scene._particle_speed <= 4.0


def test_a_large_graph_only_animates_the_selected_neighbourhood(scene):
    """Hundreds of links must not all animate at once (§25, §24)."""
    scene._get_graph = lambda: _star_graph(400)
    scene.refresh()
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "n7")
    _settle(scene)
    assert len(scene.active_link_ids()) == 1
    assert len(scene._particle_links()) <= max(12, CONNECTION_PARTICLE_CAP // 2)


# ── selecting a relationship (§15, §16) ──────────────────────────────────────

def _clearest_point_on_a_link(scene):
    """(index, point) for the relationship with the most room around it.

    A bead always wins the hover when the pointer is over it, so the test picks
    a point on a line that is genuinely between beads.
    """
    best = (None, None, -1.0)
    for row in scene._link_rows:
        for step in range(1, 20):
            t = step / 20.0
            pt = QPointF(row["p0"].x() + (row["p1"].x() - row["p0"].x()) * t,
                         row["p0"].y() + (row["p1"].y() - row["p0"].y()) * t)
            gap = min((other[2] - pt).manhattanLength() for other in scene._lay)
            if gap > best[2]:
                best = (row["i"], pt, gap)
    return best[0], best[1]


def test_hovering_a_relationship_reports_which_one_is_under_the_pointer(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    index, point = _clearest_point_on_a_link(scene)
    _mouse(scene, QEvent.Type.MouseMove, point)
    assert scene.hovered_link_id() == index
    assert scene.hovered_node_id() is None


def test_a_bead_under_the_pointer_wins_over_the_line_behind_it(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    point = scene.orb_screen_positions()["v"]
    _mouse(scene, QEvent.Type.MouseMove, point)
    assert scene.hovered_node_id() == "v"
    assert scene.hovered_link_id() is None


def test_clicking_a_relationship_selects_it_and_lights_both_ends(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    row = next(r for r in scene._link_rows if r["i"] == 1)
    mid = QPointF((row["p0"].x() + row["p1"].x()) / 2.0,
                  (row["p0"].y() + row["p1"].y()) / 2.0)
    scene._set_selection("link", 1)
    _settle(scene)
    assert scene.selected_link_id() == 1
    assert scene.selected_node_id() is None
    # Both ends of the picked relationship are lit; the load-bearing raw value
    # is the armed link itself.
    assert {"v", "cua"} <= scene.highlighted_node_ids()
    assert 1 in scene.active_link_ids()
    assert "uses" in scene._link_info(1)["type"]
    assert scene.node_visual("v")["sat"] == pytest.approx(1.0, abs=0.03)
    assert scene.node_visual("cua")["sat"] == pytest.approx(1.0, abs=0.03)
    assert 1 in scene.active_link_ids()


# ── the detail card (§16, §20) ───────────────────────────────────────────────

def test_the_card_describes_the_selected_memory_from_its_real_record(scene):
    _select(scene)
    card = scene._card
    assert card["kind"] == "memory"
    assert card["title"] == "Vision Panel"
    assert card["category"] == "PROJECTS"
    assert card["body"] == "Jarvis visual tracking panel."
    assert "Cua" in card["connections"] and "Gemini" in card["connections"]
    stats = dict(card["stats"])
    assert stats["Importance"] == "0.90"
    assert stats["Confidence"] == "0.95"
    assert stats["Created"].startswith("2023-") or stats["Created"].startswith("20")
    assert "Updated" in stats


def test_the_card_names_the_selected_relationship(scene):
    scene._set_selection("link", 1)
    card = scene._card
    assert card["kind"] == "relationship"
    assert card["category"] == "USES"
    stats = dict(card["stats"])
    assert stats["Source"] == "Vision Panel"
    assert stats["Target"] == "Cua"
    assert stats["Type"] == "uses"


def test_the_card_only_lists_real_connections(scene):
    _select(scene)
    for name in scene._card["connections"]:
        assert any(node["label"] == name for node in scene._graph["nodes"])


def test_deselecting_clears_the_card(scene):
    _select(scene)
    scene.clear_selection()
    assert scene._card is None


def test_the_card_is_rebuilt_from_the_new_graph_when_the_store_changes(scene):
    _select(scene)
    changed = _graph()
    changed["nodes"] = [n for n in changed["nodes"] if n["id"] != "v"]
    changed["links"] = [l for l in changed["links"] if "v" not in (l["s"], l["t"])]
    scene._get_graph = lambda: changed
    scene.refresh()
    # The selected memory no longer exists, so the selection is dropped rather
    # than left pointing at a memory the store does not have.
    assert scene.selected_node_id() is None
    assert scene._card is None


# ── mouse interaction: select, deselect, drag (§18) ──────────────────────────

def test_clicking_a_memory_selects_it(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    point = scene.orb_screen_positions()["v"]
    _mouse(scene, QEvent.Type.MouseButtonPress, point)
    _mouse(scene, QEvent.Type.MouseButtonRelease, point)
    assert scene.selected_node_id() == "v"


def test_clicking_empty_space_deselects_without_reloading_the_graph(scene):
    _select(scene)
    snapshot = list(scene._graph["nodes"])
    worlds = {nid: scene.world_position(nid) for nid in ("v", "cua", "gem", "head")}
    _mouse(scene, QEvent.Type.MouseButtonPress, QPointF(4, 4))
    _mouse(scene, QEvent.Type.MouseButtonRelease, QPointF(4, 4))
    assert scene.selected_node_id() is None
    assert scene.active_link_ids() == set()
    assert list(scene._graph["nodes"]) == snapshot           # nothing reloaded
    for nid, before in worlds.items():
        assert scene.world_position(nid) == before           # nothing moved


def test_dragging_the_camera_does_not_change_the_selection(scene):
    _select(scene)
    _mouse(scene, QEvent.Type.MouseButtonPress, QPointF(4, 4))
    _mouse(scene, QEvent.Type.MouseMove, QPointF(120, 90))
    _mouse(scene, QEvent.Type.MouseButtonRelease, QPointF(120, 90))
    assert scene.selected_node_id() == "v"


def test_the_state_model_covers_every_node_and_link(scene):
    _select(scene)
    nodes = {NodeState.DEFAULT, NodeState.SELECTED, NodeState.CONNECTED,
             NodeState.SECONDARY, NodeState.MUTED}
    links = {LinkState.DEFAULT, LinkState.ACTIVE, LinkState.SECONDARY,
             LinkState.MUTED}
    for node in scene._graph["nodes"]:
        assert scene._node_state(node) in nodes
    for row in scene._link_rows:
        assert scene._link_state(row["i"]) in links


# ── camera (§19) ─────────────────────────────────────────────────────────────

def _step_camera(orb, seconds=2.0, dt=1 / 30.0):
    for _ in range(int(seconds / dt)):
        orb._step_camera_focus(dt)


def test_focusing_the_camera_keeps_the_network_in_frame(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "v")
    _settle(scene)
    _step_camera(scene)
    scene._compute_layout(float(scene.width()), float(scene.height()))
    pts = [scene.world_position(nid) for nid in ("v",) + tuple(V_NEIGHBOURS)]
    rows = {row[1]["id"]: row[2] for row in scene._lay}
    for nid in ("v", "cua", "gem", "head", "core"):
        pt = rows[nid]
        assert 0 <= pt.x() <= scene.width()
        assert 0 <= pt.y() <= scene.height()
    assert len(pts) == 5


def test_the_camera_glides_rather_than_jumping(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    start = QPointF(scene._pan)
    scene._set_selection("node", "v")
    scene._step_camera_focus(1 / 30.0)
    moved = (scene._pan - start).manhattanLength()
    assert moved > 0.0                       # it is heading somewhere
    _step_camera(scene)
    assert scene._cam_target is None         # and it arrives


def test_deselect_hands_the_users_view_back(scene):
    scene._zoom = 1.4
    scene._pan = QPointF(-30.0, 18.0)
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "v")
    _step_camera(scene)
    scene.clear_selection()
    _step_camera(scene)
    assert scene._zoom == pytest.approx(1.4, abs=0.01)
    assert scene._pan.x() == pytest.approx(-30.0, abs=0.5)
    assert scene._pan.y() == pytest.approx(18.0, abs=0.5)


def test_dragging_a_bead_suspends_the_camera_so_it_never_fights_the_pointer(scene):
    scene._compute_layout(float(scene.width()), float(scene.height()))
    point = scene.orb_screen_positions()["v"]
    _mouse(scene, QEvent.Type.MouseButtonPress, point, modifiers=ALT)
    before = QPointF(scene._pan)
    _step_camera(scene, seconds=0.5)
    assert scene._pan == before


# ── the paint path itself ────────────────────────────────────────────────────

def test_the_scene_paints_with_a_selection_a_card_and_particles(scene, qapp):
    """The whole paint path must run for real, not just the state model.

    This is the only test that actually rasterises the selected network, the
    blue lines, the travelling particles, the hover chip and the detail card.
    """
    scene._compute_layout(float(scene.width()), float(scene.height()))
    scene._set_selection("node", "v")
    _settle(scene)
    scene._hover_link = 1
    assert scene._particle_links(), "a selected network should be emitting"
    selected = scene.grab()
    assert not selected.isNull()
    assert selected.width() == scene.width()
    # The coloured network must actually change the pixels, not just the state.
    scene.clear_selection()
    scene._hover_link = None
    _settle(scene)
    idle = scene.grab()
    assert selected.toImage() != idle.toImage()


def test_the_scene_paints_an_empty_graph(scene, qapp):
    scene._get_graph = lambda: {"nodes": [], "links": []}
    scene.refresh()
    assert not scene.grab().isNull()


def test_the_card_fits_a_narrow_panel(scene, qapp):
    scene.resize(300, 260)
    qapp.processEvents()
    scene._compute_layout(300.0, 260.0)
    scene._set_selection("node", "v")
    _settle(scene)
    rows = scene._card_layout(scene._card, max(150.0, min(236.0, 300.0 - 24.0)))
    total = sum(row[2] for row in rows) + 24.0
    assert total <= 260.0                       # the card cannot overflow
    assert not scene.grab().isNull()


def test_a_repaint_does_not_re_measure_the_card(scene):
    _select(scene)
    first = scene._card_layout(scene._card, 236.0)
    assert scene._card_layout(scene._card, 236.0) is first


def _saturated_pixels(widget) -> int:
    """How many sampled pixels carry real colour rather than grey/background."""
    img = widget.grab().toImage()
    count = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            col = img.pixelColor(x, y)
            r, g, b = col.red(), col.green(), col.blue()
            if r + g + b > 120 and max(r, g, b) - min(r, g, b) > 45:
                count += 1
    return count


def test_the_rendered_idle_field_is_grey_and_a_selection_colours_it(qapp):
    """The headline behaviour, measured on real pixels rather than state.

    Idle: the field is desaturated. Selected: the neighbourhood carries its
    category colours. Deselected: back to grey, with nothing destroyed.
    """
    orb = BrainGraph3D(get_graph=lambda: _star_graph(24))
    orb.resize(720, 520)
    orb.show()
    qapp.processEvents()
    try:
        orb._compute_layout(720.0, 520.0)
        idle = _saturated_pixels(orb)
        orb._set_selection("node", "hub")
        _settle(orb)
        selected = _saturated_pixels(orb)
        assert selected > idle * 2, f"idle {idle} selected {selected}"
        orb.clear_selection()
        _settle(orb)
        assert _saturated_pixels(orb) <= idle * 1.5
    finally:
        orb.close()
        qapp.processEvents()


def test_a_selection_adds_blue_lines_and_particles_to_the_render(qapp):
    """Blue is only present once a memory is selected (§7, §29)."""
    def blueness(widget):
        img = widget.grab().toImage()
        n = 0
        for y in range(0, img.height(), 2):
            for x in range(0, img.width(), 2):
                col = img.pixelColor(x, y)
                if col.blue() > 120 and col.blue() - col.red() > 70:
                    n += 1
        return n

    orb = BrainGraph3D(get_graph=lambda: _star_graph(24))
    orb.resize(720, 520)
    orb.show()
    qapp.processEvents()
    try:
        orb._compute_layout(720.0, 520.0)
        idle = blueness(orb)
        orb._set_selection("node", "hub")
        _settle(orb)
        assert blueness(orb) > idle * 2
    finally:
        orb.close()
        qapp.processEvents()
