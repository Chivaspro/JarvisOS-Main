"""J.A.R.V.I.S. layout element engine.

This module is the *rendering and data* half of the Layout Studio.  It owns:

* the persisted element schema (``SCHEMA_VERSION`` 4) and the migration from the
  older ``kind``/``text``/``color`` dicts written by earlier Layout Studio
  versions, so ``config/ui_layout.json`` files from any previous release keep
  loading unchanged (TXT sections 44 and 45);
* :class:`ElementWidget` -- a real ``QWidget`` that paints its own background,
  gradient, image (every fit mode), per-side borders, element stroke, drop
  shadow, glow, text with a true outline, animation state and a full effect
  stack.  Every property in the schema has a visible effect here; nothing is
  stored without being rendered (TXT section 56);
* caching: :class:`ImageCache` decodes each file once and reuses scaled and
  thumbnail variants, and ``ElementWidget`` rasterises its static appearance
  once per change, so neither mouse movement nor repaints re-decode images
  (TXT section 6);
* the honest subset of styling that is safe to apply to a *runtime* J.A.R.V.I.S.
  widget (QSS generation plus glow/shadow effects) -- see
  :func:`apply_runtime_style` and the module docstring notes at the bottom.

Qt limits one ``QGraphicsEffect`` per widget.  ``ElementWidget`` therefore uses
that single slot for the element glow (or the drop shadow), paints the shadow
into its own layer when both are enabled, and handles opacity in the painter.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTextLayout,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsColorizeEffect,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QWidget,
)

try:  # numpy ships with the project (opencv/numpy in requirements.txt)
    import numpy as _np
except Exception:  # pragma: no cover - numpy is optional at runtime
    _np = None

try:  # Qt SVG is optional; SVG import reports the limitation instead of faking it
    from PyQt6.QtSvg import QSvgRenderer  # type: ignore
except Exception:  # pragma: no cover
    QSvgRenderer = None  # type: ignore

try:
    from ui_theme import (  # type: ignore
        ACCENT,
        ACCENT_BRIGHT,
        BG,
        BG_DEEP,
        BORDER_FAINT,
        BORDER_SOFT,
        CARD,
        FONT_HEAD,
        FONT_LOG,
        FONT_SMALL,
        RADIUS_LG,
        RADIUS_MD,
        RADIUS_SM,
        SURFACE,
        TEXT,
        TEXT_BRIGHT,
        TEXT_DIM,
        TEXT_MID,
        qcolor,
    )
except Exception:  # pragma: no cover - ui_theme always ships with the Studio
    ACCENT = "#00d4ff"
    ACCENT_BRIGHT = "#8ceaff"
    BG = "#02060c"
    BG_DEEP = "#01060b"
    BORDER_FAINT = "rgba(0,212,255,46)"
    BORDER_SOFT = "rgba(0,212,255,70)"
    CARD = "rgba(3,20,30,168)"
    SURFACE = "#04121b"
    TEXT = "#daf5ff"
    TEXT_BRIGHT = "#f0fdff"
    TEXT_MID = "#a6e9ff"
    TEXT_DIM = "#6ba7bb"
    FONT_HEAD = "'Orbitron'"
    FONT_LOG = "'Share Tech Mono'"
    FONT_SMALL = "'Exo 2'"
    RADIUS_SM, RADIUS_MD, RADIUS_LG = 6, 9, 12

    import re as _re

    def qcolor(value, alpha=None):
        """Minimal fallback colour parser (mirrors ui_theme.qcolor)."""
        col = QColor(str(value or "#000000"))
        if not col.isValid():
            col = QColor("#000000")
        if alpha is not None:
            col.setAlpha(max(0, min(255, int(alpha))))
        return col


# The Studio reads these names at paint time, so a live theme switch has to
# refresh the copies bound above (`ui_theme.bind_module_globals`).
try:  # pragma: no cover - registration only
    from ui_theme import bind_module_globals as _bind_theme_globals
    _bind_theme_globals(globals(), (
        "ACCENT", "ACCENT_BRIGHT", "BG", "BG_DEEP", "BORDER_FAINT",
        "BORDER_SOFT", "CARD", "SURFACE", "TEXT", "TEXT_BRIGHT", "TEXT_DIM",
        "TEXT_MID", "RADIUS_LG", "RADIUS_MD", "RADIUS_SM",
    ))
except Exception:
    pass


SCHEMA_VERSION = 4

# ---------------------------------------------------------------------------
# Element vocabulary
# ---------------------------------------------------------------------------
ELEMENT_TYPES = (
    "Text", "Button", "Panel", "Frame", "Input", "CheckBox", "Toggle",
    "ProgressBar", "Indicator", "HUD Card", "Image", "Separator", "Spacer",
    "WebView", "Uiverse", "Custom",
)

FIT_MODES = ("Stretch", "Contain", "Cover", "Fit", "Center", "Tile")
ALIGN_H = ("left", "center", "right")
ALIGN_V = ("top", "middle", "bottom")
BORDER_STYLES = ("Solid", "Dashed", "Dotted", "Double")
ROTATABLE_TYPES = ("Image", "Panel", "Frame", "HUD Card", "Text", "Custom", "Indicator")
TEXT_TYPES = ("Text", "Button", "Input", "CheckBox", "Toggle", "HUD Card", "Indicator", "Custom")
IMAGE_TYPES = ("Image", "Panel", "Frame", "HUD Card", "Custom")

ELEMENT_SIZES = {
    "Text": (220, 42),
    "Button": (180, 42),
    "Panel": (260, 140),
    "Frame": (320, 200),
    "Input": (260, 40),
    "CheckBox": (180, 34),
    "Toggle": (180, 34),
    "ProgressBar": (260, 22),
    "Indicator": (120, 26),
    "HUD Card": (300, 180),
    "Image": (220, 160),
    "Separator": (260, 4),
    "Spacer": (80, 20),
    "WebView": (360, 240),
    "Uiverse": (320, 140),
    "Custom": (240, 120),
}

ELEMENT_DEFAULT_TEXT = {
    "Text": "JARVIS TEXT",
    "Button": "ACTION",
    "CheckBox": "ENABLE",
    "Toggle": "ACTIVE",
    "Indicator": "READY",
    "HUD Card": "HUD CARD",
}

# Effect-stack vocabulary.  Every kind below is really applied by
# ``apply_effect_stack`` (or by the painted layers) -- see EFFECT_SCHEMA users
# in the Studio's Properties -> EFFECTS section.
EFFECT_SCHEMA: dict[str, dict[str, tuple]] = {
    "Opacity": {"opacity": ("int", 0, 100, 100)},
    "Drop Shadow": {"color": ("color",), "blur": ("int", 0, 64, 16),
                    "dx": ("int", -40, 40, 0), "dy": ("int", -40, 40, 6),
                    "opacity": ("int", 0, 100, 70)},
    "Glow": {"color": ("color",), "radius": ("int", 1, 64, 18),
             "opacity": ("int", 0, 100, 80)},
    "Blur": {"radius": ("int", 0, 64, 8)},
    "Brightness": {"amount": ("int", -100, 100, 20)},
    "Contrast": {"amount": ("int", -100, 100, 20)},
    "Saturation": {"amount": ("int", -100, 100, 20)},
    "Grayscale": {"amount": ("int", 0, 100, 100)},
    "Colorize": {"color": ("color",), "strength": ("int", 0, 100, 45)},
    "Color Tint": {"color": ("color",), "strength": ("int", 0, 100, 35)},
    "Gradient": {"start": ("color",), "end": ("color",),
                 "angle": ("int", 0, 360, 90), "opacity": ("int", 0, 100, 100)},
    "Overlay": {"color": ("color",), "opacity": ("int", 0, 100, 30)},
    "Dark Overlay": {"opacity": ("int", 0, 100, 45)},
    "Light Overlay": {"opacity": ("int", 0, 100, 30)},
    "Transparent Overlay": {"opacity": ("int", 0, 100, 25)},
    "Inner Glow": {"color": ("color",), "radius": ("int", 1, 64, 12),
                   "strength": ("int", 0, 100, 70)},
    "Neon Edge": {"color": ("color",), "radius": ("int", 1, 32, 6),
                  "strength": ("int", 0, 100, 85)},
    "Frosted": {"radius": ("int", 0, 32, 10), "whiten": ("int", 0, 100, 18)},
}
EFFECT_KINDS = tuple(EFFECT_SCHEMA.keys())

HUD_PRESETS: dict[str, dict] = {
    "Glass": {
        "background": {"color": "#8ceaff", "opacity": 10,
                       "gradient": {"enabled": False}},
        "stroke": {"enabled": True, "color": "#ffffff", "width": 1.0, "opacity": 45, "radius": 12},
        "effects": [{"kind": "Frosted", "params": {"radius": 10, "whiten": 16}}],
    },
    "Dark Glass": {
        "background": {"color": "#01121c", "opacity": 62,
                       "gradient": {"enabled": True, "kind": "linear", "angle": 120,
                                    "stops": [[0.0, "#052232"], [1.0, "#010a12"]]}},
        "stroke": {"enabled": True, "color": ACCENT, "width": 1.0, "opacity": 60, "radius": 12},
        "effects": [{"kind": "Frosted", "params": {"radius": 8, "whiten": 8}}],
    },
    "Cyan HUD": {
        "background": {"color": "#002b3d", "opacity": 78,
                       "gradient": {"enabled": True, "kind": "linear", "angle": 90,
                                    "stops": [[0.0, "#00c8ff"], [1.0, "#00121d"]]}},
        "stroke": {"enabled": True, "color": ACCENT_BRIGHT, "width": 1.6, "opacity": 85, "radius": 8},
        "glow": {"enabled": True, "color": ACCENT, "strength": 65, "radius": 20, "opacity": 60, "spread": 2},
        "text_style": {"color": "#eafdff"},
    },
    "Blue HUD": {
        "background": {"color": "#061c3a", "opacity": 76,
                       "gradient": {"enabled": True, "kind": "radial", "angle": 90,
                                    "stops": [[0.0, "#1f6dff"], [1.0, "#020d1c"]]}},
        "stroke": {"enabled": True, "color": "#8ab6ff", "width": 1.4, "opacity": 80, "radius": 9},
        "glow": {"enabled": True, "color": "#4c8dff", "strength": 60, "radius": 18, "opacity": 55, "spread": 2},
    },
    "Neon HUD": {
        "background": {"color": "#03101a", "opacity": 65},
        "stroke": {"enabled": True, "color": ACCENT_BRIGHT, "width": 2.0, "opacity": 100, "radius": 6},
        "glow": {"enabled": True, "color": ACCENT, "strength": 85, "radius": 30, "opacity": 80, "spread": 4},
        "text_style": {"color": "#ffffff", "stroke": {"enabled": True, "color": ACCENT, "width": 2.0, "opacity": 90}},
    },
    "Transparent HUD": {
        "background": {"color": "#000000", "opacity": 0},
        "stroke": {"enabled": False},
        "glow": {"enabled": False},
        "effects": [],
    },
    "Scanner": {
        "background": {"color": "#00131c", "opacity": 72,
                       "gradient": {"enabled": True, "kind": "linear", "angle": 0,
                                    "stops": [[0.0, "#00d4ff"], [0.45, "#00202e"], [1.0, "#00d4ff"]]}},
        "stroke": {"enabled": True, "color": "#00ffd5", "width": 1.2, "opacity": 70, "radius": 4},
        "effects": [{"kind": "Saturation", "params": {"amount": 25}},
                    {"kind": "Overlay", "params": {"color": "#00ff9c", "opacity": 8}}],
    },
    "Terminal": {
        "background": {"color": "#000000", "opacity": 88},
        "stroke": {"enabled": True, "color": "#39ff8a", "width": 1.0, "opacity": 55, "radius": 2},
        "text_style": {"color": "#7dffb0", "family": "Share Tech Mono", "bold": False},
        "effects": [{"kind": "Overlay", "params": {"color": "#00ff5a", "opacity": 6}}],
    },
    "Tech Panel": {
        "background": {"color": "#031c26", "opacity": 80,
                       "gradient": {"enabled": True, "kind": "linear", "angle": 135,
                                    "stops": [[0.0, "#083445"], [1.0, "#010b12"]]}},
        "borders": {"radius": {"tl": 14, "tr": 14, "br": 14, "bl": 14},
                    "left": {"width": 3, "color": ACCENT, "style": "Solid", "opacity": 90}},
        "stroke": {"enabled": True, "color": ACCENT, "width": 1.0, "opacity": 45, "radius": 14},
    },
    "Warning": {
        "background": {"color": "#3a1f00", "opacity": 78},
        "stroke": {"enabled": True, "color": "#ffb228", "width": 1.8, "opacity": 90, "radius": 8},
        "glow": {"enabled": True, "color": "#ffb228", "strength": 60, "radius": 20, "opacity": 55, "spread": 2},
        "text_style": {"color": "#ffd694"},
    },
    "Alert": {
        "background": {"color": "#3d0410", "opacity": 80},
        "stroke": {"enabled": True, "color": "#ff4455", "width": 2.0, "opacity": 95, "radius": 8},
        "glow": {"enabled": True, "color": "#ff4455", "strength": 70, "radius": 24, "opacity": 65, "spread": 3},
        "text_style": {"color": "#ffd7dc"},
    },
    "Military": {
        "background": {"color": "#0d1408", "opacity": 84},
        "stroke": {"enabled": True, "color": "#8fd14f", "width": 1.4, "opacity": 70, "radius": 3},
        "text_style": {"color": "#cdf0a0", "family": "Share Tech Mono"},
        "effects": [{"kind": "Grayscale", "params": {"amount": 35}},
                    {"kind": "Overlay", "params": {"color": "#4f7a1f", "opacity": 12}}],
    },
    "Minimal": {
        "background": {"color": "#020d14", "opacity": 40},
        "stroke": {"enabled": False},
        "glow": {"enabled": False},
        "shadow": {"enabled": False},
        "effects": [],
    },
}

TEXT_PRESETS: dict[str, dict] = {
    "HUD Text": {"color": ACCENT_BRIGHT, "family": "Exo 2", "size": 11, "bold": True,
                 "letter_spacing": 1.2, "stroke": {"enabled": False},
                 "glow": {"enabled": True, "color": ACCENT, "radius": 6, "opacity": 60}},
    "Title Text": {"color": TEXT_BRIGHT, "family": "Orbitron", "size": 16, "bold": True,
                   "letter_spacing": 2.5, "align": "center",
                   "shadow": {"enabled": True, "color": "#000000", "blur": 8, "dx": 0, "dy": 2, "opacity": 70}},
    "Warning Text": {"color": "#ffd694", "family": "Exo 2", "size": 12, "bold": True,
                     "stroke": {"enabled": True, "color": "#7a3d00", "width": 2.5, "opacity": 90},
                     "glow": {"enabled": True, "color": "#ffb228", "radius": 8, "opacity": 65}},
    "System Text": {"color": TEXT_DIM, "family": "Share Tech Mono", "size": 9, "bold": False,
                    "letter_spacing": 0.8,
                    "shadow": {"enabled": False}, "stroke": {"enabled": False}, "glow": {"enabled": False}},
    "Text Glow": {"color": "#ffffff", "stroke": {"enabled": True, "color": ACCENT, "width": 2.0, "opacity": 95},
                  "glow": {"enabled": True, "color": ACCENT, "radius": 10, "opacity": 85}},
    "Text Stroke": {"color": "#ffffff", "stroke": {"enabled": True, "color": "#00a6d6", "width": 2.0, "opacity": 100},
                    "glow": {"enabled": False}},
}

ANIMATION_NAMES = (
    "None", "Fade In", "Fade Out", "Pulse", "Glow Pulse", "Blink",
    "Slide Left", "Slide Right", "Slide Up", "Slide Down",
    "Scale Up", "Scale Down", "Hover Glow", "Hover Brightness", "Press", "Loading",
)

CURSOR_NAMES = {
    "arrow": Qt.CursorShape.ArrowCursor,
    "pointing": Qt.CursorShape.PointingHandCursor,
    "open hand": Qt.CursorShape.OpenHandCursor,
    "closed hand": Qt.CursorShape.ClosedHandCursor,
    "text": Qt.CursorShape.IBeamCursor,
    "cross": Qt.CursorShape.CrossCursor,
    "size all": Qt.CursorShape.SizeAllCursor,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def widget_alive(obj) -> bool:
    """True only when ``obj`` is a QWidget whose C++ object still exists.

    Touching a deleted QObject from Python raises ``RuntimeError``; raised from
    inside ``paintEvent`` that aborts the whole application.  Every code path
    that dereferences a stored widget reference goes through here first, which
    is what keeps a stale selection from crashing J.A.R.V.I.S.
    """
    if obj is None:
        return False
    try:
        from PyQt6 import sip as _sip

        if _sip.isdeleted(obj):
            return False
    except Exception:
        pass
    try:
        obj.objectName()
        return True
    except Exception:
        return False


def safe_object_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return value.strip("_") or "widget"


def class_simple_name(obj) -> str:
    try:
        return type(obj).__name__.lstrip("_") or "Widget"
    except Exception:
        return "Widget"


def is_element_widget(obj) -> bool:
    return isinstance(obj, ElementWidget) and widget_alive(obj)


def clamp_int(value, low: int, high: int, default: int = 0) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, out))


def clamp_float(value, low: float, high: float, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, out))


def as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "none"):
        return default
    return text in ("1", "true", "yes", "on", "y", "t")


def as_color(value, fallback: str = ACCENT) -> str:
    """Normalise any colour-ish value to ``#rrggbb`` (alpha carried separately)."""
    text = str(value or "").strip()
    if not text:
        return fallback
    rgba = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+)\s*)?\)$", text, re.I)
    if rgba:
        col = QColor(int(rgba.group(1)), int(rgba.group(2)), int(rgba.group(3)))
        return col.name()
    col = QColor(text)
    if not col.isValid():
        return fallback
    return col.name()


