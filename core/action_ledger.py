"""Shared action ledger — one record of important actions, visible to every tool.

Why this exists (Universal Control Protocol, docs/universal-control-protocol.md)
-------------------------------------------------------------------------------
When tools cooperate and fall back to each other, the dangerous failure is not
the second method — it is the second method performing SEND / SUBMIT / UPLOAD /
PURCHASE the first method had already completed. Cooperation must never create
duplicate actions, and "did this already happen?" cannot be answered from a
tool's own memory, because the fallback by definition runs in a different tool.

This module is that shared memory: a small in-process ledger of important
actions with three states —

* pending   — the action was dispatched and its outcome is not known yet
  (a timeout or a lost connection lands here, honestly);
* done      — the action completed and the result carried evidence;
* failed    — the action verifiably did not happen (a refusal, permission
  block or a classified failure), which *unblocks* a genuine retry.

The dispatcher consults the ledger before every important action:

* ``done``   → the call is refused with a "verify, don't redo" answer, so the
  model checks the resulting state instead of clicking Send a second time;
* ``pending`` → the call is refused with a "still unconfirmed" answer; a
  pending action can only be cleared by the state-verifying path below or by
  the task moving on (age window), never by simply trying again;
* ``failed`` or unknown → the attempt is marked pending and proceeds.

Zero heavy imports, thread-safe, never raises into the caller — a broken
ledger must degrade to "no protection", never to a blocked tool.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = [
    "IMPORTANT_ACTIONS", "DUPLICATE_WINDOW_SECONDS", "PENDING_WINDOW_SECONDS",
    "fingerprint", "check_or_begin", "record_outcome", "note_state_verified",
    "recent_entries", "reset_ledger",
]

# Tool actions that change the outside world in a way a retry cannot undo.
# Keys are the real dispatcher action names (tool, action) — kept aligned with
# security/permissions.py and main.py's TOOL_DECLARATIONS.
IMPORTANT_ACTIONS: dict[tuple[str, str], str] = {
    ("file_controller", "delete"):          "DELETE",
    ("dev_agent", "execute"):               "EXECUTE",
    ("code_helper", "run"):                 "RUN",
}

# A matching DONE stays protective for this long.
DUPLICATE_WINDOW_SECONDS = 120.0
# A matching PENDING (outcome unknown) blocks a retry for this long.
PENDING_WINDOW_SECONDS = 45.0

_lock = threading.RLock()
# fingerprint -> {"kind", "task_id", "state", "at", "detail"}
_entries: dict[str, dict[str, Any]] = {}
_MAX_ENTRIES = 200


def fingerprint(tool: str, args: dict | None) -> tuple[str, str] | None:
    """(kind, fp) for an important action, or None when it is not one.

    The fingerprint keys on the *semantic target* (url/file/app/element), not
    on incidental parameters, so a fallback that retries the same Send with a
    different method still collides — which is exactly the point.
    """
    args = args or {}
    action = str(args.get("action", "") or "").strip().lower()
    kind = IMPORTANT_ACTIONS.get((str(tool or ""), action))
    if kind is None:
        return None
    target = ""
    for key in ("url", "file_path", "path", "target", "description",
                "app_name", "title", "text", "command"):
        value = args.get(key)
        if value:
            target = str(value).strip().lower()[:120]
            break
    return kind, f"{kind}|{str(tool).lower()}|{action}|{target}"


def _prune(now: float) -> None:
    """Drop stale/finished entries so the ledger cannot grow without bound."""
    if len(_entries) <= _MAX_ENTRIES:
        return
    for fp in [fp for fp, e in _entries.items()
               if e["state"] == "failed" or now - e["at"] > DUPLICATE_WINDOW_SECONDS]:
        _entries.pop(fp, None)
    while len(_entries) > _MAX_ENTRIES:
        _entries.pop(next(iter(_entries)), None)


def check_or_begin(task_id: str, tool: str, args: dict | None) -> str | None:
    """Gate one important action. Returns a refusal line, or None to proceed.

    None also means "this call has been recorded as pending" — the caller must
    follow up with :func:`record_outcome` when the result is known.
    """
    fp_pair = fingerprint(tool, args)
    if fp_pair is None:
        return None
    kind, fp = fp_pair
    now = time.time()
    with _lock:
        _prune(now)
        entry = _entries.get(fp)
        if entry is not None and entry.get("task_id", task_id) in ("", task_id):
            age = now - entry["at"]
            state = entry.get("state")
            if state == "done" and age <= DUPLICATE_WINDOW_SECONDS:
                return (f"[action-ledger] {kind} already completed "
                        f"{int(age)}s ago ({entry.get('detail') or 'outcome recorded'}). "
                        f"I did not perform it again. Verify the resulting state "
                        f"instead of retrying — use a real task_id when this is "
                        f"genuinely a different {kind.lower()}.")
            if state == "pending" and age <= PENDING_WINDOW_SECONDS:
                return (f"[action-ledger] A {kind} is already in flight "
                        f"({int(age)}s, outcome unconfirmed). I did not trigger a "
                        f"second one. Check whether the first succeeded before "
                        f"attempting anything further.")
            if state == "failed" and age <= DUPLICATE_WINDOW_SECONDS:
                pass  # verified non-action: a retry is legitimate
        _entries[fp] = {"kind": kind, "task_id": str(task_id or ""),
                        "state": "pending", "at": now, "detail": ""}
    return None


def record_outcome(task_id: str, tool: str, args: dict | None,
                   ok: bool, detail: str = "") -> None:
    """Close the pending entry with the dispatcher's real result."""
    fp_pair = fingerprint(tool, args)
    if fp_pair is None:
        return
    _kind, fp = fp_pair
    with _lock:
        entry = _entries.get(fp)
        if entry is None or entry.get("state") != "pending":
            return
        entry["state"] = "done" if ok else "failed"
        entry["at"] = time.time()
        entry["detail"] = str(detail or "")[:160]


def note_state_verified(task_id: str, tool: str, args: dict | None,
                        detail: str = "") -> None:
    """Adopt a ledger entry that the result shows already succeeded.

    Used when a *retry* was legitimately attempted after a 'failed'/'pending'
    state and the evidence proves the original action had, in fact, landed —
    the entry becomes DONE so the chain stops instead of trying a third time.
    """
    fp_pair = fingerprint(tool, args)
    if fp_pair is None:
        return
    _kind, fp = fp_pair
    with _lock:
        _entries[fp] = {"kind": _kind, "task_id": str(task_id or ""),
                        "state": "done", "at": time.time(),
                        "detail": str(detail or "state-verified")[:160]}


def recent_entries(limit: int = 10) -> list[dict]:
    """Most recent ledger rows (for diagnostics/logs)."""
    now = time.time()
    with _lock:
        rows = sorted(_entries.items(), key=lambda kv: kv[1]["at"], reverse=True)
    return [{"fingerprint": fp.split("|", 1)[0], **e,
             "age_s": round(now - e["at"], 1)} for fp, e in rows[:max(0, int(limit))]]


def reset_ledger() -> None:
    """Test/teardown hook."""
    with _lock:
        _entries.clear()
