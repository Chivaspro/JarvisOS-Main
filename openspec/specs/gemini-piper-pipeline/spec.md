# gemini-piper-pipeline Specification

## Purpose

Defines how a Gemini Live reply becomes audible JARVIS speech through the local Piper voice: which text is used, how streaming text is chunked, how speech is queued and played off the interface thread, how the user interrupts it, and which existing audio resources the pipeline reuses.

## Requirements

### Requirement: Gemini reply text is the only Piper input

When the active mode is `gemini_piper`, the system SHALL synthesize speech from Gemini's response text (the output transcription of the assistant's turn). The system SHALL NOT feed Gemini's generated audio, or any encoding of it, into the Piper voice, and SHALL NOT re-transcribe Gemini audio to obtain the text.

#### Scenario: Transcript drives synthesis

- **WHEN** Gemini completes or streams an assistant turn in `gemini_piper` mode
- **THEN** the transcribed assistant text is what Piper synthesizes

#### Scenario: Generated audio is never synthesized

- **WHEN** a turn produces both native audio and transcript text
- **THEN** the native audio is discarded and only the transcript text reaches Piper

### Requirement: The audible reply always uses the ONNX voice

In `gemini_piper` mode the audible reply SHALL be rendered by the local ONNX (Piper) voice configured in
the Piper provider's own configuration subtree, including when a different speech provider is active or
selected elsewhere in Settings. That other provider's selection SHALL remain in effect for its own mode
and SHALL NOT be altered by saving the Piper arrangement; conversely, saving or editing any other
provider SHALL NOT modify the Piper provider's subtree.

#### Scenario: Another engine is selected but Piper speaks

- **WHEN** the mode is `gemini_piper` and a different speech provider (for example EdgeTTS) is selected
  in Settings
- **THEN** the reply is still synthesized from the configured ONNX voice in the Piper provider's subtree
  and no other engine is invoked for it

#### Scenario: The local engine choice is preserved

- **WHEN** the user saves the Piper arrangement
- **THEN** the active/selected local provider's own configuration is unchanged, so switching to `local`
  mode still uses that provider's engine and voice

#### Scenario: The Piper arrangement is preserved from other providers' saves

- **WHEN** the user saves or edits any other speech provider's configuration
- **THEN** the Piper provider's subtree (model, sidecar configuration, speaker, execution provider) is
  unchanged, so switching to the Piper-rendered pipeline still uses that arrangement

### Requirement: Streaming text is buffered into sentence chunks

The system SHALL accumulate Gemini's streaming transcript fragments and submit whole sentence or phrase-sized chunks to Piper rather than one fragment per event or one huge block per reply. Speech SHALL begin before the assistant turn is complete when a complete sentence is available. The remainder SHALL be flushed when the turn completes, and text fragments SHALL never be duplicated or reordered in the spoken output.

#### Scenario: First sentence is spoken early

- **WHEN** a streaming turn has emitted one complete sentence while the turn is still running
- **THEN** that sentence is synthesized and played before the turn completes

#### Scenario: Turn remainder is flushed

- **WHEN** the assistant turn completes with buffered text that has no sentence terminator
- **THEN** the buffered remainder is synthesized and spoken

#### Scenario: Cumulative transcripts do not repeat speech

- **WHEN** consecutive transcript fragments are cumulative rather than incremental
- **THEN** each fragment is submitted only once and the spoken text matches the final transcript

### Requirement: One reusable Piper worker with a bounded queue

The system SHALL own a single long-lived Piper speech worker with one bounded queue. The worker SHALL be created once and reused for every reply; a new worker thread SHALL NOT be created per sentence, per reply or per request. When the queue is full, the system SHALL drop the incoming chunk, log it, and keep speaking what it already has, rather than growing without bound or spawning threads.

#### Scenario: Worker is reused

- **WHEN** multiple replies are spoken in one session
- **THEN** the same worker thread and the same loaded voice serve all of them

#### Scenario: Queue is bounded

- **WHEN** speech is submitted faster than it can be spoken
- **THEN** the queue never exceeds its configured bound, the overflow is logged, and no additional synthesis threads are created

#### Scenario: Piper failure degrades gracefully

- **WHEN** synthesis or playback raises
- **THEN** the failure is logged with the reason, the worker stays usable, the reply text remains on screen, and JARVIS keeps running

### Requirement: Speech runs off the interface and conversation threads

Synthesis and playback SHALL run on the Piper worker thread. The interface thread SHALL NOT block on model loading, synthesis or playback, and the Gemini realtime loop SHALL NOT be blocked by them either.

#### Scenario: Interface stays responsive

- **WHEN** a long reply is being synthesized and played
- **THEN** the interface still accepts input and updates its state

#### Scenario: Realtime loop keeps receiving

- **WHEN** Piper is synthesizing
- **THEN** the Gemini session continues to send and receive audio and text without waiting for synthesis

### Requirement: Piper playback is interruptible

When the user speaks, presses the interrupt control, or a wake-word barge-in fires while Piper is speaking, the system SHALL stop Piper playback, clear all queued Piper speech, discard the rest of the interrupted reply, and resume listening. Speech queued for an interrupted turn SHALL NOT resume afterwards. An explicit manual interrupt SHALL NOT be refused by automation settings.

#### Scenario: Barge-in stops Piper

- **WHEN** the user starts speaking while Piper is playing
- **THEN** Piper playback stops immediately, the queue is cleared, and the assistant returns to listening

#### Scenario: Queued speech does not resume

- **WHEN** a queued Piper chunk belongs to an interrupted reply
- **THEN** it is discarded and never played later

#### Scenario: Interruption is reported

- **WHEN** an interrupt stops Piper speech
- **THEN** the log reports that the interrupted speech was stopped and the assistant is listening again

#### Scenario: Server-signalled interruption clears queued speech

- **WHEN** the Gemini session reports that the user interrupted the reply
- **THEN** Piper playback stops, queued Piper speech is cleared, and the assistant returns to listening

### Requirement: The pipeline reuses the existing audio resources

The system SHALL reuse the project's existing microphone capture, the existing single Gemini Live session, and the existing audio output infrastructure. The Piper path SHALL NOT open its own microphone, create its own Gemini Live session, or introduce a second competing playback path.

#### Scenario: No extra microphone

- **WHEN** `gemini_piper` is the active mode
- **THEN** exactly one microphone stream is open and the Piper path holds no capture device

#### Scenario: No extra Gemini session

- **WHEN** `gemini_piper` is the active mode
- **THEN** the conversation still runs on the single existing Gemini Live session

#### Scenario: One playback path at a time

- **WHEN** Piper is speaking
- **THEN** no other playback path writes the same reply to the speaker

### Requirement: Piper is preloaded in the background

The system SHALL preload the Piper voice in a background thread at start-up, without blocking the interface, and SHALL reuse the loaded voice for all later synthesis. When a Piper voice is already loaded, a further preload request SHALL be a cheap no-op. Failure to preload SHALL be logged and SHALL NOT prevent JARVIS from starting or speaking through the other modes.

#### Scenario: Non-blocking warm-up

- **WHEN** JARVIS starts in `gemini_piper` mode
- **THEN** the Piper voice loads in the background while the interface remains usable

#### Scenario: Warm-up failure is survivable

- **WHEN** the Piper voice cannot be loaded at start-up
- **THEN** the reason is logged, JARVIS starts normally, and the other voice modes still work

#### Scenario: Warm-up is idempotent

- **WHEN** the voice is already loaded and preload runs again
- **THEN** the model is not reloaded
