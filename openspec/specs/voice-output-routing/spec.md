# voice-output-routing Specification

## Purpose

Defines which component renders JARVIS's audible reply, how the three voice output modes are selected and persisted, and how legacy configuration values are migrated without silently changing the user's chosen pipeline.

## Requirements

### Requirement: Exactly three voice output modes

The system SHALL support exactly three voice output modes, identified internally as `gemini`, `gemini_piper` and `local`:

- `gemini` — the realtime Gemini Live session renders the reply audio (previous behaviour, unchanged).
- `gemini_piper` — Gemini Live conducts the conversation and produces reply text, and the local Piper ONNX voice renders the audible reply; Gemini native audio is not played.
- `local` — replies are rendered by the locally selected TTS engine (existing behaviour, unchanged).

Any other stored value SHALL be rejected at validation time with a clear message, and the previous working mode SHALL be preserved.

#### Scenario: Unknown mode rejected

- **WHEN** a voice configuration containing a mode that is not one of the three is validated
- **THEN** validation fails with a message naming the offending mode and no configuration change is applied

#### Scenario: Each mode selects its own renderer

- **WHEN** the active mode is `gemini`, `gemini_piper` or `local`
- **THEN** the audible reply is rendered respectively by the Gemini Live audio stream, the Piper ONNX voice, or the locally selected TTS engine

### Requirement: Legacy configuration migrates to the Piper pipeline

The system SHALL resolve the voice output mode from configuration as follows, without deleting any existing configuration key:

- A stored `gemini_piper` value is used as-is.
- A stored `local` value is used as-is.
- A stored legacy `hybrid` value is treated as `gemini_piper`.
- A missing mode key defaults to `gemini_piper`.
- A stored `gemini` value is lifted to `gemini_piper` exactly once, recorded by a migration marker in configuration, so that the new pipeline is active by default while a later deliberate selection of Gemini Native Audio persists across restarts.
- When the migration marker is already present, a stored `gemini` value is respected as-is.

#### Scenario: Missing mode defaults to the Piper pipeline

- **WHEN** JARVIS starts with no voice output mode stored
- **THEN** the active mode is `gemini_piper`

#### Scenario: Legacy hybrid value becomes the Piper pipeline

- **WHEN** JARVIS starts with a stored `hybrid` mode
- **THEN** the active mode is `gemini_piper` and a duplicate microphone is not opened

#### Scenario: One-time lift of an existing Gemini choice

- **WHEN** JARVIS starts with a stored `gemini` mode and no migration marker
- **THEN** the active mode becomes `gemini_piper`, the migration is recorded once, and the user is told in the log which mode is active

#### Scenario: Deliberate Gemini choice is respected afterwards

- **WHEN** the migration marker is present and the stored mode is `gemini`
- **THEN** the active mode is `gemini` on this and every later start

### Requirement: Gemini native audio is not played in Piper mode

When the active mode is `gemini_piper`, the system SHALL discard Gemini's native response audio and SHALL NOT route it to the audio output device. Gemini audio and Piper audio SHALL never be audible at the same time.

#### Scenario: Gemini audio is suppressed

- **WHEN** a reply arrives in `gemini_piper` mode
- **THEN** the Gemini audio chunks are consumed and discarded and only Piper audio reaches the speaker

#### Scenario: No simultaneous playback

- **WHEN** a reply is being spoken in `gemini_piper` mode
- **THEN** only one playback path is active and no second audio stream plays the same reply

#### Scenario: Suppression is reported once

- **WHEN** the reply pipeline is active in `gemini_piper` mode
- **THEN** a single log line tells the user that Gemini native audio is suppressed and who renders the voice

#### Scenario: Discarding survives silence between turns

- **WHEN** Gemini is not producing audio (between turns) while the assistant is in `gemini_piper` or `local` mode
- **THEN** the discarding continues running instead of raising, so the voice session is not torn down by an idle period

### Requirement: Voice mode changes take effect without a restart

The system SHALL apply a saved mode change to the running assistant by rebuilding only the voice session, and SHALL stop the previously active renderer so no stale audio or stale worker survives the change. Moving out of `gemini_piper` SHALL stop Piper playback and clear any queued Piper speech.

#### Scenario: Switching into Piper mode

