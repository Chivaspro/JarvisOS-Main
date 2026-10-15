"""HoloGlobe pane — the surface, and the HUD wiring around it.

The suite runs with ``JARVIS_DISABLE_WEBENGINE=1`` (see tests/conftest.py), so
these tests exercise exactly the path an install without PyQt6-WebEngine takes:
the pane must still state what it was asked to plot, and must never claim a
renderer it does not have. The scene itself lives in ``assets/globe`` and is
driven through ``window.jarvisGlobe``; the pane's job is to hand it correct
data and to report honestly what happened.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from .test_camera_pipeline import (  # noqa: F401  (fixtures)
    _NullAnalyzer, _pump, camera, qapp, ui,
)


TOKYO = {"name": "Tokyo", "latitude": 35.6762, "longitude": 139.6503,
         "country": "Japan", "source": "gazetteer"}
LONDON = {"name": "London", "latitude": 51.5074, "longitude": -0.1278,
          "country": "United Kingdom", "source": "gazetteer"}


def _pane():
    pane = ui.GlobePane(host=None)
    pane._ensure_view()          # no WebEngine in the suite: takes the fallback
    return pane


# ── the pane, without a renderer ─────────────────────────────────────────────

def test_pane_reports_the_missing_renderer_instead_of_pretending(qapp):
    pane = _pane()
    assert pane._view is None
    assert "PyQt6-WebEngine" in pane._notice.text()
    assert "UNAVAILABLE" in pane._status.text()
    pane.deleteLater()


def test_plotting_states_what_it_was_given_even_with_no_renderer(qapp):
    pane = _pane()
    pane.show_places([TOKYO], source="gazetteer")
    assert [p["name"] for p in pane._places] == ["Tokyo"]
    assert "Tokyo" in pane._legend.text()
    assert "35.68" in pane._legend.text()
    # No renderer in the suite, so the status must not claim to be drawing: it
    # names how many places were resolved instead.
    assert "NO RENDERER · 1 PLACE(S) RESOLVED" in pane._status.text()
    # The call is held until the page says it is ready — it is never dropped.
    assert any("setPlaces(" in call for call in pane._pending)
    pane.deleteLater()


def test_showing_the_card_again_does_not_erase_the_last_status(qapp):
    pane = _pane()
    pane.show_places([TOKYO])
    pane._ensure_view()          # what showEvent does on every open
    assert "RESOLVED" in pane._status.text()
    pane.deleteLater()


def test_a_place_without_usable_coordinates_is_not_plotted(qapp):
    pane = _pane()
    pane.show_places([{"name": "Atlantis", "latitude": "somewhere",
                       "longitude": 12}, {"name": "Nowhere"}])
    assert pane._places == []
    assert "NOTHING PLOTTED" in pane._status.text()
    assert pane._legend.text().startswith("No place plotted")
    pane.deleteLater()


def test_out_of_range_coordinates_are_refused(qapp):
    pane = _pane()
    pane.show_places([{"name": "Off the map", "latitude": 900, "longitude": 900}])
    assert pane._places == []
    pane.deleteLater()


def test_the_page_saying_ready_releases_the_held_calls(qapp):
    pane = _pane()
    pane.show_places([TOKYO])
    assert pane._pending
    pane._on_js_event("ready", "{}")
    assert pane._pending == []
    assert "EARTH ONLINE" in pane._status.text()
    pane.deleteLater()


def test_the_title_wire_is_read_when_the_channel_is_unavailable(qapp):
    """A page with no WebChannel still reports through its title."""
    pane = _pane()
    pane._on_title("jarvis:places:" + json.dumps({"count": 3}))
    assert "3 MARKER(S) ONLINE" in pane._status.text()
    pane._on_title("jarvis:focus:" + json.dumps({"name": "tokyo"}))
    assert "FOCUSED TOKYO" in pane._status.text()
    pane._on_title("just a page title")          # not our wire: ignored
    assert "FOCUSED TOKYO" in pane._status.text()
    pane.deleteLater()


def test_focus_only_works_for_a_place_that_is_actually_on_the_globe(qapp):
    pane = _pane()
    pane.show_places([TOKYO, LONDON])
    assert pane.focus_place("Tokyo") is True
    assert pane.focus_place("Paris") is False
    pane.deleteLater()


def test_clear_empties_the_legend_and_the_held_places(qapp):
    pane = _pane()
    pane.show_places([TOKYO])
    pane.clear()
    assert pane._places == []
    assert "CLEARED" in pane._status.text()
    assert pane._legend.text().startswith("No place plotted")
    pane.deleteLater()


def test_the_legend_names_at_most_eight_places_then_counts_the_rest(qapp):
    many = [dict(TOKYO, name=f"City {index}") for index in range(11)]
    pane = _pane()
    pane.show_places(many)
    assert pane._legend.text().count("·") == 9      # eight names plus the total
    assert "+3 more" in pane._legend.text()
    pane.deleteLater()


# ── the HUD ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def window(qapp, monkeypatch, camera):  # noqa: F811
    """A real HUD whose config lives outside the shared one.

    ``tempfile`` rather than pytest's ``tmp_path``: on this machine the pytest
    temp root is not writable (``WinError 5``), which would turn every HUD test
    here into a fixture error rather than a result.
    """
    with tempfile.TemporaryDirectory(prefix="jarvis-globe-") as tmp:
        root = Path(tmp)
        config_dir = root / "config"
        config_dir.mkdir(exist_ok=True)
        config = {"assistant_name": "JARVIS", "user_name": "Operator",
                  "gemini_api_key": "test-key", "os_system": "windows",
                  "features": {"remember_position": True,
                               "floating_windows": True}}
        config_path = config_dir / "api_keys.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setattr(ui, "CONFIG_DIR", config_dir)
        monkeypatch.setattr(ui, "API_FILE", config_path)
        monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(config))
        monkeypatch.setattr(ui._FeatheredCamView, "_save_geom", lambda self: None)
        monkeypatch.setattr(ui._FeatheredCamView, "_load_geom", lambda self: None)
        monkeypatch.setattr(ui.RefinedCommandWindow, '_ensure_vision_analyzer',
                            lambda self: _NullAnalyzer())
        # The world monitor fetches a live RSS feed 400 ms after it is built.
        # A HUD test must not open a socket, so the fetch entry point is stubbed
        # entirely; the fetch itself now runs on Qt's event loop, which also
        # means no worker thread can outlive its window.
        monkeypatch.setattr(ui.WorldMonitorPane, "refresh_feeds", lambda self: None)

        win = ui.MainWindow(str(root / "face.png"))
        qapp.processEvents()
        yield win
        try:
            win._shutdown_camera_threads(timeout=5.0)
            for name in ("_cam_live_lbl", "_cam_preview", "_globe_panel"):
                child = getattr(win, name, None)
                if child is not None:
                    child.close()
            qapp.processEvents()
            win.close()
        except Exception:
            pass
        qapp.processEvents()


def test_the_globe_is_a_layout_studio_target(window):
    assert window._globe_panel.property("_jarvis_panel_kind") == "globe"
    assert window._system_layout_widgets["HoloGlobe"] is window._globe_panel
    assert window._globe_panel.isHidden(), "panels stay closed until they are asked for"


def test_show_globe_plots_the_evidence_and_surfaces_the_card(window, qapp):
    window.show_globe([TOKYO], source="gazetteer", focus="Tokyo")
    _pump(qapp, 0.2)
    assert [p["name"] for p in window._globe_pane._places] == ["Tokyo"]
    assert window._globe_panel.isVisible()
    assert "Tokyo" in window._globe_pane._legend.text()


def test_clear_globe_takes_the_markers_back_down(window, qapp):
    window.show_globe([TOKYO, LONDON])
    _pump(qapp, 0.2)
    window.clear_globe()
    _pump(qapp, 0.2)
    assert window._globe_pane._places == []


def test_a_plot_with_nothing_in_it_still_answers_honestly(window, qapp):
    window.show_globe([], source="geocoding service")
    _pump(qapp, 0.2)
    assert window._globe_pane._places == []
    assert "NOTHING PLOTTED" in window._globe_pane._status.text()
