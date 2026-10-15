"""Behavioural tests for the provider-scoped voice configuration layer."""
import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from voice.providers_config import (  # noqa: E402
    ProviderConfigError, active_provider, default_pair, migrate_to_providers,
    providers_block, provider_voices, save_provider, set_active_provider,
    set_default_pair, validate_provider,
)


LEGACY = {
    "tts_engine": "edgetts",
    "tts_voice": "en-GB-RyanNeural",
    "voice_output_mode": "gemini_piper",
    "voice_name": "Charon",
    "gemini_api_key": "gm-key",
    "fish_audio_api_key": "fish-key",
    "fish_audio_model_id": "s2-pro",
    "fish_audio_voice_id": "fishvoice123",
    "fish_audio_endpoint": "https://api.fish.audio/v1/tts",
    "fish_audio_format": "mp3",
    "fish_audio_latency": "normal",
    "elevenlabs_api_key": "el-key",
    "elevenlabs_voice_id": "pNInz6obpgDQGcFmaJgB",
    "elevenlabs_model_id": "eleven_multilingual_v2",
    "onnx_voice_model": "config/voices/jarvis-high.onnx",
    "onnx_voice_config": "config/voices/jarvis-high.json",
    "onnx_voice_speaker": "default",
    "onnx_execution_provider": "cpu",
    "sapi_voice": "Microsoft Zira",
}


def test_dual_read_resolves_flat_only_configs():
    pb = providers_block(LEGACY)
    fish = pb["providers"]["fishAudio"]
    assert fish["apiKey"] == "fish-key"
    assert fish["voice"] == "fishvoice123"
    assert pb["providers"]["elevenLabs"]["apiKey"] == "el-key"
    # The existing Gemini Charon configuration is preserved.
    assert pb["providers"]["gemini"]["voice"] == "Charon"
    assert pb["providers"]["gemini"]["apiKey"] == "gm-key"
    assert pb["providers"]["piperOnnx"]["modelPath"] == "config/voices/jarvis-high.onnx"
    assert pb["providers"]["edgeTts"]["voice"] == "en-GB-RyanNeural"
    assert pb["providers"]["sapi"]["voice"] == "Microsoft Zira"


def test_nested_values_win_over_flat():
    cfg = dict(LEGACY)
    cfg["voice"] = {"providers": {"fishAudio": {"voice": "nested-voice"}}}
    assert providers_block(cfg)["providers"]["fishAudio"]["voice"] == "nested-voice"
    # Untouched subtree values still fall back to the flat keys.
    assert providers_block(cfg)["providers"]["elevenLabs"]["apiKey"] == "el-key"


def test_providers_block_is_pure():
    cfg = copy.deepcopy(LEGACY)
    before = copy.deepcopy(cfg)
    providers_block(cfg)
    active_provider(cfg)
    assert cfg == before


def test_save_provider_touches_only_one_subtree():
    cfg = dict(LEGACY)
    out = save_provider(cfg, "fishAudio", {"apiKey": "new-key", "voice": "new-voice"})
    assert out["voice"]["providers"]["fishAudio"]["apiKey"] == "new-key"
    assert out["voice"]["providers"]["fishAudio"]["voice"] == "new-voice"
    # Every other provider is byte-identical.
    before = providers_block(cfg)["providers"]
    after = providers_block(out)["providers"]
    for pid in ("elevenLabs", "gemini", "piperOnnx", "edgeTts", "kokoro", "sapi"):
        assert after[pid] == before[pid]
    # Flat keys are never mutated.
    assert out["fish_audio_api_key"] == "fish-key"
    # The input dict is not mutated either.
    assert cfg.get("voice") is None


def test_save_provider_rejects_unknown_keys_and_ids():
    with pytest.raises(ProviderConfigError):
        save_provider({}, "nope", {})
    with pytest.raises(ProviderConfigError):
        save_provider({}, "fishAudio", {"geminiRenderer": "native"})


def test_default_pair_rejects_mismatched_voice():
    cfg = dict(LEGACY)
    with pytest.raises(ProviderConfigError):
        set_default_pair(cfg, "fishAudio", "Charon")


def test_default_pair_accepts_valid_pair_and_is_atomic():
    cfg = dict(LEGACY)
    out = set_default_pair(cfg, "fishAudio", "fishvoice123")
    assert default_pair(out) == ("fishAudio", "fishvoice123")
    # Nothing else changed.
    out.pop("voice")
    assert out == cfg


def test_default_pair_unset_returns_none():
    assert default_pair(LEGACY) is None
    block = providers_block(LEGACY)
    assert not (block["defaultProvider"] and block["defaultVoice"])


def test_provider_voices_for_gemini_include_live_voices():
    voices = provider_voices(LEGACY, "gemini")
    assert "Charon" in voices
    assert "Puck" in voices


def test_active_provider_derivation():
    assert active_provider(LEGACY) == "gemini"            # gemini_piper mode
    assert active_provider({**LEGACY, "voice_output_mode": "local"}) == "edgeTts"
    assert active_provider({**LEGACY, "voice_output_mode": "local", "tts_engine": "fish_audio"}) == "fishAudio"
    assert active_provider({**LEGACY, "voice_output_mode": "local", "tts_engine": "onnx"}) == "piperOnnx"
    assert active_provider({**LEGACY, "voice_output_mode": "gemini"}) == "gemini"
    assert active_provider({}) == "edgeTts"               # safe default


