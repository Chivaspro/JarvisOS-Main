## 1. Safety and baselines

- [x] 1.1 Create checkpoint commit of the entire current (untracked) tree on `master` before any change; verify `git status` is clean afterwards
- [x] 1.2 Write `.env.example` with placeholder-only variables (Gemini key, optional Mem0/Qdrant/Langfuse/Cua/OpenHands settings) and verify no real credentials appear anywhere in the repo
- [x] 1.3 Create `config/settings.py` centralizing config access (api_keys.json + env + feature flags incl. `langgraph_orchestrator`, `mem0_memory`, `cua_backend`, `langfuse_tracing`, `qdrant_knowledge`, `coding_agents`), and verify existing config consumers still read identical values

## 2. Observability

- [x] 2.1 Add structured logger (JSON lines, fields: timestamp/level/component/task_id/tool/state/duration/result/error) with UTF-8+replace output to `data/logs/`, and verify existing console behavior is unchanged
- [x] 2.2 Add latency/outcome counters per subsystem (model, orchestration, memory, computer, coding, vision) and verify counters appear in diagnostics output
- [x] 2.3 Add optional Langfuse trace adapter (no-op without flag+keys, lazy import) and verify zero network/startup cost when unconfigured
- [x] 2.4 Add secret redaction at the logging boundary with a unit test proving API-key-shaped values never reach the log file

## 3. Security and approvals

- [x] 3.1 Create `security/` package: risk-tier rule table (LOW/MEDIUM/HIGH per master prompt §22), approval modes (`always_confirm`, `high_risk_only` default, `never_confirm`, `custom`), wrapping existing permission gate + confirm-token mechanism with identical observable behavior; verify existing refusals still name the permission
- [x] 3.2 Route the current `_execute_tool` permission check through the security service and verify the existing permission-gate behaviors (mouse/keyboard, window, screen, terminal, camera, file) still pass their current tests/behavior
- [x] 3.3 Verify `never_confirm` still requires the UI-issued token for shutdown/restart/wifi/bulk-delete, and that a model-supplied `confirmed` parameter can never satisfy it

## 4. Computer control consolidation

- [x] 4.1 Create `computer/` package with `computer_use.py` facade (action contract: screen, mouse, keyboard, window, app, browser, verify) and verify import-only (no behavior change yet)
- [x] 4.2 Build `computer/local_backend.py` by moving (not rewriting) the general-purpose implementations from `actions/computer_control.py`, `actions/open_app.py`, `actions/computer_settings.py` window ops and `actions/desktop.py`; keep `actions/*.py` entry functions delegating to the facade with identical tool names/parameters/results, and verify every existing caller still works via import validation
- [x] 4.3 Build `computer/browser.py` by moving `actions/browser_control.py` internals: real-profile opens unchanged, isolated automation only where needed, API-over-GUI routing hint honored; verify browser tools return identical result shapes
- [x] 4.4 Implement `computer/cua_backend.py` adapter (lazy import, availability probe, config override `features.cua_backend=auto|on|off`) with a fake-backend contract test; verify absence on this machine selects local backend and diagnostics reports the active backend
- [x] 4.5 Add post-action verification (active window title / element presence / navigation URL) to mutating facade actions; verify a deliberately failing verification reports failure instead of silent success

## 5. Coding agents

- [x] 5.1 Create `coding/` package: `coding_lock.py` (cross-process file lock per working tree) with a unit test proving concurrent acquisition is refused
- [x] 5.2 Implement `aider_agent.py` (subprocess, native pip) and `agent_router.py` (focused→Aider, large→OpenHands, fallback chain), route `code_helper`/`dev_agent` through it with unchanged tool contracts; verify a focused edit runs via Aider and result reports the backend used
- [x] 5.3 Implement `openhands_agent.py` (Docker/WSL2 probe via OpenHands runtime CLI, cached verdict, never imported) and verify: absent Docker → clear setup message and Aider fallback; skill_query never advertises unavailable backends
- [x] 5.4 Require explicit approval for coding against JARVIS's own live tree and isolate other runs (worktree); verify self-tree task is held until approved

## 6. Memory and orchestration

- [x] 6.1 Introduce memory backend interface in `memory/` with the local store as default implementation; verify all existing recall/save/forget flows behave identically (existing data readable, no migration needed)
- [x] 6.2 Implement memory promotion policy (temporary/session/long-term/project/task) and verify durable facts promote while ephemeral turns do not (unit tests)
- [x] 6.3 Add Mem0 adapter behind `features.mem0_memory` (lazy import, key required) and verify: flag off → zero Mem0 usage; enabled+no network → falls back to local store with one degradation notice
- [x] 6.4 Add `brain/graph.py` LangGraph orchestrator (plan→execute→verify→replan, sqlite checkpointing, bounded retries) behind `features.langgraph_orchestrator` (default off); verify flag off keeps deterministic path byte-identical in behavior
- [x] 6.5 Verify orchestrator scenarios: failed-step replans with evidence, unverified success is rejected, restart resumes from checkpoint, LangGraph-missing → single degradation notice and deterministic fallback
- [x] 6.6 Emit task-state events (Planning/Executing/Verifying/Waiting for approval/Completed/Failed/Recovering) consumed by the HUD without blocking the UI thread; verify UI stays responsive during an orchestrated run

