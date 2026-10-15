"""The bridge between the Connect gateway and the running desktop.

The gateway runs on its own threads and must never touch a Qt widget from them,
and the UI must never be required for the gateway to be importable (the tests and
the diagnostics both import this package). So the desktop publishes itself here
once — ``attach(ui)`` from main.py — and everything the gateway wants to show
goes through this module, which marshals onto the UI thread or drops the message
when no UI is attached.

A dropped notification is not an error: nothing the phone sends depends on the
desktop being able to draw a line of text, and a Connect command must still work
when the window is closed.
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = ["attach", "attached", "detach", "notify", "set_doing", "clear_doing"]

_UI: Any = None
_LOCK = threading.Lock()


def attach(ui) -> None:
    """Publish the live UI object (called once from main.py)."""
    global _UI
    with _LOCK:
        _UI = ui


def detach() -> None:
    global _UI
    with _LOCK:
        _UI = None


def attached() -> bool:
    with _LOCK:
        return _UI is not None


def _ui():
    with _LOCK:
        return _UI


def notify(text: str) -> bool:
    """Show one line in the desktop's chat log. False when there is no UI."""
    ui = _ui()
    if ui is None or not text:
        return False
    for method_name in ("write_log", "log_safe", "write_log_safe"):
        method = getattr(ui, method_name, None)
        if callable(method):
            try:
                method(str(text)[:400])
                return True
            except Exception:
                continue
    return False


def set_doing(category: str, detail: str) -> bool:
    ui = _ui()
    if ui is None:
        return False
    try:
        ui.set_doing(str(category), str(detail))
        return True
    except Exception:
        return False


def clear_doing(category: str) -> bool:
    ui = _ui()
    if ui is None:
        return False
    try:
        ui.clear_doing(str(category))
        return True
    except Exception:
        return False
