## Purpose

Defines the target package layout and the rules that keep the migration behavior-
preserving: a migration map for every moved file, dependency-direction constraints
between layers, and a validation loop (imports, startup, tests, checkpoint commit)
after each batch of moves.

## ADDED Requirements

### Requirement: Target package layout
The repository SHALL be organized into the following packages, each owning a single
responsibility: `app` (entry/lifecycle), `brain` (orchestration/state/routing),
`memory`, `computer`, `coding`, `vision`, `voice`, `actions` (tool implementations),
`knowledge`, `observability`, `security`, `ui`, `config`, `tests`, `scripts`,
`docs`, `data`. UI code SHALL NOT live in `brain/`; memory code SHALL NOT live in
`brain/`; provider-specific integration SHALL sit behind adapters.

#### Scenario: Layout inspection
- **WHEN** the repository is inspected after migration
- **THEN** each package contains only code of its declared responsibility and no duplicate implementations of the same subsystem exist

### Requirement: Dependency direction
Dependencies SHALL flow UI → app → brain → services (memory/computer/coding/vision/
voice/knowledge) → tools/infrastructure. UI code SHALL NOT import computer control,
memory or model providers directly; it SHALL go through the application/event layer.

#### Scenario: Layering check
- **WHEN** the import graph is analyzed
- **THEN** no UI module imports a computer/memory/model-provider module directly

### Requirement: Migration map before moves
Every file move SHALL be recorded in a migration map (old location → new location →
reason → status) maintained with the change, and no file SHALL be removed without a
reference search proving it unused.

#### Scenario: Migration table is complete
- **WHEN** the change is completed
- **THEN** every moved, changed or removed file appears in the final migration table with its status

### Requirement: Validation loop after each batch
After each batch of file moves the migration SHALL: update all imports, update
startup scripts and asset paths, run an import validation, run the application's
startup smoke check, run affected tests, and record a checkpoint commit — before
the next batch proceeds.

#### Scenario: Broken import stops the batch
- **WHEN** an import validation fails after a move batch
- **THEN** the batch is fixed before any further moves are made

### Requirement: Entry points keep working
The user-facing launchers (`main.py`/`main.pyw`, `start_jarvis.py`, `run_jarvis.bat`,
`run_jarvis.vbs`) SHALL continue to start the application with the same user
experience, delegating to the new `app/` entry internally.

#### Scenario: Double-click launcher still works
- **WHEN** the user starts JARVIS via the existing .bat/.vbs launcher
- **THEN** the application reaches READY with the HUD visible as before the migration
