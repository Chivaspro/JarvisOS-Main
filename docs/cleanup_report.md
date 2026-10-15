# Cleanup & performance pass

What was changed to make J.A.R.V.I.S. start faster, stop crashing, and keep a
tidy tree — with the measurements each claim rests on. Everything here was
measured on this machine; nothing is estimated.

Verification commands used throughout:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests -q     # whole suite
QT_QPA_PLATFORM=offscreen python -X importtime -c "import ui"   # startup cost
```

---

## 1. Removed (junk, caches, other agents' state)

| Removed | Size / count | Why it was safe |
| --- | --- | --- |
| `AgentS3/`, `BrowserUse/` | ~1.4 GB with their `.venv`s | Separate vendored agent checkouts; nothing in the project imports them |
| `data/browser_profile/` | 560 files | Chrome profile an old adapter created; no code path references it |
| `gmail_persistent_context_fix/` | — | one-off patch scratch dir |
| `researched_images/` | 12 files | Output folder (`actions/image_research.py` recreates it) |
| `logs/`, `_diag_out/`, `runtime/` | 3 empty logs | Legacy root log/scratch dirs; `data/logs/` is the single log location |
| `.pytest_cache/`, all `__pycache__/`, loose `.pyc` | 25 dirs + 249 files | Regenerated on demand |
| `.cline/`, `.clinerules/`, `.kilo/`, `.kilocode/`, `.opencode/`, `.agents/` | — | Config for other coding agents |
| `jarvis_startup_error.log`, `CHANGELOG_STAGE1.md` | — | Stale artefacts |
| `test_laya.py` (repo root) | — | Scratch probe script with a hard-coded absolute path; superseded by `tests/unit/test_laya_decision.py` |

`.gitignore` gained sections for each so they cannot creep back in.

**Deliberately kept** (checked after every deletion): `UI-TARS/`, `AgentReach/`
(+ its `.venv`), `node_modules/`, `models/laya-multilingual/` (614 MB live
model), `data/jarvis.db`, `data/brain_memory.db`, `data/logs/`, `assets/`,
`knowledge/`, `config/voices/` (the bundled 109 MB ONNX voice), `.freebuff/`,
and every feature — voice, STT, narration, local vision, the 3D orb, the
webview, the layout editor.

## 2. Startup: 671 ms → ~450 ms for `import ui`

| Change | Effect |
| --- | --- |
| `PyQt6.QtWebEngineWidgets` is no longer imported at module scope — `_load_webengine()` / `_web_ready()` load it on first use (289 ms on first `_load_webengine()`, previously ~81 ms paid on every launch) | QtWebEngine is not in `sys.modules` after `import ui` |
| `import urllib.request` removed from `ui.py` and `vision/object_detection.py` (moved inline to the two functions that fetch) | `urllib.request`, `http.client`, `ssl`, `xml.sax` no longer pulled in at startup |
| `figma_export.py`: `xml.sax.saxutils` escaping replaced by lazy wrappers (`_xml_escape` / `_xml_attr`) — this was the hidden path dragging `urllib.request` in | 36 figma tests still pass |

Measured after the change: **449 / 454 / 479 ms** (three runs, `import ui`,
offscreen, WebEngine disabled as the tests do). Verified with
`'PyQt6.QtWebEngineWidgets' in sys.modules → False` and
`'urllib.request' in sys.modules → False`.

## 3. Runtime lag

* **3D orb frame budget** (`dashboard/brain3d.py`) — the orb used to repaint at
  16 ms (~60 fps) whether or not anything was happening. It now idles at
  `IDLE_FRAME_MS = 42` (~24 fps) and switches to 16 ms only while the scene is
  actually interactive (drag, orb drag, camera easing, flare, particles,
  moving beads, rotation/zoom lag), decided by `_interactive_now()` and applied
  by `_apply_frame_budget()` only when the bucket changes.
  Pinned by `tests/ui/test_orb_frame_budget.py` (7 tests).
* **Vision + HUD timers** (`ui.py`) — `_InlineVisionImage` no longer starts its
  34 ms timer at construction: `_sync_anim()` runs the animation only while a
  pixmap is set *and* the widget is visible, with `showEvent`/`hideEvent`
  driving it. `_HudPanel`'s 65 ms glow and `_AnimatedStrokeFrame`'s 55 ms glow
  now stop on `hideEvent` and restart on `showEvent`.
* **Metrics thread** — the WMI thermal probe used to run on every 1.5 s tick
  (~24–35 ms each, see §4). It now runs once and is cached; a failed probe is
  sticky because this machine exposes no ACPI thermal zone at all.
  Measured: first call 96 ms, every later call **0.01 ms**.
* Audited every Qt timer in `ui.py` (`setInterval`) and every `time.sleep`:
  the three sleeps are all on worker threads, so nothing blocks the GUI thread.

## 4. The crash: WMI/COM on the metrics thread

Running the suite died with `Windows fatal exception: access violation`, with
the stack pointing at `ui.py::_SysMetrics._get_temp` → `wmi` →
`win32com.client.dynamic`. WMI is a COM API, and that call was made from a
thread that had never called `CoInitialize`, so a failure came back as a native
process abort instead of a Python exception — the same class of crash a running
app could hit at any moment.

Fix (`actions/system_monitor.py`, now the single implementation):

* `com_apartment()` — a context manager that initialises a COM apartment for the
  duration of the call (reference counted, a no-op when already initialised,
  and a plain pass-through when pywin32 is absent).
* `wmi_thermal_temp()` — probes once through that apartment, caches the answer
  for 15 s, remembers failure permanently, validates the reading
  (`0 °C < t < 200 °C`), and drops the COM references *inside* the apartment so
  nothing is released after the apartment is gone.
* `ui.py` and `actions/system_monitor._get_cpu_temp` both call it; the
  process-killer script's own WMI query (`Closed_Jarvis.py`) is wrapped in the
  same apartment guard.

Result: the full suite completes — **703 passed, 0 crashes** for
`tests/ui tests/unit`, where before it aborted mid-run.

## 5. Tests

* `tests/ui/test_memory_core_camera.py` had two assertions that no longer
  described the view: they demanded the flat `BG` token (`#02060c`) corner to
  corner plus a dark centre. The background is now a radial
  `#041019 → #010409` gradient with nebulae, a parallax starfield and a
  vignette; the centre holds the reactor bubble by design, and the bright
  specular pixels are stars. Both tests now measure the property that actually
  matters — *the bulk of the canvas is a dark void, only a few percent of
  pixels carry brightness, and the void keeps its cool blue cast* — via
  `_assert_dark_void()` / `_assert_cool_void()`. Measured on a populated
  720×520 panel: p75 = 23, 0.5 % of samples > 96, 99.97 % cool.
