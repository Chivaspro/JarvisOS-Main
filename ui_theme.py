"""J.A.R.V.I.S centralized design system — one token table, three themes.

A precise, restrained holographic language: deep vacuum-black surfaces, a single
arc-reactor ice-cyan as the working color, and a sparing Stark repulsor-gold as
the one signature accent reserved for the live/active state. Strokes are thin and
consistent; glow is used as punctuation, never as wallpaper. Shared by every panel.

Three themes ship, and every one of them is *this same vocabulary*:

``hud``
    The Stark holographic HUD: vacuum-black glass, hairline cyan strokes, hard
    cut corners, no shadows.
``soft_dark``
    Modern soft UI on ink: layered slate cards, wide radii, low-contrast
    strokes, one soft shadow per card.
``soft_light``
    The same soft UI in daylight: near-white field, white cards, a deep cyan
    working color (the fill stays readable with white text), grey-blue hairlines
    and shadows instead of borders.

Design rules encoded here so the whole UI stays coherent:
  * One primary (the accent), one signature accent (GOLD), a cold neutral ramp,
    three status colors. Nothing else earns a slot.
  * Three radii only (LG / MD / SM) *per theme* — the HUD is crisp, the soft
    themes are rounder — and borders come in three weights (LINE / SOFT / FAINT).
  * Type: Orbitron for display/headings, Rajdhani for UI/body, Share Tech Mono
    for logs.

Nothing outside this file decides a colour, a radius or a shadow. A surface
imports a *name* (``ACCENT``, ``SURFACE``, ``TEXT_DIM`` …) and this module decides
what that name means for the active theme; ``set_theme()`` swaps the meaning in
place, and ``retheme_widget_stylesheets()`` re-writes the stylesheets that already
embedded the previous values, so a switch is visible without a restart.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Sequence

# ── Token helpers ───────────────────────────────────────────────────────────
# Qt's QColor()/QPen() cannot parse rgba() strings (PyQt6 rejects them), so any
# token that reaches painter code has to be flattened to an opaque hex first.
# These helpers are what lets the whole HUD share one token set: the translucent
# QSS values and the opaque painter values are derived, never declared twice.


def _rgba_parts(color: str) -> tuple[int, int, int, int]:
    """Parse '#rgb', '#rrggbb' or '#rrggbbaa' -> (r, g, b, a). Never raises."""
    s = str(color or "").strip()
    if s.lower().startswith("rgba(") or s.lower().startswith("rgb("):
        body = s[s.index("(") + 1:s.rindex(")")]
        parts = [p.strip() for p in body.split(",")]
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            return (0, 0, 0, 255)
        while len(vals) < 4:
            vals.append(255.0)
        return (int(vals[0]), int(vals[1]), int(vals[2]), int(vals[3]))
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) not in (6, 8):
        return (0, 0, 0, 255)
    try:
        r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
        a = int(s[6:8], 16) if len(s) == 8 else 255
    except ValueError:
        return (0, 0, 0, 255)
    return (r, g, b, a)


def rgba(color: str, alpha: int) -> str:
    """The same hue at a given alpha -> a QSS 'rgba(...)' colour."""
    r, g, b, _a = _rgba_parts(color)
    return f"rgba({r}, {g}, {b}, {int(alpha)})"


def hex_color(color: str) -> str:
    """Opaque '#rrggbb' of any token — the form QColor() accepts."""
    r, g, b, _a = _rgba_parts(color)
    return f"#{r:02x}{g:02x}{b:02x}"


def mix(color: str, other: str, amount: float) -> str:
    """Blend `amount` of `color` into `other` -> opaque '#rrggbb'."""
    r, g, b, _a = _rgba_parts(color)
    orr, og, ob, _b = _rgba_parts(other)
    k = max(0.0, min(1.0, float(amount)))
    return "#{:02x}{:02x}{:02x}".format(
        int(r * k + orr * (1 - k) + 0.5),
        int(g * k + og * (1 - k) + 0.5),
        int(b * k + ob * (1 - k) + 0.5))


def shade(color: str, factor: float) -> str:
    """Darken (factor < 1) or lighten (factor > 1) a token, clamped."""
    r, g, b, _a = _rgba_parts(color)
    f = max(0.0, float(factor))
    return "#{:02x}{:02x}{:02x}".format(
        *[max(0, min(255, int(c * f + 0.5))) for c in (r, g, b)])


def flatten(color: str, alpha: int | None = None, bg: str | None = None) -> str:
    """Composite a translucent token onto a background -> opaque '#rrggbb'.

    This is how one translucent token serves both QSS (needs the alpha) and
    painter code (needs a solid colour).
    """
    r, g, b, a = _rgba_parts(color)
    if alpha is not None:
        a = int(alpha)
    br, bg_g, bb, _ = _rgba_parts(BG if bg is None else bg)
    k = max(0, min(255, a)) / 255.0
    return "#{:02x}{:02x}{:02x}".format(
        int(r * k + br * (1 - k) + 0.5),
        int(g * k + bg_g * (1 - k) + 0.5),
        int(b * k + bb * (1 - k) + 0.5))


def qcolor(color: str, alpha: int | None = None):
    """QColor for any token, tolerating rgba() and '#rrggbbaa'.

    Imported lazily so this module stays Qt-free for string-only callers.
    """
    from PyQt6.QtGui import QColor
    c = QColor(hex_color(color))
    _r, _g, _b, a = _rgba_parts(color)
    c.setAlpha(int(a if alpha is None else alpha))
    return c


def contrast_ratio(color: str, other: str) -> float:
    """WCAG relative-luminance contrast between two opaque-ish tokens."""
    def _lum(c: str) -> float:
        r, g, b, _a = _rgba_parts(c)
        chans = []
        for v in (r, g, b):
            s = v / 255.0
            chans.append(s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4)
        return 0.2126 * chans[0] + 0.7152 * chans[1] + 0.0722 * chans[2]

    a, b = _lum(hex_color(color)), _lum(hex_color(other))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ── Theme seeds ─────────────────────────────────────────────────────────────
# One small table per theme, and `_derive_theme()` below turns a seed into the
# complete token vocabulary. A seed therefore only says what is *different*
# about a theme — the working colour, the field, how glassy the surfaces are,
# how round the corners are and how strong the shadows are — and the 60+ names
# the HUD uses are derived from it, identically, in all three themes.
#
# Adding a fourth theme means adding one dict here; nothing else changes, and
# `tests/ui/test_design_tokens.py` proves the new theme carries every token the
# others do.

_THEME_SPECS: dict[str, dict] = {
    # ── HUD — the shipped Stark holographic look, unchanged ────────────────
    "hud": {
        "label": "HUD",
        "summary": "Stark holographic: black glass, hairline cyan, hard corners.",
        "mode": "dark",
        "accent": "#00d4ff", "accent_bright": "#8ceaff", "accent_deep": "#007a99",
        "accent_ink": "#001f2e", "accent_text": "#8ceaff",
        "accent_gradient_start": "#8ceaff", "on_accent": "#04121a",
        "gold": "#f4b24a", "gold_deep": "#b87d1e", "gold_soft": "#ffd48a",
        "bg": "#02060c", "bg_deep_factor": 0.70, "bias": 0.06,
        "light_mix": "#00d4ff", "inset_mix": "#00d4ff",
        "surface_k": 0.075, "surface_alt_k": 0.040, "surface_inset_k": 0.050,
        "panel_alpha": 240, "panel_soft_alpha": 92, "card_alpha": 168,
        "card_hi_alpha": 190, "header_alpha": 150,
        "text": "#daf5ff", "text_bright": "#f0fdff", "text_mid": "#a6e9ff",
        "text_dim": "#6ba7bb", "text_faint": "#547f92",
        "menu_alpha": 252, "track_alpha": 180,
        "stroke_alphas": (120, 70, 46), "stroke_width": 1,
        "radii": (12, 9, 6), "corner_cut": 12, "grid_alpha": 22, "glow_alpha": 40,
        "field_base": "#03111d", "field_alpha": 185, "field_focus_alpha": 225,
        "control_base": "#051622", "control_alphas": (120, 64, 150, 44),
        "shadow_color": "#000000", "shadow_alpha": 0,
        "shadow_blur": 18, "shadow_y": 5,
        "soft": False,
    },
    # ── Soft dark — the modern control-panel look, on ink ──────────────────
    "soft_dark": {
        "label": "Soft dark",
        "summary": "Modern soft UI on ink: layered slate cards, wide radii, soft shadows.",
        "mode": "dark",
        "accent": "#00d4ff", "accent_bright": "#7fe4ff", "accent_deep": "#147f9e",
        "accent_ink": "#072a38", "accent_text": "#8fe8ff",
        "accent_gradient_start": "#8fe8ff", "on_accent": "#04121a",
        "gold": "#f4b24a", "gold_deep": "#b87d1e", "gold_soft": "#ffd48a",
        "bg": "#0a0f16", "bg_deep_factor": 0.86, "bias": 0.05,
        "light_mix": "#00d4ff", "inset_mix": "#000000",
        "surface_k": 0.085, "surface_alt_k": 0.055, "surface_inset_k": 0.030,
        "panel_alpha": 252, "panel_soft_alpha": 130, "card_alpha": 205,
        "card_hi_alpha": 235, "header_alpha": 215,
        "text": "#e6f1f8", "text_bright": "#ffffff", "text_mid": "#b9cfdd",
        "text_dim": "#8fa6b6", "text_faint": "#7d94a6",
        "menu_alpha": 254, "track_alpha": 205,
        "stroke_alphas": (95, 55, 34), "stroke_width": 1,
        "radii": (18, 14, 10), "corner_cut": 0, "grid_alpha": 12, "glow_alpha": 30,
        "field_base": "#0d131c", "field_alpha": 210, "field_focus_alpha": 240,
        "control_base": "#141c27", "control_alphas": (150, 84, 170, 52),
        "shadow_color": "#000000", "shadow_alpha": 150,
        "shadow_blur": 26, "shadow_y": 8,
        "soft": True,
    },
    # ── Soft light — the same soft UI in daylight ──────────────────────────
    # The working colour has to carry white text as a fill *and* stay readable
    # as text on the light field, so the accent itself is deep and
    # ACCENT_GRADIENT_START equals it (a solid fill, no highlight gradient).
    "soft_light": {
        "label": "Soft light",
        "summary": "Soft UI in daylight: near-white field, white cards, deep cyan, shadows.",
        "mode": "light",
        "accent": "#0a6f95", "accent_bright": "#1aa3cf", "accent_deep": "#07546f",
        "accent_ink": "#062f3d", "accent_text": "#0a6f95",
        "accent_gradient_start": "#0a6f95", "on_accent": "#ffffff",
        "gold": "#a9701c", "gold_deep": "#8a5713", "gold_soft": "#c98a2e",
        "bg": "#f1f5f9", "bg_deep_factor": 0.94, "bias": 0.03,
        "light_mix": "#ffffff", "inset_mix": "#cfd8e3",
        "surface_k": 0.55, "surface_alt_k": 0.30, "surface_inset_k": 0.16,
        "panel_alpha": 252, "panel_soft_alpha": 150, "card_alpha": 235,
        "card_hi_alpha": 252, "header_alpha": 252,
        "text": "#0f1a24", "text_bright": "#050b12", "text_mid": "#2f4252",
        "text_dim": "#55687a", "text_faint": "#6d8093",
        "menu_alpha": 255, "track_alpha": 210,
        "stroke_alphas": (120, 80, 55), "stroke_width": 1,
        "radii": (18, 14, 10), "corner_cut": 0, "grid_alpha": 14, "glow_alpha": 20,
        "field_base": "#ffffff", "field_alpha": 255, "field_focus_alpha": 255,
        "control_base": "#ffffff", "control_alphas": (170, 48, 130, 60),
        "shadow_color": "#1b2b3a", "shadow_alpha": 62,
        "shadow_blur": 24, "shadow_y": 6,
        "soft": True,
    },
}

THEMES: tuple[str, ...] = ("hud", "soft_dark", "soft_light")
DEFAULT_THEME = "hud"
THEME_LABELS: dict[str, str] = {k: str(v["label"]) for k, v in _THEME_SPECS.items()}
THEME_SUMMARIES: dict[str, str] = {k: str(v["summary"]) for k, v in _THEME_SPECS.items()}


def _derive_theme(spec: dict, theme_id: str = DEFAULT_THEME) -> dict[str, Any]:
    """Every token the HUD can reference, derived from one theme's seeds.

    This is deliberately the *only* place a token value is computed: a theme is
    a handful of decisions (accent, field, glass alphas, radii, shadow) and the
    rest of the vocabulary is a derivation of them, so all themes carry exactly
    the same names and no surface has to know which theme is active.
    """
    accent = str(spec["accent"])
    accent_bright = str(spec["accent_bright"])
    accent_deep = str(spec["accent_deep"])
    accent_ink = str(spec["accent_ink"])
    accent_text = str(spec.get("accent_text") or accent_bright)
    gradient_start = str(spec.get("accent_gradient_start") or accent_bright)
    on_accent = str(spec.get("on_accent") or accent_ink)
    gold = str(spec["gold"])
    gold_deep = str(spec["gold_deep"])
    gold_soft = str(spec["gold_soft"])
    bg = str(spec["bg"])
    bg_deep = shade(bg, float(spec["bg_deep_factor"]))
    light_mix = str(spec["light_mix"])
    inset_mix = str(spec["inset_mix"])
    bias = float(spec["bias"])
    tint = mix(accent, bg, bias)
    soft = bool(spec.get("soft"))
    mode = str(spec["mode"])

    def _surf(k: float, other: str | None = None) -> str:
        return mix(other or light_mix, bg, float(k))

    stroke_base = accent if mode == "dark" else mix(accent, light_mix, 0.45)
    a_strong, a_mid, a_faint = (int(x) for x in spec["stroke_alphas"])
    border, border_soft, border_faint = (rgba(stroke_base, a) for a in (a_strong, a_mid, a_faint))

    # Soft themes read as *raised* cards on a field: the glass is nearly opaque
    # and the elevation comes from a shadow, not from an edge.
    panel_alpha = int(spec["panel_alpha"])
    card_alpha = int(spec["card_alpha"])
    header_alpha = int(spec["header_alpha"])
    panel_soft_alpha = int(spec["panel_soft_alpha"])
    card_hi_alpha = int(spec["card_hi_alpha"])

    text = str(spec["text"])
    text_bright = str(spec["text_bright"])
    text_mid = str(spec["text_mid"])
    text_dim = str(spec["text_dim"])
    text_faint = str(spec["text_faint"])
    text_disabled = mix(text_faint, bg, 0.55 if mode == "dark" else 0.30)

    field_base = str(spec["field_base"])
    control_base = str(spec["control_base"])
    c_idle, c_hover, c_pressed, c_disabled = (int(x) for x in spec["control_alphas"])
    radii = tuple(int(x) for x in spec["radii"])

    green = "#5df0b6" if mode == "dark" else "#0f7a52"
    amber = "#ffc773" if mode == "dark" else "#8a5a00"
    red = "#ff6f8b" if mode == "dark" else "#b3263f"

    return {
        # meta
        "THEME": theme_id,
        "THEME_MODE": mode,
        "THEME_LABEL": str(spec["label"]),
        "THEME_SUMMARY": str(spec["summary"]),
        "SOFT": soft,
        # accent + signature
        "ACCENT": accent,
        "ACCENT_BRIGHT": accent_bright,
        "ACCENT_DEEP": accent_deep,
        "ACCENT_INK": accent_ink,
        "ACCENT_TEXT": accent_text,
        "ACCENT_GRADIENT_START": gradient_start,
        "ON_ACCENT": on_accent,
        "ACCENT_TINT": rgba(accent, 44 if soft else 58),
        "ACCENT_WASH": rgba(accent, 96 if soft else 80),
        "GOLD": gold,
        "GOLD_DEEP": gold_deep,
        "GOLD_SOFT": gold_soft,
        "GOLD_WASH": rgba(gold, 34),
        # surfaces
        "BG": bg,
        "BG_DEEP": bg_deep,
        "PANEL": rgba(tint, panel_alpha),
        "PANEL_SOFT": rgba(tint, panel_soft_alpha),
        "CARD": rgba(_surf(float(spec["surface_k"])), card_alpha),
        "CARD_HI": rgba(_surf(float(spec["surface_k"])), card_hi_alpha),
        "HEADER": rgba(_surf(float(spec["surface_k"])), header_alpha),
        "SURFACE": _surf(float(spec["surface_k"])),
        "SURFACE_ALT": _surf(float(spec["surface_alt_k"])),
        "SURFACE_INSET": _surf(float(spec["surface_inset_k"]), inset_mix),
        "SURFACE_RAISED": _surf(min(1.0, float(spec["surface_k"]) + 0.10)),
        "GLASS_HI": rgba("#ffffff", 18 if mode == "dark" else 120),
        "FIELD_BG": rgba(field_base, int(spec["field_alpha"])),
        "FIELD_BG_FOCUS": rgba(field_base, int(spec["field_focus_alpha"])),
        "CONTROL_BG": rgba(control_base, c_idle),
        "CONTROL_HOVER": rgba(accent, c_hover),
        "CONTROL_PRESSED": rgba(accent, c_pressed),
        "CONTROL_DISABLED_BG": rgba(control_base, c_disabled),
        "MENU_BG": rgba(_surf(float(spec["surface_k"])), int(spec["menu_alpha"])),
        "ITEM_HOVER": rgba(accent, 36 if soft else 36),
        "ITEM_SELECTED": rgba(accent, 90 if not soft else 78),
        "CHIP_BG": rgba(accent, 52 if mode == "dark" else 30),
        "TRACK_BG": rgba(shade(bg, 0.9 if mode == "dark" else 1.0),
                         int(spec["track_alpha"])),
        "GROOVE_BG": rgba(shade(bg, 0.8 if mode == "dark" else 0.94), 200),
        # strokes
        "BORDER": border,
        "BORDER_SOFT": border_soft,
        "BORDER_FAINT": border_faint,
        "STROKE_STRONG": flatten(border, bg=bg),
        "STROKE_MID": flatten(border_soft, bg=bg),
        "STROKE_HAIR": flatten(border_faint, bg=bg),
        "BORDER_DISABLED": rgba(stroke_base, 26),
        "STROKE_WIDTH": int(spec["stroke_width"]),
        # text ramp
        "TEXT": text,
        "TEXT_BRIGHT": text_bright,
        "TEXT_MID": text_mid,
        "TEXT_DIM": text_dim,
        "TEXT_FAINT": text_faint,
        "TEXT_DISABLED": text_disabled,
        # status (deliberately independent of the accent) + soft washes
        "GREEN": green,
        "GREEN_DEEP": shade(green, 0.72),
        "GREEN_WASH": rgba(green, 34 if mode == "dark" else 26),
        "AMBER": amber,
        "AMBER_WASH": rgba(amber, 34 if mode == "dark" else 26),
        "RED": red,
        "RED_WASH": rgba(red, 34 if mode == "dark" else 24),
        # geometry + elevation
        "RADIUS_LG": radii[0],
        "RADIUS_MD": radii[1],
        "RADIUS_SM": radii[2],
        "CORNER_CUT": int(spec["corner_cut"]),
        "GRID_ALPHA": int(spec["grid_alpha"]),
        "GLOW_ALPHA": int(spec["glow_alpha"]),
        "SHADOW_COLOR": str(spec["shadow_color"]),
        "SHADOW_ALPHA": int(spec["shadow_alpha"]),
        "SHADOW_BLUR": int(spec["shadow_blur"]),
        "SHADOW_Y": int(spec["shadow_y"]),
        "SHADOW": rgba(str(spec["shadow_color"]), int(spec["shadow_alpha"])),
    }


#: name -> complete token table, computed once at import.
_TOKENS: dict[str, dict[str, Any]] = {name: _derive_theme(spec, name)
                                      for name, spec in _THEME_SPECS.items()}

#: Every token name a theme must provide (the contract the tests pin).
TOKEN_NAMES: tuple[str, ...] = tuple(sorted(_TOKENS[DEFAULT_THEME]))

_ACTIVE: str = DEFAULT_THEME
_LISTENERS: list[Callable[[str, dict, dict], None]] = []
_LOCK = threading.RLock()


def _install(name: str) -> None:
    """Make `name` the active theme: swap the module-level token values.

    The names below are ordinary module globals on purpose — factories and
    painter code read them at call time, so replacing the global *is* the theme
    switch. Nothing is rebuilt and nothing is invalidated.
    """
    global _ACTIVE
    _ACTIVE = name
    globals().update(_TOKENS[name])
    _rebuild_palette()


# ── The one palette ─────────────────────────────────────────────────────────
# ui.py's `class C` is an alias table onto this mapping, not a palette of its
# own: every value here is either a canonical token above or a derived form of
# one, so there is exactly one place in the project where a colour is decided.
#
# All values are opaque '#rrggbb' because the legacy call sites pass them to
# QColor()/QPen() (which reject rgba strings) as well as into QSS. The mapping is
# mutated in place on a theme switch so every holder of this dict object — the
# `class C` alias table included — sees the new values.
PALETTE: dict[str, str] = {}

# Keys whose hue follows the user's ui_color. Everything else is fixed, so a
# user accent can never turn a warning green.
HUE_LINKED: tuple[str, ...] = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)

PALETTE_DEFAULTS: dict[str, str] = {}

# The token(s) the palette is projected from, per key. Kept next to the
# projection so a theme switch can rebuild the palette without re-deciding
# anything.
_PALETTE_SOURCE: tuple[tuple[str, str], ...] = (
    ("BG", "BG"), ("PANEL", "SURFACE"), ("PANEL2", "SURFACE_ALT"),
    ("DARK", "BG_DEEP"), ("BAR_BG", "SURFACE_INSET"),
    ("BORDER", "STROKE_HAIR"), ("BORDER_A", "STROKE_MID"), ("BORDER_B", "STROKE_STRONG"),
    ("PRI", "ACCENT"), ("PRI_DIM", "ACCENT_DEEP"), ("PRI_GHO", "ACCENT_INK"),
    ("TEXT", "TEXT"), ("TEXT_DIM", "TEXT_DIM"), ("TEXT_MED", "TEXT_MID"),
    ("WHITE", "TEXT_BRIGHT"),
    ("ACC", "GOLD"), ("ACC2", "AMBER"), ("GREEN", "GREEN"), ("GREEN_D", "GREEN_DEEP"),
    ("RED", "RED"), ("MUTED_C", "RED"), ("GOLD", "GOLD"),
)


def _rebuild_palette() -> None:
    """Re-project PALETTE (in place) from the active theme's tokens."""
    g = globals()
    values = {key: hex_color(str(g[token])) for key, token in _PALETTE_SOURCE}
    PALETTE.clear()
    PALETTE.update(values)
    PALETTE_DEFAULTS.clear()
    PALETTE_DEFAULTS.update({k: PALETTE[k] for k in HUE_LINKED})


