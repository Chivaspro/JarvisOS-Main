## Purpose

Consolidates all real-computer interaction — screen, mouse, keyboard, windows,
applications, browser — behind one computer-use facade with pluggable backends:
a Cua adapter (enabled when the Cua runtime is detected) and a consolidated local
automation backend as the Windows default, so exactly one controller drives the
machine at a time and every action is verified.

## ADDED Requirements

### Requirement: Single computer-use facade
The system SHALL route all general-purpose GUI operations (screen capture, mouse,
keyboard, clicking, typing, scrolling, window management, application launching,
desktop operations) through one computer-use facade. The system SHALL NOT run two
GUI controllers against the same desktop simultaneously.

#### Scenario: Tool call routes through the facade
- **WHEN** a Gemini tool call (computer_control, browser_control, open_app, desktop_control, computer_settings) executes a GUI operation
- **THEN** the operation is performed by the facade's active backend and the tool's name and externally visible result format are unchanged

#### Scenario: No competing controllers
- **WHEN** a GUI action is executing
- **THEN** no other backend concurrently performs GUI operations for the same request

### Requirement: Backend selection and Cua adapter
The facade SHALL select its backend at startup: the Cua backend when the Cua
runtime is detected and supports the host OS, otherwise the consolidated local
backend. When Cua is selected, GUI operations SHALL be executed through the Cua
adapter; when it is not, the same operations SHALL run locally without behavior
regression. Backend selection SHALL be overridable by configuration.

#### Scenario: Cua not installed (default)
- **WHEN** the application starts on Windows without the Cua runtime
- **THEN** the local backend serves all GUI operations and no Cua-specific failure appears

#### Scenario: Cua detected
- **WHEN** the Cua runtime is installed and supports the host OS
- **THEN** GUI operations execute through the Cua adapter and the active backend is reported in diagnostics

### Requirement: Browser control preserves the user's session
Browser automation SHALL prefer the user's existing authenticated browser profile
for open/search/navigation tasks, and SHALL use an isolated automation context only
for tasks that require it. When a reliable direct API or tool exists for a task,
the system SHALL NOT use GUI automation for it.

#### Scenario: Open a website
- **WHEN** the user asks to open a website
- **THEN** the user's own browser opens it with their real profile, as today

#### Scenario: API over GUI
- **WHEN** a web task is achievable through a reliable API already present (e.g. web search, YouTube lookup)
- **THEN** the API/tool path is used instead of GUI automation

### Requirement: Actions are verified, not assumed
The facade SHALL verify that an action achieved its intent (e.g. post-action screen
observation for GUI mutations, response checks for browser navigation) and SHALL
report verification failure as a failed action rather than silent success.

#### Scenario: Typing lands in the field
- **WHEN** a type-into-field action completes
- **THEN** the facade confirms the target field contains the text and reports failure otherwise

#### Scenario: Navigation verified
- **WHEN** a browser navigation action completes
- **THEN** the facade confirms the target URL/page loaded before reporting success

### Requirement: Tool entry points remain stable
Migration to the facade SHALL keep the tool names and parameter contracts
(computer_control, browser_control, open_app, desktop_control, computer_settings)
unchanged, so Gemini declarations, permission gating and plugin callers keep
working without modification.

#### Scenario: Existing callers keep working
- **WHEN** the dispatcher invokes the tools after migration
- **THEN** results are returned in the same shape as before the migration

### Requirement: Permission gating is preserved
Every facade action SHALL pass through the existing per-permission gates
(mouse/keyboard, window control, screen understanding, terminal control) before
execution, with refusals naming the switched-off permission exactly as today.

#### Scenario: Revoked permission blocks the action
- **WHEN** mouse/keyboard control is switched off in Settings and a click is requested
- **THEN** the action is refused with the permission name and nothing is executed
