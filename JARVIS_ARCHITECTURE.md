# J.A.R.V.I.S. — Architecture

> A desktop-first, voice-controlled assistant for Windows / macOS / Linux:
> native voice, vision, memory, coding agents, PC/web control and a futuristic
> Qt HUD. This document describes how the **current** codebase is put together —
> the real files, the real data flow, and the guarantees each layer holds.

*Last verified against the tree: 2026-10. Section §3 explains the one-release
`core/` aliases left behind by the `modernize-jarvis-architecture` change, which
is the main reason older notes about "where things live" no longer match.
Section §19 reflects the venv-isolation rewrite of the dependency installer.*

---

## 1. Design principles

These rules are visible everywhere in the tree and explain most design choices:

1. **Degrade, never crash.** An optional engine that is missing (psutil, a
   vision backend, a cloud key) reports what is missing and keeps running; it
   never takes the app down. Nearly every cross-boundary call is guarded.
2. **Probe for real, never substitute.** A control engine is probed before use;
   a missing one is *named*, not silently replaced by another. Every result
   says which engine actually ran.
3. **One writer per piece of state.** Config is written through
   `app/config_guard.py`; feature flags through `config/settings.py:set_feature`;
   logs through `observability/logger.py`. Readers share one cached view.
4. **Keep the boot cheap.** Heavy modules are imported on first use: the
   `app/lazy.py` proxy for third-party modules, and `main.py:_DeferredModules`
   for the command-action tail, which loads *concurrently with the network
   connect* instead of before the window appears.
5. **Fail-safe observability & recovery.** Logging, metrics, tracing, the crash
   guard and the self-heal sentry may observe anything but must never become the
   thing that breaks the assistant.
6. **Move, don't break.** A module may change home; its callers must not. The
   architecture-modernization pass relocated modules into their owning packages
   and left transparent aliases in `core/` for one release (see §3).

---

## 2. Top-level map

```text
JARVIS-OS/
├── main.py                 # desktop launcher + Live voice session + tool schema (~60 tools)
├── main.pyw                # Windows GUI entry (no console)
├── start_jarvis.py         # stdlib-only interpreter resolver + dependency installer
├── setup_jarvis.py         # interactive Setup & Control Center (stdlib-only)
├── launch_ui.py            # console/splash progress renderer for the launcher chain
├── startup_optimizer.py    # pre-boot temp/cache cleanup (before UI/backend load)
├── Closed_Jarvis.py        # recovery tool: clear an orphaned background instance
├── run_jarvis.bat / .vbs   # double-click launchers (delegate to start_jarvis.py)
├── setup_jarvis.bat        # one-click wizard launcher
├── install_dependencies.bat  # creates .venv + installs requirements.txt into it
├── requirements.txt        # runtime deps, grouped by subsystem (optional engines on demand)
├── package.json            # Node host manifest for Agent TARS (@agent-tars/cli)
├── .venv/                  # local Python virtual environment (gitignored; delete to reset)
│
├── ui.py                   # the HUD: main window, panels, paint, threads
├── ui_settings.py          # Settings / Control Center window
├── ui_layout.py            # Layout Studio (the drag-and-drop editor)
├── ui_layout_core.py       # element engine (images, effects, glow, stroke, …)
├── ui_layout_parts.py      # Studio widgets / palettes
├── ui_theme.py             # colours, fonts, design tokens
├── ui_mcp_panel.py         # MCP servers panel
├── ui_neural_map.py        # Memory Core → Neural Map
├── ui_vault_panel.py       # Obsidian vault / memory-backend panel
├── ui_perfomance.py        # Settings ▸ PERFORMANCE: presets · toggles · scan
├── run_layout_editor.py    # standalone Layout Studio launcher (no full app)
├── figma_export.py         # export a Studio layout as a Figma-importable SVG
│
├── actions/                # command modules (web search, files, media, reminders, …)
├── app/                    # providers · model router · LLM client · plugin loader ·
│                           #   config guard · installer · lazy proxy · brain bridge ·
│                           #   optional FastAPI service (app/main.py, app/core/)
├── brain/                  # reasoning: engine, skills, graph, context, learning, tasks
├── capabilities/           # PC/browser control: router, controllers, registry, locks
├── coding/                 # Aider / OpenHands routing + coding lock
├── config/                 # settings.py, api_keys.json, voice_config.json, voices/,
│                           #   certs/ and backups/ (generated), ui_layout.json
├── connect/                # phone/remote gateway (pairing, WebSocket transport, tools)
├── connectors/             # the single Composio connector (external apps behind 1 tool)
├── core/                   # runtime primitives + one-release import shims (§3)
├── dashboard/              # local HTTP dashboard, 3D brain view, static assets
├── documents/              # Word/Excel/PowerPoint/PDF deliverable builders + tool
├── evolution/              # self-evolution: forge · crucible · vault · registry · dgm/
├── knowledge/              # local corpus search + optional Qdrant
├── mcp_host/               # MCP server manager (stdio + http), secrets, servers/
├── memory/                 # memory backends, SQLite stores, policy, identity, rules
├── observability/          # logger, metrics, tracing, diagnostics, performance
├── plugins/                # drop-in UI/tool plugins (Suggestion UI, …)
├── recovery/               # boot sentry, crash analyzer, auto-heal patcher, tool
├── security/               # permissions, approvals, confirm, rules, undo, scanner
├── services/               # Centralized API hub: real service/key probes for Settings
├── tests/                  # unit · ui · hybrid_voice trees + live acceptance script
├── vision/                 # camera, detection backends, hands, faces, live stream
├── voice/                  # hybrid voice coordinator, STT, TTS, wake, narration, sfx
├── docs/                   # cleanup report, Agent TARS notes, PC/web control notes
├── openspec/               # specs and the change history for this project
├── assets/                 # HUD imagery, fonts, 3D orb assets, audio stings
├── data/                   # runtime state: logs, DBs, models, workspaces (gitignored)
└── models/                 # local model weights (e.g. Laya multilingual)
```

