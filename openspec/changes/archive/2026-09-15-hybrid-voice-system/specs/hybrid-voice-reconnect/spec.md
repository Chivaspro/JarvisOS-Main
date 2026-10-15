## Purpose

Provides graceful Gemini Live reconnection that stops stale sessions/tasks, prevents duplicate sessions, and preserves conversation context where supported.

## ADDED Requirements

### Requirement: Reconnect does not crash JARVIS

The system SHALL handle Gemini Live disconnects gracefully. It must not crash JARVIS when the connection is lost.

#### Scenario: Disconnect handled gracefully

- **WHEN** the Gemini Live connection is lost
- **THEN** JARVIS does not crash and enters the RECONNECTING state

### Requirement: Stop stale session tasks on reconnect

The system SHALL stop all stale session tasks before reconnecting. No old task shall continue running after a reconnect.

#### Scenario: Stale tasks stopped

- **WHEN** the Gemini session reconnects
- **THEN** all old session tasks are stopped before the new session starts

### Requirement: Prevent multiple sessions

The system SHALL prevent multiple Gemini Live sessions from being created. Only one session shall exist at any time.

#### Scenario: Single session

- **WHEN** a reconnect is attempted
- **THEN** the old session is fully closed before the new one is created

#### Scenario: No duplicate sessions

- **WHEN** the hybrid voice system is running
- **THEN** no two Gemini Live sessions exist simultaneously

### Requirement: Prevent old audio from previous session

The system SHALL prevent old audio from the previous session from playing after a reconnect.

#### Scenario: Old audio stopped

- **WHEN** the Gemini session reconnects
- **THEN** any audio from the old session that is still buffered is discarded

### Requirement: Preserve conversation context where supported

The system SHALL preserve conversation context across reconnects where the Gemini Live API supports it.

#### Scenario: Context preserved

- **WHEN** the Gemini session reconnects
- **THEN** the conversation context is preserved where the API supports it

### Requirement: Bounded reconnect backoff

The system SHALL use a bounded exponential backoff for reconnect attempts. It must not retry indefinitely without limit.

#### Scenario: Bounded backoff

- **WHEN** the Gemini session reconnects repeatedly
- **THEN** the backoff is bounded (maximum 30 seconds) and does not grow indefinitely