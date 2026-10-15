## Context

See proposal.md — Why. The current state that constrains the design, verified against the repository rather than assumed:

- `main.py :: JarvisLive` already owns the single realtime path: `_listen_audio()` (one `sd.InputStream`, PCM → `out_queue`, wake-word feed, HUD level), `_send_realtime()` (`out_queue` → `session.send_realtime_input`), `_receive_audio()` (`response.data` → `audio_in_queue`; `server_content.output_transcription` → `out_buf`; `turn_complete` → logs, Brain hooks, dashboard broadcast) and `_play_audio()` (one `sd.RawOutputStream` writing `audio_in_queue`).
- Mode handling is inconsistent: `_receive_audio` (line ~2745), `_play_audio` (line ~2846) and `_send_startup_briefing`/`prewarm_voice` (lines ~2953/1863) each re-read the config and compare `voice_output_mode == "local"`. `"hybrid"` is only recognised in `JarvisLive.__init__`, where it starts `HybridVoiceSystem` — which opens its own `MicBus` `InputStream` (a second microphone) and routes no text to any voice.
- `ui_settings._save_default_voice()` sets `data["voice_output_mode"] = "local"` and moves the combo to `local`, which is the reported bug.
- `core/tts.py :: OnnxTTSEngine` loads `PiperVoice` once per instance inside `_load()` (lock-guarded, status `LOADING` → `READY`), synthesises chunk-by-chunk via `self._voice.synthesize(text, syn_config=cfg)` and plays each chunk with `_play_np` (i.e. `sd.play` + `sd.wait`). `create_tts_player()` caches players in `_TTS_CACHE` keyed by engine inputs, so the 114 MB `jarvis-high.onnx` is loaded once per arrangement and not per sentence.
- `piper-tts` 1.8.0 `PiperVoice.load(model_path, config_path, use_cuda=False, ...)` builds the ORT session itself as `onnxruntime.InferenceSession(path, sess_options=..., providers=[...])`, accepting only `CUDAExecutionProvider` or `CPUExecutionProvider`; `PiperVoice` is a dataclass of `(session, config, espeak_data_dir, download_dir, ...)` and `PiperConfig.from_dict()` parses the same sidecar JSON that `PiperVoice.load` reads. `AudioChunk.audio_int16_bytes` is what the current engine plays.
- Modes must be chosen without touching the user's live `config/api_keys.json` on disk; the user's file stores `voice_output_mode = "gemini"` explicitly.

## Goals / Non-Goals

**Goals:**
- One audible renderer at a time, selected by one shared mode resolver instead of four inline string comparisons.
- Piper synthesis driven exclusively by Gemini's response transcript, streamed sentence-by-sentence, off the Qt thread and off the asyncio event loop.
- Model loaded once, execution provider selectable, CPU always usable, GPU failure always survivable.
- Interruption that stops playback *and* empties the queue.
- Existing engine list, `local` mode, `gemini` mode, per-reply logging, Brain hooks and HUD visuals untouched.

**Non-Goals:**
- No new microphone, Gemini session, playback device or audio thread beyond the single Piper worker.
- No changes to `core/voice.py`'s delivery-profile API, `core/stt.py`, `core/wake.py` or the wake-word detector.
- No word-level alignment/viseme work, no streaming raw PCM out of Piper, no resampling architecture change.
- No deletion of the legacy `HybridVoiceSystem` class (its tests and imports stay valid); it is simply no longer started by `JarvisLive`.
- No rewriting of `config/api_keys.json` as part of this change.

## Decisions

### Decision 1: `PiperSpeechService` lives in `core/hybrid_voice.py`

**Rationale:** That module is already the project's voice coordinator home and already owns the ONNX cache (`OnnxCachedEngine`) and a singleton pattern (`get()` / `reset()` used by `main.py` shutdown). Adding the queue/worker there reuses both instead of introducing a fourth voice module, and keeps `main.py` to a handful of call sites.

**Alternatives considered:** a new `core/piper.py` (a fourth module to wire, no extra capability), or putting the queue inside `JarvisLive` (couples a long-lived worker to a reconnectable session object, and the queue would be recreated on every reconnect — exactly the "new worker per reply" failure mode being fixed).

### Decision 2: The worker talks to the existing cached `TTSPlayer`, with a cancellation event

**Rationale:** `create_tts_player(cfg)` is already the cache that prevents model reloads, and `TTSPlayer.speak()` already serialises playback. The service resolves the player **in the worker thread** (so the 7 s model load never touches the Qt or asyncio thread) and re-resolves it after a settings change invalidates `_TTS_CACHE`. Interruption needs to stop audio *mid-chunk*: `OnnxTTSEngine.speak()`/`synthesize()` gain an optional `cancel` `threading.Event` that is checked before each synthesized chunk, so `sd.stop()` plus the flag aborts the current sentence as well as the queue.

