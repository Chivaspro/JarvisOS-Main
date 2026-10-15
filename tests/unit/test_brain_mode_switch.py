"""Gemini must be able to switch UI-TARS mode — and the mode must mean something.

The panel's Gemini ⇄ UI TARS switch only rewrote a text directive, and that
directive never mentioned the UI-TARS tool at all: in "UI TARS mode" the model
was told to use Agent TARS (computer_control/browser_control) and nothing else.
The model also had no way to flip the switch, so it could only *talk* about the
mode. Both halves are fixed and pinned here.

`resolve()` is pure, so the decision is tested directly. The rest — the tool
declaration, the dispatch, the deferred session rebuild and the directive — is
source wiring, which is how this project tests paths that need a live session.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import brain_mode  # noqa: E402

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
import main as jarvis_main  # noqa: E402


def _flat(source: str) -> str:
    """Source text with quotes and line breaks collapsed.

    Prose in this project is wrapped across several string literals, so a
    sentence is often split by a quote and a newline in the source even though
    it reads as one sentence. Phrase assertions use this view.
    """
    return " ".join(source.replace('"', " ").replace("'", " ").split())


# ── resolve(): entering UI TARS ───────────────────────────────────────────

def test_phrases_that_mean_ui_tars_mode():
    for phrase in ("ui tars", "UI TARS mode", "switch to ui_tars", "uitars",
                   "turn on pc control", "pc control mode", "computer control",
                   "take control of my pc", "act on my pc", "control my computer"):
        target, note = brain_mode.resolve(phrase, current=False)
        assert target is True, phrase
        assert note


def test_asking_for_ui_tars_while_already_there_changes_nothing():
    target, note = brain_mode.resolve("switch to ui tars", current=True)

    assert target is None
    assert "already" in note


# ── resolve(): going back to Gemini ───────────────────────────────────────

def test_phrases_that_mean_gemini_mode():
    for phrase in ("gemini", "gemini mode", "back to gemini", "go back",
                   "chat mode", "conversation mode", "normal mode"):
        target, _note = brain_mode.resolve(phrase, current=True)
        assert target is False, phrase


def test_leaving_pc_control_is_not_read_as_entering_it():
    """"stop pc control" names UI TARS only to switch it OFF.

    A naive substring match on "pc control" gets this exactly backwards, which
    is why the leave-phrases are checked first.
    """
    for phrase in ("stop pc control", "exit ui tars", "turn off ui tars",
                   "disable pc control", "stop controlling my pc",
                   "leave ui tars mode"):
        target, _note = brain_mode.resolve(phrase, current=True)
        assert target is False, phrase


def test_asking_for_gemini_while_already_there_changes_nothing():
    target, note = brain_mode.resolve("gemini mode", current=False)

    assert target is None
    assert "already" in note


# ── resolve(): toggle, status, and wording it cannot place ────────────────

def test_toggle_flips_the_current_mode():
    assert brain_mode.resolve("toggle", current=False)[0] is True
    assert brain_mode.resolve("switch mode", current=True)[0] is False


def test_an_explicit_target_wins_over_the_word_switch():
    """'switch to gemini' contains the toggle word and must still mean Gemini."""
    assert brain_mode.resolve("switch to gemini", current=True)[0] is False
    assert brain_mode.resolve("switch to ui tars", current=False)[0] is True


def test_status_questions_report_the_live_mode_without_changing_it():
    for phrase in ("status", "which mode are we in?", "what mode is this",
                   "current mode"):
        target, note = brain_mode.resolve(phrase, current=True)
        assert target is None, phrase
        assert "UI TARS" in note


def test_unrecognised_wording_asks_for_no_change_and_names_the_modes():
    target, note = brain_mode.resolve("make me a sandwich", current=False)

    assert target is None
    assert "UI TARS" in note and "GEMINI" in note


def test_an_empty_request_is_a_status_question():
    target, note = brain_mode.resolve("", current=False)

    assert target is None
    assert "GEMINI" in note


def test_resolution_never_raises_on_junk():
    for junk in (None, 0, "!!!", "   ", "ui tars " * 50, "'\"ui tars\"'"):
        target, note = brain_mode.resolve(junk, current=False)
        assert target in (True, False, None)
        assert isinstance(note, str) and note


# ── the tool the model can call ───────────────────────────────────────────

def test_brain_mode_is_declared_with_all_four_modes():
    start = MAIN_SOURCE.index('"name": "brain_mode"')
    block = MAIN_SOURCE[start:start + 1200]
    prose = _flat(block)

    for mode in ("ui_tars", "gemini", "toggle", "status"):
        assert mode in block
    assert "actually active" in prose, (
        "the tool must promise the real resulting mode, not the requested one"
    )
    assert "never claim the mode changed without calling it" in prose


def test_the_tool_applies_the_same_switch_the_panel_does():
    # One source of truth: the tool calls the method the panel switch calls, so
    # config, panel state and the session directive can never drift apart.
    assert "self._apply_brain_mode(_target, _note)" in MAIN_SOURCE
    start = MAIN_SOURCE.index("def _apply_brain_mode")
    body = MAIN_SOURCE[start:start + 1600]
    assert "self.on_ui_tars_mode_change(bool(target), reconnect=False)" in body
    assert "self._pending_mode_reconnect = True" in body


def test_the_session_rebuild_is_deferred_until_the_turn_is_over():
    """Rebuilding mid-tool-call would abandon the response the model awaits."""
    start = MAIN_SOURCE.index("if getattr(self, \"_pending_mode_reconnect\", False):")
    block = MAIN_SOURCE[start:start + 600]

    assert "request_reconnect(" in block
    assert "keep_context=True" in block, "the conversation must survive the switch"
    # ...and it is consumed on turn_complete, not inline.
    turn_complete = MAIN_SOURCE.index("if sc.turn_complete:")
    assert turn_complete < start


def test_the_panel_switch_can_still_reconnect_immediately():
    """The panel path is unchanged: only the tool defers."""
    signature = inspect.signature(jarvis_main.JarvisLive.on_ui_tars_mode_change)

    assert "reconnect" in signature.parameters
    assert signature.parameters["reconnect"].default is True
    assert signature.parameters["reconnect"].kind is inspect.Parameter.KEYWORD_ONLY


# ── the directive that made UI TARS mode meaningless ─────────────────────

def test_ui_tars_mode_now_steers_to_the_ui_tars_tool():
    start = MAIN_SOURCE.index("[UI TARS MODE - PC CONTROL]")
    # The window covers the whole directive; it grew when power_control and
    # control_plan were added to the controller list.
    block = MAIN_SOURCE[start:start + 2400]

    assert "visual_control" in block, (
        "UI TARS mode must name the UI-TARS tool it is supposedly about"
    )
    assert "browser_action" in block and "pc_control" in block
    assert "brain_mode with mode=gemini" in block, "the model must be able to leave"


def test_gemini_mode_tells_the_model_to_call_the_tool_instead_of_agreeing():
    start = MAIN_SOURCE.index("[GEMINI MODE - CONVERSATION]")
    block = MAIN_SOURCE[start:start + 800]

    assert "brain_mode with mode=ui_tars" in block
    assert "never just agree to it" in block