# Install the default theme so every name below is a real token from here on.
_install(DEFAULT_THEME)

DEFAULT_ACCENT = PALETTE["PRI"]

# The accent values this project shipped before the token consolidation. They
# are mapped to the current accent so an existing install does not keep the
# retired palette alive; anything else the user picked is left alone.
LEGACY_ACCENTS: dict[str, str] = {
    "#00d4ff": PALETTE["PRI"],
    "#007a99": PALETTE["PRI_DIM"],
    "#001f2e": PALETTE["PRI_GHO"],
}


def migrate_accent(value: str) -> str:
    """Map a legacy accent value onto the current token; pass others through."""
    key = str(value or "").strip().lower()
    return LEGACY_ACCENTS.get(key, key)


# ── Theme control ───────────────────────────────────────────────────────────

def theme_names() -> tuple[str, ...]:
    """The themes this build ships, in picker order."""
    return THEMES


def current_theme() -> str:
    """The active theme id (``hud`` | ``soft_dark`` | ``soft_light``)."""
    return _ACTIVE


def theme_label(name: str) -> str:
    return THEME_LABELS.get(normalize_theme(name), THEME_LABELS[DEFAULT_THEME])


def normalize_theme(name: str) -> str:
    """Any stored/hand-edited theme value -> a real theme id."""
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "soft": "soft_dark", "dark": "soft_dark", "softdark": "soft_dark",
        "light": "soft_light", "softlight": "soft_light",
        "classic": "hud", "holo": "hud", "holographic": "hud",
    }
    key = aliases.get(key, key)
    return key if key in _TOKENS else DEFAULT_THEME


