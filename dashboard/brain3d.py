"""3D JARVIS Brain graph widget (spec §33-§38).

A Qt QWidget that renders the REAL memory graph from memory.manager with:
* A glowing CORE node in the centre.
* Orbit systems (MEMORY, USER, PROJECTS, AI, VISION, VOICE, WEB, TOOLS,
  AUTOMATION, SYSTEM) around it - the fixed orbital architecture (spec §34).
* Actual memory nodes (from BrainMemory.graph()) attached per-category, with
  connection lines and activity highlighting (spec §35-§36).
* Smooth orbital rotation, connection pulses, travelling particles, node
  selection/focus (spec §37-§38).
* Rotate / zoom / pan via mouse (drag, wheel, right-drag).

Selection behaviour (spec §1-§32): the idle field is a subdued grey network
floating in a deep-space environment; selecting a memory colours that bead and
its real neighbourhood, turns the relationship lines inside that neighbourhood
electric blue and sends small glowing comet particles travelling along them in
the relationship's own direction. Everything unrelated fades back to grey, the
camera glides to keep the network in frame, and a HUD-style detail card names
the selected memory or relationship. The appearance of every bead and line is
derived from one explicit state model (NodeState / LinkState) and eased over
time instead of snapping.

Rendering upgrades over the first generation:
* Deep-space environment: parallax starfield with twinkle, drifting nebula
  clouds, vignette (the "video" look — the graph floats in space).
* Links are curved quadratic bezier filaments (force-graph style), not straight
  wires; particles travel along the curve.
* Additive (bloom-style) compositing for every glow pass.
* Selected bead gets staggered expanding pulse rings + a rotating arc halo.
* Active links can carry a second staggered particle on strong relationships.
* Depth fog: far beads and lines fade into the background.
* Gentle idle auto-orbit when the user has not touched the view for a while.
* HUD glass detail card with corner brackets, shadow and accent spine.

Implementation is a painter-based pseudo-3D projection: real depth (z) is
computed and projected, so it does not depend on OpenGL context availability
and cannot crash on machines without GL. It re-renders only on a 16ms timer
and never rebuilds the graph per frame - data is refreshed on an interval.
"""

from __future__ import annotations

import math
import random
import sys
import time as _time
import weakref
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter, QPen,
                         QRadialGradient, QVector3D)
from PyQt6.QtWidgets import QWidget

# The scene draws from the shared token set like every other surface. The import
# is defensive only so this module can still be imported standalone.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    from ui_theme import (
        ACCENT, ACCENT_BRIGHT, ACCENT_DEEP, TEXT as _TOKEN_TEXT,
        TEXT_FAINT, TEXT_BRIGHT, GOLD, GREEN, AMBER, RED, BG,
        qcolor as _token_qcolor, rgba as _token_rgba,
    )
    _THEME_OK = True
except Exception:                                   # pragma: no cover
    ACCENT, ACCENT_BRIGHT, ACCENT_DEEP = "#00d4ff", "#8ceaff", "#007a99"
    _TOKEN_TEXT, TEXT_FAINT, TEXT_BRIGHT = "#daf5ff", "#547f92", "#f0fdff"
    GOLD, GREEN, AMBER, RED, BG = "#f4b24a", "#5df0b6", "#ffc773", "#ff6f8b", "#02060c"
    _THEME_OK = False

    def _token_qcolor(value, alpha=None):
        c = QColor(str(value))
        if alpha is not None:
            c.setAlpha(int(alpha))
        return c

    def _token_rgba(color, alpha):
        c = QColor(str(color))
        return f"rgba({c.red()}, {c.green()}, {c.blue()}, {int(alpha)})"

# The scene paints with the values bound above, so a live theme switch has to
# refresh them (`ui_theme.bind_module_globals`).
if _THEME_OK:
    try:  # pragma: no cover - registration only
        from ui_theme import bind_module_globals as _bind_theme_globals
        _bind_theme_globals(globals(), (
            ("_TOKEN_TEXT", "TEXT"),
            "ACCENT", "ACCENT_BRIGHT", "ACCENT_DEEP", "TEXT_FAINT", "TEXT_BRIGHT",
            "GOLD", "GREEN", "AMBER", "RED", "BG",
        ))
    except Exception:
        pass


# Lifecycle budget: one animation tick per orb, one particle pool per orb.
FRAME_MS = 16                 # ~60fps repaint while the scene is really moving
# The orb is never completely frozen — a slow cinematic auto-orbit plus the
# ambient bead drift always run — so an unconditional 60fps repaint meant a
# permanently busy CPU for a scene whose only motion is 0.035 rad/s. The tick
# therefore runs at one of two rates: FRAME_MS while the user is interacting or
# anything is genuinely in motion (drag, particles, flare, a bead snapping home,
# the camera easing), and IDLE_FRAME_MS when only that slow orbit is running.
# ~24fps is indistinguishable at this speed and costs well under half the CPU.
IDLE_FRAME_MS = 42            # ~24fps for the cinematic idle scene
DATA_REFRESH_S = 8.0          # graph re-read interval, folded into the tick
PARTICLE_CAP = 160            # hard cap; oldest particles are dropped first
# Motion thresholds for the frame budget. The idle orbit settles at a steady
# lag of IDLE_ORBIT_SPEED * CAMERA_INTERACTION_TAU (~0.0023 rad), so anything
# under these is the cinematic drift, not interaction.
BUSY_ROT_LAG = 0.05           # rad
BUSY_ZOOM_LAG = 0.02
BUSY_ORB_SPEED = 0.02         # world units/s

# Interaction geometry, in world units (the orbit ring radius is 0.9).
ATTRACT_RADIUS = 0.55
SNAP_RADIUS = 0.16
ATTRACT_STRENGTH = 3.2        # exponential approach rate at point blank
SNAP_STRENGTH = 7.0
MAX_ORB_SPEED = 2.6           # world units per second — no violent acceleration
MERGE_EPSILON = 1e-3          # "attached" tolerance; smaller than one pixel
HOVER_HIT_SLOP = 12.0         # px
ORB_HIT_SLOP = 11.0           # px
DRAG_START_PX = 3.0

# ── Camera interaction ───────────────────────────────────────────────────────
ORBIT_SPEED = 0.2             # rad/s — how fast the unattached beads drift
CAMERA_INTERACTION_TAU = 0.065    # fast/eased response while the pointer drives
ROTATION_SENSITIVITY = 0.010      # radians per mouse pixel
ORBIT_HOLD_S = 0.25           # drift stays frozen this long after a gesture
IDLE_ORBIT_DELAY = 8.0        # s of pointer silence before the slow auto-orbit
IDLE_ORBIT_SPEED = 0.035      # rad/s — cinematic, never nauseating

# ── Bead sizing (the 3d-force-graph model) ───────────────────────────────────
NODE_REL_SIZE = 4.0
NODE_RADIUS_MIN = 3.5
NODE_RADIUS_MAX = 15.0

# ── Deep-space environment ───────────────────────────────────────────────────
STAR_COUNT = 150              # parallax starfield density
STAR_LAYERS = 3               # depth layers, each with its own parallax factor
NEBULA_DRIFT = 0.008          # rad/s — nebula rotation, barely perceptible

# ── Selected-network appearance (spec §1-§24) ────────────────────────────────
MUTED_GRAY = QColor("#6b7f8a")        # holographic grey muted beads fade to
MUTED_SATURATION = 0.14               # how much category hue survives muting
MUTED_BRIGHTNESS = 0.42               # muted beads stay visible, never black
CONNECTION_BLUE = QColor("#3fb8ff")   # electric blue for an active relationship
CONNECTION_BLUE_HOT = QColor("#cdf2ff")
LINK_ALPHA_IDLE = 26                  # faint filament when nothing is selected
LINK_ALPHA_ACTIVE = 168               # a relationship inside the selection
LINK_WIDTH_ACTIVE = 1.7
LINK_GLOW_ALPHA = 54                  # soft outer glow under an active line
LINK_HIT_SLOP = 6.0                   # px — how near a click must be to a line
LINK_CURVATURE = 0.14                 # max bezier bow, as a fraction of length
CONNECTION_PARTICLE_SPEED = 0.4       # link traversals per second (§12)
CONNECTION_PARTICLE_CAP = 48          # most simultaneous link particles (§13)
CONNECTION_PARTICLE_FADE = 0.12       # u-fraction faded at each end (§11)
CONNECTION_PARTICLE_TRAIL = 4         # trail samples behind each head (§23)
LINK_PARTICLE_READY = 0.45            # eased amplitude a link needs to emit
SECOND_PARTICLE_WEIGHT = 0.75         # strong links carry a staggered 2nd head
TRANSITION_TAU = 0.11                 # ~95% of a state change inside ~330 ms
CAMERA_TAU = 0.22                     # camera ease time constant (§19)
CAMERA_ZOOM_MIN, CAMERA_ZOOM_MAX = 0.72, 1.50
CAMERA_FILL = 0.32                    # share of the panel the network should fill
CAMERA_PAN_LIMIT = 0.55               # × the shorter side — one focus, one glide
SECONDARY_DEGREE_MAX_LINKS = 24       # auto second-degree below this degree (§5)
CROWDED_LINKS = 320                   # above this, particles thin out (§24)
CARD_W = 236.0
CARD_MARGIN = 12.0
CARD_MAX_CONNECTIONS = 5


# ── Selection state model (spec §26) ─────────────────────────────────────────
class NodeState:
    """Visual state of one memory bead."""

    DEFAULT = "default"      # nothing selected — the whole field is subdued
    SELECTED = "selected"    # the memory the user picked — strongest emphasis
    CONNECTED = "connected"  # directly related to the selection
    SECONDARY = "secondary"  # one hop further out (optional, §5 / §21)
    MUTED = "muted"          # unrelated: grey, low glow, still visible


class LinkState:
    """Visual state of one relationship line."""

    DEFAULT = "default"      # no selection — faint desaturated filament
    ACTIVE = "active"        # touches the selection — electric blue + glow
    SECONDARY = "secondary"  # inside the selected network but not primary
    MUTED = "muted"          # unrelated — very faint thin grey


# Per-state appearance targets: (hue kept, brightness, glow).
_NODE_LOOK = {
    NodeState.SELECTED:  (1.00, 1.00, 1.00),
    NodeState.CONNECTED: (1.00, 0.92, 0.62),
    NodeState.SECONDARY: (0.48, 0.70, 0.26),
    NodeState.MUTED:     (MUTED_SATURATION, MUTED_BRIGHTNESS, 0.10),
}
_NODE_LOOK[NodeState.DEFAULT] = _NODE_LOOK[NodeState.MUTED]

# Per-state line brightness targets.
_LINK_LOOK = {
    LinkState.ACTIVE:    1.00,
    LinkState.SECONDARY: 0.55,
    LinkState.DEFAULT:   0.22,
    LinkState.MUTED:     0.20,
}

# Registry of live orbs so non-GUI code can emit memory events without any Qt
# knowledge: ``emit_memory_event("save")`` is safe from any thread. Weak refs so
# a destroyed panel is never kept alive by this module-level list.
_BRAIN_ORBS: "list[weakref.ReferenceType[BrainGraph3D]]" = []


def _live_orbs():
    """Live orb instances, pruning dead weak refs as it goes."""
    alive = []
    for ref in list(_BRAIN_ORBS):
        orb = ref()
        if orb is None:
            try:
                _BRAIN_ORBS.remove(ref)
            except ValueError:
                pass
            continue
        alive.append(orb)
    return alive


