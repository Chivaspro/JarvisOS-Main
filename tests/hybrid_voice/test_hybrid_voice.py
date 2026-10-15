"""Focused tests for the Hybrid Voice System coordinator.

These tests do NOT touch the microphone or the network.  They exercise the
pure-Python pieces: state machine, validation, atomic save, singleton,
ONNX cache lifecycle, and playback serialization.
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core import hybrid_voice as hv
from core.hybrid_voice import (
    HybridVoiceSystem,
    MicBus,
    OnnxCachedEngine,
    PlaybackManager,
    VoiceMode,
    VoiceState,
    _validate_onnx_model,
    _validate_gemini_config,
    _validate_voice_settings,
    save_voice_config,
)


# ── state machine ─────────────────────────────────────────────────────────────
def test_state_machine_valid_transitions():
    s = HybridVoiceSystem()
    assert s.state() == VoiceState.IDLE
    s.set_state(VoiceState.CONNECTING)
    assert s.state() == VoiceState.CONNECTING
    s.set_state(VoiceState.CONNECTED)
    assert s.state() == VoiceState.CONNECTED
    s.set_state(VoiceState.LISTENING)
    assert s.state() == VoiceState.LISTENING


def test_state_machine_invalid_transition_rejected():
    s = HybridVoiceSystem()
    s.set_state(VoiceState.IDLE)
    # SPEAKING is not reachable from IDLE.
    s.set_state(VoiceState.SPEAKING)
    assert s.state() == VoiceState.IDLE


def test_state_callback_fires():
    s = HybridVoiceSystem()
    seen = []
    s.on_state(lambda st: seen.append(st))
    s.set_state(VoiceState.LISTENING)
    assert seen and seen[-1] == VoiceState.LISTENING


# ── validation ────────────────────────────────────────────────────────────────
def test_validate_onnx_model_missing():
    ok, err = _validate_onnx_model("/nonexistent/model.onnx", "")
    assert ok is False
    assert "not found" in err


def test_validate_onnx_model_empty():
    ok, err = _validate_onnx_model("", "")
    assert ok is False
    assert "empty" in err


def test_validate_gemini_config_missing_key():
    ok, err = _validate_gemini_config("", "models/foo")
    assert ok is False
    assert "API key" in err


def test_validate_gemini_config_missing_model():
    ok, err = _validate_gemini_config("key", "")
    assert ok is False
    assert "model" in err


def test_validate_voice_settings_rejects_bad_mode():
    ok, err = _validate_voice_settings({"voice_output_mode": "telepathy"})
    assert ok is False
    assert "Unknown voice mode" in err


# ── atomic save ───────────────────────────────────────────────────────────────
def test_save_voice_config_writes_and_keeps_legacy_keys(tmp_path):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({"tts_engine": "onnx", "voice_name": "x"}), encoding="utf-8")
    with mock.patch.object(hv, "_API_CONFIG", cfg_path):
        ok, msg = save_voice_config({"voice_output_mode": "hybrid", "tts_engine": "edgetts"})
    assert ok is True
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["voice_output_mode"] == "hybrid"
    assert data["tts_engine"] == "edgetts"
    assert data["voice_name"] == "x"  # legacy key preserved


def test_save_voice_config_atomic_no_tmp_left(tmp_path):
    cfg_path = tmp_path / "api_keys.json"
    cfg_path.write_text(json.dumps({}), encoding="utf-8")
    with mock.patch.object(hv, "_API_CONFIG", cfg_path):
        save_voice_config({"voice_output_mode": "gemini"})
    assert not (tmp_path / "api_keys.json.tmp").exists()


# ── singleton ─────────────────────────────────────────────────────────────────
def test_singleton_only_one_instance():
    HybridVoiceSystem.reset()
    a = HybridVoiceSystem.get(cfg={"voice_output_mode": "hybrid"})
    b = HybridVoiceSystem.get(cfg={"voice_output_mode": "hybrid"})
    assert a is b
    HybridVoiceSystem.reset()
    c = HybridVoiceSystem.get(cfg={"voice_output_mode": "hybrid"})
    assert c is not a
    HybridVoiceSystem.reset()


# ── ONNX cache ────────────────────────────────────────────────────────────────
def test_onnx_cache_singleton():
    OnnxCachedEngine.reset()
    a = OnnxCachedEngine.get(model_path="/x", config_path="/y")
    b = OnnxCachedEngine.get(model_path="/x", config_path="/y")
    assert a is b
    OnnxCachedEngine.reset()


def test_onnx_cache_warmup_idempotent():
    OnnxCachedEngine.reset()
    eng = OnnxCachedEngine.get(model_path="/missing.onnx")
    # Should not raise; returns False because model is missing.
    assert eng.warmup_once() is False
    # Calling again must not reload or raise.
    assert eng.warmup_once() is False
    OnnxCachedEngine.reset()


# ── playback manager ──────────────────────────────────────────────────────────
def test_playback_serialises_concurrent_play():
    pm = PlaybackManager()
    calls = []
    with mock.patch.object(hv.sd, "play") as mplay, \
         mock.patch.object(hv.sd, "wait") as mwait, \
         mock.patch.object(hv.sd, "stop") as mstop:
        mplay.side_effect = lambda *a, **k: calls.append("play")
        mwait.return_value = None
        mstop.return_value = None
        # Two concurrent play calls must serialise: second stops the first.
        t1 = threading.Thread(target=pm.play, args=(b"\x00\x01", 24000))
        t2 = threading.Thread(target=pm.play, args=(b"\x02\x03", 24000))
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert calls == ["play", "play"]


# ── mic bus registration ──────────────────────────────────────────────────────
def test_mic_bus_register_unregister():
    bus = MicBus()
    calls = []
    bus.register(lambda lvl, data: calls.append(lvl))
    assert len(bus._callbacks) == 1
    bus.unregister(bus._callbacks[0])
    assert len(bus._callbacks) == 0


def test_mic_bus_level_property():
    bus = MicBus()
    bus._level = 0.42
    assert bus.level == 0.42
