"""Stability guards (Task 2) — pure logic, no GUI, no real crash.

Locks down the crash/lag hardening without actually killing anything:

* install() wires sys/thread excepthooks + enables faulthandler, is idempotent,
  and never raises even with a bad log dir.
* A background-thread exception is LOGGED and the process keeps running (the
  core "don't die silently" guarantee) — asserted without terminating the suite.
* tune_for_responsiveness() raises the recursion limit.
* acquire_single_instance() dedups by name (returns False for a held name on
  Windows; always True elsewhere).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import stability  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_stability():
    # install() is intentionally global+idempotent; reset it so each test gets
    # its own crash log in its own tmp_path (no cross-test ordering coupling).
    # _INSTALLED is the guard flag (public alias: stability.installed).
    stability._INSTALLED = False
    stability.installed = False
    if getattr(stability, "_CRASH_LOG", None) is not None:
        try:
            stability._CRASH_LOG.close()
        except Exception:
            pass
    stability._CRASH_LOG = None
    yield
    stability._INSTALLED = False
    stability.installed = False


def test_install_is_idempotent_and_sets_hooks(tmp_path):
    sys_hook_before = sys.excepthook
    stability.install(log_dir=tmp_path)
    stability.install(log_dir=tmp_path)  # second call must not re-init or raise
    assert sys.excepthook is not sys_hook_before, "sys.excepthook should be installed"
    assert (tmp_path / "jarvis_crash.log").exists()


def test_thread_exception_is_logged_not_fatal(tmp_path, capfd):
    stability.install(log_dir=tmp_path)
    seen = {}

    def boom():
        raise RuntimeError("controlled-boom")

    def after():
        seen["alive"] = True

    t = threading.Thread(target=boom)
    t.start()
    t.join(timeout=2)
    # The main thread must still be running (pytest would have died otherwise).
    threading.Thread(target=after).start()
    time.sleep(0.2)
    assert seen.get("alive") is True
    log = (tmp_path / "jarvis_crash.log").read_text(encoding="utf-8")
    assert "controlled-boom" in log, "thread exception should be captured to the crash log"


def test_tune_for_responsiveness_raises_recursion_limit():
    before = sys.getrecursionlimit()
    stability.tune_for_responsiveness()
    assert sys.getrecursionlimit() >= max(before, 10000)


def test_stabilize_gc_disables_auto_gc_and_honours_opt_out(monkeypatch):
    import gc

    was_enabled = gc.isenabled()
    try:
        monkeypatch.delenv("JARVIS_GC_AUTO", raising=False)
        stability.stabilize_gc()
        assert gc.isenabled() is False, "auto GC must be off by default (3.14 workaround)"
        gc.enable()
        monkeypatch.setenv("JARVIS_GC_AUTO", "1")
        stability.stabilize_gc()
        assert gc.isenabled() is True, "JARVIS_GC_AUTO=1 must leave GC alone"
    finally:
        if was_enabled:
            gc.enable()
        else:
            gc.disable()


def test_single_instance_dedups_on_windows(tmp_path, monkeypatch):
    # Keep the real data/runtime/jarvis.lock out of this: a test run must
    # never write (or clear) the lock a live JARVIS instance depends on.
    monkeypatch.setattr(stability, "LOCK_PATH", tmp_path / "jarvis.lock")
    name = "JSTABILITY_test_" + str(os.getpid())
    first = stability.acquire_single_instance(name)
    second = stability.acquire_single_instance(name)
    if os.name == "nt":
        assert first is True
        assert second is False, "a second acquire of the held name must report not-unique"
    else:
        assert first is True and second is True
