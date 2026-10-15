"""Gemini 2.5 Flash NATIVE AUDIO — the setting must match the runtime.

The voice session used to connect with whatever `ai_model` said, and
`Settings ▸ AI` offered models that the Live API refuses outright (verified
against the real API: "models/gemini-2.0-flash-001 … is not supported for
bidiGenerateContent"). Selecting one of those did not change the voice — it
killed it, with the failure surfacing as a reconnect loop.

These tests pin the resolution rule and the honesty requirement (§16: a
substitution is reported, never swallowed). No network is involved: the
model-name rules are what is under test, and the live round-trip itself was
verified against the real API during implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from app import ai_providers as aip  # noqa: E402

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
DIAGNOSTICS = (ROOT / "observability" / "diagnostics.py").read_text(encoding="utf-8")

NATIVE_AUDIO = "models/gemini-2.5-flash-native-audio-preview-12-2025"


def test_the_configured_native_audio_model_is_still_the_live_default():
    assert aip.GEMINI_DEFAULT_MODEL == NATIVE_AUDIO
    assert f'LIVE_MODEL          = "{NATIVE_AUDIO}"' in MAIN


def test_every_offered_gemini_model_can_actually_serve_voice():
    for model in aip.models_for("gemini"):
        assert aip.is_live_model(model), f"{model} cannot drive the Live session"


def test_live_model_is_passed_through_when_it_really_is_live():
    model, note = aip.resolve_live_model({"ai_provider": "gemini",
                                          "ai_model": NATIVE_AUDIO})
    assert model == NATIVE_AUDIO
    assert note == ""


def test_a_non_live_choice_is_replaced_and_reported_not_swallowed():
    """§16: no silent fallback. The voice survives AND says what happened."""
    model, note = aip.resolve_live_model({"ai_provider": "gemini",
                                          "ai_model": "models/gemini-2.0-flash-001"})
    assert model == NATIVE_AUDIO
    assert note and "gemini-2.0-flash-001" in note and "not Live-capable" in note


def test_missing_model_config_still_lands_on_a_working_voice_model():
    model, note = aip.resolve_live_model({})
    assert model == NATIVE_AUDIO and note == ""
    # A non-Gemini text provider must not leave the voice without a model.
    model, note = aip.resolve_live_model({"ai_provider": "openai",
                                          "ai_model": "gpt-4o"})
    assert model == NATIVE_AUDIO and note == ""


def test_explicit_live_model_wins_and_bogus_explicit_names_are_reported():
    model, note = aip.resolve_live_model({
        "live_model": "models/gemini-2.5-flash-native-audio-latest",
        "ai_model": NATIVE_AUDIO})
    assert model == "models/gemini-2.5-flash-native-audio-latest" and note == ""
    model, note = aip.resolve_live_model({"live_model": "models/gemini-2.5-pro"})
    assert model == NATIVE_AUDIO and "not a real-time voice model" in note


# ── runtime wiring (pinned in the file that owns it) ─────────────────────────

def test_the_live_session_uses_the_resolved_model_and_reports_substitutions():
    assert "resolve_live_model(_acfg)" in MAIN
    start = MAIN.index("_live_model, _live_note = _aip.resolve_live_model")
    body = MAIN[start:start + 900]
    assert "if _live_note:" in body
    assert "write_log" in body, "a substituted voice model must be visible to the user"
    assert "client.aio.live.connect(model=_live_model" in MAIN


def test_diagnostics_reports_the_real_live_model():
    assert "live_voice_model=" in DIAGNOSTICS
    assert "resolve_live_model(cfg)" in DIAGNOSTICS


# ── the AI tab's TEST CONNECTION is a real probe ──────────────────────────────
# It used to return "Gemini voice model ready: <model>" from the key simply
# being non-empty, so a dead key was certified and the voice then failed. The
# one Gemini probe lives in services.hub (it knows the API's 10s deadline
# minimum); these tests pin the wiring to it.

_GM = "AQ." + "x" * 40


def test_gemini_test_connection_asks_the_hub_for_a_real_probe(monkeypatch):
    from services import hub
    calls: list = []

    def _fake_probe(service, config=None, timeout=8.0):
        calls.append((service, dict(config or {}), timeout))
        return {"state": "ready", "ok": True, "model": "gemini-flash-latest",
                "detail": "The Gemini key answered."}

    monkeypatch.setattr(hub, "probe", _fake_probe)
    reply = aip.test_connection({"ai_provider": "gemini", "gemini_api_key": _GM})
    assert calls and calls[0][0] == "gemini"
    assert calls[0][1]["gemini_api_key"] == _GM
    assert "gemini-flash-latest" in reply
    # The model that will actually drive the voice session is named.
    assert aip.GEMINI_DEFAULT_MODEL in reply


def test_a_failed_gemini_probe_raises_instead_of_being_certified(monkeypatch):
    from services import hub
    monkeypatch.setattr(hub, "probe", lambda *a, **k: {
        "state": "unavailable", "ok": False,
        "detail": "the key was rejected by Google"})
    with pytest.raises(RuntimeError) as exc:
        aip.test_connection({"ai_provider": "gemini", "gemini_api_key": _GM})
    assert "rejected" in str(exc.value)


def test_gemini_test_connection_still_refuses_without_a_key():
    with pytest.raises(ValueError):
        aip.test_connection({"ai_provider": "gemini", "gemini_api_key": ""})
