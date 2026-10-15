"""OpenRouter is a first-class provider: voice stays on Gemini, text and the
self-evolution engine can ride any configured provider.
"""
from __future__ import annotations

from app import ai_providers
from evolution import forge


def test_openrouter_is_registered_with_a_key_field_and_models():
    meta = ai_providers.PROVIDERS["openrouter"]
    assert meta["key_field"] == "openrouter_api_key"
    assert meta["endpoint"].startswith("https://openrouter.ai/")
    assert meta["models"], "the model picker needs at least one entry"


def test_active_config_prefers_the_stored_base_url_and_key():
    cfg = {
        "ai_provider": "openrouter",
        "ai_model": "anthropic/claude-sonnet-4.5",
        "openrouter_api_key": "sk-or-test",
        "openrouter_base_url": "https://proxy.example/v1/",
    }
    active = ai_providers.active_config(cfg)
    assert active["provider"] == "openrouter"
    assert active["model"] == "anthropic/claude-sonnet-4.5"
    assert active["endpoint"] == "https://proxy.example/v1"   # trailing slash trimmed
    assert active["api_key"] == "sk-or-test"


def test_openrouter_falls_back_to_the_official_endpoint():
    active = ai_providers.active_config({"ai_provider": "openrouter",
                                         "openrouter_api_key": "k"})
    assert active["endpoint"] == "https://openrouter.ai/api/v1"


def test_models_for_includes_openrouter():
    assert ai_providers.models_for("openrouter")


def test_a_non_live_openrouter_model_never_reaches_the_voice_session():
    """The voice session must stay on a Gemini Live model, whatever the text
    provider is — this is the guard that used to be the whole point of
    resolve_live_model."""
    model, note = ai_providers.resolve_live_model({
        "ai_provider": "openrouter",
        "ai_model": "anthropic/claude-sonnet-4.5",
    })
    assert model == ai_providers.GEMINI_DEFAULT_MODEL
    assert note == ""


#: Realistic shapes: long, unbroken tokens (a real OpenRouter key is ~73 chars).
OR_KEY = "sk-or-v1-" + "0123456789abcdef" * 4
GEM_KEY = "AIzaSy" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456"


def test_openrouter_is_offered_before_gemini_for_synthesis(monkeypatch):
    """OpenRouter leads the synthesis chain whenever its key is usable."""
    cfg = {"ai_provider": "openrouter", "openrouter_api_key": OR_KEY,
           "gemini_api_key": GEM_KEY}
    monkeypatch.setattr(forge, "_config_view", lambda config=None: dict(cfg))
    chain = [name for name, _cfg in forge.synthesis_providers()]
    assert chain[0] == "openrouter"
    assert "gemini" in chain


def test_openrouter_leads_even_when_the_configured_provider_is_gemini(monkeypatch):
    """The user's chosen synthesis brain, not the chat provider, leads the
    forge chain: Settings ▸ AI still drives conversation."""
    cfg = {"ai_provider": "gemini", "openrouter_api_key": OR_KEY,
           "gemini_api_key": GEM_KEY}
    monkeypatch.setattr(forge, "_config_view", lambda config=None: dict(cfg))
    chain = [name for name, _cfg in forge.synthesis_providers()]
    assert chain[:2] == ["openrouter", "gemini"]


def test_a_stored_value_that_is_not_a_key_is_skipped(monkeypatch):
    """A pasted paragraph in the OpenRouter field must never be sent: a forge
    run would spend its first attempt on a guaranteed 401."""
    cfg = {"ai_provider": "openrouter",
           "openrouter_api_key": "now  scan or innspect en the",
           "gemini_api_key": GEM_KEY}
    monkeypatch.setattr(forge, "_config_view", lambda config=None: dict(cfg))
    chain = [name for name, _cfg in forge.synthesis_providers()]
    assert "openrouter" not in chain
    assert chain[0] == "gemini", "the next usable provider must still forge"
    skipped = {row["provider"]: row for row in forge.provider_diagnostics(cfg)}
    assert skipped["openrouter"]["state"] == "skipped"
    assert "spaces" in skipped["openrouter"]["detail"]
    # ...and the chain flag makes it visible in the UI without re-reading keys.
    assert skipped["openrouter"]["in_chain"] is False
    assert skipped["gemini"]["state"] == "usable" and skipped["gemini"]["in_chain"]


def test_provider_diagnostics_names_what_is_missing(monkeypatch):
    cfg = {"ai_provider": "gemini", "gemini_api_key": GEM_KEY}
    monkeypatch.setattr(forge, "_config_view", lambda config=None: dict(cfg))
    rows = {row["provider"]: row for row in forge.provider_diagnostics(cfg)}
    assert rows["gemini"]["state"] == "usable"
    assert rows["openai"]["state"] == "missing"
    assert rows["openai"]["in_chain"] is False


def test_synthesis_chain_is_empty_without_keys(monkeypatch):
    monkeypatch.setattr(forge, "_config_view", lambda config=None: {})
    assert forge.synthesis_providers() == []


def test_chat_rejects_gemini_with_a_clear_sentence():
    import pytest
    with pytest.raises(ValueError):
        ai_providers.chat({"ai_provider": "gemini"}, "hi")