Vendored runtimes kept beside the app (own manifests, excluded from tests):
`node_modules/` (Agent TARS), `AgentReach/`, `UI-TARS/`,
`models/laya-multilingual/`, and the example `Jarvis Obsidian Vault/`.

---

## 3. Module ownership & the one-release shims

The `modernize-jarvis-architecture` change moved modules to the layer that owns
them. Old import paths still work because `core/` keeps transparent aliases — a
`sys.modules` swap, not a re-export:

```python
# core/lazy.py — deprecation shim
from app import lazy as _impl
_sys.modules[__name__] = _impl
```

| Canonical home | Owns |
| --- | --- |
| `app/` | `ai_providers` · `llm_client` · `model_router` · `plugin_loader` · `config_guard` · `installer` · `lazy` · `brain_bridge` · optional FastAPI service |
| `voice/` | `hybrid_voice` · `stt` · `tts` · `voice` · `wake` · `narrator` · `delivery_style` · `providers_config` · `audio_devices` · `sfx` |
| `security/` | `permissions` · `approvals` · `confirm` · `coding_approval` · `rules` · `undo` · `malware_scanner` |
| `observability/` | `logger` · `metrics` · `tracing` · `diagnostics` · `performance` |
| `core/` | runtime primitives: `stability` · `sys_metrics` · `maintenance` · `action_ledger` · `dependency_ops` · `joptimizer` · `laya_decision` · `brain_mode` · `speech_correction` · `audio_reactive` |
| `brain/` `memory/` `knowledge/` `capabilities/` `vision/` `coding/` `recovery/` `evolution/` | unchanged homes |

Aliases kept for one release — import from the new home instead:

| Old import | New home |
| --- | --- |
| `core.ai_providers`, `core.brain_bridge`, `core.config_guard`, `core.installer`, `core.lazy`, `core.llm_client`, `core.model_router`, `core.plugin_loader` | `app.*` |
| `core.audio_devices`, `core.hybrid_voice`, `core.sfx`, `core.stt`, `core.tts`, `core.voice`, `core.wake` | `voice.*` |
| `core.confirm`, `core.undo` | `security.*` |
| `core.diagnostics`, `core.performance` | `observability.*` |

That is 19 alias files. `main.py`'s deferred loader still resolves a few of
them through `core.*` paths; they work by design, not by accident.

---

## 4. Layers at a glance

```text
         ┌─────────────────────────────────────────────────────────────────────┐
 UI (Qt) │ ui.py · ui_settings.py · ui_layout*.py · ui_theme.py · ui_*_panel.py │
         └───────────────▲────────────────────────────┬────────────────────────┘
                         │ signals / state            │ user intent
         ┌───────────────┴────────────────────────────▼────────────────────────┐
 main.py │ JarvisLive: Live voice session, TOOL_DECLARATIONS, tool dispatch    │
         └───┬───────────────┬──────────────┬──────────────┬───────────────────┘
             │               │              │              │
     ┌───────▼─────┐ ┌───────▼─────┐ ┌──────▼──────┐ ┌─────▼──────┐
     │ memory/     │ │ knowledge/  │ │ brain/      │ │ coding/    │
     │ 3 backends  │ │ local/Qdrant│ │ engine·graph│ │ Aider /    │
     │             │ │             │ │ skills      │ │ OpenHands  │
     └─────────────┘ └─────────────┘ └─────────────┘ └────────────┘
     ┌──────────────────────────────┐  ┌────────────────────────────┐
     │ capabilities/ + actions/     │  │ vision/ + voice/           │
     │ router · controllers ·       │  │ camera · detection ·       │
     │ registry · locks             │  │ STT · TTS · wake · narration│
     └──────────────┬───────────────┘  └────────────┬───────────────┘
                    │                               │
   ┌────────────────▼───────────────────────────────▼────────────────┐
   │ core/          stability · sys_metrics · maintenance ·          │
   │                action_ledger · dependency_ops · joptimizer ·   │
   │                speech_correction · laya_decision               │
   │ security/      permissions · approvals · confirm · rules · undo│
   │ observability/ logger · metrics · tracing · diagnostics · perf │
   │ app/           providers · model_router · plugin_loader ·      │
   │                config_guard · installer                        │
   │ recovery/      boot_sentry · analyzer · patcher · service      │
   └────────────────────────────────────────────────────────────────┘
```

---

## 5. Startup & the interpreter resolver

The launcher chain exists because a Windows machine usually has several
Pythons and the wrong one used to be picked, producing
`ModuleNotFoundError: No module named 'psutil'`.