def test_set_active_provider_is_provider_only_write():
    out = set_active_provider(LEGACY, "elevenLabs")
    assert out["voice"]["activeProvider"] == "elevenLabs"
    assert set(out["voice"].keys()) == {"activeProvider"}
    with pytest.raises(ProviderConfigError):
        set_active_provider({}, "bogus")


def test_migration_preserves_all_providers_including_charon():
    out, migrated = migrate_to_providers(LEGACY)
    assert migrated
    pb = out["voice"]
    assert pb["providers"]["fishAudio"]["apiKey"] == "fish-key"
    assert pb["providers"]["fishAudio"]["voice"] == "fishvoice123"
    assert pb["providers"]["elevenLabs"]["voice"] == "pNInz6obpgDQGcFmaJgB"
    assert pb["providers"]["gemini"]["voice"] == "Charon"     # Charon preserved
    assert pb["providers"]["piperOnnx"]["modelPath"] == "config/voices/jarvis-high.onnx"
    assert pb["providers"]["piperOnnx"]["executionProvider"] == "cpu"
    assert pb["providers"]["edgeTts"]["voice"] == "en-GB-RyanNeural"
    assert pb["providers"]["sapi"]["voice"] == "Microsoft Zira"
    # The active pipeline: gemini_piper has no lift marker here, so the
    # migration lands on the native-audio renderer (the new default).
    assert pb["activeProvider"] == "gemini"
    assert pb["geminiRenderer"] == "native"
    # No default pair is invented from the shared legacy voice field.
    assert default_pair(out) is None
    # Flat keys remain (dual-read).
    assert out["tts_engine"] == "edgetts"
    assert out["voice_name"] == "Charon"


def test_migration_runs_once():
    out, first = migrate_to_providers(LEGACY)
    edited = save_provider(out, "fishAudio", {"voice": "edited-voice"})
    again, second = migrate_to_providers(edited)
    assert first and not second
    assert again["voice"]["providers"]["fishAudio"]["voice"] == "edited-voice"


def test_migration_maps_local_engine_mode():
    cfg = {**LEGACY, "voice_output_mode": "local", "tts_engine": "fish_audio"}
    out, migrated = migrate_to_providers(cfg)
    assert migrated
    assert out["voice"]["activeProvider"] == "fishAudio"


def test_migration_maps_native_gemini_after_pipeline_marker():
    cfg = {**LEGACY, "voice_output_mode": "gemini", "voice_pipeline_migrated": True}
    out, _ = migrate_to_providers(cfg)
    assert out["voice"]["activeProvider"] == "gemini"
    assert out["voice"]["geminiRenderer"] == "native"


def test_migration_maps_legacy_hybrid_to_piper_renderer():
    cfg = {**LEGACY, "voice_output_mode": "hybrid"}
    out, _ = migrate_to_providers(cfg)
    assert out["voice"]["activeProvider"] == "gemini"
    # No deliberate-choice marker → the native-audio default wins.
    assert out["voice"]["geminiRenderer"] == "native"


def test_migration_keeps_piper_renderer_after_deliberate_choice():
    cfg = {**LEGACY, "voice_output_mode": "gemini_piper",
           "voice_pipeline_migrated": True}
    out, _ = migrate_to_providers(cfg)
    assert out["voice"]["geminiRenderer"] == "piper"


def test_validation_messages_name_the_provider():
    assert "Fish Audio" in validate_provider("fishAudio", {"apiKey": "", "voice": "v"})
    assert "Fish Audio" in validate_provider("fishAudio", {"apiKey": "k", "voice": "", "endpoint": "x"})
    assert "Fish Audio" in validate_provider(
        "fishAudio", {"apiKey": "k", "voice": "v",
                      "endpoint": "https://api.fish.audio/v1/tts",
                      "model": "0123456789abcdef0123"})
    assert "ElevenLabs" in validate_provider("elevenLabs", {"apiKey": ""})
    assert "ElevenLabs" in validate_provider(
        "elevenLabs", {"apiKey": "k", "voice": "en-US-GuyNeural"})
    assert "Piper" in validate_provider("piperOnnx", {"modelPath": ""})
    assert "Edge TTS" in validate_provider("edgeTts", {"voice": ""})
    assert validate_provider("edgeTts", {"voice": "en-GB-RyanNeural"}) is None
    assert validate_provider("fishAudio", {
        "apiKey": "k", "voice": "v", "endpoint": "https://api.fish.audio/v1/tts",
        "model": "s2-pro"}) is None


def test_default_pipeline_round_trip():
    from voice.providers_config import default_pipeline, set_default_pipeline
    cfg = set_default_pipeline({}, "gemini_piper")
    assert default_pipeline(cfg) == "gemini_piper"
    assert cfg["voice"]["defaultPipeline"] == "gemini_piper"
    # An unset defaultPipeline normalizes to "" (no recorded default).
    assert default_pipeline(LEGACY) == ""
    # Invalid pipelines are refused.
    import pytest
    from voice.providers_config import ProviderConfigError
    with pytest.raises(ProviderConfigError):
        set_default_pipeline({}, "telepathy")


def test_providers_block_carries_default_pipeline():
    from voice.providers_config import set_default_pipeline
    cfg = set_default_pipeline(LEGACY, "local")
    pb = providers_block(cfg)
    assert pb["defaultPipeline"] == "local"
    # save_provider preserves the recorded default pipeline.
    out = save_provider(cfg, "edgeTts", {"voice": "en-GB-RyanNeural"})
    assert out["voice"]["defaultPipeline"] == "local"
