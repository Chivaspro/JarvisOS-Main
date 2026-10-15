## Purpose

Protects low-end CPUs by bounding thread counts, using bounded queues, running heavy work in the correct background/asynchronous worker, and preventing unnecessary continuous processing.

## ADDED Requirements

### Requirement: Bounded thread counts

The system SHALL use bounded thread counts for all voice processing. It must not use excessive CPU threads.

#### Scenario: Limited threads

- **WHEN** the hybrid voice system runs on a low-end CPU
- **THEN** the number of voice processing threads is bounded (ONNX: max 4, Gemini: 1, audio: 1)

### Requirement: Bounded queues

The system SHALL use bounded queues for audio and synthesis data. It must prevent memory growth from unbounded queues.

#### Scenario: Bounded audio queue

- **WHEN** the hybrid voice system is running
- **THEN** the audio input queue is bounded (maxsize=4) and blocks when full

#### Scenario: Bounded synthesis queue

- **WHEN** the hybrid voice system is running
- **THEN** the synthesis queue is bounded (maxsize=4) and blocks when full

### Requirement: Heavy work in background workers

The system SHALL run heavy work (ONNX inference, Gemini connection, audio playback) in the correct background/asynchronous worker. It must not block the UI thread or the Gemini Live network loop with ONNX inference.

#### Scenario: UI not blocked

- **WHEN** ONNX inference is running
- **THEN** the UI thread is not blocked and remains responsive

#### Scenario: Gemini loop not blocked

- **WHEN** ONNX inference is running
- **THEN** the Gemini Live network loop is not blocked and continues to process audio

### Requirement: No unnecessary continuous processing

The system SHALL not run unnecessary processing continuously. It must not process audio frames when the system is idle.

#### Scenario: No continuous idle processing

- **WHEN** the hybrid voice system is idle
- **THEN** no audio processing occurs and CPU usage is near zero

### Requirement: Prevent thread leaks

The system SHALL prevent thread leaks by joining all threads on shutdown and not creating new threads per request.

#### Scenario: No thread leaks

- **WHEN** the hybrid voice system starts and stops
- **THEN** no threads are leaked and all threads are joined on shutdown

### Requirement: Prevent asyncio task leaks

The system SHALL prevent asyncio task leaks by cancelling all tasks on shutdown and not creating new tasks per message.

#### Scenario: No task leaks

- **WHEN** the hybrid voice system starts and stops
- **THEN** no asyncio tasks are leaked and all tasks are cancelled on shutdown

### Requirement: Prevent duplicate callbacks

The system SHALL prevent duplicate callbacks by registering each callback exactly once and unregistering on shutdown.

#### Scenario: No duplicate callbacks

- **WHEN** the hybrid voice system starts and stops
- **THEN** no duplicate callbacks are registered and all callbacks are unregistered on shutdown

### Requirement: Prevent duplicate audio events

The system SHALL prevent duplicate audio events by deduplicating audio frames and not sending the same frame twice.

#### Scenario: No duplicate audio events

- **WHEN** the hybrid voice system is running
- **THEN** no duplicate audio events are sent to the playback system

### Requirement: Measurable performance targets

The system SHALL meet the following measurable performance targets on a low-end CPU:
- ONNX model loading time: ≤ 5 seconds
- ONNX warm-up time: ≤ 2 seconds
- Voice synthesis latency: ≤ 500 ms per sentence
- Gemini connection time: ≤ 10 seconds
- Microphone latency: ≤ 100 ms
- Audio queue size: ≤ 4 frames
- Number of running voice tasks: ≤ 5
- Number of active Gemini sessions: ≤ 1
- CPU usage: ≤ 30% during active voice processing
- Memory usage: ≤ 200 MB for voice subsystem

#### Scenario: Performance targets met

- **WHEN** the hybrid voice system runs on a low-end CPU
- **THEN** all measurable performance targets are met