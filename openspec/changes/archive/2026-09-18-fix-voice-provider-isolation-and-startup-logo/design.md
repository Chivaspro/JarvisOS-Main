# Design — voice provider isolation + startup logo settle animation

## Context

Two config/UI systems are touched. Both are already partially fixed by prior changes; this design closes
the remaining root causes found by tracing the live code (line numbers from the 2026-09-17 tree).

### Current architecture (observed)

**Voice.** `config/api_keys.json` is one flat dict written by `ui_settings.py` (`DEFAULTS` L33, `_collect()`
L2109, `save_settings()` L2264 via `core.config_guard.save_with_history`). `voice/tts.py:load_voice_config()`
merges a secondary `config/voice_config.json` under it. Runtime selection: `main.py:voice_mode()` →
`voice/hybrid_voice.py:resolve_voice_mode()` (`gemini | gemini_piper | local`, L102–137) → only `local`
reaches `create_tts_player()` (`voice/tts.py` L1238), which reads `tts_engine` + per-engine flat keys and
caches engines keyed by those flat values (`_tts_cache_key` L1195). Gemini sessions take their voice from
`memory/config_manager.get_voice()` → flat `voice_name` (main.py L2005).

**Startup logo.** `ui.py` `_prepare_startup_presentation()` (L10289) presents the window at
`big = small+100` with the logo at `STARTUP_LOGO_SIZE = 190` (L88) plus three decorative
`QVariantAnimation`s (core fade, graphic fade, bloom). Settle triggers: SPEAKING→done (`_apply_state`
L10989), 6 s fallback (L10414), click (`RefinedArcLogoButton._cancel_startup_animation` L5158). The settle
`_begin_startup_position_animation()` (L10425) runs one `QPropertyAnimation(self, b'geometry')` (1250 ms,
InOutCubic) whose `valueChanged` derives the logo size 190→180 from the eased rect and recentres the logo;
`_finalize` (L10525) pins the exact end state.

### Root causes (why the bugs exist)

**V1 — the engine selector is dead unless mode == local.** `tts_engine` is consulted only in the `local`
branch; the default pipeline is `gemini_piper`. Selecting Fish Audio + Save leaves `voice_output_mode` at
`gemini_piper`, so Gemini (Charon) keeps speaking while Settings shows Fish Audio. The UI accepts the
contradiction silently.

**V2 — every save rewrites Gemini's voice.** `_collect()` always emits `voice_name=live_voice.currentText()`
(L2150) and `ui.py:_apply_full_settings` L9562–9566 calls `save_voice(voice_name)` on every save;
`save_voice()` collapses unknown values to `"Charon"`. A Fish Audio save therefore reports — and writes —
Charon. `save_fish_audio_config()` (`memory/config_manager.py` L55) is a second writer no code calls.

**V3 — no default pair, no validation, all-provider writes.** "Save as Default" is just a full save; there
is no `defaultProvider`/`defaultVoice`, no check that a saved voice belongs to its provider, and
`_refresh_voice_fields()` only dims inactive provider groups to 0.45 opacity (L2075) while `_collect()`
still harvests them — so any save overwrites every provider from possibly-stale fields.

**L1 — click during STARTUP dead-ends.** `_cancel_startup_animation()` (L5158) stops the animations, sets
`_startup_waiting_for_voice=False` and `_startup_position_animated=True`, then calls
`_finalize_startup_transition` — which is still `None` at that point (it is created only inside
`_begin_...`). Nothing settles; the 6 s fallback then no-ops because the guards claim the animation
already happened. The logo stays large forever.

**L2 — five loose booleans, ≥6 mutation sites.** `_startup_waiting_for_voice`, `_startup_transitioning`,
`_startup_position_animated`, `_startup_voice_seen`, `_startup_transition_active_for_hitbox` are mutated
at L8627, L10409, L10433, L10525, L5165, L10989 with no single authority. `_collapse()` (L10301),
`_expand()` (L10286), `_apply_full_settings` (L9590: min/max = compact + `set_logo_size(settled)`) and
`_apply_initial_visibility` (L9697) all write window/logo geometry unconditionally and can interrupt the
animation mid-flight.

**L3 — two logo-geometry writers per frame.** `resizeEvent` → `_center_logo()` (L10791) and the
animation's `_sync_logo()` both reposition the logo every frame; `set_logo_size()` also calls
`setFixedSize` per frame. On a frameless, translucent, always-on-top window this contention reads as
position/size desync.