def token_snapshot(name: str | None = None) -> dict[str, Any]:
    """A copy of one theme's complete token table (default: the active one)."""
    return dict(_TOKENS[normalize_theme(name) if name else _ACTIVE])


def palette() -> dict[str, str]:
    """A copy of the palette as it is right now (accent rotation included)."""
    return dict(PALETTE)


def palette_defaults() -> dict[str, str]:
    """The hue-rotation base of the active theme (a fresh copy each call)."""
    return dict(PALETTE_DEFAULTS)


def is_soft() -> bool:
    """True for the two soft themes (rounded, shadowed, calm strokes)."""
    return bool(globals().get("SOFT"))


def add_theme_listener(callback: Callable[[str, dict, dict], None]) -> None:
    """Register ``callback(name, old_tokens, new_tokens)`` for theme switches.

    Listeners are called after the new tokens are installed, so a listener can
    immediately read the new values; the two dicts are what a live retheme needs
    to rewrite stylesheets that already embedded the previous values. Modules
    that cached token names at import time register one of these (usually via
    ``bind_module_globals``).
    """
    if callback not in _LISTENERS:
        _LISTENERS.append(callback)


def remove_theme_listener(callback: Callable[[str, dict, dict], None]) -> None:
    try:
        _LISTENERS.remove(callback)
    except ValueError:
        pass


