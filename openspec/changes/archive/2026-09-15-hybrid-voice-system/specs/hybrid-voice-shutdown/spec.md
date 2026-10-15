## Purpose

Provides clean shutdown that stops Gemini Live, microphone capture, audio playback, ONNX workers, and background tasks; releases all resources; and does not leave zombie processes, threads, or tasks.

## ADDED Requirements

### Requirement: Stop Gemini Live on shutdown

The system SHALL stop the Gemini Live session when JARVIS closes. It must not leave a zombie Gemini session.

#### Scenario: Gemini stopped on shutdown

- **WHEN** JARVIS closes
- **THEN** the Gemini Live session is stopped and its resources are released

### Requirement: Stop microphone capture on shutdown

The system SHALL stop the microphone capture when JARVIS closes. It must not leave a zombie microphone stream.

#### Scenario: Mic stopped on shutdown

- **WHEN** JARVIS closes
- **THEN** the microphone capture is stopped and the stream is released

### Requirement: Stop audio playback on shutdown

The system SHALL stop audio playback when JARVIS closes. It must not leave a zombie playback stream.

#### Scenario: Playback stopped on shutdown

- **WHEN** JARVIS closes
- **THEN** audio playback is stopped and the output device is released

### Requirement: Stop ONNX workers on shutdown

The system SHALL stop ONNX workers when JARVIS closes. It must not leave a zombie ONNX inference thread.

#### Scenario: ONNX workers stopped on shutdown

- **WHEN** JARVIS closes
- **THEN** ONNX workers are stopped and the ONNX Runtime session is released

### Requirement: Cancel background tasks on shutdown

The system SHALL cancel all background tasks when JARVIS closes. It must not leave zombie tasks.

#### Scenario: Background tasks cancelled on shutdown

- **WHEN** JARVIS closes
- **THEN** all background tasks are cancelled and their threads are joined

### Requirement: Release all resources on shutdown

The system SHALL release all audio, model, and session resources when JARVIS closes. It must not leak memory or file handles.

#### Scenario: All resources released on shutdown

- **WHEN** JARVIS closes
- **THEN** all audio devices, model sessions, and network connections are released

#### Scenario: No zombie processes or threads

- **WHEN** JARVIS closes
- **THEN** no zombie processes, threads, or tasks remain