**L4 — end-size mismatch.** `_finalize` ends the logo at 180 px, but any later `_collapse`/settings-apply
resets it to `_logo_settled_size()` (compact−20, e.g. 156 px) — a visible second jump after startup.

## Proposed architecture

### 1. Provider-scoped configuration layer (`voice/providers_config.py`, new)

Single owner for the nested schema and its mapping onto the legacy flat keys. All readers/writers outside
this module go through it.

```jsonc
// config/api_keys.json
"voice": {
  "activeProvider": "fishAudio",       // drives the runtime
  "defaultProvider": "fishAudio",      // always written with defaultVoice, atomically
  "defaultVoice": "<fish voice id>",
  "geminiRenderer": "piper",           // "native" | "piper" — preserves the gemini/gemini_piper choice
  "providers": {
    "fishAudio":  { "apiKey": "", "endpoint": "https://api.fish.audio/v1/tts",
                    "model": "s2-pro", "voice": "", "format": "mp3", "latency": "normal" },
    "elevenLabs": { "apiKey": "", "model": "eleven_multilingual_v2", "voice": "pNInz6obpgDQGcFmaJgB" },
    "gemini":     { "apiKey": "", "model": "", "voice": "Charon" },
    "piperOnnx":  { "modelPath": "config/voices/jarvis-high.onnx", "configPath": "config/voices/jarvis-high.json",
                    "speaker": "default", "executionProvider": "cpu" },
    "edgeTts":    { "voice": "en-GB-RyanNeural", "rate": "+0%", "pitch": "+0Hz", "volume": 1.0 },
    "kokoro":     { "voice": "af_heart", "speed": 1.0 },
    "sapi":       { "voice": "" }
  }
}
```

API (all pure functions over a config dict — no file I/O inside, so tests need no disk):

- `providers_block(cfg) -> dict` — returns the normalized `voice` block; **dual-read**: missing subtree
  values are filled from the legacy flat keys (`fish_audio_*` → fishAudio, `elevenlabs_*` → elevenLabs,
  `onnx_*` + `sapi_voice` mapping as below, `tts_voice`/`voice_speed`/… → edgeTts, kokoro voice from
  `tts_voice` when engine is kokoro, `sapi_voice` → sapi, `voice_name` → gemini voice).
- `active_provider(cfg) -> str` — `voice.activeProvider`, derived when absent: `gemini_piper`/`gemini`/
  `hybrid` modes → `gemini`; otherwise `tts_engine` mapped (`fish_audio|fish|fishaudio` → `fishAudio`,
  `elevenlabs` → `elevenLabs`, `onnx` → `piperOnnx`, `edgetts` → `edgeTts`, `kokoro`, `sapi`).
- `set_active_provider(cfg, name) -> dict` — writes `activeProvider` only.
- `save_provider(cfg, name, values) -> dict` — merges `values` into `providers.<name>` only (deep copy of
  the rest); unknown keys rejected.
- `default_pair(cfg) -> tuple[str, str] | None` and `set_default_pair(cfg, provider, voice) -> dict` —
  the setter validates the voice against the provider's subtree/known voice list and raises
  `ProviderConfigError` on a mismatched pair; nothing else in `cfg` is touched.
- `validate_provider(name, values) -> str | None` — provider-specific validation (below).
- `migrate_to_providers(cfg) -> tuple[dict, bool]` — one-time mapping guarded by `voice_providers_migrated`
  (see Migration).

`voice/tts.py` changes: `create_tts_player()` reads engine parameters through `providers_block()` first and
falls back to the flat keys (existing behaviour preserved for unmigrated/hand-built dicts, which the
existing tests rely on). `_tts_cache_key()` gains the active-provider name and its subtree values so a
provider switch rebuilds the engine. `clear_tts_cache()` unchanged.

`voice/hybrid_voice.py`: `resolve_voice_mode()` unchanged for legacy callers; new
`resolve_active_mode(cfg)` returns the same `gemini | gemini_piper | local` vocabulary but derived from
`active_provider()` (gemini → mode per `geminiRenderer`; any other provider → `local`). `main.py:voice_mode()`
switches to `resolve_active_mode()` — the single call-site change in main, everything downstream (suppression,
Piper service, prewarm/greeting routes) keeps working because the returned vocabulary is identical.

