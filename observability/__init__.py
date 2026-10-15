"""Mark LII observability (change: modernize-jarvis-architecture).

Structured logging with correlation fields, per-subsystem latency/outcome
metrics, and an optional Langfuse trace adapter — all fail-safe: observability
must never be the thing that breaks the assistant.
"""

from observability.logger import (
    get_logger,
    log_event,
    Log,
    redact_for_log,
)
from observability.metrics import (
    record,
    counters_snapshot,
    metrics_check,
)
from observability.tracing import trace_span, tracing_check

__all__ = [
    "get_logger", "log_event", "Log", "redact_for_log",
    "record", "counters_snapshot", "metrics_check",
    "trace_span", "tracing_check",
]