```text
run_jarvis.vbs / install_dependencies.bat / setup_jarvis.bat
        │  (any interpreter they can find — start_jarvis.py is stdlib-only)
        ▼
start_jarvis.py
        │  1. probe every candidate Python (project venv → py -3 → PATH)
        │  2. pick the first that can import HARD_IMPORTS
        │     (PyQt6 + QtCore/QtGui/QtWidgets, numpy)
        │  3. install requirements.txt into THAT interpreter if none qualify
        │  4. re-check, print a report, then hand over to main.py
        ▼
main.py  →  stability guards  →  boot-sentry rollback  →  single-instance guard
         →  QApplication + HUD (ui.py)
         →  deferred action-tail imports run CONCURRENTLY with the Live connect
         →  greeting, then Gemini Live session + tool dispatch
```

### Virtual-environment isolation (.venv/)

All Python packages are installed into a **project-local virtual environment**
(`.venv/`) instead of the system or user interpreter. This means:

* `install_dependencies.bat` creates `.venv/` on first run, upgrades pip inside
  it, then runs `pip install -r requirements.txt` against the venv's own Python.
* `setup_jarvis.py` does the same through `ensure_venv()` + `install_python_deps()`.
* Packages never leak into the global Python or other projects.
* **Complete reset**: delete `.venv/` and re-run `[2] Python packages` or
  `install_dependencies.bat`.

`.venv/` is gitignored and excluded from the maintenance sweeper, the
startup optimizer and the `pytest` walker.

* `SOFT_IMPORTS` (psutil, cv2, requests, PIL, sounddevice, mss, send2trash) are
  probed only to *report* what is missing — they never block a start.
  `tests/unit/test_startup_bootstrap.py` locks this behaviour **and** the
  stdlib-only invariant.
* `main.py` also carries `_startup_dependency_guard()` for the case where it is
  launched directly on the wrong interpreter: the message names the missing
  package and the interpreter it looked in, instead of a raw traceback.
* `startup_optimizer.py` runs once per launch, before the UI loads: it clears
  JARVIS's own `data/tmp` / `data/temp` / `data/cache` and OS-temp files older
  than `JARVIS_STARTUP_TEMP_AGE_HOURS` (default 24 h). It is skipped while a
  live instance holds `data/runtime/jarvis.lock`.
* `launch_ui.py` renders real launcher progress (console bar for `.bat`, a
  borderless splash overlay for `.vbs`); the percentage is the weighted sum of
  reported real work, never invented.

### Runtime hardening during boot

`core/stability.py` installs crash + hang guards, tunes responsiveness and
writes `data/logs/jarvis_crash.log`; it also owns the single-instance mutex.
Window close runs `stability.request_process_exit()` → `force_exit()` →
`os._exit()`, so the interpreter never runs the native teardown that aborts on
some vision stacks. If an older build ever leaves an orphan holding the mutex
and the microphone, `Closed_Jarvis.py` clears it.

---

## 6. Configuration

Three sources, one precedence: **environment → `config/api_keys.json` →
built-in defaults** (`config/settings.py`).

| Concern | Where | Notes |
| --- | --- | --- |
| Secrets + UI prefs | `config/api_keys.json` | primary store; gitignored |
| Optional overrides | `.env` (from `.env.example`) | e.g. `GEMINI_API_KEY` |
| Feature flags | `api_keys.json → "features"` | defaults in `FEATURE_DEFAULTS` |
| Env flag override | `JARVIS_FEATURE_<NAME>` | truthy string ⇒ on |

* **Read path** — `settings.get()` / `get_feature()` / `feature_enabled()`,
  backed by `_read_api_keys()` cached on `(mtime, size)` so repeated reads are
  free.
* **Write path** — `settings.set_feature()` and every Settings save go through
  `app/config_guard.py`: **backup → write atomically → verify → roll back if the
  file reads back wrong**, with a `config/backups/` history pruned to the newest
  20. `list_backups` / `last_good` / `restore` / `repair` make a broken config
  recoverable from the Settings UI and the `diagnostics` tool.
* **Feature flags** — roughly 45 switches in `FEATURE_DEFAULTS`, in groups:
  orchestration/memory/knowledge/tracing; the control engines
  (`agent_tars`, `desktop_control`, `playwright_browser`, `windows_automation`,
  `uitars_visual`, `physical_input`); deep Windows orchestration
  (`process_control`, `system_settings`, `window_control`, `clipboard_sentry`);
  voice (`realtime_stt`, `narrator`, `realtime_low_latency`, `tts_streaming`);
  self-evolution (`self_evolution`, `self_evolution_auto_install_deps`,
  `self_heal`, `auto_heal_mode`); and one switch per external relationship
  (`spotify`, `instagram`, `smart_home`, `connect`, `external_apps`,
  `mcp_servers`, `obsidian_memory`, …).
* **Redaction** — `SECRET_FIELD_NAMES` + `settings.redact_secrets()` + the
  logger's secret-pattern scrubber guarantee no credential reaches a log or a
  trace.
* **TLS certs** — `dashboard/server.py:_ensure_certs()` generates a self-signed
  pair in `config/certs/` on first run, so **no private key ever ships**. If
  generation fails the dashboard simply stays on plain HTTP.

