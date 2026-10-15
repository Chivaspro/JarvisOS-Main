"""Closed_Jarvis.py — clear an orphaned J.A.R.V.I.S. background process.

What this fixes
---------------
Closing the Chat Panel used to stop only the *interface*: the Qt loop ended,
the backend thread kept reconnecting, and the pythonw process survived with
no window at all — holding the single-instance mutex and the microphone. The
next launch then reported "J.A.R.V.I.S is already running" with nothing on
screen to close. main.py/core/stability.py now end the process completely on
every close path; this script is the recovery tool for instances that are
ALREADY stuck (orphans from older builds, a crashed interpreter, a hard kill
that skipped cleanup).

Safety rules (why this can never kill a live session)
-----------------------------------------------------
* The process recorded in ``data/runtime/jarvis.lock`` is the *live owner*.
  If it is running, it is an intentional instance and is NEVER touched.
* A candidate is only killed when it is (a) a python/pythonw interpreter,
  (b) provably running this project (its command line references this
  directory), and (c) not the live owner and not this script itself.
* When the owning environment cannot be inspected reliably, the script
  reports instead of acting. It never guesses a kill.

Usage
-----
    python Closed_Jarvis.py             # detect; terminate an orphan if one exists
    python Closed_Jarvis.py --status    # report only, never terminate
    python Closed_Jarvis.py --json      # machine-readable report

Exit codes: 0 = nothing to clear, 10 = an orphan was terminated,
1 = detection unclear (manual step suggested), 2 = usage error.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOCK_PATH = BASE_DIR / "data" / "runtime" / "jarvis.lock"
_NO_WINDOW = {"creation_flags": 0x08000000} if os.name == "nt" else {}


# ── lock / owner state ────────────────────────────────────────────────────────

def read_lock_pid() -> int:
    try:
        return int(LOCK_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
    except Exception:
        return 0


def clear_lock() -> bool:
    """Remove a stale lock file (its owner is already dead)."""
    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
            return True
    except Exception:
        pass
    return False


def pid_alive(pid: int) -> bool:
    """Windows: ask the OS whether the process OBJECT is still active.

    STRICT_ACTIVE_ACCESS_CHECK rather than a wait-handle state, so a dead pid
    kept "alive" by another process's lingering handle cannot fool us — the
    same false signal that produced the phantom 'already running' dialog.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNC | QUERY_LIMITED
        if not handle:
            return False
        try:
            active = ctypes.c_bool(False)
            ok = k32.GetProcessHandleActive(handle, 0x20, ctypes.byref(active))
            return bool(ok and active.value)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return True  # unknown -> assume alive; never kill on a guess


# ── process discovery ─────────────────────────────────────────────────────────

def _interpreter_names() -> set[str]:
    names = {"python.exe", "pythonw.exe"}
    try:
        exe = os.path.basename(sys.executable or "").lower()
        if exe:
            names.add(exe)
    except Exception:
        pass
    return names


def _wmi_candidates() -> list[dict]:
    """psutil-free path: WMI is a declared project dependency on Windows."""
    out: list[dict] = []
    try:
        import wmi  # type: ignore
        from actions.system_monitor import com_apartment
    except Exception:
        return out
    base_lower = str(BASE_DIR).lower()
    # WMI is COM: run it in an apartment so a thread that never initialised one
    # cannot abort the process natively.
    with com_apartment():
        try:
            w = wmi.WMI()
            rows = w.query("SELECT ProcessId, Name, CommandLine FROM Win32_Process")
            for p in rows:
                cmd = (p.CommandLine or "")
                low = cmd.lower()
                if str(p.Name or "").lower() not in _interpreter_names():
                    continue
                # Must be provably this project: the command line references our
                # own directory. A bare "python.exe" from elsewhere is never ours.
                if base_lower not in low:
                    continue
                out.append({"pid": int(p.ProcessId), "name": p.Name, "cmd": cmd[:300]})
            rows = None                              # drop COM refs in the apartment
            w = None
        except Exception:
            return []
    return out


def _psutil_candidates() -> list[dict]:
    out: list[dict] = []
    try:
        import psutil
    except Exception:
        return out
    base_lower = str(BASE_DIR).lower()
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                cmd = proc.info.get("cmdline") or []
                if name not in _interpreter_names():
                    continue
                joined = " ".join(cmd).lower()
                if base_lower not in joined:
                    continue
                out.append({"pid": int(proc.info["pid"]), "name": proc.info["name"],
                            "cmd": joined[:300]})
            except Exception:
                continue
    except Exception:
        return []
    return out


def find_candidates() -> tuple[list[dict], str]:
    """(project interpreter processes, detection method actually used)."""
    cands = _psutil_candidates()
    if cands:
        return cands, "psutil"
    if os.name == "nt":
        cands = _wmi_candidates()
        if cands:
            return cands, "wmi"
        return [], "wmi-empty"
    return cands, "psutil-empty"


# ── termination ───────────────────────────────────────────────────────────────