def _notify(name: str, old: dict, new: dict) -> None:
    for callback in list(_LISTENERS):
        try:
            callback(name, old, new)
        except Exception:
            # A listener is cosmetic: a broken one must never break the switch.
            pass


def set_theme(name: str, notify: bool = True) -> bool:
    """Switch the active theme. Returns False for an unknown name.

    Installs the theme's tokens, re-projects the palette and notifies the
    registered listeners (which is what repaints a live window). Passing an
    unknown name falls back to the default theme rather than raising, so a
    hand-edited config can never leave the UI unthemed.
    """
    raw = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = normalize_theme(raw)
    with _LOCK:
        old = dict(_TOKENS[_ACTIVE])
        _install(key)
    if notify:
        _notify(key, old, dict(_TOKENS[key]))
    return raw in _TOKENS


def bind_module_globals(namespace: dict,
                        names: Sequence = TOKEN_NAMES) -> Callable[[str, dict, dict], None]:
    """Keep a module's cached copies of the tokens in step with the theme.

    Painter code that does ``from ui_theme import SURFACE`` binds the *value* at
    import time, so a live theme switch has to refresh the module global the
    painter reads at paint time: pass the module's ``globals()`` and the names
    it cached, and the returned listener does exactly that on every switch.

    An entry may be a plain token name, or a ``(binding, token)`` pair for the
    modules that imported a token under an alias (``SURFACE as _THEME_SURFACE``).
    The listener is registered on the way out, so a module only has to keep a
    reference if it wants to unregister later.
    """
    pairs: list[tuple[str, str]] = []
    for item in names:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
        else:
            pairs.append((str(item), str(item)))

    def _rebind(_name: str, _old: dict, _new: dict) -> None:
        for binding, token in pairs:
            if token in _new:
                namespace[binding] = _new[token]

    add_theme_listener(_rebind)
    return _rebind


