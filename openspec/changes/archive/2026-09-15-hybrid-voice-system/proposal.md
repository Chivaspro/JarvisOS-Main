## Why

The current JARVIS voice system has two independent paths that can run concurrently: the Gemini Live audio session (started by `JarvisLive` in `main.py`) and the local TTS engine (selected via `tts_engine` in `config/api_keys.json`, e.g. `onnx`, `edgetts`, `kokoro`). They are not coordinated, so:

- Both can speak at the same time (Gemini Live's audio output and the local TTS engine's `sd.play`).
- The microphone can be opened twice (Gemini Live's `sd.InputStream` and the wake/VAD detector's own capture).
- The ONNX model can be reloaded per sentence because `create_tts_player` is called on every reply/test.
- A reconnect creates a new Gemini session while the old one's audio may still be playing.
- Voice settings changes do not reliably stop old sessions/tasks before starting the new mode.
- The UI status does not reflect the real runtime state.

This change introduces a single **Hybrid Voice System** that owns the microphone once, owns the Gemini Live session once, owns the ONNX engine once, and routes audio playback through a single controlled path. It adds a real voice-state machine, safe configuration save/reload, bounded queues, and graceful reconnection/interruption/shutdown.

## What Changes

- **New file**: `core/hybrid_voice.py` — the Hybrid Voice System coordinator.
- **Modified**: `core/voice.py` — add `HybridVoiceMode` enum and `HybridVoiceSystem` integration points.
- **Modified**: `core/tts.py` — add `OnnxTTSEngine.warmup_once()` and a shared ONNX session cache.
- **Modified**: `main.py` — wire the Hybrid Voice System into `JarvisLive` so the Gemini session and mic are owned by the coordinator.
- **Modified**: `config/api_keys.json` — add `voice_output_mode` ("hybrid" | "gemini" | "onnx" | "local") and `hybrid_config` block.
- **Modified**: `ui.py` — expose the real voice-state machine to the UI status bar.

## Capabilities

### New Capabilities

- `hybrid-voice-orchestration`: A single coordinator that owns microphone capture, Gemini Live session, ONNX engine, and audio playback. Only one instance of each resource exists at any time.
- `hybrid-voice-config`: Validate, save, and reload voice configuration safely. Old resources are stopped before new ones start; failures keep the previous working configuration.
- `hybrid-voice-state`: A real-time voice-state machine (IDLE / LISTENING / THINKING / SPEAKING / INTERRUPTED / CONNECTING / CONNECTED / RECONNECTING / ONNX LOADING / ONNX READY / VOICE ERROR) exposed to the UI.
- `hybrid-voice-reconnect`: Graceful Gemini Live reconnect that stops stale sessions/tasks, prevents duplicate sessions, and preserves conversation context where supported.
- `hybrid-voice-interruption`: Fast barge-in detection that stops stale output and processes the new user request.
- `hybrid-voice-shutdown`: Clean shutdown that stops Gemini, mic, playback, ONNX workers, and background tasks; releases all resources.
- `hybrid-voice-performance`: Bounded queues, single-threaded ONNX inference, cached ONNX session, and low-thread-count operation to protect low-end CPUs.

### Modified Capabilities

- None. No existing spec-level requirements are being changed; this is an additive orchestration layer over existing TTS/STT/wake components.

## Impact

- **Affected files**: `core/hybrid_voice.py` (new), `core/voice.py`, `core/tts.py`, `main.py`, `ui.py`, `config/api_keys.json`.
- **Affected systems**: Voice orchestration layer only. Existing TTS engines (EdgeTTS, Kokoro, ElevenLabs, Fish Audio, SAPI, ONNX), STT engines (Whisper, Vosk), and the wake-word detector are preserved and reused.
- **Dependencies**: No new external libraries. Uses existing `google-genai`, `sounddevice`, `numpy`, `onnxruntime`, `piper-tts`.
- **Risk**: Medium. Changes touch the core voice path. Mitigations: graceful degradation (falls back to existing behaviour if the coordinator fails to start), comprehensive logging, and a kill switch to disable the hybrid system and revert to the previous behaviour.