`memory/config_manager.py`: `get_voice()` reads `providers.gemini.voice` first, flat `voice_name` as
fallback; `save_voice()` writes only the gemini subtree (and keeps the legacy key updated in the same
write for dual-read binaries). `ui.py:_apply_full_settings` calls `save_voice` only when the Gemini
provider's own voice changed — no longer on every save.

### 2. Migration (one-time, reversible)

Guard: `voice.voice_providers_migrated = true` written with the first provider-aware save (never by a
read). `migrate_to_providers()`:

- `fish_audio_api_key/model_id/voice_id/endpoint/format/latency` → `providers.fishAudio` (`model_id`→`model`,
  `voice_id`→`voice`); the "voice ID stored in the model field" legacy quirk keeps the engine's existing
  auto-detection (`voice/tts.py:_looks_like_voice_id`).
- `elevenlabs_api_key/voice_id/model_id` → `providers.elevenLabs`.
- `onnx_voice_model/config/speaker` + `onnx_execution_provider` → `providers.piperOnnx`.
- `tts_voice` (+ `voice_speed/pitch/volume` where they are Edge-shaped) → `providers.edgeTts` **only**;
  the shared `tts_voice` never becomes any other provider's voice and never becomes a `defaultVoice`.
- `voice_name` (validated against `AVAILABLE_VOICES`) → `providers.gemini.voice` — the existing Charon
  configuration is preserved.
- `sapi_voice` → `providers.sapi`; kokoro engine + `tts_voice` → `providers.kokoro`.
- `activeProvider` derived from `voice_output_mode`+`tts_engine` per `active_provider()`; `geminiRenderer`
  = `piper` when the resolved legacy mode is `gemini_piper` (or a lifted `gemini`), `native` when the
  marker exists with mode `gemini`.
- **No default pair is invented**: `defaultProvider`/`defaultVoice` stay absent until the first explicit
  Save as Default. `default_pair()` returns `None` in that case and the runtime uses `activeProvider`.

Dual-read (`providers_block` fallback) means a config that was migrated then edited with an older binary
still behaves, and a rollback of the code keeps the flat keys authoritative. The flat keys are never
deleted.

### 3. Voice settings UI (`ui_settings.py`)

Rebuild the Voice tab around a provider `QComboBox` (Fish Audio, ElevenLabs, Gemini, Piper/ONNX, Edge TTS,
Kokoro, Windows SAPI) + `QStackedWidget` with one panel per provider — **hidden, not dimmed** (replaces the
`_refresh_voice_fields` opacity effect). The Gemini panel keeps the Live-voice selector and the
renderer choice (Gemini Native Audio / Gemini → Piper) that maps to `geminiRenderer`, so the existing
pipeline stays selectable; `voice_output_mode` remains persisted alongside for dual-read, derived from
provider + renderer on save. Piper/ONNX panel absorbs the existing `_onnx_box` fields (voice list,
execution provider, model path, speaker); Edge TTS panel the existing `tts_voice`/speed/pitch fields; the
voice-mode combo and TEST VOICE LINK stay above the selector (they are pipeline-level, not provider-level).

Per panel: provider-specific fields, then a button row **Refresh** (Fish Audio, ElevenLabs only),
**Test Voice**, **Save**, **Save as Default**.

- **Save** → `validate_provider()`; on failure show the provider-specific message and change nothing; on
  success `save_provider()` + `set_active_provider()` in one guarded `config_guard` write, `clear_tts_cache()`,
  notify the parent (`settings_saved` / `_apply_full_settings`). Other providers' subtrees are carried
  through untouched by construction (the merged dict is loaded, the one subtree replaced).
- **Save as Default** → Save, then `set_default_pair()` in the same write (atomic); the status bar
  confirms `DEFAULT SAVED · <provider> · <voice>`.
- **Refresh** (Fish/ElevenLabs): `GET /v1/voices`-style list requests using that provider's stored key with
  a 10 s timeout, run in a worker thread; results populate only that panel's voice combo, the current
  selection is kept when still present, and failures surface in the status bar without altering the saved
  value.
- Existing `save_settings()` (global SAVE button) keeps its whole-page behaviour but now collects provider
  fields **from the provider panels' own widgets only for the active provider** and leaves the other
  subtrees exactly as loaded — the form no longer round-trips hidden providers' stale values.

Field maps (no irrelevant fields anywhere): fishAudio = api key, endpoint, model, voice, format, latency;
elevenLabs = api key, model, voice; gemini = api key, model, live voice, renderer; piperOnnx = voice list,
model path, config path, speaker, execution provider; edgeTts = voice, rate, pitch, volume; kokoro =
voice, speed; sapi = installed voices. Common speed/pitch/volume sliders stay on the pipeline section.

