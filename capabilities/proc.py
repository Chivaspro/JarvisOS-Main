"""Out-of-process engine runner.

Two kinds of engine are driven from here:

* Python engines (Agent Reach) live in their own Python 3.11 virtual
  environment while J.A.R.V.I.S. runs on 3.12, so an engine can never be
  imported into this process. Each adapter spawns
  ``<engine venv>/python.exe workers/<engine>_worker.py`` and exchanges exactly
  one JSON document per task over stdin/stdout.

* Agent TARS v0.3.0 is a Node runtime (`@agent-tars/cli`). Its adapter spawns the
  pinned ``node <cli> ...`` (one-shot or the long-lived ``serve`` server) with the
  same ownership and timeout guarantees.

Why this shape:
* the engine's dependency tree cannot corrupt J.A.R.V.I.S.'s own environment;
* a hung or crashed engine dies with the task, not with the assistant;
* every child is registered, so shutdown can terminate *only* J.A.R.V.I.S.-owned
  automation processes (never an unrelated python.exe or node.exe).

Protocol (Python worker -> J.A.R.V.I.S., one final line on stdout):
    {"ok": bool, "summary": str, "data": {...}, "error": str,
     "engine_version": str, "steps": [...]}
Anything the child prints before that line is treated as its log stream and is
captured, redacted and written to J.A.R.V.I.S.'s own log file — never to ChatLog.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

#: Environment variables a child engine must never inherit from J.A.R.V.I.S. A
#: caller that genuinely wants one (Agent TARS needs GEMINI_API_KEY) re-adds it
#: through ``env_extra``, so the secret still travels by environment and never on
#: the command line where a process listing could read it.
_SECRET_ENV = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
               "ANTHROPIC_API_KEY")

_lock = threading.Lock()
#: PIDs of processes this process started and still owns.
_owned: dict[int, str] = {}
#: Interpreter version probe cache, keyed by interpreter path.
_VERSION_CACHE: dict[str, str] = {}
_hook_installed = False


class WorkerError(RuntimeError):
    """A worker could not be started, timed out, or returned an unreadable result."""


# ── ownership tracking ────────────────────────────────────────────────────────

def _register(pid: int, label: str) -> None:
    with _lock:
        _owned[pid] = label


def _unregister(pid: int) -> None:
    with _lock:
        _owned.pop(pid, None)


def owned_pids() -> list[int]:
    with _lock:
        return list(_owned)


def descendants(pid: int) -> list[int]:
    """Child PIDs of *pid* (best effort; [] when psutil is unavailable)."""
    try:
        import psutil
        p = psutil.Process(pid)
        return [c.pid for c in p.children(recursive=True)]
    except Exception:
        return []


def _kill_pid(pid: int) -> int:
    """Terminate *pid* and its child tree. Returns 1 when something was signalled."""
    procs = [pid, *descendants(pid)]
    signalled = 0
    for p in procs:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(p), "/T", "/F"],
                    capture_output=True, timeout=10, **_NO_WINDOW,
                )
            else:
                import signal
                os.killpg(os.getpgid(p), signal.SIGTERM)
            signalled += 1
        except Exception:
            pass
    return 1 if signalled else 0


def terminate_pid(pid: int, reason: str = "") -> bool:
    """Stop one J.A.R.V.I.S.-owned process tree by PID (used by the TARS server).

    Only PIDs this process started are touched; an unknown PID is ignored so a
    caller can never signal an unrelated process.
    """
    with _lock:
        label = _owned.pop(pid, None)
    if label is None:
        return False
    _kill_pid(pid)
    print(f"[Capabilities] stopped owned worker {label} (pid {pid})"
          + (f" — {reason}" if reason else ""))
    return True


def terminate_all(reason: str = "shutdown") -> int:
    """Kill every J.A.R.V.I.S.-owned engine worker and its child tree.

    Called from the shutdown coordinator. Only registered PIDs are touched, so a
    user's own python/node processes are never signalled. Returns how many workers
    were signalled.
    """
    with _lock:
        targets = dict(_owned)
        _owned.clear()
    workers_stopped = 0
    for pid, label in targets.items():
        if _kill_pid(pid):
            workers_stopped += 1
            print(f"[Capabilities] stopped owned worker {label} (pid {pid}, "
                  f"{len([pid, *descendants(pid)])} process(es) in tree) — {reason}")
    return workers_stopped


# ── child environment ─────────────────────────────────────────────────────────

def _child_env(env_extra: dict[str, str] | None,
               python: bool = False) -> dict[str, str]:
    """A clean environment for a child engine, secrets re-added explicitly."""
    child_env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
    if python:
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUNBUFFERED"] = "1"
    child_env.update({k: str(v) for k, v in (env_extra or {}).items()
                      if v not in (None, "")})
    return child_env


# ── the run ───────────────────────────────────────────────────────────────────

def _run_child(argv: list[str], *,
               payload: str | None = None,
               timeout_s: float = 300.0,
               engine: str,
               cwd: str | None = None,
               env_extra: dict[str, str] | None = None,
               label: str = "",
               python: bool = False) -> dict[str, Any]:
    """Start one child, feed it *payload* on stdin, and parse its JSON result."""
    child_env = _child_env(env_extra, python=python)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [str(a) for a in argv],
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None, env=child_env, **_NO_WINDOW,
        )
    except OSError as exc:
        return _fail(engine, "spawn_failed", f"Could not start {engine}: {exc}")

    _register(proc.pid, label or f"{engine}:task")
    try:
        stdout, stderr = proc.communicate(input=payload,
                                          timeout=max(5.0, float(timeout_s)))
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return _fail(engine, "timeout",
                     f"{engine} did not finish within {int(timeout_s)} s and was stopped.")
    except Exception as exc:
        _kill_tree(proc)
        return _fail(engine, "io_failed", f"Communication with {engine} failed: {exc}")
    finally:
        _unregister(proc.pid)

    elapsed = time.monotonic() - started
    result = _parse(stdout or "")
    if result is None:
        # Some engines (Agent TARS `--format json`) pretty-print a multi-line JSON
        # document instead of one line, so keep stdout in the diagnostic log for
        # the adapter to extract rather than discarding it.
        log = (stdout or "")[-4000:]
        if stderr:
            log += "\n--- stderr ---\n" + (stderr or "")[-2000:]
        return _fail(engine, "bad_output",
                     f"{engine} returned no readable result (exit {proc.returncode}).",
                     log=log, elapsed=elapsed)
    result.setdefault("elapsed_s", round(elapsed, 2))
    result.setdefault("engine", engine)
    # A worker exits non-zero when it reports a real failure, which is a
    # legitimate answer, not a crash. Only a failure with no explanation at all
    # needs the exit code to stand in for a message.
    if proc.returncode != 0 and not result.get("ok") and not result.get("error"):
        result["error"] = f"{engine} exited with code {proc.returncode}"
    if stderr:
        result["log"] = (stderr or "")[-2000:]
    return result


def run_worker(engine: str, request: dict[str, Any], *,
               timeout_s: float = 300.0,
               env_extra: dict[str, str] | None = None,
               label: str = "") -> dict[str, Any]:
    """Execute one task in a Python *engine*'s environment and return its result.

    Never raises for an ordinary engine failure: an unavailable interpreter, a
    timeout and a worker crash all come back as ok=False with the reason, which
    is what lets the router report honestly instead of substituting a backend.
    """
    from capabilities import config as _cfg

    interpreter = _cfg.interpreter(engine)
    if not interpreter:
        return _fail(engine, "environment_missing",
                     f"No Python environment found for {engine}.")

    script = _cfg.worker(engine)
    if not script.exists():
        return _fail(engine, "worker_missing", f"Worker script absent: {script}")

    return _run_child(
        [interpreter, str(script)],
        payload=json.dumps(request, ensure_ascii=False),
        timeout_s=timeout_s, engine=engine, cwd=str(script.parent),
        env_extra=env_extra, label=label or f"{engine}:{request.get('action', 'task')}",
        python=True,
    )


def spawn_argv(argv: list[str], *, engine: str = "agent_tars",
               timeout_s: float = 300.0,
               env_extra: dict[str, str] | None = None,
               label: str = "",
               cwd: str | None = None) -> dict[str, Any]:
    """Run one non-Python engine command and return its JSON result.

    Used for the Node-based Agent TARS runtime: the CLI prints one JSON document
    as its last stdout line (the same contract the Python workers use), so the
    parse/ownership/timeout path is shared.
    """
    return _run_child(argv, payload=None, timeout_s=timeout_s, engine=engine,
                      cwd=cwd, env_extra=env_extra, label=label, python=False)


def start_background(argv: list[str], *, engine: str = "agent_tars",
                     env_extra: dict[str, str] | None = None,
                     label: str = "",
                     cwd: str | None = None,
                     log_path: str | Path | None = None) -> subprocess.Popen:
    """Start a long-lived owned engine process (Agent TARS ``serve``).

    The child is registered like a one-shot worker, so the existing shutdown
    coordinator stops it via ``terminate_all``/``terminate_pid`` and never an
    unrelated process.
    """
    child_env = _child_env(env_extra, python=False)
    sink = None
    if log_path:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            sink = open(log_path, "ab")
        except OSError:
            sink = None
    kwargs: dict[str, Any] = dict(env=child_env, cwd=str(cwd) if cwd else None,
                                  **_NO_WINDOW)
    if sink is not None:
        kwargs.update(stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    else:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      stdin=subprocess.DEVNULL)
    proc = subprocess.Popen([str(a) for a in argv], **kwargs)
    if sink is not None:
        # The child holds the descriptor; the parent no longer needs it.
        try:
            sink.close()
        except Exception:
            pass
    _register(proc.pid, label or f"{engine}:server")
    return proc


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate the worker and everything it spawned — no zombie automation."""
    try:
        for pid in descendants(proc.pid):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=5, **_NO_WINDOW)
                else:
                    os.kill(pid, 15)
            except Exception:
                pass
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _parse(stdout: str) -> dict | None:
    """Last JSON object on stdout is the result; earlier lines are worker logs."""
    for line in reversed([ln for ln in (stdout or "").splitlines() if ln.strip()]):
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _fail(engine: str, code: str, message: str, *,
          log: str = "", elapsed: float = 0.0) -> dict[str, Any]:
    return {
        "ok": False,
        "engine": engine,
        "error": message,
        "error_code": code,
        "summary": message,
        "data": {},
        "log": log,
        "elapsed_s": round(elapsed, 2),
    }


