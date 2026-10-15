# Fix voice provider isolation and the startup logo settle animation

## Why

Two independent defects ship in this build. (1) The Voice settings page reads as if each speech engine
were an independent provider, but all engines share one flat field set with no notion of a provider, so
selecting Fish Audio and saving can leave JARVIS speaking Gemini's voice ("Charon") — the UI accepts
contradictory configurations silently and rewrites Gemini's own voice on every save. (2) The startup
Arc-Reactor logo, which opens at 190 px and must shrink smoothly to 180 px, still teleports for some
users because the settle path can dead-end and unrelated geometry writers can interrupt the animation
mid-flight.

## What Changes

- **Provider-scoped voice configuration**: one nested `voice` configuration block with `activeProvider`,
  `defaultProvider`, `defaultVoice` and an independent per-provider subtree (`providers.fishAudio`,
  `providers.elevenLabs`, `providers.gemini`, `providers.piperOnnx`, `providers.edgeTts`,
  `providers.kokoro`, `providers.sapi`). Saving or switching one provider never writes another's subtree.
- **Voice settings page restructure**: a provider selector at the top of the Voice tab; only the selected
  provider's own fields are visible and editable (the other panels are hidden, not dimmed); per-provider
  **Test Voice / Save / Save as Default** actions; **Refresh** (voice/model lists) for Fish Audio and
  ElevenLabs, populated only into that provider's panel.
- **Save semantics scoped to the selected provider**: **Save** validates and persists only the current
  provider's configuration, makes it the active provider, and rebuilds the TTS cache. **Save as Default**
  additionally persists `defaultProvider` + `defaultVoice` as one atomic, validated pair.
- **Default-pair validity guarantee**: a persisted default voice must belong to the persisted default
  provider; an invalid pair is rejected at save and at load, never silently rewritten.
- **Migration**: flat keys (`tts_engine`, `tts_voice`, `fish_audio_*`, `elevenlabs_*`, `onnx_*`,
  `voice_name`…) map once into the provider subtrees behind a migration marker, with dual-read retained so
  older binaries and tests keep working; the existing Gemini Charon configuration is preserved.
- **Runtime selection follows `activeProvider`**: the resolved mode is Gemini (native or Piper-rendered)
  when the Gemini provider is active, otherwise the local engine named by `activeProvider`; the selected
  Fish Audio voice and model reach the Fish Audio engine.
- **Startup logo settle animation**: a single explicit state machine (STARTUP → ANIMATING → SETTLED) owns
  the whole startup sequence; position and logo size interpolate together in one timeline from 190 px to
  180 px on the same logo widget; the click-during-startup dead-end is fixed; unrelated geometry writers
  (`_collapse`, `_expand`, settings apply, initial visibility) become phase-aware and can no longer
  overwrite animated geometry.
- **Non-goals**: no change to the three voice output modes' meaning or to the Gemini→Piper pipeline itself
  (`gemini-piper-pipeline` behaviour is preserved via the Gemini provider's renderer setting); no change to
  the startup logo asset or to `STARTUP_LOGO_SIZE = 190`; no animation-duration tuning as a fix; no new
  TTS engines and no removal of existing ones.

## Capabilities

### New Capabilities

- `voice-provider-isolation`: per-provider voice configuration, scoping of Save / Save as Default to the
  selected provider, a validated `defaultProvider`/`defaultVoice` pair, provider-scoped refresh, provider
  switching without cross-contamination, flat-key migration, and runtime selection driven by
  `activeProvider`.
- `startup-logo-animation`: the startup reactor's settle sequence — a 190→180 px same-asset transition
  with position and size interpolated together in one timeline, explicit STARTUP/ANIMATING/SETTLED states,
  a click path that always settles, and freedom from competing geometry writers.

### Modified Capabilities

- `voice-output-routing`: SAVE AS DEFAULT VOICE must additionally persist the selected provider and its
  voice as a validated pair (extending the existing "persists the selected arrangement" requirement, not
  replacing it); the local engine choice becomes the provider named by `activeProvider`. Mode meanings,
  migration of `gemini`/`hybrid`/`gemini_piper`, audio suppression and hot-apply requirements are
  unchanged.
- `gemini-piper-pipeline`: the ONNX-voice-render requirement is restated against the Piper provider
  subtree (`providers.piperOnnx`) instead of the flat engine keys; the requirement that selecting another
  engine elsewhere does not alter the Piper arrangement becomes an isolation guarantee (saving Fish Audio
  must not touch `providers.piperOnnx`).

## Impact

- **Code**: `ui_settings.py` (Voice tab restructure, provider panels, save scoping, DEFAULTS),
  `voice/tts.py` (provider-subtree config loading, factory reading, cache key), `voice/hybrid_voice.py`
  (provider-aware mode resolution, one-time migration, default-pair validation),
  `memory/config_manager.py` (`get_voice` reads only the Gemini subtree; `save_voice` scope),
  `main.py` (mode plumbing only — `voice_mode()` and prewarm/greeting routes), `ui.py` (startup state
  machine around `_prepare_startup_presentation` / `_begin_startup_position_animation` / `_finalize`,
  phase-aware geometry writers, logo-button cancel path), `core/config_guard.py` (nested-key tolerance).
- **Config**: `config/api_keys.json` gains one nested `voice` key; flat keys remain readable for one
  release (dual-read, no deletion).
- **Tests**: new regression tests under `tests/hybrid_voice/` (provider isolation, default-pair validity,
  switch cycle, runtime mapping, migration) and `tests/ui/` (startup phase transitions, no-teleport
  geometry monotonicity, same-asset assertion, competing-writer immunity), plus updates to
  `tests/hybrid_voice/test_settings_default_voice.py` and `tests/hybrid_voice/test_piper_pipeline.py`.
- **Dependencies**: none added.
- **Risk focus**: the nested-key migration must be reversible (dual-read) and the startup state machine
  must preserve the existing click-to-open and drag behaviour of the reactor.
