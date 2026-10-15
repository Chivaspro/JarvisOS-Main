# self-inspection Specification

## Purpose

Gives JARVIS a self-inspection workflow: inspect its own source, logs and running
processes, compare implementation against observed behavior, produce a repair
plan, execute it through the coding agents, and verify the fix by re-running tests.

## Requirements

### Requirement: Self-inspection covers source, logs and process
The system SHALL provide a self-inspection workflow that can inspect: the
repository source (via the filesystem, never by OCR of screenshots when the actual
files are available), application logs (structured log files), and the running
application (process and subsystem status).

#### Scenario: Inspect a reported defect
- **WHEN** the user reports behavior that appears broken
- **THEN** the workflow gathers the relevant source files, recent logs and process state before proposing a repair

#### Scenario: Source is preferred over screenshots
- **WHEN** the defect concerns code behavior and the source files are accessible
- **THEN** inspection reads the files directly instead of inferring from screen captures

### Requirement: Repair plan before execution
Self-inspection SHALL produce an explicit repair plan (findings, affected files,
proposed changes, verification steps) before any code modification, and SHALL
execute the plan through the coding-agent router with the exclusive coding lock.

#### Scenario: Plan is presented
- **WHEN** self-inspection completes its analysis
- **THEN** a repair plan with concrete files and changes is produced before edits begin

### Requirement: Verification after repair
After a repair executes, the system SHALL verify the outcome by re-running the
affected tests; unresolved failures SHALL be
reported rather than marked fixed.

#### Scenario: Post-repair verification
- **WHEN** a repair completes
- **THEN** the workflow re-runs the affected tests and only reports success if they pass

#### Scenario: Repair failed
- **WHEN** post-repair verification fails
- **THEN** the failure is reported with evidence and the change is not marked as a success in memory

### Requirement: Learning from successful repairs
When a self-inspection repair is verified successful, the system MAY record the
workflow as a reusable experience in memory so future similar defects are repaired
faster; failed repairs SHALL NOT be recorded as successes.

#### Scenario: Successful repair is remembered
- **WHEN** a repair passes verification
- **THEN** the successful workflow is available to recall for similar future defects
