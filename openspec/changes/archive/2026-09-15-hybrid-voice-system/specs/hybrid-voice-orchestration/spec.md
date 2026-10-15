## Purpose

Provides a single coordinator that owns microphone capture, the Gemini Live session, the ONNX engine, and audio playback. Only one instance of each resource exists at any time, and the components cooperate rather than duplicate each other.

## ADDED Requirements

### Requirement: Single microphone capture shared by all consumers

The system SHALL capture the microphone exactly once and share the captured PCM safely between the Gemini Live session, the wake-word detector, the VAD, and the soundwave visualization. No component shall open its own microphone handle.

#### Scenario: Microphone opened once

- **WHEN** the hybrid voice system starts
- **THEN** exactly one `sd.InputStream` (or equivalent) is opened and its PCM is distributed to all consumers via a shared bus

#### Scenario: No duplicate mic handles

- **WHEN** the hybrid voice system is running
- **THEN** no component opens a second microphone handle; all audio input flows through the shared bus

### Requirement: Single Gemini Live session

The system SHALL create and maintain exactly one Gemini Live session. No component shall create a second session, and no reconnect shall leave the old session's audio playing.

#### Scenario: One Gemini session

- **WHEN** the hybrid voice system is active
- **THEN** exactly one Gemini Live session exists and all conversation traffic flows through it

#### Scenario: Reconnect does not leave stale audio

- **WHEN** the Gemini session reconnects
- **THEN** the old session's audio playback is stopped before the new session starts

### Requirement: Single ONNX engine loaded once and reused

The system SHALL load the ONNX voice model exactly once and reuse it for all synthesis. No new ONNX Runtime session shall be created per sentence, and no new model load shall occur per response.

#### Scenario: ONNX loaded once

- **WHEN** the ONNX engine is first used
- **THEN** the model is loaded exactly once and cached for all subsequent synthesis

#### Scenario: No per-sentence reload

- **WHEN** multiple sentences are synthesized
- **THEN** the ONNX model is not reloaded between sentences

### Requirement: Single audio playback path

The system SHALL route all voice output (Gemini Live audio and ONNX synthesis) through a single controlled playback path. No component shall call `sd.play` directly.

#### Scenario: All audio through one path

- **WHEN** any voice output is produced
- **THEN** all audio (Gemini and ONNX) flows through the single playback coordinator

#### Scenario: No duplicate playback streams

- **WHEN** the hybrid voice system is running
- **THEN** no two audio streams play simultaneously unless one is explicitly interrupting the other

### Requirement: Components cooperate rather than duplicate

The system SHALL assign clear responsibility to each component:
- Gemini Live handles real-time conversation and its own audio output.
- Local ONNX handles local voice/audio processing defined by the implementation.
- The coordinator decides which component is responsible for each job.

#### Scenario: Clear responsibility assignment

- **WHEN** the hybrid voice system is active
- **THEN** each job (mic capture, conversation, TTS, playback, soundwave) is assigned to exactly one component

#### Scenario: No duplicate speaking

- **WHEN** a response is produced
- **THEN** only one component speaks the response; Gemini Live and ONNX do not both speak the same response