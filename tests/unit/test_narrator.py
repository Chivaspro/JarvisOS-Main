"""Narration, and the latency work it shares a file with.

Two things are pinned here, both of which the user can hear:

* **Narration** must only ever describe something that really happened. These
  tests feed it real engine progress strings (the exact shapes Agent TARS
  emits) and check the sentences, then check the gate: no repeats in a row, no
  chatter faster than the gap, no more than the per-task budget, nothing at all
  when it is switched off or muted.
* **Hybrid-VAD bookkeeping** on the microphone gate, because that is what turns
  "the user stopped talking" into a signal. A gate that reported it during
  silence would cut replies off at random, so the transition is tested both
  ways: real speech sets it, noise alone never does.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice.narrator import (  # noqa: E402
    Narrator,
    clean_target,
    describe_end,
    describe_interrupt,
    describe_mode,
    describe_progress,
    describe_task,
)


def _load_main():
    spec = importlib.util.spec_from_file_location("main_impl_narrator_tests",
                                                 ROOT / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── phrasing: derived from the real event text ─────────────────────────────

def test_click_includes_what_is_being_clicked():
    """The user's own example: \"sir, I'm clicking the stop button\"."""
    line = describe_progress("Clicking the Stop button")
    assert line is not None
    assert "clicking" in line.lower()
    assert "Stop button" in line
    assert line.lower().startswith("sir")


def test_opening_a_url_reads_the_host_not_the_url():
    line = describe_progress("Opening https://example.com/path?q=1")
    assert line == "Sir, I'm opening example.com."


def test_reading_a_page_is_narrated_as_reading():
    assert describe_progress("Reading the page") == "Sir, I'm reading the page."


def test_failure_is_never_dressed_up_as_success():
    for text in ("Task failed", "Error: element not found",
                 "could not complete the step"):
        line = describe_progress(text)
        assert line is not None
        assert "done" not in line.lower()
        assert "did not" in line.lower() or "stopped" in line.lower()


def test_completion_is_only_said_for_a_completion_event():
    assert describe_progress("Task completed") == "Sir, that is done."
    assert "done" not in (describe_progress("Typing into the search box") or "")


def test_unknown_text_is_not_invented_into_a_sentence():
    assert describe_progress("   ") is None
    assert describe_progress("") is None


def test_target_cleanup_drops_noise_but_keeps_words():
    assert clean_target("https://a.b/c/d") == "a.b"
    assert clean_target('"the Save button"') == "the Save button"
    assert clean_target("C:\\Windows\\System32\\cmd.exe") == ""
    assert clean_target("7f3a9c21d4b8e6f0") == ""
    assert clean_target("element 42") == ""
    assert clean_target("the button") == ""      # no meaning out loud


def test_task_and_end_lines_come_from_the_tool_and_status():
    assert "computer" in (describe_task("computer_control") or "").lower()
    assert describe_task("not_a_tool") is None
    assert "done" in (describe_end("computer_control", "SUCCESS") or "")
    assert "did not work" in (describe_end("browser_control", "FAILED") or "")
    assert describe_end("computer_control", "") is None


def test_interrupt_and_mode_lines_are_explicit():
    assert "halting" in describe_interrupt().lower()
    assert "UI TARS" in describe_mode(True)
    assert "Gemini" in describe_mode(False)


# ── the gate ────────────────────────────────────────────────────────────────

def test_speaks_through_the_speaker_and_returns_the_line():
    spoken: list = []
    n = Narrator(speak=lambda text, forced=False: spoken.append(text),
                 min_gap_s=0.0)
    assert n.task("computer_control") is not None
    assert spoken and spoken[-1].lower().startswith("sir")


def test_never_repeats_the_same_line_twice_in_a_row():
    spoken: list = []
    n = Narrator(speak=lambda text, forced=False: spoken.append(text),
                 min_gap_s=0.0)
    n.progress("computer_control", "Reading the page")
    n.progress("computer_control", "Reading the page")
    assert spoken.count("Sir, I'm reading the page.") == 1


def test_gap_throttles_a_burst_of_events():
    spoken: list = []
    n = Narrator(speak=lambda text, forced=False: spoken.append(text),
                 min_gap_s=30.0)
    n.progress("browser_control", "Reading the page")
    n.progress("browser_control", "Scrolling down")
    assert len(spoken) == 1, "a burst must not become a monologue"