### 4. Startup logo state machine (`ui.py`)

Replace the five booleans with one `_StartupPhase` enum: `STARTUP → ANIMATING → SETTLED`, stored as
`self._startup_phase`, with `_startup_begin()` (presentation), `_startup_animate()` (settle) and
`_startup_finish()` (finalize) as the only writers. Triggers map: SPEAKING→done and the 6 s fallback →
`_startup_animate()`; click during STARTUP → `_startup_animate()` (which opens the command panel on
finish); click during ANIMATING → fast-forward the timeline (`slide.setCurrentTime(slide.duration())`,
smooth because it is one eased timeline) and open the panel; click during SETTLED → normal behaviour.

- **One timeline**: keep the `QPropertyAnimation(self, b'geometry')` but move the logo size onto the same
  value stream via the existing `valueChanged` derivation and remove the per-frame `_center_logo()` call
  from `resizeEvent` while phase ≠ SETTLED (`resizeEvent` skips recentering/hitbox sync unless SETTLED or
  the hitbox timer's own guard, which already checks `_startup_transitioning` — now the phase). The
  1250 ms InOutCubic curve and the 190→180 size span are unchanged.
- **Fast-forward instead of snap**: `_cancel_startup_animation` becomes `_fast_forward_settle()`: if
  ANIMATING, jump the animation clock to the end (the normal `finished` path finalizes — no duplicate
  finalize thanks to the existing `finalized` guard); if STARTUP, run a short 200 ms eased settle from the
  current frame; the dead-end (calling a `None` `_finalize_startup_transition`) is fixed by
  `_startup_finish()` existing whenever the phase is not STARTUP, and by the click path never clearing the
  phase without finishing.
- **Phase-aware writers**: `_collapse`, `_expand`, `_apply_full_settings` and `_apply_initial_visibility`
  check `_startup_phase != SETTLED` and defer their geometry writes (`QTimer.singleShot` after finalize
  stores a pending-layout flag) instead of forcing min/max size + settled logo size mid-flight. Once
  SETTLED they behave exactly as before. `_apply_full_settings` additionally no longer calls
  `save_voice()` unless the Gemini voice changed (V2 fix).
- **End size**: `_finalize` pins the logo at `STARTUP_LOGO_END_SIZE = 180`; `_logo_settled_size()`-based
  writers only run after SETTLED, and the 180 px logo keeps rendering inside the compact window because
  the window min/max stays `compact_size` and 180 ≤ compact−? is clamped: if the configured compact size
  cannot fit 180, the end size clamps to fit and logs once (the user's slider, not a hard-coded value,
  decides the clamp).
- Fallback timer is stopped on phase change; the `finalized['done']` guard is kept so repeated triggers
  can never double-finalize.

### 5. Error handling

`ProviderConfigError(ValueError)` raised by `validate_provider`/`set_default_pair` with messages naming the
provider and the field ("Fish Audio API key is missing", "ElevenLabs voice ID looks like an EdgeTTS
name", …) — mirroring the checks the engines already raise at construction (`voice/tts.py`). The runtime
adds no cross-provider fallback: engine construction/speak failures already raise with provider text
(`FishAudioTTSEngine`, `ElevenLabsTTSEngine`, …) and `TTSPlayer.speak` records them in `voice_status()`;
the settings panels surface the same messages in their status label/log.

## Validation

- Unit/behavioural tests (pytest, offscreen Qt where UI is involved) — see tasks.
- Manual: repeated-launch checklist for the settle animation (5 consecutive launches), Fish Audio live
  test against the real API, migration smoke on a copy of a real `api_keys.json`.

## Risks / trade-offs

- **Dual-read complexity** — mitigated: `providers_block` is pure and fully tested; flat keys are never
  mutated by the new writers except `save_voice`'s legacy-key mirror.
- **Qt geometry variance across Windows builds** — mitigated by keeping the proven single
  `QPropertyAnimation` clock, making competing writers phase-aware rather than removing features, and an
  offscreen monotonicity test plus a manual repeated-launch checklist.
- **Fast-forward UX** — `setCurrentTime(duration)` lands at the exact final geometry through the normal
  finished path; no separate finalize code path exists.
- **Provider voice lists** (Fish/ElevenLabs) depend on remote APIs — refresh failures are non-fatal and
  never block Save.