# ── Live retheme of existing stylesheets ────────────────────────────────────
# A widget that was built with `button_css()` already carries the previous
# theme's values inside its own stylesheet, so installing new tokens is not
# enough on its own. The swap is a string replacement keyed by the token values
# themselves — which is exactly why every factory draws from the token table
# instead of a literal, and why the radii are swapped by their own pass.

def _looks_like_colour(value: Any) -> bool:
    """Only colours are ever swapped inside a stylesheet.

    A theme also carries labels and numbers (``THEME_LABEL``, ``RADIUS_LG``);
    substituting those as strings could rewrite an unrelated word, so they are
    not part of the swap at all.
    """
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    return text.startswith("#") or text.startswith("rgb(") or text.startswith("rgba(")


def theme_swap_map(old: dict, new: dict, extra: dict | None = None) -> dict[str, str]:
    """old colour -> new colour for every token that actually changed."""
    swap: dict[str, str] = {}
    for key, old_value in old.items():
        new_value = new.get(key)
        if old_value == new_value:
            continue
        if _looks_like_colour(old_value) and _looks_like_colour(new_value):
            swap.setdefault(old_value.lower(), new_value.lower())
    for old_value, new_value in (extra or {}).items():
        if old_value != new_value and _looks_like_colour(old_value) and _looks_like_colour(new_value):
            swap.setdefault(old_value.lower(), new_value.lower())
    # A value that the *new* theme also uses must never be rewritten away: the
    # mapping would otherwise turn a stale dark value into a live one twice.
    for old_value in list(swap):
        if old_value == swap[old_value]:
            swap.pop(old_value, None)
    return swap


def remap_stylesheet(sheet: str, swap: dict[str, str],
                     old_radii: Iterable[int] = (), new_radii: Iterable[int] = (),
                     target_mode: str = "") -> str:
    """Rewrite one stylesheet: token values, the radii, then the dark washes.

    The third pass is what keeps a surface honest when a widget embedded a
    literal ``rgba(1,18,29,115)`` inset instead of a token: a wash is a *wash*,
    so a dark one becomes a light one when the field turns light (and back
    again), regardless of which surface wrote it. The mapping is a pure
    function of the target mode, so repeated switches are stable.
    """
    if not sheet:
        return sheet
    out = sheet
    for old_value, new_value in swap.items():
        if old_value and old_value in out:
            out = out.replace(old_value, new_value)
    old_r = [int(r) for r in old_radii]
    new_r = [int(r) for r in new_radii]
    for old_v, new_v in zip(old_r, new_r):
        if old_v == new_v:
            continue
        for spaced in (f"border-radius:{old_v}px", f"border-radius: {old_v}px"):
            if spaced in out:
                out = out.replace(spaced, spaced.replace(str(old_v), str(new_v)))
    if target_mode in ("dark", "light"):
        out = flip_dark_washes(out, target_mode)
    return out


#: A wash that swaps sides with the field. Fixed values on purpose: the flip has
#: to be a function of the target mode alone, or a second switch would drift.
WASH_LIGHT_RGB = (243, 247, 251)
WASH_DARK_RGB = (8, 13, 20)

_RGBA_LITERAL = None


def flip_dark_washes(sheet: str, target_mode: str) -> str:
    """Turn dark translucent washes light (or light ones dark) in one sheet."""
    global _RGBA_LITERAL
    if not sheet or target_mode not in ("dark", "light"):
        return sheet
    if _RGBA_LITERAL is None:
        import re
        _RGBA_LITERAL = re.compile(
            r"rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)")
    pattern = _RGBA_LITERAL

    def _sub(match) -> str:
        r, g, b, a = (int(match.group(i)) for i in range(1, 5))
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
        if target_mode == "light" and lum < 0.22:
            return f"rgba({WASH_LIGHT_RGB[0]}, {WASH_LIGHT_RGB[1]}, {WASH_LIGHT_RGB[2]}, {a})"
        if target_mode == "dark" and lum > 0.78:
            return f"rgba({WASH_DARK_RGB[0]}, {WASH_DARK_RGB[1]}, {WASH_DARK_RGB[2]}, {a})"
        return match.group(0)

    return pattern.sub(_sub, sheet)


def retheme_widget_stylesheets(old: dict, new: dict, widgets: Iterable | None = None,
                               extra_swap: dict[str, str] | None = None,
                               target_mode: str | None = None) -> int:
    """Re-write the stylesheets of live widgets from `old` tokens to `new`.

    Returns how many widgets changed. Uses every widget of the running
    application when no explicit list is given, which is what makes a theme
    switch visible across the settings dialog, the floating panels and the HUD
    in one pass.
    """
    swap = theme_swap_map(old, new, extra_swap)
    old_radii = (old.get("RADIUS_LG"), old.get("RADIUS_MD"), old.get("RADIUS_SM"))
    new_radii = (new.get("RADIUS_LG"), new.get("RADIUS_MD"), new.get("RADIUS_SM"))
    mode = str(target_mode or new.get("THEME_MODE") or "")
    if not swap and list(old_radii) == list(new_radii) and mode == old.get("THEME_MODE"):
        return 0
    if widgets is None:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        widgets = app.allWidgets() if app is not None else ()
    changed = 0
    for widget in list(widgets):
        try:
            sheet = widget.styleSheet()
            if not sheet:
                continue
            updated = remap_stylesheet(sheet, swap, old_radii, new_radii, mode)
            if updated != sheet:
                widget.setStyleSheet(updated)
                changed += 1
        except Exception:
            pass
    return changed


# ── Soft-UI surfaces ────────────────────────────────────────────────────────
# QSS has no drop shadow, so elevation is QGraphicsDropShadowEffect per card
# (one effect per widget, applied by `apply_soft_shadow`) and the QSS factories
# below supply the rounded, low-contrast card/row/pill bodies.

def soft_card_css(level: int = 1) -> str:
    """A raised panel body. The shadow itself comes from `apply_soft_shadow`."""
    radius = RADIUS_LG if level <= 1 else RADIUS_MD
    fill = SURFACE_RAISED if level <= 1 else SURFACE
    return (
        f"QFrame {{ background:{fill}; border:1px solid {BORDER_FAINT};"
        f" border-radius:{radius}px; }}"
    )


def soft_row_css() -> str:
    """One settings row: quiet by default, tinted on hover, never boxed in."""
    return (
        "QFrame { background:transparent; border:1px solid transparent;"
        f" border-radius:{RADIUS_MD}px; }}"
        f"QFrame:hover {{ background:{SURFACE_ALT}; border-color:{BORDER_FAINT}; }}"
    )