def test_task_budget_caps_a_long_run():
    spoken: list = []
    n = Narrator(speak=lambda text, forced=False: spoken.append(text),
                 min_gap_s=0.0, max_lines_per_task=2)
    for text in ("Reading the page", "Scrolling down", "Clicking Send",
                 "Typing that in"):
        n.progress("browser_control", text)
    assert len(spoken) == 2


def test_disabled_narrator_is_silent():
    spoken: list = []
    n = Narrator(speak=lambda text, forced=False: spoken.append(text),
                 enabled=False, min_gap_s=0.0)
    assert n.task("computer_control") is None
    assert n.interrupt() is None
    assert spoken == []


def test_suppression_holds_automatic_lines_but_not_user_ones():
    """Automatic narration waits for silence; a stop acknowledgement does not."""
    spoken: list = []
    busy = {"value": True}
    n = Narrator(speak=lambda text, forced=False: spoken.append((text, forced)),
                 min_gap_s=0.0)
    n.set_suppress(lambda: busy["value"])
    assert n.progress("browser_control", "Reading the page") is None
    assert spoken == []
    # The user pressed stop: that line must still be spoken even though J.A.R.V.I.S.
    # was talking at that instant.
    assert n.interrupt() is not None
    assert spoken and spoken[-1][1] is True


def test_forced_lines_bypass_the_gap_but_not_the_duplicate_rule():
    spoken: list = []
    n = Narrator(speak=lambda text, forced=False: spoken.append(text),
                 min_gap_s=30.0)
    assert n.mode(True) is not None
    assert n.mode(True) is None, "the same mode line twice adds nothing"
    assert len(spoken) == 1


def test_speaker_failure_never_escapes():
    def boom(text, forced=False):
        raise RuntimeError("no audio device")

    n = Narrator(speak=boom, min_gap_s=0.0)
    assert n.task("computer_control") is None      # swallowed, not raised


def test_text_only_speaker_still_works():
    """Callers that pass a one-argument speak() keep working."""
    spoken: list = []
    n = Narrator(speak=lambda text: spoken.append(text), min_gap_s=0.0)
    assert n.task("computer_control") is not None
    assert len(spoken) == 1


# ── microphone gate: the end-of-speech signal ───────────────────────────────

def _block(rms: float, n: int = 1024, seed: int = 0):
    rs = np.random.RandomState(seed)
    x = rs.randn(n).astype(np.float32)
    x = x / float(np.sqrt(np.mean(x * x))) * float(rms)
    xi = np.clip(x, -32767, 32767).astype(np.int16)
    return xi, xi.tobytes()


def test_quiet_blocks_never_report_end_of_speech():
    main = _load_main()
    gate = main._MicVoiceGate(16000, 1024, hangover_ms=500)
    for i in range(40):
        gate.accept(*_block(40, seed=i))
        assert not gate.just_closed, "silence must not finalise a turn"


def test_real_utterance_reports_end_of_speech_once():
    main = _load_main()
    gate = main._MicVoiceGate(16000, 1024, hangover_ms=500)
    closes = 0
    for i in range(4):
        gate.accept(*_block(2200, seed=i))
        assert not gate.just_closed, "end-of-speech cannot fire while speaking"
    # ~500 ms of hangover at 1024 samples / 16 kHz ≈ 8 blocks
    for i in range(20):
        gate.accept(*_block(40, seed=100 + i))
        closes += 1 if gate.just_closed else 0
    assert closes == 1, "exactly one finalisation per utterance"


def test_reset_clears_a_pending_end_of_speech():
    main = _load_main()
    gate = main._MicVoiceGate(16000, 1024, hangover_ms=500)
    for i in range(4):
        gate.accept(*_block(2200, seed=i))
    gate.reset()
    assert gate.just_closed is False and gate.speech_seen is False
    # After a mute/barge-in the gate is closed again: noise cannot finalise.
    for i in range(20):
        gate.accept(*_block(40, seed=200 + i))
        assert not gate.just_closed