**Alternatives considered:** driving `PiperVoice.synthesize` directly in the service (duplicates the inference/parameter logic that `OnnxTTSEngine` already contains, and bypasses the cache), or relying only on `sd.stop()` (the engine loop would continue with the next chunk of the interrupted sentence).

### Decision 3: Sentence chunking with a cumulative-transcript guard

**Rationale:** Gemini streams `output_transcription` fragments. They are appended to a per-turn "fed" string and only the new suffix is submitted, which makes incremental *and* cumulative transcript shapes safe. The suffix is then cut at sentence terminators (`.!?…`, newline) once a chunk reaches a minimum length, with a comma/space split as a hard cap so a long unpunctuated sentence still starts speaking early. `flush()` emits the remainder at `turn_complete`.

**Alternatives considered:** speaking only on `turn_complete` (simple, but adds full-reply latency and ignores requirement 6), or one chunk per transcript fragment (choppy prosody and one synthesis call per fragment).

### Decision 4: Bounded queue, drop-and-log overflow

**Rationale:** `queue.Queue(maxsize=N)` with `put_nowait` and a dropped-chunk warning matches the project's existing bounded-queue convention (`out_queue` maxsize 200, Kokoro audio queue maxsize 4) and cannot stall the Gemini receive loop. Blocking on `put()` would push back into `_receive_audio`; unbounded growth would delay speech behind a stale backlog.

**Alternatives considered:** blocking put with timeout (stalls audio/text processing), or a thread per chunk (explicitly forbidden).

### Decision 5: Build the ORT session explicitly for execution-provider control

**Rationale:** `PiperVoice.load(use_cuda=...)` only offers CUDA-or-CPU, and it does not verify what it actually got. The new path resolves a provider list from `onnxruntime.get_available_providers()` (`gpu` prefers CUDA → TensorRT → DirectML → ROCm → CoreML, `auto` prefers GPU then falls back to CPU, `cpu` is CPU-only), constructs `onnxruntime.InferenceSession(path, sess_options, providers=...)`, then builds `PiperVoice(config=PiperConfig.from_dict(meta), session=session)`. The realised provider list is read back from `session.get_providers()`, which is what gets logged. Session creation through a GPU provider that cannot initialise is retried once with CPU-only providers, and any structural failure falls back to plain `PiperVoice.load(...)` so the previous behaviour survives a `piper-tts` API change.

**Alternatives considered:** monkey-patching `onnxruntime.InferenceSession` during `PiperVoice.load` (global, fragile, and not thread-safe), or only supporting `use_cuda` (no Auto mode, no DirectML, no verifiable fallback).

### Decision 6: Mode resolution in one helper with a one-time migration marker

**Rationale:** `normalize_voice_output_mode()` in `core/hybrid_voice.py` is the single source of truth: `gemini_piper`/`local` pass through, `hybrid` → `gemini_piper`, missing → `gemini_piper`, `gemini` → `gemini_piper` **only when the `voice_pipeline_migrated` marker is absent**, then the marker is recorded. `main.py` and `ui_settings.py` both call it, so no inline `== "gemini"` comparisons remain. Legacy `gemini` values that a user deliberately selects after the migration persist.

