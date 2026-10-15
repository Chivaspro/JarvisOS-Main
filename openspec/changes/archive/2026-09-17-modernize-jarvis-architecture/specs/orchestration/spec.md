## Purpose

Enables LangGraph-based task orchestration for complex multi-step goals — plan →
execute → verify → replan with checkpointing — while keeping the existing
deterministic skill router as the always-on default path so the assistant never
loses capability when the optional orchestrator is disabled or unavailable.

## ADDED Requirements

### Requirement: LangGraph orchestrator is config-gated and off by default
The system SHALL enable the LangGraph orchestration path only when the feature flag
`features.langgraph_orchestrator` is explicitly true in configuration, and SHALL use
the existing deterministic skill router and task tracker as the default execution
path otherwise. The flag SHALL be readable from the UI Settings at runtime.

#### Scenario: Flag off (default)
- **WHEN** a multi-step request arrives and `features.langgraph_orchestrator` is not set or is false
- **THEN** the request is handled by the existing deterministic router/task-tracker path with identical behavior to today

#### Scenario: Flag on
- **WHEN** a request arrives with `features.langgraph_orchestrator=true`
- **THEN** complex multi-step goals are planned and executed through the LangGraph graph while single-step tool calls continue to dispatch directly

### Requirement: Orchestrator graph covers plan, execute, verify, replan
When enabled, the orchestrator SHALL execute goals through a graph that plans steps,
executes one step at a time, observes the result, verifies the outcome against the
step's intent, and on failure either retries via replanning (bounded) or reports the
failure to the user. The orchestrator SHALL NOT mark a step complete merely because
its tool call returned without an exception.

#### Scenario: Successful multi-step goal
- **WHEN** every planned step executes and verification passes
- **THEN** the goal is marked complete and a summary is returned to the conversational layer

#### Scenario: Failed step recovers
- **WHEN** a step's verification fails
- **THEN** the orchestrator replans with the observed error, up to a bounded number of attempts, and only then reports failure with the evidence gathered

#### Scenario: Unverified success is rejected
- **WHEN** a tool call returns without an exception but the observable outcome contradicts the step's intent
- **THEN** the step is NOT marked complete and enters the replan path

### Requirement: Checkpointing and recovery
The orchestrator SHALL persist task state (goal, plan, completed steps, errors) at
each step boundary so an interrupted task can be resumed, and SHALL surface the
resumable state to the model through the existing task-memory context block.

#### Scenario: Task survives a session restart
- **WHEN** a task is in flight and the application restarts
- **THEN** the task state is available and the model is informed of the remaining steps instead of starting over

### Requirement: Graceful degradation when LangGraph is unavailable
If the LangGraph package is missing, fails to import, or errors at runtime, the
system SHALL fall back to the deterministic router path, log the degradation once,
and SHALL NOT crash the application or the active session.

#### Scenario: LangGraph not installed
- **WHEN** the flag is enabled but the langgraph package cannot be imported
- **THEN** requests proceed through the deterministic path and a single degradation notice is logged

### Requirement: Task state events are visible in the UI
While the orchestrator runs, the system SHALL emit task-state transitions
(Planning, Executing, Verifying, Waiting for approval, Completed, Failed,
Recovering) that the UI can display, without blocking the UI thread.

#### Scenario: User sees live progress
- **WHEN** an orchestrated task transitions between states
- **THEN** the UI state indicator reflects the new state within one event-loop iteration and the UI remains responsive
