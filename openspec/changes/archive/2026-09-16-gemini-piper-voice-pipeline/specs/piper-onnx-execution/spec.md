## Purpose

Defines how the local Piper ONNX voice model is loaded, kept loaded, executed on CPU or GPU, and reported, so that the JARVIS voice is fast to reuse, works without a GPU, and never crashes when GPU acceleration is unavailable.

## ADDED Requirements

### Requirement: The Piper voice model is loaded once and reused

The system SHALL load the configured Piper ONNX model (`config/voices/jarvis-high.onnx` with its sidecar `config/voices/jarvis-high.json` by default) once per voice arrangement and reuse it for every synthesis. The model SHALL NOT be reloaded per sentence, per reply or per preload request. Changing the model, the sidecar, the speaker or the execution provider SHALL select a new cached voice, and the previous one SHALL be released.

#### Scenario: Reuse across replies

- **WHEN** several replies are spoken with the same voice arrangement
- **THEN** the model file is loaded once and the same loaded voice renders all of them

#### Scenario: Reload only on change

- **WHEN** the voice arrangement changes (model or execution provider)
- **THEN** the new arrangement is loaded once and the old one is no longer used

#### Scenario: Missing model is reported clearly

- **WHEN** the configured model or its sidecar is missing
- **THEN** a clear, actionable message names the missing path and nothing crashes

### Requirement: Execution provider selection with CPU, GPU and Auto

The system SHALL offer an execution provider setting with the values `cpu`, `gpu` and `auto`, stored as a backward-compatible configuration key, and SHALL pass the resolved execution providers to ONNX Runtime when the Piper voice is loaded:

- `cpu` SHALL use the CPU execution provider.
- `gpu` SHALL use an available GPU execution provider when one exists.
- `auto` SHALL prefer a GPU execution provider when one exists and otherwise use the CPU.

#### Scenario: CPU always works

- **WHEN** the execution provider is `cpu`
- **THEN** the voice loads and speaks using the CPU execution provider

#### Scenario: GPU used when available

- **WHEN** the execution provider is `gpu` (or `auto`) and a GPU execution provider is available
- **THEN** the voice loads with that GPU execution provider and reports which provider is active

#### Scenario: Unknown setting is normalised

- **WHEN** the stored execution provider value is missing or unrecognised
- **THEN** the system uses `cpu` and does not fail

### Requirement: GPU unavailability falls back to CPU without crashing

When a GPU execution provider is requested but unavailable — not compiled into ONNX Runtime, no usable device, or session creation with it fails — the system SHALL log the reason, load the voice with the CPU execution provider instead, and continue speaking. A missing or broken GPU SHALL NOT raise to the user, SHALL NOT prevent start-up, and SHALL NOT require any GPU package to be installed.

#### Scenario: GPU reported unavailable

- **WHEN** the execution provider is `gpu` and ONNX Runtime reports no GPU provider
- **THEN** the reason is logged, the CPU provider is used, and speech continues normally

#### Scenario: GPU session creation fails

- **WHEN** a GPU provider is offered but the session cannot be created with it
- **THEN** the system retries with the CPU provider, logs the failure, and continues

#### Scenario: No GPU package installed

- **WHEN** only the CPU build of ONNX Runtime is installed
- **THEN** every mode still works and no import error surfaces

### Requirement: Piper load and provider diagnostics are reported

The system SHALL report, in the log and through the voice status available to the interface: the loaded Piper model file name, the active execution provider, and the reasons for any fallback. Diagnostics SHALL include lines of the form `[PIPER] Model loaded: jarvis-high.onnx` and `[PIPER] Execution provider: CPUExecutionProvider` (or `CUDAExecutionProvider` when a GPU is in use).

#### Scenario: Model load is reported

- **WHEN** the Piper voice finishes loading
- **THEN** the log names the loaded model file and the execution provider actually in use

#### Scenario: Fallback reason is reported

- **WHEN** a GPU request falls back to the CPU
- **THEN** the log states that the GPU provider was unavailable and which providers the runtime does offer

#### Scenario: Diagnostics are exposed to the interface

- **WHEN** the interface queries the voice status in Piper mode
- **THEN** it can read the active model and execution provider without touching the audio device