**Alternatives considered:** always honouring an explicit `gemini` (the user's stated goal — Gemini→Piper active by default — would silently not happen), or forcing `gemini_piper` for every non-`local` config (Gemini Native Audio could never be persisted, violating backward compatibility).

### Decision 7: Suppression by refusing to open the output stream

**Rationale:** `_play_audio()` currently opens the `RawOutputStream` and only then notices `local` mode, and writes Gemini audio whenever mode is anything else. The mode check moves to the top of the function and covers `{"local", "gemini_piper"}`: Gemini chunks are drained and discarded, and the speaker stream for Gemini audio is never opened. That makes simultaneous Gemini + Piper playback structurally impossible rather than a matter of ordering.

**Alternatives considered:** filtering `response.data` in `_receive_audio` (works, but leaves a second, easy-to-regress suppression point and a queue that grows if playback stops), or muting the stream (still holds the device).

### Decision 8: Settings surface reuses existing controls; SAVE AS DEFAULT just stops overriding

**Rationale:** the Voice tab already has an engine combo, an ONNX voice picker (`list_onnx_voices()` reading `config/voices`), an ONNX preview button and a `voice_output_mode` combo. Adding the three required mode labels, an execution-provider combo, retargeting the preview into `TEST PIPER VOICE` and removing the single line that forces `local` is the minimal change that satisfies the UI requirement without touching the HUD design.

**Alternatives considered:** a new Piper tab (unnecessary UI churn), or leaving the ONNX preview as-is and adding a second test button (two overlapping controls).

## Risks / Trade-offs

- [Piper now owns the audible reply, so a Piper failure means no voice] → Mitigation: every failure is caught and logged, text stays on screen, the worker stays alive, and the user can switch back to Gemini Native Audio in Settings without a restart; the existing Gemini audio path is untouched in `gemini` mode.
- [Streaming chunks can produce prosody that is worse than one-shot synthesis] → Mitigation: minimum chunk length before splitting, sentence terminators preferred over comma splits, and the same `SynthesisConfig` (length/noise/volume) the single-shot path used.
- [Interruption races: `sd.stop()` vs. the worker's next `sd.play`] → Mitigation: an epoch counter is bumped on interrupt; the worker checks the epoch before submitting each chunk and skips anything from a stale epoch, in addition to the per-engine cancel event.
- [GPU selection can be slow or unstable on some drivers] → Mitigation: GPU is opt-in (`cpu` is the default), `auto` never fails a load, and any GPU failure retries on CPU once with the reason logged.
- [Two configuration surfaces (`config/api_keys.json` and `config/voice_config.json`) both feed the voice engines] → Mitigation: no existing key is renamed or removed; the new execution-provider key is read from the flat config first and then from the structured file, matching the existing `config.get(...) or vcfg.get(...)` precedence.
- [Diagnostic noise on every reply] → Mitigation: `[PIPER]` load/provider lines are logged once per arrangement; queue and state transitions are logged at most once per speech start/stop, never per audio frame.

## Migration Plan

1. Land `core/tts.py` changes: execution-provider resolution plus `synthesize()`/`cancel`; existing callers keep working because the default provider is `cpu` and `speak()` is unchanged in behaviour.
2. Land `core/hybrid_voice.py`: mode enum + `normalize_voice_output_mode()` + `PiperSpeechService`. Marked-only addition; `HybridVoiceSystem` stays for its tests.
3. Land `main.py` wiring (mode resolver, gateway submit points, suppression, interrupt, prewarm) and stop `JarvisLive` from starting `HybridVoiceSystem`.
4. Land the Settings/UI changes, including the SAVE AS DEFAULT VOICE fix.
5. Rollback: set `voice_output_mode` to `gemini` in Settings (or store it directly) — the Gemini audio path is unchanged — or revert the commit; no data migration is irreversible because `voice_pipeline_migrated` only records that the one-time lift happened.

## Implementation Notes (as built)

Decisions that surfaced during implementation, recorded so this design matches the code and tests:

1. **The Piper pipeline forces the ONNX engine.** `PiperSpeechService._ensure_player()` builds the cached player from a copy of the config with `tts_engine = "onnx"` (and the bundled `jarvis-high` model/config when none is configured). Without this, selecting `gemini_piper` while the local-engine picker said `edgetts` would have spoken through EdgeTTS and never touched Piper. The local engine choice is not written back, so `local` mode still honours it.
2. **Pre-existing fixed:** the discarding loop used by `local` mode let `asyncio.TimeoutError` escape on the first idle poll (100 ms), which killed the audio task and with it the session. `gemini_piper` shares that loop, so it now catches the timeout and keeps watching — silence between turns is normal, not fatal.
3. **Server-side barge-in also clears Piper.** In addition to the manual/ESC and wake-word interrupt paths, `_receive_audio` reacts to the session's `interrupted` signal by stopping Piper and clearing its queue.
4. **Settings shows the resolved mode, not the raw stored string.** The Voice tab preselects the result of `resolve_voice_mode()`, so the window cannot display "Gemini Native Audio" while the one-time migration is actually running Piper — which the user could otherwise save back over.
5. **Interruption uses an epoch plus a per-engine cancel token**, not just `sd.stop()`: stopping the device alone would have let the ONNX engine continue with the next chunk of the interrupted sentence. `OnnxTTSEngine.speak(cancel=..., on_audio=...)` checks the token between chunks, and `on_audio` is what distinguishes "synthesizing" from "playing" for the status surface.
6. **Status surface:** `PiperSpeechService.status()` exposes phase, queue depth, dropped-chunk count, model name, active execution provider and the GPU-fallback reason (read from the engine's status), so diagnostics do not have to be scraped from the log.

## Open Questions

- Whether Piper should eventually stream raw PCM instead of one `sd.play` per chunk — deferred; it changes only internal playback strategy, not the specified behaviour, and the current chunked playback already starts speaking before the turn ends.
