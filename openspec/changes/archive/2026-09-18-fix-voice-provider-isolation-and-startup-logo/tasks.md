## 1. Provider configuration layer (`voice/providers_config.py`)

- [x] 1.1 Create `voice/providers_config.py` with the schema constants (`PROVIDER_IDS = {fishAudio,
      elevenLabs, gemini, piperOnnx, edgeTts, kokoro, sapi}`), `ProviderConfigError(ValueError)`, and
      `providers_block(cfg)` implementing dual-read: nested `voice.providers.*` values first, legacy flat
      keys (`fish_audio_*`, `elevenlabs_*`, `onnx_*`, `tts_voice`, `sapi_voice`, `voice_name`) as
      per-key fallback, normalized against per-provider defaults; pure functions only (no file I/O)
- [x] 1.2 Implement `active_provider(cfg)` (derive from `voice_output_mode`+`tts_engine` when
      `voice.activeProvider` is absent, mapping fish/fish_audio/fishaudio → fishAudio etc.), `set_active_provider`,
      `save_provider` (replaces exactly one subtree), `default_pair`, and `set_default_pair` (raises
      `ProviderConfigError` unless the voice belongs to that provider's subtree/voice list; writes
      `defaultProvider`+`defaultVoice` together)
- [x] 1.3 Implement `validate_provider(name, values)` returning `None` or a provider-specific message for:
      missing/blank API key, endpoint not http(s), voice/model shape checks (reuse the ElevenLabs
      voice-ID heuristics from `voice/tts.py` via import so the rules live in one place), empty Piper
      model path, empty Edge voice
- [x] 1.4 Implement `migrate_to_providers(cfg) -> (new_cfg, migrated)` with the `voice_providers_migrated`
      marker, mapping every legacy key per design.md §2, deriving `activeProvider` + `geminiRenderer`,
      and inventing no default pair; leave all flat keys untouched in the returned dict
- [x] 1.5 Unit tests `tests/hybrid_voice/test_providers_config.py`: dual-read resolves flat-only configs;
      `save_provider("fishAudio", …)` leaves every other subtree byte-identical; `set_default_pair`
      rejects `("fishAudio", "Charon")` and accepts a valid pair; `migrate_to_providers` preserves all
      five providers incl. `providers.gemini.voice == "Charon"`, sets the marker, runs once (second call
      is a no-op), never invents `defaultVoice`, and maps `gemini_piper` → gemini+piper renderer;
      `active_provider` maps every legacy combination; validation messages name the provider

## 2. TTS runtime reads the provider layer

- [x] 2.1 `voice/tts.py`: `create_tts_player()` resolves engine parameters via `providers_block()` with
      the existing flat-key fallback for unmigrated dicts; each factory branch (`fish_audio`,
      `elevenlabs`, `kokoro`, `sapi`, `onnx`, `edgetts`) reads only its own provider's values
