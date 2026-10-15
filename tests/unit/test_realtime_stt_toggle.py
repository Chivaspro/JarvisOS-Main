"""Realtime STT (interim/live transcript) — the switch and what it governs.

Spec §10-§14: a REAL ON/OFF control for the realtime transcript, wired through
the project's own feature-flag store, that governs ONLY the live display:

  ON  → every interim result is rendered into the ONE existing command textbox
        (through the same intelligent correction the final transcript gets).
  OFF → no interim rendering and no per-chunk correction work, while the
        microphone, wake word, VAD, the FINAL transcript and the command
        pipeline keep working exactly as before.

Offscreen Qt and a live Gemini session are not needed to pin this: the flag's
precedence is exercised against the real config module, and the runtime wiring
is pinned in the file that owns it (same convention as
test_realtime_stt_correction / test_greeting_and_activity_wiring).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
UI_SETTINGS = (ROOT / "ui_settings.py").read_text(encoding="utf-8")


# ── the flag itself (project's existing configuration architecture) ──────────

def test_realtime_stt_is_a_known_feature_flag():
    assert "realtime_stt" in settings.FEATURE_DEFAULTS
    assert settings.FEATURE_DEFAULTS["realtime_stt"] is True


def test_env_override_follows_the_project_convention(monkeypatch):
    monkeypatch.setenv("JARVIS_FEATURE_REALTIME_STT", "off")
    assert settings.get_feature("realtime_stt") is False
    monkeypatch.setenv("JARVIS_FEATURE_REALTIME_STT", "on")
    assert settings.get_feature("realtime_stt") is True


# ── the setting is reachable from the existing Settings system ───────────────

def test_settings_exposes_a_realtime_stt_control():
    assert '("realtime_stt", "Realtime STT' in UI_SETTINGS
    # It lives in the same VOICE AUTOMATION group as the other live flags, so
    # SAVE writes it through the existing features map (no parallel store).
    start = UI_SETTINGS.index("def _build_automation_tab")
    body = UI_SETTINGS[start:start + 4200]
    assert 'self._voice_checks' in body
    assert "saved=voice_features" in body, (
        "the checkbox must open showing the STORED flag, not a built-in default")


# ── runtime wiring ────────────────────────────────────────────────────────────

def test_interim_render_is_gated_by_the_switch():
    """OFF means no live display work at all — including the per-chunk
    correction, which is the CPU cost the display was causing."""
    start = MAIN.index("if sc.input_transcription")
    end = MAIN.index("# Server-side barge-in")
    block = MAIN[start:end]
    assert "if self._realtime_stt_on():" in block
    # The buffer that the final transcript is built from is still filled —
    # switching the display off must not switch speech recognition off.
    assert "in_buf.append(txt)" in block
    assert block.index("in_buf.append(txt)") < block.index("if self._realtime_stt_on():")


def test_final_transcript_is_not_gated():
    """The FINAL result is normal STT, not realtime STT: it must keep rendering
    into the existing textbox whatever the switch says."""
    start = MAIN.index('full_in = " ".join(in_buf).strip()')
    end = MAIN.index("in_buf = []", start)
    block = MAIN[start:end]
    assert "self.ui.set_live_transcript(full_in, final=True)" in block
    assert "_realtime_stt_on" not in block, "final STT must not depend on the flag"


def test_the_switch_is_read_live_not_once_at_boot():
    """A Settings save changes running behaviour without a restart; the read is
    cached (≤1 s) because the interim path runs many times per utterance."""
    start = MAIN.index("def _realtime_stt_on")
    body = MAIN[start:start + 1900]
    assert "_read_cached_json(API_CONFIG_PATH)" in body
    assert 'features' in body and '"realtime_stt"' in body
    assert "_rt_stt_checked" in body and "1.0" in body
    # It is a live view, not a microphone switch.
    assert "deliberately not a microphone switch" in body


def test_one_command_per_utterance_is_still_true():
    """Interim text is display only: the final transcript is never re-submitted
    (the Gemini audio turn already drove execution)."""
    start = MAIN.index('full_in = " ".join(in_buf).strip()')
    end = MAIN.index("in_buf = []", start)
    block = MAIN[start:end]
    assert "send_client_content" not in block
    assert "_send_backend_command" not in block


def test_mic_watchdog_replacement_actually_starts():
    """The stall watchdog used to rebind the stream INSIDE `with _mic_stream:`,
    which never starts the replacement — so the microphone stayed deaf while
    the message repeated forever. The stream is now started and stopped by
    hand, and a replacement that will not open is reported and backed off."""
    start = MAIN.index("async def _listen_audio")
    end = MAIN.index("async def _receive_audio", start)
    block = MAIN[start:end]
    assert not any(line.strip().startswith("with _mic_stream:")
                   for line in block.splitlines()), \
        "the stream must not be bound to a with-block the watchdog cannot replace"
    assert "_mic_stream.start()" in block
    assert "candidate.start()" in block, "a reopened stream must actually start"
    assert "stale.stop()" in block and "stale.close()" in block
    assert "_failed_reopens >= 3" in block, "a failing reopen must back off, not storm"

