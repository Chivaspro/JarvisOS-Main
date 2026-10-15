## Purpose

Adds a coding-agent layer that routes code work to the right backend — Aider for
focused, precise edits; OpenHands for larger autonomous multi-file tasks — with an
exclusive workspace lock, capability probing, and safe degradation when a backend
is unavailable.

## ADDED Requirements

### Requirement: Coding task routing
The system SHALL route coding requests by scope: focused single-file or precise
refactoring edits to Aider; larger autonomous multi-file tasks, debugging sessions
and project-level implementations to OpenHands when it is available. The existing
`code_helper` and `dev_agent` tools SHALL route through this router, keeping their
tool names and parameter contracts unchanged.

#### Scenario: Focused edit
- **WHEN** the user asks for a small targeted change in one file
- **THEN** the request is executed by Aider

#### Scenario: Autonomous multi-file task
- **WHEN** the user asks for a project-level implementation or multi-file change and OpenHands is available
- **THEN** the request is executed by OpenHands

#### Scenario: OpenHands unavailable falls back to Aider
- **WHEN** a large task arrives and OpenHands is not available but Aider is
- **THEN** the router falls back to Aider and the result still reports which backend ran

### Requirement: Exclusive coding lock
The system SHALL hold an exclusive lock per working tree while a coding backend
runs, and SHALL refuse concurrent coding-agent execution on the same tree with a
clear busy message. Aider and OpenHands SHALL NOT edit the same working tree
simultaneously.

#### Scenario: Concurrent request refused
- **WHEN** a coding task is running and a second coding request targets the same working tree
- **THEN** the second request is refused with a busy indication and the running task is unaffected

### Requirement: Capability probing before offering
The system SHALL probe backend availability (Aider installed; OpenHands reachable
via its runtime) before offering or promising a coding capability, and the skill
query SHALL NOT advertise backends that are unavailable.

#### Scenario: Skill query reflects reality
- **WHEN** the user asks what coding capabilities exist while OpenHands is not installed
- **THEN** Aider is offered and OpenHands is reported as unavailable with its setup requirement

### Requirement: Safe degradation
If the selected backend fails to start or errors, the system SHALL report the
failure with the backend's error evidence, SHALL NOT crash the session, and MAY
fall back to the other backend only when the task's scope still fits it.

#### Scenario: Backend crash
- **WHEN** Aider terminates unexpectedly mid-task
- **THEN** the user receives a failure report with the captured output and the session continues

### Requirement: Isolation and approval
Coding agents SHALL run against an isolated or explicitly user-confirmed target:
never JARVIS's own live tree without the user's approval, and never with the
approval service bypassed for medium/high-risk operations.

#### Scenario: Editing JARVIS itself requires approval
- **WHEN** a coding task targets the assistant's own installation tree
- **THEN** execution is held until the user explicitly approves it