- [x] 2.2 Extend `_tts_cache_key()` with the active provider id and its subtree values so switching
      providers (or editing one provider's fields) rebuilds the engine while unrelated saves reuse it
- [x] 2.3 `voice/hybrid_voice.py`: add `resolve_active_mode(cfg)` returning the existing
      `gemini | gemini_piper | local` vocabulary from `active_provider()` + `geminiRenderer` (gemini
      provider with renderer `piper` → `gemini_piper`; renderer `native` → `gemini`; any other provider →
      `local`); keep `resolve_voice_mode()` untouched for legacy callers and tests
- [x] 2.4 `main.py`: switch `voice_mode()` (and the `_apply_voice_mode` config read) from
      `resolve_voice_mode` to `resolve_active_mode`; verify the prewarm/greeting routes and the
      Gemini-audio suppression branches need no other change (vocabulary identical)
- [x] 2.5 `memory/config_manager.py`: `get_voice()` reads `providers.gemini.voice` first with the flat
      `voice_name` fallback; `save_voice()` writes the gemini subtree and mirrors the flat key in the
      same write; `ui.py:_apply_full_settings` calls `save_voice` only when the Gemini voice actually
      changed
- [x] 2.6 Tests `tests/hybrid_voice/test_provider_runtime.py`: with `activeProvider=fishAudio` the factory
      builds `FishAudioTTSEngine` with the fish voice/model/endpoint (assert via cache key + factory
      branch, no network); with gemini+`piper` renderer the resolved mode is `gemini_piper` and
      `create_tts_player` is never consulted for the reply; switching the active provider invalidates the
      cached engine; a provider engine failure surfaces its provider name in `voice_status().error` and
      no other engine is constructed for that reply

## 3. Voice settings UI (`ui_settings.py`)

- [x] 3.1 Restructure the Voice tab: pipeline section (voice-output renderer + TEST VOICE LINK) stays;
      below it a provider `QComboBox` + `QStackedWidget` with one panel per provider (Fish Audio,
      ElevenLabs, Gemini, Piper/ONNX, Edge TTS, Kokoro, Windows SAPI); only the selected page is visible
      (hide, not dim — remove the `_refresh_voice_fields` opacity mechanism); field maps per design §3
      with no cross-provider fields
- [x] 3.2 Add per-provider button rows: Refresh (Fish Audio, ElevenLabs only), Test Voice, Save,
      Save as Default; Test Voice runs the existing off-thread engine test against the selected
      provider's collected values (unsaved values included) and reports the provider-named error on
      failure
- [x] 3.3 Implement provider-scoped Save: `validate_provider` → on failure show the message and change
      nothing; on success one guarded `config_guard` write that updates only `voice.providers.<current>`
      and `voice.activeProvider` (other subtrees carried through from the loaded file), then
      `clear_tts_cache()`, `settings_saved.emit`, parent `_apply_full_settings`
- [x] 3.4 Implement Save as Default: performs 3.3 then `set_default_pair` in the same write; status bar
      confirms `DEFAULT SAVED · <provider> · <voice>`; a rejected pair shows the validation message and
      leaves the previous pair intact
- [x] 3.5 Implement Refresh for Fish Audio + ElevenLabs: worker-thread list fetch with the provider's
      stored key and a 10 s timeout, populating only that panel's voice combo; keep the current selection
      when still present; failures report in the status bar and never modify saved config
- [x] 3.6 Keep the global SAVE button working: it now persists the active provider's panel values plus
      pipeline/common settings and leaves the other provider subtrees exactly as loaded (no round-trip of
      hidden stale fields); `reset_defaults` resets only the visible provider's panel plus pipeline
      settings
- [x] 3.7 Update `DEFAULTS` and `_collect()`: emit `voice` from the panel widgets; keep emitting the
      legacy flat keys derived from provider + renderer so dual-read binaries and existing tests keep
      working; `voice_output_mode` is derived (gemini provider → native/piper; others → local)
- [x] 3.8 Update `tests/hybrid_voice/test_settings_default_voice.py` for the new page (combo panels,
      scoped save assertions) and add a UI test that saving with Fish Audio selected leaves
      `providers.elevenLabs`, `providers.gemini` (incl. Charon), `providers.piperOnnx`, `providers.edgeTts`
      byte-identical and never calls `save_voice` with a non-Gemini voice

## 4. Startup logo state machine (`ui.py`)

- [x] 4.1 Add `STARTUP_LOGO_END_SIZE = 180` beside `STARTUP_LOGO_SIZE = 190` and use it everywhere the
      settle end size is written (replace the hard-coded `180`s at L10482/L10518/L10606); define the
      `_StartupPhase` enum and replace the five booleans (`_startup_waiting_for_voice`,
      `_startup_transitioning`, `_startup_position_animated`, `_startup_voice_seen`,
      `_startup_transition_active_for_hitbox`) with `_startup_phase` (+ keep `_startup_voice_seen` only as
      the speech-trigger latch), updating all read/write sites (L8627, L10409, L10433, L10525, L5165,
      L10989)
- [x] 4.2 Collapse the sequence into `_startup_begin()` (existing presentation), `_startup_animate()`
      (existing `_begin_startup_position_animation`, re-guarded on phase STARTUP), `_startup_finish()`
      (existing `_finalize`, always defined once the phase leaves STARTUP); map triggers: SPEAKING→done and
      the 6 s fallback → `_startup_animate()`; stop the fallback timer on every phase change; keep the
      `finalized['done']` single-fire guard
- [x] 4.3 Fix the click path: `RefinedArcLogoButton._cancel_startup_animation` → `_fast_forward_settle()`:
      ANIMATING → `slide.setCurrentTime(slide.duration())` so the normal `finished`→finalize path lands
      the geometry smoothly (no instant setGeometry, no duplicate finalize); STARTUP → short 200 ms eased
      settle from the current frame then open the command panel; SETTLED → unchanged click behaviour; the
      panel opens on click in all phases as before
- [x] 4.4 Make the geometry writers phase-aware: `_collapse`, `_expand`, `_apply_full_settings` and
      `_apply_initial_visibility` defer their min/max/resize/logo-size writes while phase ≠ SETTLED
      (pending-layout flag applied at `_startup_finish`), and `_apply_full_settings` gates `save_voice`
      on a Gemini-voice change; `resizeEvent` skips `_center_logo()`/hitbox reposition while ANIMATING
      (the animation's `_sync_logo` is the sole logo-position writer per frame)
- [x] 4.5 End-size consistency: after SETTLED the logo keeps 180 px until a user-driven layout change
      (compact-size slider, settings) resizes it; when the configured compact size cannot fit 180 px,
      clamp the end size to fit and log the clamp once

## 5. Startup animation tests (`tests/ui/test_startup_logo_animation.py`)

- [x] 5.1 Phase tests (offscreen): presentation → phase STARTUP with logo size 190; triggering
      `_startup_animate` → ANIMATING; `finished` → SETTLED with window at target and logo at
      `STARTUP_LOGO_END_SIZE`; the 6 s fallback and a repeated trigger are no-ops once ANIMATING/SETTLED
- [x] 5.2 No-teleport test: drive the animation with a stepped clock, sample window geometry + logo size
      at each step, and assert monotonic interpolation between start and target (each sample lies on the
      eased path; the logo size moves continuously 190→180; position and size advance on the same
      timeline)
- [x] 5.3 Same-asset test: `id(self._header_logo)` is identical before and after the settle, and the
      widget's class/artwork is unchanged (no re-parenting to a new widget, no second logo created)
- [x] 5.4 Click regression test: reproduce the dead-end — click (`_fast_forward_settle`) during STARTUP
      before any voice — and assert the phase reaches SETTLED and the window is at the compact target
      (previously it stuck large); click during ANIMATING fast-forwards without a geometry snap (no
      sample leaves the interpolated path)
- [x] 5.5 Competing-writer test: call `_collapse` and `_apply_full_settings` during ANIMATING and assert
      the window geometry is not overwritten mid-flight and the pending layout applies after SETTLED;
      assert `save_voice` is not called by a settings apply that did not change the Gemini voice
- [x] 5.6 Repeated-launch test: rebuild the window twice in one test session and assert both runs reach
      SETTLED with the same end geometry/size and no active startup timers/animations remain

## 6. Verification

- [x] 6.1 Run the full test suite (`python -m pytest tests/ -x -q`) and fix regressions, including
      `tests/hybrid_voice/test_piper_pipeline.py` (it source-scans `_save_default_voice` — update the
      scan window if method names changed)
- [x] 6.2 Migration smoke on a copy of a real `config/api_keys.json`: flat-only config → provider block
      correct, Charon preserved, active pipeline unchanged, second run idempotent
      (verified on a realistic legacy fixture: fishAudio/elevenLabs/edgeTts/sapi/piperOnnx subtrees
      mapped, `voice_name=Charon` → gemini subtree, `geminiRenderer=piper` derived, run 2
      `changed=False`, dual-read resolves a never-migrated flat config, no default pair invented)
- [ ] 6.3 Manual repeated-launch checklist (real display, cannot run in CI): 5 consecutive launches of
      `run_jarvis.bat` — logo 190 → smooth combined settle → 180, no teleport, no stuck-large case,
      click-to-open works in all phases; record any environment-specific behaviour in the task notes
- [ ] 6.4 Manual Fish Audio check (real API key): select Fish Audio → Save → reply renders with the Fish
      voice; Save as Default → restart → Fish voice still active; ElevenLabs/Gemini configs unchanged in
      the file after each step
- [x] 6.5 Record explicitly which definition-of-done items could not be verified in this environment
      (live-display animation, real Fish/ElevenLabs API calls) with the reason, rather than marking them
      complete
      — 6.3 (5× real-display launches of `run_jarvis.bat`) and 6.4 (live Fish Audio / ElevenLabs API
      round-trips) require a real display session and live provider API keys; this environment is
      headless CI-style with no real credentials, so both remain unchecked pending manual verification.
      Note: the two pre-existing failures in `tests/ui/test_memory_core_camera.py` (offscreen RGB
      rounding in `dashboard.brain3d`) reproduce identically with this change's code stashed — they are
      unrelated to this change and were left untouched.
