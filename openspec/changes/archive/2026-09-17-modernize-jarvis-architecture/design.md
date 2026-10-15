## Context

Mark LII is a working Gemini 2.5 Flash Live assistant: `main.py` (3,968 lines) owns
the Live session, audio I/O, deferred imports and an inline `_execute_tool`
dispatcher (~40 branches); `actions/` holds 20 tool modules; `brain/` already
provides a Brain facade, deterministic skill router (`skills.py`), task tracker and
sqlite memory; `core/` holds 22 service modules (voice/TTS/STT/wake, confirm/undo,
diagnostics, plugin loader); `memory/` is sqlite + `long_term.json`; `vision/` is a
camera/detection pipeline; `app/` + `dashboard/` host a FastAPI remote dashboard.
Permission gating is an if-chain in `main.py`; irreversible-action confirmation uses
UI-issued tokens (`core/confirm.py`). Git has **zero commits** — everything is
untracked, so a checkpoint commit must precede any move. The user confirmed: Cua via
adapter + local fallback; Aider active now with OpenHands optional (Docker-detected);
Langfuse/LiveKit/sherpa-onnx/Qdrant as config-gated adapters off by default; small
logical commits allowed.

## Goals / Non-Goals

**Goals:**
- One coherent agent: clear subsystem boundaries, a single orchestration brain, a
  single computer-control policy, a single approval service.
- Modularity without regressions: behavior-preserving moves validated after each batch.
- Optional heavy integrations (LangGraph, Mem0, Cua, OpenHands, Qdrant, Langfuse,
  LiveKit, sherpa-onnx) add capability when present and disappear when absent.
- Keep Gemini 2.5 Flash Live as the sole conversational/model layer.

**Non-Goals:**
- Replacing Gemini Live or the Piper/local voice pipeline.
- Rewriting working tools for style; only rerouting internals where the architecture
  requires it.
- Making any optional layer a hard dependency for boot or voice.
- Removing pyautogui/playwright/pywinauto/mss until usage is audited and replacements
  are tested.

## Decisions

- **One brain, two execution paths.** The `Brain` facade keeps its public API; a new
  `brain/graph.py` builds the LangGraph state machine lazily and only when
  `features.langgraph_orchestrator=true`. Deterministic routing stays default.
  *Alternative considered:* replacing the deterministic router — rejected: it is
  offline, instant and the model-visible prompt already depends on it.
- **Registry-based dispatcher.** `_execute_tool`'s inline branches move to a handler
  registry (`actions/registry.py` → per-module `register(dispatcher)`), preserving the
  deferred-import wait and the executor-thread hop that protects audio latency.
  *Alternative:* keeping the if-chain — rejected: it is the main growth point of
  `main.py`.
- **ComputerUse facade with two backends.** `computer/computer_use.py` defines the
  action contract (screen/mouse/keyboard/window/app/browser ops + verify); the local
  backend *wraps the proven code* inside `actions/computer_control.py`,
  `browser_control.py`, `open_app.py` (moved into `computer/`, not rewritten); the Cua
  backend lazily imports the Cua SDK behind an availability probe and is never a hard
  import. Post-action verification uses cheap observation (active-window title,
  element presence, screenshot diff where already captured).
- **Coding router behind one lock.** `coding/router.py` picks Aider (subprocess, pip
  native) or OpenHands (Docker/WSL2 probe via its CLI container interface, never
  imported); `coding/coding_lock.py` is a cross-process file lock per working tree.
  Coding runs in an isolated worktree or requires explicit approval for the live tree.
- **Distribution of `core/`.** Voice modules (`voice/tts/stt/hybrid_voice/wake/
  audio_devices/sfx`) → `voice/`; `confirm.py`/`undo.py` → `security/`; `diagnostics/
  performance/logging-adjacent` → `observability/`; `plugin_loader`, `installer`,
  `config_guard` → `app/`. Thin re-export shims keep third-party/plugin imports alive
  for one release.
- **UI stays PyQt6, moves as a package.** `ui.py` + `ui_layout/ui_settings/ui_theme.py`
  → `ui/` with `ui/__init__.py` re-exporting the application class so `main.py` shim
  keeps working; asset paths stay resolved from a single `ui/paths.py`.
- **Lifecycle owner.** `app/lifecycle.py` implements ordered init/shutdown with
  registered stop callbacks; `main.py` becomes a thin shim into `app/main.py`.
- **Config centralization.** `config/settings.py` merges `api_keys.json` + env vars +
  `.env.example` placeholders; all new flags live under a `features` map; no real
  credentials in the repo (`.gitignore` already excludes the secrets pattern).
- **Observability-first ordering.** Structured logging + correlation ids land *before*
  the LangGraph/Mem0 work so every later phase is measurable.
- **Phases follow the master prompt's 15-phase order** (§40), compressed where the
  codebase already provides the capability (vision, knowledge remain light).

## Risks / Trade-offs

- [Zero-commit git history] → checkpoint commit of the current tree before any move;
  per-phase commits after tests pass.
- [3,968-line main.py split can break session behavior] → split is mechanical
  (move code, keep call sites), validated by import checks + startup smoke + manual
  voice/session test before each commit; shims keep old import paths alive.
- [Four GUI backends consolidation changes timing-sensitive behavior] → the local
  backend initially wraps the *same* code unchanged (pure move), so timing is
  preserved; only the routing layer is new.
- [Optional deps bloat install] → all optional deps live in a commented
  `requirements-optional.txt`; base `requirements.txt` unchanged until the audit.
- [Cua adapter cannot be tested without the runtime] → adapter is contract-tested
  with a fake backend; availability probe tested for both absence and presence paths.
- [OpenHands Docker dependence] → probe caches its verdict; absence yields a clear
  setup message, never a crash.
- [Windows console/locale crashes (cp1254 etc.) during migration logging] → all new
  logging goes through the structured logger with UTF-8 + replace, as `main.py` does.

## Migration Plan

1. Checkpoint commit (current tree, zero diffs lost).
2. Phase A — observability + config/settings + `.env.example` (no moves).
3. Phase B — security service (`security/` wrapping existing confirm/undo/permission
   gate), consumed by the existing dispatcher (behavior identical).
4. Phase C — new packages `computer/`, `coding/`, `knowledge/` with facades; tool
   internals rerouted; tool contracts unchanged.
5. Phase D — `brain/` LangGraph graph (flag off), memory backend interface + Mem0
   adapter (flag off), self-inspection workflow.
6. Phase E — physical reorganization per the migration map (leaf modules first,
   `main.py` split last), shims for old paths, batch-validated.
7. Phase F — launcher updates (`start_jarvis.py`, `.bat/.vbs`, `main.pyw`), docs,
   dependency audit, test expansion, final report with the migration table.
Rollback: `git reset --hard <checkpoint>` per phase; each phase is independently
revertable.

## Open Questions

None — scoping decisions were confirmed with the user (Cua strategy, coding-agent
strategy, optional-stack depth, commit policy).
