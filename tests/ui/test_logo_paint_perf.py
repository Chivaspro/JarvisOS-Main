"""Logo paint / chat-link micro-optimizations (offscreen, no MainWindow).

Two targeted speed fixes on hot paths:

* ``_logo_text_layout`` caches the logo title's QFont + per-letter advances
  (previously rebuilt every 60fps frame). Must be a real cache, recompute on a
  font swap, and equal the old per-frame ``p.fontMetrics()`` result.
* ``_ChatBubble._linkify`` uses precompiled patterns; output must be identical
  to the original ``re.sub(pattern, ..., flags=re.I)`` implementation.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from .qt_harness import qapp  # noqa: F401,E402

_spec = importlib.util.spec_from_file_location("ui_impl_logo_perf_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


# ── cached logo-title layout ────────────────────────────────────────────────

def test_layout_is_cached_and_recomputed_on_font_change(qapp):
    ui._LOGO_TEXT_LAYOUT = None
    ui._LOGO_TEXT_LAYOUT_KEY = None
    a = ui._logo_text_layout()
    b = ui._logo_text_layout()
    assert a is b, "second call must reuse the cached tuple, not re-measure"

    saved_font = ui.DISPLAY_FONT
    try:
        ui.DISPLAY_FONT = "Some Other Font 123"
        c = ui._logo_text_layout()
        assert c is not a, "a font swap must invalidate the cache"
        assert ui._logo_text_layout() is c
    finally:
        ui.DISPLAY_FONT = saved_font
        ui._LOGO_TEXT_LAYOUT = None
        ui._LOGO_TEXT_LAYOUT_KEY = None


def test_layout_matches_per_frame_fontmetrics(qapp):
    """The cached widths/baseline must equal what the old inline code computed
    from ``p.fontMetrics()`` — otherwise the logo text would shift."""
    from PyQt6.QtGui import QFont, QFontMetrics
    from PyQt6.QtWidgets import QLabel
    lbl = QLabel("x")
    font, widths, total, baseline = ui._logo_text_layout()
    # Reference: exactly the pre-optimization computation.
    ref_font = QFont(ui.DISPLAY_FONT)
    ref_font.setPixelSize(ui.LOGO_TEXT_SIZE)
    fm = QFontMetrics(ref_font)
    ref_widths = [fm.horizontalAdvance(ch) for ch in ui.LOGO_TITLE]
    assert widths == ref_widths
    assert total == sum(ref_widths) + ui.LOGO_TEXT_TRACKING * (len(ui.LOGO_TITLE) - 1)
    assert baseline == (fm.ascent() - fm.descent()) / 2.0
    assert len(widths) == len(ui.LOGO_TITLE)
    lbl.deleteLater()


# ── precompiled linkify equivalence ─────────────────────────────────────────

def _linkify_reference(text: str) -> str:
    import html, re
    safe = html.escape(str(text or '')).replace('\n', '<br/>')
    safe = re.sub(
        r'\[([^\]]+)\]\((https?://[^\s\)]+)\)',
        lambda m: f'<a href="{html.escape(m.group(2), quote=True)}" style="color:{ui.C.PRI};text-decoration:none;">{m.group(1)}</a>',
        safe, flags=re.I)
    safe = re.sub(
        r'(?<![="/])(https?://[^\s<]+)',
        lambda m: f'<a href="{html.escape(m.group(1).rstrip(".,!?;:"), quote=True)}" style="color:{ui.C.PRI};text-decoration:none;">{html.escape(m.group(1))}</a>',
        safe, flags=re.I)
    return safe


@pytest.mark.parametrize("text", [
    "hello world",
    "see [docs](https://example.com/a) now",
    "visit https://example.com/path?q=1 for more",
    "trailing https://a.b.c.",
    "two https://x.io and https://y.io!",
    "<script>escape & keep https://z.org</script>",
    "[label](HTTP://UPPER.com)",
    "no trailing https://p.com,,,",
    "",
])
def test_linkify_output_is_identical_to_reference(text):
    assert ui._ChatBubble._linkify(text) == _linkify_reference(text)
