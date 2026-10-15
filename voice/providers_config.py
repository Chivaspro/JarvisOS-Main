"""Provider-scoped voice configuration.

Single owner of the nested ``voice`` configuration block and its mapping onto
the legacy flat keys.  Every reader/writer outside this module goes through
these functions; they are pure functions over config dicts (no file I/O) so
they can be exercised without touching disk.

Schema (stored under the ``voice`` key of config/api_keys.json)::

    "voice": {
        "activeProvider": "fishAudio",
        "defaultProvider": "fishAudio",
        "defaultVoice": "<fish voice id>",
        "defaultPipeline": "gemini",         # "gemini" | "gemini_piper" | "local"
        "geminiRenderer": "native",          # "native" | "piper"
        "voice_providers_migrated": true,
        "providers": {
            "fishAudio":  {...}, "elevenLabs": {...}, "gemini": {...},
            "piperOnnx": {...}, "edgeTts": {...}, "kokoro": {...}, "sapi": {...}
        }
    }

Dual-read: any subtree value missing from the nested block is filled from the
legacy flat keys, so pre-migration and hand-built configs keep working and the
flat keys are never deleted.
"""
from __future__ import annotations

import copy
from typing import Optional

PROVIDER_IDS = ("fishAudio", "elevenLabs", "gemini", "piperOnnx", "edgeTts", "kokoro", "sapi")

# Canonical TTS engine name each provider maps onto in create_tts_player().
# ``gemini`` never builds a local engine — the Live session renders it.
PROVIDER_ENGINE = {
    "fishAudio": "fish_audio",
    "elevenLabs": "elevenlabs",
    "gemini": "gemini",
    "piperOnnx": "onnx",
    "edgeTts": "edgetts",
    "kokoro": "kokoro",
    "sapi": "sapi",
}

VOICE_KEY = "voice"
MIGRATED_KEY = "voice_providers_migrated"

# Legacy mode aliases already understood by voice.hybrid_voice — mirrored here
# so provider derivation does not import the UI-adjacent module.
_GEMINI_MODES = {"gemini", "gemini_piper", "hybrid", "native", "gemini_native", "native_audio", "piper"}

_ENGINE_TO_PROVIDER = {
    "fish_audio": "fishAudio",
    "fish": "fishAudio",
    "fishaudio": "fishAudio",
    "elevenlabs": "elevenLabs",
    "onnx": "piperOnnx",
    "piper": "piperOnnx",
    "edgetts": "edgeTts",
    "edge": "edgeTts",
    "kokoro": "kokoro",
    "sapi": "sapi",
    "sapi5": "sapi",
    "pyttsx3": "sapi",
    "windows": "sapi",
}

_PROVIDER_DEFAULTS: dict[str, dict] = {
    "fishAudio": {
        "apiKey": "",
        "endpoint": "https://api.fish.audio/v1/tts",
        "model": "s2-pro",
        "voice": "",
        "format": "mp3",
        "latency": "normal",
    },
    "elevenLabs": {
        "apiKey": "",
        "model": "eleven_multilingual_v2",
        "voice": "pNInz6obpgDQGcFmaJgB",
    },
    "gemini": {
        "apiKey": "",
        "model": "",
        "voice": "Charon",
    },
    "piperOnnx": {
        "modelPath": "config/voices/jarvis-high.onnx",
        "configPath": "config/voices/jarvis-high.json",
        "speaker": "default",
        "executionProvider": "cpu",
    },
    "edgeTts": {
        "voice": "en-GB-RyanNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "volume": 1.0,
    },
    "kokoro": {
        "voice": "af_heart",
        "speed": 1.0,
    },
    "sapi": {
        "voice": "",
    },
}

_GEMINI_LIVE_VOICES = ("Charon", "Puck", "Kore", "Fenrir", "Aoede")


