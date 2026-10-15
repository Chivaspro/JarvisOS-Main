# CODEBASE AUDIT — JARVIS OS V7

Audit + repair pass. Business date: 2026-10-09. Working tree: no Git commits
(everything untracked), so all changes were made surgically and none are
destructive. Companion ledger: `TODO.md`.

---

## 1. Scan coverage

Inspected (first-party only):

| Area | What was read |
| --- | --- |
| Entry chain | `main.py` (~7.6k lines: boot, `_DeferredModules`, `JarvisLive`, tool dispatch, shutdown), `start_jarvis.py`, `run_jarvis.bat/.vbs`, `install_dependencies.bat`, `setup_jarvis.py` (spot checks) |
| UI framework | `ui.py` (~23.5k lines: HUD, `_ActivityTimeline`, command window, panels), `ui_settings.py` (Settings + PERFORMANCE tab wiring), `ui_perfomance.py` (full read), `ui_theme.py` (spot checks) |
| Runtime state | `data/logs/jarvis_crash.log` (26 native dumps), `data/logs/jarvis-20261009.jsonl`, `jarvis_startup_error.log` (stale) |
| Dependencies | `.venv/` (Python 3.14.8), `requirements.txt`, `core/dependency_ops.py` (full read of reconcile path) |
| Subsystems (targeted) | `core/stability.py` (shutdown/faulthandler), `core/laya_decision.py` (load path), `voice/wake.py` (detector + manager), `vision/config.py` (`detect_cuda`), `mcp_host/bridge.py` + `manager.py` (publish path), `app/plugin_loader.py` (registry), `observability/` (aliases) |
| Tests | `pytest.ini`, `tests/conftest.py`, `tests/ui/conftest.py`, `tests/unit/test_laya_decision.py`, `tests/unit/test_mcp_host.py`, `tests/ui/test_performance_tab.py`, runner conventions |

Excluded on purpose (vendored/generated, per project rules): `.venv/`,
`node_modules/` (Agent TARS), `AgentReach/`, `UI-TARS/`, `models/` weights,
`assets/`, `build/`, `dist/`, `__pycache__/`, caches.

## 2. Architecture (as found)

- **Launch chain:** `run_jarvis.bat/.vbs` → `start_jarvis.py` (stdlib-only
  interpreter resolver) → `main.py` → stability guards → single-instance
  mutex → PyQt6 HUD (`ui.py`) → background backend thread (`_DeferredModules`
  imports, then Gemini Live session + tool dispatch).
- **UI:** PyQt6, offscreen-testable (`QT_QPA_PLATFORM=offscreen`,
  `JARVIS_DISABLE_WEBENGINE=1` in tests). Settings is
  `ui_settings.SettingsWindow`, opened from the HUD as
  `_full_settings_window` (`ui.py:19764`).
- **State writers:** one guarded writer per store (`app/config_guard.py`,
  `config/settings.py`).
- **Degrade-never-crash conventions** documented in `JARVIS_ARCHITECTURE.md`
  and honored in the code paths inspected.

## 3. Discovered problems, root causes, evidence

### P0-A — Native crash (0xC0000005) in the microphone watchdog (CONFIRMED, FIXED)

- **Symptom:** process died ~20-30 s after boot with
  `STATUS_ACCESS_VIOLATION`; crash log holds 26 native dumps across sessions
  (audio/mic threads: `sounddevice _wrap_callback`, `voice/wake.py`,
  `main.py:5262`).
- **Reproduced 2/2:** offscreen boot (45 s window) crashed with exit code
  `-1073741819`; dumps showed `jarvis-backend` inside the watchdog reopen at
  `main.py:5382 stale.stop()` and at `main.py:5375 _open_mic()` of the
  replacement stream.
- **Root cause:** the watchdog kept the stalled stream open while it opened
  and started a second stream for the same device, then stopped/closed the
  stale one; when the device never recovered it repeated the open/stop/close
  cycle forever. Every cycle is a PortAudio stream-lifecycle race window;
  concurrently live streams on one device is the trigger.
- **Fix (`main.py`):** abort+close the stale stream BEFORE opening its
  replacement (one live stream only), give up after 3 consecutive silent
  reopens (voice degrades to text), reset the budget only after ~10 s of
  audio continuity since the last reopen.

### P0-B — UI freeze: activity feed rebuilt every card on every log line (CONFIRMED, FIXED)

- **Symptom:** HUD freezes during log bursts (MCP hot-registration,
  transcripts); crash dumps show the main thread inside
  `ui.py _render → _make_card` while only logging.
