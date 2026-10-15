"""Coding-agent router (tasks 5.2, 5.4) — the right backend for the job.

Scope routing (master prompt §15):
* focused single-file / precise refactor  → Aider
* larger autonomous / multi-file task     → OpenHands (when available)
* OpenHands unavailable                   → Aider fallback (scope permitting)

Every run: exclusive coding lock on the target tree, risk evaluation through
the security service (coding against JARVIS's own live tree is held until the
user approves), result reporting that names the backend actually used.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from coding.coding_lock import get_coding_lock, CodingLockBusy
from coding import aider_agent, openhands_agent

_SELF_TREE = Path(__file__).resolve().parents[1]

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _feature(name: str, default: Any = None) -> Any:
    try:
        from config.settings import get_feature
        return get_feature(name, default)
    except Exception:
        return default


def backend_availability(refresh: bool = False) -> dict[str, dict]:
    """Both backends' probe results (diagnostics + skill_query use this)."""
    return {
        "aider": aider_agent.status_detail(),
        "openhands": openhands_agent.status_detail(),
    }


def skill_availability_report() -> str:
    """What skill_query may honestly advertise about coding capabilities."""
    av = backend_availability()
    lines = ["CODING AGENTS"]
    lines.append(f"- aider: {'available' if av['aider']['available'] else 'not installed (pip install aider-chat)'}")
    oh = av["openhands"]
    if oh["available"]:
        lines.append("- openhands: available")
    elif not oh.get("docker"):
        lines.append("- openhands: unavailable (Docker Desktop / WSL2 not running)")
    elif not oh.get("image"):
        lines.append(f"- openhands: unavailable (image missing: docker pull {openhands_agent.IMAGE})")
    lines.append("- self-inspection and code_helper route through these backends.")
    return "\n".join(lines)


def route_scope(instruction: str, files: list[str] | None = None,
                scope: str = "") -> str:
    """'aider' | 'openhands' | 'none'. Deterministic, offline."""
    s = (scope or "").strip().lower()
    if s in ("aider", "openhands", "auto", "none"):
        if s in ("aider", "openhands"):
            return s
    text = (instruction or "").lower()
    large_markers = (
        "project", "multi-file", "across files", "refactor the", "whole repo",
        "build me", "scaffold", "migrate", "rewrite", "implement feature",
        "multiple files", "several files",
    )
    focused_markers = ("fix", "rename", "small change", "tweak", "adjust",
                       "one file", "this file", "single file", "bug", "typo")
    n_files = len(files or [])
    if n_files == 1 and any(m in text for m in focused_markers):
        return "aider"
    if n_files >= 3 or any(m in text for m in large_markers):
        return "openhands" if openhands_agent.available() else "aider"
    return "aider"


def execute_coding_task(
    instruction: str,
    files: list[str] | None = None,
    tree: str | os.PathLike = "",
    scope: str = "auto",
    timeout_s: float = 900.0,
) -> dict:
    """Execute one coding task. Never raises; returns
    {ok, backend, output, files, approval|lock evidence on refusal}."""
    tree_path = Path(tree).resolve() if tree else _SELF_TREE
    files = [str(f) for f in (files or [])]

    # ── approval: the assistant's own live tree needs a human yes ────────────
    # 'coding_self_tree' is classified irreversible by the security service, so
    # the UI token is required in every approval mode — including never_confirm.
    if tree_path == _SELF_TREE:
        try:
            from security.approvals import evaluate
            decision = evaluate("coding_self_tree", {"instruction": instruction[:200]})
            if decision.get("needs_ui_token"):
                return {
                    "ok": False, "backend": "none", "files": files,
                    "output": ("[CONFIRMATION_PENDING] Editing JARVIS's own "
                               "installation needs your confirmation on the HUD "
                               "before I touch it."),
                    "approval": "pending_ui_token",
                }
            if not decision.get("allowed"):
                return {"ok": False, "backend": "none", "files": files,
                        "output": decision.get("refusal")
                        or "This coding task needs your approval first.",
                        "approval": "refused"}
        except Exception:
            return {"ok": False, "backend": "none", "files": files,
                    "output": "The approval service is unavailable; coding "
                              "against JARVIS's own tree was not attempted.",
                    "approval": "service_unavailable"}

    if not _feature("coding_agents", True):
        return {"ok": False, "backend": "none", "files": files,
                "output": "Coding agents are switched off in Settings."}

    # ── exclusive lock: never two agents in one tree ─────────────────────────
    lock = get_coding_lock(tree_path)
    try:
        lock.acquire(owner=f"coding:{scope or 'auto'}")
    except CodingLockBusy as exc:
        return {"ok": False, "backend": "none", "files": files,
                "output": str(exc), "lock": "busy"}

    try:
        choice = route_scope(instruction, files, scope)
        if choice == "openhands":
            result = openhands_agent.execute(instruction, tree=tree_path,
                                             timeout_s=timeout_s)
            if result.get("ok"):
                return result
            # OpenHands could not run (or failed): fall through to Aider and
            # let it report its own availability honestly.
            result["output"] = (f"OpenHands unavailable — {result.get('output', '')[:200]} "
                                f"Falling back to Aider.")
            prefix = result["output"]
        else:
            prefix = ""
        aider_result = aider_agent.execute(instruction, files=files, tree=tree_path,
                                           timeout_s=timeout_s)
        if prefix:
            aider_result["output"] = prefix + " " + str(aider_result.get("output", ""))
        return aider_result
    finally:
        lock.release()


def coding_check() -> "Check":
    """Diagnostics Check: coding backends' availability."""
    try:
        from core.diagnostics import Check, ONLINE, DEGRADED
        av = backend_availability()
        if av["aider"]["available"]:
            state, detail = ONLINE, "aider ready"
            if av["openhands"]["available"]:
                detail += ", openhands ready"
            else:
                detail += ", openhands unavailable (optional)"
            return Check("coding_agents", state, detail, "")
        return Check("coding_agents", DEGRADED,
                     "aider not installed; focused edits unavailable "
                     "(pip install aider-chat)", "")
    except Exception as exc:
        from core.diagnostics import Check, ERROR
        return Check("coding_agents", ERROR, f"{type(exc).__name__}: {exc}", "")
