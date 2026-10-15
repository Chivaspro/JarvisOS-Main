## Purpose

Separates persistent memory from orchestration: defines a memory promotion policy
(temporary / session / long-term / project / task) and makes the long-term memory
backend pluggable — local sqlite/JSON store by default, Mem0 as an optional,
config-gated backend — behind one backend-agnostic interface.

## ADDED Requirements

### Requirement: Long-term memory backend is pluggable and defaults to the local store
The system SHALL expose long-term memory read/write through a single backend-
agnostic interface whose default implementation is the existing local store
(sqlite + `memory/long_term.json`), and SHALL route memory operations to the Mem0
backend only when `features.mem0_memory=true` and a Mem0 API key is configured.

#### Scenario: Default configuration
- **WHEN** memory is written or read with default configuration
- **THEN** operations are served by the local store with unchanged observable behavior

#### Scenario: Mem0 enabled
- **WHEN** `features.mem0_memory=true` and a Mem0 API key is present
- **THEN** promoted long-term facts are written to and retrieved from Mem0, while session-scoped context stays local

### Requirement: Memory promotion policy
The system SHALL classify memory into temporary context, session memory, long-term
memory, project memory and task memory, and SHALL promote information to long-term
memory only when it is durable and useful (identity, preferences, project facts,
important decisions, contacts/entities, successful recurring workflows). The system
SHALL NOT store every conversation turn blindly.

#### Scenario: Durable fact is promoted
- **WHEN** the user states a durable preference or fact (e.g. "my sister's name is Ayşe")
- **THEN** it is stored as a long-term fact and is recallable in later sessions

#### Scenario: Ephemeral chatter is not promoted
- **WHEN** a conversation turn contains only transient context (e.g. "open Chrome now")
- **THEN** it is not written to long-term memory

### Requirement: Backend failure degrades to the local store
If the Mem0 backend is unreachable, unconfigured, or errors, the system SHALL fall
back to the local store for that operation, log the degradation once per session,
and SHALL NOT fail the user's request or crash the session.

#### Scenario: Mem0 unavailable
- **WHEN** Mem0 is enabled but the network call fails
- **THEN** the memory operation completes against the local store and a single degradation notice is logged

### Requirement: Task state and long-term memory remain distinct
The system SHALL keep current-task state (goal, steps, errors) in the orchestrator's
task state, separate from long-term memory, and SHALL NOT persist in-flight task
steps as durable facts unless the task finishes successfully and the workflow is
reusable.

#### Scenario: Failed task does not pollute memory
- **WHEN** a multi-step task is abandoned or fails
- **THEN** its intermediate steps remain in task state only and no partial workflow is promoted to long-term memory

### Requirement: Existing memory data is preserved
The migration to the backend interface SHALL NOT require a data migration: existing
`memory/long_term.json` contents and the sqlite memory database SHALL remain
readable and unchanged.

#### Scenario: Upgrade with existing memory
- **WHEN** the application starts after the change with pre-existing memory data
- **THEN** all previously stored facts are recalled exactly as before