- **WHEN** the user saves `gemini_piper` as the mode
- **THEN** the running assistant rebuilds the voice session and the next spoken reply uses the Piper voice

#### Scenario: Switching out of Piper mode

- **WHEN** the user saves `gemini` or `local` while Piper is speaking or has queued speech
- **THEN** Piper playback stops, its queue is cleared, and the newly selected renderer handles subsequent replies

### Requirement: SAVE AS DEFAULT VOICE persists the selected arrangement

The **SAVE AS DEFAULT VOICE** action SHALL persist the arrangement currently selected in Settings: the
chosen voice output mode, the selected Piper voice (model and sidecar configuration), the selected
execution provider, and the local engine settings. It SHALL additionally persist the selected provider
and its voice as one atomic, validated `defaultProvider`/`defaultVoice` pair whose voice belongs to its
provider. It SHALL NOT force the mode to `local`, SHALL NOT change the mode to any value the user did
not select, and SHALL NOT write any other provider's configuration. The saved arrangement SHALL be
restored on the next start.

#### Scenario: Saving a Piper arrangement

- **WHEN** the user has selected `gemini_piper`, the JARVIS High Piper voice and `auto` execution, then
  clicks SAVE AS DEFAULT VOICE
- **THEN** the stored mode remains `gemini_piper` with that Piper voice and execution provider, the
  default pair names the Piper provider and the Piper voice, and no local-standalone mode is activated

#### Scenario: Saving does not force local mode

- **WHEN** the user clicks SAVE AS DEFAULT VOICE with any mode selected
- **THEN** the stored and active mode equals the selected mode, and `local` appears only if the user
  selected it

#### Scenario: Saving one provider does not write another

- **WHEN** the user clicks SAVE AS DEFAULT VOICE with any provider selected
- **THEN** only the selected provider's subtree, the mode/renderer selection and the default pair change,
  and every other provider's configuration is byte-identical to before the save

#### Scenario: Saved arrangement survives a restart

- **WHEN** JARVIS restarts after SAVE AS DEFAULT VOICE
- **THEN** the same mode, Piper voice and execution provider are active without user interaction

#### Scenario: Save failure is reported and harmless

- **WHEN** persisting the default voice fails
- **THEN** the previous working configuration stays active, a message is shown, and JARVIS keeps running

### Requirement: Voice mode and renderer state are visible

The system SHALL expose the active mode plus the renderer states `MIC LISTENING`, `GEMINI LIVE`, `PIPER SYNTHESIZING`, `PIPER PLAYING` and `READY` to the interface, using the existing HUD state vocabulary so the visual design is unchanged, and SHALL log which renderer is in use.

#### Scenario: Piper states are surfaced

- **WHEN** Piper is synthesizing or playing a reply
- **THEN** the interface reflects the Piper synthesizing / playing state and returns to the listening state when playback finishes

#### Scenario: Active arrangement is logged

- **WHEN** the voice configuration is applied
- **THEN** the log states the active mode, the engine or Piper voice in use, and the Piper execution provider when Piper is active

### Requirement: Lifecycle-owned startup and shutdown for the voice chain
The voice chain (Gemini Live audio, hybrid local voice, wake detection, audio
device selection) SHALL be initialized and shut down by the application lifecycle
in a deterministic order (configuration → logging → Gemini → orchestration →
memory → vision/audio → optional agents → UI → READY), instead of
ad-hoc startup code. Shutdown SHALL stop audio streams, camera, background tasks
and coding agents with no orphan processes, and SHALL
preserve the existing shutdown behavior of the voice chain itself.

#### Scenario: Startup order is deterministic
- **WHEN** the application boots
- **THEN** voice initializes after configuration and logging, and READY is reached without depending on optional subsystems

#### Scenario: Shutdown leaves no orphans
- **WHEN** the application exits
- **THEN** audio streams, camera, background tasks and agent processes are stopped and no orphan process remains

### Requirement: Voice works without optional subsystems
Voice interaction SHALL NOT require any optional subsystem introduced by this
change (orchestrator, Mem0, coding agents, Qdrant, Langfuse); each
optional subsystem's absence SHALL degrade to the local/default path without
affecting audio latency or the existing mode selection behavior.

#### Scenario: Fresh install without optional packages
- **WHEN** a fresh installation has none of the optional packages installed
- **THEN** voice conversation, wake word and audio device selection work exactly as before this change