def apply_soft_shadow(widget, level: int = 1):
    """Give one card the theme's elevation; level 0 removes the effect.

    One effect per widget on purpose: Qt repaints a widget with an effect into
    an offscreen buffer, so this is for cards and pinned rows, never for a whole
    page. The HUD theme reports an alpha of 0, so it clears any effect instead —
    the holographic look stays flat.
    """
    try:
        from PyQt6.QtWidgets import QGraphicsDropShadowEffect
    except Exception:
        return None
    alpha = int(SHADOW_ALPHA)
    if level <= 0 or alpha <= 0:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        return None
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(float(SHADOW_BLUR) * (1.0 + 0.35 * max(0, level - 1)))
    effect.setOffset(0.0, float(SHADOW_Y))
    colour = qcolor(SHADOW_COLOR)
    colour.setAlpha(max(0, min(255, int(alpha * (0.7 + 0.3 * max(1, level))))))
    effect.setColor(colour)
    try:
        widget.setGraphicsEffect(effect)
    except Exception:
        return None
    return effect


def state_pill_css(state: str) -> str:
    """The status pill used by the service hub and the Quick Setup summary.

    One treatment per state, drawn from the theme's own tokens, so a pill is
    readable in all three themes and red never means anything but a problem.
    """
    key = str(state or "").strip().lower()
    if key in ("ready", "ok", "connected", "on"):
        fg, bg = GREEN, GREEN_WASH
    elif key in ("configured", "key_optional", "idle", "not tested", "optional"):
        fg, bg = TEXT_MID, SURFACE_INSET
    elif key in ("missing_key", "needs key", "warn", "attention"):
        fg, bg = AMBER, AMBER_WASH
    elif key in ("unavailable", "error", "off", "disabled"):
        fg, bg = RED, RED_WASH
    elif key in ("testing", "live", "active"):
        fg, bg = ACCENT_TEXT, ACCENT_TINT
    else:
        fg, bg = TEXT_FAINT, SURFACE_INSET
    return (
        f"color:{fg}; background:{bg}; border:none; border-radius:{RADIUS_SM}px;"
        " padding:4px 10px; font:700 7pt 'Exo 2'; letter-spacing:1px;"
    )


# ── Type ────────────────────────────────────────────────────────────────────
# Type does not change with the theme: one display face, one UI face, one mono.
FONT_UI    = "'Rajdhani'"
FONT_HEAD  = "'Orbitron'"
FONT_LOG   = "'Share Tech Mono'"
FONT_NUM   = "'Orbitron'"
FONT_SMALL = "'Exo 2'"
FONT_MONO  = "'Share Tech Mono'"
FONT_DISP  = "'Orbitron'"

# ── Spacing + control geometry (theme-independent) ──────────────────────────
SPACE_XS    = 4
SPACE_SM    = 6
SPACE_MD    = 10
SPACE_LG    = 14
SPACE_XL    = 20
SPACING     = (SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL)
SPACING_SCALE = (SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL)

# Control heights. Legacy widgets used 26/28/30/34/36 at random; these three
# are the only allowed heights, and ui.py's class C aliases them.
CONTROL_SM  = 28
CONTROL_MD  = 34
CONTROL_LG  = 38
CONTROL_HEIGHTS = (CONTROL_SM, CONTROL_MD, CONTROL_LG)

# Panel header band. A minimum, not a fixed height: the shared header grows when
# a title wraps instead of clipping it.
HEADER_MIN_H = 54
PANEL_MIN_W  = 380
PANEL_MIN_H  = 250

# Text that must never be clipped: how much of the header a title may take.
TITLE_MAX_LINES  = 2


def button_css(min_height: int = 34) -> str:
    """Quiet default control: hairline edge, accent only on interaction."""
    return (
        "QPushButton {"
        f" color:{TEXT_MID}; background:{CONTROL_BG};"
        f" border:1px solid {BORDER_FAINT}; border-radius:{RADIUS_SM}px;"
        f" font:700 8pt {FONT_SMALL}; letter-spacing:1px;"
        f" min-height:{min_height}px; padding:6px 13px; }}"
        "QPushButton:hover {"
        f" color:{TEXT_BRIGHT}; border-color:{ACCENT}; background:{CONTROL_HOVER}; }}"
        f"QPushButton:pressed {{ background:{CONTROL_PRESSED}; }}"
        "QPushButton:disabled {"
        f" color:{TEXT_DISABLED}; border-color:{BORDER_DISABLED};"
        f" background:{CONTROL_DISABLED_BG}; }}"
        "QPushButton:checked {"
        f" color:{TEXT_BRIGHT}; background:{ACCENT_WASH}; border-color:{ACCENT}; }}"
    )


def primary_button_css(min_height: int = 36) -> str:
    """The committed action — the one filled control on a page."""
    return (
        "QPushButton {"
        f" color:{ON_ACCENT}; background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
        f"  stop:0 {ACCENT_GRADIENT_START}, stop:1 {ACCENT});"
        f" border:1px solid {ACCENT_GRADIENT_START}; border-radius:{RADIUS_SM}px;"
        f" font:800 8pt {FONT_SMALL}; letter-spacing:1px;"
        f" min-height:{min_height}px; padding:6px 15px; }}"
        "QPushButton:hover {"
        f" background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
        f"  stop:0 {ACCENT_BRIGHT}, stop:1 {ACCENT}); }}"
        "QPushButton:pressed {"
        f" background:{ACCENT_DEEP}; color:{ON_ACCENT}; }}"
    )


def accent_button_css(min_height: int = 34) -> str:
    """Gold signature button — use sparingly for a single 'armed/live' action."""
    return (
        "QPushButton {"
        f" color:{GOLD}; background:{GOLD_WASH};"
        f" border:1px solid {GOLD}; border-radius:{RADIUS_SM}px;"
        f" font:800 8pt {FONT_SMALL}; letter-spacing:1px;"
        f" min-height:{min_height}px; padding:6px 13px; }}"
        "QPushButton:hover {"
        f" color:{ON_ACCENT if THEME_MODE == 'light' else '#1c1204'};"
        f" background:{GOLD}; border-color:{GOLD_SOFT}; }}"
        f"QPushButton:pressed {{ background:{GOLD_DEEP}; color:#1c1204; }}"
    )


