## Purpose

Provides structured logging with correlation ids, latency/failure metrics per
subsystem, and optional Langfuse tracing — so failures can be diagnosed across
Gemini, orchestration, memory, computer control and coding agents without ever
logging secrets.

## ADDED Requirements

### Requirement: Structured logs with correlation fields
The system SHALL write structured logs carrying at minimum: timestamp, level,
component, task_id (when in a task), tool name, state, duration, result and error.
Existing user-facing activity-log behavior SHALL be preserved.

#### Scenario: Tool call is traceable
- **WHEN** a tool call fails
- **THEN** a log entry exists linking the tool name, task id, duration and error so the failure can be traced end-to-end

### Requirement: Subsystem latency and failure tracking
The system SHALL record latency and outcome for calls into: Gemini/model calls,
orchestration steps, memory retrieval, computer actions, coding-agent runs and
webcam inference, and SHALL expose the counters to diagnostics.

#### Scenario: Slow computer action is measurable
- **WHEN** a computer action takes noticeably long
- **THEN** its duration is recorded per action type and visible in diagnostics output

### Requirement: Optional Langfuse export
The system SHALL support exporting traces to Langfuse only when explicitly
configured (feature flag and keys present); without configuration it SHALL be a
complete no-op with zero runtime cost.

#### Scenario: Unconfigured Langfuse is a no-op
- **WHEN** the application runs without Langfuse configuration
- **THEN** no trace export occurs, no extra network calls happen, and startup time is unaffected

### Requirement: No secrets in logs or traces
The system SHALL NOT write API keys, tokens, passwords or other credentials to
logs or traces, and SHALL redact values matching known secret fields before
emission.

#### Scenario: Config dump is redacted
- **WHEN** a component logs configuration for diagnosis
- **THEN** secret-valued fields appear redacted and the actual keys are absent from the log file
