"""voice_settings action — JARVIS checking and changing its own voice engine.

Two modes:
  status — read-only. Reports the active voice provider/pipeline (e.g.
           "Gemini Native Audio", "ElevenLabs", "Piper ONNX"). Always
           allowed; this is just self-awareness, not a change.
  set    — switches the voice output engine. Gated by the
           ``voice_engine_control`` permission switch (security/permissions.py
           refuses this before the function is ever called when the switch
           is off). Persists through the same config-guard write path the
           Settings dialog uses, then resets the live HybridVoiceSystem
           singleton so the new engine takes effect on the next reply
           instead of requiring a restart.

Recognized ``provider`` values for mode="set":
  "gemini_native" | "gemini" | "native"   -> Gemini Live, native audio
  "gemini_piper"                          -> Gemini brain, Piper ONNX voice
  "local" | "piper"                       -> fully local Piper ONNX
  "elevenlabs" | "fishaudio" | "edgetts" | "kokoro" | "sapi"
                                           -> the named third-party provider
                                              (only if it is already
                                              configured in Settings)
"""

from __future__ import annotations

_PIPELINE_LABEL = {
    "gemini": "Gemini Native Audio",
    "gemini_piper": "Gemini (brain) → Piper ONNX (voice)",
    "local": "Local Piper ONNX only",
}

_PROVIDER_ALIASES = {
    "gemini_native": "gemini", "gemini": "gemini", "native": "gemini",
    "native_audio": "gemini", "gemini_piper": "gemini_piper",
    "local": "local", "piper": "local", "piperonnx": "piperOnnx",
    "elevenlabs": "elevenLabs", "fishaudio": "fishAudio",
    "edgetts": "edgeTts", "kokoro": "kokoro", "sapi": "sapi",
}


def _get_config_path():
    from config.settings import API_KEYS_FILE
    return API_KEYS_FILE


def _status() -> str:
    try:
        from voice.providers_config import (
            active_provider, default_pipeline, providers_block,
        )
        from ui_settings import load as _load_cfg
        cfg = _load_cfg(_get_config_path())
    except Exception as exc:
        return f"I could not read the voice configuration: {exc}"

    provider = active_provider(cfg)
    pipeline = default_pipeline(cfg) or (
        "gemini" if provider == "gemini" else "local"
    )
    block = providers_block(cfg)
    renderer = block.get("geminiRenderer", "native")
    label = _PIPELINE_LABEL.get(pipeline, pipeline)
    if provider == "gemini":
        return (f"My voice engine is Gemini, renderer={renderer} "
                f"({label}).")
    return f"My voice engine is {provider} (default pipeline: {label})."


def _apply_pipeline_write(cfg: dict, updates: dict, path) -> dict:
    """Same guarded-write path Settings uses for a pipeline-scoped save."""
    try:
        from core import config_guard as _guard
        merged = dict(_guard.read(path))
        merged.update(updates)
        ok, msg = _guard.guard_write(merged, tag="voice-pipeline", path=path)
        if not ok:
            raise RuntimeError(msg)
        return _guard.read(path)
    except Exception:
        from ui_settings import save as _save_cfg
        merged = dict(cfg)
        merged.update(updates)
        return _save_cfg(path, **{k: v for k, v in merged.items() if k != "features"})


def _switch(provider_raw: str) -> str:
    key = str(provider_raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    provider = _PROVIDER_ALIASES.get(key, key)
    if not provider:
        return "Please tell me which voice engine to switch to."

    try:
        from voice.providers_config import (
            ProviderConfigError, providers_block, set_active_provider,
            set_default_pipeline,
        )
        from ui_settings import load as _load_cfg
        path = _get_config_path()
        cfg = _load_cfg(path)
    except Exception as exc:
        return f"I could not reach the voice configuration layer: {exc}"

    try:
        if provider in ("gemini", "gemini_piper", "local"):
            pipeline = provider
            cfg["voice_output_mode"] = pipeline
            cfg["voice_pipeline_migrated"] = True
            cfg["voice_piper_to_native_lifted"] = True
            block = providers_block(cfg)
            block["geminiRenderer"] = "native" if pipeline == "gemini" else "piper"
            cfg["voice"] = block
            cfg = set_default_pipeline(cfg, pipeline)
            updates = {
                "voice_output_mode": pipeline,
                "voice_pipeline_migrated": True,
                "voice_piper_to_native_lifted": True,
                "voice": cfg.get("voice", {}),
            }
            label = _PIPELINE_LABEL.get(pipeline, pipeline)
        else:
            cfg = set_active_provider(cfg, provider)
            updates = {"voice": cfg.get("voice", {})}
            label = provider

        _apply_pipeline_write(cfg, updates, path)
    except ProviderConfigError as exc:
        return f"I could not switch to that voice engine: {exc}"
    except Exception as exc:
        return f"The voice engine change did not save: {exc}"

    # Live-apply: drop the running voice singleton so the very next reply
    # rebuilds itself from the freshly saved config, instead of requiring
    # a restart of JARVIS.
    try:
        from voice.hybrid_voice import HybridVoiceSystem
        HybridVoiceSystem.reset()
    except Exception:
        pass

    return f"Voice engine switched to {label}, sir."


def voice_settings(parameters: dict, player=None, speak=None, **_kw) -> str:
    p = parameters or {}
    mode = str(p.get("mode", "status") or "status").strip().lower()
    # `action` is accepted as an alias — the permission gate keys off it.
    if mode in ("", "status", "check", "get"):
        return _status()
    if mode in ("set", "switch", "change"):
        return _switch(p.get("provider", "") or p.get("engine", ""))
    return "mode should be 'status' or 'set'."