---

## 7. Voice pipeline

`voice/hybrid_voice.py` is the single owner of microphone capture, the Live
session's audio path, the ONNX engine and playback — one instance of each
resource, cooperating instead of duplicating.

```text
 mic ─► hybrid_voice capture ─► VAD gate ─► Live session (Gemini native audio)
                                             │            ▲
                        wake word (voice/wake.py)         │ barge-in / interruption
                                             ▼            │
                               STT (voice/stt.py)     TTS (voice/tts.py)
                                                          │
               narration (voice/narrator.py) ──► speakers / device selection
                                                          │
                       delivery style (voice/delivery_style.py) · sfx (voice/sfx.py)
```

* **Realtime low latency** — the client finalises your turn the moment local VAD
  sees the sentence end (`features.realtime_low_latency`), instead of waiting out
  the server's ~800 ms silence window (the hybrid VAD in `main.py`).
* **Realtime STT** (`features.realtime_stt`) — interim transcripts are rendered
  live into the command box, then `core/speech_correction.py` corrects the
  phonetically-misheard final transcript against known commands and applications.
  Off restores final-only transcripts; nothing else about voice changes.
* **Narration** (`features.narrator`) — J.A.R.V.I.S. says what he is doing while
  he does it ("Sir, I'm opening the page."). Every line comes from a real event
  or engine progress string; off is pure silence and measured turn latency is
  still reported.
* **Voice output routing** — `voice/providers_config.py` + `voice/tts.py` select
  between Gemini native audio, cloud TTS (Fish Audio, ElevenLabs, Edge),
  bundled Piper/ONNX and OS SAPI, all configured in `config/voice_config.json`.
  `features.tts_streaming` renders cloud voices sentence by sentence.
* **Devices & level meter** — `voice/audio_devices.py`, `core/sys_metrics.py`
  and `core/audio_reactive.py` feed the HUD waveform and the speech-reactive
  logo from one shared energy signal. The mic-level callback is on the audio
  thread, never the UI thread.

---

## 8. AI providers & model router

* `app/ai_providers.py` + `app/llm_client.py` — Gemini, OpenAI, Anthropic,
  Groq, Ollama (auto-launched), OpenRouter and any OpenAI-compatible endpoint.
* `app/model_router.py` — maps **roles** (`default`, `fast`, `reasoning`,
  `coding`, `vision`) to models, so the planner, writer and vision paths can use
  different models.
* `core/laya_decision.py` — a 322M local "System 1" model that classifies each
  request into SEARCH / CODE / REVIEW / VIEW / FIND / GENERATE / ANALYZE /
  AUTOMATE / CHAT (mirroring the dashboard activity rows). It routes locally and
  falls back safely when the model is absent; `capabilities/decide.py` consumes
  the classification when planning controller order.

---

## 9. The brain, memory & knowledge

`brain/` is the reasoning/orchestration layer:

| File | Role |
| --- | --- |
| `engine.py` | central Cognitive Engine that coordinates subsystems |
| `skills.py` | **declarative skill registry** — one `Skill(...)` per capability; deterministic word-boundary routing (fast, no model round-trip) |
| `graph.py` | optional **LangGraph** orchestrator (`plan → execute → verify → replan`) with a pure-Python fallback; checkpoints to `data/runtime/` |
| `context.py` | builds the prompt context (personality + memory + skills) |
| `personality.py` | tone / behaviour |
| `learning.py` | learns from outcomes |
| `tasks.py` | task lifecycle + correlation ids |
| `self_inspect.py` | the assistant inspecting its own structure |

**Memory** (`memory/`) — one backend contract (`memory/backend.py`), three ways
to keep long-term memory; callers never know which is active:

* **Local (default)** — SQLite (`data/brain_memory.db`, `data/conversations.db`)
  plus `memory/long_term.json` (identity, preferences, projects, relationships,
  wishes, notes, sessions).
* **Obsidian vault** (`memory/obsidian_backend.py`, `vault_store.py`,
  `vault_watch.py`) — selects the vault as the memory home, indexed into
  `data/obsidian_index.db` and watched for edits in Obsidian; J.A.R.V.I.S. only
  ever writes under `Memory/`, records provenance (`source`, `conversation_id`,
  reason, confidence) and links only to real hits. Managed from
  `ui_vault_panel.py`; gated by `features.obsidian_memory`.
* **Mem0 (optional)** — `memory/mem0_client.py`, enabled by
  `features.mem0_memory` + a key.

Whatever the backend, `memory/policy.py` is the single deterministic promotion
gate: temporary/session/task scopes never reach long-term storage, only
long_term/project facts do. `memory/identity.py` and `memory/rules.py` provide
the bounded prompt blocks that keep who the user is (and their corrections)
alive across restarts, models and reconnects.

**Knowledge** (`knowledge/`) — local corpus retrieval by default
(`retriever.py`, token scoring, zero dependencies); optional **Qdrant** vector
store (`qdrant_store.py`) when `features.qdrant_knowledge` is on. Backend
failure degrades to local search with one honest notice.

---

## 10. Tools & the dispatch path