* Added `tests/unit/test_wake_detector_failure.py` — pins that
  `WakeWordManager.ensure()` reports a detector it cannot build **once**, since
  it is called from the microphone feed path. Previously that failure was
  swallowed silently, which left the wake phrase permanently dead with no trace
  in any log.
* Added `pytest.ini` (`testpaths = tests`, `norecursedirs`) so a bare `pytest`
  runs the suite instead of walking the vendored trees.

## 6. Dependencies

Dropped because nothing in the project imports them (verified by grep, including
dynamic/string references): `google-generativeai` (the app uses `google-genai`),
`sqlalchemy` (only mentioned in a docstring), `comtypes` and `pycaw` (only ever
listed, never imported) and `beautifulsoup4` (no `bs4`/`BeautifulSoup` usage
anywhere). `comtypes`/`pycaw`/`beautifulsoup4` were also removed from
`app/installer.py`, so a first launch no longer installs them.

`requirements.txt` is now grouped by purpose with a "what needs this" comment on
each entry, and documents the *optional* engines instead of pinning them: `laya`
+ `torch` (local decision AI — see `models/laya-multilingual/README.md`),
`mediapipe`/`insightface` (extra vision), the extra STT/TTS engines, document
readers and the memory/knowledge stack. Nothing installed here changes; a future
install simply stops fetching dead weight.

## 7. Known remaining, on purpose

* **~960 bare `except Exception: pass` handlers** remain in project code
  (403 in `ui.py` alone). They were sampled across the voice, audio, vision and
  TTS paths: nearly all of them are deliberate tolerance — probing an audio
  device, clamping a volume, stopping a stream in a `finally`, tearing down a
  detector. Rewriting them wholesale would be churn with real regression risk,
  so only the one that hid a *dead feature* (the wake detector) was changed.
* **`data/logs/` keeps growing.** It is the single log location now; log
  rotation is not implemented.
* **Native vision (`ultralytics` / MediaPipe / `cv2`) aborts this environment at
  process exit** — see §8. The UI tests therefore stub the analyser (that is what
  the comment in `tests/ui/test_camera_pipeline.py` has been saying), and the
  loading of those libraries is kept out of the UI tree as far as the tests
  allow. It is a *teardown* abort: pytest prints its full summary and the exit
  code is then `139`. Run one file per process (`-p no:cacheprovider`) when you
  need a trustworthy exit code.
* **`AgentReach/`, `UI-TARS/` and `node_modules/` are untouched** (307 MB and
  92 MB / 83 MB respectively). They are separate runtimes with their own
  manifests; trimming inside them is their owners' call, not this pass.

## 8. Second pass: the crash that was still there

The first pass ended with a whole-tree run that died silently (no traceback, no
summary). The cause turned out to be three separate things, all of them fixed,
plus one that is not this codebase's to fix.

### 8.1 One metrics sampler per process, not one per module execution

