"""Agent TARS adapter — the browser + PC automation engine.

Drives the installed ``@agent-tars/cli@0.3.0`` Node runtime. Nothing here
reimplements automation and there is no fake wrapper: if Agent TARS cannot run,
this adapter says exactly what is missing and the router does not substitute
another engine.

Model: Agent TARS v0.3.0 has a native ``gemini`` provider, so J.A.R.V.I.S.'s
existing Gemini key runs TARS's own agent loop — no second provider and no
second credential. There is deliberately no UI-TARS/Ark key path here: the
UI-TARS-family work in this project is the visual layer
(``capabilities/uitars.py``) plus the local controllers, and all of it runs on
the one Gemini configuration. A provider left pointing somewhere else reads as
unsupported rather than silently borrowing another key. Two verified details are
baked in:

* ``--toolCallEngine prompt_engineering``: the native Gemini function-calling path
  is rejected by the generativelanguage API (TARS emits JSON-Schema
  ``$schema``/``additionalProperties`` fields that API does not accept).
* The ``agent_tars`` model defaults to ``gemini-flash-latest`` and falls back to
  the lighter ``gemini-flash-lite-latest`` once on a transient provider error
  (503/429), mirroring app.model_router's role/fallback behaviour.

Capability boundary (verified against the installed package, not assumed):
Agent TARS v0.3.0's built-in local automation is browser + shell commands +
filesystem + search. It has no local pixel-level desktop GUI controller — that
only exists as its remote ``aioSandbox`` environment. Desktop requests are
therefore carried out through TARS's ``run_command``/filesystem tools (open or
launch applications, run commands); in-app GUI clicking is reported as not
supported by this installed version rather than pretended.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

_ENGINE = "agent_tars"

_lock = threading.Lock()
_status_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_STATUS_TTL = 300.0
_VERSION_CACHE: dict[str, str] = {}
#: The model that last completed a task. Tried first next time so a busy primary
#: does not cost a failed call (and a server restart) on every single task.
_preferred_model = ""


def _log(message: str) -> None:
    try:
        from observability.logger import log_event
        log_event("INFO", "capabilities", "agent_tars", detail=message[:400])
        return
    except Exception:
        pass
    print(f"[AgentTars] {message}")


# ── runtime discovery ─────────────────────────────────────────────────────────

def runtime() -> dict:
    """Resolved Node + CLI paths, with the reason when either is missing."""
    from capabilities import config
    node = config.agent_tars_node()
    cli = config.agent_tars_cli()
    missing = []
    if not node:
        missing.append("Node.js is not installed or not on PATH")
    if not cli:
        missing.append("Agent TARS CLI is not installed (run 'npm install')")
    return {"node": node, "cli": cli, "missing": missing}


def available() -> bool:
    """Switched on AND installed (node + CLI both present)."""
    from capabilities import config
    if not config.enabled(_ENGINE):
        return False
    r = runtime()
    return bool(r["node"] and r["cli"])


def ready() -> bool:
    """Installed AND usable: the configured provider has its credential."""
    from capabilities import config
    return available() and bool(config.agent_tars_credential()[1])


def version(force: bool = False) -> str:
    """Real ``agent-tars --version`` probe (e.g. '0.3.0'). Cached.

    ``--version`` prints plain text (`agent-tars/0.3.0 win32-x64 node-v26.7.0`),
    not JSON, so it is read directly rather than through the JSON worker path.
    """
    from capabilities import proc
    r = runtime()
    if not (r["node"] and r["cli"]):
        return ""
    if not force and _VERSION_CACHE.get(r["cli"]):
        return _VERSION_CACHE[r["cli"]]
    text = ""
    try:
        import subprocess
        p = subprocess.run([r["node"], r["cli"], "--version"],
                           capture_output=True, text=True, timeout=40,
                           **proc._NO_WINDOW)
        blob = (p.stdout or "") + "\n" + (p.stderr or "")
        for token in blob.split():
            if token.startswith("agent-tars/"):
                text = token.split("/", 1)[1]
                break
    except Exception:
        text = ""
    if text:
        _VERSION_CACHE[r["cli"]] = text
    return text


def status(timeout_s: float = 0.0, force: bool = False) -> dict:
    """Probe Agent TARS for real. Never raises."""
    from capabilities import config
    from capabilities import tars_server

    with _lock:
        age = time.time() - _status_cache["at"]
        if not force and _status_cache["payload"] and age < _STATUS_TTL:
            return _status_cache["payload"]

    r = runtime()
    base = {
        "name": _ENGINE,
        "kind": "node",
        "node": r["node"],
        "cli": r["cli"],
        "enabled": config.enabled(_ENGINE),
        "provider": config.agent_tars_provider(),
        "model": config.agent_tars_model(),
        "fallback_model": config.agent_tars_fallback_model(),
        "mode": config.agent_tars_mode(),
        "browser_control": config.agent_tars_browser_control(),
        "cdp_endpoint": config.agent_tars_cdp_endpoint(),
        "workspace": str(config.agent_tars_workspace()),
    }

    if not r["node"] or not r["cli"]:
        payload = {**base, "available": False, "usable": False, "version": "",
                   "missing": list(r["missing"]),
                   "detail": "; ".join(r["missing"]) or "Agent TARS is not installed",
                   "error": ""}
    elif not base["enabled"]:
        payload = {**base, "available": False, "usable": False, "version": "",
                   "missing": ["switched off in Settings"],
                   "detail": "disabled in Settings", "error": ""}
    else:
        ver = version()
        key_env, key = config.agent_tars_credential()
        key_present = bool(key)
        provider_ok = config.agent_tars_provider_supported()
        server = tars_server.status()
        missing = []
        if not ver:
            missing.append("Agent TARS CLI did not answer --version")
        if not provider_ok:
            missing.append(
                "Agent TARS is set to a provider other than Gemini; the only "
                "supported model source is the existing Gemini key")
        if not key_present:
            missing.append("no Gemini API key configured for Agent TARS")
        usable = bool(ver) and key_present and provider_ok
        detail = (f"Agent TARS {ver or '(no version)'}; model "
                  f"{base['provider']}/{base['model']}; browser "
                  f"{base['browser_control']}") if ver else (
                  "; ".join(missing) or "Agent TARS CLI not answering")
        payload = {**base, "available": bool(ver), "usable": usable, "version": ver,
                   "credential_env": key_env, "credential_present": key_present,
                   "server": server, "missing": missing, "detail": detail, "error": ""}

    with _lock:
        _status_cache["at"] = time.time()
        _status_cache["payload"] = payload
    return payload


# ── task execution ────────────────────────────────────────────────────────────

def _compose_query(task: str, url: str, mode: str) -> str:
    goal = str(task or "").strip()
    link = str(url or "").strip()
    if link and goal:
        return f"Open {link} and then {goal}"
    if link:
        return f"Open {link} and report what is on the page."
    return goal


def _extract_cli_json(stdout: str) -> dict | None:
    """Last JSON object on stdout (the CLI pretty-prints its result)."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    candidates: list[dict] = []
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
    if not candidates:
        return None
    # The CLI prints {"sessionId": ..., "result": {...}}. Scanning every '{' also
    # decodes the inner result object, so prefer the outer envelope.
    for obj in candidates:
        if "result" in obj or "sessionId" in obj:
            return obj
    return max(candidates, key=len)