The model's entire surface is `TOOL_DECLARATIONS` in `main.py`: 58 declarations
inline plus the `mcp` and `connector` declarations appended when their modules
import — about 60 built-ins in a full install. Multi-action work is grouped into
single tools with an `action` enum (`pc_control`, `visual_control`,
`browser_action`) to keep the Live schema small; `capabilities/registry.py`
validates every action and parameter before a controller is touched.

Runtime tools are published the same way as built-ins:

* **Plugins** contribute declarations through the runtime plugin registry.
* **MCP servers** publish each of their tools as `<server>__<tool>`.
* **Forged skills** are installed as ordinary plugins and hot-registered
  mid-session, so a newly forged tool is callable without a restart.

`JarvisLive._execute_tool()` is the one dispatch path. Its order is the safety
model:

1. publish the activity row *before* doing anything (a `laya_decision` fallback
   label when the tool has no mapping);
2. barrier on the deferred action-module imports;
3. permission gate (`security/permissions.py`) — a refusal always names the
   switch that stopped it;
4. the tool's own handler: built-in, plugin, MCP, connector, or brain bridge;
5. security gates for consequential/irreversible work (approval, confirmation
   token, coding approval);
6. result bookkeeping — `security/undo.py` when reversible,
   `core/action_ledger.py` always, `_tool_result_status()` for the HUD, and a
   `FunctionResponse` whose text names the engine that actually ran.

Built-in groups, roughly: conversation & brain (`brain_*`, `task_plan`,
`control_plan`), web & research (`web_search`, `web_research`, `youtube_video`,
`weather_report`), media generation (`generate_image`, `image_research`),
files & deliverables (`file_controller`, `file_processor`, `documents`),
system (`system_status`, `system_control`, `power_control`, `window_control`,
`manage_monitor`, `clipboard_sentry`), control (`computer_control`,
`browser_control`, `pc_control`, `browser_action`, `visual_control`), vision
(`vision_look`, `live_vision`, `detect_objects`, `detect_gesture`,
`recognize_faces`, `what_am_i_holding`, `close_camera`), memory & identity
(`save_memory`, `recall_memory`, `brain_remember`, `brain_forget`,
`identity_profile`), services (`play_music`, `smart_home`, `instagram`,
`connector`, `mcp`, `service_hub`, `connect_devices`), self-management
(`diagnostics`, `performance_scan`, `self_repair`, `skill_forge`, `skill_query`,
`voice_settings`, `shutdown_jarvis`, `undo`).

---

## 11. Capabilities & computer control

One router, several engines; **the result always names the engine that ran**:

```text
capabilities/router.py
   ├── Agent TARS (node_modules, v0.3.0)  computer_control / browser_control
   │                                      whole-task PC + browser automation
   ├── Agent Reach                        web_research — internet research channels
   ├── browser_pw.py    Playwright        browser_action — role/label/text/CSS, no coordinates
   ├── windows_ext.py   pywinauto         pc_control — apps + native window controls
   ├── uitars.py        UI-TARS prompt    visual_control — grounding on the Gemini key
   ├── desktop.py       PyAutoGUI         physical mouse/keyboard, executor of grounded clicks
   └── power/proc/processes/clipboard/smart_home/media/instagram/sysctl/geospatial
```

* `capabilities/registry.py` — the structured action table: action → tool →
  controller → permission switch → required parameters → fallback chain.
  Nothing else hardcodes an action name.
* `capabilities/actions.py` — one executor per grouped tool; validates against
  the registry, runs the locks, returns one text block for Gemini.
* `capabilities/decide.py` — turns a spoken request into an ordered list of
  controllers to try, grounded in what is actually installed and switched on
  (fact-injectable, so the policy is testable without Spotify or a browser).
* `capabilities/locks.py` serialises conflicting actions;
  `capabilities/health.py` feeds Diagnostics with what is missing;
  `core/action_ledger.py` records what ran and its outcome so a fallback can
  never perform a second SEND/SUBMIT unnoticed.

Agent TARS is the single PC/browser executor behind `computer_control` and
`browser_control`; UI-TARS has **no API key of its own** — it runs on the same
Gemini configuration as everything else. See `docs/pc_web_control.md` for the
action list and switches.

---

## 12. Local actions & deliverables

`actions/` holds the command modules the tools execute: web search, YouTube
(open + transcript), weather, file controller/processor (documents, media,
archives), screen/camera capture, reminders, system monitor, proactive engine,
background monitors, image research, `code_helper`, `dev_agent` and voice
settings. Optional parsers/FFmpeg are probed and reported, never assumed.

`documents/` builds real deliverables (Word, Excel, PowerPoint, PDF) behind the
`documents` tool and the `document_architect` feature switch; `coding/` routes
work to Aider (focused edits) or OpenHands (larger tasks, Docker) through
`agent_router.py`, with a coding lock and an approval flow
(`security/coding_approval.py`, `auto_approve_coding` flag).

---

## 13. Vision

`vision/` provides camera frames, detection and understanding, with a real
backend chosen at runtime (`vision/object_detection.py:ensure_backend`):

* object detection — Ultralytics/YOLO, MediaPipe/EfficientDet
  (`data/models/vision/efficientdet_lite0.tflite`), OpenCV DNN, or a multimodal
  fallback; `auto` picks the first backend whose model file is actually present;
* `vision/gesture.py`, `hand_tracking.py`, `face_recognition.py`,
  `register_face.py` for hands and faces (optional InsightFace backend);
