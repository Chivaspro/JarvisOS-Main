"""The HoloGlobe: where a place is, and what the globe is told to draw.

Two rules are worth a test each, because both are easy to get wrong in a way
that looks fine on screen:

* a place that cannot be resolved is reported in words — never dropped as a
  marker at 0,0, which reads as "somewhere in the Atlantic";
* the pane is painted from the *same* result the model is handed, so the HUD
  can never show a marker the model was not told about.
"""
from __future__ import annotations

import pytest

from capabilities import actions as local_actions
from capabilities import geospatial, registry


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No test may depend on the geocoding service.

    Making every outbound call fail proves the interesting half of the design:
    the places in the gazetteer resolve with no network at all, and everything
    else fails *honestly* instead of silently.
    """
    import requests

    def _boom(*a, **k):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(requests, "get", _boom)


# ── resolution ───────────────────────────────────────────────────────────────

def test_gazetteer_places_resolve_offline_and_exactly():
    place, error = geospatial.resolve("Tokyo")
    assert error == ""
    assert place["name"] == "Tokyo"
    assert place["latitude"] == pytest.approx(35.6762)
    assert place["longitude"] == pytest.approx(139.6503)
    assert place["source"] == "gazetteer"


def test_aliases_resolve_to_the_real_city():
    for alias in ("NYC", "nyc", "New York City"):
        place, error = geospatial.resolve(alias)
        assert error == "", alias
        assert place["name"] == "New York"


def test_an_explicit_coordinate_pair_is_used_verbatim():
    place, error = geospatial.resolve("51.5, -0.12")
    assert error == ""
    assert place["source"] == "coordinates"
    assert place["latitude"] == pytest.approx(51.5)
    assert place["longitude"] == pytest.approx(-0.12)


def test_impossible_coordinates_are_not_accepted_as_a_position():
    """'900, 900' is not a place — it must not become a marker anywhere."""
    result = geospatial.geocode("900, 900")
    assert result["ok"] is False


def test_an_unknown_place_is_reported_rather_than_dropped():
    place, error = geospatial.resolve("Nowhereville")
    assert place is None
    assert "Nowhereville" in error
    assert "latitude and longitude" in error, (
        "the refusal has to tell the model what a usable answer looks like")


def test_names_that_cannot_be_told_apart_are_refused():
    """'new' matches New York, New Delhi and Newcastle — an ambiguous prefix
    must return nothing rather than pick one."""
    place, error = geospatial.resolve("new")
    assert place is None


# ── the resolver, as the tool calls it ───────────────────────────────────────

def test_show_plots_every_place_it_can_and_names_the_one_it_cannot():
    result = geospatial.run("show", {"places": "London, Paris, Nowhereville"})
    assert result["ok"] is True
    assert [p["name"] for p in result["places"]] == ["London", "Paris"]
    assert len(result["failed"]) == 1
    assert "Nowhereville" in result["detail"]
    assert "Nowhereville" in result["failed"][0]


def test_show_that_places_nothing_is_not_reported_as_success():
    result = geospatial.run("show", {"place": "Nowhereville"})
    assert result["ok"] is False
    assert result["reason"] == "not_found"
    assert result["places"] == []


def test_a_single_place_is_focused_and_several_are_left_where_they_are():
    one = geospatial.run("show", {"place": "Reykjavik"})
    assert one["focus"] == "Reykjavik"
    many = geospatial.run("show", {"places": "London, Berlin"})
    assert many["focus"] == ""


def test_focus_resolves_exactly_one_place():
    result = geospatial.run("focus", {"place": "Cairo"})
    assert result["ok"] is True
    assert result["focus"] == "Cairo"
    assert len(result["places"]) == 1


def test_clear_needs_no_resolution_and_plots_nothing():
    result = geospatial.run("clear", {})
    assert result["ok"] is True and result["cleared"] is True
    assert result["places"] == []


def test_an_unknown_globe_action_lists_the_real_ones():
    result = geospatial.run("teleport", {})
    assert result["ok"] is False
    assert result["reason"] == "unknown_action"
    assert "show" in result["detail"] and "clear" in result["detail"]


# ── the registry ─────────────────────────────────────────────────────────────

def test_globe_is_a_registered_tool_with_its_own_vocabulary():
    assert registry.TOOL_GLOBE in registry.TOOLS
    assert registry.names(registry.TOOL_GLOBE) == ["show", "focus", "clear",
                                                   "list", "status"]
    params = registry.parameters(registry.TOOL_GLOBE)
    assert params["required"] == ["action"]
    assert params["properties"]["action"]["enum"] == registry.names(
        registry.TOOL_GLOBE)
    assert params["properties"]["place"]["type"] == "STRING"


def test_show_without_a_place_is_refused_before_anything_is_looked_up():
    action, error = registry.validate("globe", {"action": "show"})
    assert action is None
    assert "'place'" in error


def test_an_unknown_action_is_refused_with_the_valid_list():
    action, error = registry.validate("globe", {"action": "zoom"})
    assert action is None
    assert "zoom" in error and "status" in error


# ── the handler the model actually calls ─────────────────────────────────────

def _run(args, enabled=None):
    """Call the handler and capture the evidence it hands to the pane."""
    seen: dict = {}

    def _capture(action_name, data, ok):
        seen.update({"action": action_name, "data": data, "ok": ok})

    text = local_actions.handle_globe(args, enabled=enabled, on_result=_capture)
    return text, seen


def test_the_model_and_the_pane_are_given_the_same_coordinates():
    text, seen = _run({"action": "show", "place": "Tokyo"})
    assert seen["ok"] is True
    place = seen["data"]["places"][0]
    assert place["name"] == "Tokyo"
    # The sentence the model reads and the numbers the pane draws come from one
    # lookup: 35.68 appears in both, so they cannot disagree.
    assert f"{place['latitude']:.2f}" in text
    assert seen["data"]["focus"] == "Tokyo"


def test_a_revoked_desktop_control_switch_stops_the_globe_and_says_so():
    text, seen = _run({"action": "show", "place": "Tokyo"},
                      enabled=lambda feature: feature != "desktop_control")
    assert "switched off in Settings" in text
    # The hook is still called — honest reporting — but with nothing to draw, so
    # the pane can never paint a marker for an action that did not run.
    assert seen["ok"] is False
    assert seen["data"] == {}


def test_asking_what_can_be_placed_works_with_everything_switched_off():
    text, seen = _run({"action": "list"}, enabled=lambda feature: False)
    assert "Places known offline" in text
    assert seen["ok"] is True
    assert seen["data"]["evidence"]["count"] == len(geospatial.GAZETTEER)


def test_clearing_reaches_the_pane_as_a_clear_not_as_a_plot():
    _text, seen = _run({"action": "clear"})
    assert seen["ok"] is True
    assert seen["data"]["cleared"] is True
    assert seen["data"]["places"] == []


def test_focus_tells_the_pane_which_place_to_turn_to():
    _text, seen = _run({"action": "focus", "place": "Osaka"})
    assert seen["action"] == "focus"
    assert seen["data"]["focus"] == "Osaka"


def test_a_place_that_cannot_be_found_is_not_reported_as_plotted():
    text, seen = _run({"action": "show", "place": "Nowhereville"})
    assert seen["ok"] is False
    assert seen["data"]["places"] == []
    assert "could not place" in text