def python_version(engine: str) -> str:
    """Interpreter version string for an engine environment ("" when absent)."""
    from capabilities import config as _cfg
    interpreter = _cfg.interpreter(engine)
    if not interpreter:
        return ""
    cached = _VERSION_CACHE.get(interpreter)
    if cached:
        return cached
    out = ""
    try:
        r = subprocess.run([interpreter, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                           capture_output=True, text=True, timeout=25, **_NO_WINDOW)
        if r.returncode == 0:
            out = (r.stdout or "").strip()
    except Exception:
        out = ""
    if out:
        _VERSION_CACHE[interpreter] = out
    return out


def register_shutdown_hook() -> None:
    """Hook terminate_all() into every shutdown path (once).

    Registers with J.A.R.V.I.S.'s stability coordinator AND with ``atexit``. The
    coordinator only runs on the app's own graceful shutdown, so a bare script, a
    crash in the event loop, or a hard exit would otherwise leave the owned Agent
    TARS server alive; atexit closes that gap without ever touching a PID this
    process did not start.
    """
    global _hook_installed
    if _hook_installed:
        return
    _hook_installed = True
    try:
        from core import stability
        stability.register_shutdown(lambda: terminate_all("shutdown"),
                                    name="capabilities-workers")
    except Exception:
        pass
    try:
        import atexit
        atexit.register(lambda: terminate_all("atexit"))
    except Exception:
        pass
