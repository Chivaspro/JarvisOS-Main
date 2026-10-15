"""Process manager — inspect, freeze, resume, prioritise and (with approval) end
processes on this PC.

One rule shapes the whole module: **J.A.R.V.I.S. is not root.** Killing the wrong
process on Windows can blue-screen the machine or end the session the user is
sitting in, so:

* a small deny-list (System, Registry, csrss, wininit, services, lsass, and this
  process) can never be touched, whatever the model asks for;
* ``kill_process`` is declared with ``gate="confirm"`` in
  ``capabilities/registry.py``, so it always waits for the on-screen
  confirmation — freezing (reversible) does not;
* when the *action* is refused, the sentence says exactly why.

``psutil`` is required; the suspend/resume pair additionally needs the Windows
NtSuspendProcess/NtResumeProcess calls, exposed through ctypes (no extra
dependency). On a non-Windows host suspend/resume reports that honestly instead
of pretending.
"""

from __future__ import annotations

import ctypes
import os
from typing import Any

__all__ = ["run", "status"]

#: Never killable, never suspendable. Matched case-insensitively against the
#: image name (with and without the .exe suffix).
PROTECTED_NAMES: frozenset[str] = frozenset({
    "system", "system idle process", "registry", "memory compression",
    "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso",
    "svchost", "dwm", "explorer", "fontdrvhost", "audiodg",
})

_PRIORITY_MAP: dict[str, str] = {
    "idle": "IDLE_PRIORITY_CLASS",
    "low": "BELOW_NORMAL_PRIORITY_CLASS",
    "below_normal": "BELOW_NORMAL_PRIORITY_CLASS",
    "normal": "NORMAL_PRIORITY_CLASS",
    "above_normal": "ABOVE_NORMAL_PRIORITY_CLASS",
    "high": "HIGH_PRIORITY_CLASS",
    "realtime": "REALTIME_PRIORITY_CLASS",
}

_NT_PRIORITY: dict[str, int] = {
    "high": 13, "above_normal": 10, "normal": 8, "below_normal": 6,
    "low": 4, "idle": 1,
}


def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "psutil", "detail": detail, "evidence": evidence}


def _fail(detail: str, reason: str = "action_failed", **evidence: Any) -> dict:
    return {"ok": False, "controller": "psutil", "detail": detail,
            "reason": reason, "evidence": evidence}


def _psutil():
    import psutil  # type: ignore
    return psutil


def status() -> dict:
    try:
        _psutil()
    except Exception as exc:
        return {"available": False, "detail": f"psutil is not installed ({exc})"}
    return {"available": True, "detail": "psutil ready"}


def _is_protected(process) -> bool:
    try:
        name = (process.name() or "").lower()
    except Exception:
        return True
    bare = name[:-4] if name.endswith(".exe") else name
    if bare in PROTECTED_NAMES or name in PROTECTED_NAMES:
        return True
    try:
        return int(process.pid) in (0, 4, os.getpid())
    except Exception:
        return True


def _resolve_target(args: dict):
    """Find one process by pid, otherwise by name (the largest match wins)."""
    psutil = _psutil()
    pid = args.get("pid")
    if pid not in (None, "", 0, "0"):
        try:
            proc = psutil.Process(int(pid))
            return proc, ""
        except Exception as exc:
            return None, f"no process with pid {pid} ({exc})"
    name = str(args.get("name") or "").strip().lower()
    if not name:
        return None, "a pid or a process name is required"
    matches = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            if name in pname:
                matches.append(proc)
        except Exception:
            continue
    if not matches:
        return None, f"no running process matches '{name}'"
    def _rss(proc):
        try:
            info = proc.info.get("memory_info")
            return int(getattr(info, "rss", 0) or 0)
        except Exception:
            return 0
    matches.sort(key=_rss, reverse=True)
    return matches[0], ""


# ── handlers ──────────────────────────────────────────────────────────────────

