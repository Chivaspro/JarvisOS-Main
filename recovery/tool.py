"""The ``self_repair`` tool — how the assistant talks about its own injuries.

Actions:

* ``status``   — mode, last captured error, recent patch history
* ``heal``     — repair the last captured error (or a traceback given in
                 ``traceback``); applies or proposes per policy
* ``approve``  — apply the proposal that is waiting for approval
* ``rollback`` — restore a backup (``patch_id`` or ``latest``)
* ``history``  — the patch log
* ``policy``   — read or set the auto-heal mode

All of it is thin: the decisions live in ``recovery.service`` and the writes in
``recovery.patcher``, so the tool can never do anything the UI could not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from recovery import patcher, service

__all__ = ["handler"]


def _policy_line() -> str:
    mode = service.status()["mode"]
    described = {
        "auto_low_risk": ("low-risk capabilities are patched automatically; core code "
                          "waits for your approval"),
        "propose": "I propose every patch and never write one without your approval",
        "auto": "any non-protected file may be patched automatically",
        "off": "detection only — I never patch anything",
    }.get(mode, mode)
    return f"Auto-heal mode '{mode}': {described}."


def handler(args: dict | None = None, player=None) -> str:
    args = dict(args or {})
    action = str(args.get("action") or "status").strip().lower()

    if action in ("status", "report"):
        st = service.status()
        lines = [_policy_line()]
        if st["captured"]:
            lines.append(f"Last captured error: {st['last_error'][:200]}"
                         if st.get("last_error") else "A traceback is captured and waiting.")
        outcome = st.get("last_outcome") or {}
        if outcome.get("message"):
            lines.append(f"Last outcome: {outcome['message']}")
        rows = st.get("history") or []
        if rows:
            lines.append("Recent patches:")
            for row in rows[:5]:
                lines.append(f"- {row.get('patch_id')} [{row.get('status')}] "
                             f"{Path(str(row.get('file'))).name} line {row.get('line')}"
                             f" — {row.get('explanation')}")
        else:
            lines.append("No patches have been applied.")
        return "\n".join(lines)

    if action in ("heal", "fix", "repair"):
        text = str(args.get("traceback") or "").strip()
        if not text:
            text = service.last_error()
        if not text:
            return ("I have not captured a fault to repair, sir. If something just "
                    "failed, tell me what you did and I will look at the log.")
        outcome = service.handle_now(text, source="tool",
                                     request=str(args.get("request") or ""),
                                     force=True)
        return str(outcome.get("message") or "Repair finished with no report.")

    if action in ("approve", "apply"):
        result = service.approve_last()
        return str(result.get("message") or "There was nothing waiting to apply.")

    if action == "rollback":
        result = patcher.rollback(str(args.get("patch_id") or "latest"))
        return str(result.get("message") or "Rollback finished.")

    if action == "history":
        rows = patcher.history(limit=int(args.get("limit") or 10))
        if not rows:
            return "I have no patch history yet, sir."
        return "\n".join(
            f"- {row.get('patch_id')} [{row.get('status')}] "
            f"{Path(str(row.get('file'))).name} line {row.get('line')} "
            f"({row.get('risk')}) — {row.get('explanation')}"
            for row in rows)

    if action == "policy":
        requested = str(args.get("mode") or "").strip().lower()
        if not requested:
            return _policy_line()
        if requested not in ("auto_low_risk", "propose", "auto", "off"):
            return ("Valid modes are auto_low_risk, propose, auto and off, sir.")
        try:
            from core import config_guard
            from config.settings import API_KEYS_FILE
            data: dict[str, Any] = {}
            try:
                data = dict(config_guard.read(API_KEYS_FILE))
            except Exception:
                data = {}
            features = dict(data.get("features") or {})
            features["auto_heal_mode"] = requested
            data["features"] = features
            ok, message = config_guard.guard_write(data, tag="auto-heal-mode")
            if not ok:
                raise RuntimeError(message)
            return f"Auto-heal mode is now '{requested}'."
        except Exception as exc:
            return f"I could not save that mode, sir: {exc}"

    return (f"'{action}' is not a self-repair action, sir. Use one of: status, heal, "
            f"approve, rollback, history, policy.")
