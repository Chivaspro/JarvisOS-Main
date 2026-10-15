## Purpose

Provides fast barge-in detection that stops stale output and processes the new user request when the user starts speaking while JARVIS is speaking.

## ADDED Requirements

### Requirement: Detect interruption quickly

The system SHALL detect when the user starts speaking while JARVIS is speaking and stop or discard stale output quickly.

#### Scenario: Fast interruption detection

- **WHEN** the user starts speaking while JARVIS is speaking
- **THEN** JARVIS detects the interruption quickly and stops the current output

#### Scenario: Stale output discarded

- **WHEN** the user interrupts JARVIS
- **THEN** any stale output from the old response is discarded and not played

### Requirement: Process new user request after interruption

The system SHALL process the new user request normally after an interruption. It must not continue speaking the old response over the new user voice.

#### Scenario: New request processed

- **WHEN** the user interrupts JARVIS with a new request
- **THEN** the new request is processed normally and JARVIS responds to it

### Requirement: No overlap of old and new voice

The system SHALL ensure that the old response does not continue speaking over the new user voice.

#### Scenario: No voice overlap

- **WHEN** the user interrupts JARVIS
- **THEN** the old voice output stops before the new user voice is processed