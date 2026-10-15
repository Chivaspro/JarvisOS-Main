## Why

JARVIS has two half-built voice paths and they contradict each other. `JarvisLive` in `main.py` owns the realtime Gemini Live session but only recognises `voice_output_mode == "local"`, while `ui_settings._save_default_voice()` hard-codes `voice_output_mode = "local"` when the user clicks **SAVE AS DEFAULT VOICE** — so the single button that is supposed to persist a voice choice silently switches JARVIS into standalone local mode and stops using Gemini Live audio. The legacy `"hybrid"` value additionally starts `HybridVoiceSystem`, which opens a *second* `sd.InputStream` (a duplicate microphone) and never renders anything, because nothing routes Gemini transcript text into the ONNX voice. The user wants one clear pipeline: Gemini Live for conversation, the local `jarvis-high.onnx` Piper voice for the audible reply, and no native Gemini audio in that mode.

## What Changes

- **New primary pipeline** (`voice_output_mode = "gemini_piper"`, the new default for this feature): microphone → Gemini 2.5 Flash Live → Gemini response text → Piper ONNX (`config/voices/jarvis-high.onnx`) → speaker. Gemini's native 24 kHz audio is drained and discarded, never written to the output stream, so Gemini audio and Piper audio can never overlap.
- **Voice output modes** become exactly `gemini` | `gemini_piper` | `local`, with a single normaliser/migration helper. Legacy `"hybrid"` migrates to `gemini_piper`; a missing key defaults to `gemini_piper`; an existing explicit `"gemini"` is lifted to `gemini_piper` exactly once (marked in config) so the new pipeline is active out of the box while a later deliberate choice of Gemini Native Audio persists.
- **Fix SAVE AS DEFAULT VOICE**: it now persists the mode currently selected in the UI (Genius Live + Piper ONNX + chosen Piper voice + CPU/GPU/Auto) instead of forcing `local`.
- **Reusable Piper worker/queue**: one bounded `queue.Queue` and one long-lived synthesis thread owned by `core/hybrid_voice.py`. No thread per sentence or per reply. Gemini streaming `output_transcription` deltas are buffered into sentence/phrase chunks before synthesis.
- **CPU / GPU / Auto execution provider selector** for Piper via ONNX Runtime (`onnx_execution_provider`). CPU always works; a GPU request that cannot be honoured logs the reason and falls back to CPU instead of raising.
- **Interruption**: user speech / ESC / wake-word barge-in stops Piper playback, clears the queued speech and resumes listening; stale queued responses never resume.
- **Startup warm-up**: Piper preloads in a background thread (non-blocking, model reused), with diagnostics such as `[PIPER] Model loaded: jarvis-high.onnx` and `[PIPER] Execution provider: CPUExecutionProvider`.
- **Settings UI** gains Voice Output (Gemini Native Audio / Gemini → Piper ONNX / Local Piper Only), Piper Voice, Execution (CPU/GPU/Auto), **TEST PIPER VOICE** and the repaired **SAVE AS DEFAULT VOICE**, in the existing HUD styling. No unrelated UI is redesigned.
- **Backward compatible configuration**: existing keys (`tts_engine`, `tts_voice`, ElevenLabs/Fish/SAPI/ONNX settings, features, everything else) are preserved; only new keys (`voice_output_mode`, `onnx_execution_provider`, `voice_pipeline_migrated`) are added.
- **Duplicate-resource cleanup (non-breaking)**: `JarvisLive` stops starting the legacy `HybridVoiceSystem` microphone; one mic stream, one Gemini Live session and one playback path per mode remain.

## Capabilities

### New Capabilities

- `voice-output-routing`: the three voice output modes, their normalisation/migration from legacy values, the one-time `gemini` → `gemini_piper` lift, Gemini native audio suppression in Piper mode, what SAVE AS DEFAULT VOICE persists, and the UI status/diagnostics surface for the active mode.
- `gemini-piper-pipeline`: Gemini transcript text becoming the sole Piper input — streaming sentence/phrase buffering, the single bounded Piper queue and long-lived worker, off-thread synthesis and playback, prewarm, interruption semantics, and reuse of the existing microphone bus and Gemini Live session.
- `piper-onnx-execution`: Piper ONNX model reuse (no per-response reload), CPU/GPU/Auto execution provider selection, guaranteed CPU operation with logged fallback, and `[PIPER]` diagnostics plus status reporting.

### Modified Capabilities

- None. `openspec/specs/` is empty (`openspec list --specs` returns no capabilities), so all three capabilities above are new; the archived `2026-09-15-hybrid-voice-system` change was never synced into `openspec/specs/` and its requirements are superseded by `voice-output-routing` rather than deleted.

## Impact

- **Modified files**: `core/tts.py` (execution-provider resolution, session creation, chunked synthesis, `[PIPER]` diagnostics, cache keying), `core/hybrid_voice.py` (mode enum + normaliser/migration, `PiperSpeechService` queue/worker/chunker), `main.py` (`JarvisLive`: mode resolution, Gemini audio suppression, transcript → Piper streaming, interrupt, prewarm, mode change), `ui_settings.py` (Voice tab controls, SAVE AS DEFAULT VOICE fix), `ui.py` (Piper state aliases for the HUD, applied-settings log line).
- **Untouched by design**: `config/api_keys.json` is not rewritten by this change — migration is applied in memory and persisted only when the user saves settings in the UI. Existing engines (EdgeTTS, Kokoro, ElevenLabs, Fish Audio, SAPI, ONNX), STT engines, the wake-word detector and all other Settings tabs keep working.
- **Dependencies**: none added. Uses the already-required `piper-tts` 1.8, `onnxruntime` 1.30, `sounddevice`, `numpy`. No GPU package, driver or CUDA install is required.
- **Risk**: medium — the voice path is user-visible. Mitigations: `gemini` and `local` modes keep their exact previous behaviour, every Piper failure degrades to "text on screen, no crash", the Piper path owns no microphone and creates no Gemini session, and a failed Piper warm-up leaves Gemini native audio untouched as a fallback.