def color_with_alpha(value, alpha: int) -> QColor:
    col = qcolor(as_color(value), max(0, min(255, int(alpha))))
    return col


def get_path(mapping: dict, path: str, default=None):
    """Read ``a.b.c`` out of nested dicts without raising."""
    node: Any = mapping
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(mapping: dict, path: str, value) -> None:
    parts = [p for p in str(path or "").split(".") if p]
    if not parts or not isinstance(mapping, dict):
        return
    node = mapping
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursive merge used by the HUD/text presets (patch wins)."""
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def apply_preset(spec: dict, preset: dict) -> dict:
    return deep_merge(copy.deepcopy(spec), preset)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def default_spec(name: str = "Element", kind: str = "Panel") -> dict:
    """Return a complete, renderable element spec for ``kind``."""
    kind = kind if kind in ELEMENT_TYPES else "Panel"
    width, height = ELEMENT_SIZES.get(kind, (240, 120))
    spec = {
        "name": str(name or kind),
        "type": kind,
        "kind": kind,                      # legacy mirror, kept for old readers
        "parent": "",
        "x": 24,
        "y": 24,
        "w": width,
        "h": height,
        "min_w": 0,
        "min_h": 0,
        "max_w": 16777215,
        "max_h": 16777215,
        "z": 0,
        "visible": True,
        "enabled": True,
        "hidden": False,
        "locked": False,
        "object_name": "",
        "tooltip": "",
        "group": "",
        "custom": {},
        "opacity": 100,
        "rotation": 0.0,
        "rotation_center": [0.5, 0.5],
        "background": {
            "color": "#04121b",
            "opacity": 72,
            "gradient": {"enabled": False, "kind": "linear", "angle": 90,
                         "stops": [[0.0, ACCENT], [1.0, BG_DEEP]]},
            "overlay": {"enabled": False, "kind": "dark", "color": "#000000", "opacity": 35},
        },
        "image": {
            "path": "", "asset": "", "opacity": 100, "fit": "Contain",
            "align_h": "center", "align_v": "middle", "tint": "", "tint_strength": 0,
            "smoothing": True, "keep_aspect": True, "crop": [0, 0, 0, 0],
            "is_background": False,
        },
        "text": ELEMENT_DEFAULT_TEXT.get(kind, name if kind not in ("ProgressBar", "Separator", "Spacer", "Image", "WebView") else ""),
        "text_style": {
            "color": TEXT,
            "opacity": 100,
            "family": "Exo 2",
            "size": 11,
            "bold": kind in ("Button", "Toggle", "Indicator"),
            "italic": False,
            "underline": False,
            "align": "center" if kind != "Text" else "left",
            "v_align": "middle",
            "wrap": True,
            "letter_spacing": 0.0,
            "stroke": {"enabled": False, "color": ACCENT_BRIGHT, "width": 2.0, "opacity": 100},
            "shadow": {"enabled": False, "color": "#000000", "blur": 6, "dx": 1, "dy": 1, "opacity": 70},
            "glow": {"enabled": False, "color": ACCENT, "radius": 8, "opacity": 80},
        },
        "stroke": {"enabled": False, "color": ACCENT, "width": 2.0, "opacity": 100, "radius": 10},
        "borders": {
            "top": {"width": 0, "color": ACCENT, "style": "Solid", "opacity": 100},
            "right": {"width": 0, "color": ACCENT, "style": "Solid", "opacity": 100},
            "bottom": {"width": 0, "color": ACCENT, "style": "Solid", "opacity": 100},
            "left": {"width": 0, "color": ACCENT, "style": "Solid", "opacity": 100},
            "radius": {"tl": 10, "tr": 10, "br": 10, "bl": 10},
        },
        "shadow": {"enabled": False, "color": "#000000", "blur": 18, "dx": 0, "dy": 6, "opacity": 60},
        "glow": {"enabled": False, "color": ACCENT, "strength": 60, "radius": 24, "opacity": 80, "spread": 0},
        "effects": [],
        "states": {
            "hover": {"enabled": False, "background": "", "border_color": "", "text_color": "",
                      "opacity": 100, "glow_radius": 0},
            "pressed": {"enabled": False, "background": "", "border_color": "", "text_color": "",
                        "opacity": 100, "glow_radius": 0},
            "focused": {"enabled": False, "background": "", "border_color": "", "text_color": "",
                        "opacity": 100, "glow_radius": 0},
            "disabled": {"enabled": False, "background": "", "border_color": "", "text_color": "",
                         "opacity": 55, "glow_radius": 0},
            "selected": {"enabled": False, "background": "", "border_color": "", "text_color": "",
                         "opacity": 100, "glow_radius": 0},
        },
        "animation": {"name": "None", "duration": 600, "delay": 0, "loops": 1,
                      "easing": "In Out Quad", "reverse": False},
        "interaction": {"cursor": "arrow", "click_action": "", "tooltip": ""},
        "script": {"enabled": False, "code": "", "language": "python"},
        "layout": {"anchor": "absolute", "padding": 0, "spacing": 0},
        "stylesheet": "",
        "html": "",
        "css": "",
    }
    if kind in ("Separator", "Spacer"):
        spec["background"]["color"] = ACCENT
        spec["background"]["opacity"] = 90
    if kind == "Image":
        spec["background"]["opacity"] = 0
        spec["borders"]["radius"] = {"tl": 8, "tr": 8, "br": 8, "bl": 8}
    return spec


def _migrate_legacy(raw: dict, spec: dict) -> dict:
    """Fold the v3 ``kind``/``text``/``color`` dict into the v4 schema."""
    legacy_kind = str(raw.get("kind") or raw.get("type") or "").strip()
    if legacy_kind:
        aliases = {
            "window": "Frame", "monitor": "HUD Card", "uiverse": "Uiverse",
            "text": "Text", "button": "Button", "panel": "Panel", "input": "Input",
            "checkbox": "CheckBox", "toggle": "Toggle", "progressbar": "ProgressBar",
            "image": "Image", "separator": "Separator", "spacer": "Spacer",
            "webview": "WebView", "indicator": "Indicator", "frame": "Frame",
            "hud card": "HUD Card", "custom": "Custom",
        }
        spec["type"] = aliases.get(legacy_kind.lower(), legacy_kind if legacy_kind in ELEMENT_TYPES else "Panel")
        spec["kind"] = spec["type"]

    legacy_color = raw.get("color")
    if legacy_color:
        # v3 used one colour for both the border and the label.
        set_path(spec, "stroke.color", as_color(legacy_color, ACCENT))
        set_path(spec, "text_style.color", as_color(legacy_color, TEXT))
        spec["background"]["color"] = as_color(legacy_color, "#04121b")

    legacy_opacity = raw.get("opacity")
    if legacy_opacity is not None:
        spec["opacity"] = clamp_int(legacy_opacity, 0, 100, 100)

    legacy_z = raw.get("z_order")
    if legacy_z is not None:
        spec["z"] = clamp_int(legacy_z, -100000, 100000, 0)

    if str(spec.get("type")) in ("Panel", "Frame", "HUD Card") and as_bool(raw.get("visible", True)):
        if raw.get("border") is True:
            spec["stroke"]["enabled"] = True
    return spec


def normalize_element(raw: Any, name: str | None = None) -> dict:
    """Return a complete, safe element spec for any stored element dict.

    Accepts v3 dicts (``kind``/``text``/``color``/``x``/``y``/``w``/``h``) and v4
    specs, fills every missing field with a default, and never raises -- the
    loader runs on application start, where an exception would blank the UI.
    """
    spec = default_spec(str(name or (raw or {}).get("name") or "Element") if isinstance(raw, dict) else str(name or "Element"))
    if not isinstance(raw, dict):
        return spec

    element_name = str(raw.get("name") or name or spec["name"])
    spec["name"] = element_name
    _migrate_legacy(raw, spec)

    for key in ("parent", "object_name", "tooltip", "group", "text", "stylesheet", "html", "css"):
        if key in raw and raw[key] is not None:
            spec[key] = str(raw[key])
    if not spec["text"] and str(spec["type"]) not in ("Image", "Separator", "Spacer", "WebView", "ProgressBar", "Uiverse", "Panel", "Frame"):
        spec["text"] = ELEMENT_DEFAULT_TEXT.get(str(spec["type"]), element_name)

    for key, low, high, default in (
        ("x", -20000, 20000, spec["x"]),
        ("y", -20000, 20000, spec["y"]),
        ("w", 1, 20000, spec["w"]),
        ("h", 1, 20000, spec["h"]),
        ("min_w", 0, 20000, 0),
        ("min_h", 0, 20000, 0),
        ("max_w", 0, 16777215, 16777215),
        ("max_h", 0, 16777215, 16777215),
        ("z", -100000, 100000, 0),
        ("opacity", 0, 100, 100),
    ):
        if key in raw:
            spec[key] = clamp_int(raw[key], low, high, default)

    for key in ("visible", "enabled", "locked", "hidden"):
        if key in raw:
            spec[key] = as_bool(raw[key], spec[key])
    # v3 collapsed "hidden" into "visible".
    if "hidden" in raw:
        spec["hidden"] = as_bool(raw["hidden"])
        spec["visible"] = not spec["hidden"] if "visible" not in raw else spec["visible"]

    if "rotation" in raw:
        spec["rotation"] = clamp_float(raw["rotation"], -360.0, 360.0, 0.0)
    if isinstance(raw.get("rotation_center"), (list, tuple)) and len(raw["rotation_center"]) == 2:
        spec["rotation_center"] = [clamp_float(raw["rotation_center"][0], -2, 3, 0.5),
                                   clamp_float(raw["rotation_center"][1], -2, 3, 0.5)]
    if isinstance(raw.get("custom"), dict):
        spec["custom"] = copy.deepcopy(raw["custom"])

    # Runtime capture payloads (``_runtime_svg`` / ``_runtime_image`` /
    # ``_runtime_mode``) are how the Figma exporter receives what a live widget
    # actually painted — custom QPainter logos, pixmap assets, WebEngine frames,
    # stylesheet backgrounds. They were previously dropped here, which meant
    # every captured visual vanished between the Studio and the SVG and the
    # export showed blank layers instead. They are read-only export hints: the
    # save path strips them again in ``elements_to_layout``.
    for key in ("_runtime_svg", "_runtime_image", "_runtime_mode"):
        if key in raw:
            spec[key] = raw[key]

    # Nested blocks: merge stored values over the defaults field by field so a
    # partial block from an older file still yields a complete spec.
    for block in ("background", "image", "text_style", "stroke", "borders", "shadow",
                  "glow", "states", "animation", "interaction", "script", "layout"):
        stored = raw.get(block)
        if isinstance(stored, dict):
            deep_merge(spec[block], stored)

    if isinstance(raw.get("effects"), list):
        clean: list[dict] = []
        for entry in raw["effects"]:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind") or entry.get("type") or "").strip()
            if kind not in EFFECT_SCHEMA:
                continue
            params = dict(entry.get("params") or {})
            schema = EFFECT_SCHEMA[kind]
            clean_params: dict[str, Any] = {}
            for pkey, ptype in schema.items():
                value = params.get(pkey)
                if value is None:
                    continue
                if ptype[0] == "int":
                    clean_params[pkey] = clamp_int(value, ptype[1], ptype[2], ptype[3])
                elif ptype[0] == "color":
                    clean_params[pkey] = as_color(value, ACCENT)
            clean.append({"kind": kind, "enabled": as_bool(entry.get("enabled", True), True),
                          "params": clean_params})
        spec["effects"] = clean

    # Enum guards.
    spec["image"]["fit"] = str(spec["image"].get("fit") or "Contain")
    if spec["image"]["fit"] not in FIT_MODES:
        spec["image"]["fit"] = "Contain"
    spec["image"]["align_h"] = str(spec["image"].get("align_h") or "center")
    if spec["image"]["align_h"] not in ALIGN_H:
        spec["image"]["align_h"] = "center"
    spec["image"]["align_v"] = str(spec["image"].get("align_v") or "middle")
    if spec["image"]["align_v"] not in ALIGN_V:
        spec["image"]["align_v"] = "middle"
    spec["image"]["opacity"] = clamp_int(spec["image"].get("opacity", 100), 0, 100, 100)
    spec["image"]["tint_strength"] = clamp_int(spec["image"].get("tint_strength", 0), 0, 100, 0)
    crop = spec["image"].get("crop")
    if not (isinstance(crop, (list, tuple)) and len(crop) == 4):
        spec["image"]["crop"] = [0, 0, 0, 0]
    else:
        spec["image"]["crop"] = [clamp_int(v, 0, 100000, 0) for v in crop]

    spec["text_style"]["align"] = str(spec["text_style"].get("align") or "left")
    if spec["text_style"]["align"] not in ALIGN_H:
        spec["text_style"]["align"] = "left"
    spec["text_style"]["v_align"] = str(spec["text_style"].get("v_align") or "middle")
    if spec["text_style"]["v_align"] not in ALIGN_V:
        spec["text_style"]["v_align"] = "middle"
    spec["text_style"]["size"] = clamp_int(spec["text_style"].get("size", 11), 1, 200, 11)
    spec["text_style"]["opacity"] = clamp_int(spec["text_style"].get("opacity", 100), 0, 100, 100)
    spec["text_style"]["color"] = as_color(spec["text_style"].get("color"), TEXT)
    spec["text_style"]["letter_spacing"] = clamp_float(spec["text_style"].get("letter_spacing", 0.0), -20.0, 40.0, 0.0)

    spec["background"]["color"] = as_color(spec["background"].get("color"), "#04121b")
    spec["background"]["opacity"] = clamp_int(spec["background"].get("opacity", 72), 0, 100, 72)
    stops = get_path(spec, "background.gradient.stops")
    if not (isinstance(stops, list) and len(stops) >= 2):
        spec["background"]["gradient"]["stops"] = [[0.0, ACCENT], [1.0, BG_DEEP]]
    else:
        fixed: list[list[Any]] = []
        for stop in stops:
            # A stop is ``[position, colour]``, optionally ``[position, colour,
            # alpha]`` when it was read back out of a Qt ``rgba()`` gradient, so
            # per-stop transparency survives a save/load round trip instead of
            # being silently replaced by the default accent ramp.
            if not isinstance(stop, (list, tuple)) or len(stop) not in (2, 3):
                continue
            normalised = [clamp_float(stop[0], 0.0, 1.0, 0.0), as_color(stop[1], ACCENT)]
            if len(stop) == 3:
                normalised.append(clamp_float(stop[2], 0.0, 1.0, 1.0))
            fixed.append(normalised)
        spec["background"]["gradient"]["stops"] = fixed if len(fixed) >= 2 else [[0.0, ACCENT], [1.0, BG_DEEP]]
    spec["background"]["gradient"]["angle"] = clamp_int(get_path(spec, "background.gradient.angle", 90), 0, 360, 90)
    spec["background"]["gradient"]["kind"] = str(get_path(spec, "background.gradient.kind", "linear") or "linear")

    for side in ("top", "right", "bottom", "left"):
        block = spec["borders"].setdefault(side, {})
        if not isinstance(block, dict):
            block = {}
            spec["borders"][side] = block
        block["width"] = clamp_int(block.get("width", 0), 0, 60, 0)
        block["color"] = as_color(block.get("color"), ACCENT)
        block["opacity"] = clamp_int(block.get("opacity", 100), 0, 100, 100)
        style = str(block.get("style") or "Solid")
        block["style"] = style if style in BORDER_STYLES else "Solid"
    radius = spec["borders"].setdefault("radius", {})
    if not isinstance(radius, dict):
        radius = {}
        spec["borders"]["radius"] = radius
    for corner in ("tl", "tr", "br", "bl"):
        radius[corner] = clamp_int(radius.get(corner, 10), 0, 400, 10)

    for block_name in ("stroke", "shadow", "glow"):
        block = spec[block_name]
        block["enabled"] = as_bool(block.get("enabled", False))
        block["color"] = as_color(block.get("color"), ACCENT)
        block["opacity"] = clamp_int(block.get("opacity", 100), 0, 100, 100)

    states = spec["states"]
    if not isinstance(states, dict):
        states = default_spec()["states"]
        spec["states"] = states
    for state_name in ("hover", "pressed", "focused", "disabled", "selected"):
        state = states.setdefault(state_name, {})
        if not isinstance(state, dict):
            state = {}
            states[state_name] = state
        state["enabled"] = as_bool(state.get("enabled", False))
        for colour_key in ("background", "border_color", "text_color"):
            if state.get(colour_key):
                state[colour_key] = as_color(state[colour_key], "")
        state["opacity"] = clamp_int(state.get("opacity", 100), 0, 100, 100)
        state["glow_radius"] = clamp_int(state.get("glow_radius", 0), 0, 64, 0)

    animation = spec["animation"]
    if not isinstance(animation, dict):
        animation = default_spec()["animation"]
        spec["animation"] = animation
    if str(animation.get("name") or "None") not in ANIMATION_NAMES:
        animation["name"] = "None"
    animation["duration"] = clamp_int(animation.get("duration", 600), 30, 20000, 600)
    animation["delay"] = clamp_int(animation.get("delay", 0), 0, 20000, 0)
    animation["loops"] = clamp_int(animation.get("loops", 1), 0, 999, 1)

    script = spec["script"]
    if not isinstance(script, dict):
        script = default_spec()["script"]
        spec["script"] = script
    script["enabled"] = as_bool(script.get("enabled", False))
    script["code"] = str(script.get("code") or "")

    return spec


# ---------------------------------------------------------------------------
# Persistence (merge-write; never truncates another subsystem's keys)
# ---------------------------------------------------------------------------
LAYOUT_DEFAULTS = {
    "version": SCHEMA_VERSION,
    "active_panel": "Main Surface",
    "widgets": {},
    "elements": [],
    "assets": {},
}


def load_layout(path: str | Path) -> dict:
    """Load a layout file while keeping older ``elements``-only files valid."""
    path = Path(path)
    data = copy.deepcopy(LAYOUT_DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update(raw)
            if not isinstance(data.get("widgets"), dict):
                data["widgets"] = {}
            if not isinstance(data.get("elements"), list):
                data["elements"] = []
            if not isinstance(data.get("assets"), dict):
                data["assets"] = {}
            try:
                data["version"] = max(SCHEMA_VERSION, int(data.get("version", SCHEMA_VERSION)))
            except Exception:
                data["version"] = SCHEMA_VERSION
    except (OSError, ValueError, TypeError):
        pass
    return data


def save_layout(path: str | Path, data: dict) -> None:
    """Merge-write the layout file.

    The original implementation rebuilt the payload from ``LAYOUT_DEFAULTS`` and
    updated it with only the caller's dict, which silently discarded every
    top-level key written by another subsystem (``MainWindow._save_layout_elements``
    passes just ``{"elements": [...]}``, so each save deleted the ``camera_window``
    geometry stored by the camera panel).  Unknown keys are preserved, and the
    write is staged through a temp file so an interrupted save cannot truncate a
    working layout.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            existing = raw
    except (OSError, ValueError, TypeError):
        existing = {}

    payload = copy.deepcopy(LAYOUT_DEFAULTS)
    payload.update(existing)
    payload.update(data if isinstance(data, dict) else {})
    payload["version"] = SCHEMA_VERSION
    if not isinstance(payload.get("widgets"), dict):
        payload["widgets"] = {}
    if not isinstance(payload.get("elements"), list):
        payload["elements"] = []
    if not isinstance(payload.get("assets"), dict):
        payload["assets"] = {}

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def elements_from_layout(data: dict) -> dict[str, dict]:
    """``{name: spec}`` for every element in a loaded layout mapping."""
    out: dict[str, dict] = {}
    for raw in (data.get("elements") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        out[name] = normalize_element(raw, name)
    return out


def elements_to_layout(elements: dict[str, dict]) -> list[dict]:
    ordered = sorted(
        (normalize_element(spec, name) for name, spec in (elements or {}).items()),
        key=lambda s: (int(s.get("z", 0) or 0), str(s.get("name", ""))),
    )
    # Captured pixel/vector payloads belong to one export, not to the layout
    # file: embedding base64 art in config/ui_layout.json would bloat every
    # start-up load for data that is regenerated from the live widgets anyway.
    for spec in ordered:
        for key in ("_runtime_svg", "_runtime_image", "_runtime_mode"):
            spec.pop(key, None)
    return ordered


# ---------------------------------------------------------------------------
# Image cache
# ---------------------------------------------------------------------------
class ImageCache:
    """LRU caches for full, scaled and thumbnail pixmaps.

    Keyed by ``(resolved path, mtime_ns)`` so an edited file is re-read, while
    the same file is decoded exactly once.  This is what keeps dragging an image
    element smooth: the painter asks for an already-scaled pixmap instead of
    re-decoding the source.
    """

    def __init__(self, max_full: int = 32, max_scaled: int = 96, max_thumbs: int = 256):
        self._full: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._scaled: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._thumbs: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._max_full = int(max_full)
        self._max_scaled = int(max_scaled)
        self._max_thumbs = int(max_thumbs)
        self.decode_failures: dict[str, str] = {}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _stamp(path: str | Path) -> tuple | None:
        p = Path(str(path))
        try:
            stat = p.stat()
        except OSError:
            return None
        return (str(p.resolve()), int(stat.st_mtime_ns), int(stat.st_size))

    @staticmethod
    def _trim(cache: "OrderedDict[tuple, QPixmap]", limit: int) -> None:
        while len(cache) > max(1, limit):
            cache.popitem(last=False)

    # -- public API ------------------------------------------------------
    def is_svg(self, path: str | Path) -> bool:
        return Path(str(path)).suffix.lower() == ".svg"

    def pixmap(self, path: str | Path) -> QPixmap | None:
        stamp = self._stamp(path)
        if stamp is None:
            self.decode_failures[str(path)] = "file not found"
            return None
        cached = self._full.get(stamp)
        if cached is not None:
            self._full.move_to_end(stamp)
            return cached
        pix = self._decode(stamp[0])
        if pix is None:
            return None
        self._full[stamp] = pix
        self._trim(self._full, self._max_full)
        return pix

    def _decode(self, resolved: str) -> QPixmap | None:
        if resolved.lower().endswith(".svg"):
            if QSvgRenderer is None:
                self.decode_failures[resolved] = "SVG support requires PyQt6.QtSvg (pip install PyQt6)"
                return None
            try:
                renderer = QSvgRenderer(resolved)
                if not renderer.isValid():
                    self.decode_failures[resolved] = "invalid SVG"
                    return None
                size = renderer.defaultSize()
                if size.width() <= 0 or size.height() <= 0:
                    size = QSize(512, 512)
                pix = QPixmap(size)
                pix.fill(Qt.GlobalColor.transparent)
                painter = QPainter()
                if not painter.begin(pix):
                    self.decode_failures[resolved] = "SVG painter unavailable"
                    return None
                try:
                    renderer.render(painter)
                finally:
                    if painter.isActive():
                        painter.end()
                return pix
            except Exception as exc:  # pragma: no cover - defensive
                self.decode_failures[resolved] = str(exc)
                return None
        pix = QPixmap(resolved)
        if pix.isNull():
            self.decode_failures[resolved] = "unsupported or corrupt image"
            return None
        self.decode_failures.pop(resolved, None)
        return pix

    def scaled(self, path: str | Path, size: QSize, smooth: bool = True,
               crop: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QPixmap | None:
        """A scaled copy of ``path`` for the given target size (cached)."""
        if size.width() <= 0 or size.height() <= 0:
            return None
        stamp = self._stamp(path)
        if stamp is None:
            return None
        key = (stamp, int(size.width()), int(size.height()), bool(smooth), tuple(crop))
        cached = self._scaled.get(key)
        if cached is not None:
            self._scaled.move_to_end(key)
            return cached
        source = self.pixmap(path)
        if source is None:
            return None
        left, top, right, bottom = (max(0, int(v)) for v in crop)
        src = QRect(left, top,
                    max(1, source.width() - left - right),
                    max(1, source.height() - top - bottom))
        mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
        out = source.copy(src).scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, mode)
        self._scaled[key] = out
        self._trim(self._scaled, self._max_scaled)
        return out

    def thumbnail(self, path: str | Path, size: int = 96) -> QPixmap | None:
        stamp = self._stamp(path)
        if stamp is None:
            return None
        key = (stamp, int(size))
        cached = self._thumbs.get(key)
        if cached is not None:
            self._thumbs.move_to_end(key)
            return cached
        source = self.pixmap(path)
        if source is None:
            return None
        out = source.scaled(max(8, int(size)), max(8, int(size)),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        self._thumbs[key] = out
        self._trim(self._thumbs, self._max_thumbs)
        return out

    def info(self, path: str | Path) -> QSize:
        pix = self.pixmap(path)
        return pix.size() if pix is not None else QSize(0, 0)

    def clear(self) -> None:
        self._full.clear()
        self._scaled.clear()
        self._thumbs.clear()


IMAGE_CACHE = ImageCache()

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".svg")


# ---------------------------------------------------------------------------
# Asset store
# ---------------------------------------------------------------------------
class AssetStore:
    """Registry of imported image assets (``{name: {"path": ..., "imported": ...}}``).

    Assets are copied into ``assets/layout_images`` when imported from outside
    the project so a layout keeps working after the source file moves.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else Path.cwd()
        self.directory = self.root / "assets" / "layout_images"
        self.entries: dict[str, dict] = {}

    # -- state -----------------------------------------------------------
    def load(self, mapping: Any) -> None:
        self.entries = {}
        if isinstance(mapping, dict):
            for name, entry in mapping.items():
                if isinstance(entry, dict) and entry.get("path"):
                    self.entries[str(name)] = {"path": str(entry["path"]),
                                               "imported": entry.get("imported", "")}
                elif isinstance(entry, str):
                    self.entries[str(name)] = {"path": entry, "imported": ""}

    def to_json(self) -> dict:
        return copy.deepcopy(self.entries)

    # -- operations ------------------------------------------------------
    def unique_name(self, path: str | Path, base: str = "") -> str:
        stem = base or Path(str(path)).stem or "asset"
        stem = re.sub(r"[^A-Za-z0-9_ -]+", "_", stem).strip() or "asset"
        name = stem
        index = 2
        while name in self.entries:
            name = f"{stem}_{index}"
            index += 1
        return name

    def register(self, path: str | Path, name: str = "", copy_into: bool = False) -> str:
        src = Path(str(path))
        if copy_into:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                target = self.directory / src.name
                index = 2
                while target.exists() and target.resolve() != src.resolve():
                    target = self.directory / f"{src.stem}_{index}{src.suffix}"
                    index += 1
                if target.resolve() != src.resolve():
                    target.write_bytes(src.read_bytes())
                src = target
            except OSError:
                pass
        resolved_name = self.unique_name(src, name)
        self.entries[resolved_name] = {"path": str(src), "imported": ""}
        return resolved_name

    def remove(self, name: str, delete_file: bool = False) -> None:
        entry = self.entries.pop(str(name), None)
        if delete_file and isinstance(entry, dict):
            try:
                path = Path(str(entry.get("path", "")))
                if path.is_file() and self.directory in path.parents:
                    path.unlink()
            except OSError:
                pass

    def rename(self, old: str, new: str) -> str:
        entry = self.entries.pop(str(old), None)
        if entry is None:
            return str(old)
        clean = re.sub(r"[^A-Za-z0-9_ -]+", "_", str(new)).strip() or str(old)
        final = clean
        index = 2
        while final in self.entries:
            final = f"{clean}_{index}"
            index += 1
        self.entries[final] = entry
        return final

    def replace(self, name: str, path: str | Path, copy_into: bool = True) -> str:
        new_name = self.register(path, name=str(name), copy_into=copy_into)
        if new_name != str(name):
            self.entries.pop(str(name), None)
        return new_name

    def path_for(self, name: str) -> str:
        entry = self.entries.get(str(name))
        if isinstance(entry, dict):
            return str(entry.get("path", ""))
        return ""

    def name_for_path(self, path: str | Path) -> str:
        target = str(Path(str(path)))
        for name, entry in self.entries.items():
            if str(entry.get("path", "")) == target:
                return name
        return ""

    def items(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for name, entry in sorted(self.entries.items(), key=lambda kv: kv[0].lower()):
            path = str(entry.get("path", ""))
            if path:
                out.append((name, path))
        return out

    def unused(self, elements: Iterable[dict]) -> list[str]:
        used: set[str] = set()
        for spec in elements or []:
            if not isinstance(spec, dict):
                continue
            asset = get_path(spec, "image.asset")
            if asset:
                used.add(str(asset))
        return [name for name, _ in self.items() if name not in used]


# ---------------------------------------------------------------------------
# Effect stack (real pixel work)
# ---------------------------------------------------------------------------
def _qimage_to_array(image: QImage):
    if _np is None:
        return None
    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = img.width(), img.height()
    bpl = img.bytesPerLine()
    buf = img.constBits()
    if buf is None:
        return None
    buf.setsize(bpl * height)
    try:
        arr = _np.frombuffer(bytes(buf), dtype=_np.uint8).reshape(height, bpl)
    except ValueError:
        return None
    arr = arr[:, : width * 4].reshape(height, width, 4).astype(_np.float32)
    return arr


def _array_to_qimage(arr) -> QImage:
    if _np is None:
        return QImage()
    data = _np.clip(arr, 0, 255).astype(_np.uint8)
    data = _np.ascontiguousarray(data)
    height, width = data.shape[0], data.shape[1]
    return QImage(data.tobytes(), width, height, width * 4,
                  QImage.Format.Format_RGBA8888).copy()


def _box_blur(arr, radius: int, passes: int = 2):
    radius = int(max(0, radius))
    if _np is None or radius <= 0:
        return arr
    out = arr
    for _ in range(max(1, passes)):
        for axis in (0, 1):
            out = _box_blur_axis(out, radius, axis)
    return out


def _box_blur_axis(arr, radius: int, axis: int):
    count = arr.shape[axis]
    kernel = 2 * radius + 1
    pad = [(0, 0)] * arr.ndim
    pad[axis] = (radius, radius)
    padded = _np.pad(arr, pad, mode="edge")
    cumulative = _np.cumsum(padded, axis=axis, dtype=_np.float32)
    zero_shape = list(arr.shape)
    zero_shape[axis] = 1
    cumulative = _np.concatenate([_np.zeros(zero_shape, _np.float32), cumulative], axis=axis)
    upper = [slice(None)] * arr.ndim
    lower = [slice(None)] * arr.ndim
    upper[axis] = slice(kernel, kernel + count)
    lower[axis] = slice(0, count)
    return (cumulative[tuple(upper)] - cumulative[tuple(lower)]) / float(kernel)


def _apply_effect_to_array(arr, kind: str, params: dict):
    if _np is None or arr is None:
        return arr
    rgb = arr[:, :, :3]
    if kind == "Brightness":
        amount = clamp_float(params.get("amount", 0), -100, 100, 0) / 100.0
        arr[:, :, :3] = rgb * (1.0 + amount) if amount >= 0 else rgb * max(0.0, 1.0 + amount)
    elif kind == "Contrast":
        amount = clamp_float(params.get("amount", 0), -100, 100, 0) / 100.0
        factor = 1.0 + amount
        arr[:, :, :3] = (rgb - 128.0) * factor + 128.0
    elif kind == "Saturation":
        amount = clamp_float(params.get("amount", 0), -100, 100, 0) / 100.0
        gray = (rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114)[:, :, None]
        arr[:, :, :3] = gray + (rgb - gray) * (1.0 + amount)
    elif kind == "Grayscale":
        amount = clamp_float(params.get("amount", 100), 0, 100, 100) / 100.0
        gray = (rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114)[:, :, None]
        arr[:, :, :3] = rgb + (gray - rgb) * amount
    elif kind in ("Colorize", "Color Tint"):
        colour = qcolor(as_color(params.get("color"), ACCENT))
        strength = clamp_float(params.get("strength", params.get("amount", 40)), 0, 100, 40) / 100.0
        target = _np.array([colour.red(), colour.green(), colour.blue()], _np.float32)
        arr[:, :, :3] = rgb + (target.reshape(1, 1, 3) - rgb) * strength
    elif kind == "Blur":
        radius = clamp_int(params.get("radius", 8), 0, 64, 8)
        if radius > 0:
            blurred = _box_blur(arr[:, :, :3], radius)
            arr[:, :, :3] = blurred
    elif kind in ("Inner Glow", "Neon Edge"):
        radius = clamp_int(params.get("radius", 12), 1, 64, 12)
        strength = clamp_float(params.get("strength", 70), 0, 100, 70) / 100.0
        colour = qcolor(as_color(params.get("color"), ACCENT))
        alpha = arr[:, :, 3:4] / 255.0
        silhouette = _np.repeat(alpha, 3, axis=2) * 255.0
        blurred = _box_blur(silhouette, max(1, radius // 2))
        edge = _np.clip(blurred - silhouette, 0, 255) / 255.0
        target = _np.array([colour.red(), colour.green(), colour.blue()], _np.float32).reshape(1, 1, 3)
        if kind == "Inner Glow":
            mask = _np.clip(silhouette / 255.0, 0, 1) * (1.0 - edge) * strength
        else:
            mask = shift_edge(edge) * strength
        arr[:, :, :3] = rgb + (target - rgb) * mask
    elif kind == "Frosted":
        radius = clamp_int(params.get("radius", 10), 0, 32, 10)
        whiten = clamp_float(params.get("whiten", 18), 0, 100, 18) / 100.0
        if radius > 0:
            arr[:, :, :3] = _box_blur(arr[:, :, :3], radius, passes=1)
        arr[:, :, :3] = arr[:, :, :3] + (255.0 - arr[:, :, :3]) * whiten * 0.35
    elif kind in ("Overlay", "Dark Overlay", "Light Overlay", "Transparent Overlay"):
        if kind == "Overlay":
            colour = qcolor(as_color(params.get("color"), "#000000"))
            opacity = clamp_float(params.get("opacity", 30), 0, 100, 30) / 100.0
        elif kind == "Light Overlay":
            colour = QColor(255, 255, 255)
            opacity = clamp_float(params.get("opacity", 30), 0, 100, 30) / 100.0
        elif kind == "Transparent Overlay":
            colour = QColor(255, 255, 255)
            opacity = clamp_float(params.get("opacity", 25), 0, 100, 25) / 100.0
        else:
            colour = QColor(0, 0, 0)
            opacity = clamp_float(params.get("opacity", 45), 0, 100, 45) / 100.0
        target = _np.array([colour.red(), colour.green(), colour.blue()], _np.float32).reshape(1, 1, 3)
        arr[:, :, :3] = rgb + (target - rgb) * opacity
    elif kind == "Opacity":
        alpha = clamp_float(params.get("opacity", 100), 0, 100, 100) / 100.0
        arr[:, :, 3] = arr[:, :, 3] * alpha
    return arr


def shift_edge(edge):
    """Return the silhouette edge rolled inward, used by the Neon Edge effect."""
    if _np is None:
        return edge
    out = edge.copy()
    out[1:, :, :] = _np.maximum(out[1:, :, :], edge[:-1, :, :])
    out[:-1, :, :] = _np.maximum(out[:-1, :, :], edge[1:, :, :])
    out[:, 1:, :] = _np.maximum(out[:, 1:, :], edge[:, :-1, :])
    out[:, :-1, :] = _np.maximum(out[:, :-1, :], edge[:, 1:, :])
    return out


def apply_effect_stack(image: QImage, effects: Iterable[dict]) -> QImage:
    """Apply every enabled effect in order.  Returns ``image`` when it cannot."""
    if _np is None or image is None or image.isNull():
        return image
    active = [e for e in (effects or []) if isinstance(e, dict) and as_bool(e.get("enabled", True), True)]
    if not active:
        return image
    arr = _qimage_to_array(image)
    if arr is None:
        return image
    for effect in active:
        kind = str(effect.get("kind") or "")
        if kind not in EFFECT_SCHEMA:
            continue
        try:
            arr = _apply_effect_to_array(arr, kind, dict(effect.get("params") or {}))
        except Exception:
            continue
    return _array_to_qimage(arr)


def effects_supported() -> tuple[bool, str]:
    """Whether pixel effects can run, and why not when they cannot."""
    if _np is None:
        return False, "numpy is not installed — pixel effects (blur/brightness/…) are unavailable"
    return True, ""


# ---------------------------------------------------------------------------
# QSS for runtime widgets (and reusable gradient/border maths)
# ---------------------------------------------------------------------------
def gradient_qss(block: dict, angle: int | None = None, kind: str | None = None) -> str:
    stops = get_path(block, "gradient.stops", []) or []
    kind = str(kind or get_path(block, "gradient.kind", "linear") or "linear")
    angle = clamp_int(angle if angle is not None else get_path(block, "gradient.angle", 90), 0, 360, 90)
    parts = []
    for index, stop in enumerate(stops[:8]):
        try:
            position, colour = float(stop[0]), as_color(stop[1], ACCENT)
        except (TypeError, ValueError, IndexError):
            continue
        parts.append(f"stop:{max(0.0, min(1.0, position)):.3f} {colour}")
    if len(parts) < 2:
        parts = [f"stop:0.0 {ACCENT}", f"stop:1.0 {BG_DEEP}"]
    if kind == "radial":
        first = parts[0].split(" ", 1)[-1]
        last = parts[-1].split(" ", 1)[-1]
        return f"qradialgradient(cx:0.5,cy:0.5,radius:0.75,stop:0 {first},stop:1 {last})"
    # QSS angles are clockwise degrees starting at the top-left corner.
    rad = math.radians(float(angle))
    x1, y1 = 1 - math.cos(rad), 1 + math.sin(rad)
    x2, y2 = 1 + math.cos(rad), 1 - math.sin(rad)
    return ("qlineargradient(x1:%.3f,y1:%.3f,x2:%.3f,y2:%.3f,%s)"
            % (x1 / 2.0, y1 / 2.0, x2 / 2.0, y2 / 2.0, ",".join(parts)))


def _rgba_css(colour: str, opacity: int) -> str:
    col = qcolor(as_color(colour, ACCENT), clamp_int(opacity, 0, 100, 100) * 255 // 100)
    return f"rgba({col.red()},{col.green()},{col.blue()},{col.alpha()})"


def build_qss(spec: dict) -> str:
    """Generate the QSS that reproduces ``spec`` on a real Qt widget.

    Used for runtime J.A.R.V.I.S. widgets, which cannot be re-painted by
    ``ElementWidget``.  Everything it emits is an actual Qt style rule:
    background colour/gradient, per-side borders, corner radii, padding, font,
    text colour and the hover/pressed/focus/disabled/selected state blocks.
    """
    selector = "QWidget"
    rules: list[str] = []
    background = spec.get("background") or {}
    bg_opacity = clamp_int(background.get("opacity", 100), 0, 100, 100)
    if bg_opacity > 0:
        if as_bool(get_path(background, "gradient.enabled", False)):
            rules.append(f"background:{gradient_qss(background)};")
        else:
            rules.append(f"background-color:{_rgba_css(background.get('color', BG), bg_opacity)};")

    borders = spec.get("borders") or {}
    for side in ("top", "right", "bottom", "left"):
        block = borders.get(side) or {}
        width = clamp_int(block.get("width", 0), 0, 60, 0)
        if width <= 0:
            continue
        style = str(block.get("style", "Solid")).lower()
        style = style if style in ("solid", "dashed", "dotted", "double") else "solid"
        rules.append(f"border-{side}:{width}px {style} "
                     f"{_rgba_css(block.get('color', ACCENT), block.get('opacity', 100))};")
    if not any(str(rule).startswith("border-") for rule in rules):
        stroke = spec.get("stroke") or {}
        if as_bool(stroke.get("enabled", False)):
            width = max(1, int(round(clamp_float(stroke.get("width", 1), 0.5, 20, 1))))
            rules.append(f"border:{width}px solid {_rgba_css(stroke.get('color', ACCENT), stroke.get('opacity', 100))};")
        else:
            rules.append("border:none;")

    radius = (borders.get("radius") or {})
    top_left = clamp_int(radius.get("tl", 0), 0, 400, 0)
    top_right = clamp_int(radius.get("tr", 0), 0, 400, 0)
    bottom_right = clamp_int(radius.get("br", 0), 0, 400, 0)
    bottom_left = clamp_int(radius.get("bl", 0), 0, 400, 0)
    if any((top_left, top_right, bottom_right, bottom_left)):
        rules.append(f"border-top-left-radius:{top_left}px;border-top-right-radius:{top_right}px;"
                     f"border-bottom-right-radius:{bottom_right}px;border-bottom-left-radius:{bottom_left}px;")

    text_style = spec.get("text_style") or {}
    padding = clamp_int(get_path(spec, "layout.padding", 0), 0, 80, 0)
    if padding:
        rules.append(f"padding:{padding}px;")
    rules.append(f"color:{_rgba_css(text_style.get('color', TEXT), text_style.get('opacity', 100))};")
    family = str(text_style.get("family") or "Exo 2")
    size = clamp_int(text_style.get("size", 11), 1, 200, 11)
    weight = 700 if as_bool(text_style.get("bold", False)) else 500
    italic = "italic" if as_bool(text_style.get("italic", False)) else "normal"
    rules.append(f'font: {italic} {weight} {size}pt "{family}";')
    if as_bool(text_style.get("underline", False)):
        rules.append("text-decoration: underline;")
    align = {"left": "left", "center": "center", "right": "right"}.get(
        str(text_style.get("align") or "left"), "left")
    rules.append(f"text-align:{align};")

    blocks = [f"{selector} {{ {''.join(rules)} }}"]
    states = spec.get("states") or {}
    state_map = {"hover": ":hover", "pressed": ":pressed", "focused": ":focus",
                 "disabled": ":disabled", "selected": ":checked"}
    for state_name, pseudo in state_map.items():
        state = states.get(state_name) or {}
        if not as_bool(state.get("enabled", False)):
            continue
        state_rules: list[str] = []
        if state.get("background"):
            state_rules.append(f"background-color:{_rgba_css(state['background'], 100)};")
        if state.get("border_color"):
            state_rules.append(f"border-color:{_rgba_css(state['border_color'], 100)};")
        if state.get("text_color"):
            state_rules.append(f"color:{_rgba_css(state['text_color'], 100)};")
        if state.get("opacity") not in (None, 100):
            col = qcolor(str(state.get("text_color") or text_style.get("color") or TEXT),
                         clamp_int(state.get("opacity"), 0, 100, 100) * 255 // 100)
            state_rules.append(f"color:rgba({col.red()},{col.green()},{col.blue()},{col.alpha()});")
        if state_rules:
            blocks.append(f"{selector}{pseudo} {{ {''.join(state_rules)} }}")
    return "\n".join(blocks)


def _clear_graphics_effect(widget: QWidget) -> None:
    try:
        if widget.graphicsEffect() is not None:
            widget.setGraphicsEffect(None)
    except RuntimeError:
        pass


def apply_effect_model(widget: QWidget, spec: dict, respect_existing: bool = True) -> str:
    """Put the element glow (or drop shadow) on the widget's single effect slot.

    Returns a short note describing what was applied, so the Studio can show the
    real behaviour instead of a fake success message.
    """
    if not widget_alive(widget):
        return ""
    glow = spec.get("glow") or {}
    shadow = spec.get("shadow") or {}
    glow_on = as_bool(glow.get("enabled", False))
    shadow_on = as_bool(shadow.get("enabled", False))

    if glow_on:
        try:
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsDropShadowEffect):
                effect = QGraphicsDropShadowEffect(widget)
                _clear_graphics_effect(widget)
                widget.setGraphicsEffect(effect)
            alpha = clamp_int(glow.get("opacity", 80), 0, 100, 80) * 255 // 100
            colour = qcolor(as_color(glow.get("color", ACCENT)), alpha)
            blur = clamp_int(glow.get("radius", 24), 0, 200, 24)
            spread = clamp_int(glow.get("spread", 0), 0, 40, 0)
            strength = clamp_float(glow.get("strength", 60), 0, 100, 60) / 100.0
            effect.setOffset(0, 0)
            effect.setColor(colour)
            effect.setBlurRadius(max(1.0, (blur + spread) * max(0.15, strength)))
            return "glow"
        except Exception as exc:  # pragma: no cover - defensive
            return f"glow failed: {exc}"

    if shadow_on:
        try:
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsDropShadowEffect):
                effect = QGraphicsDropShadowEffect(widget)
                _clear_graphics_effect(widget)
                widget.setGraphicsEffect(effect)
            alpha = clamp_int(shadow.get("opacity", 60), 0, 100, 60) * 255 // 100
            effect.setColor(qcolor(as_color(shadow.get("color", "#000000")), alpha))
            effect.setBlurRadius(max(0, clamp_int(shadow.get("blur", 18), 0, 200, 18)))
            effect.setOffset(clamp_int(shadow.get("dx", 0), -80, 80, 0),
                             clamp_int(shadow.get("dy", 6), -80, 80, 6))
            return "shadow"
        except Exception as exc:  # pragma: no cover - defensive
            return f"shadow failed: {exc}"

    _clear_graphics_effect(widget)
    return ""


def apply_widget_state(widget: QWidget, state: dict, live: bool = True) -> None:
    """Apply a *captured widget state* (the shape ``capture_widget_state`` returns).

    Used by the Studio's history replay and by the override loader so a runtime
    widget's geometry, font, text, tooltip, stylesheet, opacity and glow/shadow
    are restored exactly.  Deleting or re-parenting a live widget is never part of
    this path; a widget that disappeared mid-replay is simply skipped.
    """
    if not isinstance(state, dict) or not widget_alive(widget):
        return
    try:
        x = int(state.get("x", widget.x()))
        y = int(state.get("y", widget.y()))
        w = max(1, int(state.get("w", widget.width())))
        h = max(1, int(state.get("h", widget.height())))
        widget.setGeometry(x, y, w, h)
    except (TypeError, ValueError, RuntimeError):
        pass
    for attr, setter in (("min_w", widget.setMinimumWidth), ("min_h", widget.setMinimumHeight),
                         ("max_w", widget.setMaximumWidth), ("max_h", widget.setMaximumHeight)):
        try:
            setter(max(0, int(state.get(attr))))
        except (TypeError, ValueError, RuntimeError):
            pass

    try:
        if "visible" in state:
            widget.setVisible(as_bool(state["visible"], True))
        if "enabled" in state:
            widget.setEnabled(as_bool(state["enabled"], True))
        if "mouse_tracking" in state:
            widget.setMouseTracking(as_bool(state["mouse_tracking"], False))
        if "auto_fill_background" in state:
            widget.setAutoFillBackground(as_bool(state["auto_fill_background"], False))
        object_name = str(state.get("object_name") or "").strip()
        if object_name:
            widget.setObjectName(object_name)
    except (TypeError, RuntimeError):
        pass

    try:
        family = str(state.get("font_family") or widget.font().family())
        size = float(state.get("font_size") or widget.font().pointSizeF() or 9)
        font = QFont(family, max(1, int(round(size))))
        font.setBold(as_bool(state.get("font_bold", False)))
        font.setItalic(as_bool(state.get("font_italic", False)))
        widget.setFont(font)
    except (TypeError, ValueError, RuntimeError):
        pass

    try:
        if "tooltip" in state:
            widget.setToolTip(str(state.get("tooltip") or ""))
        if "text" in state and str(state.get("text") or ""):
            for setter_name in ("setText", "setPlainText"):
                setter = getattr(widget, setter_name, None)
                if callable(setter):
                    try:
                        setter(str(state.get("text")))
                        break
                    except Exception:
                        continue
        if "stylesheet" in state:
            widget.setStyleSheet(str(state.get("stylesheet") or ""))
    except (RuntimeError, TypeError, ValueError):
        pass

    # Effect slot: glow/shadow model first, opacity only when the slot is free.
    note = apply_effect_model(widget, {"glow": state.get("glow") or {},
                                      "shadow": state.get("shadow") or {}})
    try:
        opacity = max(0, min(100, int(state.get("opacity", 100))))
    except (TypeError, ValueError):
        opacity = 100
    if opacity < 100 and not note:
        try:
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(widget)
                widget.setGraphicsEffect(effect)
            effect.setOpacity(opacity / 100.0)
        except RuntimeError:
            pass
    _ = live


def apply_runtime_style(widget: QWidget, spec: dict) -> str:
    """Apply the safe subset of ``spec`` to a *runtime* J.A.R.V.I.S. widget.

    Safe means: real Qt mechanisms that cannot delete or re-parent live widgets.
    Geometry, visibility, tooltip, font, text and opacity are applied directly;
    background/borders/radius/typography/states become QSS; glow and shadow use
    the widget's graphics effect.
    Real PyQt6 limitations (documented, never faked):

    * a widget has one ``QGraphicsEffect`` slot, so glow and shadow cannot both
      use Qt's blur at the same time -- glow wins, and the Properties panel says
      so for runtime widgets;
    * a *painted* element stroke outline and text outline need custom painting,
      which is exactly what custom elements (``ElementWidget``) provide, so those
      controls are custom-element only;
    * rounded QSS corners do not clip child widgets.
    """
    if not widget_alive(widget):
        return ""
    notes: list[str] = []

    try:
        x = clamp_int(spec.get("x", widget.x()), -20000, 20000, widget.x())
        y = clamp_int(spec.get("y", widget.y()), -20000, 20000, widget.y())
        w = max(1, clamp_int(spec.get("w", widget.width()), 1, 20000, widget.width()))
        h = max(1, clamp_int(spec.get("h", widget.height()), 1, 20000, widget.height()))
        if (widget.x(), widget.y()) != (x, y):
            widget.move(x, y)
        if (widget.width(), widget.height()) != (w, h):
            widget.resize(w, h)
    except RuntimeError:
        return "widget disappeared"

    try:
        widget.setVisible(as_bool(spec.get("visible", True), True) and not as_bool(spec.get("hidden", False)))
        widget.setEnabled(as_bool(spec.get("enabled", True), True))
        tooltip = str(spec.get("tooltip") or get_path(spec, "interaction.tooltip", "") or "")
        widget.setToolTip(tooltip)
        object_name = str(spec.get("object_name") or "").strip()
        if object_name:
            widget.setObjectName(object_name)
    except RuntimeError:
        return "widget disappeared"

    text_style = spec.get("text_style") or {}
    try:
        font = QFont(str(text_style.get("family") or widget.font().family()))
        font.setPointSize(max(1, clamp_int(text_style.get("size", 11), 1, 200, 11)))
        font.setBold(as_bool(text_style.get("bold", False)))
        font.setItalic(as_bool(text_style.get("italic", False)))
        font.setUnderline(as_bool(text_style.get("underline", False)))
        spacing = clamp_float(text_style.get("letter_spacing", 0.0), -20.0, 40.0, 0.0)
        if spacing:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
        widget.setFont(font)
    except (RuntimeError, TypeError, ValueError):
        pass

    if "text" in spec:
        for setter in ("setText", "setPlainText"):
            method = getattr(widget, setter, None)
            if callable(method):
                try:
                    method(str(spec.get("text") or ""))
                    break
                except Exception:
                    continue

    cursor_name = str(get_path(spec, "interaction.cursor", "arrow") or "arrow")
    try:
        widget.setCursor(CURSOR_MAP.get(cursor_name, Qt.CursorShape.ArrowCursor))
    except (RuntimeError, TypeError):
        pass

    try:
        widget.setStyleSheet(build_qss(spec))
    except (RuntimeError, TypeError):
        notes.append("stylesheet rejected")

    note = apply_effect_model(widget, spec)
    if note:
        notes.append(note)

    opacity = clamp_int(spec.get("opacity", 100), 0, 100, 100)
    if opacity < 100 and note in ("", "shadow", "glow"):
        # The effect slot may already hold the glow/shadow, in which case alpha
        # has to come from the QSS colours above; say so instead of silently
        # dropping the value.
        if note:
            notes.append("opacity applied through QSS colours (Qt allows one graphics effect)")
        else:
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(widget)
                try:
                    widget.setGraphicsEffect(effect)
                except RuntimeError:
                    return "widget disappeared"
            effect.setOpacity(opacity / 100.0)
    return ", ".join(notes)


CURSOR_MAP = {name: value for name, value in CURSOR_NAMES.items()}


# ---------------------------------------------------------------------------
# ElementWidget
# ---------------------------------------------------------------------------
class ElementWidget(QWidget):
    """A real Qt widget that paints a complete layout element.

    The widget owns no timers and starts no animations by itself: the Studio's
    ``AnimationManager`` drives motion, and repaints happen only when something
    actually changed.  ``paintEvent`` never calls ``update()``/``repaint()`` and
    never raises: a failure keeps the last good frame instead of taking the
    application down (the white-screen bug this design exists to prevent).
    """

    geometryCommitted = pyqtSignal(object)
    picked = pyqtSignal(object)
    elementActivated = pyqtSignal(object)

    def __init__(self, spec: dict | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._spec = normalize_element(spec or {}, None)
        self._version = 0
        self._static: QImage | None = None
        self._static_key: tuple = ()
        self._paint_failed = False
        self._hover = False
        self._pressed = False
        self._focused = False
        self._enabled_state = True
        self._press_pos = QPoint()
        self._drag_origin = QPoint()
        self._dragging = False
        self.script_runner = None            # set by ScriptManager
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoFillBackground(False)
        self.apply_spec(self._spec, force=True)

    # -- data ------------------------------------------------------------
    @property
    def spec(self) -> dict:
        return self._spec

    def element_name(self) -> str:
        return str(self._spec.get("name") or "")

    def element_type(self) -> str:
        return str(self._spec.get("type") or self._spec.get("kind") or "Panel")

    def supports(self, feature: str) -> bool:
        kind = self.element_type()
        if feature in ("rotation",):
            return kind in ROTATABLE_TYPES
        if feature in ("text", "text_stroke", "typography"):
            return kind in TEXT_TYPES
        if feature in ("image", "crop"):
            return kind in IMAGE_TYPES
        if feature in ("effects", "stroke", "borders", "glow", "shadow", "gradient", "states"):
            return True
        return True

    # -- application -----------------------------------------------------
    def set_spec_property(self, path: str, value, apply_now: bool = True) -> None:
        set_path(self._spec, path, value)
        if apply_now:
            self.apply_spec(self._spec)

    def apply_spec(self, spec: dict, force: bool = False) -> None:
        """Install ``spec`` on the widget (geometry, style, effect, image)."""
        if isinstance(spec, dict):
            self._spec = spec
        self._version += 1
        spec = self._spec

        geometry = QRect(clamp_int(spec.get("x", self.x()), -20000, 20000, self.x()),
                         clamp_int(spec.get("y", self.y()), -20000, 20000, self.y()),
                         max(1, clamp_int(spec.get("w", self.width()), 1, 20000, self.width())),
                         max(1, clamp_int(spec.get("h", self.height()), 1, 20000, self.height())))
        try:
            self.setMinimumSize(max(0, clamp_int(spec.get("min_w", 0), 0, 20000, 0)),
                                max(0, clamp_int(spec.get("min_h", 0), 0, 20000, 0)))
            self.setMaximumSize(clamp_int(spec.get("max_w", 16777215), 0, 16777215, 16777215),
                                clamp_int(spec.get("max_h", 16777215), 0, 16777215, 16777215))
        except (TypeError, ValueError, RuntimeError):
            pass
        if force or self.geometry() != geometry:
            try:
                self.setGeometry(geometry)
            except (TypeError, RuntimeError):
                pass

        hidden = as_bool(spec.get("hidden", False))
        try:
            self.setVisible(as_bool(spec.get("visible", True), True) and not hidden)
            self.setEnabled(as_bool(spec.get("enabled", True), True))
        except RuntimeError:
            return
        self._enabled_state = as_bool(spec.get("enabled", True), True)

        object_name = str(spec.get("object_name") or "").strip()
        if object_name and object_name != self.objectName():
            try:
                self.setObjectName(object_name)
            except RuntimeError:
                return
        tooltip = str(spec.get("tooltip") or get_path(spec, "interaction.tooltip", "") or "")
        try:
            self.setToolTip(tooltip)
            self.setCursor(CURSOR_MAP.get(str(get_path(spec, "interaction.cursor", "arrow")),
                                          Qt.CursorShape.ArrowCursor))
        except (RuntimeError, TypeError):
            pass

        apply_effect_model(self, spec)
        self._invalidate()

    def set_image(self, path: str | Path, asset_name: str = "") -> None:
        self._spec.setdefault("image", {})
        self._spec["image"]["path"] = str(path)
        if asset_name:
            self._spec["image"]["asset"] = str(asset_name)
        self._version += 1
        self._invalidate()

    def replace_image(self, path: str | Path, asset_name: str = "") -> None:
        """Swap only the asset, keeping every other property (TXT section 4)."""
        self.set_image(path, asset_name)

    def image_path(self) -> str:
        return str(get_path(self._spec, "image.path", "") or "")

    def element_json(self) -> dict:
        return normalize_element(copy.deepcopy(self._spec), self.element_name())

    # -- state -----------------------------------------------------------
    def state_key(self) -> tuple:
        return (self._hover, self._pressed, self._focused, self.isEnabled(),
                as_bool(get_path(self._spec, "locked", False)))

    def _active_state(self) -> dict:
        states = self._spec.get("states") or {}
        order = []
        if not self._enabled_state:
            order.append("disabled")
        if self._pressed:
            order.append("pressed")
        elif self._hover:
            order.append("hover")
        if self._focused:
            order.append("focused")
        merged: dict = {}
        for name in reversed(order):
            state = states.get(name) or {}
            if as_bool(state.get("enabled", False)):
                for key in ("background", "border_color", "text_color", "opacity", "glow_radius"):
                    if state.get(key) not in (None, ""):
                        merged[key] = state[key]
        return merged

    def _invalidate(self) -> None:
        self._static = None
        try:
            self.update()
        except RuntimeError:
            pass

    # -- painting --------------------------------------------------------
    def _corner_radius(self) -> tuple[float, float, float, float]:
        radius = get_path(self._spec, "borders.radius", {}) or {}
        return (float(clamp_int(radius.get("tl", 0), 0, 400, 0)),
                float(clamp_int(radius.get("tr", 0), 0, 400, 0)),
                float(clamp_int(radius.get("br", 0), 0, 400, 0)),
                float(clamp_int(radius.get("bl", 0), 0, 400, 0)))

    def _shape_path(self, rect: QRectF, radius: tuple[float, float, float, float]) -> QPainterPath:
        tl, tr, br, bl = radius
        max_radius = max(0.0, min(rect.width(), rect.height()) / 2.0)
        tl, tr, br, bl = (min(v, max_radius) for v in (tl, tr, br, bl))
        path = QPainterPath()
        path.moveTo(rect.left() + tl, rect.top())
        path.lineTo(rect.right() - tr, rect.top())
        if tr:
            path.arcTo(QRectF(rect.right() - 2 * tr, rect.top(), 2 * tr, 2 * tr), 90, -90)
        path.lineTo(rect.right(), rect.bottom() - br)
        if br:
            path.arcTo(QRectF(rect.right() - 2 * br, rect.bottom() - 2 * br, 2 * br, 2 * br), 0, -90)
        path.lineTo(rect.left() + bl, rect.bottom())
        if bl:
            path.arcTo(QRectF(rect.left(), rect.bottom() - 2 * bl, 2 * bl, 2 * bl), 270, -90)
        path.lineTo(rect.left(), rect.top() + tl)
        if tl:
            path.arcTo(QRectF(rect.left(), rect.top(), 2 * tl, 2 * tl), 180, -90)
        path.closeSubpath()
        return path

    def _ensure_static(self) -> QImage | None:
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            return None
        key = (width, height, self._version, tuple(sorted(self._active_state().items(), key=str)))
        if self._static is not None and key == self._static_key:
            return self._static
        try:
            image = self._build_static(width, height)
        except Exception:
            self._paint_failed = True
            image = self._static            # keep the last good frame
        if image is not None:
            self._static = image
            self._static_key = key
            self._paint_failed = False
        return self._static

    def _build_static(self, width: int, height: int) -> QImage:
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter()
        if not painter.begin(image):
            return image
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            self._paint_layers(painter, QRectF(0.0, 0.0, float(width), float(height)))
        finally:
            if painter.isActive():
                painter.end()

        effects = self._spec.get("effects") or []
        if effects and any(as_bool(e.get("enabled", True), True) for e in effects):
            return apply_effect_stack(image, effects)
        return image

    # -- layer painting --------------------------------------------------
    def _paint_layers(self, painter: QPainter, rect: QRectF) -> None:
        spec = self._spec
        state = self._active_state()
        stroke = spec.get("stroke") or {}
        stroke_width = clamp_float(stroke.get("width", 0), 0.0, 20.0, 0.0) if as_bool(stroke.get("enabled", False)) else 0.0
        inset = stroke_width / 2.0
        shape_rect = rect.adjusted(inset, inset, -inset, -inset)
        radius = self._corner_radius()
        path = self._shape_path(shape_rect, radius)

        # 1. shadow painted into the element's own layer (used when the single
        #    Qt effect slot is busy with a glow).
        glow_on = as_bool(get_path(spec, "glow.enabled", False))
        shadow = spec.get("shadow") or {}
        if as_bool(shadow.get("enabled", False)) and glow_on:
            self._paint_soft_shadow(painter, shape_rect, radius, shadow)
        elif as_bool(shadow.get("enabled", False)):
            self._paint_soft_shadow(painter, shape_rect, radius, shadow, subtle=True)

        # 2. background: image-as-background, gradient, or solid colour.
        image_block = spec.get("image") or {}
        background = spec.get("background") or {}
        state_background = state.get("background")
        painter.save()
        painter.setClipPath(path)
        drew_background = False
        if as_bool(image_block.get("is_background", False)) and str(image_block.get("path") or ""):
            drew_background = self._paint_image(painter, shape_rect, image_block, as_background=True)
        if not drew_background:
            opacity = clamp_int(background.get("opacity", 100), 0, 100, 100) / 100.0
            if opacity > 0:
                colour = as_color(state_background or background.get("color", BG), BG)
                if as_bool(get_path(background, "gradient.enabled", False)) and not state_background:
                    painter.setOpacity(opacity)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(self._gradient(shape_rect, background)))
                    painter.drawPath(path)
                    painter.setOpacity(1.0)
                else:
                    fill = qcolor(colour, int(255 * opacity))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QBrush(fill))
                    painter.drawPath(path)
        # 3. overlay layer (dark / light / transparent).
        overlay = background.get("overlay") or {}
        if as_bool(overlay.get("enabled", False)):
            kind = str(overlay.get("kind") or "dark")
            colour = as_color(overlay.get("color", "#000000"), "#000000")
            opacity = clamp_int(overlay.get("opacity", 35), 0, 100, 35) / 100.0
            if kind == "light":
                colour = "#ffffff"
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(qcolor(colour, int(255 * opacity))))
            painter.drawPath(path)

        # 4. content image (not the background variant).
        if not as_bool(image_block.get("is_background", False)) or not drew_background:
            if str(image_block.get("path") or ""):
                self._paint_image(painter, shape_rect, image_block, as_background=False)
        painter.restore()

        # 5. per-side borders.
        self._paint_borders(painter, shape_rect, radius)

        # 6. element stroke (independent outline).
        if stroke_width > 0:
            pen = QPen(qcolor(state.get("border_color") or stroke.get("color", ACCENT),
                              clamp_int(stroke.get("opacity", 100), 0, 100, 100) * 255 // 100))
            pen.setWidthF(stroke_width)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        # 7. text.
        self._paint_text(painter, shape_rect, state)

    def _gradient(self, rect: QRectF, background: dict):
        block = background.get("gradient") or {}
        stops = block.get("stops") or [[0.0, ACCENT], [1.0, BG_DEEP]]
        angle = clamp_int(block.get("angle", 90), 0, 360, 90)
        if str(block.get("kind") or "linear") == "radial":
            gradient = QRadialGradient(rect.center(), max(rect.width(), rect.height()) / 1.4)
        else:
            rad = math.radians(float(angle))
            dx, dy = math.cos(rad), math.sin(rad)
            gradient = QLinearGradient(
                rect.center().x() - dx * rect.width() / 2.0,
                rect.center().y() + dy * rect.height() / 2.0,
                rect.center().x() + dx * rect.width() / 2.0,
                rect.center().y() - dy * rect.height() / 2.0,
            )
        for stop in stops:
            try:
                gradient.setColorAt(max(0.0, min(1.0, float(stop[0]))), qcolor(as_color(stop[1], ACCENT)))
            except (TypeError, ValueError, IndexError):
                continue
        return gradient

    def _paint_soft_shadow(self, painter: QPainter, rect: QRectF, radius, shadow: dict,
                           subtle: bool = False) -> None:
        colour = qcolor(as_color(shadow.get("color", "#000000")))
        base_alpha = clamp_int(shadow.get("opacity", 45), 0, 100, 45) * 255 // 100
        blur = clamp_int(shadow.get("blur", 16), 0, 64, 16)
        dx = clamp_int(shadow.get("dx", 0), -40, 40, 0)
        dy = clamp_int(shadow.get("dy", 6), -40, 40, 6)
        layers = max(1, min(6, (blur // 4) + 1))
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index in range(layers, 0, -1):
            spread = float(index * max(1.0, blur / float(layers))) * (0.5 if subtle else 1.0)
            alpha = int(base_alpha * (0.16 if subtle else 0.22) * (1.0 - index / float(layers + 1)))
            if alpha <= 0:
                continue
            pen_colour = QColor(colour)
            pen_colour.setAlpha(max(1, alpha))
            pen = QPen(pen_colour)
            pen.setWidthF(max(1.0, spread))
            painter.setPen(pen)
            painter.drawPath(self._shape_path(rect.adjusted(dx, dy, dx, dy), radius))
        painter.restore()

    def _paint_borders(self, painter: QPainter, rect: QRectF, radius) -> None:
        borders = self._spec.get("borders") or {}
        style_map = {
            "Solid": Qt.PenStyle.SolidLine,
            "Dashed": Qt.PenStyle.DashLine,
            "Dotted": Qt.PenStyle.DotLine,
            "Double": Qt.PenStyle.SolidLine,
        }
        for side in ("top", "right", "bottom", "left"):
            block = borders.get(side) or {}
            width = clamp_int(block.get("width", 0), 0, 60, 0)
            if width <= 0:
                continue
            pen = QPen(qcolor(as_color(block.get("color", ACCENT), ACCENT),
                              clamp_int(block.get("opacity", 100), 0, 100, 100) * 255 // 100))
            pen.setWidthF(float(width))
            pen.setStyle(style_map.get(str(block.get("style", "Solid")), Qt.PenStyle.SolidLine))
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            offset = width / 2.0
            if side == "top":
                painter.drawLine(QPointF(rect.left(), rect.top() + offset), QPointF(rect.right(), rect.top() + offset))
            elif side == "bottom":
                painter.drawLine(QPointF(rect.left(), rect.bottom() - offset), QPointF(rect.right(), rect.bottom() - offset))
            elif side == "left":
                painter.drawLine(QPointF(rect.left() + offset, rect.top()), QPointF(rect.left() + offset, rect.bottom()))
            else:
                painter.drawLine(QPointF(rect.right() - offset, rect.top()), QPointF(rect.right() - offset, rect.bottom()))
            if str(block.get("style")) == "Double":
                inner = pen.widthF() / 2.0
                if side in ("top", "bottom"):
                    y = rect.top() + offset + inner if side == "top" else rect.bottom() - offset - inner
                    painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                else:
                    x = rect.left() + offset + inner if side == "left" else rect.right() - offset - inner
                    painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))

    def _paint_image(self, painter: QPainter, rect: QRectF, block: dict, as_background: bool) -> bool:
        path = str(block.get("path") or "")
        if not path:
            return False
        fit = str(block.get("fit") or "Contain")
        if not as_background and fit == "Cover":
            fit = "Contain"          # cover only makes sense for a background
        if not as_background and fit == "Tile":
            fit = "Center"
        crop = block.get("crop") or [0, 0, 0, 0]
        source = IMAGE_CACHE.pixmap(path)
        if source is None:
            return False
        opacity = clamp_int(block.get("opacity", 100), 0, 100, 100) / 100.0
        if opacity <= 0:
            return False

        left, top, right, bottom = (max(0, int(v)) for v in (list(crop) + [0, 0, 0, 0])[:4])
        crop_rect = QRect(left, top,
                          max(1, source.width() - left - right),
                          max(1, source.height() - top - bottom))
        src_pix = source.copy(crop_rect)

        painter.save()
        painter.setOpacity(opacity)

        target = rect
        if fit == "Stretch":
            dest = QRectF(target)
        elif fit == "Tile":
            dest = None
            natural_w = max(1.0, float(src_pix.width()))
            natural_h = max(1.0, float(src_pix.height()))
            rows = int(target.height() // natural_h) + 2
            cols = int(target.width() // natural_w) + 2
            for row in range(max(1, rows)):
                for col in range(max(1, cols)):
                    painter.drawPixmap(
                        QRectF(target.left() + col * natural_w, target.top() + row * natural_h,
                               natural_w, natural_h), src_pix,
                        QRectF(0, 0, float(src_pix.width()), float(src_pix.height())))
        else:
            scale_w = target.width() / max(1.0, float(src_pix.width()))
            scale_h = target.height() / max(1.0, float(src_pix.height()))
            if fit == "Cover":
                scale = max(scale_w, scale_h)
            elif fit == "Center":
                scale = 1.0
            else:                                   # Contain / Fit
                scale = min(scale_w, scale_h)
            width = src_pix.width() * scale
            height = src_pix.height() * scale
            align_h = str(block.get("align_h") or "center")
            align_v = str(block.get("align_v") or "middle")
            x = target.left() + (target.width() - width) / 2.0
            y = target.top() + (target.height() - height) / 2.0
            if align_h == "left":
                x = target.left()
            elif align_h == "right":
                x = target.right() - width
            if align_v == "top":
                y = target.top()
            elif align_v == "bottom":
                y = target.bottom() - height
            dest = QRectF(x, y, width, height)

        if dest is not None:
            painter.drawPixmap(dest, src_pix, QRectF(0, 0, float(src_pix.width()), float(src_pix.height())))

        tint = str(block.get("tint") or "")
        strength = clamp_int(block.get("tint_strength", 0), 0, 100, 0)
        if tint and strength > 0:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(qcolor(as_color(tint, ACCENT), int(255 * strength / 100.0))))
            painter.drawRect(rect)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.restore()
        return True

    def _paint_text(self, painter: QPainter, rect: QRectF, state: dict) -> None:
        if not self.supports("text"):
            return
        text = str(self._spec.get("text") or "")
        if not text:
            return
        style = self._spec.get("text_style") or {}
        if not text.strip() and not as_bool(style.get("wrap", True), True):
            return
        colour = qcolor(as_color(state.get("text_color") or style.get("color", TEXT), TEXT),
                        clamp_int(style.get("opacity", 100), 0, 100, 100) * 255 // 100)
        font = QFont(str(style.get("family") or "Exo 2"))
        font.setPointSize(max(1, clamp_int(style.get("size", 11), 1, 200, 11)))
        font.setBold(as_bool(style.get("bold", False)))
        font.setItalic(as_bool(style.get("italic", False)))
        font.setUnderline(as_bool(style.get("underline", False)))
        spacing = clamp_float(style.get("letter_spacing", 0.0), -20.0, 40.0, 0.0)
        if spacing:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)

        padding = clamp_int(get_path(self._spec, "layout.padding", 0), 0, 80, 0)
        text_rect = rect.adjusted(6 + padding, 4 + padding, -6 - padding, -4 - padding)
        if text_rect.width() <= 2 or text_rect.height() <= 2:
            return

        wrap = as_bool(style.get("wrap", True), True)
        align = str(style.get("align") or "left")
        lines, total_height = self._layout_text(text, font, text_rect.width(), wrap)
        valign = str(style.get("v_align") or "middle")
        if valign == "top":
            y = text_rect.top()
        elif valign == "bottom":
            y = text_rect.bottom() - total_height
        else:
            y = text_rect.top() + (text_rect.height() - total_height) / 2.0

        metrics = QFontMetricsF(font)
        path = QPainterPath()
        for line in lines:
            line_width = metrics.horizontalAdvance(line)
            if align == "center":
                x = text_rect.left() + (text_rect.width() - line_width) / 2.0
            elif align == "right":
                x = text_rect.right() - line_width
            else:
                x = text_rect.left()
            baseline = y + metrics.ascent()
            if line:
                path.addText(QPointF(x, baseline), font, line)
            y += metrics.lineSpacing()

        # text shadow / glow are painted from the same path, so they follow the
        # real glyph outlines (a QLabel cannot do any of this).
        shadow = style.get("shadow") or {}
        if as_bool(shadow.get("enabled", False)):
            colour_shadow = qcolor(as_color(shadow.get("color", "#000000"), "#000000"),
                                   clamp_int(shadow.get("opacity", 70), 0, 100, 70) * 255 // 100)
            painter.save()
            painter.translate(clamp_int(shadow.get("dx", 1), -20, 20, 1),
                              clamp_int(shadow.get("dy", 1), -20, 20, 1))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(colour_shadow))
            painter.drawPath(path)
            painter.restore()

        glow = style.get("glow") or {}
        if as_bool(glow.get("enabled", False)):
            glow_colour = qcolor(as_color(glow.get("color", ACCENT), ACCENT))
            radius = clamp_int(glow.get("radius", 8), 0, 40, 8)
            base_alpha = clamp_int(glow.get("opacity", 80), 0, 100, 80)
            painter.save()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for step in range(max(1, radius // 3), 0, -1):
                pen = QPen(glow_colour)
                pen.setWidthF(float(step * 2))
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                glow_colour.setAlpha(max(1, int(base_alpha * 2.55 * (1.0 - step / (radius / 3.0 + 1)))))
                pen.setColor(glow_colour)
                painter.setPen(pen)
                painter.drawPath(path)
            painter.restore()

        stroke = style.get("stroke") or {}
        if as_bool(stroke.get("enabled", False)):
            width = clamp_float(stroke.get("width", 2.0), 0.4, 20.0, 2.0)
            pen = QPen(qcolor(as_color(stroke.get("color", ACCENT_BRIGHT), ACCENT_BRIGHT),
                              clamp_int(stroke.get("opacity", 100), 0, 100, 100) * 255 // 100))
            pen.setWidthF(width * 2.0)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawPath(path)

    @staticmethod
    def _layout_text(text: str, font: QFont, width: float, wrap: bool) -> tuple[list[str], float]:
        metrics = QFontMetricsF(font)
        if not wrap or width <= 0:
            return text.split("\n"), max(metrics.lineSpacing(), metrics.lineSpacing() * len(text.split("\n")))
        lines: list[str] = []
        for paragraph in str(text).split("\n"):
            words = paragraph.split(" ")
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and metrics.horizontalAdvance(candidate) > width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        return lines, max(metrics.lineSpacing(), metrics.lineSpacing() * max(1, len(lines)))

    def paintEvent(self, event):  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            image = self._ensure_static()
            if image is None:
                return
            spec = self._spec
            rotation = clamp_float(spec.get("rotation", 0.0), -360.0, 360.0, 0.0)
            opacity = clamp_int(spec.get("opacity", 100), 0, 100, 100) / 100.0
            opacity *= clamp_int(self._active_state().get("opacity", 100), 0, 100, 100) / 100.0

            painter.save()
            try:
                if rotation and self.supports("rotation"):
                    center = spec.get("rotation_center") or [0.5, 0.5]
                    cx = float(self.width()) * clamp_float(center[0], -2, 3, 0.5)
                    cy = float(self.height()) * clamp_float(center[1], -2, 3, 0.5)
                    painter.translate(cx, cy)
                    painter.rotate(rotation)
                    painter.translate(-cx, -cy)
                painter.setOpacity(max(0.0, min(1.0, opacity)))
                painter.drawImage(QPoint(0, 0), image)
            finally:
                painter.restore()

            if self._paint_failed:
                painter.setPen(QPen(qcolor("#ff4455", 180), 1.0, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        except Exception:
            # Never let a paint failure escape: an exception here aborts Qt and
            # is exactly what produced the old white-screen crash.
            self._paint_failed = True
        finally:
            if painter.isActive():
                painter.end()

    # -- interaction -----------------------------------------------------
    def enterEvent(self, event):  # noqa: N802
        self._hover = True
        if self._state_fields_present():
            self._invalidate()
        else:
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hover = False
        self._pressed = False
        if self._state_fields_present():
            self._invalidate()
        else:
            self.update()
        super().leaveEvent(event)

    def _state_fields_present(self) -> bool:
        states = self._spec.get("states") or {}
        return any(as_bool((states.get(name) or {}).get("enabled", False))
                   for name in ("hover", "pressed", "focused", "disabled", "selected"))

    def focusInEvent(self, event):  # noqa: N802
        self._focused = True
        self._invalidate()
        super().focusInEvent(event)

    def focusOutEvent(self, event):  # noqa: N802
        self._focused = False
        self._invalidate()
        super().focusOutEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self._press_pos = event.globalPosition().toPoint()
            self._drag_origin = QPoint(self.spec.get("x", self.x()), self.spec.get("y", self.y()))
            self._dragging = False
            self._invalidate()
            self.picked.emit(self)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._pressed and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._press_pos
            if delta.manhattanLength() > 3:
                self._dragging = True
            if self._dragging:
                self.move(self._drag_origin + delta)
                self._spec["x"] = self.x()
                self._spec["y"] = self.y()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._dragging
            self._pressed = False
            self._dragging = False
            self._invalidate()
            if was_dragging:
                self.geometryCommitted.emit(self)
            else:
                self.elementActivated.emit(self)
                runner = self.script_runner
                if callable(runner):
                    try:
                        runner(self, "click")
                    except Exception:
                        pass
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        self._invalidate()
        super().resizeEvent(event)


__all__ = [
    "SCHEMA_VERSION",
    "ELEMENT_TYPES",
    "FIT_MODES",
    "BORDER_STYLES",
    "ALIGN_H",
    "ALIGN_V",
    "EFFECT_KINDS",
    "EFFECT_SCHEMA",
    "HUD_PRESETS",
    "TEXT_PRESETS",
    "ANIMATION_NAMES",
    "CURSOR_NAMES",
    "LAYOUT_DEFAULTS",
    "ElementWidget",
    "ImageCache",
    "AssetStore",
    "IMAGE_CACHE",
    "IMAGE_SUFFIXES",
    "default_spec",
    "normalize_element",
    "load_layout",
    "save_layout",
    "elements_from_layout",
    "elements_to_layout",
    "apply_preset",
    "deep_merge",
    "get_path",
    "set_path",
    "widget_alive",
    "safe_object_name",
    "class_simple_name",
    "is_element_widget",
    "as_bool",
    "as_color",
    "clamp_int",
    "clamp_float",
    "qcolor",
    "gradient_qss",
    "build_qss",
    "apply_runtime_style",
    "apply_widget_state",
    "apply_effect_model",
    "apply_effect_stack",
    "effects_supported",
]
