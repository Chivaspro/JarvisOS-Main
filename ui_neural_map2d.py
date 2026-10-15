"""The 2D neural map — an Obsidian-style force-directed graph of memory.

The visual language is the Obsidian graph view: a dark field, thin glowing
edges, and nodes whose size carries their link count while their colour groups
them by type, folder or tag. The *data* rule matters more than the picture:
every node is a real note or a real memory, and every edge is a relationship
that exists in the store (a wiki link, a backlink, a belief-to-evidence link).
Nothing is drawn to fill the screen.

Three decisions shape this module:

**The layout is a grid-aggregated force simulation.** Repulsion is computed
against the centre of mass of each occupied grid cell around a node rather than
against every other node — a Barnes-Hut-style approximation in numpy, so a
2,000-node vault lays out in milliseconds per tick instead of the seconds a
pairwise loop would take. Link springs, a centre force and damping finish the
model; the timer stops when the total kinetic energy settles.

**Drawing is culled and layered by zoom.** Off-screen nodes and edges are
skipped before a single primitive is emitted, glow gradients are dropped above
400 visible nodes, and labels appear only when the view is zoomed in far enough
(or for the selected, hovered and highest-degree nodes). A large vault
therefore costs about what a small one does.

**The view never invents a source.** A loader function supplies the graph; the
Mind panel supplies ``ui_neural_map.build_graph`` (vault when selected, brain
store otherwise) and a background thread builds it off the GUI thread. The
panel can also drive ``reload_async`` from the vault watcher, so a note edited
in Obsidian reaches the map without a restart.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from pathlib import Path

import numpy as np

from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

import ui_theme as _t
from ui_theme import (
    ACCENT, ACCENT_BRIGHT, ACCENT_DEEP, AMBER, BG_DEEP, BORDER_FAINT,
    FONT_UI, GOLD, SURFACE, TEXT_BRIGHT, TEXT_DIM, TEXT_FAINT, TEXT_MID,
    button_css, checkbox_css, field_css, qcolor as _qcolor, rgba as _rgba,
    slider_css,
)

__all__ = ["NeuralMap2D", "open_neural_map2d"]

#: Group colours for folder/tag modes: one blue ramp, cyan into blue-white, so
#: the map stays inside the JARVIS palette no matter how many groups appear.
_GROUP_RAMP = (
    "#00d4ff", "#41b9f0", "#6fc6ff", "#8ceaff", "#37d6d0",
    "#59a9e8", "#7f9dff", "#a8c9ff", "#2fa8cc", "#9ae8ff",
)

#: Per-type colour. Notes and memories are the working cyan; beliefs are
#: blue-white; dreams keep the one signature accent (gold); tags get a teal
#: that reads as part of the same family.
_TYPE_COLORS = {
    "note": ACCENT,
    "memory": ACCENT,
    "belief": "#8ceaff",
    "dream": GOLD,
    "task": AMBER,
    "tag": "#41d6c3",
    "folder": ACCENT_DEEP,
    "core": "#ff6b9d",
    "attachment": TEXT_FAINT,
}

_EDGE_ALPHA = {
    "link": 120, "backlink": 100, "tag": 55, "folder": 40, "attachment": 45,
    "has_belief": 60, "has_dream": 60, "dream_source": 80, "related": 80,
    "has_memory": 55, "owns_task": 60, "semantic": 60,
}

_BASE_RADIUS = {"core": 12.0, "folder": 7.5, "tag": 4.5, "attachment": 4.0}

#: Above this many nodes the glow/gradient work is skipped — the map stays
#: readable and the repaint stays cheap.
_EFFECT_LIMIT = 400

#: Grid hashing constants: (col + BIAS) * SPAN + (row + BIAS) makes a stable
#: positive integer key for any cell, so sorting groups cells contiguously.
_CELL_BIAS = 1_000_000
_CELL_SPAN = 2_000_003

#: Graphics refresh while the simulation runs. 16 ms is one frame at 60 Hz;
#: very large graphs step every 24 ms so a frame stays inside its budget.
#: The live values follow the Performance profile.
_TICK_MS = 16
_TICK_MS_LARGE = 24
_LARGE_GRAPH = 1500


def _perf_settings() -> dict:
    try:
        from ui_perfomance import effective
        return effective()
    except Exception:
        return {}


def _perf_tick_ms(large: bool) -> int:
    settings = _perf_settings()
    base = int(settings.get("neural_tick_ms") or _TICK_MS)
    if large:
        base = max(base, _TICK_MS_LARGE)
    return max(8, min(100, base))


def _perf_effects_enabled() -> bool:
    return bool(_perf_settings().get("neural_effects", True))


class _Node:
    """One graph node: metadata plus its degree (the simulation owns x/y)."""

    __slots__ = ("id", "label", "type", "path", "folder", "tags", "mtime",
                 "body", "degree")

    def __init__(self, data: dict, degree: int = 0) -> None:
        self.id = str(data.get("id") or "")
        self.label = " ".join(str(data.get("label") or self.id).split())[:70]
        self.type = str(data.get("type") or "note")
        self.path = str(data.get("path") or "")
        folder = str(data.get("folder") or "")
        if not folder and self.path and "/" in self.path.replace("\\", "/"):
            folder = self.path.replace("\\", "/").rsplit("/", 1)[0]
        self.folder = folder
        tags = data.get("tags") or []
        self.tags = [str(tag) for tag in tags if str(tag).strip()][:12]
        try:
            self.mtime = float(data.get("mtime") or 0.0)
        except Exception:
            self.mtime = 0.0
        self.body = " ".join(
            str(data.get("body_head") or data.get("content") or "").split())[:400]
        self.degree = int(degree)


def _seed(nodes: list[_Node], previous: dict) -> np.ndarray:
    """Starting positions: keep the ones already on screen, spiral the rest.

    Keeping positions across a live refresh is what stops the map from
    reshuffling every time the vault index changes — a graph the user is
    reading must stay the graph they are reading.
    """
    out = np.zeros((len(nodes), 2), dtype=np.float64)
    for i, node in enumerate(nodes):
        cached = previous.get(node.id)
        if cached is not None:
            out[i, 0], out[i, 1] = float(cached[0]), float(cached[1])
    golden = math.pi * (3.0 - math.sqrt(5.0))
    fresh = 0
    for i, node in enumerate(nodes):
        if node.id in previous:
            continue
        radius = 40.0 + 26.0 * math.sqrt(fresh + 1)
        angle = (fresh + 1) * golden
        out[i, 0] = radius * math.cos(angle)
        out[i, 1] = radius * math.sin(angle)
        fresh += 1
    return out


def _hash_pick(key: str) -> int:
    """Deterministic group index (stable across runs, unlike Python's hash)."""
    text = str(key or "").encode("utf-8", "ignore")
    return int(sum(text[:24])) % len(_GROUP_RAMP)


def _top_degree(nodes: list[_Node]) -> int:
    """Degree at the top ~8%: labels for the hubs show even when zoomed out."""
    if not nodes:
        return 0
    degrees = sorted(node.degree for node in nodes)
    cut = degrees[max(0, min(len(degrees) - 1, int(len(degrees) * 0.92)))]
    return int(cut)


class _Sim:
    """Grid-aggregated force layout over numpy arrays.

    Each tick, occupied grid cells are reduced to (mass, centre of mass); every
    node is pushed away from the aggregate of the 24 cells around it (its own
    cell with its own mass removed) and pulled toward its linked neighbours.
    That is the Barnes-Hut idea with a uniform grid instead of a quadtree —
    O(n) numpy work per tick at any graph size that fits this panel.
    """

    __slots__ = ("n", "pos", "vel", "pinned", "_src", "_dst", "params",
                 "energy", "cooling", "frames")

    def __init__(self) -> None:
        self.n = 0
        self.pos = np.zeros((0, 2), dtype=np.float64)
        self.vel = np.zeros((0, 2), dtype=np.float64)
        self.pinned = np.zeros(0, dtype=bool)
        self._src = np.zeros(0, dtype=np.int64)
        self._dst = np.zeros(0, dtype=np.int64)
        self.params = {"center": 0.014, "repel": 1.0, "link": 0.02,
                       "distance": 70.0}
        self.energy = 0.0
        self.cooling = 1.0
        self.frames = 0

    def reheat(self) -> None:
        """Give the layout its force budget back (a drag, a filter, a reload)."""
        self.cooling = 1.0
        self.frames = 0

    def build(self, count: int, positions, pairs) -> None:
        n = max(0, int(count))
        self.n = n
        self.pos = np.asarray(positions, dtype=np.float64).reshape(n, 2)
        self.vel = np.zeros((n, 2), dtype=np.float64)
        self.pinned = np.zeros(n, dtype=bool)
        if pairs:
            arr = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
            self._src = arr[:, 0].copy()
            self._dst = arr[:, 1].copy()
        else:
            self._src = np.zeros(0, dtype=np.int64)
            self._dst = np.zeros(0, dtype=np.int64)
        self.energy = 0.0
        self.cooling = 1.0
        self.frames = 0

    def step(self) -> float:
        n = self.n
        if n == 0:
            return 0.0
        pos, vel = self.pos, self.vel
        distance = max(18.0, float(self.params["distance"]))
        repel = max(0.05, float(self.params["repel"]))
        cell = max(42.0, distance * 1.15)

        cols = np.floor(pos[:, 0] / cell).astype(np.int64)
        rows = np.floor(pos[:, 1] / cell).astype(np.int64)
        flat = (cols + _CELL_BIAS) * _CELL_SPAN + (rows + _CELL_BIAS)
        order = np.argsort(flat, kind="stable")
        sflat = flat[order]
        uniq, starts = np.unique(sflat, return_index=True)
        ends = np.append(starts[1:], n)
        mass = (ends - starts).astype(np.float64)
        com = np.add.reduceat(pos[order], starts, axis=0) / mass[:, None]

        strength = 1400.0 * repel

        def _push(dxy: np.ndarray, ok: np.ndarray):
            d2 = np.einsum("ij,ij->i", dxy, dxy) + 1.0
            scale = strength / (d2 * np.sqrt(d2))
            scale = np.where(ok, scale, 0.0)
            return dxy[:, 0] * scale, dxy[:, 1] * scale

        own = np.searchsorted(uniq, flat)
        own_mass = mass[own]
        own_com = com[own]
        safe_mass = np.maximum(own_mass - 1.0, 1.0)
        excl = (own_com * own_mass[:, None] - pos) / safe_mass[:, None]
        fx, fy = _push(excl, own_mass > 1.0)
        for ddx in (-2, -1, 0, 1, 2):
            for ddy in (-2, -1, 0, 1, 2):
                if ddx == 0 and ddy == 0:
                    continue
                want = ((cols + ddx + _CELL_BIAS) * _CELL_SPAN
                        + (rows + ddy + _CELL_BIAS))
                found = np.searchsorted(uniq, want)
                found = np.minimum(found, len(uniq) - 1)
                ok = uniq[found] == want
                if not bool(ok.any()):
                    continue
                px, py = _push(com[found] - pos, ok)
                fx += px
                fy += py
        # Annealing. A grid-aggregated repulsion never reaches a perfect
        # equilibrium — a node crossing a cell boundary shifts the aggregate
        # force, which would feed a limit cycle forever. The force budget is
        # therefore decayed geometrically (about six seconds to the floor), and
        # ``reheat`` restores it whenever the user interacts with the map.
        if self.frames > 40:
            self.cooling = max(0.008, self.cooling * 0.985)
        self.frames += 1
        cool = self.cooling

        vel[:, 0] += fx * cool
        vel[:, 1] += fy * cool

        if len(self._src):
            si, ti = self._src, self._dst
            delta = pos[ti] - pos[si]
            dist = np.sqrt(np.einsum("ij,ij->i", delta, delta)) + 1e-9
            diff = (dist - distance) * max(0.001, float(self.params["link"]))
            force = delta * (diff / dist * cool)[:, None]
            np.add.at(vel, si, force)
            np.add.at(vel, ti, -force)

        centre = max(0.0, float(self.params["center"]))
        if centre:
            vel -= pos * (centre * cool)

        vel[self.pinned] = 0.0
        moving = ~self.pinned
        # Braking: damping hardens as the force budget drains, so the last of
        # the motion dies out instead of boiling forever.
        damping = 0.82 * max(0.30, cool ** 0.25)
        vel[moving] *= damping
        speed = np.sqrt(np.einsum("ij,ij->i", vel, vel))
        fast = speed > 30.0
        if bool(fast.any()):
            vel[fast] *= (30.0 / speed[fast])[:, None]
        pos[moving] += vel[moving]
        self.energy = float(np.einsum("ij,ij->i", vel, vel).sum())
        return self.energy


class _Canvas(QWidget):
    """The drawing surface and its direct-manipulation gestures."""

    def __init__(self, view: "NeuralMap2D") -> None:
        super().__init__(view)
        self.view = view
        self._panning = False
        self._pan_start = QPointF(0.0, 0.0)
        self._pan_origin = QPointF(0.0, 0.0)
        self.setMouseTracking(True)
        self.setMinimumSize(300, 180)
        self.setStyleSheet("background:transparent;")
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ── transforms ────────────────────────────────────────────────────────────

    def to_screen(self, x: float, y: float) -> QPointF:
        view = self.view
        return QPointF(x * view._scale + view._offset.x() + self.width() / 2.0,
                       y * view._scale + view._offset.y() + self.height() / 2.0)

    def to_world(self, pos: QPointF) -> QPointF:
        view = self.view
        return QPointF(
            (pos.x() - self.width() / 2.0 - view._offset.x()) / view._scale,
            (pos.y() - self.height() / 2.0 - view._offset.y()) / view._scale)

    def fit(self) -> None:
        view = self.view
        nodes = view._nodes
        if not nodes:
            view._scale = 1.0
            view._offset = QPointF(0.0, 0.0)
            self.update()
            return
        vis = view._visible_ids()
        xy = view._sim.pos
        xs: list[float] = []
        ys: list[float] = []
        for i, node in enumerate(nodes):
            if not view._node_visible(node, vis):
                continue
            xs.append(float(xy[i, 0]))
            ys.append(float(xy[i, 1]))
        if not xs:
            xs = [float(xy[i, 0]) for i in range(len(nodes))]
            ys = [float(xy[i, 1]) for i in range(len(nodes))]
        span_x = max(80.0, max(xs) - min(xs))
        span_y = max(80.0, max(ys) - min(ys))
        width = max(120.0, float(self.width()))
        height = max(120.0, float(self.height()))
        view._scale = min(2.5, max(0.12, min(width / (span_x + 140.0),
                                               height / (span_y + 140.0))))
        view._offset = QPointF(-((max(xs) + min(xs)) / 2.0) * view._scale,
                               -((max(ys) + min(ys)) / 2.0) * view._scale)
        self.update()

    def center_on(self, index: int) -> None:
        view = self.view
        if 0 <= int(index) < len(view._nodes):
            view._offset = QPointF(
                -float(view._sim.pos[index, 0]) * view._scale,
                -float(view._sim.pos[index, 1]) * view._scale)
            self.update()

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            self._paint(painter)
        except Exception:
            pass
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        view = self.view
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(_qcolor(BG_DEEP, 255)))
        nodes = view._nodes
        if not nodes:
            painter.setPen(QColor(TEXT_DIM))
            painter.setFont(QFont(FONT_UI.strip("'"), 8))
            painter.drawText(
                self.rect().adjusted(16, 16, -16, -16),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                view._empty_text())
            return

        scale = view._scale
        width, height = float(self.width()), float(self.height())
        ox, oy = view._offset.x(), view._offset.y()
        xy = view._sim.pos
        visible = view._visible_ids()
        query = view._query
        hits = view._search_hits
        focus = view._focus_set()
        selected = view._selected_id
        hover = view._hover_id
        effects = _perf_effects_enabled() and len(nodes) <= _EFFECT_LIMIT
        margin = 60.0

        screen: dict[int, tuple] = {}
        on_screen: set[int] = set()
        for i, node in enumerate(nodes):
            if not view._node_visible(node, visible):
                continue
            sx = float(xy[i, 0]) * scale + ox + width / 2.0
            sy = float(xy[i, 1]) * scale + oy + height / 2.0
            screen[i] = (sx, sy)
            if -margin <= sx <= width + margin and -margin <= sy <= height + margin:
                on_screen.add(i)

        # ── edges first, so nodes sit on top of them ──────────────────────────
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for si, ti, kind, _weight in view._pairs:
            if si not in screen or ti not in screen:
                continue
            ax, ay = screen[si]
            bx, by = screen[ti]
            if si not in on_screen and ti not in on_screen:
                if (max(ax, bx) < -margin or min(ax, bx) > width + margin
                        or max(ay, by) < -margin or min(ay, by) > height + margin):
                    continue
            hot = bool(focus) and (nodes[si].id in focus or nodes[ti].id in focus)
            alpha = _EDGE_ALPHA.get(kind, 60) + (80 if hot else 0)
            if query and nodes[si].id not in hits and nodes[ti].id not in hits:
                alpha = min(alpha, 22)
            colour = QColor(ACCENT if hot else TEXT_DIM)
            colour.setAlpha(min(235, alpha))
            painter.setPen(QPen(colour, max(0.6, view._link_width * (1.5 if hot else 1.0))))
            painter.drawLine(QPointF(ax, ay), QPointF(bx, by))

        # ── nodes, with glow only on a small graph ────────────────────────────
        for i in on_screen:
            node = nodes[i]
            sx, sy = screen[i]
            radius = max(2.2, view._radius(node) * scale)
            colour = QColor(view._colour(node))
            dim = False
            if focus and node.id not in focus:
                dim = True
            if query and node.id not in hits:
                dim = True
            if dim:
                colour.setAlpha(58)
            if effects and radius > 3.0 and not dim:
                glow = QRadialGradient(QPointF(sx, sy), radius * 2.8)
                glow.setColorAt(0.0, QColor(colour.red(), colour.green(),
                                            colour.blue(),
                                            70 if node.id == selected else 36))
                glow.setColorAt(1.0, QColor(0, 0, 0, 0))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(glow))
                painter.drawEllipse(QPointF(sx, sy), radius * 2.8, radius * 2.8)
            painter.setBrush(QBrush(colour))
            painter.setPen(QPen(QColor(0, 0, 0, 130), 1.0))
            painter.drawEllipse(QPointF(sx, sy), radius, radius)
            if node.id == selected:
                pen = QPen(QColor(TEXT_BRIGHT))
                pen.setWidthF(1.6)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(sx, sy), radius + 3.5, radius + 3.5)
            elif node.id == hover:
                pen = QPen(QColor(ACCENT_BRIGHT))
                pen.setWidthF(1.2)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(sx, sy), radius + 2.5, radius + 2.5)

        # ── labels last, and only where they earn their place ─────────────────
        painter.setFont(QFont(FONT_UI.strip("'"), 7))
        many = len(nodes) > 40
        for i in on_screen:
            node = nodes[i]
            prominent = node.id in (selected, hover) or node.id in focus
            if not prominent and many:
                if scale < view._label_threshold and node.degree < view._top_degree:
                    continue
            sx, sy = screen[i]
            colour = QColor(TEXT_BRIGHT if prominent else TEXT_MID)
            colour.setAlpha(230 if prominent else 165)
            painter.setPen(colour)
            painter.drawText(
                QPointF(sx + max(2.2, view._radius(node) * scale) + 4.0, sy + 3.0),
                node.label[:30])

    # ── interaction ───────────────────────────────────────────────────────────

    def _node_at(self, pos: QPointF):
        view = self.view
        if not view._nodes:
            return None
        visible = view._visible_ids()
        xy = view._sim.pos
        scale = view._scale
        best = None
        for i, node in enumerate(view._nodes):
            if not view._node_visible(node, visible):
                continue
            sx = float(xy[i, 0]) * scale + view._offset.x() + self.width() / 2.0
            sy = float(xy[i, 1]) * scale + view._offset.y() + self.height() / 2.0
            radius = max(6.0, view._radius(node) * scale + 5.0)
            distance = math.hypot(sx - pos.x(), sy - pos.y())
            if distance <= radius and (best is None or distance < best[0]):
                best = (distance, i, node)
        return (best[1], best[2]) if best else None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._node_at(event.position())
        if hit is not None:
            view = self.view
            view._select(hit[1])
            view._drag = hit[0]
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        self._panning = True
        self._pan_start = event.position()
        self._pan_origin = QPointF(self.view._offset)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        view = self.view
        if view._drag is not None and 0 <= view._drag < len(view._nodes):
            world = self.to_world(event.position())
            view._sim.pos[view._drag, 0] = world.x()
            view._sim.pos[view._drag, 1] = world.y()
            view._sim.pinned[view._drag] = True
            view._sim.vel[view._drag] = 0.0
            view._wake()
            self.update()
            return
        if self._panning:
            delta = event.position() - self._pan_start
            view._offset = QPointF(self._pan_origin.x() + delta.x(),
                                   self._pan_origin.y() + delta.y())
            self.update()
            return
        hit = self._node_at(event.position())
        node_id = hit[1].id if hit is not None else ""
        if node_id != view._hover_id:
            view._hover_id = node_id
            self.update()
        self.setToolTip(f"{hit[1].label}\n{hit[1].type} · {hit[1].degree} link(s)"
                        if hit is not None else "")

    def mouseReleaseEvent(self, event) -> None:
        self.view._drag = None
        self._panning = False
        self.unsetCursor()
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, event) -> None:
        hit = self._node_at(event.position())
        if hit is not None:
            self.view._select(hit[1])
            self.view._open(hit[1])

    def leaveEvent(self, event) -> None:
        if self.view._hover_id:
            self.view._hover_id = ""
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        view = self.view
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_scale = view._scale * factor
        if not (0.12 <= new_scale <= 6.0):
            return
        before = self.to_world(event.position())
        view._scale = new_scale
        after = self.to_world(event.position())
        view._offset = QPointF(
            view._offset.x() + (after.x() - before.x()) * new_scale,
            view._offset.y() + (after.y() - before.y()) * new_scale)
        self.update()


