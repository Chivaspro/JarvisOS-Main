"""The design-token contract.

These tests are the reason ``ui.py`` no longer owns a palette: they pin the
token vocabulary to three radii, three stroke weights, one accent, and a small
spacing scale, and they prove the derived helpers (which are what let painter
code and QSS share one colour) actually work.

They also pin the *theme* contract, because a second look is only worth having if
it is the same vocabulary with different values: every theme carries every token,
the HUD keeps its crisp geometry while the soft themes round off, a light theme
still clears the contrast floors, and a live switch can rewrite the stylesheets
that already embedded the previous values.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import ui_theme as T

HEX6 = re.compile(r"^#[0-9a-f]{6}$")
SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def _restore_default_theme():
    """A theme switch is global state; no test may leak one into the next."""
    before = T.current_theme()
    yield
    T.set_theme(before)


def test_palette_values_are_opaque_hex():
    """Legacy call sites hand these to QColor()/QPen(), which reject rgba()."""
    for name, value in T.PALETTE.items():
        assert HEX6.match(value), f"{name} is not opaque #rrggbb: {value!r}"


def test_hue_linked_keys_all_exist_in_the_palette():
    for key in T.HUE_LINKED:
        assert key in T.PALETTE, f"hue-linked key {key} has no token"


def test_palette_defaults_are_hex_for_the_accent_rotation():
    """apply_ui_accent() parses each default as '#rrggbb'; anything else is a
    silently ignored accent change."""
    for key, value in T.PALETTE_DEFAULTS.items():
        assert HEX6.match(value), f"{key} cannot be hue-rotated: {value!r}"


def test_geometry_vocabulary_is_closed():
    assert {T.RADIUS_LG, T.RADIUS_MD, T.RADIUS_SM} == {12, 9, 6}
    assert len({T.RADIUS_LG, T.RADIUS_MD, T.RADIUS_SM}) == 3


def test_there_are_exactly_three_stroke_weights():
    """One accent at three alphas — not four independent border colours."""
    parsed = [T._rgba_parts(v) for v in (T.BORDER, T.BORDER_SOFT, T.BORDER_FAINT)]
    hues = {p[:3] for p in parsed}
    assert len(hues) == 1, "the three stroke weights must share one hue"
    alphas = [p[3] for p in parsed]
    assert alphas == sorted(alphas, reverse=True)
    assert len({T.STROKE_STRONG, T.STROKE_MID, T.STROKE_HAIR}) == 3


def test_spacing_scale_is_small_and_ordered():
    assert T.SPACING_SCALE == tuple(sorted(T.SPACING_SCALE))
    assert len(T.SPACING_SCALE) <= 5


def test_control_heights_are_a_small_closed_set():
    assert len(T.CONTROL_HEIGHTS) == 3
    assert T.CONTROL_HEIGHTS == (28, 34, 38)


@pytest.mark.parametrize("value,expected", [
    ("#00d4ff", "#00d4ff"),
    ("#00D4FF", "#00d4ff"),
    ("rgba(0, 212, 255, 120)", "#00d4ff"),
    ("#0af", "#00aaff"),
    ("#00d4ff80", "#00d4ff"),
    ("", "#000000"),
    ("not-a-colour", "#000000"),
])
def test_hex_color_normalises_anything_qcolor_would_reject(value, expected):
    assert T.hex_color(value) == expected


def test_rgba_and_flatten_round_trip():
    assert T.rgba("#00d4ff", 120) == "rgba(0, 212, 255, 120)"
    flattened = T.flatten(T.rgba("#00d4ff", 120))
    assert HEX6.match(flattened)
    # A fully opaque token flattens to itself.
    assert T.flatten("#00d4ff") == "#00d4ff"


def test_mix_and_shade_stay_in_gamut():
    assert T.mix("#ffffff", "#000000", 0.5) == "#808080"
    assert T.mix("#ffffff", "#000000", 9.9) == "#ffffff"
    assert T.mix("#ffffff", "#000000", -3) == "#000000"
    assert T.shade("#808080", 2.0) == "#ffffff"
    assert T.shade("#808080", 0) == "#000000"


def test_qcolor_accepts_rgba_tokens():
    """The bug this helper exists for: QColor('rgba(...)') is invalid in PyQt6."""
    from PyQt6.QtGui import QColor
    assert not QColor(T.PANEL).isValid() or QColor(T.PANEL).alpha() == 255
    colour = T.qcolor(T.PANEL)
    assert colour.isValid()
    assert colour.alpha() == 240


@pytest.mark.parametrize("legacy,current", [
    ("#00d4ff", T.PALETTE["PRI"]),
    ("#007a99", T.PALETTE["PRI_DIM"]),
])
def test_legacy_accents_migrate(legacy, current):
    assert T.migrate_accent(legacy) == current


def test_custom_accents_pass_through_untouched():
    assert T.migrate_accent("#ff00aa") == "#ff00aa"
    assert T.migrate_accent("#AB12CD") == "#ab12cd"


def test_stylesheet_factories_produce_valid_css():
    """Doubled braces leaking into a stylesheet is invalid QSS that Qt silently
    ignores, which is exactly how a panel ends up unstyled. Every factory is
    checked in every theme, because the soft themes take different branches."""
    factories = [
        T.button_css, T.primary_button_css, T.accent_button_css, T.field_css,
        T.card_css, T.header_css, T.chip_css, T.section_css, T.title_css,
        T.subtitle_css, T.scrollbar_css, T.hidden_scrollbar_css, T.tab_css,
        T.slider_css, T.checkbox_css, T.dialog_css, T.tooltip_css,
        T.shell_dialog_css, T.nav_header_css, T.qtabbar_css, T.scroll_area_css,
        T.group_box_css, T.settings_surface_css, T.separator_css,
        T.micro_label_css, T.panel_title_css, T.panel_subtitle_css,
        T.panel_icon_css, T.status_chip_css, T.composed_app_css,
        T.soft_card_css, T.soft_row_css, T.toggle_css,
    ]
    for theme in T.THEMES:
        T.set_theme(theme)
        for factory in factories:
            sheet = factory()
            assert "{{" not in sheet and "}}" not in sheet, \
                f"{factory.__name__} has doubled braces in {theme}"
            assert sheet.count("{") == sheet.count("}"), \
                f"{factory.__name__} has unbalanced braces in {theme}"


def test_no_surface_declares_its_own_palette():
    """The audit that keeps this consolidated: none of the HUD modules may carry
    a private colour table."""
    allowed = {"ui_theme.py"}
    offenders = {}
    for name in ("ui.py", "ui_settings.py", "ui_layout.py"):
        text = (SOURCE_ROOT / name).read_text(encoding="utf-8")
        # A palette declaration looks like `NAME = "#rrggbb"` at module or class
        # level; local colour choices inside one widget are handled below.
        hits = re.findall(r"^\s{0,8}[A-Za-z_][A-Za-z0-9_]*\s*=\s*[\"']#[0-9a-fA-F]{6}[\"']", text, re.M)
        if hits:
            offenders[name] = hits
    for name in ("dashboard/brain3d.py",):
        text = (SOURCE_ROOT / name).read_text(encoding="utf-8")
        hits = re.findall(r"^\s{0,8}[A-Za-z_][A-Za-z0-9_]*\s*=\s*[\"']#[0-9a-fA-F]{6}[\"']", text, re.M)
        if hits:
            offenders[name] = hits
    assert not offenders, f"modules still declaring their own palette: {offenders}"
    assert allowed == {"ui_theme.py"}


# ── the theme contract ──────────────────────────────────────────────────────

def test_every_theme_carries_every_token():
    """A theme is the same vocabulary with different values, or a surface that
    works in one look would be a KeyError in another."""
    baseline = set(T.token_snapshot("hud"))
    assert baseline == set(T.TOKEN_NAMES)
    for theme in T.THEMES:
        assert set(T.token_snapshot(theme)) == baseline, f"{theme} is missing tokens"


def test_every_theme_declares_the_three_radii_in_order():
    for theme in T.THEMES:
        tokens = T.token_snapshot(theme)
        radii = (tokens["RADIUS_LG"], tokens["RADIUS_MD"], tokens["RADIUS_SM"])
        assert len(set(radii)) == 3, f"{theme} does not have three distinct radii"
        assert radii == tuple(sorted(radii, reverse=True)), f"{theme} radii are out of order"
        assert all(isinstance(r, int) and r > 0 for r in radii)
    # The HUD stays crisp; both soft themes are rounder.
    assert (T.token_snapshot("hud")["RADIUS_LG"], T.token_snapshot("hud")["RADIUS_SM"]) == (12, 6)
    for theme in ("soft_dark", "soft_light"):
        assert T.token_snapshot(theme)["RADIUS_LG"] >= 16


def test_the_hud_is_flat_and_the_soft_themes_are_elevated():
    hud = T.token_snapshot("hud")
    assert hud["SOFT"] is False and hud["SHADOW_ALPHA"] == 0 and hud["CORNER_CUT"] > 0
    for theme in ("soft_dark", "soft_light"):
        tokens = T.token_snapshot(theme)
        assert tokens["SOFT"] is True
        assert tokens["SHADOW_ALPHA"] > 0, f"{theme} has no elevation"
        assert tokens["CORNER_CUT"] == 0, f"{theme} still cuts its corners"
        assert tokens["THEME_MODE"] in ("dark", "light")


@pytest.mark.parametrize("theme", list(T.THEMES))
def test_text_stays_readable_in_every_theme(theme):
    """A look is not allowed to cost legibility: the body ramp, the accent used
    as text and the text on the filled action button all have floors."""
    tokens = T.token_snapshot(theme)
    bg = tokens["BG"]
    assert T.contrast_ratio(tokens["TEXT"], bg) >= 7.0, f"{theme} body text is too faint"
    assert T.contrast_ratio(tokens["TEXT_MID"], bg) >= 4.5, f"{theme} secondary text is too faint"
    assert T.contrast_ratio(tokens["TEXT_DIM"], bg) >= 3.0, f"{theme} dim text is too faint"
    assert T.contrast_ratio(tokens["ACCENT_TEXT"], bg) >= 4.0, f"{theme} accent text is too faint"
    assert T.contrast_ratio(tokens["ON_ACCENT"], tokens["ACCENT_GRADIENT_START"]) >= 4.5, \
        f"{theme} button text does not clear its fill"


def test_the_light_theme_is_actually_light():
    """A light theme that quietly keeps the dark field is the classic half-done
    theme, so the direction is asserted, not just the presence of tokens."""
    hud = T.token_snapshot("hud")
    light = T.token_snapshot("soft_light")
    assert T._rgba_parts(hud["BG"])[0] < 30
    assert min(T._rgba_parts(light["BG"])[:3]) > 200
    # The text ramp flips with the field: dark ink on a light page.
    assert max(T._rgba_parts(light["TEXT"])[:3]) < 70
    assert min(T._rgba_parts(hud["TEXT"])[:3]) > 150


@pytest.mark.parametrize("value,expected", [
    ("hud", "hud"),
    ("HUD", "hud"),
    ("soft_dark", "soft_dark"),
    ("soft dark", "soft_dark"),
    ("Soft-Light", "soft_light"),
    ("dark", "soft_dark"),
    ("light", "soft_light"),
    ("", "hud"),
    ("nonsense", "hud"),
])
def test_theme_names_normalise(value, expected):
    assert T.normalize_theme(value) == expected


def test_set_theme_installs_tokens_and_reports_unknown_names():
    assert T.set_theme("soft_light") is True
    assert T.current_theme() == "soft_light"
    assert T.RADIUS_LG == T.token_snapshot("soft_light")["RADIUS_LG"]
    assert T.BG == T.token_snapshot("soft_light")["BG"]
    # An unknown name must fall back rather than leave the UI half-themed.
    assert T.set_theme("plaid") is False
    assert T.current_theme() == "hud"


def test_palette_is_rebuilt_in_place_on_a_switch():
    """`class C` in ui.py holds this dict object, so a switch has to mutate it
    rather than replace it."""
    holder = T.PALETTE
    hud_bg = T.PALETTE["BG"]
    T.set_theme("soft_light")
    assert T.PALETTE is holder, "the palette object was replaced, not updated"
    assert T.PALETTE["BG"] != hud_bg
    assert T.PALETTE_DEFAULTS["BG"] == T.PALETTE["BG"]
    assert all(isinstance(v, str) and v.startswith("#") and len(v) == 7
               for v in T.PALETTE.values())


def test_theme_listeners_are_told_what_changed():
    seen = []
    listener = lambda name, old, new: seen.append((name, old["BG"], new["BG"]))  # noqa: E731
    T.add_theme_listener(listener)
    try:
        T.set_theme("soft_dark")
    finally:
        T.remove_theme_listener(listener)
    assert seen and seen[-1][0] == "soft_dark"
    assert seen[-1][1] != seen[-1][2]


def test_a_broken_listener_cannot_break_the_switch():
    def _boom(_name, _old, _new):
        raise RuntimeError("listener exploded")
    T.add_theme_listener(_boom)
    try:
        assert T.set_theme("soft_dark") is True
        assert T.current_theme() == "soft_dark"
    finally:
        T.remove_theme_listener(_boom)


def test_theme_swap_map_only_reports_colour_changes():
    """Labels and numbers must never enter the swap: substituting those as
    strings would rewrite unrelated text inside a stylesheet."""
    old = T.token_snapshot("hud")
    new = T.token_snapshot("soft_light")
    swap = T.theme_swap_map(old, new)
    assert swap  # the two themes do differ
    for key, value in swap.items():
        assert value != key
        assert T._looks_like_colour(key) and T._looks_like_colour(value)
        assert value in new.values(), f"{key} -> {value} is not a value of the new theme"
    assert swap[old["BG"]] == new["BG"]
    assert swap[old["ACCENT"]] == new["ACCENT"]
    assert old["THEME_SUMMARY"] not in swap
    assert old["THEME_LABEL"] not in swap
    # Two themes that share a value must not map it onto anything.
    assert T.theme_swap_map(old, dict(old)) == {}


def test_remap_stylesheet_moves_values_and_radii_together():
    sheet = ("QPushButton { color:#00d4ff; background:#02060c;"
             f" border-radius:{T.token_snapshot('hud')['RADIUS_SM']}px; }}")
    updated = T.remap_stylesheet(sheet, T.theme_swap_map(T.token_snapshot("hud"),
                                                        T.token_snapshot("soft_light")),
                                 (12, 9, 6), (18, 14, 10), "light")
    assert "#00d4ff" not in updated
    assert "border-radius:10px" in updated
    assert updated.count("{") == updated.count("}")


def test_dark_washes_flip_with_the_field():
    """A literal `rgba(1,18,29,115)` inset is not a token, but it is still a
    wash: it has to move to the light side of the field."""
    dark = "QLabel { background: rgba(1, 18, 29, 115); color: #fff; }"
    light = T.flip_dark_washes(dark, "light")
    assert "rgba(1, 18, 29, 115)" not in light
    assert "rgba(243, 247, 251, 115)" in light
    # An accent tint is not a wash and must survive untouched.
    tint = "QLabel { background: rgba(0, 212, 255, 40); }"
    assert T.flip_dark_washes(tint, "light") == tint
    # And the flip is a function of the target only: stable on a round trip.
    back = T.flip_dark_washes(light, "dark")
    assert "rgba(8, 13, 20, 115)" in back


def test_soft_ui_helpers_exist_for_both_modes():
    T.set_theme("soft_dark")
    assert "border-radius" in T.soft_card_css()
    assert "QFrame" in T.soft_row_css()
    assert "min-height" in T.toggle_css()
    for state, expected in (("ready", T.GREEN), ("missing_key", T.AMBER),
                            ("unavailable", T.RED), ("testing", T.ACCENT_TEXT)):
        assert expected in T.state_pill_css(state), f"{state} lost its colour"


def test_apply_soft_shadow_is_flat_in_the_hud_and_elevated_in_the_soft_themes(qapp=None):
    """One effect per card, cleared again by the flat theme."""
    from PyQt6.QtWidgets import QApplication, QFrame, QGraphicsDropShadowEffect
    app = QApplication.instance() or QApplication([])
    frame = QFrame()
    try:
        T.set_theme("hud")
        assert T.apply_soft_shadow(frame, 1) is None
        assert frame.graphicsEffect() is None
        T.set_theme("soft_light")
        effect = T.apply_soft_shadow(frame, 1)
        assert isinstance(effect, QGraphicsDropShadowEffect)
        assert isinstance(frame.graphicsEffect(), QGraphicsDropShadowEffect)
        T.set_theme("hud")
        T.apply_soft_shadow(frame, 1)
        assert frame.graphicsEffect() is None
    finally:
        frame.deleteLater()
        del app
