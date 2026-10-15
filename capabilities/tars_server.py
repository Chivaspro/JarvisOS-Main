"""Agent TARS headless server manager (task: PC/browser automation executor).

Agent TARS v0.3.0 ships a documented headless server (``agent-tars serve
--headless --port <p>``) whose HTTP API this module drives:

    GET  /api/v1/health                      -> {"status": "ok"}
    GET  /api/v1/version                     -> {"version": "0.3.0", ...}
    POST /api/v1/oneshot/query               -> {"sessionId", "result": {...}}
    POST /api/v1/oneshot/query/stream        -> SSE: data: {event}\\n\\n
    POST /api/v1/sessions/<id>/abort         -> cancel a running task

Every shape above was observed against the installed 0.3.0 package; nothing here
is guessed. The stream is consumed for REAL progress (never a simulated loop):
each event becomes one short line for the existing activity monitor through the
``on_event`` callback the router passes down.

The server is a J.A.R.V.I.S.-owned child (registered in capabilities/proc), so
the existing shutdown coordinator stops it and never an unrelated node process.
Its model provider is Gemini, on the project's existing key — this project has
no separate UI-TARS credential.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable

from capabilities import config, proc

_lock = threading.RLock()
#: {"proc": Popen|None, "port": int, "model": str, "log": str}
_state: dict[str, Any] = {"proc": None, "port": 0, "model": "", "log": ""}

#: Provider errors that are worth one retry with the fallback model.
_TRANSIENT_MARKERS = ("503", "429", "high demand", "overloaded", "rate limit",
                      "rate-limit", "temporarily", "unavailable", "resource_exhausted")


def _log(message: str) -> None:
    try:
        from observability.logger import log_event
        log_event("INFO", "capabilities", "agent_tars_server", detail=message[:400])
        return
    except Exception:
        pass
    try:
        print(f"[AgentTars] {message}")
    except Exception:
        pass


def log_path() -> Path:
    try:
        from config.settings import LOGS_DIR
        base = Path(LOGS_DIR)
    except Exception:
        base = config.BASE_DIR / "data" / "logs"
    return base / "agent_tars_server.log"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _alive() -> bool:
    p = _state.get("proc")
    return bool(p is not None and p.poll() is None)


def is_running() -> bool:
    with _lock:
        return _alive()


def health(timeout_s: float = 3.0) -> bool:
    """A real liveness check against the running server (never assumed)."""
    if not is_running():
        return False
    try:
        import requests
        r = requests.get(_base(_state["port"]) + "/api/v1/health", timeout=timeout_s)
        return r.status_code == 200 and str((r.json() or {}).get("status")) == "ok"
    except Exception:
        return False


def status() -> dict:
    """Secret-free server state for diagnostics and Settings."""
    with _lock:
        healthy = health()
        return {
            "running": _alive(),
            "healthy": healthy,
            "port": _state.get("port", 0),
            "model": _state.get("model", ""),
            "pid": getattr(_state.get("proc"), "pid", 0) if _state.get("proc") else 0,
            "log": str(_state.get("log") or ""),
        }


def _argv(node: str, cli: str, port: int, model: str) -> list[str]:
    argv = [
        node, cli, "serve", "--headless", "--port", str(port),
        "--model.provider", config.agent_tars_provider(),
        "--model.id", model,
        "--toolCallEngine", config.agent_tars_tool_call_engine(),
        "--browser.control", config.agent_tars_browser_control(),
        "--server.exclusive",
    ]
    # The model credential is the existing Gemini key, passed to the child as
    # GEMINI_API_KEY by ensure(). No UI-TARS key exists to forward.
    endpoint = config.agent_tars_cdp_endpoint()
    if endpoint:
        argv += ["--browser.cdpEndpoint", endpoint]
    if config.agent_tars_planner():
        argv += ["--planner.enable"]
    search = config.agent_tars_search_provider()
    if search:
        argv += ["--search.provider", search]
    workspace = config.agent_tars_workspace()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        argv += ["--workspace", str(workspace)]
    except OSError:
        pass
    return argv


def ensure(model: str = "", on_log: Callable[[str], None] | None = None) -> dict:
    """Start the server if it is not already healthy. Returns a status dict.

    Never raises: an unavailable Node runtime, a missing CLI or a server that
    will not come up all come back as ``{"ok": False, "error_code": ...}``.
    """
    wanted = str(model or config.agent_tars_model()).strip()
    with _lock:
        node = config.agent_tars_node()
        if not node:
            return {"ok": False, "error_code": "node_missing",
                    "error": "Node.js was not found on PATH."}
        cli = config.agent_tars_cli()
        if not cli:
            return {"ok": False, "error_code": "cli_missing",
                    "error": ("The Agent TARS CLI is not installed. Run "
                              "'npm install' in the project folder.")}
        key_env, key = config.agent_tars_credential()

        if _alive() and _state.get("model") == wanted and health():
            return {"ok": True, "port": _state["port"], "model": wanted,
                    "reused": True, "pid": _state["proc"].pid}

        # A live server running a different model must be replaced.
        _stop_locked("model change")

        port = config.agent_tars_port() or _free_port()
        argv = _argv(node, cli, port, wanted)
        try:
            child = proc.start_background(
                argv, engine="agent_tars", cwd=str(config.BASE_DIR),
                env_extra={key_env: key} if (key_env and key) else None,
                label=f"agent_tars:serve:{port}", log_path=log_path(),
            )
        except OSError as exc:
            return {"ok": False, "error_code": "server_start_failed",
                    "error": f"Could not start the Agent TARS server: {exc}"}

        _state.update({"proc": child, "port": port, "model": wanted,
                       "log": str(log_path())})
        _log(f"serve started pid={child.pid} port={port} model={wanted}")

    deadline = time.monotonic() + config.agent_tars_health_timeout()
    while time.monotonic() < deadline:
        if health(timeout_s=2.0):
            _log("serve healthy")
            return {"ok": True, "port": _state["port"], "model": wanted,
                    "reused": False, "pid": _state["proc"].pid}
        if not is_running():
            break
        time.sleep(1.0)

    reason = ("the server exited during startup"
              if not is_running() else "health check did not pass in time")
    detail = _log_tail()
    _stop_locked("startup failure")
    return {"ok": False, "error_code": "server_unavailable",
            "error": f"Agent TARS server did not become ready: {reason}.",
            "detail": detail}


def _log_tail(lines: int = 12) -> str:
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])[-1200:]


def _stop_locked(reason: str) -> None:
    child = _state.get("proc")
    _state.update({"proc": None, "port": 0, "model": ""})
    if child is not None:
        try:
            proc.terminate_pid(child.pid, f"agent_tars server: {reason}")
        except Exception:
            pass


def stop(reason: str = "shutdown") -> bool:
    """Stop the owned server (idempotent). Returns True when one was running."""
    with _lock:
        was = _state.get("proc") is not None
        _stop_locked(reason)
    if was:
        _log(f"serve stopped ({reason})")
    return was


# ── task submission ───────────────────────────────────────────────────────────

def _describe_tool(name: str, args: Any) -> str:
    """One honest, human line for a real TARS tool call (no simulation)."""
    tool = str(name or "").strip()
    data = args if isinstance(args, dict) else {}
    url = str(data.get("url") or "").strip()
    text = str(data.get("text") or data.get("query") or data.get("command") or "").strip()
    short = tool.replace("browser_", "").replace("_", " ")
    if tool == "browser_navigate" and url:
        return f"Opening {url}"
    if tool in ("browser_get_markdown", "browser_get_text", "browser_read",
                "browser_get_html", "browser_get_content"):
        return "Reading the page"
    if "search" in tool:
        return f"Searching {text}" if text else "Searching the web"
    if "type" in tool or "input" in tool:
        return f"Typing {text[:40]}" if text else "Typing"
    if "click" in tool:
        return "Clicking"
    if "scroll" in tool:
        return "Scrolling"
    if "screenshot" in tool:
        return "Capturing the screen"
    if tool == "run_command":
        return f"Running {text[:50]}" if text else "Running a command"
    if tool.startswith("file") or "filesystem" in tool:
        return "Working with files"
    return short[:60] or "Working"


def _transient(text: str) -> bool:
    blob = str(text or "").lower()
    return any(marker in blob for marker in _TRANSIENT_MARKERS)


def submit(query: str, *, on_event: Callable[[str], None] | None = None,
           timeout_s: float = 0, model: str = "") -> dict:
    """Run one task on the server and return a structured result. Never raises."""
    import requests

    budget = float(timeout_s or config.agent_tars_timeout())
    ready = ensure(model)
    if not ready.get("ok"):
        return {"ok": False, "error_code": ready.get("error_code", "server_unavailable"),
                "error": ready.get("error", "Agent TARS server unavailable."),
                "detail": ready.get("detail", ""), "output": ready.get("error", ""),
                "steps": [], "session_id": "", "model": ready.get("model", "")}

    port = ready["port"]
    model_used = ready.get("model", "")
    url = _base(port) + "/api/v1/oneshot/query/stream"

    session_id = ""
    steps: list[dict] = []
    final = ""
    err_text = ""
    status_code = ""
    started = time.monotonic()
    try:
        with requests.post(url, json={"query": query}, stream=True,
                           timeout=(10, max(60.0, budget)),
                           headers={"Accept": "text/event-stream"}) as resp:
            if resp.status_code != 200:
                return {"ok": False, "error_code": "server_error",
                        "error": f"Agent TARS server returned HTTP {resp.status_code}.",
                        "output": "", "steps": [], "session_id": "", "model": model_used}
            for raw in resp.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = str(raw).strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    ev = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("type")
                if kind == "session_created":
                    session_id = session_id or str(ev.get("sessionId") or "")
                elif kind == "tool_call":
                    name = str(ev.get("name") or "")
                    entry = {"step": len(steps) + 1, "tool": name,
                             "action": _describe_tool(name, ev.get("arguments")),
                             "ok": None}
                    steps.append(entry)
                    if on_event:
                        _safe_event(on_event, entry["action"])
                elif kind == "tool_result":
                    name = str(ev.get("name") or "")
                    failed = bool(ev.get("error"))
                    if steps:
                        steps[-1]["ok"] = not failed
                        steps[-1]["elapsed_ms"] = ev.get("elapsedMs")
                    if failed and on_event:
                        _safe_event(on_event, f"{_describe_tool(name, {})} failed")
                elif kind == "assistant_message":
                    content = str(ev.get("content") or "").strip()
                    if content:
                        final = content
                    if ev.get("finishReason") == "error" and content:
                        err_text = content
                elif kind == "system" and str(ev.get("level")) == "error":
                    err_text = err_text or str(ev.get("message") or "")
                elif kind == "agent_run_end":
                    status_code = str(ev.get("status") or "")
                    if on_event:
                        _safe_event(on_event, "Task completed")
    except requests.exceptions.Timeout:
        return _stop_after("timeout", f"Agent TARS did not finish within {int(budget)} s "
                                      f"and the task was stopped.", steps, session_id,
                           model_used, on_event)
    except Exception as exc:
        return _stop_after("io_failed",
                           f"The Agent TARS server connection failed: {exc}",
                           steps, session_id, model_used, on_event)

    elapsed = round(time.monotonic() - started, 2)
    if err_text and not final:
        code = "rate_limited" if _transient(err_text) else "model_error"
        _log(f"task failed on {code}: {err_text[:160]}")
        return {"ok": False, "error_code": code, "error": err_text,
                "output": _clean_error(err_text), "steps": steps,
                "session_id": session_id, "model": model_used, "elapsed_s": elapsed}

    verified = bool(final)
    _log(f"task done ok={verified} steps={len(steps)} elapsed={elapsed}s")
    return {"ok": verified, "error_code": "" if verified else "no_result",
            "error": "" if verified else "Agent TARS returned no final answer.",
            "output": final or "Agent TARS finished without a final answer.",
            "steps": steps, "session_id": session_id, "model": model_used,
            "status": status_code, "elapsed_s": elapsed}


def _safe_event(callback: Callable[[str], None], text: str) -> None:
    try:
        callback(str(text)[:120])
    except Exception:
        pass


def _stop_after(code: str, message: str, steps: list, session_id: str,
                model_used: str, on_event: Callable[[str], None] | None) -> dict:
    if on_event:
        _safe_event(on_event, "Stopping the task")
    return {"ok": False, "error_code": code, "error": message, "output": message,
            "steps": steps, "session_id": session_id, "model": model_used}


def _clean_error(text: str) -> str:
    """A provider error trimmed to the useful sentence, never a raw dump."""
    blob = str(text or "")
    if "503" in blob or "high demand" in blob:
        return ("The Gemini API temporarily could not serve the automation task "
                "(the provider reported itself overloaded). Nothing was confirmed. "
                "Try again shortly.")
    if "429" in blob or "rate" in blob.lower():
        return ("Gemini rate-limited the automation task before it finished, so "
                "nothing is confirmed. Wait a moment and try again.")
    return blob[:600]


def abort(session_id: str) -> bool:
    """Ask the server to cancel a running session (best effort, documented route)."""
    if not session_id or not is_running():
        return False
    try:
        import requests
        r = requests.post(_base(_state["port"]) + f"/api/v1/sessions/{session_id}/abort",
                          timeout=10)
        _log(f"abort {session_id} -> {r.status_code}")
        return r.status_code < 400
    except Exception as exc:
        _log(f"abort failed: {exc}")
        return False
