"""The greeting trigger guard: "hi" must never fire a side-effect tool.

The Live model occasionally answers a bare greeting with a tool call — opening
the HUD (``jarvis_window``) or touching the auto-heal policy
(``self_repair``, the ``auto_low_risk`` mode) — which reads as the assistant
acting on a greeting. ``main.py`` now refuses exactly those tools when the
turn's utterance is a pure greeting, and only those tools: anything with real
content keeps working.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as jarvis_main  # noqa: E402

MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")


# ── the pure predicate ────────────────────────────────────────────────────────

def test_greetings_are_recognised():
    for text in ("hi", "Hi", "Hi!", "hi.", "hi jarvis", "hey jarvis",
                 "hello", "hello there", "hey there", "yo", "howdy",
                 "good morning", "Good evening, sir.", "good night",
                 "greetings", "what's up", "Hi, Jarvis!"):
        assert jarvis_main._is_pure_greeting(text), text


def test_real_requests_are_not_greetings():
    for text in ("", "   ", "hi, open the hud", "hi how are you",
                 "hey jarvis open the webview", "hide the hud",
                 "this is broken", "hi risk settings", "auto heal mode",
                 "set auto low risk", "open settings", "hello, what can you do?",
                 "good morning, give me the news"):
        assert not jarvis_main._is_pure_greeting(text), text


# ── the refusal decision ──────────────────────────────────────────────────────

def test_side_effect_tools_are_refused_on_a_greeting():
    for tool in ("jarvis_window", "self_repair", "brain_mode",
                 "shutdown_jarvis", "pc_control", "computer_control",
                 "visual_control", "browser_action", "web_task",
                 "identity_profile", "voice_settings", "save_memory", "mcp"):
        refusal = jarvis_main._greeting_tool_refusal(tool, "hi")
        assert refusal, tool
        assert "no action" in refusal.lower()


def test_real_requests_still_reach_the_side_effect_tools():
    assert jarvis_main._greeting_tool_refusal("jarvis_window", "open the hud") is None
    assert jarvis_main._greeting_tool_refusal("self_repair",
                                              "set auto heal to propose") is None
    assert jarvis_main._greeting_tool_refusal("pc_control",
                                              "click the start button") is None


def test_unrelated_tools_are_never_refused():
    assert jarvis_main._greeting_tool_refusal("system_status", "hi") is None
    assert jarvis_main._greeting_tool_refusal("web_search", "hi") is None


def test_guard_never_raises():
    assert jarvis_main._greeting_tool_refusal("jarvis_window", None) is None
    assert jarvis_main._is_pure_greeting(None) is False


# ── wiring: the dispatcher consults the guard ─────────────────────────────────

def test_execute_tool_runs_the_guard_before_acting():
    start = MAIN_SRC.index("async    def _execute_tool")
    end = MAIN_SRC.index("async def _brain_tool", start)
    body = MAIN_SRC[start:end]
    assert "_greeting_tool_refusal(name, _utterance)" in body
    assert "return types.FunctionResponse" in body


def test_the_turn_utterance_is_tracked_in_both_directions():
    # Voice: live preview while transcribing, final text once settled.
    assert "self._live_user_utterance = live_input_preview" in MAIN_SRC
    assert "self._last_user_utterance = full_in" in MAIN_SRC
    # Typed: the textbox command is the turn's utterance.
    assert "self._last_user_utterance = str(text or \"\").strip()" in MAIN_SRC
