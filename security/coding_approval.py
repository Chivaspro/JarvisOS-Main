"""Coding approval gate — a chatlog Approve/Decline card before any coding runs.

Unlike security/confirm.py (a HUD banner for irreversible actions like
shutdown), this gate is scoped to coding: every write/edit/build/run/optimize
task from dev_agent or code_helper posts a card into the CHAT tab explaining
what JARVIS is about to code, and — unless "Auto-approve coding" is switched
on in Settings — blocks until the user presses Approve or Decline.

dev_agent/code_helper already run off the asyncio loop (main.py dispatches
them through ``loop.run_in_executor``), so blocking here in ``request()``
costs nothing: it does not freeze the UI, it just makes that one coding call
wait for a human decision, exactly like the master prompt's coding-approval
requirement.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

# Generous relative to security/confirm.py's 90s — a coding decision usually
# needs the user to actually read the explanation, not just glance at a HUD.
TIMEOUT_SECONDS = 600.0


@dataclass
class _Pending:
    id: str
    at: float
    event: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False


_pending: dict[str, _Pending] = {}
_lock = threading.Lock()

# Set once at startup by main.py. show: (request_id, title, detail) -> None,
# posts the Approve/Decline card into the chat log. log: (str) -> None, the
# existing activity/log stream. Both must be safe to call off the UI thread
# (the UI side marshals onto Qt itself, same contract as security/confirm.py).
_show_cb = None
_log_cb = None


def bind(show, log=None) -> None:
    """Wire this module to the chat log UI. Called once from main.py at startup."""
    global _show_cb, _log_cb
    _show_cb, _log_cb = show, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def auto_approve_enabled() -> bool:
    try:
        from config.settings import get_feature
        return bool(get_feature("auto_approve_coding", False))
    except Exception:
        return False


def request_approval(title: str, detail: str) -> bool:
    """Ask the user before coding. Returns True iff it is OK to proceed.

    ``title`` is a short one-line summary ("Build a Flask to-do app");
    ``detail`` is the fuller explanation shown in the chat card. When
    Auto-approve coding is on, this returns True immediately (and says so
    in the log) without ever posting a card.
    """
    if auto_approve_enabled():
        _log(f"SYS: Auto-approved coding — {title}")
        return True

    if _show_cb is None:
        # No chat UI bound (headless run): fail safe by declining rather
        # than coding without anyone able to see or approve it.
        _log(f"SYS: Coding declined — no interface available to approve '{title}'.")
        return False

    req_id = uuid.uuid4().hex[:8]
    pending = _Pending(id=req_id, at=time.monotonic())
    with _lock:
        _pending[req_id] = pending

    try:
        _show_cb(req_id, str(title), str(detail))
    except Exception as exc:
        with _lock:
            _pending.pop(req_id, None)
        _log(f"SYS: Could not show coding approval card — {exc}")
        return False

    _log(f"SYS: Awaiting coding approval — {title}")
    got_answer = pending.event.wait(TIMEOUT_SECONDS)
    with _lock:
        _pending.pop(req_id, None)

    if not got_answer:
        _log(f"SYS: Coding approval timed out — {title}")
        return False

    _log(f"SYS: Coding {'approved' if pending.accepted else 'declined'} — {title}")
    return pending.accepted


def resolve(request_id: str, accepted: bool) -> None:
    """Called by the UI when the user presses Approve/Decline in the chat log."""
    with _lock:
        pending = _pending.get(request_id)
    if pending is None:
        return
    pending.accepted = bool(accepted)
    pending.event.set()


def pending_ids() -> list[str]:
    """Request ids still awaiting an answer (diagnostics / tests)."""
    with _lock:
        return list(_pending.keys())