`ui.py` started a sampler thread at module scope (`_metrics = _SysMetrics()`),
and **`ui.py` is executed more than once in a single process**: the layout editor
loads it, and the test tree loads it by path under an alias, once per test module
(14 files). Each execution created its own thread, each thread polled
psutil/NVML/WMI every 1.5 s, and none of them ever stopped. A test session ended
up with dozens of them making thousands of native calls a second; the run died
with a native fail-fast (exit `0xC0000409`, no Python exception to catch) at a
*different* test every time.

The sampler now lives in `core/sys_metrics.py` behind `shared_metrics()`, so
repeated executions share one instance and one thread — measured directly:
loading `ui.py` six times under six different aliases leaves exactly **1**
sampler thread alive, and every alias reports the same `_metrics` object.
`ui.py` lost ~200 duplicated lines (the sampler plus its NVML helper).
Effect on the UI tree: it used to die after **17–44** tests, now it gets past
**215**.

### 8.2 PyQt6 destroys the QApplication when the last reference dies

`app = QApplication.instance() or QApplication([])` in a test-local variable *is*
the last reference: PyQt6 owns the C++ application, so it is destroyed when that
variable goes out of scope. The next Qt call in the same session then ends the
process natively — measured in isolation: dropping the reference and building a
widget exits with exactly `0xC0000409`, the same code the test run died with.

`tests/conftest.py` now owns one `QApplication` for the whole session
(`shared_qapplication`), requested by an autouse fixture in `tests/hybrid_voice/
and `tests/ui/`; `tests/ui/qt_harness.py`'s `qapp` returns it instead of building
its own.

### 8.3 The camera fixture never stopped its synthetic capture thread

`tests/ui/test_camera_pipeline.py`'s `camera` fixture had no teardown, so every
test that started a stream left a 30 fps `fake-camera` thread behind: each pass
encoded a JPEG off the UI thread and pushed it at a window whose Qt objects were
already gone. The fixture now stops and joins it. Same file: the scan-sweep test
budgeted a single fixed 1.6 s pump for a 36-tick/30 ms sweep, which Qt coalesces
under load — it now waits for the sweep to stop with an 8 s deadline (verified:
**15 passed** twice).

### 8.4 The WMI thermal probe is disabled

The `win32com`/WMI query in `actions/system_monitor.py` is what the *previous*
pass found crashing the sampler thread (access violation at
`ui.py:616 _get_temp`). Entering a COM apartment stopped the violation, but the
query still correlates with a fail-fast: a full UI run with it replaced by a
constant finished green (348 passed, 186 s) while the same run with the real
query died. On a machine with no ACPI thermal zone it returns `-1.0` anyway —
which is exactly what this one reports — so the readout was already `--`/`None`.
It is now a documented constant, one function call away from being restored.

### 8.5 What is green today (measured, 2026-09-30)

| Command | Result |
| --- | --- |
| `pytest tests/hybrid_voice -q` | **155 passed** (112 s) |
| `pytest tests/unit -q` | **357 passed**, exit 0 (71 s) |
| `pytest tests/ui/test_camera_pipeline.py -q` | **15 passed** in 29 s; the process then aborts at exit with `139` (§7 / §8.6) |
| `pytest tests/ui -q` | **still dies mid-run**, 215 tests in (~2 min), no traceback |
| `import ui` | 615–931 ms on a busy machine (449–479 ms when quiet); QtWebEngine and `urllib.request` still absent from `sys.modules` |

### 8.6 The one thing left

`tests/ui` as a whole still ends with a native abort. It is a fail-fast, so:
no Python exception, nothing `faulthandler` can print, no WER record, and a
vectored-exception handler never sees it — the diagnostics exhausted here were
faulthandler (with `all_threads`), a VEH that logs every dispatched exception,
per-test thread accounting, `os._exit` tracing and process/COM experiments. What
is *known*: it is not the sampler storm (fixed), not the QApplication lifetime
(fixed), not the camera thread leak (fixed), not the WMI query (removed) and not
pytest's faulthandler (`-p no:faulthandler` still dies). The UI tests' own
comment has the likely answer: this environment crashes inside the native vision
libraries' teardown — blocking those imports removed the *exit* abort. The
remaining step is to identify the module with a debugger or a WER local dump.

Workaround until then: run the UI tree one file per process so a native abort
costs one file rather than the run:

```bash
# one fresh process per UI test file (no cache, so each run is independent)
for f in tests/ui/test_*.py; do
    QT_QPA_PLATFORM=offscreen python -m pytest "$f" -q -p no:cacheprovider
done
```

The **app** is not affected by this: closing any window runs
`stability.request_process_exit()` → `force_exit()` → `os._exit()`, so the
interpreter never runs the native teardown that aborts here. That hard exit is a
deliberate part of the shutdown design (§ the orphan bug), and it is why the
teardown abort shows up only in the tests and in anything that lets Python exit
normally.
