## Purpose

Provides safe voice configuration save/reload: validate settings, validate the ONNX model, validate the Gemini configuration, save the configuration safely, stop old voice resources, clean up old sessions/tasks, and start the new voice mode once.

## ADDED Requirements

### Requirement: Validate settings before saving

The system SHALL validate all voice settings before saving them. Invalid settings must be rejected with a clear error message and must not partially activate broken configuration.

#### Scenario: Valid settings saved

- **WHEN** the user saves valid voice settings
- **THEN** the configuration is saved and the new mode is activated

#### Scenario: Invalid settings rejected

- **WHEN** the user saves invalid voice settings
- **THEN** an error is shown and the previous working configuration is preserved

### Requirement: Validate the ONNX model before saving

The system SHALL validate the ONNX model path and sidecar JSON before saving an ONNX-based configuration. A missing or invalid model must be rejected.

#### Scenario: Valid ONNX model

- **WHEN** the user saves an ONNX configuration with a valid model
- **THEN** the configuration is saved and the model is loaded once

#### Scenario: Invalid ONNX model rejected

- **WHEN** the user saves an ONNX configuration with a missing or invalid model
- **THEN** an error is shown and the previous working configuration is preserved

### Requirement: Validate the Gemini configuration before saving

The system SHALL validate the Gemini API key and model name before saving a Gemini-based configuration. An invalid key or model must be rejected.

#### Scenario: Valid Gemini configuration

- **WHEN** the user saves a Gemini configuration with a valid API key and model
- **THEN** the configuration is saved and the session is started

#### Scenario: Invalid Gemini configuration rejected

- **WHEN** the user saves a Gemini configuration with an invalid API key or model
- **THEN** an error is shown and the previous working configuration is preserved

### Requirement: Save configuration safely

The system SHALL save the voice configuration atomically — write to a temp file and rename — so a crash during save never leaves a corrupt config file.

#### Scenario: Atomic save

- **WHEN** the user saves voice settings
- **THEN** the config file is written atomically (temp + rename) and is never partially written

### Requirement: Stop old voice resources before starting new mode

The system SHALL stop all old voice resources (Gemini session, mic, playback, ONNX workers, background tasks) before starting the new voice mode. No old session or task shall continue running after a mode change.

#### Scenario: Old resources stopped

- **WHEN** the user saves a new voice configuration
- **THEN** all old voice resources are stopped before the new mode starts

#### Scenario: No old tasks continue

- **WHEN** the user saves a new voice configuration
- **THEN** no old background tasks continue running after the mode change

### Requirement: Start the new voice mode once

The system SHALL start the new voice mode exactly once after saving. It shall not start the new mode multiple times or leave the old mode partially active.

#### Scenario: New mode started once

- **WHEN** the user saves a new voice configuration
- **THEN** the new voice mode is started exactly once and confirmed active

### Requirement: Keep previous configuration on failure

The system SHALL keep the previous working configuration if saving fails. It shall not partially activate broken settings, and it shall show a useful error message without crashing JARVIS.

#### Scenario: Save failure preserves previous config

- **WHEN** saving a new voice configuration fails
- **THEN** the previous working configuration remains active and a useful error is shown

#### Scenario: No crash on save failure

- **WHEN** saving a new voice configuration fails
- **THEN** JARVIS does not crash and remains usable with the previous configuration

### Requirement: Save "Hybrid — ONNX + Gemini Live" as default

The system SHALL support a voice option called "Hybrid — ONNX + Gemini Live". When the user selects this mode and clicks "SAVE AS DEFAULT", the application must save that mode as the active default configuration and restore it automatically after restart.

#### Scenario: Hybrid mode saved as default

- **WHEN** the user selects "Hybrid — ONNX + Gemini Live" and clicks "SAVE AS DEFAULT"
- **THEN** the mode is saved as the active default configuration

#### Scenario: Hybrid mode restored after restart

- **WHEN** JARVIS restarts
- **THEN** the saved hybrid mode is automatically restored and the runtime uses it