* `vision/multimodal.py` for scene descriptions; `vision/live_stream.py` drives
  the live panel and `features.live_vision` the continuous understanding path;
* `vision/manager.py` owns the shared camera stream and privacy toggle;
  `vision/frame_processor.py` stages work so the camera is not over-sampled.
  The vision panel's repaint timers only run while the widget is visible
  (`showEvent`/`hideEvent`).

---

## 14. Observability

| Module | Provides |
| --- | --- |
| `observability/logger.py` | structured JSONL events + unchanged console echo, secret scrubbing, **log rotation** (14-day default, `JARVIS_LOG_RETENTION_DAYS`) |
| `observability/metrics.py` | per-subsystem call/error/duration counters |
| `observability/tracing.py` | optional Langfuse spans (no-op when unconfigured) |
| `observability/diagnostics.py` | the `diagnostics` tool: health checks, runtime state, safe mode, config backups/repair, `optimize` |
| `observability/performance.py` | “why is my PC slow?” MEASURE → IDENTIFY → DIAGNOSE (changes nothing) |

Every event carries timestamp, level, component, task id, tool, state, duration,
result and error, and redaction runs at the logging boundary — a key that is
read but not listed in `SECRET_FIELD_NAMES` is the one that ends up in a log
line, so the list is maintained with each provider added. Logging is guarded:
it may never raise into its caller.

---

## 15. Security

`security/` is the gate every risky action passes:

* `permissions.py` — a refusal always names the **feature switch** that stopped
  it (e.g. “Terminal and command execution”), and an unknown feature never
  blocks. One switch per relationship, enforced per action.
* `approvals.py`, `confirm.py`, `coding_approval.py` — approval/confirmation
  gates for higher-risk or irreversible actions. UI-issued confirmation tokens
  (not model-authored `confirmed` flags) authorise things like scanner removal.
* `rules.py` — the user's learned rules and the evaluation that applies them;
  `undo.py` — a shared undo stack, fed by the dispatcher.
* `malware_scanner.py` — scanning for risky content.
* `core/action_ledger.py` records every important action; pending entries close
  with the dispatcher's real result. `capabilities/locks.py` and `core/locks.py`
  prevent collisions.

---

## 16. Recovery, stability & self-evolution

* `core/stability.py` — crash + hang guards, single-exit process teardown,
  single-instance mutex.
* `recovery/` — `boot_sentry.py` (startup health + rollback of a patch that
  died), `analyzer.py` (parse a traceback), `patcher.py` (propose/apply a fix,
  AST-checked in memory, backed up, `py_compile`-verified, always
  rollback-able), `service.py` (the sentry), `tool.py` (the `self_repair` tool).
  Modes from `features.auto_heal_mode`: `auto_low_risk` (default — patch a
  leaf-capability file in `actions/`, `plugins/`, `evolution/` automatically;
  everything else waits for the on-screen confirmation gate), `propose`
  (approval for everything), `auto`, `off`. Limits: never the same line twice
  in a row (5-minute window), at most 5 automatic patches per session, every
  patch in `data/recovery/patch_history.json`.
* `evolution/` — two self-improvement systems behind one router:
  * **`manager.py` — the Evolution Manager.** The only place that decides which
    system runs: a **missing capability** goes to the Forge, a capability that
    **keeps failing** goes to the DGM adapter, and something **already built** is
    neither — it is run. It remembers every forge (name, goal, triggers,
    provider) in `data/evolution/forged_skills.json` and mirrors that into
    long-term memory, so asking for the same skill twice does not burn a second
    synthesis on it.
  * **Forge pipeline** — `discovery.py` (spot a real gap or an explicit "learn
    how to…"), `forge.py`, `crucible.py` (AST check → dependency resolve →
    sandboxed child-process test), `vault.py` (install as an ordinary plugin +
    hot-register), `registry.py` (list/run/toggle/delete — the `skill_forge`
    tool). Synthesis walks the first **usable** provider in the chain
    **OpenRouter → (provider selected in Settings ▸ AI) → Anthropic → OpenAI →
    Groq → Gemini**; a stored value that is not a key is skipped and named in
    Settings ▸ EVOLUTION, never sent.
  * **`dgm/` — the Darwin Gödel Machine adapter**, for improving what exists:
    `policy.py` (what may be touched, default deny), `archive.py` (every version
    plus its metrics, so rollback is a property of the storage), `adapter.py`
    (`ANALYZE → EVOLVE → TEST → COMPARE → KEEP/REJECT`, with `ROLLBACK`).
    A candidate replaces the incumbent only when it measurably wins; otherwise
    the working version is left byte-for-byte intact.

  **The DGM policy in code:** *never touched* — `config/`, `security/`,
  `core/`, `app/`, `capabilities/`, `memory/`, `voice/`, `vision/`, `brain/`,
  `observability/`, `recovery/`, `dashboard/`, `connect/`, `mcp_host/`,
  `evolution/{crucible,vault,manager}.py`, and the single files `main.py`,
  `start_jarvis.py`, `setup_jarvis.py`, `setup_jarvis.bat`, `ui.py`,
  `ui_settings.py`, `ui_layout.py`, `ui_theme.py`, `requirements.txt`,
  `package.json`, `package-lock.json`. *Writable*: `plugins/**` and
  `evolution/adapters/**`, and nothing else. A refusal always names why
  (“protected core file” ≠ “not on the allowed list”).