## 7. Knowledge and self-inspection

- [x] 7.1 Create `knowledge/` package: retriever over existing file tools (default path) and optional Qdrant adapter behind `features.qdrant_knowledge`; verify default behavior unchanged and enabled mode returns sources with passages
- [x] 7.2 Implement self-inspection workflow (source via filesystem, logs, process via psutil, rendered UI via computer facade; repair plan → coding router → visual verification) exposed as a tool; verify the workflow produces a repair plan before any edit and reports verification evidence

## 8. Repository organization (migration map execution)

- [x] 8.1 Write `docs/migration-map.md` with the full OLD→NEW→REASON table before any move, covering: core/voice*→voice/, core/confirm+undo→security/, core/diagnostics+performance→observability/, core/plugin_loader+installer+config_guard→app/, actions/computer_control+open_app+browser_control+desktop→computer/ (delegation shims remain in actions/), ui.py+ui_*.py→ui/, main.py→app/main.py (thin shim), start_jarvis.py/run_jarvis.*→scripts/ (root shims kept)
- [x] 8.2 Execute leaf-module moves (core/ voice group → voice/, security pair → security/, observability pair → observability/, app trio → app/) with re-export shims in old locations; verify import validation + startup smoke + affected tests pass, then commit
- [ ] 8.3 Move UI package (ui.py, ui_layout.py, ui_settings.py, ui_theme.py → ui/ with `ui/__init__.py` re-exports and `ui/paths.py` for asset paths); verify HUD opens with theme, waveform, panels and asset paths intact, then commit
- [ ] 8.4 Split main.py into app/main.py (entry) + app/lifecycle.py (ordered init/shutdown with stop callbacks) + brain/dispatch glue, leaving a thin main.py shim; verify `python main.py` boots to READY, voice connects, one tool call executes, and shutdown leaves no orphans, then commit
- [ ] 8.5 Move launcher scripts to scripts/ with root shims (`start_jarvis.py`, `run_jarvis.bat`, `run_jarvis.vbs`, `main.pyw` still work from repo root); verify double-click launch path reaches READY, then commit
- [ ] 8.6 Enforce dependency-direction checks (UI does not import computer/memory/providers directly) via a test that walks the import graph; verify it fails on a deliberately bad import and passes on the real tree

## 9. Vision, voice adapters, performance

- [ ] 9.1 Refactor webcam pipeline into the vision manager's throttle/skip policy (frame skipping, bounded queues) and verify the webcam UI stays responsive during inference; no model changes
- [ ] 9.2 Add evaluation notes + no-op adapter stubs for LiveKit Agents and sherpa-onnx behind disabled flags (no dependencies installed by default); verify zero startup impact
- [ ] 9.3 Run a performance pass: verify no duplicate Gemini requests, one active GUI controller, lazy optional imports, and startup time not regressed vs. checkpoint (measure with the existing [STARTUP] markers)

## 10. Dependency audit and cleanup

- [ ] 10.1 Audit imports of pyautogui/playwright/pywinauto/pywin32/mss and record usage in the migration map; remove a dependency only when no code uses it and its replacement is tested; verify `pip check` and startup pass afterwards
- [ ] 10.2 Add optional dependencies as a commented `requirements-optional.txt` (langgraph, mem0ai, aider-chat, qdrant-client, langfuse); verify base requirements.txt still installs a working JARVIS
- [ ] 10.3 Remove dead code (starting with `jarvis_kokoro.py`, `patch_v9.py`) only after a reference search proves zero usage; verify import validation and tests pass after each removal

## 11. Testing

- [ ] 11.1 Unit tests: security rule table + modes, memory policy, coding router + lock, computer facade (fake backends), router/registry parity with the old dispatcher branches
- [ ] 11.2 Failure-mode tests: Gemini unavailable, Cua unavailable, Mem0 unavailable, OpenHands unavailable, Aider unavailable, web search unavailable, camera unavailable, network unavailable — each verifies graceful degrade and session survival
- [ ] 11.3 Integration: import-graph validation, startup smoke to READY, dispatcher registry parity, shutdown orphan check; verify `pytest tests/` passes green

## 12. Documentation and final report

- [ ] 12.1 Update README and write docs/architecture.md, docs/setup.md, docs/tools.md, docs/troubleshooting.md (env vars, feature flags, startup/shutdown, known limitations)
- [ ] 12.2 Produce the final report: architecture summary, original/final structure, files moved/changed/removed, modules merged, dependencies added/removed, env vars, install/start/shutdown commands, test results, performance observations, known limitations, remaining debt, rollback instructions, and the complete OLD→NEW→REASON→STATUS migration table
- [ ] 12.3 Run end-to-end validation: boot to READY via launcher, voice round-trip, one orchestrated task (flag on), one Aider task, one computer-facade action with verification, memory recall across restart; verify all pass before marking the change complete