def test_raw_stream_end_flag_is_never_claimed_without_speech():
    main = _load_main()
    gate = main._MicVoiceGate(16000, 1024, hangover_ms=500)
    out = gate.accept(*_block(600, seed=1))
    assert len(out) >= 1 and gate.speech_seen is True


# ── the Live session's VAD config ───────────────────────────────────────────

def test_realtime_input_config_uses_documented_hybrid_vad_values():
    main = _load_main()
    from google.genai import types
    main.types = types          # what the deferred loader installs at runtime

    cfg = main.JarvisLive._realtime_input_config(object())
    assert cfg is not None
    auto = cfg.automatic_activity_detection
    # End-of-speech sensitivity stays LOW: the client finalises the turn, so the
    # server must not also cut it short mid-sentence.
    assert auto.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW
    assert auto.start_of_speech_sensitivity == (
        types.StartSensitivity.START_SENSITIVITY_LOW)
    assert auto.prefix_padding_ms == main.PREFIX_PADDING_MS == 20
    # Inside the docs' recommended 500-800 ms band (not the 100 ms that
    # fragments speech, not the ~800 ms default that feels slow).
    assert main.SILENCE_FALLBACK_MS == auto.silence_duration_ms == 500
    assert main.CLIENT_HANGOVER_MS >= 500, "docs' minimum client threshold"
    assert cfg.activity_handling == (
        types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS)


def _bare_live():
    """A JarvisLive with no __init__: real methods, no Qt, no audio.

    This is how the narration wiring is exercised for real — the methods under
    test are the ones that run in the app, resolved through the class, not
    stand-ins that could drift from it.
    """
    import types as _types
    main = _load_main()
    stub = main.JarvisLive.__new__(main.JarvisLive)
    stub._narrator = None
    stub._piper = None
    stub.ui = _types.SimpleNamespace()
    return main, stub


def test_progress_event_updates_the_activity_row_and_speaks():
    """The real wiring: one TARS progress event → activity row + one sentence."""
    main, stub = _bare_live()
    rows: list = []
    spoken: list = []
    stub.ui.set_doing = lambda cat, detail: rows.append((cat, detail))
    stub._narrate_line = lambda text, forced=False: spoken.append(text)

    stub._narrate_progress("computer_control", "Clicking the Stop button")
    assert rows == [("AUTOMATE", "Clicking the Stop button")]
    assert spoken == ["Sir, I'm clicking the Stop button."]


def test_progress_event_never_breaks_the_activity_row_when_audio_fails():
    main, stub = _bare_live()
    rows: list = []
    stub.ui.set_doing = lambda cat, detail: rows.append((cat, detail))

    def boom(text, forced=False):
        raise RuntimeError("no output device")

    stub._narrate_line = boom
    stub._narrate_progress("browser_control", "Opening a tab")
    assert rows == [("AUTOMATE", "Opening a tab")]


def test_narration_is_silent_while_already_speaking_or_muted():
    """The suppression predicate reads the real state, not a flag copy."""
    import threading
    main, stub = _bare_live()
    stub.ui.set_doing = lambda cat, detail: None
    stub.ui.muted = False
    stub._is_speaking = True
    stub._speaking_lock = threading.Lock()
    spoken: list = []
    stub._narrate_line = lambda text, forced=False: spoken.append(text)
    assert stub._narration_blocked() is True
    stub._narrate_progress("computer_control", "Clicking Send")
    assert spoken == []

    stub._is_speaking = False
    stub.ui.muted = True
    assert stub._narration_blocked() is True


def test_realtime_input_config_respects_the_off_switch(monkeypatch):
    main = _load_main()
    from google.genai import types
    main.types = types

    monkeypatch.setattr(main, "_read_cached_json",
                        lambda path: {"features": {"realtime_low_latency": False}})
    assert main.JarvisLive._realtime_input_config(object()) is None


def test_realtime_input_config_stops_being_sent_after_a_server_rejection():
    """A rejected config must not be re-sent on every reconnect forever."""
    import types as _types
    main = _load_main()
    from google.genai import types
    main.types = types

    stub = _types.SimpleNamespace(_vad_tuning_ok=True)
    assert main.JarvisLive._realtime_input_config(stub) is not None
    stub._vad_tuning_ok = False
    assert main.JarvisLive._realtime_input_config(stub) is None
