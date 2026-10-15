# Modernize JARVIS Architecture — modular orchestration, memory, computer control, coding agents

## Why

Mark LII works today, but it is architected as a single 3,968-line `main.py` with an
inline tool dispatcher, plus a set of parallel subsystems (`brain/`, `memory/`, `core/`,
`actions/`) that overlap in responsibility. Every new capability lands in `main.py`
or `actions/`, so the codebase grows monolithic instead of modular: tool routing
policy lives in prompt text, permission gating is a hand-maintained if-chain,
computer control is split across four automation backends with no shared policy,
and there is no coding-agent layer, no external long-term memory, and no observability.
The user wants JARVIS to evolve into a single coherent agent with clear subsystem
boundaries before adding orchestration, memory, and computer-use capabilities on top.

## What Changes

- **Orchestration layer (`brain/`)**: introduce LangGraph as the central orchestrator
  behind the existing `Brain` facade — planner → executor → verifier → replanner graph
  with checkpointing, gated by a config flag (`features.langgraph_orchestrator`, default
  **off**). The deterministic skill router and task tracker already in `brain/` remain
  the always-on path; the graph adds multi-step execution, verification and recovery for
  complex goals. One brain, two execution paths, clean hand-off — no competing orchestrator.
- **Memory layer (`memory/`)**: add Mem0 as an optional long-term memory backend behind
  the existing `BrainMemory` interface (config-gated, default off). Introduce an explicit
  memory policy (temporary / session / long-term / project / task) so only durable facts
  are promoted; local sqlite store remains the default and the fallback.
- **Computer control layer (`computer/`)**: consolidate the four competing automation
  backends (pyautogui via `computer_control.py`, Playwright via `browser_control.py`,
  app launching via `open_app.py`, window ops via `computer_settings.py`/`desktop.py`)
  behind one `computer_use.py` facade with a **pluggable backend design**: a Cua adapter
  (auto-enabled when the Cua runtime is detected and supports the host OS) and a
  consolidated local backend (pyautogui + pywinauto + Playwright-for-browser) as the
  default on Windows. Tool-level entry points (`computer_control`, `browser_control`,
  `open_app`, `desktop_control`, `computer_settings`) keep their names and signatures —
  only their internals are rerouted. **BREAKING (internal only)**: direct imports of
  `actions.computer_control`, `actions.browser_control`, `actions.open_app` outside the
  tool layer must switch to the `computer/` facade.
- **Coding agent layer (`coding/`)**: add a coding agent router — **Aider** as the active
  backend for focused edits (native on Windows), **OpenHands** as an optional backend for
  autonomous multi-file tasks (Docker/WSL2 detected; degrades with a clear message when
  absent). A coding-lock prevents concurrent edits; existing `code_helper`/`dev_agent`
  tools route through the router.
- **Self-inspection workflow**: a `self_inspect` capability that combines source/log
  inspection (filesystem), running-process inspection (psutil) and rendered-UI inspection
  (computer facade) into a repair plan executed by the coding router, with visual
  verification through the computer facade.
- **Observability**: structured logging with task/component correlation fields; optional
  Langfuse tracing behind a config flag, no-op when unconfigured.
- **Permissions/approvals**: extend the existing `_permission_gate` + UI-issued confirm
  tokens into a reusable risk-tiered approval service (low/medium/high) consumed by the
  orchestrator, coding agents and computer facade; presets `always_confirm`,
  `high_risk_only`, `never_confirm`, `custom` (never bypasses the UI-token gate for
  irreversible actions).
- **Repository organization**: fold the current flat layout into the target structure
  (`app/ brain/ memory/ computer/ coding/ vision/ voice/ actions/ knowledge/ observability/
  security/ ui/ config/ tests/ scripts/ docs/`) via a migration map — moves are behavior-
  preserving, import-updating, and validated by an import/startup check after each batch.
  Existing `vision/`, `brain/`, `memory/`, `config/`, `app/` packages are extended rather
  than rebuilt; `ui.py`/`ui_*.py` move under `ui/`; `core/` services are distributed to
  `voice/` (audio/TTS/STT/wake), `observability/`, `security/`, `app/`.