def _oneshot(query: str, model: str, timeout_s: float) -> dict:
    """Run one task as a single headless CLI call (no server, final result only)."""
    from capabilities import config, proc
    r = runtime()
    argv = [r["node"], r["cli"], "--headless", "--input", query, "--format", "json",
            "--model.provider", config.agent_tars_provider(), "--model.id", model,
            "--toolCallEngine", config.agent_tars_tool_call_engine(),
            "--browser.control", config.agent_tars_browser_control()]
    endpoint = config.agent_tars_cdp_endpoint()
    if endpoint:
        argv += ["--browser.cdpEndpoint", endpoint]
    if config.agent_tars_planner():
        argv += ["--planner.enable"]
    workspace = config.agent_tars_workspace()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        argv += ["--workspace", str(workspace)]
    except OSError:
        pass
    key_env, key = config.agent_tars_credential()
    res = proc.spawn_argv(argv, engine=_ENGINE, timeout_s=timeout_s + 60.0,
                          env_extra={key_env: key} if (key_env and key) else None,
                          cwd=str(config.BASE_DIR),
                          label=f"agent_tars:oneshot:{query[:28]}")
    # The result JSON is on stdout. `spawn_argv` parses a single-line object
    # directly; the CLI pretty-prints, in which case stdout arrives in `log`.
    data = res if isinstance(res.get("result"), dict) else None
    if data is None:
        data = _extract_cli_json(str(res.get("log") or ""))
    if res.get("error_code") and data is None:
        return {"ok": False, "error_code": res["error_code"],
                "error": res.get("error", ""), "output": res.get("error", ""),
                "steps": [], "session_id": "", "model": model,
                "elapsed_s": res.get("elapsed_s", 0)}
    if not isinstance(data, dict):
        return {"ok": False, "error_code": "bad_output",
                "error": "Agent TARS returned no readable result.",
                "output": "Agent TARS returned no readable result.",
                "steps": [], "session_id": "", "model": model,
                "elapsed_s": res.get("elapsed_s", 0)}
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    content = str((result or {}).get("content") or "").strip()
    finish = str((result or {}).get("finishReason") or "")
    if finish == "error" or not content:
        return {"ok": False,
                "error_code": "rate_limited" if _transient(content) else "failed",
                "error": content or "Agent TARS returned no final answer.",
                "output": content or "Agent TARS returned no final answer.",
                "steps": [], "session_id": str(data.get("sessionId") or ""),
                "model": model, "elapsed_s": res.get("elapsed_s", 0)}
    return {"ok": True, "error_code": "", "error": "", "output": content,
            "steps": [], "session_id": str(data.get("sessionId") or ""),
            "model": model, "elapsed_s": res.get("elapsed_s", 0)}