def field_css() -> str:
    return (
        "QLineEdit,QComboBox,QTextEdit,QListWidget,QSpinBox,QDoubleSpinBox "
        f"{{background:{FIELD_BG};"
        f"color:{TEXT_BRIGHT};"
        f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_MD}px;"
        f"padding:6px 11px;font:9pt {FONT_UI};"
        f"selection-background-color:{ACCENT_WASH};selection-color:{TEXT_BRIGHT};}}"
        "QLineEdit:hover,QComboBox:hover,QTextEdit:hover,QSpinBox:hover,QDoubleSpinBox:hover "
        f"{{border-color:{BORDER_SOFT};}}"
        "QLineEdit:focus,QComboBox:focus,QTextEdit:focus,QSpinBox:focus,QDoubleSpinBox:focus "
        f"{{border-color:{ACCENT};background:{FIELD_BG_FOCUS};}}"
        "QComboBox::drop-down{width:26px;border:none;background:transparent;}"
        f"QComboBox QAbstractItemView{{background:{MENU_BG};color:{TEXT};"
        f"border:1px solid {BORDER_SOFT};border-radius:{RADIUS_SM}px;padding:4px;"
        f"selection-background-color:{ACCENT_WASH};selection-color:{TEXT_BRIGHT};}}"
        f"QListWidget{{background:{FIELD_BG};}}"
        f"QListWidget::item{{padding:7px 9px;border:1px solid transparent;"
        f"border-radius:{RADIUS_SM - 1}px;margin:1px 0;}}"
        f"QListWidget::item:selected{{background:{ACCENT_WASH};color:{TEXT_BRIGHT};"
        f"border-color:{BORDER_SOFT};}}"
        f"QListWidget::item:hover{{background:{ITEM_HOVER};}}"
    )


def card_css() -> str:
    return (
        f"QFrame {{ background:{CARD}; border:1px solid {BORDER_FAINT};"
        f" border-radius:{RADIUS_LG}px; }}"
    )


def header_css() -> str:
    return (
        f"QFrame {{ background:{HEADER}; border:1px solid {BORDER_SOFT};"
        f" border-radius:{RADIUS_MD}px; }}"
    )


def chip_css() -> str:
    return (
        f"color:{ACCENT_TEXT}; background:{CHIP_BG};"
        f"border:1px solid {BORDER_FAINT}; border-radius:{RADIUS_SM}px;"
        f"font:700 7pt {FONT_SMALL}; letter-spacing:1px; padding:5px 10px;"
    )


def section_css() -> str:
    return (
        f"color:{ACCENT_TEXT}; font:800 8pt {FONT_HEAD}; letter-spacing:2px;"
        " background:transparent; padding:2px 2px 6px 2px;"
    )


def title_css(size: str = "10pt") -> str:
    return (
        f"color:{TEXT}; font:700 {size} {FONT_HEAD}; letter-spacing:3px;"
        " background:transparent; border:none;"
    )


def subtitle_css() -> str:
    return (
        f"color:{TEXT_FAINT}; font:600 6.5pt {FONT_SMALL}; letter-spacing:1px;"
        " background:transparent; border:none;"
    )


def scrollbar_css(width: int = 6) -> str:
    handle = rgba(ACCENT_DEEP if THEME_MODE == "dark" else ACCENT, 150)
    return (
        f"QScrollBar:vertical {{ background:transparent; width:{width}px; border:none; margin:2px; }}"
        f"QScrollBar::handle:vertical {{ background:{handle}; border-radius:{width // 2}px; min-height:26px; }}"
        f"QScrollBar::handle:vertical:hover {{ background:{ACCENT}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0px; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }"
        f"QScrollBar:horizontal {{ background:transparent; height:{width}px; border:none; margin:2px; }}"
        f"QScrollBar::handle:horizontal {{ background:{handle}; border-radius:{width // 2}px; min-width:26px; }}"
        f"QScrollBar::handle:horizontal:hover {{ background:{ACCENT}; }}"
        "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0px; }"
        "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:transparent; }"
    )


def hidden_scrollbar_css() -> str:
    return (
        "QScrollBar:vertical { background:transparent; width:0px; border:none; }"
        "QScrollBar::handle:vertical { background:transparent; border:none; }"
        "QScrollBar:horizontal { background:transparent; height:0px; border:none; }"
        "QScrollBar::handle:horizontal { background:transparent; border:none; }"
    )


def tab_css() -> str:
    return (
        f"QPushButton {{ color:{TEXT_DIM}; background:{CONTROL_BG}; border:1px solid transparent;"
        f" border-radius:{RADIUS_SM}px; font:700 7pt {FONT_SMALL}; letter-spacing:1px; padding:6px 10px; }}"
        f"QPushButton:hover {{ color:{TEXT_BRIGHT}; background:{CONTROL_HOVER}; }}"
        f"QPushButton:checked {{ color:{TEXT_BRIGHT}; background:{ACCENT_WASH};"
        f" border-color:{BORDER_SOFT}; }}"
    )


def icon_font_css() -> str:
    return "font-family:'Segoe UI Symbol','Segoe UI Emoji','Rajdhani','Segoe UI',sans-serif;"


def slider_css() -> str:
    return (
        f"QSlider::groove:horizontal {{ height:4px; background:{GROOVE_BG};"
        f" border:1px solid {BORDER_FAINT}; border-radius:2px; }}"
        "QSlider::sub-page:horizontal { height:4px;"
        f" background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {ACCENT_DEEP}, stop:1 {ACCENT});"
        " border:none; border-radius:2px; }"
        f"QSlider::add-page:horizontal {{ background:{TRACK_BG}; border-radius:2px; }}"
        f"QSlider::handle:horizontal {{ width:14px; height:14px; margin:-6px 0;"
        f" background:{SURFACE_RAISED}; border:2px solid {ACCENT}; border-radius:7px; }}"
        f"QSlider::handle:horizontal:hover {{ background:{ACCENT_BRIGHT}; border-color:{ACCENT_BRIGHT}; }}"
    )


def checkbox_css() -> str:
    return (
        f"QCheckBox {{ spacing:8px; padding:6px 2px; color:{TEXT_MID}; background:transparent;"
        f" font:600 9pt {FONT_UI}; }}"
        f"QCheckBox::indicator {{ width:15px; height:15px; border:1px solid {BORDER_SOFT};"
        f" background:{FIELD_BG}; border-radius:4px; }}"
        f"QCheckBox::indicator:hover {{ border-color:{ACCENT}; }}"
        f"QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}"
    )


def toggle_css(min_height: int = 40) -> str:
    """A switch row (label + state) for the Quick Setup page and five essentials.

    The whole row is the control: it states what it does on the left, what it is
    doing now on the right, and tints itself on hover instead of drawing a box.
    """
    return (
        "QPushButton {"
        f" color:{TEXT}; background:{SURFACE_ALT}; text-align:left;"
        f" border:1px solid {BORDER_FAINT}; border-radius:{RADIUS_MD}px;"
        f" font:600 9pt {FONT_UI}; min-height:{min_height}px; padding:7px 13px; }}"
        "QPushButton:hover {"
        f" background:{CONTROL_HOVER}; border-color:{BORDER_SOFT}; }}"
        "QPushButton:checked {"
        f" color:{TEXT_BRIGHT}; background:{ACCENT_WASH}; border-color:{ACCENT}; }}"
        f"QPushButton:disabled {{ color:{TEXT_DISABLED}; }}"
    )


def progress_css() -> str:
    """Compact telemetry bars with a clear track/chunk relationship."""
    return (
        "QProgressBar {"
        f" background:{TRACK_BG}; color:{TEXT_DIM};"
        f" border:1px solid {BORDER_FAINT}; border-radius:{RADIUS_SM}px;"
        " text-align:center; min-height:12px; padding:1px;"
        f" font:700 6.5pt {FONT_SMALL}; }}"
        f"QProgressBar::chunk {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
        f" stop:0 {ACCENT_DEEP}, stop:1 {ACCENT}); border-radius:4px; }}"
    )


