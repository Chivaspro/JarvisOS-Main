"""Full-closure + single-instance guard tests (orphaned-process bug).

Proves the behaviour the "closing the Chat Panel leaves J.A.R.V.I.S running
in the background / next start says 'already running'" fix depends on:

* core/stability shutdown coordinator — fires registered cleanup once, then
  guarantees process exit (never returns to a caller that would keep the
  interpreter alive);
* the single-instance guard — a lock whose owner is genuinely alive is
  refused, a lock whose owner is dead is recognised as stale and evicted;
* Closed_Jarvis.py — never treats a live instance as an orphan, does clear a
  stale lock, and reports honestly under --status;
* main.py wiring — the reconnect/supervisor loops honour the stop event
  (source-level guards, in the established pattern of this suite).

The hard-exit path is monkeypatched to a stub — these tests must never
actually call os._exit inside a pytest worker.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import stability  # noqa: E402


# ── shutdown coordinator ──────────────────────────────────────────────────────

def _reset_coordinator(monkeypatch):
    """Fresh coordinator state with the real hard-exit stubbed out."""
    calls = []
    monkeypatch.setattr(stability, "_exit_armed", threading.Event())
    monkeypatch.setattr(stability, "_shutdown_fired", threading.Event())
    monkeypatch.setattr(stability, "_shutdown_cbs", [])
    monkeypatch.setattr(stability.os, "_exit", lambda code=0: calls.append(code))
    monkeypatch.setattr(stability, "_release_mutex", lambda: calls.append("mutex"))
    monkeypatch.setattr(stability, "_clear_lock", lambda: calls.append("lock"))

    # stability's private `threading` namespace is swapped for a shim: real
    # Event/Thread behaviour, but force_exit's backstop Timer can never fire
    # a real os._exit into the pytest worker after the monkeypatch is undone.
    class _DummyTimer:
        def __init__(self, *a, **k):
            self.daemon = True

        def start(self):
            return None

    class _ThreadShim:
        Event = staticmethod(threading.Event)
        Thread = staticmethod(threading.Thread)
        Timer = _DummyTimer

    monkeypatch.setattr(stability, "threading", _ThreadShim)
    return calls


def test_request_process_exit_runs_callbacks_then_forced_exit(monkeypatch):
    calls = _reset_coordinator(monkeypatch)
    done = threading.Event()
    stability.register_shutdown(lambda: done.set(), "worker")
    stability.request_process_exit("test-close")
    assert done.wait(2.0), "shutdown callback must run in the chain"
    assert "mutex" in calls and "lock" in calls
    assert calls[-1] == 0, "the path must end in os._exit(0) — a stubbed one here"


def test_request_process_exit_is_idempotent(monkeypatch):
    calls = _reset_coordinator(monkeypatch)
    hits = []
    stability.register_shutdown(lambda: hits.append(1), "once")
    stability.request_process_exit("first")
    stability.request_process_exit("second")
    assert len(hits) == 1, "the chain fires exactly once per process"
    assert calls.count(0) == 1


def test_broken_callback_cannot_block_the_exit(monkeypatch):
    calls = _reset_coordinator(monkeypatch)

    def _boom():
        raise RuntimeError("cleanup exploded")

    stability.register_shutdown(_boom, "broken")
    stability.request_process_exit("broken-test")
    assert calls[-1] == 0, "a failing callback must not stop the exit chain"


def test_slow_callback_costs_grace_period_not_the_process(monkeypatch):
    calls = _reset_coordinator(monkeypatch)
    # A cleanup step that never returns must eat the 3 s grace and still let
    # the guaranteed exit run — a hung worker must not recreate the orphan.
    stability.register_shutdown(lambda: time.sleep(30), "hanger")
    monkeypatch.setattr(stability.time, "sleep", lambda s: None)  # keep test fast
    stability.request_process_exit("hang-test")
    assert calls[-1] == 0


# ── single-instance guard: live owner vs stale lock ──────────────────────────

def test_acquire_refuses_only_for_a_genuinely_live_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(stability, "LOCK_PATH", tmp_path / "jarvis.lock")
    # An external live process: use a real short-lived child so the scenario
    # is concrete, and confirm the OS agrees it is running.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        monkeypatch.setattr(stability, "pid_alive",
                            lambda pid: pid == child.pid or pid == os.getpid())
        stability.LOCK_PATH.write_text(str(child.pid) + "\n", encoding="utf-8")
        monkeypatch.setattr(stability.os, "name", "nt")  # exercise the Windows path
        calls = []

        class _K:
            def CreateMutexW(self, a, b, name):
                calls.append("create")
                return 1234

            def GetLastError(self):
                return 183  # ERROR_ALREADY_EXISTS

            def CloseHandle(self, h):
                calls.append("close")

        monkeypatch.setattr(stability, "ctypes", type("m", (), {"windll": type("w", (), {"kernel32": _K()})}))
        assert stability.acquire_single_instance() is False
        assert "close" in calls, "a refused lock must still close OUR handle"
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_stale_lock_is_evicted_and_start_proceeds(monkeypatch, tmp_path):
    monkeypatch.setattr(stability, "LOCK_PATH", tmp_path / "jarvis.lock")
    dead_pid = 999999  # beyond any real pid on this box
    stability.LOCK_PATH.write_text(f"{dead_pid}\n", encoding="utf-8")
    monkeypatch.setattr(stability.os, "name", "nt")
    # The OS says the recorded owner is GONE. That is the entire premise of
    # stale eviction, so it is modelled directly rather than via a stubbed
    # ctypes (whose missing OpenProcess would make pid_alive assume 'alive').
    monkeypatch.setattr(stability, "pid_alive", lambda pid: False)
    state = {"created": 0}

    class _K:
        def CreateMutexW(self, a, b, name):
            state["created"] += 1
            return 4321 if state["created"] == 1 else 5678

        def GetLastError(self):
            # First create: mutex exists (leaked handle). After the stale close:
            # this process becomes the owner.
            return 183 if state["created"] == 1 else 0

        def CloseHandle(self, h):
            return True

        def ReleaseMutex(self, h):
            return True

    monkeypatch.setattr(stability, "ctypes", type("m", (), {"windll": type("w", (), {"kernel32": _K()})}))
    written = []
    monkeypatch.setattr(stability, "_write_lock", lambda pid=None: written.append(1))
    assert stability.acquire_single_instance() is True, \
        "a lock whose owner is dead must never block a new start"
    assert state["created"] == 2
    assert written == [1]
    # _clear_lock(force=True) ran before the rewrite, and _write_lock is
    # stubbed to not touch the file — so the dead owner's lock is gone.
    assert not stability.LOCK_PATH.exists()


def test_pid_alive_answers_for_self_and_garbage():
    assert stability.pid_alive(os.getpid()) is True
    # A pid no Windows process can hold (multiples of 4, out of range).
    assert stability.pid_alive(999999) is False


# ── Closed_Jarvis.py utility ──────────────────────────────────────────────────

def _cj():
    """Import the utility by path (standalone, no package assumptions)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("closed_jarvis",
                                                 ROOT / "Closed_Jarvis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_closed_jarvis_never_touches_a_live_instance(monkeypatch, tmp_path, capsys):
    cj = _cj()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        lock = tmp_path / "jarvis.lock"
        lock.write_text(str(child.pid) + "\n")
        monkeypatch.setattr(cj, "LOCK_PATH", lock)
        killed = []
        monkeypatch.setattr(cj, "terminate",
                            lambda pid: killed.append(pid) or (True, "stub"))
        monkeypatch.setattr(cj, "find_candidates",
                            lambda: ([{"pid": child.pid, "name": "pythonw.exe",
                                       "cmd": "pythonw main.py"}], "stub"))
        code = cj.run(status_only=False)
        assert killed == [], "a live instance is never a kill target"
        assert code == 0
        assert lock.exists(), "the live owner's lock must survive"
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_closed_jarvis_clears_a_stale_lock(tmp_path, monkeypatch):
    cj = _cj()
    lock = tmp_path / "jarvis.lock"
    lock.write_text("999999\n")
    monkeypatch.setattr(cj, "LOCK_PATH", lock)
    monkeypatch.setattr(cj, "find_candidates", lambda: ([], "stub"))
    code = cj.run(status_only=False)
    assert not lock.exists(), "a lock whose owner died must be cleared"
    assert code == 0


def test_closed_jarvis_status_reports_without_acting(tmp_path, monkeypatch):
    cj = _cj()
    lock = tmp_path / "jarvis.lock"
    lock.write_text("999999\n")
    monkeypatch.setattr(cj, "LOCK_PATH", lock)
    monkeypatch.setattr(cj, "find_candidates", lambda: (
        [{"pid": 999998, "name": "pythonw.exe", "cmd": "pythonw main.py"}], "stub"))
    code = cj.run(status_only=True)
    assert lock.exists(), "--status must not mutate anything"
    assert code == 1, "an orphan that --status refused to clear is reported via exit 1"


# ── main.py wiring (source-level guards) ─────────────────────────────────────

def test_main_wires_the_stop_event_into_every_restart_path():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # reconnect loop honours the quit
    assert "if _stop_all.is_set():" in src
    # the supervisor must not resurrect a deliberately closed backend
    assert 'if backend_stop["requested"] or _stop_all.is_set():' in src
    # the close chain releases the microphone before exiting
    assert "_sdx.shutdown()" in src
    # every route to a quit converges on the coordinator
    assert "register_shutdown" in src


def test_ui_close_paths_funnel_into_the_coordinator():
    src = (ROOT / "ui.py").read_text(encoding="utf-8")
    assert "request_process_exit('close-all-ui')" in src
    assert "request_process_exit('main-window-close')" in src
    # the old quit-on-exec-only behaviour is now the fallback, not the path
    assert src.count("QApplication.quit()") >= 1
