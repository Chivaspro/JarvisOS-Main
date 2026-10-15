"""Coding-agent unit tests (tasks 5.1-5.4).

Proves: the cross-process lock refuses concurrent acquisition, scope routing
is deterministic with a working fallback chain, OpenHands absence yields a
clear setup message (never a crash), skill_query honesty, and self-tree
coding is held until the user approves it.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coding.coding_lock import CodingLock, CodingLockBusy, get_coding_lock  # noqa: E402
from coding import agent_router  # noqa: E402


# ── 5.1 exclusive lock ────────────────────────────────────────────────────────

def test_lock_refuses_concurrent_acquisition(tmp_path):
    lock = CodingLock(tmp_path)
    lock.acquire(owner="first")
    try:
        with pytest.raises(CodingLockBusy):
            CodingLock(tmp_path).acquire(owner="second", timeout_s=0.2)
    finally:
        lock.release()


def test_lock_release_allows_reacquire(tmp_path):
    lock = CodingLock(tmp_path)
    lock.acquire(owner="first")
    lock.release()
    CodingLock(tmp_path).acquire(owner="second")
    # released on context exit by the second owner in real use; clean up here
    CodingLock(tmp_path).release()


def test_lock_refuses_same_thread_reentry(tmp_path):
    lock = CodingLock(tmp_path)
    lock.acquire(owner="first")
    with pytest.raises(CodingLockBusy):
        lock.acquire(owner="first")


def test_lock_is_cross_process_via_file(tmp_path):
    lock = CodingLock(tmp_path)
    lock.acquire(owner="first")
    assert (tmp_path / ".jarvis-coding.lock").exists()
    lock.release()
    assert not (tmp_path / ".jarvis-coding.lock").exists()


def test_get_coding_lock_is_singleton_per_tree(tmp_path):
    a = get_coding_lock(tmp_path)
    b = get_coding_lock(str(tmp_path))
    assert a is b


# ── 5.2/5.3 routing + fallback ────────────────────────────────────────────────

def test_route_scope_focused_to_aider():
    assert agent_router.route_scope("fix the typo in this file", ["a.py"]) == "aider"


def test_route_scope_large_prefers_openhands_when_available(monkeypatch):
    monkeypatch.setattr(agent_router.openhands_agent, "available", lambda: True)
    assert agent_router.route_scope(
        "refactor the whole repo across multiple files", ["a", "b", "c"]
    ) == "openhands"


def test_route_scope_large_falls_back_to_aider(monkeypatch):
    monkeypatch.setattr(agent_router.openhands_agent, "available", lambda: False)
    assert agent_router.route_scope(
        "build me a project scaffold", []
    ) == "aider"


def test_execute_falls_back_when_openhands_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_router.openhands_agent, "available", lambda: True)
    monkeypatch.setattr(agent_router.openhands_agent, "execute",
                        lambda *a, **k: {"ok": False, "backend": "openhands",
                                         "output": "container died"})
    captured = {}
    monkeypatch.setattr(agent_router.aider_agent, "execute",
                        lambda instruction, files, tree, timeout_s:
                        captured.update(backend="aider") or
                        {"ok": True, "backend": "aider", "output": "done",
                         "files": files})
    r = agent_router.execute_coding_task("multi-file migration", files=["a", "b", "c"],
                                         tree=tmp_path)
    assert r["backend"] == "aider" and r["ok"] is True
    assert "OpenHands unavailable" in r["output"]


def test_openhands_absence_yields_setup_message(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_router.openhands_agent, "available", lambda: False)
    monkeypatch.setattr(agent_router.aider_agent, "available", lambda: False)
    r = agent_router.execute_coding_task("refactor everything", tree=tmp_path)
    assert r["ok"] is False
    assert r["backend"] == "aider"  # aider attempted, reports its absence
    assert "not installed" in r["output"]


# ── 5.3 skill_query honesty ───────────────────────────────────────────────────

def test_skill_report_never_advertises_unavailable_backends(monkeypatch):
    monkeypatch.setattr(agent_router.openhands_agent, "available", lambda: False)
    monkeypatch.setattr(agent_router.openhands_agent, "_docker_present",
                        lambda: False)
    report = agent_router.skill_availability_report()
    assert "openhands: unavailable" in report
    assert "Docker" in report or "image missing" in report


# ── 5.4 self-tree approval ────────────────────────────────────────────────────

def test_self_tree_coding_is_held_for_approval():
    import security.approvals as ap
    ap.set_approval_mode("high_risk_only")
    r = agent_router.execute_coding_task(
        "fix the HUD button", tree=None)  # None → JARVIS's own tree
    assert r["ok"] is False
    assert r.get("approval") == "pending_ui_token"
    assert "CONFIRMATION_PENDING" in r["output"]


def test_self_tree_coding_respects_never_confirm_still(monkeypatch):
    # Even in never_confirm, the router holds self-tree edits for the UI token
    # (coding-agents spec: never bypassed).
    import security.approvals as ap
    ap.set_approval_mode("never_confirm")
    r = agent_router.execute_coding_task("tweak this file", tree=None)
    assert r.get("approval") == "pending_ui_token"


def test_non_self_tree_coding_does_not_need_ui_token(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_router.aider_agent, "available", lambda: False)
    r = agent_router.execute_coding_task("fix this bug", files=[], tree=tmp_path)
    assert "approval" not in r or r.get("approval") != "pending_ui_token"


# ── diagnostics ───────────────────────────────────────────────────────────────

def test_coding_diagnostics_check():
    from core.diagnostics import check
    c = check("coding_agents")
    assert c.name == "coding_agents" and c.state in ("ONLINE", "DEGRADED", "ERROR")