- **Evidence:** `_ActivityTimeline.add_entry()` called `_render()` per line;
  `_render()` destroys and recreates up to 60 cards (4 widgets each) per call.
  Benched: the old pattern natively crashed (`0xC0000005`) within ~60 rapid
  rebuilds (reproduced twice; animated cards deleted mid-flight). The new
  path: 300 lines with per-line event processing → exit 0, cap correct,
  ~2.1 ms/line deferred.
- **Fix (`ui.py`):** incremental paint — insert only the new card, trim the
  oldest at the cap; full repaint only on filter change.

### P0-C — Startup `pip install` inside the live process (CONFIRMED, FIXED)

- **Evidence:** JSONL `dependencies.startup_sync`: "installed now: PyQt6
  (import not confirmed), PyQt6-WebEngine (import not confirmed)" on every
  boot; crash dumps show `jarvis-dep-sync → _pip_install` running while the
  app is live.
- **Root cause:** `core/dependency_ops._module_for("PyQt6")` derived the
  lowercase module `pyqt6`; `importlib.util.find_spec` is case-sensitive, so
  both Qt distributions probed missing forever.
- **Fix (`core/dependency_ops.py`):** `_PIP_TO_IMPORT` maps
  `pyqt6 → PyQt6`, `pyqt6-webengine → PyQt6.QtWebEngineWidgets`.
  Verified in JSONL: "already present: 33 / installed now: nothing was
  missing".

### P0-D — Wasted `torch` import for an unavailable decision model (CONFIRMED, FIXED)

- **Evidence:** every boot logs "loading decision model … torch threads=…"
  then "model load failed (ModuleNotFoundError: No module named 'laya')";
  crash dumps show `laya-init` mid-`import torch` (heavy native import)
  competing with UI/audio.
- **Root cause:** `_load_blocking` called `_cap_torch_threads()` (full torch
  import) before `import laya`; the runtime package is not installed, so the
  import was guaranteed waste.
- **Fix (`core/laya_decision.py`):** probe `find_spec("laya")` first; missing
  → the same honest error, no torch import.

### P1-E — `ui_perfomance.py` "does not open" (NOT REPRODUCIBLE in this tree)

- Module imports, `PerformanceTab` instantiates, and the real
  `SettingsWindow` builds the PERFORMANCE tab (integration test + lifecycle
  script: open → scan → cancel → close → reopen, single scan thread, no
  leak, exactly one tab). The reported failure belongs to an older build; no
  code change was needed. (The stale `jarvis_startup_error.log` also predates
  the current `main.py`.)

### P2-F — MCP tools re-registered O(n²) at startup (CONFIRMED, LEFT AS-IS)

- Evidence: boot log registers every tool of every connected server on each
  new server connection (`bridge.publish()` per `manager._connect`), ~4
  duplicate bursts per boot. Idempotent but wasteful + log noise. Left
  unchanged: deduping requires session-rebind semantics review (a reconnect
  must refresh runner closures) — documented in `TODO.md`.

### P3-G — Spotify MCP: RESOLVED BY REMOVAL (2026-10-09 evening)

- The missing `mcp_host/servers/spotify` module made the catalog-declared
  Spotify server unreachable. Per the owner's request the Spotify MCP was
  removed outright: builtin spec + preset (`mcp_host/catalog.py`), saved config
  entry (`data/mcp/servers.json`, backup `.bak-20261009`), quick-setup button
  and name map (`ui_mcp_panel.py`), icon entries (`mcp_host/icons.py`), the 12
  obsolete tests (replaced by a builtin-list test asserting Spotify is NOT
  shipped), and the architecture doc mention. The Spotify *service*
  integrations (media playback, connectors, `control_pc_open_spotify` plugin)
  are not MCP and were kept.

### P3-I — HUD flash / repair-card card loop (2026-10-09 evening)

- `McpManageDialog` wired a button to a missing `_remove` method, so the
  dialog raised during construction; the auto-heal sentry captured it and
  surfaced the repair card. Each subsequent event re-ran
  `_set_panel_visible(..., True)`, which restarted the fade, re-fit geometry
  and called `activateWindow()` — the blinking HUD window seen while speaking
  or typing. Fixed: `_remove` implemented; a visible repair panel is only
  refreshed (raise), never re-shown/activated. Tests:
  `tests/ui/test_mcp_manage_dialog.py`, `tests/ui/test_repair_card_focus.py`.

### P3-J — First typed message dropped before connect (2026-10-09 evening)

- `_on_text_command` silently returned while the Live session was still
  connecting (~13-16 s window with the chat window already usable). Fixed with
  a bounded pre-session queue, a one-time notice, and a flush after the startup
  briefing. Tests: `tests/unit/test_pending_text_queue.py`.