class NeuralMap2D(QWidget):
    """The 2D neural map: controls, canvas and preview, in one compact card."""

    nodeSelected = pyqtSignal(dict)
    openRequested = pyqtSignal(dict)
    statusMessage = pyqtSignal(str)
    graphReady = pyqtSignal(dict)
    graphBuilt = pyqtSignal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._nodes: list[_Node] = []
        self._by_id: dict[str, _Node] = {}
        self._pairs: list[tuple] = []
        self._adj: dict[str, set] = {}
        self._sim = _Sim()
        self._scale = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._hover_id = ""
        self._selected_id = ""
        self._drag = None
        self._query = ""
        self._search_hits: set[str] = set()
        self._group_mode = "TYPE"
        self._tag_filter = ""
        self._folder_filter = ""
        self._local_depth = 0
        self._node_scale = 1.0
        self._link_width = 1.0
        self._label_threshold = 0.45
        self._top_degree = 0
        self._source: dict = {}
        self._error = ""
        self._last_status = ""
        self._loader = None
        self._loaded = False
        self._booting = False
        self._reload_again = False
        self._settled = False
        self._quiet = 0
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(_perf_tick_ms(False))
        self._timer.timeout.connect(self._tick)
        # The worker thread emits this signal; the queued connection delivers it
        # back on the GUI thread, where adopting the graph belongs.
        self.graphBuilt.connect(self._on_graph_built)

    # ── public surface ────────────────────────────────────────────────────────

    def set_loader(self, loader) -> None:
        """The callable that builds ``{nodes, links, source}`` off the GUI thread."""
        self._loader = loader

    def reload_async(self) -> None:
        """Rebuild the graph in a worker thread; adopt it on the GUI thread."""
        if self._booting:
            self._reload_again = True
            return
        loader = self._loader
        if loader is None:
            return
        self._booting = True
        self._emit_status_text("Building the graph…")

        def _work() -> None:
            try:
                graph = loader()
            except Exception as exc:
                graph = {"nodes": [], "links": [], "error": str(exc)}
            self.graphBuilt.emit(dict(graph or {}))

        threading.Thread(target=_work, name="neural-graph", daemon=True).start()

    def set_graph(self, graph: dict) -> None:
        """Adopt one graph — nodes, links, source metadata — as the map."""
        graph = dict(graph or {})
        raw_nodes = list(graph.get("nodes") or [])
        raw_links = list(graph.get("links") or [])
        self._source = dict(graph.get("source") or {})
        self._error = str(graph.get("error") or "")

        previous: dict[str, tuple] = {}
        if self._nodes:
            xy = self._sim.pos
            for i, node in enumerate(self._nodes):
                previous[node.id] = (float(xy[i, 0]), float(xy[i, 1]))

        degree: dict[str, int] = {}
        for edge in raw_links:
            s, t = str(edge.get("s")), str(edge.get("t"))
            if not s or not t or s == t:
                continue
            degree[s] = degree.get(s, 0) + 1
            degree[t] = degree.get(t, 0) + 1

        nodes: list[_Node] = []
        seen: set[str] = set()
        for data in raw_nodes:
            node = _Node(data, degree.get(str(data.get("id")), 0))
            if not node.id or node.id in seen:
                continue
            seen.add(node.id)
            nodes.append(node)

        index = {node.id: i for i, node in enumerate(nodes)}
        pairs: list[tuple] = []
        sim_pairs: list[tuple] = []
        for edge in raw_links:
            si = index.get(str(edge.get("s")))
            ti = index.get(str(edge.get("t")))
            if si is None or ti is None or si == ti:
                continue
            pairs.append((si, ti, str(edge.get("type") or "link"),
                          float(edge.get("weight") or 0.5)))
            sim_pairs.append((si, ti))

        self._nodes = nodes
        self._by_id = {node.id: node for node in nodes}
        self._pairs = pairs
        self._adj = {node.id: set() for node in nodes}
        for si, ti, _kind, _weight in pairs:
            a, b = nodes[si].id, nodes[ti].id
            self._adj[a].add(b)
            self._adj[b].add(a)
        self._sim.build(len(nodes), _seed(nodes, previous), sim_pairs)
        self._timer.setInterval(_perf_tick_ms(len(nodes) > _LARGE_GRAPH))
        self._top_degree = _top_degree(nodes)
        self._hover_id = ""
        self._selected_id = ""
        self._update_detail(None)
        self._rebuild_filters()
        changed = set(previous) ^ set(self._by_id)
        if not previous or len(changed) > max(4, len(nodes) // 4):
            self._canvas.fit()
        self._settled = False
        self._quiet = 0
        self._wake()
        self._emit_status(force=True)
        try:
            self.graphReady.emit({"nodes": raw_nodes, "links": raw_links,
                                  "source": dict(self._source),
                                  "error": self._error})
        except Exception:
            pass

    def stop(self) -> None:
        """Stop the simulation timer (panel teardown)."""
        self._timer.stop()

    @property
    def settled(self) -> bool:
        return bool(self._settled)

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(4)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setStyleSheet(field_css())
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(26)
        self._search.textChanged.connect(self._on_query)
        top.addWidget(self._search, 1)
        self._group_box = QComboBox()
        self._group_box.addItems(["TYPE", "FOLDER", "TAG"])
        self._group_box.setStyleSheet(field_css())
        self._group_box.setMinimumHeight(26)
        self._group_box.setToolTip("Colour groups: by type, vault folder or first tag.")
        self._group_box.currentTextChanged.connect(self._on_group)
        top.addWidget(self._group_box)
        self._local = QSpinBox()
        self._local.setRange(0, 4)
        self._local.setValue(0)
        self._local.setPrefix("L")
        self._local.setToolTip("LOCAL GRAPH — the selected node plus N levels of "
                               "neighbours (0 shows the whole graph).")
        self._local.setStyleSheet(field_css())
        self._local.setMinimumHeight(26)
        self._local.setFixedWidth(56)
        self._local.valueChanged.connect(self._on_local)
        top.addWidget(self._local)
        self._fit_btn = QPushButton("FIT")
        self._fit_btn.setMinimumHeight(26)
        self._fit_btn.setStyleSheet(button_css(26))
        self._fit_btn.clicked.connect(lambda: self._canvas.fit())
        top.addWidget(self._fit_btn)
        self._tune_btn = QPushButton("TUNE")
        self._tune_btn.setCheckable(True)
        self._tune_btn.setMinimumHeight(26)
        self._tune_btn.setStyleSheet(button_css(26))
        self._tune_btn.setToolTip("Filters, display and force settings.")
        self._tune_btn.toggled.connect(self._on_tune)
        top.addWidget(self._tune_btn)
        root.addLayout(top)

        self._tune = QFrame()
        self._tune.setStyleSheet(
            f"QFrame{{background:{_rgba(SURFACE, 140)};"
            f"border:1px solid {_rgba(BORDER_FAINT, 110)};"
            f"border-radius:{int(_t.RADIUS_MD)}px;}}")
        grid = QGridLayout(self._tune)
        grid.setContentsMargins(8, 6, 8, 8)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        self._orphans = QCheckBox("Orphans")
        self._orphans.setChecked(True)
        self._orphans.setStyleSheet(checkbox_css())
        self._orphans.setToolTip("Show notes with no links.")
        self._orphans.toggled.connect(lambda _state: self._filter_changed())
        grid.addWidget(self._orphans, 0, 0)
        self._attach = QCheckBox("Attachments")
        self._attach.setChecked(True)
        self._attach.setStyleSheet(checkbox_css())
        self._attach.setToolTip("Show links to real files (images, PDFs) in the vault.")
        self._attach.toggled.connect(lambda _state: self._filter_changed())
        grid.addWidget(self._attach, 0, 1)
        self._tag_box = QComboBox()
        self._tag_box.addItem("All tags")
        self._tag_box.setStyleSheet(field_css())
        self._tag_box.setMinimumHeight(24)
        self._tag_box.currentTextChanged.connect(self._on_tag)
        grid.addWidget(self._tag_box, 0, 2)
        self._folder_box = QComboBox()
        self._folder_box.addItem("All folders")
        self._folder_box.setStyleSheet(field_css())
        self._folder_box.setMinimumHeight(24)
        self._folder_box.currentTextChanged.connect(self._on_folder)
        grid.addWidget(self._folder_box, 0, 3)

        self._sliders: dict[str, QSlider] = {}
        specs = (
            ("SIZE", "SIZE", 20, 220, 100, "Node size"),
            ("LINKS", "LINKS", 30, 300, 100, "Link thickness"),
            ("LABELS", "LABEL", 10, 250, 45, "Label fade: zoom level where labels appear"),
            ("DIST", "DIST", 30, 180, 70, "Link distance"),
            ("CENTER", "CENTR", 0, 300, 140, "Center force"),
            ("REPEL", "REPEL", 20, 300, 100, "Repel force"),
            ("LINK", "LINKF", 1, 80, 20, "Link force"),
        )
        for i, (key, label, lo, hi, value, tip) in enumerate(specs):
            self._add_slider(grid, 1 + i // 4, i % 4, key, label, lo, hi,
                             value, tip)
        self._tune.setVisible(False)
        root.addWidget(self._tune)

        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{_rgba(BG_DEEP, 210)};"
            f"border:1px solid {_rgba(BORDER_FAINT, 120)};"
            f"border-radius:{int(_t.RADIUS_MD)}px;}}")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        frame_layout.setSpacing(0)
        self._canvas = _Canvas(self)
        frame_layout.addWidget(self._canvas, 1)
        root.addWidget(frame, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        self._detail = QLabel("Select a node for a preview.")
        self._detail.setWordWrap(True)
        self._detail.setMaximumHeight(30)
        self._detail.setStyleSheet(
            f"color:{TEXT_MID};font:500 7pt '{FONT_UI}';background:transparent;")
        bottom.addWidget(self._detail, 1)
        self._open_btn = QPushButton("OPEN")
        self._open_btn.setMinimumHeight(24)
        self._open_btn.setFixedWidth(62)
        self._open_btn.setStyleSheet(button_css(24))
        self._open_btn.setEnabled(False)
        self._open_btn.setToolTip("Open the selected note (Obsidian when the map "
                                  "shows a vault, the file itself otherwise).")
        self._open_btn.clicked.connect(self._open_selected)
        bottom.addWidget(self._open_btn)
        root.addLayout(bottom)

        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color:{TEXT_FAINT};font:500 6.5pt '{FONT_UI}';background:transparent;")
        root.addWidget(self._status)

    def _add_slider(self, grid, row: int, col: int, key: str, label: str,
                    lo: int, hi: int, value: int, tip: str) -> None:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        tag = QLabel(label)
        tag.setStyleSheet(
            f"color:{TEXT_FAINT};font:700 6.5pt '{FONT_UI}';background:transparent;")
        tag.setFixedWidth(36)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(int(lo), int(hi))
        slider.setValue(int(value))
        slider.setStyleSheet(slider_css())
        slider.setMinimumWidth(48)
        slider.setToolTip(tip)
        slider.valueChanged.connect(lambda v, k=key: self._on_slider(k, int(v)))
        layout.addWidget(tag)
        layout.addWidget(slider, 1)
        grid.addWidget(box, row, col)
        self._sliders[key] = slider

    # ── runtime ───────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._nodes:
            self._timer.stop()
            return
        try:
            # A very large graph runs the force field twice per frame while it
            # is still hot: the anneal trajectory is unchanged, the wall-clock
            # layout finishes in about half the time, and the extra cost lasts
            # only for the opening seconds.
            steps = (2 if self._sim.n > _LARGE_GRAPH and self._sim.frames < 140
                     else 1)
            energy = 0.0
            for _ in range(steps):
                energy = self._sim.step()
        except Exception:
            self._timer.stop()
            return
        self._canvas.update()
        limit = max(1.5, self._sim.n * 0.15)
        if energy < limit:
            self._quiet += 1
            if self._quiet >= 30:
                self._timer.stop()
                self._settled = True
                self._emit_status()
        else:
            self._quiet = 0

    def _wake(self) -> None:
        if not self._nodes:
            return
        self._settled = False
        self._quiet = 0
        self._sim.reheat()
        if not self._timer.isActive() and self.isVisible():
            self._timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._loader is not None and not self._loaded:
            self._loaded = True
            self.reload_async()
        elif self._nodes and not self._settled:
            self._wake()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _on_graph_built(self, graph: dict) -> None:
        self._booting = False
        try:
            self.set_graph(graph)
        except Exception as exc:
            self._emit_status_text(f"The graph could not be drawn: {exc}")
        if self._reload_again:
            self._reload_again = False
            self.reload_async()

    # ── filters / controls ────────────────────────────────────────────────────

    def _on_query(self, text: str) -> None:
        self._query = str(text or "").strip().lower()
        if self._query:
            hits: set[str] = set()
            for node in self._nodes:
                hay = (f"{node.label} {node.path} {' '.join(node.tags)} "
                       f"{node.body}").lower()
                if self._query in hay:
                    hits.add(node.id)
            self._search_hits = hits
        else:
            self._search_hits = set()
        self._emit_status()
        self._canvas.update()

    def _on_group(self, text: str) -> None:
        self._group_mode = text if text in ("TYPE", "FOLDER", "TAG") else "TYPE"
        self._canvas.update()

    def _on_local(self, value: int) -> None:
        self._local_depth = max(0, int(value))
        if self._local_depth and not self._selected_id:
            self.statusMessage.emit("LOCAL GRAPH — select a node to centre it.")
        self._wake()
        self._canvas.update()

    def _on_tune(self, shown: bool) -> None:
        self._tune.setVisible(bool(shown))

    def _on_tag(self, text: str) -> None:
        self._tag_filter = "" if text in ("", "All tags") else str(text)
        self._filter_changed()

    def _on_folder(self, text: str) -> None:
        self._folder_filter = "" if text in ("", "All folders") else str(text)
        self._filter_changed()

    def _filter_changed(self) -> None:
        self._wake()
        self._canvas.update()
        self._emit_status()

    def _on_slider(self, key: str, value: int) -> None:
        if key == "SIZE":
            self._node_scale = max(0.2, value / 100.0)
        elif key == "LINKS":
            self._link_width = max(0.3, value / 100.0)
        elif key == "LABELS":
            self._label_threshold = max(0.1, value / 100.0)
        elif key == "CENTER":
            self._sim.params["center"] = value / 10000.0
        elif key == "REPEL":
            self._sim.params["repel"] = max(0.05, value / 100.0)
        elif key == "LINK":
            self._sim.params["link"] = max(0.001, value / 1000.0)
        elif key == "DIST":
            self._sim.params["distance"] = float(max(18, value))
        self._wake()
        self._canvas.update()

    def _rebuild_filters(self) -> None:
        tags = sorted({tag for node in self._nodes for tag in node.tags},
                      key=str.lower)
        folders = sorted({node.folder for node in self._nodes if node.folder},
                         key=str.lower)
        self._fill_combo(self._tag_box, "All tags", tags)
        self._fill_combo(self._folder_box, "All folders", folders)
        if self._tag_filter and self._tag_filter not in tags:
            self._tag_filter = ""
        if self._folder_filter and self._folder_filter not in folders:
            self._folder_filter = ""

    def _fill_combo(self, box: QComboBox, all_label: str,
                    values: list[str]) -> None:
        current = box.currentText()
        box.blockSignals(True)
        try:
            box.clear()
            box.addItem(all_label)
            box.addItems(values)
            if current and current in values:
                box.setCurrentText(current)
            else:
                box.setCurrentText(all_label)
        finally:
            box.blockSignals(False)

    # ── visibility helpers used by the canvas ─────────────────────────────────

    def _visible_ids(self):
        """Local-graph set, or None for the whole graph."""
        if self._local_depth > 0 and self._selected_id:
            return self._neighbourhood(self._selected_id, self._local_depth)
        return None

    def _neighbourhood(self, node_id: str, depth: int) -> set[str]:
        seen = {node_id}
        frontier = deque([node_id])
        for _ in range(max(1, int(depth))):
            for _ in range(len(frontier)):
                current = frontier.popleft()
                for other in self._adj.get(current) or ():
                    if other not in seen:
                        seen.add(other)
                        frontier.append(other)
            if not frontier:
                break
        return seen

    def _node_visible(self, node: _Node, visible) -> bool:
        if visible is not None and node.id not in visible:
            return False
        if not self._orphans.isChecked() and node.degree == 0:
            return False
        if not self._attach.isChecked() and node.type == "attachment":
            return False
        if self._tag_filter and self._tag_filter not in node.tags:
            return False
        if self._folder_filter and node.folder != self._folder_filter:
            return False
        return True

    def _focus_set(self) -> set[str]:
        focus: set[str] = set()
        for anchor in (self._hover_id, self._selected_id):
            if anchor and anchor in self._by_id:
                focus.add(anchor)
                focus.update(self._adj.get(anchor) or ())
        return focus

    def _radius(self, node: _Node) -> float:
        base = _BASE_RADIUS.get(node.type, 6.5)
        return (base + min(9.0, math.sqrt(max(0, node.degree)) * 1.15)) \
            * self._node_scale

    def _colour(self, node: _Node) -> str:
        mode = self._group_mode
        if mode == "FOLDER" and node.type not in ("tag", "folder"):
            return _GROUP_RAMP[_hash_pick(node.folder or "vault")]
        if mode == "TAG" and node.type not in ("tag", "folder") and node.tags:
            return _GROUP_RAMP[_hash_pick(node.tags[0])]
        return _TYPE_COLORS.get(node.type, ACCENT)

    # ── selection / preview / open ────────────────────────────────────────────

    def _select(self, node: _Node) -> None:
        self._selected_id = node.id
        self._update_detail(node)
        try:
            self.nodeSelected.emit(self._node_payload(node))
        except Exception:
            pass
        self._wake()
        self._canvas.update()

    def _update_detail(self, node) -> None:
        if node is None:
            self._detail.setText("Select a node for a preview.")
            self._detail.setToolTip("")
            self._open_btn.setEnabled(False)
            return
        meta = f"{node.type} · {node.degree} link(s)"
        if node.folder:
            meta += f" · {node.folder}"
        text = f"{node.label}   [{meta}]"
        if node.body:
            text += f"   —  {node.body[:110]}"
        self._detail.setText(text)
        self._detail.setToolTip(node.body or node.label)
        self._open_btn.setEnabled(bool(node.path))

    def _node_payload(self, node: _Node) -> dict:
        neighbours = sorted(self._adj.get(node.id) or (),
                            key=lambda nid: -self._by_id[nid].degree)
        return {
            "id": node.id, "label": node.label, "type": node.type,
            "path": node.path, "folder": node.folder, "tags": list(node.tags),
            "mtime": node.mtime, "degree": node.degree, "body_head": node.body,
            "connected": [self._by_id[nid].label for nid in neighbours[:8]
                          if nid in self._by_id],
        }

    def _open_selected(self) -> None:
        node = self._by_id.get(self._selected_id)
        if node is not None:
            self._open(node)

    def _open(self, node: _Node) -> None:
        try:
            self.openRequested.emit(self._node_payload(node))
        except Exception:
            pass
        if not node.path:
            self.statusMessage.emit("That node has no note to open.")
            return
        try:
            from urllib.parse import quote
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices
            source = self._source or {}
            root = str(source.get("path") or "")
            target = ""
            if str(source.get("kind")) == "vault" and root:
                name = str(source.get("name") or "")
                rel = node.path.replace("\\", "/")
                if QDesktopServices.openUrl(QUrl(
                        f"obsidian://open?vault={quote(name)}&file={quote(rel)}")):
                    return
                target = str(Path(root) / rel)
            if target and Path(target).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))
                return
            self.statusMessage.emit("The note could not be opened.")
        except Exception as exc:
            self.statusMessage.emit(f"The note could not be opened: {exc}")

    # ── status ────────────────────────────────────────────────────────────────

    def _empty_text(self) -> str:
        if self._error:
            return f"The graph could not be built: {self._error}"
        if str((self._source or {}).get("kind")) == "vault":
            name = str((self._source or {}).get("name") or "the vault")
            return (f"No notes in {name} yet — add a note, or select a "
                    f"different vault.")
        return "No memories to map yet."

    def _status_text(self) -> str:
        source = self._source or {}
        if source.get("kind") == "vault":
            label = f"VAULT: {source.get('name') or source.get('path') or 'vault'}"
        else:
            label = "BRAIN STORE"
        if self._error:
            return f"{label} · ⚠ {self._error}"
        if self._query:
            state = f"{len(self._search_hits)} match(es)"
        elif self._settled:
            state = "settled"
        else:
            state = "laying out…"
        return (f"{label} · {len(self._nodes):,} nodes · "
                f"{len(self._pairs):,} links · {state}")

    def _emit_status(self, force: bool = False) -> None:
        text = self._status_text()
        if not force and text == self._last_status:
            return
        self._last_status = text
        try:
            self._status.setText(text)
            self.statusMessage.emit(text)
        except Exception:
            pass

    def _emit_status_text(self, text: str) -> None:
        try:
            self._status.setText(str(text))
            self.statusMessage.emit(str(text))
        except Exception:
            pass



