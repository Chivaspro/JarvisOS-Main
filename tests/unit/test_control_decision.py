"""One request, several possible controllers — the layer that picks one.

"Play music" opened a web page on a machine with Spotify installed because
nothing decided; the handler simply had a hardcoded order. The control-decision
layer is the fix: it reads what is really installed, running and switched on,
and returns an ordered, explained plan.

These tests pin the policy with injected facts, so they never probe the real
machine and never touch Spotify, Playwright or a screen:

* classification is conservative — conversational sentences must not become
  automation;
* a plan prefers the installed application over a web page, the structured
  controller over a coordinate, and always reports what is NOT ready with the
  reason;
* planning executes nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capabilities import decide  # noqa: E402

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")


def _state(*, media=("spotify",), preferred="spotify", desktop=True,
           playwright=True, uitars=True, tars=False, reach=False) -> dict:
    """Facts shaped exactly like ``decide.facts()`` returns them."""
    return {
        "media": {
            "available": bool(media),
            "installed": {name: rf"C:\apps\{name}.exe" for name in media},
            "preferred": preferred if media else "",
            "detail": "probed",
        },
        "controllers": {
            "desktop": {"usable": desktop,
                        "detail": "pyautogui + pywinauto ready" if desktop
                                  else "missing: pyautogui"},
            "playwright": {"usable": playwright,
                           "detail": "ready" if playwright else "playwright missing"},
            "uitars_visual": {"usable": uitars,
                              "detail": "ready" if uitars else "UI-TARS SDK missing"},
            "power_control": {"usable": True, "detail": "ready"},
        },
        "agent_tars": {"usable": tars, "detail": "ready" if tars else "node missing"},
        "agent_reach": {"available": reach, "detail": "ready" if reach else "missing"},
        "switches": {name: True for name in decide._SWITCHES},
    }


def _off(state: dict, *names: str) -> dict:
    for name in names:
        state["switches"][name] = False
    return state


def _controllers(result: dict) -> list[str]:
    return [candidate["controller"] for candidate in result["candidates"]]


# ── classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize("spoken,expected", [
    ("play daft punk", "media"),
    ("play some music", "media"),
    ("pause the music", "media"),
    ("turn the volume up", "media"),
    ("open notepad", "app"),
    ("launch spotify", "app"),
    ("close excel", "app"),
    ("open youtube in the browser", "web"),
    ("go to https://example.com", "web"),
    ("click the blue button", "screen"),
    ("type into the search field", "screen"),
    ("research the best laptops of 2026", "research"),
    ("what is the capital of France", "chat"),
    ("tell me a joke", "chat"),
])
def test_classification_is_conservative(spoken, expected):
    intent, why = decide.classify(spoken)

    assert intent == expected, f"{spoken!r} → {intent} ({why})"
    assert why


def test_a_named_application_beats_the_media_vocabulary():
    # "open Spotify" contains a media word, but the user asked for an app.
    assert decide.classify("open spotify")[0] == "app"


def test_an_explicit_intent_overrides_classification():
    result = decide.plan("do the thing", intent="screen", facts=_state())

    assert result["intent"] == "screen"
    assert result["reason"] == "the caller named the intent"


# ── media: the installed application first ────────────────────────────────

def test_music_prefers_the_installed_player():
    result = decide.plan("play daft punk", facts=_state())

    assert result["controller"] == "media"
    assert result["tool"] == "play_music"
    assert result["args"]["query"] == "daft punk"
    assert _controllers(result) == ["media", "webview", "youtube"]
    assert result["ready"] is True
    assert "installed player" in result["candidates"][0]["reason"]


def test_music_falls_back_to_the_internal_player_without_one(monkeypatch):
    result = decide.plan("play daft punk", facts=_state(media=()))

    assert result["controller"] == "webview"
    assert result["candidates"][0]["ready"] is False
    assert "no native music player" in result["candidates"][0]["reason"]


def test_switching_native_players_off_is_respected():
    result = decide.plan("play daft punk",
                         facts=_off(_state(), "native_music_apps"))

    assert result["controller"] == "webview"
    assert "switched off" in result["candidates"][0]["reason"]


def test_no_player_and_no_webview_leaves_youtube():
    state = _off(_state(media=()), "webview_engine", "webview_music")

    result = decide.plan("play daft punk", facts=state)

    assert result["controller"] == "youtube"
    assert result["tool"] == "youtube_video"


# ── the other chains ─────────────────────────────────────────────────────

def test_a_web_request_prefers_the_structured_browser_controller():
    result = decide.plan("open the dashboard at https://example.com",
                         facts=_state())

    assert result["controller"] == "playwright"
    assert result["tool"] == "browser_action"
    assert result["args"]["url"] == "https://example.com"


def test_a_web_request_without_playwright_reports_what_is_ready():
    result = decide.plan("open https://example.com in the browser",
                         facts=_state(playwright=False, tars=False))

    assert result["controller"] == "webview"
    assert result["candidates"][0]["ready"] is False
    assert "playwright" in result["candidates"][0]["reason"]


def test_a_screen_request_prefers_visual_control_when_it_is_ready():
    result = decide.plan("click the blue button", facts=_state())

    assert result["controller"] == "uitars"
    assert result["tool"] == "visual_control"


def test_a_screen_request_falls_back_to_physical_input():
    result = decide.plan("click the blue button", facts=_state(uitars=False))

    assert result["controller"] == "pyautogui"
    assert result["tool"] == "pc_control"


def test_a_screen_request_with_everything_off_says_so():
    state = _off(_state(uitars=False), "physical_input")

    result = decide.plan("click the blue button", facts=state)

    assert result["ready"] is False
    assert "switched off" in result["candidates"][1]["reason"]


def test_an_app_request_uses_the_desktop_controller_and_names_the_app():
    result = decide.plan("open notepad", facts=_state())

    assert result["controller"] == "desktop"
    assert result["args"] == {"action": "open_application", "app": "notepad"}


def test_an_app_request_respects_the_master_switch():
    result = decide.plan("open notepad", facts=_off(_state(), "desktop_control"))

    assert result["candidates"][0]["ready"] is False
    assert "desktop control is switched off" in result["candidates"][0]["reason"]


def test_research_prefers_agent_reach_and_keeps_browser_fallbacks():
    result = decide.plan("research the best laptops", facts=_state(reach=True))

    assert result["controller"] == "agent_reach"
    assert result["tool"] == "web_research"
    assert "playwright" in _controllers(result)


def test_research_without_agent_reach_uses_the_browser():
    result = decide.plan("research the best laptops", facts=_state(reach=False))

    assert result["controller"] == "playwright"


def test_a_conversational_request_needs_no_controller():
    result = decide.plan("tell me a joke", facts=_state())

    assert result["intent"] == "chat"
    assert result["controller"] == ""
    assert result["candidates"] == []


# ── the plan is advice, never execution ───────────────────────────────────

def test_planning_never_probes_when_facts_are_given(monkeypatch):
    monkeypatch.setattr(decide, "_live_facts",
                        lambda: pytest.fail("plan must not probe when facts are given"))

    result = decide.plan("play daft punk", facts=_state())

    assert result["controller"] == "media"


def test_the_report_names_the_route_the_reasons_and_the_fallbacks():
    text = decide.report("play daft punk", intent="media")

    assert "CONTROL PLAN" in text
    assert "media" in text
    assert "facts:" in text
    for name in ("media", "webview", "youtube"):
        assert name in text


def test_the_report_for_a_conversation_sends_no_controller():
    text = decide.report("tell me a joke")

    assert "No controller is needed" in text


# ── wiring: the model can ask, and the handler order agrees ───────────────

def test_control_plan_is_declared_and_dispatched_in_main():
    assert '"name": "control_plan"' in MAIN_SOURCE
    assert "_decide.report(" in MAIN_SOURCE
    assert "from capabilities import decide as _decide" in MAIN_SOURCE


def test_the_play_music_handler_follows_the_same_order_the_planner_advises():
    """The two must not drift: native player → internal player → YouTube."""
    start = MAIN_SOURCE.index('elif name == "play_music":')
    end = MAIN_SOURCE.index('elif name == "brain_mode":', start)
    block = MAIN_SOURCE[start:end]
    native = block.index("_media.play(query)")
    webview = block.index("play_web_music")
    youtube = block.index("youtube_video")

    assert native < webview < youtube
    assert _controllers(decide.plan("play x", facts=_state())) == [
        "media", "webview", "youtube"]