### P3-K — Chat log could stay above the fold (2026-10-09 evening)

- `_RoundedChatLog._scroll_bottom` could read a stale scroll maximum on its
  deferred tick and give up, leaving the newest message unreachable. Fixed
  with a layout flush and a synchronous re-read; one coalesced scroll per
  burst is preserved. Tests: `tests/ui/test_chat_autoscroll.py` (including
  the scroll-up-then-new-message scenario).

### P3-L — Typing-bubble paint raised on every repaint (2026-10-10)

- `ui.py:10833` bound the bubble painter with
  `self._paint_bubble.__get__(self._bubble, QFrame)` — a no-op on an already
  bound method, so `QFrame.paintEvent(row, event)` raised
  `TypeError: first argument ... must have type 'QFrame'` once per visible
  frame (285 recorded tracebacks). Fixed with `types.MethodType(...)` bound
  to the bubble plus a guarded, always-ended painter. Tests:
  `tests/ui/test_typing_bubble_paint.py`.

### P3-M — Protected faults never reached the approval UI (2026-10-10)

- `recovery/analyzer.parse` skipped every protected file (`main.py`, `ui*.py`),
  so a fault like the P3-L one produced "found no first-party file", no
  `repair_proposed` event, and a permanently empty patch preview in the HUD
  repair card ("UI Approval component failing to display"). Fixed: protected
  first-party frames are returned as candidates (`risk="protected"`), the
  patcher synthesises their proposal, and `_auto_allowed()` still refuses all
  automatic application — explicit on-screen approval remains mandatory.
  Tests: `tests/ui/test_repair_approval_display.py`,
  `tests/unit/test_recovery_auto_heal.py` (updated semantics).

### P3-N — Floating panels could not be dragged by their title (2026-10-10)

- The shared header rail only tagged the bar itself; the title/subtitle labels
  consumed the mouse press, so dragging the visible title did nothing. Fixed:
  the labels are `WA_TransparentForMouseEvents`, the press reaches the tagged
  bar and `MainWindow.eventFilter` moves the window (position remembered).
  Tests: `tests/ui/test_panel_drag.py`.

### P3-H — Stale/failing unrelated tests (CONFIRMED, PRE-EXISTING)

- `test_agent_reach_worker_runs_inside_its_environment` needs
  `C:\Users\Administrator\...\python.exe` (original dev machine path).
- `test_documents_architect::test_empty_content_...` reports
  `missing_dependency` (optional document lib not installed in `.venv`).
- Two `test_setup_overlay` tests are stale against the current overlay
  (signal arity / button names).
  None touch code changed in this pass; documented, not "fixed" by weakening
  the tests.

## 4. Repair plan (executed)

1. Crash first: mic-watchdog serialization + budget (P0-A). — done, verified.
2. UI freeze: incremental feed (P0-B). — done, verified.
3. Boot hygiene: Qt module mapping (P0-C), laya probe (P0-D). — done, verified.
4. Performance UI: verify through the real window path; no repair needed
   (P1-E). — done, verified.
5. Tests: regression tests for each fix; suite unblocked from the missing
   Spotify import; all failures triaged. — done.
6. Cleanup: nothing deleted; inventory + classification documented (P5).
7. Docs: this file; `TODO.md` ledger.

## 5. Verification snapshot

- `tests/unit`: **1191 passed, 4 failed (pre-existing), 15 skipped**
  (12 Spotify-module skips + 3 pre-existing) in 2:10.
- New regression tests: `tests/ui/test_activity_feed_incremental.py` (4),
  `tests/unit/test_dependency_ops_qt_mapping.py` (2) — green.
- UI spot checks: `test_performance_tab.py` (4), `test_mic_waveform_idle.py`
  (4), `test_logo_paint_perf.py` (11), `test_ui_control_open_verify.py` (15)
  — all green.
- Runtime: three 45 s offscreen boots after fixes → `EXITCODE=0`, no new
  crash dumps (26 before, 26 after), "Process exiting cleanly (force_exit)".
  Before fixes: 2/2 boots crashed `0xC0000005`.
- PERF UI lifecycle script (offscreen, real `SettingsWindow`): open → scan on
  exactly 1 thread → cancel → buttons restored → close (no leaked thread) →
  reopen (exactly 1 tab) → exit 0.

## 6. Remaining risks

See `TODO.md` §Remaining risks (Spotify module, mic edge cases, MCP re-publish
idempotency, startup time, pre-existing test failures).
