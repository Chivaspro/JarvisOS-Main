## Purpose

Provides a real-time microphone soundwave visualization that responds to the user's actual microphone volume levels, with smooth updates, an idle state when silent, and full preservation of existing voice input functionality.

## ADDED Requirements

### Requirement: Soundwave responds to actual microphone input volume

The system SHALL drive the soundwave visualization from the actual microphone audio volume rather than a looping or fake animation, so that quiet speech produces smaller movements and loud speech produces stronger and larger waveform or particle movement.

#### Scenario: Soundwave reacts to quiet speech

- **WHEN** the user speaks quietly into the microphone
- **THEN** the soundwave shows small movements proportional to the low volume

#### Scenario: Soundwave reacts to loud speech

- **WHEN** the user speaks loudly into the microphone
- **THEN** the soundwave shows strong and large waveform or particle movement proportional to the high volume

#### Scenario: Soundwave is not faked

- **WHEN** the microphone is active
- **THEN** the soundwave is driven by actual microphone audio levels, not a looping animation

### Requirement: Soundwave remains calm when there is no voice input

The system SHALL keep the soundwave calm or nearly idle when there is no voice input, so the visualization does not show activity when the user is silent.

#### Scenario: No voice input

- **WHEN** the microphone is active but the user is not speaking
- **THEN** the soundwave remains calm or nearly idle

### Requirement: Smooth updates without excessive flickering

The system SHALL update the soundwave smoothly to reflect microphone volume changes without excessive flickering or jitter.

#### Scenario: Smooth volume updates

- **WHEN** the microphone volume changes gradually
- **THEN** the soundwave updates smoothly without flickering

#### Scenario: No excessive flickering during speech

- **WHEN** the user speaks continuously
- **THEN** the soundwave moves smoothly without rapid flickering or jitter

### Requirement: Existing voice input functionality preserved

The system SHALL preserve all existing voice input functionality, including microphone activation, PCM16 WebSocket streaming, encryption, and the existing recording state styling.

#### Scenario: Existing voice input still works

- **WHEN** the user taps the microphone button
- **THEN** the existing voice input functionality (microphone access, audio processing, WebSocket streaming) works exactly as before

#### Scenario: Recording state styling preserved

- **WHEN** the microphone is recording
- **THEN** the existing recording state styling (recording class, pulse animation) is preserved