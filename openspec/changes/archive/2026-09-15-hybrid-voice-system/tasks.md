## 1. Hybrid Voice System Coordinator

- [x] 1.1 Created `core/hybrid_voice.py` with `HybridVoiceSystem`, `VoiceState` enum (IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED, CONNECTING, CONNECTED, RECONNECTING, ONNX_LOADING, ONNX_READY, VOICE_ERROR), and `VoiceMode` enum (HYBRID, GEMINI, LOCAL).
- [x] 1.2 Implemented `MicBus` shared microphone bus: opens exactly one `sd.InputStream`, distributes PCM to registered callbacks (Gemini send queue, wake detector feed, VAD, soundwave level) via the audio thread.
- [x] 1.3 Implemented single Gemini Live session ownership: `HybridVoiceSystem` owns the session; `JarvisLive` continues to own the actual connect/reconnect, but the coordinator tracks state and prevents duplicate sessions.
- [x] 1.4 Implemented `OnnxCachedEngine`: loads the ONNX model exactly once, reuses for all synthesis, guarded by a lock, exposes `warmup_once()`.
- [x] 1.5 Implemented `PlaybackManager` single audio playback path: stops current output before starting new output.
- [x] 1.6 Implemented voice-state machine: validates transitions, exposes current state to the UI, reflects actual runtime state.
- [x] 1.7 Implemented clean shutdown: stops Gemini, mic, playback, ONNX workers, cancels background tasks, releases all resources, joins all threads.

## 2. Configuration Save/Reload

- [x] 2.1 Implemented `validate_voice_settings()` that checks all voice settings, the ONNX model path and sidecar JSON, and the Gemini API key and model name.
- [x] 2.2 Implemented atomic config save: writes to a temp file and renames, so a crash never leaves a corrupt config.
- [x] 2.3 Implemented safe reload: stops old voice resources, cleans up old sessions/tasks, validates new config, starts the new voice mode once, confirms active.
- [x] 2.4 Implemented rollback on failure: keeps the previous working configuration, shows a useful error message, does not crash JARVIS.
- [x] 2.5 Added the "Hybrid — ONNX + Gemini Live" voice option and "SAVE AS DEFAULT" button to the Settings UI.

## 3. Integration with Existing Components

- [x] 3.1 Wired the Hybrid Voice System into `JarvisLive` in `main.py` so the Gemini session and mic are owned by the coordinator.
- [x] 3.2 Updated `core/tts.py` to expose `OnnxTTSEngine.warmup_once()` and a shared ONNX session cache.
- [x] 3.3 Updated `core/voice.py` to add `HybridVoiceMode` enum and `HybridVoiceSystem` integration points.
- [x] 3.4 Updated `ui.py` to expose the real voice-state machine to the UI status bar.
- [x] 3.5 Added `voice_output_mode` ("hybrid" | "gemini" | "onnx" | "local") and `hybrid_config` block to `config/api_keys.json`.

## 4. Performance and Safety

- [x] 4.1 Implemented bounded queues (maxsize=4 for audio input and synthesis) to prevent memory growth.
- [x] 4.2 Implemented bounded thread counts (ONNX: max 4, Gemini: 1, audio: 1) to protect low-end CPUs.
- [x] 4.3 Ran heavy work (ONNX inference, Gemini connection, audio playback) in the correct background/asynchronous worker; did not block the UI or the Gemini loop.
- [x] 4.4 Prevented thread leaks, asyncio task leaks, duplicate callbacks, and duplicate audio events.
- [x] 4.5 Implemented diagnostics logging for voice initialization, ONNX load, Gemini connection, reconnect, session creation/destruction, audio queue size, mic state, playback state, voice mode changes, save configuration, resource cleanup, and errors. Did not log every audio frame.

## 5. Verification

- [x] 5.1 Verified the hybrid voice system starts and all resources are owned exactly once (one mic, one Gemini session, one ONNX engine, one playback path).
- [x] 5.2 Verified the voice-state machine reflects the actual runtime state in the UI.
- [x] 5.3 Verified safe config save/reload: valid settings save and activate; invalid settings are rejected and the previous config is preserved.
- [x] 5.4 Verified graceful reconnect: disconnect does not crash JARVIS, stale tasks are stopped, old audio is discarded, and bounded backoff is used.
- [x] 5.5 Verified interruption: user speaking while JARVIS is speaking stops stale output and processes the new request.
- [x] 5.6 Verified clean shutdown: all resources are released and no zombie processes, threads, or tasks remain.
- [x] 5.7 Verified performance targets on a low-end CPU: ONNX load ≤ 5s, warm-up ≤ 2s, synthesis latency ≤ 500ms, Gemini connect ≤ 10s, mic latency ≤ 100ms, queue size ≤ 4, tasks ≤ 5, sessions ≤ 1, CPU ≤ 30%, memory ≤ 200MB.
- [x] 5.8 Verified existing voice functionality is preserved (existing TTS engines, STT engines, wake-word detector, and all Settings controls).