---

## 17. Plugins, MCP host, connectors, dashboard, connect & services

* **Plugins** (`plugins/`) — drop-in UI panels and tools; each folder has
  `plugin.json` + `plugin.py` (see `plugins/_template.py`). Loaded by
  `app/plugin_loader.py`; the runtime registry accepts forged skills and MCP
  processes mid-session.
* **MCP host** (`mcp_host/`) — the universal Model Context Protocol manager.
  `catalog.py` defines server specs (stdio process or HTTP endpoint; the
  built-in list ships the native system/files/web/vault/dev servers),
  overridable from `data/mcp/servers.json`;
  `manager.py` owns every session and models four different states honestly —
  enabled, connected, authenticated, tools-ready; `secrets.py` keeps credential
  *values* out of the visible configuration. Needs Node 18+.
* **Connectors** (`connectors/`) — **exactly one** route to every external app
  (Spotify, Gmail, GitHub, Telegram, …): `manager.py` → Composio sessions →
  toolkit tools. Authentication is Composio's (a Connect Link in the browser);
  no provider OAuth app or token lives in this process. One `connector` tool for
  the model, one Settings page for the user, `external_apps` switch.
* **Dashboard** (`dashboard/server.py`) — FastAPI + uvicorn on port 8000 by
  default; application-layer AES-256-CBC with a session-derived key and a
  locally cached CryptoJS (no CDN dependency). A second HTTPS alias on port
  8001 uses the locally generated `config/certs/` pair for browsers that
  HTTPS-upgrade bare IPs. The QR pairing flow lives in the HUD; `brain3d.py`
  serves the 3D brain view.
