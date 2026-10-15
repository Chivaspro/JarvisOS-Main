"""Per-subsystem latency/outcome counters (observability spec).

Records call counts, error counts and duration totals per subsystem
(model, orchestration, memory, computer, coding, vision, voice, tools).
Thread-safe, bounded, in-memory; surfaced through diagnostics so slow or
failing subsystems are measurable, not anecdotal. Never raises.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_start = time.time()
_counters: dict[str, dict[str, float]] = {}

_SUBSYSTEMS = (
    "model", "orchestration", "memory", "computer",
    "coding", "vision", "voice", "tools", "knowledge",
)


def _bucket(subsystem: str) -> dict[str, float]:
    b = _counters.get(subsystem)
    if b is None:
        b = {"calls": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0}
        _counters[subsystem] = b
    return b


def record(subsystem: str, duration_ms: float = 0.0, ok: bool = True) -> None:
    """Record one call outcome. Unknown subsystems are accepted (forward
    compatibility) and still counted."""
    try:
        with _lock:
            b = _bucket(str(subsystem or "other").strip().lower() or "other")
            b["calls"] += 1
            if not ok:
                b["errors"] += 1
            ms = max(0.0, float(duration_ms or 0.0))
            b["total_ms"] += ms
            b["max_ms"] = max(b["max_ms"], ms)
    except Exception:
        pass


def counters_snapshot() -> dict[str, dict[str, float]]:
    """Copy of all counters with derived averages."""
    with _lock:
        out: dict[str, dict[str, float]] = {}
        for name, b in _counters.items():
            calls = int(b["calls"])
            avg = (b["total_ms"] / calls) if calls else 0.0
            out[name] = {
                "calls": calls,
                "errors": int(b["errors"]),
                "avg_ms": round(avg, 1),
                "max_ms": round(b["max_ms"], 1),
            }
    return out


def reset() -> None:
    """Clear counters (used by tests; harmless at runtime)."""
    with _lock:
        _counters.clear()
        globals()["_start"] = time.time()


def _fmt_line(name: str, b: dict[str, float]) -> str:
    err = f", {int(b['errors'])} errors" if b["errors"] else ""
    return f"- {name}: {int(b['calls'])} calls, avg {b['avg_ms']:.0f} ms, max {b['max_ms']:.0f} ms{err}"


def metrics_report() -> str:
    """Human-readable metrics summary (diagnostics mode=target=metrics)."""
    snap = counters_snapshot()
    if not snap:
        return "METRICS\n- no calls recorded yet this session"
    lines = ["METRICS  //  per-subsystem latency and outcomes since start"]
    for name in sorted(snap):
        lines.append(_fmt_line(name, snap[name]))
    return "\n".join(lines)


def metrics_check() -> "Check":
    """Diagnostics Check: reports the metrics subsystem itself. Import of
    Check is deferred to avoid a circular import with core.diagnostics."""
    try:
        from core.diagnostics import Check, ONLINE, DEGRADED
        snap = counters_snapshot()
        total = sum(b["calls"] for b in snap.values())
        errors = sum(b["errors"] for b in snap.values())
        err_rate = (errors / total) if total else 0.0
        if total == 0:
            return Check("metrics", ONLINE, "no calls recorded yet", "")
        if err_rate > 0.5:
            return Check("metrics", DEGRADED,
                         f"{errors}/{total} calls errored across subsystems",
                         "Inspect the structured log for the failing subsystem.")
        return Check("metrics", ONLINE,
                     f"{total} calls tracked, {errors} errors", "")
    except Exception as exc:
        from core.diagnostics import Check, ERROR
        return Check("metrics", ERROR, f"{type(exc).__name__}: {exc}", "")