def _transient(text: str) -> bool:
    from capabilities import tars_server
    return tars_server._transient(text)


def run_task(task: str, url: str = "", mode: str = "browser", max_steps: int = 0,
             timeout_s: float = 0, on_event: Callable[[str], None] | None = None) -> dict:
    """Execute one automation task through Agent TARS. Never raises.

    ``mode`` is 'browser' (web workflows) or 'computer' (desktop: launch and
    control applications via TARS's commands/filesystem tools). ``max_steps`` is
    accepted for tool-schema compatibility but is not forwarded: v0.3.0 exposes
    no step-limit flag, so TARS governs its own loop budget.
    """
    from capabilities import config

    goal = str(task or "").strip()
    if not goal and not str(url or "").strip():
        return {"ok": False, "backend": _ENGINE, "reason": "no_input",
                "output": "Agent TARS needs a task, a URL, or both."}

    if not available():
        r = runtime()
        return {"ok": False, "backend": _ENGINE, "reason": "unavailable",
                "output": ("PC/browser automation is unavailable: Agent TARS is "
                           "not installed or is switched off in Settings ("
                           + ("; ".join(r["missing"]) or "disabled")
                           + "). The task was not attempted and no other control "
                             "engine was substituted."),
                "missing": r["missing"]}

    if not config.agent_tars_provider_supported():
        return {"ok": False, "backend": _ENGINE, "reason": "provider_unsupported",
                "output": ("PC/browser automation is unavailable: Agent TARS is "
                           "configured with a model provider other than Gemini. "
                           "UI-TARS has no credential of its own here — it runs on "
                           "the existing Gemini key. Set the provider back to "
                           "'gemini'; no substitute engine was used.")}

    if not config.agent_tars_credential()[1]:
        return {"ok": False, "backend": _ENGINE, "reason": "no_gemini_key",
                "output": ("PC/browser automation is unavailable: Agent TARS runs "
                           "its own loop on Gemini and no Gemini API key is "
                           "configured. Add the Gemini API key on Settings \u25b8 "
                           "API; there is no separate UI-TARS key. No substitute "
                           "engine was used.")}

    query = _compose_query(goal, url, mode)
    if mode == "computer":
        # TARS's run_command waits for the command to exit, so a GUI app launched
        # directly blocks until its 60s MCP timeout. Steer it to a non-blocking
        # launch (its own retry loop learns this, but a hint avoids two timeouts).
        query = ("Using the desktop application tools (run_command / filesystem), and "
                 "launching GUI applications in a non-blocking way (for example "
                 "PowerShell Start-Process so the command returns immediately), " + query)
    budget = float(timeout_s or config.agent_tars_timeout())
    server_mode = config.agent_tars_mode() == "server"

    # Order: the model that last worked, then the configured primary, then the
    # fallback — deduplicated. Starting from the last known-good model avoids
    # paying the provider's 503 and a server restart on every task.
    global _preferred_model
    preferred = _preferred_model
    models: list[str] = []
    for candidate in (preferred, config.agent_tars_model(),
                      config.agent_tars_fallback_model()):
        if candidate and candidate not in models:
            models.append(candidate)
    attempts = max(1, min(len(models), 1 + config.agent_tars_retries()))

    started = time.monotonic()
    result: dict = {}
    used_model = models[0]
    for index in range(attempts):
        used_model = models[min(index, len(models) - 1)]
        if server_mode:
            from capabilities import tars_server
            result = tars_server.submit(query, on_event=on_event,
                                        timeout_s=budget, model=used_model)
        else:
            result = _oneshot(query, used_model, budget)
        if result.get("ok"):
            _preferred_model = used_model
            break
        if not _transient(str(result.get("error") or "") + str(result.get("error_code") or "")):
            break
        _log(f"transient provider error on {used_model}; retrying")

    elapsed = result.get("elapsed_s", round(time.monotonic() - started, 2))
    ok = bool(result.get("ok"))
    steps = result.get("steps") or []
    output = _render(ok, result, used_model, mode)
    return {
        "ok": ok,
        "backend": _ENGINE,
        "output": output,
        "reason": "" if ok else str(result.get("error_code") or "failed"),
        "steps": steps,
        "final_result": str(result.get("output") or "") if ok else "",
        "session_id": result.get("session_id", ""),
        "mode": "server" if server_mode else "oneshot",
        "elapsed_s": elapsed,
        "verification": {
            "verified": ok,
            "how": ("agent_tars returned a final answer after acting"
                    if ok else "agent_tars returned no confirmed result"),
            "steps_taken": len(steps),
            "last_actions": [s.get("action", "") for s in steps[-3:]],
            "model": used_model,
        },
    }