def orb_registry_size() -> int:
    """Live registered orbs — used by the teardown test."""
    return len(_live_orbs())


def emit_memory_event(kind: str, count: int = 1) -> None:
    """Publish a memory activity event to every live orb (any thread)."""
    for orb in _live_orbs():
        try:
            orb.note_memory_event(kind, count)
        except Exception:
            pass

# ── Theme (shared tokens) ─────────────────────────────────────────────────────
_PRI = _token_qcolor(ACCENT)
_PRI_DIM = _token_qcolor(ACCENT_DEEP)
_GLOW = _token_qcolor(_token_rgba(ACCENT, 30))
_TEXT = _token_qcolor(_TOKEN_TEXT)
_TEXT_FAINT_Q = _token_qcolor(TEXT_FAINT)
_TEXT_BRIGHT_Q = _token_qcolor(TEXT_BRIGHT)
_BG_Q = _token_qcolor(BG)
_ACCENT_HEX = ACCENT


_CATEGORY_COLORS = {
    "CORE": _token_qcolor(ACCENT),          # the anchor uses the HUD accent
    "PROFILE": QColor("#ffd166"),
    "PREFERENCES": QColor("#06d6a0"),
    "PROJECTS": QColor("#4cc9f0"),
    "CONVERSATIONS": QColor("#a29bfe"),
    "EPISODES": QColor("#f9a8d4"),
    "TASKS": QColor("#fec260"),
    "EVENTS": QColor("#95a5a6"),
    "TOOLS": QColor("#f72585"),
    "SOLUTIONS": QColor("#06d6a0"),
    "ERRORS": QColor("#ee6c4d"),
    "WORKFLOWS": QColor("#3a86ff"),
    "ENVIRONMENT": QColor("#8ac926"),
    "CONTEXT": QColor("#5c95a8"),
    "RELATIONSHIPS": QColor("#ff9e00"),
    "DEFAULT": QColor("#7fc8cf"),
    "PERSON": QColor("#ffd166"),
    "PROJECT": QColor("#4cc9f0"),
    "TASK": QColor("#fec260"),
    "EVENT": QColor("#95a5a6"),
    "DECISION": QColor("#c77dff"),
    "PREFERENCE": QColor("#06d6a0"),
    "TECHNOLOGY": QColor("#3a86ff"),
    "FILE": QColor("#8ac926"),
    "CONVERSATION": QColor("#a29bfe"),
    "FACT": QColor("#7fc8cf"),
}

_ORBITS = ["MEMORY", "USER", "PROJECTS", "AI", "VISION", "VOICE",
           "WEB", "TOOLS", "AUTOMATION", "SYSTEM"]

_ORBIT_CAT = {
    "MEMORY": ("PROFILE", "PREFERENCES", "CONVERSATIONS", "EPISODES"),
    "PROJECTS": ("PROJECTS", "TASKS"),
    "VISION": ("ENVIRONMENT",),
    "TOOLS": ("TOOLS", "WORKFLOWS", "SOLUTIONS", "ERRORS"),
    "CONTEXT": ("CONTEXT", "RELATIONSHIPS", "EVENTS"),
}


def _project(x: float, y: float, z: float, cx: float, cy: float,
             fov: float = 1.6, scale: float = 1.0) -> QPointF:
    """Perspective projection of a 3D point onto the 2D canvas."""
    d = fov + z
    k = scale * fov / d if d > 0 else scale
    return QPointF(cx + x * k, cy + y * k)


def _cat_color(category: str) -> QColor:
    """The stored base colour of a memory type (spec §2). Never destructively
    rewritten — muting is applied at draw time."""
    return _CATEGORY_COLORS.get(str(category).upper(), _CATEGORY_COLORS["DEFAULT"])


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend between two colours (t=0 → a, t=1 → b)."""
    t = max(0.0, min(1.0, float(t)))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _muted_color(base: QColor, sat: float, bright: float) -> QColor:
    """Base category colour → desaturated holographic grey, dimmed (§1, §6)."""
    col = _mix(MUTED_GRAY, base, max(0.0, min(1.0, sat)))
    k = 0.34 + 0.66 * max(0.0, min(1.0, bright))
    return QColor(int(col.red() * k), int(col.green() * k), int(col.blue() * k))


def _node_look_target(state: str) -> dict:
    """The appearance a bead settles at in a given state."""
    sat, bright, glow = _NODE_LOOK.get(state, _NODE_LOOK[NodeState.MUTED])
    return {"sat": sat, "bright": bright, "glow": glow}


def _stable_unit(*parts) -> float:
    """Deterministic 0-1 value for a key: no RNG, identical across runs."""
    hsh = 0
    for ch in ":".join(str(p) for p in parts):
        hsh = (hsh * 1103515245 + ord(ch)) & 0x7FFFFFFF
    return (hsh % 9973) / 9973.0


def _weight_of(link: dict) -> float:
    """A relationship's real strength, clamped — used to prioritise (§13)."""
    try:
        return max(0.05, min(1.0, float(link.get("weight", 0.5))))
    except Exception:
        return 0.5


def _segment_distance(pt: QPointF, a: QPointF, b: QPointF) -> float:
    """Shortest distance from a point to a line segment (link hit-testing)."""
    vx, vy = b.x() - a.x(), b.y() - a.y()
    wx, wy = pt.x() - a.x(), pt.y() - a.y()
    vv = vx * vx + vy * vy
    if vv <= 1e-9:
        return math.hypot(wx, wy)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
    return math.hypot(wx - t * vx, wy - t * vy)


def _quad_bezier(p0: QPointF, c: QPointF, p1: QPointF, u: float) -> QPointF:
    """Point at parameter u on a quadratic bezier — the link filament curve."""
    v = 1.0 - u
    return QPointF(v * v * p0.x() + 2.0 * v * u * c.x() + u * u * p1.x(),
                   v * v * p0.y() + 2.0 * v * u * c.y() + u * u * p1.y())


def _curve_control(p0: QPointF, p1: QPointF, seed: float) -> QPointF:
    """Stable perpendicular bow for one link: force-graph style curvature.

    ``seed`` is a deterministic per-link value so the same relationship always
    bows the same way, and the field of filaments reads as organic, not noisy.
    """
    mx, my = (p0.x() + p1.x()) * 0.5, (p0.y() + p1.y()) * 0.5
    dx, dy = p1.x() - p0.x(), p1.y() - p0.y()
    length = math.hypot(dx, dy)
    if length <= 1e-6:
        return QPointF(mx, my)
    # perpendicular unit vector, bow magnitude from the link's own seed
    bow = (seed * 2.0 - 1.0) * LINK_CURVATURE * length
    return QPointF(mx - (dy / length) * bow, my + (dx / length) * bow)


