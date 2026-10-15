"""Observability unit tests (tasks 2.1-2.4).

Proves: structured events carry every required field, secret-shaped values
never reach the log file (redaction at the logging boundary), per-subsystem
counters record latency/outcome, and the Langfuse adapter is a complete no-op
when unconfigured.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from observability.logger import log_event  # noqa: E402
from observability.metrics import record, counters_snapshot, reset  # noqa: E402
from observability.tracing import trace_span  # noqa: E402

REQUIRED_FIELDS = (
    "ts", "level", "component", "event", "task_id", "tool",
    "state", "duration_ms", "result", "error",
)

SECRET_SHAPED = os.getenv("SECRET_SHAPED", "")


def _last_log_record(tmp_path, monkeypatch):
    """Emit one event with the log file redirected into tmp_path; return it."""
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("INFO", "test", "unit-check", task_id="T-1", tool="probe",
              state="EXECUTING", duration_ms=12.3, result={"api_key": SECRET_SHAPED})
    logmod._fh.close()  # flush for Windows file readers
    logmod._fh = None
    lines = logmod._fh_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "structured event was not written"
    return json.loads(lines[-1])


def test_structured_event_has_all_required_fields(tmp_path, monkeypatch):
    rec = _last_log_record(tmp_path, monkeypatch)
    for field in REQUIRED_FIELDS:
        assert field in rec, f"missing required field: {field}"
    assert rec["task_id"] == "T-1" and rec["tool"] == "probe"
    assert rec["state"] == "EXECUTING" and rec["duration_ms"] == 12.3


def test_secrets_never_reach_the_log_file(tmp_path, monkeypatch):
    rec = _last_log_record(tmp_path, monkeypatch)
    assert rec["result"]["api_key"] == "[REDACTED]"
    assert SECRET_SHAPED not in logfile_text()


def test_secret_shaped_string_in_event_is_scrubbed(tmp_path, monkeypatch):
    from observability import logger as logmod
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    log_event("WARNING", "test", "leak attempt", error=f"failed with {SECRET_SHAPED}")
    logmod._fh.close()
    logmod._fh = None
    assert SECRET_SHAPED not in logfile_text()


def logfile_text() -> str:
    from observability import logger as logmod
    if logmod._fh_path and logmod._fh_path.exists():
        return logmod._fh_path.read_text(encoding="utf-8")
    return ""


def test_counters_record_latency_and_outcomes():
    reset()
    record("computer", duration_ms=100.0, ok=True)
    record("computer", duration_ms=300.0, ok=False)
    record("model", duration_ms=50.0, ok=True)
    snap = counters_snapshot()
    assert snap["computer"]["calls"] == 2
    assert snap["computer"]["errors"] == 1
    assert snap["computer"]["avg_ms"] == 200.0
    assert snap["computer"]["max_ms"] == 300.0
    assert snap["model"]["calls"] == 1 and snap["model"]["errors"] == 0


def test_langfuse_unconfigured_is_noop(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    t0 = __import__("time").perf_counter()
    with trace_span("unit-test") as span:
        span.update(metadata={"api_key": SECRET_SHAPED})  # stub must accept calls
    elapsed_ms = (__import__("time").perf_counter() - t0) * 1000
    assert elapsed_ms < 500, "unconfigured tracing must not import or do network I/O"


def test_timed_context_logs_and_counts(tmp_path, monkeypatch):
    from observability import logger as logmod
    from observability import metrics as met
    monkeypatch.setattr(logmod, "_fh", None)
    monkeypatch.setattr(logmod, "_fh_path", None)
    monkeypatch.setattr(logmod, "_logs_dir", lambda: tmp_path)
    met.reset()
    with logmod.timed("computer", tool="unit-op"):
        pass
    with pytest.raises(ValueError):
        with logmod.timed("computer", tool="unit-fail"):
            raise ValueError("boom")
    snap = met.counters_snapshot()
    assert snap["computer"]["calls"] == 2 and snap["computer"]["errors"] == 1