def _render(ok: bool, result: dict, model: str, mode: str) -> str:
    if ok:
        lines = [str(result.get("output") or "Done.")]
        steps = result.get("steps") or []
        if steps:
            lines.append("")
            lines.append("What Agent TARS did:")
            for s in steps[-8:]:
                mark = "" if s.get("ok") in (True, None) else "  [failed]"
                lines.append(f"{s.get('step')}. {str(s.get('action') or s.get('tool') or '')[:140]}{mark}")
        lines.append("")
        lines.append("Report only what these actions accomplished; the task is "
                     "not confirmed beyond the final answer Agent TARS returned.")
        return "\n".join(lines)

    code = str(result.get("error_code") or "")
    friendly = {
        "timeout": "Agent TARS did not finish in time and was stopped; nothing is confirmed.",
        "io_failed": "The connection to Agent TARS failed mid-task; nothing is confirmed.",
        "server_unavailable": "Agent TARS could not be started, so the task was not run.",
        "node_missing": "Node.js is not available, so Agent TARS cannot run.",
        "cli_missing": "The Agent TARS CLI is not installed (run 'npm install').",
        "rate_limited": ("Gemini rate-limited Agent TARS before it finished; "
                         "nothing is confirmed. Try again shortly."),
        "no_gemini_key": "No Gemini API key is configured for Agent TARS.",
        "provider_unsupported": ("Agent TARS is configured with a provider other "
                                 "than Gemini, which this project does not use. Set "
                                 "it back to 'gemini'."),
        "bad_output": "Agent TARS returned a result that could not be read.",
    }
    if code in friendly:
        return friendly[code]
    head = str(result.get("error") or result.get("output") or "Agent TARS failed.")
    note = (f"\n(Model {model}; mode {mode}. The engine ran but did not confirm the "
            f"outcome, so it must not be presented as finished.)")
    return head + note


def health_check() -> "Check":
    """Diagnostics Check for the automation engine."""
    from core.diagnostics import Check, ONLINE, DEGRADED, OFFLINE, ERROR
    st = status()
    if not st.get("node") or not st.get("cli"):
        return Check("agent_tars", OFFLINE,
                     "; ".join(st.get("missing") or ["Agent TARS is not installed"]),
                     "Install Node.js (>=22.15) and run 'npm install' in the project.")
    if not st.get("enabled"):
        return Check("agent_tars", OFFLINE, "switched off in Settings",
                     "Enable Agent TARS in Settings.")
    if not st.get("version"):
        return Check("agent_tars", ERROR,
                     st.get("detail") or "Agent TARS CLI did not answer",
                     "Check the pinned @agent-tars/cli install in node_modules.")
    if not st.get("usable"):
        return Check("agent_tars", DEGRADED,
                     f"installed (Agent TARS {st.get('version')}) but no Gemini key — "
                     f"automation is OFF",
                     "Add the Gemini API key in Settings so TARS can run its loop.",
                     {"version": st.get("version", ""), "cli": st.get("cli", "")})
    return Check("agent_tars", ONLINE, st.get("detail", "")[:300], "",
                 {"version": st.get("version", ""), "cli": st.get("cli", ""),
                  "model": st.get("model", ""),
                  "browser_control": st.get("browser_control", "")})


def shutdown(reason: str = "shutdown") -> None:
    """Stop the owned headless server (idempotent)."""
    try:
        from capabilities import tars_server
        tars_server.stop(reason)
    except Exception:
        pass