def terminate(pid: int) -> tuple[bool, str]:
    """Graceful WM_CLOSE first, then a targeted taskkill /F. Never /T: the
    tree may contain a browser or a coding agent the user still wants."""
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            pid_c = ctypes.c_ulong(pid)

            WNDENUMPROC = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _proc(hwnd, _lparam):
                try:
                    got = ctypes.c_ulong(0)
                    if user32.GetWindowThreadProcessId(
                            ctypes.c_void_p(hwnd), ctypes.byref(got)) \
                            and got.value == pid_c.value:
                        user32.PostMessageW(ctypes.c_void_p(hwnd),
                                            0x0010, 0, 0)  # WM_CLOSE
                except Exception:
                    pass
                return True

            user32.EnumWindows(WNDENUMPROC(_proc), 0)
        except Exception:
            pass
        time.sleep(1.2)
        if not pid_alive(pid):
            return True, "closed gracefully (WM_CLOSE)"
        try:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, text=True, timeout=15,
                               **_NO_WINDOW)
            if r.returncode == 0 and not pid_alive(pid):
                return True, "terminated (taskkill /F)"
            return (False, (r.stderr or r.stdout or "taskkill reported failure")[:200])
        except Exception as exc:
            return False, f"taskkill failed: {exc}"
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if not pid_alive(pid):
                return True, "terminated (SIGTERM)"
            time.sleep(0.5)
        os.kill(pid, signal.SIGKILL)
        return (not pid_alive(pid)), "killed (SIGKILL)"
    except Exception as exc:
        return False, f"kill failed: {exc}"


# ── the run ───────────────────────────────────────────────────────────────────

def run(status_only: bool = False, as_json: bool = False) -> int:
    owner = read_lock_pid()
    owner_alive = pid_alive(owner) if owner else False
    candidates, method = find_candidates()
    orphans = [c for c in candidates
               if c["pid"] != owner and c["pid"] != os.getpid()]

    report = {
        "lock_pid": owner,
        "lock_alive": owner_alive,
        "detection": method,
        "jarvis_processes": candidates,
        "orphans": orphans,
        "actions": [],
    }

    if owner_alive:
        # A genuinely running instance is exactly what the user did NOT ask to
        # kill. Report it and change nothing.
        report["verdict"] = "live-instance"
        killed = []
    else:
        if owner:
            if status_only:
                report["verdict"] = "stale-lock"
            else:
                if clear_lock():
                    report["actions"].append(
                        f"cleared stale lock (pid {owner} is not running)")
                report["verdict"] = "lock-cleared"
        else:
            report["verdict"] = "no-lock"
        killed = []
        for cand in orphans:
            if status_only:
                continue
            ok, how = terminate(cand["pid"])
            report["actions"].append(
                f"pid {cand['pid']}: {'terminated' if ok else 'NOT terminated'} — {how}")
            if ok:
                killed.append(cand["pid"])
        if orphans and not status_only:
            report["verdict"] = "orphan-terminated" if killed else "orphan-survived"
        elif orphans and status_only:
            report["verdict"] = "orphan-present"

    if as_json:
        print(json.dumps(report, indent=2))
        return _exit_code(report)

    print("J.A.R.V.I.S. orphan cleaner")
    print(f"  lock file ........ {owner or 'none'} "
          f"({'RUNNING — treated as a live instance' if owner_alive else 'not running'})")
    print(f"  detection ........ {method}")
    for cand in report["jarvis_processes"]:
        tag = "ORPHAN" if cand in report["orphans"] else "live owner"
        print(f"  pid {cand['pid']:<7} [{tag}] {cand['cmd'][:110]}")
    for line in report["actions"]:
        print(f"  → {line}")
    verdict = report["verdict"]
    if verdict == "live-instance":
        print("A live J.A.R.V.I.S. instance is running — nothing was touched.")
    elif verdict == "orphan-terminated":
        print("Orphaned background instance(s) terminated. You can start J.A.R.V.I.S. now.")
    elif verdict == "lock-cleared":
        print("Stale lock cleared. You can start J.A.R.V.I.S. now.")
    elif verdict == "orphan-present":
        print("Orphan candidate(s) present — rerun without --status to terminate them.")
    elif verdict == "orphan-survived":
        print("A process refused to close. End it manually:  "
              f"taskkill /F /PID {report['orphans'][0]['pid'] if report['orphans'] else '?'}")
    else:
        print("No J.A.R.V.I.S. background process found — nothing to clear.")
    return _exit_code(report)


def _exit_code(report: dict) -> int:
    v = report.get("verdict")
    if v == "orphan-terminated":
        return 10
    if v == "orphan-present":
        # something is there to clear but --status refused to clear it
        return 1
    if v == "orphan-survived":
        return 1
    # lock-cleared / no-lock / stale-lock / live-instance: all "nothing left
    # to do" outcomes for the caller.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect and terminate an orphaned J.A.R.V.I.S. background "
                    "process, and clear a stale single-instance lock.")
    parser.add_argument("--status", action="store_true",
                        help="report only — never terminate anything")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args()
    if args.status and args.json:
        print("--status and --json: --json implies a full report; "
              "combining them is allowed but --json wins.", file=sys.stderr)
    return run(status_only=args.status, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
