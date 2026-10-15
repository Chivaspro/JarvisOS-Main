"""Activity feed incremental paint — offscreen.

The feed used to rebuild every card widget on every arriving log line
(``_render()`` per ``add_entry``), so a burst of lines destroyed and recreated
up to 60 cards per line on the UI thread and the HUD visibly froze. The feed
now inserts only the new card and trims the oldest at the cap.

Verifies:
* existing cards keep their widget identity across inserts (no rebuild);
* the widget count never exceeds MAX_ENTRIES (+ the trailing stretch);
* the entry store keeps exactly MAX_ENTRIES newest-first entries;
* a filtered-out entry paints nothing but is still recorded, and a matching
  entry paints immediately;
* a filter change still repaints the full view from the stored entries.
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

_ui_spec = importlib.util.spec_from_file_location("ui_impl_activity_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


def _cards(feed):
    """Card widgets in layout order (the trailing stretch is not a widget)."""
    out = []
    for i in range(feed._cl.count()):
        item = feed._cl.itemAt(i)
        if item is not None and item.widget() is not None:
            out.append(item.widget())
    return out


def test_existing_cards_are_not_rebuilt(qapp):
    feed = ui._ActivityTimeline()
    feed.add_entry("SYS: first")
    first_card = _cards(feed)[0]
    for n in range(6):
        feed.add_entry(f"SYS: line {n}")
    qapp.processEvents()
    cards = _cards(feed)
    assert first_card in cards, "the first card was destroyed and rebuilt"
    assert len(cards) == 7, "one card per visible entry expected"
    assert len(feed._entries) == 7


def test_cap_trims_widgets_and_entries(qapp):
    feed = ui._ActivityTimeline()
    for n in range(feed.MAX_ENTRIES + 5):
        feed.add_entry(f"TOOL: run {n}")
    qapp.processEvents()
    assert len(feed._entries) == feed.MAX_ENTRIES
    assert len(_cards(feed)) == feed.MAX_ENTRIES
    # Newest first: the cap must drop the oldest lines, not the newest.
    assert feed._entries[0]["text"] == f"TOOL: run {feed.MAX_ENTRIES + 4}"


def test_filtered_entry_is_recorded_without_painting(qapp):
    feed = ui._ActivityTimeline()
    feed.set_filter("ERROR")
    feed.add_entry("OK: all good")
    qapp.processEvents()
    assert _cards(feed) == [], "a filtered-out entry must not paint"
    assert len(feed._entries) == 1, "the entry is still stored"
    feed.add_entry("ERR: boom")
    qapp.processEvents()
    assert len(_cards(feed)) == 1
    assert feed._entries[0]["tag"] == "ERROR"


def test_filter_change_repaints_from_store(qapp):
    feed = ui._ActivityTimeline()
    for n in range(4):
        feed.add_entry(f"TOOL: item {n}")
    feed.add_entry("ERR: failed")
    feed.set_filter("TOOL")
    qapp.processEvents()
    assert len(_cards(feed)) == 4
    feed.set_filter("ALL")
    qapp.processEvents()
    assert len(_cards(feed)) == 5
