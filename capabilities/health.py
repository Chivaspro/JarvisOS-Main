"""Backend health checks for the capability layer.

Follows the project's existing external-backend pattern: diagnostics asks one
function for one Check per engine, and every state comes from a probe that
actually ran, never from a config file's optimism.
"""

from __future__ import annotations

from typing import Callable


def _check(name: str, state: str, detail: str, fix: str, **evidence):
    from core.diagnostics import Check
    return Check(name, state, detail, fix, dict(evidence))


def capabilities_check() -> "Check":
    """One summary line for the whole capability layer."""
    from core.diagnostics import DEGRADED, ERROR, OFFLINE, ONLINE
    try:
        from capabilities.router import backends_status
        st = backends_status()
    except Exception as exc:
        return _check("capabilities", ERROR, f"{type(exc).__name__}: {exc}",
                      "The capabilities package failed to import.")

    ready, degraded, offline = [], [], []
    for key, info in st.items():
        usable = info.get("usable", info.get("available"))
        if usable:
            ready.append(key)
        elif info.get("version") or info.get("interpreter") or info.get("cli"):
            degraded.append(key)
        else:
            offline.append(key)

    if len(ready) == len(st):
        return _check("capabilities", ONLINE,
                      f"all {len(ready)} control engines ready "
                      f"({', '.join(ready)})", "",
                      ready=ready)
    if ready or degraded:
        return _check("capabilities", DEGRADED,
                      f"ready: {', '.join(ready) or 'none'}; "
                      f"degraded: {', '.join(degraded) or 'none'}; "
                      f"absent: {', '.join(offline) or 'none'}",
                      "See the per-engine lines below for the exact missing setting.",
                      ready=ready, degraded=degraded, absent=offline)
    return _check("capabilities", OFFLINE,
                  "no control engine was found",
                  "Install Agent TARS ('npm install') and configure Agent Reach, "
                  "or set JARVIS_AGENTREACH_ENV to the folder containing .venv.",
                  absent=offline)


def engine_checks() -> list:
    """One Check per engine and feature package, with real probe evidence."""
    checks = []
    for name, module_name in (("agent_tars", "agent_tars"),
                              ("agent_reach", "agent_reach")):
        try:
            import importlib
            module = importlib.import_module(f"capabilities.{module_name}")
            checks.append(module.health_check())
        except Exception as exc:
            from core.diagnostics import ERROR
            checks.append(_check(name, ERROR, f"probe failed: {exc}",
                                 "Check the capabilities package install."))
    checks.extend(local_controller_checks())
    checks.extend(feature_checks())
    return checks


def feature_checks() -> list:
    """One Check per on-demand feature package, from a real import probe.

    These are not engines and their absence is not a fault: the document
    architect, the clipboard sentry, the smart-home layer and the MCP host all
    answer honestly when their package is missing, and each row carries the
    exact command that would add it. They appear in diagnostics so the answer to
    "can you write me a spreadsheet?" is visible before anything is asked.
    """
    from core.diagnostics import DEGRADED, ONLINE
    try:
        from app.installer import feature_status
        rows = feature_status()
    except Exception as exc:
        return [_check("feature packages", DEGRADED,
                       f"probe failed: {type(exc).__name__}: {exc}",
                       "Check the app/installer.py install.")]
    checks = []
    for row in rows:
        ready = bool(row.get("ready"))
        detail = str(row.get("purpose") or "")
        if row.get("detail"):
            detail = f"{detail} — {row['detail']}"
        checks.append(_check(f"{row.get('feature')} · {row.get('package')}",
                             ONLINE if ready else DEGRADED, detail,
                             "" if ready else str(row.get("fix") or ""),
                             package=row.get("package"), ready=ready))
    return checks


#: Local controller probe name → (label, what to install when it is missing,
#: the Settings switch that gates it).
_LOCAL_CONTROLLERS = {
    "desktop": ("pc_control (pywinauto + PyAutoGUI)",
                "pip install pyautogui pywinauto", "physical_input"),
    "playwright": ("browser_action (Playwright)",
                   "pip install playwright && playwright install chromium",
                   "playwright_browser"),
    "uitars_visual": ("visual_control (UI-TARS on Gemini)",
                      "keep the UI-TARS/codes folder and add the Gemini API key",
                      "uitars_visual"),
    "power_control": ("power_control (Windows system power)",
                      "run J.A.R.V.I.S. on Windows with PowerShell available",
                      "power_control"),
}


def local_controller_checks() -> list:
    """One Check per local controller, from a real probe.

    "Enabled" is never reported for something that cannot act: the state comes
    from the probe, and a switched-off controller says exactly which Settings
    switch turned it off.
    """
    from core.diagnostics import DEGRADED, OFFLINE, ONLINE
    from capabilities.router import local_status
    checks = []
    for key, info in (local_status() or {}).items():
        label, fix, switch = _LOCAL_CONTROLLERS.get(key, (key, "", key))
        enabled = info.get("enabled")
        if enabled is False:
            checks.append(_check(label, OFFLINE,
                                 f"switched off in Settings ({switch})",
                                 f"Enable '{switch}' in Settings to allow it."))
            continue
        if info.get("usable"):
            checks.append(_check(label, ONLINE, str(info.get("detail") or ""), "",
                                 switch=switch))
        else:
            checks.append(_check(label, DEGRADED,
                                 str(info.get("detail") or "not ready"), fix))
    return checks


def status_snapshot(enabled: Callable[[str], bool] | None = None) -> dict:
    """Secret-free snapshot for the dashboard, Settings and the API."""
    from capabilities import config
    from capabilities import locks
    from capabilities.router import backends_status
    st = backends_status()
    return {
        "paths": config.summary(),
        "engines": {k: {"available": bool(v.get("available")),
                        "usable": bool(v.get("usable", v.get("available"))),
                        "version": v.get("version", ""),
                        "python": v.get("python", ""),
                        "cli": v.get("cli", ""),
                        "model": v.get("model", ""),
                        "detail": v.get("detail", ""),
                        "missing": v.get("missing") or [],
                        "active_channels": v.get("active_channels") or []}
                    for k, v in st.items()},
        "locks": locks.status(),
        "permissions": {
            "desktop_control": (enabled("desktop_control") if enabled else None),
            "web_task": (enabled("web_task") if enabled else None),
            "webview": (enabled("webview") if enabled else None),
            "playwright_browser": (enabled("playwright_browser") if enabled else None),
            "windows_automation": (enabled("windows_automation") if enabled else None),
            "uitars_visual": (enabled("uitars_visual") if enabled else None),
            "physical_input": (enabled("physical_input") if enabled else None),
        },
    }