def _h_list_processes(args: dict) -> dict:
    psutil = _psutil()
    needle = str(args.get("name") or "").strip().lower()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            info = proc.info
            name = str(info.get("name") or "")
            if needle and needle not in name.lower():
                continue
            rss = int(getattr(info.get("memory_info"), "rss", 0) or 0)
            rows.append((rss, info.get("pid"), name))
        except Exception:
            continue
    rows.sort(reverse=True)
    listing = "; ".join(f"{name} (pid {pid}, {rss // (1024 * 1024)} MB)"
                        for rss, pid, name in rows[:limit])
    if not listing:
        return _fail(f"no running process matched '{needle}'" if needle
                     else "the process list came back empty.", "not_found")
    return _ok(f"{len(rows)} process(es); heaviest first: {listing}",
               count=len(rows))


def _h_kill_process(args: dict) -> dict:
    proc, error = _resolve_target(args)
    if proc is None:
        return _fail(error, "not_found")
    if _is_protected(proc):
        return _fail(f"I will not end {proc.pid} — it is a protected system process.",
                     "protected_process")
    try:
        name = proc.name()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    except Exception as exc:
        return _fail(f"Could not end {getattr(proc, 'pid', '?')}: {exc}")
    return _ok(f"Ended {name} (pid {proc.pid}).", pid=proc.pid)


def _nt_call(proc, which: str) -> bool:
    """NtSuspendProcess / NtResumeProcess through ntdll."""
    if os.name != "nt":
        return False
    try:
        PROCESS_SUSPEND_RESUME = 0x0800
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False,
                                                    int(proc.pid))
        if not handle:
            return False
        try:
            func = getattr(ctypes.windll.ntdll, which)
            return int(func(handle)) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _h_suspend_process(args: dict) -> dict:
    proc, error = _resolve_target(args)
    if proc is None:
        return _fail(error, "not_found")
    if _is_protected(proc):
        return _fail(f"I will not freeze {proc.pid} — it is a protected process.",
                     "protected_process")
    if not _nt_call(proc, "NtSuspendProcess"):
        return _fail("Freezing a process needs Windows and the NtSuspendProcess "
                     "call, which this system refused.", "unsupported")
    return _ok(f"Froze {proc.info.get('name') if hasattr(proc, 'info') else proc.pid} "
               f"(pid {proc.pid}). It can be resumed.", pid=proc.pid)


def _h_resume_process(args: dict) -> dict:
    proc, error = _resolve_target(args)
    if proc is None:
        return _fail(error, "not_found")
    if not _nt_call(proc, "NtResumeProcess"):
        return _fail("Resuming a process needs Windows and the NtResumeProcess "
                     "call, which this system refused.", "unsupported")
    return _ok(f"Resumed pid {proc.pid}.", pid=proc.pid)


def _h_set_process_priority(args: dict) -> dict:
    proc, error = _resolve_target(args)
    if proc is None:
        return _fail(error, "not_found")
    level = str(args.get("level") or "normal").strip().lower().replace(" ", "_")
    if level not in _PRIORITY_MAP:
        return _fail(f"'{level}' is not a priority level I know. Use one of: "
                     + ", ".join(_PRIORITY_MAP) + ".")
    if _is_protected(proc):
        return _fail(f"I will not change {proc.pid}'s priority — it is protected.",
                     "protected_process")
    try:
        if os.name == "nt" and hasattr(proc, "nice"):
            handle = ctypes.windll.kernel32.OpenProcess(0x0200, False, int(proc.pid))
            if handle:
                try:
                    klass = getattr(ctypes.windll.kernel32, _PRIORITY_MAP[level])
                    ctypes.windll.kernel32.SetPriorityClass(handle, klass)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        else:
            proc.nice(_NT_PRIORITY[level] - 8)
    except Exception as exc:
        return _fail(f"Could not set the priority: {exc}")
    return _ok(f"Set pid {proc.pid} to {level} priority.", pid=proc.pid, level=level)


_HANDLERS = {
    "list_processes": _h_list_processes,
    "kill_process": _h_kill_process,
    "suspend_process": _h_suspend_process,
    "resume_process": _h_resume_process,
    "set_process_priority": _h_set_process_priority,
}


def run(action_name: str, args: dict) -> dict:
    """Execute one process action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not a process action.")
    try:
        return handler(dict(args or {})) or _fail("No result.")
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")


__all__ = ["PROTECTED_NAMES", "run", "status"]
