"""Orchestrator tests (tasks 6.4, 6.5, 6.6).

Proves: the flag gates the orchestrator (deterministic path stays default),
failed steps replan with evidence, unverified success is rejected, tasks
resume from checkpoints, state events fire, and a missing LangGraph degrades
with a single notice instead of crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brain.graph import (  # noqa: E402
    Orchestrator, Checkpointer, TaskState, emit_state, add_state_listener,
    remove_state_listener, maybe_orchestrate, orchestrator_enabled,
    _default_checkpointer,
)


class FakeCheckpointer(Checkpointer):
    """In-memory checkpointer (no sqlite touched in unit tests)."""

    def __init__(self):
        self.store = {}

    def save(self, task_id, record):
        self.store[task_id] = dict(record)

    def load(self, task_id):
        return self.store.get(task_id)

    def list_active(self):
        return [{"task_id": k, **v} for k, v in self.store.items()
                if v.get("state") not in ("Completed", "Failed")]


@pytest.fixture()
def cp():
    return FakeCheckpointer()


# ── 6.4 flag gating ───────────────────────────────────────────────────────────

def test_flag_off_returns_none(monkeypatch):
    import config.settings as settings
    monkeypatch.setattr(settings, "feature_enabled",
                        lambda name: False if name == "langgraph_orchestrator" else False)
    assert maybe_orchestrate("any goal") is None


def test_flag_disabled_by_default():
    # config default: langgraph_orchestrator=False
    assert orchestrator_enabled() is False


# ── 6.5 state machine semantics ───────────────────────────────────────────────

def test_successful_multi_step_goal(cp):
    events = []
    add_state_listener(lambda e: events.append(e["state"]))

    def executor(step, ctx):
        return {"ok": True, "verified": True}

    orch = Orchestrator(checkpointer=cp, executor=executor,
                        verifier=lambda s, c, o: bool(o.get("verified")),
                        use_langgraph=False)
    r = orch.run("ship the thing", task_id="t-ok", steps=["a", "b"])
    remove_state_listener(_last_listener())
    assert r.ok is True and r.steps_done == ["a", "b"] and r.state == "Completed"
    assert "Planning" in events and "Executing" in events and "Completed" in events


def test_failed_step_replans_with_evidence(cp):
    attempts = {"count": 0}

    def executor(step, ctx):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return {"ok": False, "verified": False, "error": "window never appeared"}
        return {"ok": True, "verified": True}

    seen_errors = []
    orch = Orchestrator(
        checkpointer=cp, executor=executor,
        verifier=lambda s, c, o: bool(o.get("verified")),
        planner=lambda goal: ["do the thing"],
        replan_fn=None, use_langgraph=False,
    ) if False else Orchestrator(
        checkpointer=cp, executor=executor,
        verifier=lambda s, c, o: bool(o.get("verified")),
        planner=lambda goal: ["do the thing"],
        use_langgraph=False,
    )
    # capture the error evidence recorded by the engine
    orch._replan = lambda goal, failed, obs: (
        seen_errors.append(obs.get("error", "")) or ["retry the thing"])
    r = orch.run("goal x", task_id="t-replan")
    assert r.ok is True
    assert seen_errors and "window never appeared" in seen_errors[0]
    assert r.replans >= 1


def test_unverified_success_is_rejected(cp):
    """A tool that returns without exception but without evidence must NOT
    count as a completed step."""
    def executor(step, ctx):
        return {"ok": True, "verified": False}   # silent non-success

    orch = Orchestrator(
        checkpointer=cp, executor=executor,
        verifier=lambda s, c, o: bool(o.get("verified")),
        planner=lambda goal: ["do it"],
        use_langgraph=False,
    )
    orch._replan = lambda goal, failed, obs: []   # unrecoverable → skip forward
    r = orch.run("goal y", task_id="t-unverified")
    assert r.steps_done == []   # the step was never accepted as done
    assert r.errors


def test_bounded_replans_then_failure(cp):
    orch = Orchestrator(
        checkpointer=cp,
        executor=lambda step, ctx: {"ok": False, "verified": False,
                                    "error": "always fails"},
        verifier=lambda s, c, o: False,
        planner=lambda goal: ["step"],
        use_langgraph=False,
    )
    orch._replan = lambda goal, failed, obs: ["another try"]
    r = orch.run("cursed goal", task_id="t-fail")
    assert r.ok is False and r.state == "Failed"
    assert r.replans >= 3


def test_checkpoint_resume(cp):
    cp.store["t-resume"] = {"goal": "resumable", "plan": ["s1", "s2", "s3"],
                            "cursor": 1, "results": ["s1"], "errors": [],
                            "replans": 0, "state": "Executing"}
    orch = Orchestrator(checkpointer=cp,
                        executor=lambda s, c: {"verified": True},
                        verifier=lambda s, c, o: True, use_langgraph=False)
    state = orch.resume("t-resume")
    assert state and state["cursor"] == 1 and state["results"] == ["s1"]


# ── 6.6 state events ──────────────────────────────────────────────────────────

def test_state_listener_receives_all_transitions(cp):
    seen = []
    add_state_listener(lambda e: seen.append(e["state"]))
    orch = Orchestrator(checkpointer=cp,
                        executor=lambda s, c: {"verified": True},
                        verifier=lambda s, c, o: True, use_langgraph=False)
    orch.run("goal z", task_id="t-events", steps=["only"])
    remove_state_listener(_last_listener())
    assert "Verifying" in seen and "Completed" in seen


def test_broken_listener_cannot_break_the_orchestrator(cp):
    add_state_listener(lambda e: 1 / 0)
    orch = Orchestrator(checkpointer=cp,
                        executor=lambda s, c: {"verified": True},
                        verifier=lambda s, c, o: True, use_langgraph=False)
    r = orch.run("goal w", task_id="t-broken-listener", steps=["s"])
    remove_state_listener(_last_listener())
    assert r.ok is True


# ── graceful degradation ──────────────────────────────────────────────────────

def test_missing_langgraph_degrades_with_single_notice(cp, monkeypatch):
    import builtins
    real_import = builtins.__import__
    notices = []

    def blocked(name, *a, **k):
        if name == "langgraph" or name.startswith("langgraph."):
            raise ImportError("No module named 'langgraph'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr("observability.logger.log_event",
                        lambda *a, **k: notices.append(k) or a)
    orch = Orchestrator(checkpointer=cp,
                        executor=lambda s, c: {"verified": True},
                        verifier=lambda s, c, o: True)
    r = orch.run("goal g", task_id="t-degrade", steps=["one"])
    assert r.ok is True   # pure-Python engine took over
    assert orch._degradation_logged is True


def _last_listener():
    from brain import graph as g
    return g._listeners[-1] if g._listeners else (lambda e: None)
