# docs/

Project documentation that is not part of the main README.

| Document | What it covers |
| --- | --- |
| [`cleanup_report.md`](cleanup_report.md) | The cleanup / performance pass: what was removed, the measured startup and runtime improvements, the WMI/COM crash fix, test changes and the dependency trim — plus what was left alone and why. |
| [`agent_tars_integration.md`](agent_tars_integration.md) | Agent TARS integration notes. |
| [`pc_web_control.md`](pc_web_control.md) | PC and web control: the four local controllers (Playwright · pywinauto · UI-TARS-on-Gemini · PyAutoGUI), the action registry, routing, settings, activity rows and how each layer was verified. |

Everything else lives in the root [`readme.md`](../readme.md): capabilities,
install, AI provider setup, voice/TTS compatibility, vision setup, optional
packages, configuration and feature flags.

## Working on the project

```bash
# the two trees that are reliably green
QT_QPA_PLATFORM=offscreen python -m pytest tests/hybrid_voice -q   # 155 passed
QT_QPA_PLATFORM=offscreen python -m pytest tests/unit -q           # 390 passed

# the UI tree aborts natively in this environment (§7/§8 of cleanup_report.md),
# so run it one file per process to keep a native abort local to one file
for f in tests/ui/test_*.py; do
    QT_QPA_PLATFORM=offscreen python -m pytest "$f" -q -p no:cacheprovider
done

# what `import ui` costs at startup, and what it must NOT pull in
QT_QPA_PLATFORM=offscreen python -X importtime -c "import ui"
```

`pytest.ini` pins `testpaths = tests` so a bare `pytest` never walks the
vendored trees (`UI-TARS/`, `AgentReach/`, `node_modules/`, `models/`).

The PC/web control layer is covered offline by `tests/unit/test_pc_web_control.py`
(registry ↔ handler agreement, permission refusals, approval tiering, coordinate
conversion, and the UI-TARS parse→execute mapping with a fake executor). It never
opens a browser, moves the pointer or touches the network.

## Performance rules this codebase follows

1. **Nothing heavy on the import path.** QtWebEngine, `urllib.request` and
   similar are imported inside the function that needs them (`_load_webengine()`
   in `ui.py`), never at module scope.
2. **Timers run only when they can be seen.** A widget that is hidden or holds
   no pixmap stops its animation timer and restarts it in `showEvent`.
3. **Animation drops to an idle frame budget.** The 3D orb paints at ~24 fps
   when nothing is happening and 60 fps only during interaction
   (`dashboard/brain3d.py`).
4. **A failure is either handled or reported — never silently fatal.** Anything
   that can take the process down natively (COM, device handles) is probed in a
   guarded way and cached, and a failure that kills a user-visible feature is
   logged at least once instead of being swallowed.
5. **Background pollers are process-wide and never touch COM.** `ui.py` is
   executed more than once in a process (the layout editor, the tests), so a
   module-level thread is a thread *per execution*; `core/sys_metrics.py` keeps
   one sampler for the whole process instead. The ACPI/WMI read it used to make
   is disabled: win32com from such a thread ends the process natively
   (`actions/system_monitor.wmi_thermal_temp` documents it).