def dialog_css() -> str:
    return (
        f"QDialog {{ background:{PANEL}; border:1px solid {BORDER_SOFT};"
        f" border-radius:{RADIUS_LG}px; color:{TEXT}; }}"
    )


def tooltip_css() -> str:
    return (
        f"QToolTip {{ background:{BG_DEEP}; color:{TEXT_BRIGHT};"
        f" border:1px solid {ACCENT_DEEP}; border-radius:{RADIUS_SM}px; padding:5px 8px;"
        f" font:8pt {FONT_SMALL}; }}"
    )


STATUS_COLORS = {
    "ok": GREEN, "run": ACCENT_TEXT, "think": ACCENT_BRIGHT, "warn": AMBER,
    "error": RED, "dim": TEXT_FAINT, "you": TEXT_BRIGHT, "ai": ACCENT_TEXT,
    "sys": TEXT_DIM, "live": GOLD,
}


def status_color(kind: str) -> str:
    return STATUS_COLORS.get(str(kind or "").lower(), STATUS_COLORS["dim"])


def meter_color(pct: float) -> str:
    try:
        pct = float(pct)
    except Exception:
        return ACCENT
    if pct >= 90:
        return RED
    if pct >= 70:
        return AMBER
    return ACCENT


# ── Shared components ───────────────────────────────────────────────────────
# One title treatment, one status chip, one section heading, one control row.
# Every panel and Settings tab renders through these, which is what stops each
# surface from inventing its own title style.

def panel_title_css() -> str:
    """Panel title type role — the same on every surface."""
    return (
        f"color:{TEXT_BRIGHT}; font:800 9.5pt {FONT_HEAD}; letter-spacing:1.8px;"
        " background:transparent; border:none;"
    )


def panel_subtitle_css() -> str:
    """Micro-label under a panel title (SYSTEM MEMORY / LIVE TELEMETRY …)."""
    return (
        f"color:{TEXT_FAINT}; font:700 6.5pt {FONT_SMALL}; letter-spacing:1.4px;"
        " background:transparent; border:none;"
    )


def panel_icon_css() -> str:
    return (
        f'color:{ACCENT_TEXT}; font:800 15pt {FONT_UI}; letter-spacing:0;'
        " background:transparent; border:none; padding:0;"
    )


def status_chip_css(kind: str = "live") -> str:
    """The small state marker in a header — gold only for live/armed."""
    color = status_color(kind)
    return (
        f"color:{color}; font:800 6.5pt {FONT_SMALL}; letter-spacing:1px;"
        " background:transparent; border:none;"
    )


def separator_css() -> str:
    """The hairline that runs between a title and its status marker."""
    return f"background:{BORDER_FAINT}; border:none;"


def micro_label_css() -> str:
    """Technical identifier text (TELEMETRY / SYS / NODE 04)."""
    return (
        f"color:{TEXT_FAINT}; font:700 6.5pt {FONT_LOG}; letter-spacing:1.2px;"
        " background:transparent; border:none;"
    )


def shell_dialog_css() -> str:
    """Translucent dialog shell — the painted backdrop supplies the field."""
    return (
        "QDialog { background: transparent;"
        f" color:{TEXT}; }}"
        "QFrame#SettingsRoot { background: transparent; border: none; }"
        f"QLabel {{ color: {TEXT_MID}; background: transparent; }}"
    )


def nav_header_css() -> str:
    """The gradient header band shared by the settings window and panels."""
    return (
        "QFrame#HudHeader {"
        " background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
        f"  stop:0 {rgba(mix(ACCENT, BG, 0.08), 238)},"
        f"  stop:0.55 {rgba(BG, 222)}, stop:1 {rgba(mix(ACCENT, BG, 0.09), 205)});"
        f" border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS_MD}px; }}"
    )


def qtabbar_css() -> str:
    """Left-rail tab bar (QTabWidget). The HUD's only tab treatment."""
    return (
        "QTabWidget { background: transparent; }"
        "QTabWidget::pane { border: none; background: transparent; }"
        "QTabBar { background: transparent; }"
        "QTabBar::tab {"
        " min-width: 154px; min-height: 46px;"
        f" color: {TEXT_DIM}; background: {PANEL_SOFT};"
        f" border: 1px solid {BORDER_FAINT}; border-left: 2px solid transparent;"
        f" border-radius: {RADIUS_MD}px; padding: 8px 13px; margin: 2px 8px 5px 0;"
        f" text-align: left; font: 700 8pt {FONT_SMALL}; }}"
        "QTabBar::tab:hover {"
        f" color: {TEXT_BRIGHT}; background: {CONTROL_HOVER};"
        f" border-color: {BORDER}; }}"
        "QTabBar::tab:selected {"
        f" color: {TEXT_BRIGHT}; background: {ACCENT_WASH};"
        f" border-color: {BORDER}; border-left: 2px solid {ACCENT}; }}"
    )


def scroll_area_css() -> str:
    return (
        "QScrollArea { border: none; background: transparent; }"
        "QScrollArea > QWidget > QWidget { background: transparent; }"
    )


def group_box_css() -> str:
    """Section container. The final block is the unified finish: no stacked
    frames, just an Orbitron caption over the panel's own background."""
    return (
        "QGroupBox {"
        f" background: {PANEL_SOFT};"
        f" border: 1px solid {BORDER_SOFT}; border-radius: {RADIUS_LG}px;"
        " margin-top: 16px; padding: 16px 14px 13px 14px; }"
        "QGroupBox::title {"
        " subcontrol-origin: margin; left: 14px; padding: 0 8px;"
        f" color: {ACCENT_TEXT}; background: {BG}; font: 800 8pt {FONT_HEAD}; }}"
        "QGroupBox { background: transparent; border: none;"
        " margin-top: 10px; padding-top: 8px; }"
        f"QGroupBox::title {{ background: transparent; color: {ACCENT_TEXT}; padding: 0 4px; }}"
    )


def settings_surface_css() -> str:
    """Cards and section frames inside the control center."""
    return (
        "QFrame#TabIntro, QFrame#SettingsCard, QFrame#SettingsSection {"
        f" background: {PANEL_SOFT}; border: none; }}"
    )


def composed_app_css() -> str:
    """The whole window chrome, assembled from the shared component rules.

    Surfaces used to hand-write their own copy of this; composing it here is
    what keeps Settings, the floating panels and the main shell identical.
    """
    return "\n".join((
        shell_dialog_css(), nav_header_css(), qtabbar_css(), scroll_area_css(),
        button_css(), primary_button_css(), accent_button_css(), field_css(),
        checkbox_css(), slider_css(), scrollbar_css(), group_box_css(),
        progress_css(), tooltip_css(), settings_surface_css(),
    ))