* **Connect** (`connect/`) — the phone/remote gateway: `gateway.py` (stdlib
  `http.server` + hand-rolled WebSocket, bound to `127.0.0.1` by default),
  `devices.py` (pairing, one revocable token per device), `commands.py`
  (every command checked against the desktop's own permission switches),
  `tool.py`, `runtime.py`. Irreversible commands park behind the HUD
  confirmation and the phone is told it is waiting — not that it succeeded.
* **Services** (`services/hub.py`) — the Centralized API hub behind
  Settings ▸ API and the `service_hub` tool: one `Service`/`ProviderKey`
  registry, and probes that measure (one bounded request, import or TCP
  connect) instead of guessing. “Not configured” is never dressed up as
  “working”.

---

## 18. Performance & maintenance (the self-optimizing layer)

J.A.R.V.I.S. keeps itself small and smooth in five places:

| Piece | What it does |
| --- | --- |
| `observability/logger.py` | **rotates logs**: on first open it drops dated `jarvis-*.jsonl` older than `JARVIS_LOG_RETENTION_DAYS` (default 14), always keeping the live file |
| `core/maintenance.py` | `run_maintenance()` → rotate logs (14-day + size cap), expire `data/cache`, prune regenerable `__pycache__` (never into `.venv`/`node_modules`/`models`/`UI-TARS`), then report reclaimed MB |
| `startup_optimizer.py` | clears leftover temp/cache before the UI/backend load each launch |
| `observability/performance.py` | measures CPU/RAM/disk/net, names the bottleneck, *suggests* fixes — changes nothing |
| `core/joptimizer.py` | Windows OS tuning with reversible snapshots (power plan, Game DVR, visual effects) + JARVIS cache clean |

`core/sys_metrics.py` runs **one** metrics sampler per process (shared via
`shared_metrics()`), and the HUD orb idles at ~24 fps, switching to 60 fps only
while interactive — both are documented, measured wins from the cleanup pass
(`docs/cleanup_report.md`, `tests/ui/test_orb_frame_budget.py`).

The maintenance routine is reachable three ways:

```text
python setup_jarvis.py --optimize        # from a terminal
diagnostics mode="optimize"              # ask J.A.R.V.I.S. to do it
from core.maintenance import run_maintenance   # from code
```

### Settings ▸ PERFORMANCE (`ui_perfomance.py`)

Three named profiles — **Balanced**, **Performance**, **Ultra Performance** —
plus **Custom** (activates the moment a control is moved off a preset). Each
profile is a deterministic bundle over switches that already exist: HUD
animations (`smooth_animations`), panel sound effects (`enable_sfx`),
audio-reactive paint (`audio_reactive`), the Mind panel's live-refresh cadence,
the vault watcher and index-freshness windows, World Monitor's refresh, and the
Neural Map's node budget, glow work and simulation tick. Every change persists
through `config/settings.py:set_value`/`set_feature` (the guarded writer) and is
pushed into the running app — no restart. No profile ever touches memory
storage, data integrity, sync guarantees or security.

**SCAN PERFORMANCE** runs a bounded, cancellable scan off the UI thread and
reports only measured facts: the config store, the active memory backend and its
local row count, one incremental vault index scan, one bounded graph build, and
process CPU/RAM when psutil is present. Findings carry evidence, impact,
confidence and a suggested action; the scan itself changes nothing.

---

## 19. Setup & running

```text
setup_jarvis.bat / python setup_jarvis.py     # interactive Setup & Control Center
  SYSTEM group
    [1] environment check   — Python · pip · Node · git · FFmpeg · .venv
    [i] inspect & fix crash — debug run_jarvis crashes & auto-fix
    [9] diagnose & repair   — report providers/engines, then fix
    [t] view logs           — tail dependency_install, setup_error, jarvis logs
    [d] open project folder — open JARVIS OS V7/ in Explorer
  INSTALL group
    [p] install Python      — auto-install Python 3.13 via winget
    [2] python packages     — requirements.txt → isolated .venv/ (delete to clean)
    [u] update packages     — pip upgrade all .venv packages to latest
    [v] wipe .venv          — delete .venv/ for a clean reinstall
    [3] node runtime        — Agent TARS browser + PC automation
    [4] playwright browser  — chromium binary for browser_action
    [f] install FFmpeg      — auto-install FFmpeg via winget
    [g] install Git         — auto-install Git via winget
    [7] install ALL missing — every optional feature package
  ACCESS group
    [5] API keys            — Gemini + others → .env / api_keys.json
    [6] test connections    — verify each key with its provider
    [o] authorize providers — open sign-up / key pages in browser
  MAINTAIN group
    [8] optimize            — rotate logs, expire cache, drop bytecode
    [s] compress / shrink   — reduce folder size · clean cache & builds
    [a] essentials          — keys → packages → node → optimize
  PACKAGE group
    [c] convert to .exe     — build Jarvis OS with its saved data bundled
    [r] reset data          — fresh start (keys, memory, cache backed up)
  LAUNCH
    [l] launch J.A.R.V.I.S. — hand over to start_jarvis.py

setup_jarvis.py --all --yes                    # scripted, non-interactive
setup_jarvis.py --install-missing              # add every dependency still absent
install_dependencies.bat                       # create .venv + install py deps
run_jarvis.bat                                 # just launch
run_layout_editor.py                           # Layout Studio only, no full app
```

Both `setup_jarvis.py` and `start_jarvis.py` are **stdlib-only** so they run on
the bare interpreter before anything is installed; tests lock that invariant.

**Clean reinstall**: delete `.venv/` then run `install_dependencies.bat` or
choose `[v] Wipe .venv` → `[2] Python packages` in the setup wizard.

---

## 20. Extending J.A.R.V.I.S.

| To add… | Do this |
| --- | --- |
| a **Tool** the model can call | add a declaration in `main.py` (`TOOL_DECLARATIONS`) + a dispatch branch in `_execute_tool`, and gate it in `security/permissions.py` if risky |
| a **grouped controller action** | add a row to `capabilities/registry.py` + an executor in `capabilities/actions.py` |
| a **Capability/controller** | add a module in `capabilities/`, register it in `router.py` and `registry.py`, probe it in `health.py` |
| a **Skill** (routing entry) | append one `Skill(...)` to `brain/skills.py` — that is the whole change |
| a **UI plugin** | copy `plugins/_template.py` to `plugins/<Name>/` with `plugin.json` + `plugin.py` |
| an **MCP server** | add a spec to `mcp_host/catalog.py` or `data/mcp/servers.json`; its tools are published as `<server>__<tool>` |
| a **connector app** | nothing per-app: enable the toolkit through the `connector` tool / Settings ▸ Connectors |
| a **provider** | add it in `app/ai_providers.py` and wire a role in `app/model_router.py` |
| a **feature flag** | add the default to `FEATURE_DEFAULTS` (`config/settings.py`) and toggle via Settings |
| a **state store** | write through the owning guarded writer; never add a second writer (principle §3) |

---

## 21. Testing & conventions

```powershell
python -m pytest                       # whole suite (tests/ only; vendored trees excluded)
python -m pytest tests/unit -q         # fast units      (~390 tests)
QT_QPA_PLATFORM=offscreen python -m pytest tests/hybrid_voice -q   # voice (~155 tests)
python -m compileall .                 # syntax / import smoke check
```

* `pytest.ini` pins `testpaths = tests`, a project-local `--basetemp`, and
  `norecursedirs` for `UI-TARS`, `AgentReach`, `node_modules`, `models`, `data`,
  `assets` and friends — a bare `pytest` must never walk vendored trees.
* `tests/conftest.py` forces `QT_QPA_PLATFORM=offscreen` and
  `JARVIS_DISABLE_WEBENGINE=1`, so a headless run builds real widgets without
  initialising Chromium. The UI tree is run one file per process (a native
  abort stays local to one file); `tests/live_capability_acceptance.py` is the
  manual, live end-to-end check.
* The counts above are from `docs/README.md` and move as the suite grows — the
  invariant is that both trees are green.

Conventions the codebase holds to:

* **stdlib-only** is a *tested* property of `start_jarvis.py` and
  `setup_jarvis.py`;
* optional subsystems live behind **feature flags** that default to off (or on
  only where the fallback is a real local implementation);
* every **cross-boundary call is guarded** and reports honestly;
* **one writer** per state file, always verified on write;
* **modules move with aliases**, callers are updated, and the alias is dropped
  after one release;
* new behaviour ships with a **test** that pins it.

---

*J.A.R.V.I.S. — Windows · macOS · Linux · Voice · Vision · Memory · PC/Web Control · Coding Agents*
