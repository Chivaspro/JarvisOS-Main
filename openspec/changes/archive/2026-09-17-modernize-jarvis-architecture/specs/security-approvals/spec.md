## Purpose

Extends the existing permission gate and UI-issued confirmation tokens into a
reusable risk-tiered approval service used by tools, the computer-use facade and
the coding agents — so irreversible or high-risk actions always require a human
press, with modes that never bypass the gate.

## ADDED Requirements

### Requirement: Risk-tiered classification
The approval service SHALL classify actions into LOW, MEDIUM and HIGH risk tiers
covering at least: LOW — read screen, search web, open application, read file,
inspect project; MEDIUM — edit/delete file, install software, change settings, run
shell commands; HIGH — send email/message, purchase, bulk deletion, modify security
settings, execute dangerous commands, control security software.

#### Scenario: Classification is deterministic
- **WHEN** an action is submitted for approval
- **THEN** its risk tier is derived from the action type and parameters by a rule table, not by model judgment alone

### Requirement: Approval modes
The service SHALL support user-selectable approval modes: `always_confirm`,
`high_risk_only` (default), `never_confirm`, and `custom` (per-permission
overrides). In `never_confirm` mode irreversible actions SHALL still require the
UI-issued confirmation token.

#### Scenario: Default mode gates high risk only
- **WHEN** the mode is `high_risk_only` and a MEDIUM-risk file edit runs
- **THEN** it proceeds without interruption while a HIGH-risk send-message waits for the user's confirmation

#### Scenario: never_confirm still gates irreversible actions
- **WHEN** the mode is `never_confirm` and a shutdown or bulk deletion is requested
- **THEN** the UI-issued confirmation is still required before execution

### Requirement: UI-issued tokens for irreversible actions
Irreversible actions (shutdown, restart, wifi toggle, bulk deletes, high-risk sends)
SHALL execute only with a confirmation token issued by the UI after a human press;
the system SHALL NOT accept a model-supplied confirmation parameter as proof of
human approval.

#### Scenario: Model cannot self-confirm
- **WHEN** the model passes `confirmed=true` as a tool parameter for a shutdown
- **THEN** execution is still withheld until the user presses CONFIRM in the UI

### Requirement: Refusals name the permission
When a permission is revoked or an approval is denied, the refusal SHALL name the
switched-off permission or denied action, matching today's behavior where JARVIS
says what it did not do instead of quietly proceeding.

#### Scenario: Revoked permission refusal
- **WHEN** a tool runs while its Settings permission is off
- **THEN** the refusal message names that permission and no action executes

### Requirement: Service is shared by all subsystems
The orchestrator, computer-use facade, coding agents and tool dispatcher SHALL all
consume the same approval service, so risk policy is defined once and cannot be
bypassed by choosing a different subsystem.

#### Scenario: Coding agent inherits risk policy
- **WHEN** a coding task includes running tests or installing dependencies
- **THEN** those sub-actions pass through the same approval service as direct tool calls