def _fmt_ts(value) -> str:
    """A stored epoch timestamp as a short local date, or "" when unset."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    try:
        return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts))
    except Exception:
        return ""


def _card_font(size: float, bold: bool = False) -> QFont:
    f = QFont()
    f.setPointSizeF(float(size))
    f.setBold(bool(bold))
    return f


class BrainGraph3D(QWidget):
    """Interactive 3D orbital brain graph over the real memory store."""

    # Emitted when the user selects a memory bead (dict, empty when cleared).
    nodeFocused = pyqtSignal(dict)
    # Emitted when the user selects a relationship (dict, empty when cleared).
    linkFocused = pyqtSignal(dict)

    def __init__(self, parent=None, get_graph=None):
        super().__init__(parent)
        self.setMinimumSize(420, 320)
        # ── the clock ────────────────────────────────────────────────────
        self._t0 = _time.perf_counter()
        self._t = 0.0            # seconds since t0 — cosmetic phase only
        self._dt = 0.0           # seconds since the previous tick
        self._last_tick = self._t0
        self._last_refresh = 0.0
        self.setMouseTracking(True)

        self._get_graph = get_graph

        # ── the view (camera) ─────────────────────────────────────────────
        self._rot_x = -0.35      # radians
        self._rot_y = 0.6
        self._zoom = 1.0
        self._rot_target_x = self._rot_x
        self._rot_target_y = self._rot_y
        self._zoom_target = self._zoom
        self._pan = QPointF(0, 0)
        self._orbit_phase = 0.0
        self._orbit_hold_until = 0.0   # drift frozen until this perf time
        self._last_interaction = _time.perf_counter()  # for idle auto-orbit

        self._drag = None        # (last pos, mode) — camera drag only
        self._focus = None       # selected node id
        self._hover = None

        # ── the selected network (spec §3-§8, §26-§29) ───────────────────
        self._selection_kind = None    # None | "node" | "link"
        self._focus_link = None        # selected relationship (link index)
        self._focus_ids: set = set()
        self._neighbors: set = set()
        self._secondary: set = set()
        self._active_links: set = set()
        self._secondary_links: set = set()
        self._hover_link = None
        self._secondary_degree = None
        self._particle_speed = CONNECTION_PARTICLE_SPEED
        self._card = None
        self._card_rows_cache = None
        self._nodes_by_id: dict = {}
        self._degrees: dict = {}
        self._vis: dict = {}           # node id -> {"sat","bright","glow"}
        self._link_vis: dict = {}      # link index -> brightness
        self._press: QPointF | None = None
        self._moved = False
        self._cam_restore = None
        self._cam_target = None

        # ── object positions ──────────────────────────────────────────────
        self._anchor_world = QVector3D(0.0, 0.0, 0.0)
        self._anchor_screen = QPointF(0.0, 0.0)
        self._world: dict = {}
        self._orb_states: dict = {}
        self._orb_drag = None
        self._orb_grab = None

        self._graph = {"nodes": [], "links": []}
        # pane state + per-frame projection cache
        self._offsets: dict = {}
        self._query = ""
        self._query_ids: set = set()
        self._cat_filter = ""
        self._stats: dict = {}
        self._lay: list = []
        self._lay_ids: dict = {}
        self._link_rows: list = []
        self._lay_w = 0.0
        self._lay_h = 0.0
        self._frame_key = None       # projection cache key (camera-aware)
        self._world_version = 0      # bumped whenever any world position moves
        self._geo_key = None
        self._slots_cache: dict = {}
        self._spread_cache: dict = {}
        # deep-space environment caches (rebuilt on resize)
        self._stars: list = []         # (unit_x, unit_y, z, size, phase, speed)
        self._stars_key = None
        self._nebulae = (              # (fx, fy, radius_frac, color, alpha)
            (0.24, 0.30, 0.55, QColor("#073048"), 26),
            (0.78, 0.68, 0.50, QColor("#141437"), 22),
            (0.55, 0.16, 0.42, QColor("#0a2a20"), 16),
        )

        self._anim = QTimer(self)
        try:
            self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        except Exception:
            pass
        self._anim.timeout.connect(self._tick)
        # Whoever owns the window decides visibility; start at the idle budget
        # and let the first real interaction promote it to 60fps.
        self._frame_ms = IDLE_FRAME_MS
        self._anim.start(IDLE_FRAME_MS)

        self._particles: deque = deque(maxlen=PARTICLE_CAP)
        self._activity = "idle"
        self._flare_until = 0.0
        self._events: deque = deque(maxlen=32)
        self._alive = True
        _BRAIN_ORBS.append(weakref.ref(self))
        self.destroyed.connect(lambda *_: self._unregister())
        self.refresh()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _unregister(self) -> None:
        """Drop this orb from the module registry (idempotent)."""
        self._alive = False
        for ref in list(_BRAIN_ORBS):
            if ref() is self or ref() is None:
                try:
                    _BRAIN_ORBS.remove(ref)
                except ValueError:
                    pass

    def closeEvent(self, event) -> None:
        self.stop_animating()
        self._unregister()
        super().closeEvent(event)

    def hideEvent(self, event) -> None:
        self.stop_animating()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.start_animating()

    def stop_animating(self) -> None:
        try:
            self._anim.stop()
        except Exception:
            pass

    def start_animating(self) -> None:
        try:
            self._last_tick = _time.perf_counter()
            if not self._anim.isActive():
                self._anim.start(getattr(self, "_frame_ms", IDLE_FRAME_MS))
        except Exception:
            pass

    def _interactive_now(self) -> bool:
        """True when the scene needs the full frame rate right now.

        Deliberately conservative: anything that is visibly moving — a drag, the
        camera easing, a bead being pulled home, particles, an event flare — or
        anything the user is touching counts. Only the ambient drift does not.
        """
        if self._drag is not None or self._orb_drag is not None:
            return True
        if self._cam_target is not None:
            return True
        try:
            if self._flare_until > _time.perf_counter():
                return True
        except Exception:
            pass
        if self._particles:
            return True
        for state in self._orb_states.values():
            if state.get("dragging") or state.get("snapping"):
                return True
            try:
                if float(state.get("velocity") or 0.0) > BUSY_ORB_SPEED:
                    return True
            except (TypeError, ValueError):
                continue
        if abs(self._rot_target_x - self._rot_x) > BUSY_ROT_LAG:
            return True
        if abs(self._rot_target_y - self._rot_y) > BUSY_ROT_LAG:
            return True
        if abs(self._zoom_target - self._zoom) > BUSY_ZOOM_LAG:
            return True
        return False

    def _apply_frame_budget(self) -> None:
        """Settle on the right tick rate for the scene's current motion.

        setInterval is only touched when the bucket actually changes, so the
        common case costs one comparison per frame.
        """
        want = FRAME_MS if self._interactive_now() else IDLE_FRAME_MS
        if want == self._frame_ms:
            return
        self._frame_ms = want
        try:
            self._anim.setInterval(want)
        except Exception:
            pass

    def _tick(self) -> None:
        """The single animation tick: advance the clock, drain events, step the
        orb interaction, refresh the graph when due, then repaint once."""
        now = _time.perf_counter()
        self._dt = max(0.0, min(0.25, now - self._last_tick))
        self._last_tick = now
        self._t = now - self._t0
        if not self._orbit_paused():
            self._orbit_phase += self._dt * ORBIT_SPEED
        self._step_idle_orbit(now)
        self._drain_memory_events()
        self._step_orb_interaction(self._dt)
        self._step_visuals(self._dt)
        self._step_camera_interaction(self._dt)
        self._step_camera_focus(self._dt)
        if self._get_graph is not None and (self._t - self._last_refresh) >= DATA_REFRESH_S:
            self._last_refresh = self._t
            self.refresh(repaint=False)
        self._apply_frame_budget()
        self.update()

    def _step_idle_orbit(self, now: float) -> None:
        """Cinematic slow auto-orbit after the pointer has been silent.

        Any real gesture cancels it instantly (it also suppresses itself while
        a selection is being examined, so the network stays put for reading).
        """
        if self._drag is not None or self._orb_drag is not None:
            return
        if self._selection_kind is not None:
            return
        if self._cam_target is not None:
            return
        if (now - self._last_interaction) < IDLE_ORBIT_DELAY:
            return
        self._rot_target_y += IDLE_ORBIT_SPEED * self._dt

    # ── activity feed from the Brain ──────────────────────────────────────────

    def set_activity(self, activity: str) -> None:
        self._activity = activity or "idle"
        if activity in ("thinking", "speaking", "learning", "remembering"):
            self._spawn_particles(6)

    def note_memory_event(self, kind: str, count: int = 1) -> None:
        """Queue a live memory event (save/recall/forget/learn). Any thread."""
        kind = str(kind or "recall").lower()
        if kind not in ("save", "recall", "forget", "learn"):
            kind = "recall"
        try:
            self._events.append((kind, max(1, min(8, int(count)))))
        except Exception:
            pass

    def _drain_memory_events(self) -> None:
        if not self._events:
            return
        n = 0
        try:
            while True:
                kind, count = self._events.popleft()
                n += count
                if kind in ("save", "learn"):
                    self._activity = "remembering"
        except IndexError:
            pass
        if n:
            self._spawn_particles(min(14, 3 * n))
            self._activity = self._activity if self._activity != "idle" else "learning"
            self._flare_until = _time.perf_counter() + 1.2

    def _spawn_particle(self) -> dict:
        return {
            "born": self._t,
            "angle": random.uniform(0, math.tau),
            "radius": random.uniform(0.35, 0.9),
            "speed": random.uniform(0.4, 1.0),
            "life": random.uniform(1.0, 2.0),
            "hue": random.uniform(0.0, 0.6),
        }

    def _spawn_particles(self, count: int) -> None:
        for _ in range(max(0, int(count))):
            self._particles.append(self._spawn_particle())

    def particle_count(self) -> int:
        self._prune_particles()
        return len(self._particles)

    def _prune_particles(self) -> None:
        cutoff = self._t
        while self._particles:
            part = self._particles[0]
            if (cutoff - part["born"]) < part["life"]:
                break
            self._particles.popleft()

    # ── data ──────────────────────────────────────────────────────────────────

    def refresh(self, repaint: bool = True) -> None:
        try:
            if self._get_graph is not None:
                self._graph = self._get_graph() or {"nodes": [], "links": []}
            elif self._selection_kind is not None:
                self._set_selection(None, None)
        except Exception:
            self._graph = {"nodes": [], "links": []}
        self._prune_caches()
        self._world_version += 1     # graph data changed → invalidate projection cache
        self._nodes_by_id = {n.get("id"): n for n in (self._graph.get("nodes") or [])}
        self._refresh_degrees()
        self._lay, self._lay_ids, self._link_rows = [], {}, []
        self._lay_w = self._lay_h = 0.0
        if self._selection_kind == "node" and self._focus not in self._nodes_by_id:
            self._set_selection(None, None)
        elif self._selection_kind == "link" and self._focus_link is not None \
                and self._focus_link >= len(self._graph.get("links") or []):
            self._set_selection(None, None)
        self._recompute_selection()
        self._card = self._card_model()
        if repaint:
            self.update()

    def _prune_caches(self) -> None:
        live = {n.get("id") for n in self._graph.get("nodes", [])}
        for cache in (self._world, self._offsets, self._orb_states, self._vis):
            for key in [k for k in cache if k not in live]:
                cache.pop(key, None)
        keep = range(len(self._graph.get("links") or []))
        for key in [k for k in self._link_vis if k not in keep]:
            self._link_vis.pop(key, None)

    # ── input (spec §38) ──────────────────────────────────────────────────────

    def mousePressEvent(self, e) -> None:
        pos = e.position()
        self._press = QPointF(pos)
        self._moved = False
        self._last_interaction = _time.perf_counter()
        if e.button() == Qt.MouseButton.LeftButton:
            alt = bool(e.modifiers() & Qt.KeyboardModifier.AltModifier)
            orb = self._pick_orb(pos) if alt else None
            if orb is not None:
                self._orb_drag = orb.get("id")
                self._orb_grab = (pos, QVector3D(self._world_of(orb)))
                state = self._state_of(orb.get("id"))
                state.update({"dragging": True, "merged": False, "placed": True})
                self._hold_orbit()
                self._set_selection("node", orb.get("id"))
                e.accept()
                return
            self._drag = (pos, "rotate")
            self._cam_target = None
            self._hold_orbit()
        elif e.button() == Qt.MouseButton.RightButton:
            self._drag = (pos, "pan")
            self._cam_target = None
            self._hold_orbit()
        elif e.button() == Qt.MouseButton.MiddleButton:
            self._pick(pos)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._orb_drag is not None:
            self._moved = True
            self.drag_orb_to(pos)
            e.accept()
            return
        if self._drag is not None:
            last, mode = self._drag
            dx, dy = pos.x() - last.x(), pos.y() - last.y()
            if mode == "rotate":
                self._rot_target_y += dx * ROTATION_SENSITIVITY
                self._rot_target_x += dy * ROTATION_SENSITIVITY
                self._rot_target_x = max(-1.4, min(1.4, self._rot_target_x))
            elif mode == "pan":
                self._pan += QPointF(dx, dy)
            self._drag = (pos, mode)
            if self._press is not None and \
                    (pos - self._press).manhattanLength() > DRAG_START_PX:
                self._moved = True
            self.update()
        else:
            self._hover = self._pick_hover(pos)
            self._hover_link = None if self._hover is not None else self._pick_link(pos)
            self.setCursor(Qt.CursorShape.PointingHandCursor
                           if (self._hover is not None or self._hover_link is not None)
                           else Qt.CursorShape.ArrowCursor)
            self.update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = None
        self._hover_link = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._orb_drag is not None:
            node_id = self._orb_drag
            self._orb_drag = None
            self._orb_grab = None
            state = self._state_of(node_id)
            state["dragging"] = False
            state["placed"] = True
            if self._distance_to_anchor(node_id) <= SNAP_RADIUS:
                state["snapping"] = True
            else:
                state["snapping"] = False
            self.update()
        else:
            was_drag, self._drag = self._drag, None
            if was_drag is not None:
                self._hold_orbit()
            if was_drag is not None and was_drag[1] == "rotate" and not self._moved:
                self._pick(e.position())
        self._drag = None
        self._press = None
        self._moved = False
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e) -> None:
        delta = e.angleDelta().y() or 0
        self._last_interaction = _time.perf_counter()
        factor = math.exp(delta / 1200.0)
        self._zoom_target = max(0.40, min(3.0, self._zoom_target * factor))
        self._cam_target = None
        self._hold_orbit()
        self.update()
        e.accept()

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._pick(e.position())
        super().mouseDoubleClickEvent(e)

    # ── public controls (driven by the Memory Core pane) ──────────────────

    def visible_ids(self) -> list:
        return [row[1].get("id") for row in self._lay]

    def set_query(self, text: str) -> None:
        """Highlight memories matching a search string; dim everything else."""
        self._query = str(text or "").strip().lower()
        hits: set = set()
        if self._query:
            for n in self._graph.get("nodes", []):
                label = str(n.get("label", "")).lower()
                cat = str(n.get("category", "")).lower()
                if self._query in label or self._query in cat:
                    hits.add(n.get("id"))
        self._query_ids = hits
        self.update()

    def set_category(self, category: str) -> None:
        cat = str(category or "All").strip()
        self._cat_filter = "" if cat.lower() in ("", "all") else cat
        self.update()

    def set_stats(self, stats: dict | None) -> None:
        self._stats = dict(stats or {})
        self.update()

    def focus_category(self, category: str) -> int:
        self.set_category(category)
        want = self._cat_filter.lower()
        if not want:
            return len(self._graph.get("nodes", []))
        return sum(1 for n in self._graph.get("nodes", [])
                   if str(n.get("category", "")).lower() == want)

    def focused_node(self) -> dict | None:
        if self._focus is None:
            return None
        for n in self._graph.get("nodes", []):
            if n.get("id") == self._focus:
                return n
        return None

    # ── memory field layout ───────────────────────────────────────────────

    def _node_base(self, node: dict, slots: dict, spread: dict) -> QVector3D:
        cat = node.get("category", "DEFAULT")
        if cat == "CORE":
            return QVector3D(0.0, 0.0, 0.0)
        anchor = slots.get(cat)
        if anchor is None:
            hsh = 0
            for ch in f"anchor:{cat}":
                hsh = (hsh * 1103515245 + ord(ch)) & 0x7FFFFFFF
            ang = (hsh % 3600) / 3600.0 * math.tau
            rr = 0.55 + ((hsh >> 12) % 1000) / 1000.0 * 0.45
            zz = (((hsh >> 22) % 1600) / 1000.0) - 0.8
            anchor = QVector3D(rr * math.cos(ang),
                               rr * math.sin(ang) * 0.6,
                               zz)
        return anchor + self._local_offset(node, spread.get(cat, 1.0))

    def _local_offset(self, node: dict, spread: float = 1.0) -> QVector3D:
        key = (node.get("id"), round(float(spread), 2))
        cached = self._offsets.get(key)
        if cached is not None:
            return cached
        hsh = 0
        for ch in f"{node.get('id')}:{node.get('category', '')}":
            hsh = (hsh * 1103515245 + ord(ch)) & 0x7FFFFFFF
        u = ((hsh % 997) + 0.5) / 997.0
        theta = math.tau * 0.618033988749895 * ((hsh >> 7) % 997)
        z = u * 2.0 - 1.0
        ring = math.sqrt(max(0.0, 1.0 - z * z))
        r = (0.20 + 0.26 * u) * spread
        off = QVector3D(r * ring * math.cos(theta),
                        r * ring * math.sin(theta) * 0.78,
                        z * r * 0.90)
        self._offsets[key] = off
        return off

    def _refresh_degrees(self) -> None:
        degrees: dict = {}
        for link in self._graph.get("links") or []:
            for end in (link.get("s"), link.get("t")):
                if end is not None:
                    degrees[end] = degrees.get(end, 0) + 1
        self._degrees = degrees

    def _node_val(self, node: dict) -> float:
        try:
            imp = float(node.get("importance", 0.5))
        except Exception:
            imp = 0.5
        if imp > 1.0:
            imp /= 5.0
        imp = max(0.25, min(1.0, imp))
        degree = self._degrees.get(node.get("id"), 0)
        return max(1.0, (1.0 + degree) * (0.55 + 0.95 * imp))

    def _node_radius(self, node: dict, depth: float) -> float:
        radius = NODE_REL_SIZE * self._node_val(node) ** (1.0 / 3.0)
        radius = max(NODE_RADIUS_MIN, min(NODE_RADIUS_MAX, radius))
        return radius * (0.70 + 0.55 * depth) * self._zoom

    def _layout(self, w: float, h: float) -> list:
        """Project every node once per frame: [[z, node, point, r, depth]].

        Sorted back-to-front, so size, alpha and draw order all read correctly
        and links/picking reuse one projection instead of recomputing it.
        """
        nodes = self._graph.get("nodes", [])
        if not nodes:
            self._lay_w, self._lay_h = w, h
            self._link_rows = []
            return []
        self._sync_geometry()
        cx = w / 2 + self._pan.x()
        cy = h / 2 + self._pan.y()
        scale = min(w, h) / 360.0 * self._zoom
        self._anchor_screen = QPointF(cx, cy)
        raw = []
        for n in nodes:
            if str(n.get("category", "")).upper() == "CORE":
                raw.append([0.0, n, QPointF(cx, cy)])
                continue
            base = self._world_of(n)
            state = self._orb_states.get(n.get("id")) or {}
            spin = 0.0 if state.get("placed") else self._orbit_phase
            x, y, z = _rotate(base.x(), base.y(), base.z(),
                              self._rot_x, self._rot_y, spin)
            raw.append([z, n, _project(x * 60, y * 60, z * 60, cx, cy,
                                       scale=scale)])
        zs = [r[0] for r in raw]
        zmin, zmax = min(zs), max(zs)
        span = (zmax - zmin) or 1.0
        out = []
        for z, n, pt in raw:
            depth = (z - zmin) / span
            out.append([z, n, pt, self._node_radius(n, depth), depth])
        out.sort(key=lambda r: r[0])
        return out

    def _compute_layout(self, w: float, h: float) -> list:
        """Project the whole graph ONCE and publish it to the frame cache."""
        self._lay = self._layout(w, h)
        self._lay_w, self._lay_h = w, h
        self._lay_ids = {}
        for row in self._lay:
            self._lay_ids[row[1].get("id")] = row
        self._sync_link_rows()
        self._frame_key = self._frame_key_now(w, h)
        return self._lay

    def _sync_link_rows(self) -> None:
        """Cache this frame's relationship geometry once.

        Each row carries the REAL link endpoints plus a stable bezier control
        point — the filament curves and the particles riding it share the exact
        same geometry, so motion and line never separate.
        """
        rows = []
        for i, link in enumerate(self._graph.get("links") or []):
            r0 = self._lay_ids.get(link.get("s"))
            r1 = self._lay_ids.get(link.get("t"))
            if r0 is None or r1 is None:
                continue
            seed = _stable_unit("c", link.get("s"), link.get("t"), link.get("type"))
            rows.append({
                "i": i, "s": link.get("s"), "t": link.get("t"),
                "type": str(link.get("type") or ""),
                "weight": _weight_of(link),
                "p0": r0[2], "p1": r1[2],
                "ctrl": _curve_control(r0[2], r1[2], seed),
                "z": (r0[0] + r1[0]) * 0.5,
                "phase": _stable_unit("p", link.get("s"), link.get("t"), link.get("type")),
                "two_way": bool(link.get("bidirectional") or link.get("two_way")),
            })
        self._link_rows = rows

    def _frame_key_now(self, w: float, h: float):
        """Cache key for this frame's projection.

        Covers EVERY input the projection depends on: panel size, the RENDERED
        camera (rotation/zoom/pan — which the easing steps rewrite every
        animation frame), the ambient orbit phase, and a version counter for
        world positions (orb drags, attraction/snap steps, data refresh).

        The old key only checked the panel size, so the cached projection went
        stale the instant the view moved: the scene kept repainting the same
        frozen frame while dragging/zooming and only re-projected when an
        unrelated refresh happened to clear the cache — which read as "the
        camera only updates when I release the mouse".
        """
        return (round(w, 1), round(h, 1),
                round(self._rot_x, 4), round(self._rot_y, 4),
                round(self._zoom, 4),
                round(self._pan.x(), 2), round(self._pan.y(), 2),
                round(self._orbit_phase, 4),
                self._world_version, len(self._graph.get("nodes") or []))

    def _ensure_frame(self) -> list:
        w, h = float(self.width()), float(self.height())
        key = self._frame_key_now(w, h)
        if not self._lay or key != self._frame_key:
            return self._compute_layout(w, h)
        return self._lay

    def _node_pos(self, node: dict, w: float, h: float) -> QPointF | None:
        row = self._lay_ids.get(node.get("id"))
        if row is not None and self._lay and abs(self.width() - w) < 1.5 \
                and abs(self.height() - h) < 1.5:
            return row[2]
        for _z, n, pt, _r, _d in self._ensure_frame():
            if n is node or n.get("id") == node.get("id"):
                return pt
        return None

    def _emphasis(self, node: dict) -> float:
        cat = str(node.get("category", ""))
        if cat == "CORE":
            return 1.0
        factor = 1.0
        if self._cat_filter and cat.lower() != self._cat_filter.lower():
            factor *= 0.18
        if self._query_ids and node.get("id") not in self._query_ids:
            factor *= 0.22
        return factor

    def _is_match(self, node: dict) -> bool:
        return bool(self._query_ids) and node.get("id") in self._query_ids

    # ── selection (spec §3, §15, §16, §18, §26-§29) ───────────────────────

    def _selection_ids(self, links: list) -> set:
        if self._selection_kind == "node" and self._focus is not None:
            return {self._focus}
        if self._selection_kind == "link" and self._focus_link is not None \
                and 0 <= self._focus_link < len(links):
            link = links[self._focus_link]
            return {i for i in (link.get("s"), link.get("t")) if i is not None}
        return set()

    def _degree_enabled(self, degree: int) -> bool:
        if self._secondary_degree is not None:
            return bool(self._secondary_degree)
        return degree <= SECONDARY_DEGREE_MAX_LINKS

    def _recompute_selection(self) -> None:
        """Rebuild the highlighted neighbourhood from real graph data (§27)."""
        self._neighbors, self._secondary = set(), set()
        self._active_links, self._secondary_links = set(), set()
        links = list(self._graph.get("links") or [])
        focus_ids = self._selection_ids(links)
        self._focus_ids = focus_ids
        if not focus_ids:
            return
        for i, link in enumerate(links):
            s, t = link.get("s"), link.get("t")
            if s not in focus_ids and t not in focus_ids:
                continue
            self._active_links.add(i)
            for end in (s, t):
                if end is not None and end not in focus_ids:
                    self._neighbors.add(end)
        if not self._degree_enabled(len(self._neighbors)):
            return
        level1 = set(self._neighbors)
        for i, link in enumerate(links):
            if i in self._active_links:
                continue
            s, t = link.get("s"), link.get("t")
            if s not in level1 and t not in level1:
                continue
            self._secondary_links.add(i)
            for end in (s, t):
                if end is not None and end not in focus_ids and end not in level1:
                    self._secondary.add(end)

    def _node_state(self, node: dict) -> str:
        nid = node.get("id")
        if self._selection_kind is None:
            return NodeState.DEFAULT
        if self._selection_kind == "node" and nid == self._focus:
            return NodeState.SELECTED
        if nid in self._focus_ids or nid in self._neighbors:
            return NodeState.CONNECTED
        if nid in self._secondary:
            return NodeState.SECONDARY
        return NodeState.MUTED

    def _link_state(self, index: int) -> str:
        if self._selection_kind is None:
            return LinkState.DEFAULT
        if index in self._active_links:
            return LinkState.ACTIVE
        if index in self._secondary_links:
            return LinkState.SECONDARY
        return LinkState.MUTED

    def _vis_of(self, node: dict) -> dict:
        nid = node.get("id")
        row = self._vis.get(nid)
        if row is None:
            row = _node_look_target(self._node_state(node))
            self._vis[nid] = row
        return row

    def _link_amp(self, index: int) -> float:
        amp = self._link_vis.get(index)
        if amp is None:
            amp = _LINK_LOOK.get(self._link_state(index), 0.2)
            self._link_vis[index] = amp
        return amp

    def _step_visuals(self, dt: float) -> None:
        """Cross-fade every bead and line toward its state's appearance (§17)."""
        if dt <= 0:
            return
        a = 1.0 - math.exp(-dt / TRANSITION_TAU)
        for node in self._graph.get("nodes") or []:
            target = _node_look_target(self._node_state(node))
            cur = self._vis.get(node.get("id"))
            if cur is None:
                self._vis[node.get("id")] = target
                continue
            for key in ("sat", "bright", "glow"):
                cur[key] += (target[key] - cur[key]) * a
        for i in range(len(self._graph.get("links") or [])):
            target = _LINK_LOOK.get(self._link_state(i), 0.2)
            cur = self._link_vis.get(i)
            self._link_vis[i] = target if cur is None else cur + (target - cur) * a

    def _set_selection(self, kind, target) -> None:
        """Install a selection, recompute its network, refocus and report it."""
        if kind == "node":
            self._focus, self._focus_link = target, None
        elif kind == "link":
            self._focus, self._focus_link = None, target
        else:
            kind, self._focus, self._focus_link = None, None, None
        self._selection_kind = kind
        self._last_interaction = _time.perf_counter()   # pause idle auto-orbit
        self._recompute_selection()
        self._card = self._card_model()
        if kind is None:
            self._release_camera()
        else:
            self._focus_camera()
        try:
            if kind == "link":
                self.linkFocused.emit(self._link_info(self._focus_link))
            else:
                self.nodeFocused.emit(self.focused_node() or {})
        except Exception:
            pass
        self.update()

    def clear_selection(self) -> None:
        """Deselect, returning the scene to its calm idle state (§18)."""
        self._set_selection(None, None)

    def selected_node_id(self):
        return self._focus if self._selection_kind == "node" else None

    def selected_link_id(self):
        return self._focus_link if self._selection_kind == "link" else None

    def connected_node_ids(self) -> set:
        return set(self._neighbors)

    def highlighted_node_ids(self) -> set:
        return set(self._focus_ids) | set(self._neighbors)

    def secondary_node_ids(self) -> set:
        return set(self._secondary)

    def active_link_ids(self) -> set:
        return set(self._active_links)

    def hovered_node_id(self):
        return None if self._hover is None else self._hover.get("id")

    def hovered_link_id(self):
        return self._hover_link

    def node_visual(self, node_id) -> dict:
        return dict(self._vis.get(node_id) or {})

    def link_visual(self, index) -> float:
        return float(self._link_amp(index))

    def set_particle_speed(self, speed: float) -> None:
        try:
            self._particle_speed = max(0.0, min(4.0, float(speed)))
        except Exception:
            pass
        self.update()

    def set_secondary_degree(self, enabled) -> None:
        self._secondary_degree = None if enabled is None else bool(enabled)
        self._recompute_selection()
        self._card = self._card_model()
        self.update()

    def _label_of(self, nid) -> str:
        node = self._nodes_by_id.get(nid)
        text = str((node or {}).get("label") or nid or "?")
        return text[:26]

    def _link_info(self, index) -> dict:
        links = self._graph.get("links") or []
        if index is None or not (0 <= index < len(links)):
            return {}
        link = links[index]
        return {"s": link.get("s"), "t": link.get("t"),
                "type": link.get("type") or "related",
                "weight": link.get("weight", 1.0),
                "source": self._label_of(link.get("s")),
                "target": self._label_of(link.get("t"))}

    def _neighbours_of(self, nid) -> list:
        names = []
        for link in self._graph.get("links") or []:
            s, t = link.get("s"), link.get("t")
            other = t if s == nid else (s if t == nid else None)
            if other is None:
                continue
            label = self._label_of(other)
            if label and label not in names:
                names.append(label)
        return names[:CARD_MAX_CONNECTIONS]

    def _pick(self, pos: QPointF) -> None:
        """Select a memory bead, a relationship, or nothing at all (§18)."""
        best, dist = None, 1e9
        for _z, n, pt, radius, _depth in reversed(self._ensure_frame()):
            d = (pt - pos).manhattanLength()
            hit = 22.0 if n.get("category") == "CORE" else max(ORB_HIT_SLOP, radius * 1.3)
            if d < dist and d < hit:
                dist, best = d, n
        if best is not None:
            self._set_selection("node", best.get("id"))
            return
        index = self._pick_link(pos)
        if index is not None:
            self._set_selection("link", index)
        else:
            self._set_selection(None, None)

    def _pick_link(self, pos: QPointF) -> int | None:
        best, best_d = None, LINK_HIT_SLOP
        for row in self._link_rows:
            d = _segment_distance(pos, row["p0"], row["p1"])
            if d <= best_d:
                best, best_d = row["i"], d
        return best

    def _pick_hover(self, pos: QPointF) -> dict | None:
        for _z, n, pt, radius, _depth in reversed(self._ensure_frame()):
            if (pt - pos).manhattanLength() < max(HOVER_HIT_SLOP, radius * 1.15):
                return n
        return None

    def pick_orb(self, pos: QPointF) -> dict | None:
        return self._pick_orb(pos)

    def _pick_orb(self, pos: QPointF) -> dict | None:
        best, best_d = None, 1e9
        for _z, n, pt, radius, _depth in reversed(self._ensure_frame()):
            if str(n.get("category", "")).upper() == "CORE":
                continue
            d = (pt - pos).manhattanLength()
            if d < best_d and d <= max(ORB_HIT_SLOP, radius * 1.3):
                best, best_d = n, d
        return best

    # ── interaction state ──────────────────────────────────────────────────

    def anchor_world(self) -> QVector3D:
        return QVector3D(self._anchor_world)

    def anchor_point(self) -> QPointF:
        return QPointF(self._anchor_screen)

    def orb_states(self) -> dict:
        return {k: dict(v) for k, v in self._orb_states.items()}

    def world_position(self, node_id):
        pos = self._world.get(node_id)
        return None if pos is None else QVector3D(pos)

    def dragging_orb(self):
        return self._orb_drag

    def orb_screen_positions(self) -> dict:
        return {row[1].get("id"): QPointF(row[2]) for row in self._lay}

    def _state_of(self, node_id) -> dict:
        return self._orb_states.setdefault(node_id, {
            "placed": False, "dragging": False, "snapping": False,
            "merged": False, "velocity": 0.0,
        })

    def _distance_to_anchor(self, node_id) -> float:
        pos = self._world.get(node_id)
        return 1e9 if pos is None else pos.length()

    # ── freezing the field while the camera is driven ────────────────────────

    def _hold_orbit(self) -> None:
        self._orbit_hold_until = _time.perf_counter() + ORBIT_HOLD_S

    def _orbit_paused(self) -> bool:
        return (self._drag is not None or self._orb_drag is not None
                or self._orbit_hold_until > _time.perf_counter())

    # ── camera focus (spec §18, §19) ──────────────────────────────────────────

    def _focus_camera(self) -> None:
        rows = {row[1].get("id"): row[2] for row in self._ensure_frame()}
        ids = set(self._focus_ids) | self._neighbors | self._secondary
        pts = [rows[i] for i in ids if i in rows]
        if not pts:
            return
        cx, cy = self.width() / 2.0, self.height() / 2.0
        gx = sum(p.x() for p in pts) / len(pts)
        gy = sum(p.y() for p in pts) / len(pts)
        if self._cam_restore is None:
            self._cam_restore = (self._zoom, QPointF(self._pan))
        dx, dy = cx - gx, cy - gy
        limit = CAMERA_PAN_LIMIT * min(self.width(), self.height())
        mag = math.hypot(dx, dy)
        if mag > limit > 0:
            dx, dy = dx * limit / mag, dy * limit / mag
        zoom = self._zoom
        span = max((math.hypot(p.x() - gx, p.y() - gy) for p in pts), default=0.0)
        if span > 1.0:
            want = CAMERA_FILL * min(self.width(), self.height())
            zoom = max(CAMERA_ZOOM_MIN,
                       min(CAMERA_ZOOM_MAX, self._zoom * want / span))
        self._cam_target = (zoom, QPointF(self._pan.x() + dx, self._pan.y() + dy))
        self._zoom_target = zoom

    def _release_camera(self) -> None:
        if self._cam_restore is not None:
            self._cam_target = (self._cam_restore[0], QPointF(self._cam_restore[1]))
            self._zoom_target = self._cam_restore[0]
            self._cam_restore = None
        else:
            self._cam_target = None

    def _step_camera_interaction(self, dt: float) -> None:
        if dt <= 0:
            return
        a = 1.0 - math.exp(-dt / CAMERA_INTERACTION_TAU)
        self._rot_x += (self._rot_target_x - self._rot_x) * a
        self._rot_y += (self._rot_target_y - self._rot_y) * a
        self._zoom += (self._zoom_target - self._zoom) * a

        if abs(self._rot_target_x - self._rot_x) < 1e-5:
            self._rot_x = self._rot_target_x
        if abs(self._rot_target_y - self._rot_y) < 1e-5:
            self._rot_y = self._rot_target_y
        if abs(self._zoom_target - self._zoom) < 1e-5:
            self._zoom = self._zoom_target

    def _step_camera_focus(self, dt: float) -> None:
        if self._cam_target is None or dt <= 0 or self._orb_drag is not None:
            return
        zoom, pan = self._cam_target
        a = 1.0 - math.exp(-dt / CAMERA_TAU)
        self._zoom += (zoom - self._zoom) * a
        self._pan = QPointF(self._pan.x() + (pan.x() - self._pan.x()) * a,
                            self._pan.y() + (pan.y() - self._pan.y()) * a)
        if abs(zoom - self._zoom) < 1e-3 and (pan - self._pan).manhattanLength() < 0.4:
            self._zoom = zoom
            self._pan = QPointF(pan)
            self._cam_target = None

    def _view_scale(self) -> float:
        return min(float(self.width()), float(self.height())) / 360.0 * self._zoom

    def _rotated(self, world: QVector3D, spin: float) -> QVector3D:
        return QVector3D(*_rotate(world.x(), world.y(), world.z(),
                                  self._rot_x, self._rot_y, spin))

    def _screen_delta_to_world(self, delta: QPointF, at_world: QVector3D) -> QVector3D:
        rot = self._rotated(at_world, 0.0)
        fov, z = 1.6, rot.z() * 60.0
        d = fov + z
        k = (self._view_scale() * fov / d if d > 0 else self._view_scale()) * 60.0
        k = k if abs(k) > 1e-6 else 1e-6
        wx, wy, _wz = _unrotate(delta.x() / k, delta.y() / k, 0.0,
                                self._rot_x, self._rot_y, 0.0)
        return QVector3D(at_world.x() + wx, at_world.y() + wy, at_world.z())

    def drag_orb_to(self, pos: QPointF) -> None:
        if self._orb_drag is None or self._orb_grab is None:
            return
        node_id = self._orb_drag
        start_pos, start_world = self._orb_grab
        delta = QPointF(pos.x() - start_pos.x(), pos.y() - start_pos.y())
        self._world[node_id] = self._screen_delta_to_world(delta, start_world)
        self._world_version += 1     # world moved → invalidate projection cache
        state = self._state_of(node_id)
        state.update({"placed": True, "dragging": True, "merged": False})
        self.update()

    def _step_orb_interaction(self, dt: float) -> None:
        if dt <= 0:
            return
        for node_id, state in list(self._orb_states.items()):
            if state.get("dragging") or state.get("merged"):
                continue
            pos = self._world.get(node_id)
            if pos is None:
                continue
            dist = pos.length()
            if state.get("snapping"):
                rate = SNAP_STRENGTH
            elif dist <= ATTRACT_RADIUS:
                ramp = 1.0 - (dist / ATTRACT_RADIUS)
                if ramp <= 0.0:
                    continue
                rate = ATTRACT_STRENGTH * (ramp ** 1.5)
            else:
                continue
            alpha = 1.0 - math.exp(-rate * dt)
            target = QVector3D(0.0, 0.0, 0.0)
            new = QVector3D(pos.x() + (target.x() - pos.x()) * alpha,
                            pos.y() + (target.y() - pos.y()) * alpha,
                            pos.z() + (target.z() - pos.z()) * alpha)
            step = (new - pos).length()
            cap = MAX_ORB_SPEED * dt
            if step > cap > 0.0:
                new = QVector3D(pos.x() + (new.x() - pos.x()) * (cap / step),
                                pos.y() + (new.y() - pos.y()) * (cap / step),
                                pos.z() + (new.z() - pos.z()) * (cap / step))
                step = cap
            self._world[node_id] = new
            self._world_version += 1     # world moved → invalidate projection cache
            state["velocity"] = step / dt
            if new.length() <= MERGE_EPSILON:
                self._world[node_id] = QVector3D(0.0, 0.0, 0.0)
                self._world_version += 1
                state["velocity"] = 0.0
                state["snapping"] = False
                if not state.get("merged"):
                    state["merged"] = True

    # ── orbit slot layout (spec §34) ──────────────────────────────────────────

    def _sync_geometry(self) -> None:
        w, h = float(self.width()), float(self.height())
        nodes = self._graph.get("nodes", [])
        key = (round(w, 1), round(h, 1), len(nodes))
        if key == self._geo_key:
            return
        self._geo_key = key
        self._slots_cache = self._slots(w, h)
        counts: dict = {}
        for n in nodes:
            cat = n.get("category", "DEFAULT")
            counts[cat] = counts.get(cat, 0) + 1
        self._spread_cache = {cat: min(1.9, 0.85 + 0.10 * max(0, cnt - 1))
                              for cat, cnt in counts.items()}
        self._rebuild_stars(w, h)

    def _rebuild_stars(self, w: float, h: float) -> None:
        """Deterministic parallax starfield for the current panel size.

        Each star lives on a unit sphere shell with its own depth; depth drives
        parallax (far stars barely react to rotation), size and twinkle phase,
        so the background has real 3D structure instead of a flat dot pattern.
        """
        key = (round(w, 1), round(h, 1), STAR_COUNT)
        if key == self._stars_key:
            return
        self._stars_key = key
        stars = []
        span = math.hypot(w, h) * 0.62          # stars live just outside the field
        for k in range(STAR_COUNT):
            u = _stable_unit("star-u", k)
            v = _stable_unit("star-v", k)
            ang = u * math.tau
            # bias stars toward the rim so the graph zone stays clean
            rr = span * (0.55 + 0.45 * v)
            z = (_stable_unit("star-z", k) * 2.0 - 1.0) * 0.9
            size = 0.5 + 1.7 * _stable_unit("star-s", k) ** 2
            phase = _stable_unit("star-p", k) * math.tau
            speed = 0.4 + 2.2 * _stable_unit("star-w", k)
            warm = _stable_unit("star-c", k) > 0.82   # a few amber stars
            stars.append((rr * math.cos(ang), rr * math.sin(ang) * 0.82,
                          z, size, phase, speed, warm))
        self._stars = stars

    def _world_of(self, node: dict) -> QVector3D:
        node_id = node.get("id")
        cached = self._world.get(node_id)
        if cached is not None:
            return cached
        self._sync_geometry()
        pos = self._node_base(node, self._slots_cache, self._spread_cache)
        self._world[node_id] = pos
        return pos

    def _slots(self, w: float, h: float) -> dict[str, QVector3D]:
        slots: dict[str, QVector3D] = {}
        n = len(_ORBITS)
        for i, orbit in enumerate(_ORBITS):
            angle = i / n * math.tau
            radius = 0.9
            slots[orbit] = QVector3D(
                radius * math.cos(angle),
                radius * math.sin(angle) * 0.6,
                math.sin(i * 1.7) * 0.25,
            )
        for family, cats in _ORBIT_CAT.items():
            base = slots.get(family)
            if base is None:
                continue
            for j, cat in enumerate(cats):
                off = QVector3D(math.cos(j * 2.1) * 0.25,
                                math.sin(j * 2.1) * 0.18,
                                (j - len(cats) / 2) * 0.3)
                slots[cat] = base + off
        return slots

    # ── detail card model (spec §16, §20) ─────────────────────────────────────

    def _card_model(self) -> dict | None:
        if self._selection_kind == "node":
            node = self.focused_node()
            if not node:
                return None
            stats = []
            imp = node.get("importance")
            conf = node.get("confidence")
            if isinstance(imp, (int, float)):
                stats.append(("Importance", f"{float(imp):.2f}"))
            if isinstance(conf, (int, float)):
                stats.append(("Confidence", f"{float(conf):.2f}"))
            created = _fmt_ts(node.get("created_at"))
            updated = _fmt_ts(node.get("updated_at"))
            if created:
                stats.append(("Created", created))
            if updated:
                stats.append(("Updated", updated))
            return {
                "kind": "memory",
                "title": str(node.get("label") or "(untitled memory)")[:46],
                "colour": _cat_color(node.get("category", "DEFAULT")),
                "category": str(node.get("category") or "MEMORY").upper(),
                "body": str(node.get("content") or node.get("label") or "")[:240],
                "connections": self._neighbours_of(node.get("id")),
                "stats": stats[:4],
            }
        if self._selection_kind == "link":
            info = self._link_info(self._focus_link)
            if not info:
                return None
            return {
                "kind": "relationship",
                "title": "RELATIONSHIP",
                "colour": QColor(CONNECTION_BLUE),
                "category": str(info["type"]).upper(),
                "body": f"{info['source']}\n↓\n{info['target']}",
                "connections": [],
                "stats": [("Type", str(info["type"])),
                          ("Source", info["source"]),
                          ("Target", info["target"])],
            }
        return None

    @staticmethod
    def _card_rows(card: dict) -> list:
        rows = [("title", card["title"]), ("category", card["category"])]
        if card["body"]:
            rows.append(("body", card["body"]))
        if card["connections"]:
            rows.append(("label", "Connections"))
            rows.extend(("bullet", name) for name in card["connections"])
        for label, value in card["stats"]:
            rows.append(("stat", f"{label}|{value}"))
        return rows

    def _card_layout(self, card: dict, width: float) -> list:
        key = (id(card), round(float(width), 1))
        if self._card_rows_cache is not None and self._card_rows_cache[0] == key:
            return self._card_rows_cache[1]
        pad = 12.0
        text_w = max(40.0, width - pad * 2)
        fm = QFontMetrics(_card_font(7.5))
        out = []
        for role, text in self._card_rows(card):
            if role == "body":
                box = fm.boundingRect(QRect(0, 0, int(text_w), 400),
                                      int(Qt.TextFlag.TextWordWrap), text)
                height = float(min(70, max(fm.height(), box.height())))
            elif role == "title":
                height = pad + 6.0
            elif role == "category":
                height = fm.height() + 4.0
            elif role == "label":
                height = fm.height() + 6.0
            else:
                height = fm.height() + 3.0
            out.append((role, text, height))
        self._card_rows_cache = (key, out)
        return out

    # ── painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        cx, cy = w / 2 + self._pan.x(), h / 2 + self._pan.y()

        self._draw_background(p, cx, cy, w, h)
        self._draw_particles(p, cx, cy)

        layout = self._ensure_frame()

        self._draw_links(p, w, h)
        self._draw_link_particles(p)
        self._draw_nodes(p, layout)

        self._draw_activity_sweep(p, cx, cy, w, h)
        self._draw_link_hover(p, w, h)
        self._draw_detail_card(p, w, h)

        p.end()

    # ── deep-space environment ────────────────────────────────────────────────

    def _draw_background(self, p: QPainter, cx: float, cy: float,
                         w: float, h: float) -> None:
        """Deep-space scene: gradient base, drifting nebulae, parallax
        starfield with twinkle, vignette. This is what makes the graph feel
        like it floats in a holographic void instead of sitting on a canvas."""
        # 1 — vertical space gradient (near-black with a cold blue floor)
        g = QRadialGradient(QPointF(cx, cy * 0.9), max(w, h) * 0.75)
        g.setColorAt(0.0, QColor("#041019"))
        g.setColorAt(0.55, QColor("#020a12"))
        g.setColorAt(1.0, QColor("#010409"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(QRectF(0.0, 0.0, w, h))

        # 2 — nebula clouds: huge, nearly-still, additive
        drift = self._t * NEBULA_DRIFT
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for fx, fy, rfrac, col, alpha in self._nebulae:
            nx = cx + math.cos(drift + fx * 6.0) * 14.0 + (fx - 0.5) * w * 0.7
            ny = cy + math.sin(drift * 0.8 + fy * 6.0) * 10.0 + (fy - 0.5) * h * 0.7
            rr = max(w, h) * rfrac
            ng = QRadialGradient(QPointF(nx, ny), rr)
            acol = QColor(col)
            acol.setAlpha(alpha)
            ng.setColorAt(0.0, acol)
            ng.setColorAt(1.0, QColor(col.red(), col.green(), col.blue(), 0))
            p.setBrush(QBrush(ng))
            p.drawEllipse(QPointF(nx, ny), rr, rr)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 3 — parallax starfield with twinkle (additive for a real glow)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for sx, sy, z, size, phase, speed, warm in self._stars:
            # depth-weighted parallax: far stars follow the camera slowly
            parallax = 0.25 + 0.75 * (1.0 - abs(z)) * 0.5
            px = cx + sx + math.sin(self._orbit_phase * parallax + z) * 9.0
            py = cy + sy + math.cos(self._orbit_phase * parallax * 0.8 + z) * 6.0
            tw = 0.5 + 0.5 * math.sin(self._t * speed + phase)
            base = QColor("#ffcf9e") if warm else QColor("#bfe9ff")
            base.setAlpha(int(26 + 96 * tw * (0.45 + 0.55 * (1.0 - abs(z)))))
            p.setBrush(base)
            p.drawEllipse(QPointF(px, py), size * (0.7 + 0.5 * tw), size * (0.7 + 0.5 * tw))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 4 — vignette
        vg = QRadialGradient(QPointF(cx, cy), max(w, h) * 0.72)
        vg.setColorAt(0.0, QColor(0, 0, 0, 0))
        vg.setColorAt(1.0, QColor(0, 0, 0, 120))
        p.setBrush(QBrush(vg))
        p.drawRect(QRectF(0.0, 0.0, w, h))

    def _draw_particles(self, p: QPainter, cx: float, cy: float) -> None:
        self._prune_particles()
        span = min(self.width(), self.height()) * 0.4
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for part in self._particles:
            age = self._t - part["born"]
            remain = max(0.0, 1.0 - age / part["life"])
            if remain <= 0.0:
                continue
            a = part["angle"] + part["speed"] * age
            r = part["radius"]
            x = r * math.cos(a) * span
            y = r * math.sin(a) * span
            z = math.sin(a * 3) * 20
            pt = _project(x, y, z, cx, cy, scale=1.0 * self._zoom)
            col = _token_qcolor(ACCENT, int(120 * remain))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            size = 2.4 * remain * self._zoom
            p.drawEllipse(pt, size, size)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    # ── links ─────────────────────────────────────────────────────────────────

    def _draw_links(self, p: QPainter, w: float, h: float) -> None:
        """Curved relationship filaments, drawn from the state model.

        Nothing selected: every filament is a faint desaturated grey curve
        (spec §7). With a selection the links inside the network are electric
        blue with a soft additive outer glow; everything else stays behind.
        The brightness is the eased value, so links fade instead of snapping.
        """
        if not self._link_rows:
            return
        rows = sorted(self._link_rows,
                      key=lambda r: (1 if r["i"] in self._active_links else 0, r["z"]))
        searching = self._selection_kind is None and bool(self._query_ids)
        path = __import__("PyQt6.QtGui", fromlist=["QPainterPath"]).QPainterPath
        for row in rows:
            i, p0, p1, ctrl = row["i"], row["p0"], row["p1"], row["ctrl"]
            state = self._link_state(i)
            amp = self._link_amp(i)
            if state == LinkState.ACTIVE:
                colour, alpha, width = CONNECTION_BLUE, int(LINK_ALPHA_ACTIVE * amp), LINK_WIDTH_ACTIVE
            elif state == LinkState.SECONDARY:
                colour, alpha, width = CONNECTION_BLUE, int(96 * amp), 1.0
            elif searching and (row["s"] in self._query_ids or row["t"] in self._query_ids):
                colour, alpha, width = CONNECTION_BLUE, 74, 0.9
            else:
                colour = MUTED_GRAY
                alpha = int(LINK_ALPHA_IDLE * (0.55 + 0.45 * amp))
                width = 0.55
            if alpha <= 2:
                continue
            curve = path(p0)
            curve.quadTo(ctrl, p1)
            if state in (LinkState.ACTIVE, LinkState.SECONDARY):
                # additive outer glow under the core filament (§8)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                glow = QColor(colour)
                glow.setAlpha(max(0, min(255, int(LINK_GLOW_ALPHA * amp))))
                pen = QPen(glow, max(1.4, width * 3.4))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(curve)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            core = QColor(colour)
            core.setAlpha(max(0, min(255, alpha)))
            pen = QPen(core, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(curve)
            if i in (self._hover_link, self._focus_link):
                hot = QColor(CONNECTION_BLUE_HOT)
                hot.setAlpha(int(170 * max(amp, 0.45)))
                p.setPen(QPen(hot, max(0.8, width * 0.7)))
                p.drawPath(curve)

    # ── travelling connection particles (§9-§14) ──────────────────────────────

    def _draw_link_particles(self, p: QPainter) -> None:
        """Small glowing comets travelling the active blue filaments.

        A particle is *derived* from the clock and the link's own bezier, so it
        is frame-rate independent, it travels in the relationship's real
        direction, and it loops with no visible jump. Nothing is emitted until a
        node is selected — that is what keeps the idle graph calm (§29).
        """
        pool = self._particle_links()
        if not pool:
            return
        active = [row for row in self._link_rows
                  if self._link_state(row["i"]) == LinkState.ACTIVE]
        scale = min(1.0, len(pool) / float(max(1, len(active))))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for row in pool:
            amp = self._link_amp(row["i"])
            hot = row["i"] in (self._hover_link, self._focus_link)
            speed = self._particle_speed * (1.35 if hot else 1.0)

            def emit(u, bright):
                value = bright * scale * self._particle_fade(u)
                if value > 0.02:
                    self._draw_link_particle(p, row, u, value)

            emit(((self._t * speed) + row["phase"]) % 1.0,
                 amp * (1.25 if hot else 1.0))
            if row["weight"] >= SECOND_PARTICLE_WEIGHT:
                # strong relationships carry a second, staggered signal
                emit(((self._t * speed) + row["phase"] + 0.5) % 1.0, amp * 0.6)
            if row["two_way"]:
                reverse = ((self._t * speed * 0.85) + row["phase"] + 0.5) % 1.0
                emit(1.0 - reverse, amp * 0.75)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    def _particle_links(self) -> list:
        live = [row for row in self._link_rows
                if self._link_state(row["i"]) == LinkState.ACTIVE
                and self._link_amp(row["i"]) > LINK_PARTICLE_READY]
        if not live:
            return []
        live.sort(key=lambda r: -r["weight"])
        budget = CONNECTION_PARTICLE_CAP
        if len(self._link_rows) > CROWDED_LINKS:
            budget = max(12, CONNECTION_PARTICLE_CAP // 2)
        return live[:budget]

    @staticmethod
    def _particle_fade(u: float) -> float:
        return max(0.0, min(1.0, min(u, 1.0 - u) / CONNECTION_PARTICLE_FADE))

    def _draw_link_particle(self, p: QPainter, row: dict, u: float, bright: float) -> None:
        """One comet head plus its short luminous trail along the curve (§23)."""
        ctrl = row["ctrl"]
        for k in range(CONNECTION_PARTICLE_TRAIL, 0, -1):
            t = 1.0 - k / (CONNECTION_PARTICLE_TRAIL + 1.0)
            alpha = int(150 * bright * t * t)
            if alpha <= 2:
                continue
            trail = QColor(CONNECTION_BLUE_HOT)
            trail.setAlpha(alpha)
            p.setBrush(trail)
            tu = max(0.0, u - k * 0.022)
            pt = _quad_bezier(row["p0"], ctrl, row["p1"], tu)
            size = 0.9 + 1.0 * t
            p.drawEllipse(pt, size, size)
        head = _quad_bezier(row["p0"], ctrl, row["p1"], u)
        g = QRadialGradient(head, 7.0)
        g.setColorAt(0.0, QColor(255, 255, 255, int(235 * bright)))
        g.setColorAt(0.45, QColor(150, 230, 255, int(190 * bright)))
        g.setColorAt(1.0, QColor(60, 180, 255, 0))
        p.setBrush(QBrush(g))
        p.drawEllipse(head, 7.0, 7.0)

    def _draw_link_hover(self, p: QPainter, w: float, h: float) -> None:
        """Name the relationship under the pointer (§15)."""
        index = self._hover_link
        if index is None or self._hover is not None:
            return
        row = next((r for r in self._link_rows if r["i"] == index), None)
        if row is None:
            return
        info = self._link_info(index)
        text = f"{info.get('source', '?')} ──{info.get('type', 'related')}──→ {info.get('target', '?')}"
        p.setFont(_card_font(7.0))
        fm = QFontMetrics(p.font())
        tw = min(float(fm.horizontalAdvance(text)) + 16.0, max(80.0, w - 16.0))
        mid = _quad_bezier(row["p0"], row["ctrl"], row["p1"], 0.5)
        mx, my = mid.x(), mid.y() - 18.0
        x = max(8.0, min(w - tw - 8.0, mx - tw / 2.0))
        y = max(8.0, min(h - 24.0, my))
        rect = QRectF(x, y, tw, 18.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(3, 14, 22, 208))
        p.drawRoundedRect(rect, 5, 5)
        border = QColor(CONNECTION_BLUE)
        border.setAlpha(140)
        p.setPen(QPen(border, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 5, 5)
        p.setPen(QPen(QColor(CONNECTION_BLUE_HOT)))
        p.drawText(rect.adjusted(6, 0, -6, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(text, Qt.TextElideMode.ElideRight, int(tw - 12)))

    # ── HUD detail card (spec §16, §20) ───────────────────────────────────────

    def _draw_detail_card(self, p: QPainter, w: float, h: float) -> None:
        """The selected memory / relationship card: a floating HUD glass panel
        with corner brackets, drop shadow and an accent spine, drawn on the
        opposite side of the panel from the selection."""
        card = self._card
        if not card:
            return
        card_w = max(150.0, min(CARD_W, w - CARD_MARGIN * 2))
        pad = 12.0
        text_w = card_w - pad * 2
        rows = self._card_layout(card, card_w)
        p.setFont(_card_font(7.5))
        fm = QFontMetrics(p.font())
        total = sum(row[2] for row in rows) + pad * 2.0
        y = max(CARD_MARGIN, (h - total) / 2.0)
        x = CARD_MARGIN
        focus_row = self._lay_ids.get(self._focus) if self._selection_kind == "node" else None
        if focus_row is not None and focus_row[2].x() < w / 2.0:
            x = max(CARD_MARGIN, w - CARD_MARGIN - card_w)
        rect = QRectF(x, y, card_w, total)

        # drop shadow
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawRoundedRect(rect.translated(3.0, 4.0), 10, 10)
        # glass panel
        p.setBrush(QColor(4, 14, 23, 222))
        p.drawRoundedRect(rect, 10, 10)
        border = QColor(card["colour"])
        border.setAlpha(110)
        p.setPen(QPen(border, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 10, 10)
        # HUD corner brackets
        accent = QColor(card["colour"])
        accent.setAlpha(220)
        p.setPen(QPen(accent, 1.6))
        bl = 9.0
        for cx0, cy0, sx, sy in ((rect.left(), rect.top(), 1, 1),
                                 (rect.right(), rect.top(), -1, 1),
                                 (rect.left(), rect.bottom(), 1, -1),
                                 (rect.right(), rect.bottom(), -1, -1)):
            p.drawLine(QPointF(cx0 + sx * 1.0, cy0 + sy * 3.0),
                       QPointF(cx0 + sx * 1.0, cy0 + sy * bl))
            p.drawLine(QPointF(cx0 + sx * 1.0, cy0 + sy * 1.0),
                       QPointF(cx0 + sx * bl, cy0 + sy * 1.0))
        # accent spine
        spine = QColor(card["colour"])
        spine.setAlpha(190)
        p.setPen(QPen(spine, 2.0))
        p.drawLine(QPointF(rect.left() + 1.0, rect.top() + 12.0),
                   QPointF(rect.left() + 1.0, rect.bottom() - 12.0))

        cy = y + pad
        for role, text, hh in rows:
            if role == "title":
                p.setFont(_card_font(8.5, True))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(card["colour"]))
                p.drawEllipse(QPointF(x + pad + 4.0, cy + 5.0), 4.0, 4.0)
                p.setPen(QPen(QColor(card["colour"]) if card["kind"] == "memory"
                              else QColor(CONNECTION_BLUE_HOT)))
                p.drawText(QRectF(x + pad + 13.0, cy, text_w - 13.0, 14.0),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                         int(text_w - 14.0)))
            elif role == "category":
                p.setFont(_card_font(6.5, True))
                p.setPen(QPen(_TEXT_FAINT_Q))
                p.drawText(QRectF(x + pad + 13.0, cy, text_w, 12.0),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            elif role == "body":
                p.setFont(_card_font(7.0))
                p.setPen(QPen(_TEXT))
                p.drawText(QRectF(x + pad, cy, text_w, hh),
                           int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
                           text)
            elif role == "label":
                p.setFont(_card_font(6.5, True))
                p.setPen(QPen(_TEXT_BRIGHT_Q))
                p.drawText(QRectF(x + pad, cy, text_w, 12.0),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            elif role == "bullet":
                p.setFont(_card_font(7.0))
                p.setBrush(QColor(CONNECTION_BLUE))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(x + pad + 2.0, cy + 5.0), 1.8, 1.8)
                p.setPen(QPen(_TEXT_FAINT_Q))
                p.drawText(QRectF(x + pad + 9.0, cy, text_w - 9.0, 12.0),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           fm.elidedText(text, Qt.TextElideMode.ElideRight,
                                         int(text_w - 10.0)))
            elif role == "stat":
                label, _, value = text.partition("|")
                p.setFont(_card_font(7.0))
                p.setPen(QPen(_TEXT_FAINT_Q))
                p.drawText(QRectF(x + pad, cy, text_w * 0.6, 12.0),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
                p.setPen(QPen(_TEXT_BRIGHT_Q))
                p.drawText(QRectF(x + pad, cy, text_w, 12.0),
                           Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           fm.elidedText(value, Qt.TextElideMode.ElideRight,
                                         int(text_w * 0.55)))
            cy += hh

    # ── nodes ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _find(nodes: list[dict], nid) -> dict | None:
        for n in nodes:
            if n.get("id") == nid:
                return n
        return None

    def _draw_nodes(self, p: QPainter, layout: list) -> None:
        """Draw every memory as a glass bead, back-to-front.

        Colour comes from the category's stored base colour desaturated by the
        eased state; glow passes composite additively for a bloom feel; depth
        fogs the far beads so the field keeps its 3D structure (§22).
        """
        label_budget = 14
        labels_drawn = 0
        for z, n, pt, r, depth in layout:
            cat = n.get("category", "DEFAULT")
            if cat == "CORE":
                self._draw_core(p, self._anchor_screen)
                continue

            state = self._node_state(n)
            vis = self._vis_of(n)
            is_focus = (state == NodeState.SELECTED)
            is_hover = (self._hover is not None and self._hover.get("id") == n.get("id"))
            is_hit = self._is_match(n)
            lit = state in (NodeState.SELECTED, NodeState.CONNECTED)
            active = bool(n.get("activity"))
            emph = self._emphasis(n)
            if emph < 0.3 and not (is_focus or is_hover or lit):
                p.setPen(Qt.PenStyle.NoPen)
                dot = _muted_color(_cat_color(cat), vis["sat"], vis["bright"])
                dot.setAlpha(52)
                p.setBrush(dot)
                p.drawEllipse(pt, max(1.6, r * 0.34), max(1.6, r * 0.34))
                continue

            col = _muted_color(_cat_color(cat), vis["sat"], vis["bright"])
            if is_hover and state == NodeState.MUTED:
                col = col.lighter(155)
            glow = vis["glow"] * (0.45 + 0.55 * emph)
            alpha_scale = 0.40 + 0.60 * vis["bright"]
            fog = 0.60 + 0.40 * depth          # far beads sink into the dark
            scale_up = 1.0
            if is_focus:
                scale_up = 1.18
            elif is_hover or state == NodeState.CONNECTED:
                scale_up = 1.10
            r *= scale_up

            # halo — additive bloom, two passes for a soft falloff
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            halo = QColor(col)
            halo.setAlpha(max(0, min(255, int((26 + 80 * glow) * fog *
                                              (1.15 if active else 1.0)))))
            rad = r * (2.3 + 0.9 * glow)
            g = QRadialGradient(pt, rad)
            g.setColorAt(0.0, halo)
            g.setColorAt(1.0, QColor(halo.red(), halo.green(), halo.blue(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawEllipse(pt, rad, rad)
            halo2 = QColor(col)
            halo2.setAlpha(max(0, min(255, int(60 * glow * fog))))
            g2 = QRadialGradient(pt, r * 1.5)
            g2.setColorAt(0.0, halo2)
            g2.setColorAt(1.0, QColor(halo2.red(), halo2.green(), halo2.blue(), 0))
            p.setBrush(QBrush(g2))
            p.drawEllipse(pt, r * 1.5, r * 1.5)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            # selected bead: staggered expanding pulse rings (sonar) + arc halo
            if is_focus:
                for k in range(2):
                    rip = (self._t * 0.55 + k * 0.5) % 1.0
                    ring = QColor(col)
                    ring.setAlpha(int(120 * (1.0 - rip)))
                    p.setPen(QPen(ring, 1.3))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    rr = r * (1.35 + 1.15 * rip)
                    p.drawEllipse(pt, rr, rr)
                arc_a = self._t * 1.4
                pen = QPen(QColor(255, 255, 255, 120), 1.1)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawArc(QRectF(pt.x() - r * 1.6, pt.y() - r * 1.6,
                                 r * 3.2, r * 3.2),
                          int(arc_a * 2864.8), 70 * 16)

            # glass body
            body = QRadialGradient(pt.x() - r * 0.35, pt.y() - r * 0.4, r * 1.9)
            body.setColorAt(0.0, QColor(255, 255, 255, int((120 + 115 * vis["bright"]) * fog)))
            body.setColorAt(0.28, col.lighter(150))
            body.setColorAt(1.0, QColor(col.red() // 3, col.green() // 3,
                                       col.blue() // 3, int((120 + 115 * vis["bright"]) * fog)))
            p.setBrush(QBrush(body))
            if is_hit:
                rim = QColor(255, 236, 160, 230)
            else:
                rim = QColor(col.red(), col.green(), col.blue(), int((60 + 100 * glow) * fog))
            p.setPen(QPen(rim, 1.5 if is_hit else 0.8))
            p.drawEllipse(pt, r, r)

            # specular dot
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, int(200 * alpha_scale * fog)))
            p.drawEllipse(QPointF(pt.x() - r * 0.35, pt.y() - r * 0.42), r * 0.26, r * 0.26)

            want_label = is_hover or is_focus or is_hit or (
                lit and labels_drawn < label_budget) or (
                emph > 0.99 and labels_drawn < label_budget and depth > 0.55)
            if want_label:
                labels_drawn += 1
                font = p.font()
                font.setPointSize(7)
                font.setBold(bool(is_focus or is_hit or state == NodeState.CONNECTED))
                p.setFont(font)
                if is_hit:
                    pen = QColor("#ffe9b0")
                elif state == NodeState.SELECTED:
                    pen = _TEXT_BRIGHT_Q
                elif state in (NodeState.CONNECTED, NodeState.SECONDARY):
                    pen = _TEXT
                else:
                    pen = _TEXT_FAINT_Q
                p.setPen(QPen(pen))
                txt = str(n.get("label", ""))[:34]
                p.drawText(QRectF(pt.x() - 70, pt.y() - r - 18, 140, 16),
                           Qt.AlignmentFlag.AlignCenter, txt)
            f2 = p.font()
            f2.setBold(False)
            p.setFont(f2)

    def _draw_activity_sweep(self, p: QPainter, cx: float, cy: float,
                             w: float, h: float) -> None:
        if self._activity in ("idle", "standby", "sleeping", ""):
            return
        R = min(w, h) * 0.47 * self._zoom
        a = (self._t * 0.6) % math.tau
        p.setPen(QPen(QColor(120, 235, 255, 60), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy),
                   QPointF(cx + R * math.cos(a), cy + R * math.sin(a) * 0.55))

    def _draw_core(self, p: QPainter, pt: QPointF) -> None:
        """The JARVIS brain: layered glass bubble with additive bloom."""
        R = 21 * self._zoom
        breathing = 1.0 + 0.05 * math.sin(self._t * 2.1)
        R *= breathing

        flare = _time.perf_counter() < getattr(self, "_flare_until", 0.0)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        aura_alpha = 46 + (18 if self._activity != "idle" else 0) + (34 if flare else 0)
        aura = QRadialGradient(pt, R * 3.4)
        aura.setColorAt(0.0, QColor(0, 212, 255, aura_alpha))
        aura.setColorAt(1.0, QColor(0, 212, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(aura))
        p.drawEllipse(pt, R * (3.4 + (0.5 if flare else 0.0)), R * (3.4 + (0.5 if flare else 0.0)))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        shell = QRadialGradient(pt.x() - R * 0.3, pt.y() - R * 0.35, R * 2.2)
        shell.setColorAt(0.0, QColor(190, 245, 255, 215))
        shell.setColorAt(0.35, QColor(0, 190, 235, 175))
        shell.setColorAt(0.75, QColor(0, 90, 140, 215))
        shell.setColorAt(1.0, QColor(0, 210, 255, 235))
        p.setBrush(QBrush(shell))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(pt, R, R)

        pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._t * 2.6))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        core_g = QRadialGradient(pt, R * 0.8)
        core_g.setColorAt(0.0, QColor(255, 255, 255, int(230 * pulse)))
        core_g.setColorAt(0.4, QColor(120, 235, 255, int(210 * pulse)))
        core_g.setColorAt(1.0, QColor(0, 140, 190, 0))
        p.setBrush(QBrush(core_g))
        p.drawEllipse(pt, R * 0.62, R * 0.62)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        for phase in (0.0, math.pi):
            a0 = self._t * 1.15 + phase
            pen = QPen(QColor(160, 240, 255, 150), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            rect = QRectF(pt.x() - R * 1.55, pt.y() - R * 1.55, R * 3.1, R * 3.1)
            p.drawArc(rect, int(a0 * 2864.8), 55 * 16)
            p.drawArc(rect, int((a0 + math.pi) * 2864.8), 55 * 16)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 215))
        p.drawEllipse(QPointF(pt.x() - R * 0.34, pt.y() - R * 0.42), R * 0.24, R * 0.24)

        if self._activity in ("thinking", "speaking", "learning", "remembering"):
            rip = (self._t * 0.9) % 1.0
            rr = R * (1.3 + 1.1 * rip)
            p.setPen(QPen(QColor(180, 240, 255, int(120 * (1 - rip))), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(pt, rr, rr)


def _unrotate(x: float, y: float, z: float, rx: float, ry: float, spin: float) -> tuple[float, float, float]:
    """Exact inverse of ``_rotate`` (inverse operations in reverse order)."""
    s, c = math.sin(ry), math.cos(ry)
    x, z = x * c - z * s, x * s + z * c
    s, c = math.sin(rx), math.cos(rx)
    y, z = y * c + z * s, -y * s + z * c
    s, c = math.sin(spin), math.cos(spin)
    x, y = x * c + y * s, -x * s + y * c
    return x, y, z


def _rotate(x: float, y: float, z: float, rx: float, ry: float, spin: float) -> tuple[float, float, float]:
    """Apply spin, then pitch (rot_x), then yaw (rot_y)."""
    s, c = math.sin(spin), math.cos(spin)
    x, y = x * c - y * s, x * s + y * c
    s, c = math.sin(rx), math.cos(rx)
    y, z = y * c - z * s, y * s + z * c
    s, c = math.sin(ry), math.cos(ry)
    x, z = x * c + z * s, -x * s + z * c
    return x, y, z


class BrainPanel(QWidget):
    """A floating-panel wrapper: 3D graph + live stats bar, fed from the real
    BrainMemory singleton. Additive to the app - never imported eagerly by ui."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QLabel, QVBoxLayout

        def _graph():
            try:
                from memory.manager import get_brain_memory
                return get_brain_memory().graph()
            except Exception:
                return {"nodes": [], "links": []}

        self._stats = QLabel("BRAIN  //  starting…")
        self._stats.setStyleSheet("color:#5796ad;font:700 7pt 'Exo 2';background:transparent;padding:2px;")
        self._stats.setWordWrap(True)
        self.graph = BrainGraph3D(get_graph=_graph)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.graph, 1)
        lay.addWidget(self._stats)

        self._stats_timer = __import__("PyQt6.QtCore", fromlist=["QTimer"]).QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(1500)
        self._update_stats()

    def _update_stats(self) -> None:
        try:
            from memory.manager import get_brain_memory
            mem = get_brain_memory()
            counts = mem.db.count()
            active = sum(counts.values())
            tasks = len(mem.active_tasks())
            self._stats.setText(
                f"BRAIN  //  {active} memories active · {len(mem._db.list_memories(limit=0) or []) or ''} · "
                f"{tasks} active task(s)"
            )
        except Exception as exc:
            self._stats.setText(f"BRAIN  //  stats unavailable ({exc})")
        self.graph.refresh()

    def set_activity(self, activity: str) -> None:
        self.graph.set_activity(activity)
