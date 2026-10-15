"""Runtime selection follows the active provider (spec: voice-provider-isolation)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from voice.hybrid_voice import resolve_active_mode, resolve_voice_mode  # noqa: E402
from voice.providers_config import (  # noqa: E402
    migrate_to_providers, save_provider, set_active_provider,
)

tts = pytest.importorskip("voice.tts")


FISH_CFG = {
    "tts_engine": "edgetts",
    "tts_voice": "en-GB-RyanNeural",
    "voice_output_mode": "local",
    "voice_name": "Charon",
    "voice": {
        "activeProvider": "fishAudio",
        "geminiRenderer": "piper",
        "providers": {
            "fishAudio": {
                "apiKey": "test-key",
                "endpoint": "https://api.fish.audio/v1/tts",
                "model": "s2-pro",
                "voice": "fish-voice-1",
                "format": "mp3",
                "latency": "normal",
            },
            "gemini": {"apiKey": "gm", "model": "", "voice": "Charon"},
        },
    },
}


def test_fish_active_resolves_local_mode():
    assert resolve_active_mode(FISH_CFG) == "local"


def test_gemini_active_with_piper_renderer():
    cfg = {"voice": {"activeProvider": "gemini", "geminiRenderer": "piper"}}
    assert resolve_active_mode(cfg) == "gemini_piper"


def test_gemini_active_with_native_renderer():
    cfg = {"voice": {"activeProvider": "gemini", "geminiRenderer": "native"}}
    assert resolve_active_mode(cfg) == "gemini"


def test_resolve_active_mode_falls_back_to_legacy():
    # No provider block at all: legacy resolution still works.
    cfg = {"voice_output_mode": "gemini_piper", "voice_pipeline_migrated": True}
    assert resolve_active_mode(cfg) == resolve_voice_mode(cfg)[0]


def test_factory_builds_fish_engine_with_provider_values():
    tts.clear_tts_cache()
    player = tts.create_tts_player(dict(FISH_CFG))
    # The player is lazy: force construction to check the class + wiring.
    engine = player._ensure_engine()
    assert isinstance(engine, tts.FishAudioTTSEngine)
    assert engine.api_key == "test-key"
    assert engine.reference_id == "fish-voice-1"
    assert engine.model == "s2-pro"
    assert engine.endpoint == "https://api.fish.audio/v1/tts"


def test_provider_switch_invalidates_cached_engine():
    tts.clear_tts_cache()
    p1 = tts.create_tts_player(dict(FISH_CFG))
    e1 = p1._ensure_engine()
    # Switch to Gemini: the resolved mode is the pipeline, and the factory must
    # not be consulted for the reply at all — simulate by building the edge
    # engine config the provider layer now names.
    switched = set_active_provider(dict(FISH_CFG), "edgeTts")
    p2 = tts.create_tts_player(switched)
    e2 = p2._ensure_engine()
    assert isinstance(e2, tts.EdgeTTSEngine)
    assert not isinstance(e2, type(e1))
    # And the fish voice reached the fish engine, not the edge engine.
    assert e2.voice != "fish-voice-1"
    tts.clear_tts_cache()


def test_migration_then_factory_uses_migrated_fish_values():
    cfg = {
        "tts_engine": "fish_audio",
        "voice_output_mode": "local",
        "fish_audio_api_key": "k",
        "fish_audio_voice_id": "fv",
        "fish_audio_model_id": "s2-pro",
    }
    migrated, _ = migrate_to_providers(cfg)
    assert migrated["voice"]["activeProvider"] == "fishAudio"
    tts.clear_tts_cache()
    engine = tts.create_tts_player(migrated)._ensure_engine()
    assert isinstance(engine, tts.FishAudioTTSEngine)
    assert engine.reference_id == "fv"
    tts.clear_tts_cache()


def test_provider_failure_names_the_provider(monkeypatch):
    """A fish engine failure surfaces its provider text; no other engine is built."""
    tts.clear_tts_cache()
    built = []
    real_fish = tts.FishAudioTTSEngine

    class BoomFish(real_fish):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built.append("fish")

        def speak(self, text):
            raise RuntimeError("Fish Audio request failed (401)")

    monkeypatch.setattr(tts, "FishAudioTTSEngine", BoomFish)
    player = tts.create_tts_player(dict(FISH_CFG))
    player.speak("hello")
    assert built == ["fish"]                       # only the fish engine was constructed
    assert "Fish Audio" in (tts.voice_status().error or "")
    tts.clear_tts_cache()