_MAP_WINDOW = None


def _forget_map_window(*_args) -> None:
    global _MAP_WINDOW
    _MAP_WINDOW = None


def open_neural_map2d(parent=None):
    """Open the 2D neural map as a window — one window, never blocking.

    Every surface that offers "Neural Map" lands here: there is exactly one
    map, and it is the 2D graph. The window is reused while it is open (a
    live map is a view of the vault, not a second copy) and it is *not*
    modal, so the HUD stays usable behind it.
    """
    global _MAP_WINDOW
    try:
        if _MAP_WINDOW is not None and _MAP_WINDOW.isVisible():
            _MAP_WINDOW.raise_()
            _MAP_WINDOW.activateWindow()
            return _MAP_WINDOW
    except Exception:
        _MAP_WINDOW = None

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QDialog, QVBoxLayout

    # This module is PyQt6 while the HUD is PySide6: the two bindings share the
    # Qt libraries but not each other's object types, so a PySide6 parent
    # cannot be adopted by this dialog (it raises and the map never opens).
    # The window is kept alive by _MAP_WINDOW instead.
    dialog = QDialog(None)
    dialog.setWindowTitle("NEURAL MAP  //  2D GRAPH")
    dialog.setMinimumSize(900, 620)
    dialog.setStyleSheet("QDialog{background:#01121d;}")
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(10, 10, 10, 10)
    view = NeuralMap2D(dialog)
    try:
        from ui_neural_map import build_graph
        view.set_loader(build_graph)
    except Exception:
        pass
    layout.addWidget(view, 1)
    try:
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.destroyed.connect(_forget_map_window)
    except Exception:
        pass
    try:
        view.reload_async()
    except Exception:
        pass
    dialog.show()
    try:
        # The map follows memory while it is open: a fact stored from any
        # conversation shows up without closing and reopening the window, on
        # the original store and the vault alike.
        refresh = QTimer(dialog)
        refresh.setInterval(15000)
        refresh.timeout.connect(lambda: view.reload_async())
        refresh.start()
        dialog._refresh_timer = refresh
    except Exception:
        pass
    _MAP_WINDOW = dialog
    return dialog
