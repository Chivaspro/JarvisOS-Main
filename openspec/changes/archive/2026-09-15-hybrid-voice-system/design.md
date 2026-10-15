## Context

The existing voice system has two independent paths: `JarvisLive` (Gemini Live audio session in `main.py`) and the local TTS engine (selected via `tts_engine` in `config/api_keys.json`). They are not coordinated, leading to duplicate mic capture, duplicate audio playback, per-sentence ONNX reloads, and unsafe config save/reload. This design introduces a single Hybrid Voice System coordinator that owns all voice resources.

See proposal.md for the motivation and scope.

## Goals / Non-Goals

**Goals:**
- Single microphone capture shared by Gemini, wake, VAD, and soundwave.
- Single Gemini Live session with safe reconnect.
- Single ONNX engine loaded once and reused.
- Single audio playback path.
- Safe config save/reload with validation and rollback.
- Real-time voice-state machine exposed to the UI.
- Clean shutdown with no zombie resources.
- Bounded queues, threads, and tasks to protect low-end CPUs.

**Non-Goals:**
- No changes to the existing TTS engines (EdgeTTS, Kokoro, ElevenLabs, Fish Audio, SAPI, ONNX) or STT engines (Whisper, Vosk).
- No changes to the wake-word detector logic.
- No new external libraries.
- No redesign of the UI beyond exposing the voice-state machine.

## Decisions

### Decision 1: Single coordinator in `core/hybrid_voice.py`

**Rationale:** A single coordinator class (`HybridVoiceSystem`) owns all voice resources and exposes a clean interface to the rest of the app. This avoids scattering ownership logic across `main.py`, `tts.py`, and `ui.py`.

**Alternatives considered:**
- *Patch existing modules individually*: Would leave ownership ambiguous and risk duplicate resources. Rejected.
- *External orchestration framework*: Overkill and adds dependencies. Rejected.

### Decision 2: Shared microphone bus

**Rationale:** The coordinator opens exactly one `sd.InputStream` and distributes PCM to consumers via a shared bus (Gemini send queue, wake detector feed, VAD, and soundwave level). Consumers register callbacks on the bus; the coordinator pumps the bus in its own audio thread.

**Alternatives considered:**
- *Let each consumer open its own mic*: Would create duplicate streams and audio contention. Rejected.
- *Use `sounddevice`-recorder callback*: Already the existing pattern; the coordinator wraps it.

### Decision 3: Single Gemini session with reconnect state machine

**Rationale:** The coordinator owns the Gemini Live session. On disconnect, it enters RECONNECTING, stops stale tasks, discards old audio, and reconnects with bounded exponential backoff (max 30s). Only one session exists at any time.

**Alternatives considered:**
- *Let `JarvisLive` manage its own reconnect*: Would leave duplicate session risk. Rejected.

### Decision 4: Cached ONNX session

**Rationale:** The ONNX model is loaded once into a cached `PiperVoice` instance. `OnnxTTSEngine._load()` is idempotent and guarded by a lock. A new `TTSPlayer` is not created per sentence; the cached engine is reused.

**Alternatives considered:**
- *Reload ONNX per sentence*: Would add 6-9s latency per sentence. Rejected.

### Decision 5: Single playback path

**Rationale:** All audio output (Gemini Live audio and ONNX synthesis) is routed through the coordinator's playback manager. The playback manager stops any current output before starting new output, preventing overlap.

**Alternatives considered:**
- *Let each component call `sd.play` directly*: Would cause duplicate/overlapping audio. Rejected.

### Decision 6: Atomic config save with rollback

**Rationale:** Voice config is written to a temp file and renamed atomically. On save failure, the previous config is preserved and the previous mode remains active. Validation happens before save: settings, ONNX model, and Gemini config are all checked.

**Alternatives considered:**
- *Direct write*: Risk of corrupt config on crash. Rejected.
- *Backup + restore*: More complex than atomic write. Rejected in favor of atomic write.

### Decision 7: Voice-state machine

**Rationale:** A finite state machine with the eleven requested states is maintained by the coordinator. The UI reads the current state and updates the status bar. Transitions are validated to prevent invalid state changes.

## Risks / Trade-offs

[Risk: Coordinator failure breaks all voice] → Mitigation: The coordinator degrades gracefully; if it fails to start, the app falls back to the previous behaviour (existing `JarvisLive` + TTS player). A kill switch in Settings disables the hybrid system.

[Risk: ONNX inference blocks the Gemini loop] → Mitigation: ONNX inference runs in a dedicated background thread with a bounded queue; the Gemini loop is never blocked.

[Risk: Reconnect storms on flaky networks] → Mitigation: Bounded exponential backoff (max 30s) and a circuit breaker after N consecutive failures.

[Risk: Memory growth from unbounded queues] → Mitigation: All queues are bounded (maxsize=4 for audio, maxsize=4 for synthesis); producers block when full.

[Risk: Thread leaks from per-request thread creation] → Mitigation: Threads are created once at startup and joined on shutdown; no per-request threads.

## Migration Plan

This change is additive and requires no server migration. To deploy:
1. Add `core/hybrid_voice.py`.
2. Wire the coordinator into `main.py` (`JarvisLive`) and `ui.py`.
3. Add `voice_output_mode` and `hybrid_config` to `config/api_keys.json`.
4. The existing `tts_engine` and `voice_mode` keys are preserved for backward compatibility.
5. To rollback, set `voice_output_mode` to `"gemini"` or `"local"` in Settings, or disable the hybrid system via the kill switch.

## Open Questions

- Should the hybrid system be opt-in or default? Decision: opt-in via the "Hybrid — ONNX + Gemini Live" setting; existing behaviour is preserved.
- Should the ONNX engine be used for TTS in hybrid mode? Decision: In hybrid mode, Gemini Live handles conversation and audio output; ONNX is used for local voice/audio processing defined by the implementation (e.g., offline wake-word, VAD, or fallback TTS). The coordinator decides which component is responsible for each job.