- **Knowledge layer (optional, config-gated)**: Qdrant adapter for searchable documents
  with a local fallback; off by default.
- **Voice**: keep Gemini 2.5 Flash Live as-is; LiveKit Agents and sherpa-onnx evaluated
  as optional adapters behind flags, not replacements.
- **Config/env hygiene**: `.env.example` with placeholders; secrets stay in
  `config/api_keys.json` (gitignored pattern preserved); no real credentials in docs.
- **Testing**: import/startup smoke tests, unit tests for router/planner/verifier/permissions,
  failure-mode tests (Gemini/Cua/Mem0/Aider/OpenHands/network unavailable → graceful degrade),
  plus a final report with the full migration table.

## Capabilities

### New Capabilities

- `orchestration`: LangGraph-based task orchestration (plan → execute → verify → replan),
  routing policy, checkpointing and graceful degradation to the deterministic router.
- `memory-policy`: memory promotion policy and pluggable long-term memory backends
  (local store default, Mem0 optional) with backend-agnostic read/write semantics.
- `computer-control`: unified computer-use facade with pluggable backends (Cua adapter
  optional, local automation default), browser integration preserving the user's real
  profile, and verification of executed actions.
- `coding-agents`: coding task routing (Aider/OpenHands), exclusive workspace locking,
  and safe degradation when a backend is unavailable.
- `self-inspection`: repository/log/running-app/UI self-inspection producing a verified
  repair plan via the coding agents.
- `security-approvals`: risk-tiered permission and approval service for tools, computer
  control and coding agents, with UI-issued confirmation for irreversible actions.
- `observability`: structured logging with correlation ids, latency/failure tracking per
  component, and optional Langfuse export (no secrets in logs).
- `repo-organization`: the enforced target package layout, dependency-direction rules and
  the migration/validation loop that keeps moves behavior-preserving.

### Modified Capabilities

- `voice-output-routing`: startup/shutdown lifecycle moves into the new lifecycle owner
  (`app/lifecycle.py`); the voice output chain itself is unchanged. Startup validation
  gains the same graceful-degrade guarantees (voice works even when optional subsystems
  are missing).

## Impact

- **Code**: `main.py` (dispatcher + session split out), `actions/*` (tool internals →
  facade calls), `core/*` (split into voice/observability/security/app), `brain/*`,
  `memory/*`, `vision/*`, new `computer/`, `coding/`, `knowledge/`, `security/`,
  `observability/`, `ui/`, `scripts/`, `docs/`, `tests/`.
- **Dependencies**: add `langgraph`, `mem0ai`, `aider-chat`, `qdrant-client`,
  `langfuse` (all optional-use, config-gated); Cua adapter imports lazily behind
  availability detection; OpenHands used via its CLI/container interface, not imported.
  Audit pyautogui/playwright/pywinauto/mss before any removal — none removed blindly.
- **Startup**: `main.py` → thin entry importing `app/main.py`; `start_jarvis.py`,
  `run_jarvis.bat/.vbs`, `main.pyw` updated to the new entry; `app/lifecycle.py` owns
  ordered init/shutdown with no orphan processes.
- **Data**: `data/logs/`, `data/cache/`, `data/runtime/` introduced; existing
  `memory/long_term.json` and sqlite schema preserved (no data migration required).
- **Docs**: README, `docs/architecture.md`, `docs/setup.md`, `docs/tools.md`,
  `docs/troubleshooting.md`; final report with the full migration table.
- **Tests**: `tests/unit`, `tests/integration`, `tests/computer`, `tests/vision`,
  `tests/brain`, `tests/ui` (pytest already in use under `tests/`).
