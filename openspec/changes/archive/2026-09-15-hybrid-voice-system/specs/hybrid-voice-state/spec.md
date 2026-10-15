## Purpose

Provides a real-time voice-state machine that reflects the actual runtime state instead of simply showing what was selected in Settings.

## ADDED Requirements

### Requirement: UI shows real voice state

The system SHALL expose the real voice-state machine to the UI. The status must represent the actual runtime state.

#### Scenario: State reflects runtime

- **WHEN** the voice system is in a given state
- **THEN** the UI shows that exact state

### Requirement: Supported states

The system SHALL support the following states: IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED, CONNECTING, CONNECTED, RECONNECTING, ONNX LOADING, ONNX READY, VOICE ERROR.

#### Scenario: All states supported

- **WHEN** the voice system transitions between states
- **THEN** all eleven states are supported and reflected in the UI

### Requirement: State transitions are valid

The system SHALL only transition between valid states. Invalid transitions must be rejected.

#### Scenario: Valid transitions

- **WHEN** the voice system transitions from IDLE to LISTENING
- **THEN** the transition is allowed and the UI updates

#### Scenario: Invalid transitions rejected

- **WHEN** the voice system attempts an invalid transition
- **THEN** the transition is rejected and the state remains unchanged