class ProviderConfigError(ValueError):
    """A provider's configuration is invalid (missing key, bad shape, …)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _s(value) -> str:
    return str(value or "").strip()


def _block(cfg: dict) -> dict:
    """Return the raw stored ``voice`` block (empty dict when absent)."""
    raw = (cfg or {}).get(VOICE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _providers(raw_block: dict) -> dict:
    raw = raw_block.get("providers")
    return dict(raw) if isinstance(raw, dict) else {}


def _flat(cfg: dict) -> dict:
    return cfg or {}


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def providers_block(cfg: dict) -> dict:
    """The normalized ``voice`` block with dual-read over the legacy flat keys.

    Pure: never mutates ``cfg``.  Nested values win; flat keys fill the gaps.
    """
    cfg = cfg or {}
    block = _block(cfg)
    flat = cfg
    providers: dict[str, dict] = {}

    raw_providers = _providers(block)
    for pid in PROVIDER_IDS:
        merged = dict(_PROVIDER_DEFAULTS[pid])
        merged.update({k: v for k, v in (raw_providers.get(pid) or {}).items() if v is not None})

        # ── dual-read: legacy flat keys fill any still-default value ──
        def _fill(key: str, value) -> None:
            if _s(merged.get(key, "")) == _s(_PROVIDER_DEFAULTS[pid].get(key, "")) and _s(value):
                merged[key] = value

        if pid == "fishAudio":
            _fill("apiKey", _s(flat.get("fish_audio_api_key")))
            _fill("endpoint", _s(flat.get("fish_audio_endpoint")))
            _fill("model", _s(flat.get("fish_audio_model_id")))
            _fill("voice", _s(flat.get("fish_audio_voice_id")))
            if merged.get("format") == _PROVIDER_DEFAULTS[pid]["format"] and _s(flat.get("fish_audio_format")):
                merged["format"] = _s(flat.get("fish_audio_format"))
            if merged.get("latency") == _PROVIDER_DEFAULTS[pid]["latency"] and _s(flat.get("fish_audio_latency")):
                merged["latency"] = "balanced" if _s(flat.get("fish_audio_latency")) == "low" \
                    else _s(flat.get("fish_audio_latency"))
        elif pid == "elevenLabs":
            _fill("apiKey", _s(flat.get("elevenlabs_api_key")))
            _fill("model", _s(flat.get("elevenlabs_model_id")))
            _fill("voice", _s(flat.get("elevenlabs_voice_id")))
        elif pid == "gemini":
            _fill("apiKey", _s(flat.get("gemini_api_key")))
            _fill("voice", _s(flat.get("voice_name")))
            if not merged.get("model") and _s(flat.get("ai_model")):
                merged["model"] = _s(flat.get("ai_model"))
        elif pid == "piperOnnx":
            _fill("modelPath", _s(flat.get("onnx_voice_model")))
            _fill("configPath", _s(flat.get("onnx_voice_config")))
            _fill("speaker", _s(flat.get("onnx_voice_speaker")))
            if merged.get("executionProvider", "cpu") == "cpu" and _s(flat.get("onnx_execution_provider")):
                merged["executionProvider"] = _s(flat.get("onnx_execution_provider"))
        elif pid == "edgeTts":
            if merged.get("voice") == _PROVIDER_DEFAULTS[pid]["voice"] and _s(flat.get("tts_voice")):
                merged["voice"] = _s(flat.get("tts_voice"))
        elif pid == "kokoro":
            # The kokoro voice historically lived in the shared tts_voice field.
            if str(_s(flat.get("tts_engine"))).lower() == "kokoro" and _s(flat.get("tts_voice")):
                merged["voice"] = _s(flat.get("tts_voice"))
        elif pid == "sapi":
            _fill("voice", _s(flat.get("sapi_voice")))

        providers[pid] = merged

    renderer = _s(block.get("geminiRenderer")) or "native"
    if renderer not in ("native", "piper"):
        renderer = "native"
    pipeline = _s(block.get("defaultPipeline"))
    if pipeline not in ("gemini", "gemini_piper", "local"):
        pipeline = ""
    out = {
        "activeProvider": active_provider(cfg),
        "defaultProvider": _s(block.get("defaultProvider")),
        "defaultVoice": _s(block.get("defaultVoice")),
        "defaultPipeline": pipeline,
        "geminiRenderer": renderer,
        MIGRATED_KEY: bool(block.get(MIGRATED_KEY, flat.get(MIGRATED_KEY, False))),
        "providers": providers,
    }
    return out


def active_provider(cfg: dict) -> str:
    """The configured/derived active provider id (never empty)."""
    cfg = cfg or {}
    block = _block(cfg)
    stored = _s(block.get("activeProvider"))
    if stored in PROVIDER_IDS:
        return stored
    # Derive from the legacy flat keys: the Gemini pipeline modes own speech
    # unless the mode is local, in which case the engine decides.
    mode = _s(cfg.get("voice_output_mode") or cfg.get("mode")).lower()
    engine = _s(cfg.get("tts_engine")).lower()
    if mode and mode in _GEMINI_MODES and mode != "local":
        return "gemini"
    if not mode or mode == "local":
        return _ENGINE_TO_PROVIDER.get(engine, "edgeTts")
    return "gemini"


def set_active_provider(cfg: dict, provider: str) -> dict:
    """Return a copy of ``cfg`` with ``activeProvider`` set (provider-only write)."""
    if provider not in PROVIDER_IDS:
        raise ProviderConfigError(f"Unknown voice provider: {provider}")
    out = copy.deepcopy(cfg or {})
    block = dict(out.get(VOICE_KEY) or {})
    block["activeProvider"] = provider
    out[VOICE_KEY] = block
    return out


def save_provider(cfg: dict, provider: str, values: dict) -> dict:
    """Replace exactly one provider's subtree; every other key is carried through.

    The written ``voice`` block is the full normalized structure (all seven
    provider subtrees, derived from the nested block plus the legacy flat
    keys), so a save leaves a self-describing config behind.
    """
    if provider not in PROVIDER_IDS:
        raise ProviderConfigError(f"Unknown voice provider: {provider}")
    # Underscore-prefixed keys are transport-only sentinels (e.g. the AI-page
    # Gemini key presence flag); they are never validated nor persisted.
    clean = {k: v for k, v in (values or {}).items() if not str(k).startswith("_")}
    allowed = set(_PROVIDER_DEFAULTS[provider])
    unknown = set(clean) - allowed
    if unknown:
        raise ProviderConfigError(
            f"Unknown {provider} settings: {', '.join(sorted(unknown))}")
    # Start from the normalized (dual-read) view so the persisted block is
    # complete, then replace just this provider's subtree.
    normalized = providers_block(cfg or {})
    out = copy.deepcopy(cfg or {})
    block = dict(out.get(VOICE_KEY) or {})
    providers = dict(normalized["providers"])
    current = dict(providers.get(provider) or {})
    current.update(clean)
    providers[provider] = current
    block["providers"] = providers
    block["defaultProvider"] = normalized["defaultProvider"]
    block["defaultVoice"] = normalized["defaultVoice"]
    block["defaultPipeline"] = normalized["defaultPipeline"]
    block["geminiRenderer"] = normalized["geminiRenderer"]
    block[MIGRATED_KEY] = normalized[MIGRATED_KEY]
    out[VOICE_KEY] = block
    return out


def default_pair(cfg: dict) -> Optional[tuple[str, str]]:
    """The persisted (defaultProvider, defaultVoice) pair, or None when unset."""
    block = _block(cfg or {})
    provider = _s(block.get("defaultProvider"))
    voice = _s(block.get("defaultVoice"))
    if provider and voice:
        return (provider, voice)
    return None


def provider_voices(cfg: dict, provider: str) -> tuple[str, ...]:
    """The voices that legitimately belong to ``provider`` (for pair validation)."""
    values = providers_block(cfg or {}).get("providers", {}).get(provider, {})
    if provider == "gemini":
        known = tuple(v for v in _GEMINI_LIVE_VOICES if v)
        stored = _s(values.get("voice"))
        return (known + (stored,)) if stored and stored not in known else known
    if provider == "piperOnnx":
        # A Piper arrangement is identified by its model file, not a named
        # voice — jarvis-high.onnx IS the voice identity for the default pair.
        import os
        mp = _s(values.get("modelPath"))
        name = os.path.basename(mp.replace("\\", "/")) if mp else ""
        return (name,) if name else ()
    stored = _s(values.get("voice"))
    return (stored,) if stored else ()


def set_default_pair(cfg: dict, provider: str, voice: str) -> dict:
    """Write defaultProvider + defaultVoice together, validated. Atomic pair."""
    if provider not in PROVIDER_IDS:
        raise ProviderConfigError(f"Unknown voice provider: {provider}")
    voice = _s(voice)
    if not voice:
        raise ProviderConfigError(f"{provider} default voice is empty — select a voice first.")
    voices = provider_voices(cfg or {}, provider)
    if voices and voice not in voices:
        raise ProviderConfigError(
            f"Invalid default pair: voice '{voice}' does not belong to provider "
            f"'{provider}' (expected one of: {', '.join(voices)}).")
    out = copy.deepcopy(cfg or {})
    block = dict(out.get(VOICE_KEY) or {})
    block["defaultProvider"] = provider
    block["defaultVoice"] = voice
    out[VOICE_KEY] = block
    return out


def default_pipeline(cfg: dict) -> str:
    """The persisted startup pipeline, or '' when no default was recorded."""
    pipeline = _s(_block(cfg or {}).get("defaultPipeline"))
    return pipeline if pipeline in ("gemini", "gemini_piper", "local") else ""


def set_default_pipeline(cfg: dict, pipeline: str) -> dict:
    """Return a copy of ``cfg`` with the validated default pipeline recorded.

    The default pipeline is the startup renderer for every session that loads
    without an explicit mode choice; a fresh install starts on it.
    """
    pipeline = _s(pipeline)
    if pipeline not in ("gemini", "gemini_piper", "local"):
        raise ProviderConfigError(
            f"Unknown pipeline: {pipeline} (expected gemini, gemini_piper or local).")
    out = copy.deepcopy(cfg or {})
    block = dict(out.get(VOICE_KEY) or {})
    block["defaultPipeline"] = pipeline
    out[VOICE_KEY] = block
    return out


# ---------------------------------------------------------------------------
# provider-specific validation
# ---------------------------------------------------------------------------

def _looks_like_elevenlabs_id(value: str) -> bool:
    v = _s(value)
    return len(v) >= 20 and all(ch in "0123456789abcdefABCDEF" for ch in v)


def validate_provider(name: str, values: dict) -> Optional[str]:
    """Provider-specific validation. Returns an error message or None."""
    v = values or {}
    if name == "fishAudio":
        if not _s(v.get("apiKey")):
            return "Fish Audio API key is missing — add it in Settings ▸ Voice ▸ Fish Audio."
        endpoint = _s(v.get("endpoint")) or "https://api.fish.audio/v1/tts"
        if not endpoint.startswith(("https://", "http://")):
            return "Fish Audio endpoint must be an HTTP(S) URL."
        if not _s(v.get("voice")):
            return "Fish Audio needs a voice: fill Voice ID in Settings ▸ Voice ▸ Fish Audio."
        if _s(v.get("model")) and len(_s(v.get("model"))) >= 20 and \
                all(ch in "0123456789abcdefABCDEF" for ch in _s(v.get("model"))):
            return ("Fish Audio model looks like a voice ID — put the voice ID in the "
                    "Voice field and the backend model (s1, s2-pro) in Model.")
        return None
    if name == "elevenLabs":
        if not _s(v.get("apiKey")):
            return "ElevenLabs API key is missing — add it in Settings ▸ Voice ▸ ElevenLabs."
        vid = _s(v.get("voice"))
        lowered = vid.lower()
        if "neural" in lowered or "-" in vid or "_" in vid or " " in vid:
            return (f"'{vid}' looks like an EdgeTTS voice, not an ElevenLabs voice ID — "
                    f"use a 20-character ID (default Adam: pNInz6obpgDQGcFmaJgB).")
        if vid and not _looks_like_elevenlabs_id(vid):
            return f"'{vid}' does not look like an ElevenLabs voice ID (20 hex characters)."
        if not _s(v.get("model")):
            return "ElevenLabs model ID is empty — pick one (e.g. eleven_multilingual_v2)."
        return None
    if name == "gemini":
        # The Gemini key is entered on the AI page, not here — the caller
        # passes it through ``apiKey`` when known; an empty value is only an
        # error when the whole config has no Gemini key either.
        if not _s(v.get("apiKey")) and not v.get("_configHasGeminiKey"):
            return "Gemini API key is missing — add it in Settings ▸ AI (Gemini)."
        return None
    if name == "piperOnnx":
        if not _s(v.get("modelPath")):
            return ("Piper ONNX voice not configured — pick a local .onnx model "
                    "(config/voices/jarvis-high.onnx ships with JARVIS).")
        return None
    if name == "edgeTts":
        if not _s(v.get("voice")):
            return "Edge TTS voice is empty — pick a voice like en-GB-RyanNeural."
        return None
    if name == "kokoro":
        if not _s(v.get("voice")):
            return "Kokoro voice is empty — pick a voice like af_heart."
        return None
    if name == "sapi":
        if not _s(v.get("voice")):
            return "Windows SAPI voice is empty — pick an installed Windows voice."
        return None
    return f"Unknown voice provider: {name}"


# ---------------------------------------------------------------------------
# one-time migration
# ---------------------------------------------------------------------------

def migrate_to_providers(cfg: dict) -> tuple[dict, bool]:
    """Map the legacy flat keys into the provider subtrees exactly once.

    Returns ``(new_cfg, migrated)``.  The flat keys are left untouched
    (dual-read); no default pair is invented from the shared legacy voice
    field; the existing Gemini voice (e.g. Charon) is preserved.
    """
    cfg = cfg or {}
    if _block(cfg).get(MIGRATED_KEY) or cfg.get(MIGRATED_KEY):
        return cfg, False

    out = copy.deepcopy(cfg)
    block = dict(out.get(VOICE_KEY) or {})
    providers = dict(block.get("providers") or {})

    def _sub(pid: str) -> dict:
        merged = dict(_PROVIDER_DEFAULTS[pid])
        merged.update({k: v for k, v in (providers.get(pid) or {}).items() if v is not None})
        return merged

    fish = _sub("fishAudio")
    for src, dst in (("fish_audio_api_key", "apiKey"), ("fish_audio_endpoint", "endpoint"),
                     ("fish_audio_model_id", "model"), ("fish_audio_voice_id", "voice"),
                     ("fish_audio_format", "format"), ("fish_audio_latency", "latency")):
        if _s(out.get(src)):
            fish[dst] = _s(out.get(src))
    if _s(fish.get("latency")) == "low":
        fish["latency"] = "balanced"
    providers["fishAudio"] = fish

    eleven = _sub("elevenLabs")
    for src, dst in (("elevenlabs_api_key", "apiKey"), ("elevenlabs_model_id", "model"),
                     ("elevenlabs_voice_id", "voice")):
        if _s(out.get(src)):
            eleven[dst] = _s(out.get(src))
    providers["elevenLabs"] = eleven

    gem = _sub("gemini")
    if _s(out.get("gemini_api_key")):
        gem["apiKey"] = _s(out.get("gemini_api_key"))
    vname = _s(out.get("voice_name"))
    if vname and vname in _GEMINI_LIVE_VOICES:
        gem["voice"] = vname
    providers["gemini"] = gem

    piper = _sub("piperOnnx")
    for src, dst in (("onnx_voice_model", "modelPath"), ("onnx_voice_config", "configPath"),
                     ("onnx_voice_speaker", "speaker")):
        if _s(out.get(src)):
            piper[dst] = _s(out.get(src))
    if _s(out.get("onnx_execution_provider")):
        piper["executionProvider"] = _s(out.get("onnx_execution_provider"))
    providers["piperOnnx"] = piper

    edge = _sub("edgeTts")
    if _s(out.get("tts_voice")):
        edge["voice"] = _s(out.get("tts_voice"))
    providers["edgeTts"] = edge

    kok = _sub("kokoro")
    if _s(out.get("tts_engine")).lower() == "kokoro" and _s(out.get("tts_voice")):
        kok["voice"] = _s(out.get("tts_voice"))
    providers["kokoro"] = kok

    sap = _sub("sapi")
    if _s(out.get("sapi_voice")):
        sap["voice"] = _s(out.get("sapi_voice"))
    providers["sapi"] = sap

    block["providers"] = providers

    # Derive the active provider and the Gemini renderer from the legacy mode.
    # Since the native-audio flip, a mode that maps to the Piper pipeline
    # WITHOUT the deliberate-choice marker lands on the native renderer.
    mode = _s(out.get("voice_output_mode") or out.get("mode")).lower()
    engine = _s(out.get("tts_engine")).lower()
    renderer = "native" if (mode == "gemini" or not out.get("voice_pipeline_migrated")) else "piper"
    if mode in ("", "local") or mode not in _GEMINI_MODES:
        block["activeProvider"] = _ENGINE_TO_PROVIDER.get(engine, "edgeTts")
    else:
        block["activeProvider"] = "gemini"
    block["geminiRenderer"] = renderer
    block[MIGRATED_KEY] = True
    out[VOICE_KEY] = block
    return out, True
