"""Structured logging with correlation fields and secret redaction.

Fields carried by every event (observability spec):
timestamp, level, component, task_id, tool, state, duration, result, error.

Design constraints:
* UTF-8 with ``errors="replace"`` — a locale console (cp1254, cp932, …) can
  never kill the app over a log line (the exact failure that once took down
  the Live session).
* Console output is byte-identical with today's behavior: ``print()`` calls in
  the existing code stay untouched. The JSONL file output is additive, so the
  console experience does not change at all.
* Secret redaction runs at the logging boundary: any dict field whose name
  looks like a credential is replaced with ``[REDACTED]`` before it reaches
  the log file.
* Every call is guarded — the logger must never raise into its caller.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_lock = threading.Lock()
_fh = None
_fh_path: Path | None = None

# Console-visible key events also go to stdout at INFO+ (matches the
# existing [JARVIS]/[Vision] print style). JSONL keeps the structured copy.
_console_echo_levels = ("WARNING", "ERROR", "CRITICAL")


def _log_level() -> int:
    return _LEVELS.get(os.environ.get("JARVIS_LOG_LEVEL", "INFO").upper(), 20)


def _logs_dir() -> Path:
    try:
        from config.settings import logs_dir
        return logs_dir()
    except Exception:
        d = Path(__file__).resolve().parents[1] / "data" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


# ── Log rotation (cleanup report §7: the log dir used to grow unbounded) ─────
# Runs only when the file handler is (re)opened — normally once per process, so
# it costs nothing on the hot path. It deletes dated JSONL files older than the
# retention window and always keeps the newest one, so the live log is never a
# deletion candidate. Fully guarded: a failed prune must never break logging.
def _prune_old_logs(d: Path) -> None:
    try:
        days = int(os.environ.get("JARVIS_LOG_RETENTION_DAYS", "14") or 14)
    except Exception:
        days = 14
    if days <= 0:
        return
    try:
        files = sorted(d.glob("jarvis-*.jsonl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        return
    if len(files) <= 1:
        return
    cutoff = time.time() - days * 86400
    for p in files[1:]:                      # files[0] is the live log
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            continue


def _get_fh():
    global _fh, _fh_path
    if _fh is not None:
        return _fh
    try:
        d = _logs_dir()
        _prune_old_logs(d)
        path = d / f"jarvis-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        _fh = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        _fh_path = path
    except Exception:
        _fh = None
    return _fh


def redact_for_log(value: Any) -> Any:
    """Redact secret-shaped content before it reaches any log sink."""
    try:
        from config.settings import redact_secrets
        return redact_secrets(value)
    except Exception:
        return value


# Values that look like credentials even without a secret-named key around
# them (API-key prefixes seen in the wild). Never logged verbatim.
_SECRET_PATTERN = re.compile(
    r"\b(AIzaSy[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})\b"
)


def _scrub_text(text: str) -> str:
    return _SECRET_PATTERN.sub("[REDACTED]", text)


def log_event(
    level: str = "INFO",
    component: str = "app",
    event: str = "",
    task_id: str = "",
    tool: str = "",
    state: str = "",
    duration_ms: float | None = None,
    result: Any = None,
    error: str = "",
    **extra: Any,
) -> None:
    """Emit one structured event. Never raises.

    Console behavior is unchanged (only WARN+ echoes a line, prefixed
    ``[component]``); the JSONL file always receives the full record.
    """
    try:
        level = str(level).upper()
        if _LEVELS.get(level, 20) < _log_level():
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level,
            "component": component,
            "event": _scrub_text(str(event or "")),
            "task_id": str(task_id or ""),
            "tool": str(tool or ""),
            "state": str(state or ""),
            "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
            "result": redact_for_log(result) if result is not None else None,
            "error": _scrub_text(str(error or "")),
        }
        for k, v in extra.items():
            record[k] = redact_for_log(v)
        line = json.dumps(record, ensure_ascii=True, default=str)
        with _lock:
            fh = _get_fh()
            if fh is not None:
                try:
                    fh.write(line + "\n")
                except Exception:
                    pass
        if level in _console_echo_levels:
            try:
                head = f"{record['ts']} {level} [{component}]"
                detail = record["event"] or record["error"] or ""
                if record["tool"]:
                    detail = f"{record['tool']} — {detail}"
                print(_scrub_text(f"{head} {detail}".rstrip()))
            except Exception:
                pass
    except Exception:
        pass


class Log:
    """Component-scoped logger facade: ``log = Log('computer')``."""

    def __init__(self, component: str) -> None:
        self.component = component

    def event(self, level: str = "INFO", **kw) -> None:
        log_event(level=level, component=self.component, **kw)

    def debug(self, event: str = "", **kw) -> None:
        log_event("DEBUG", self.component, event, **kw)

    def info(self, event: str = "", **kw) -> None:
        log_event("INFO", self.component, event, **kw)

    def warning(self, event: str = "", **kw) -> None:
        log_event("WARNING", self.component, event, **kw)

    def error(self, event: str = "", error: str = "", **kw) -> None:
        log_event("ERROR", self.component, event, error=error or event, **kw)


def get_logger(component: str) -> Log:
    return Log(component)


class _Timer:
    """Context manager recording one latency+outcome record on exit."""

    def __init__(self, component: str, tool: str = "", task_id: str = "",
                 state: str = "") -> None:
        self._component = component
        self._tool = tool
        self._task_id = task_id
        self._state = state
        self._t0 = time.perf_counter()
        self.ok = True
        self.error = ""

    def __enter__(self) -> "_Timer":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.ok = exc_type is None
        if exc_type is not None:
            self.error = f"{exc_type.__name__}: {exc}"
        duration_ms = (time.perf_counter() - self._t0) * 1000.0
        log_event(
            level="INFO" if self.ok else "ERROR",
            component=self._component,
            event="call",
            task_id=self._task_id,
            tool=self._tool,
            state=self._state,
            duration_ms=duration_ms,
            error=self.error,
        )
        try:
            from observability.metrics import record
            record(self._component, duration_ms=duration_ms, ok=self.ok)
        except Exception:
            pass
        return False  # never swallow the exception


def timed(component: str, tool: str = "", task_id: str = "", state: str = "") -> _Timer:
    """``with timed('computer', tool='click'): ...`` — logs + counts one call."""
    return _Timer(component, tool=tool, task_id=task_id, state=state)
