"""Optional Langfuse trace export (observability spec).

Complete no-op unless ``features.langfuse_tracing=true`` AND Langfuse keys are
present: no import, no network, no startup cost. When enabled, ``trace_span``
yields a Langfuse span; when anything is missing it yields a null span so
callers need no branching. All failures are silent — tracing must never
affect the session.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any

_client = None
_client_tried = False
_client_lock = threading.Lock()


def _enabled() -> bool:
    try:
        from config.settings import get_feature
        if not bool(get_feature("langfuse_tracing")):
            return False
    except Exception:
        return False
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def _get_client():
    """Lazily build the Langfuse client exactly once, only when enabled."""
    global _client, _client_tried
    if _client is not None or _client_tried:
        return _client
    with _client_lock:
        if _client is not None or _client_tried:
            return _client
        _client_tried = True
        if not _enabled():
            return None
        try:
            from langfuse import Langfuse  # lazy: only imported when enabled
            _client = Langfuse(
                public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
                secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
                host=os.environ.get("LANGFUSE_HOST")
                or "https://cloud.langfuse.com",
            )
        except Exception:
            _client = None
    return _client


@contextmanager
def trace_span(name: str, task_id: str = "", metadata: dict | None = None):
    """Trace one span. No-op (yields a stub) when Langfuse is unconfigured."""
    stub = _NullSpan()
    client = _get_client()
    if client is None:
        yield stub
        return
    try:
        with client.start_as_current_span(name) as span:
            try:
                if task_id:
                    span.update(trace_id=task_id)
                if metadata:
                    span.update(metadata=metadata)
            except Exception:
                pass
            yield span
    except Exception:
        yield stub


class _NullSpan:
    """Safe stub so ``with trace_span(...) as span`` never needs None checks."""

    def update(self, *a, **kw) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def tracing_check() -> "Check":
    """Diagnostics Check describing the tracing subsystem's state."""
    try:
        from core.diagnostics import Check, ONLINE, DISABLED
        from config.settings import get_feature
        flag = bool(get_feature("langfuse_tracing"))
        if not flag:
            return Check("tracing", DISABLED,
                         "Langfuse tracing off (features.langfuse_tracing)", "")
        if not (os.environ.get("LANGFUSE_PUBLIC_KEY")
                and os.environ.get("LANGFUSE_SECRET_KEY")):
            return Check("tracing", DISABLED,
                         "enabled in config but LANGFUSE keys are missing", "")
        if _get_client() is not None:
            return Check("tracing", ONLINE, "Langfuse client ready", "")
        return Check("tracing", DISABLED,
                     "langfuse package unavailable; spans are discarded", "")
    except Exception as exc:
        from core.diagnostics import Check, ERROR
        return Check("tracing", ERROR, f"{type(exc).__name__}: {exc}", "")
