"""Cross-day continuity and the Mind store (beliefs / dreams / open threads).

The example this locks in: yesterday the user said "upgrade the JARVIS project,
but let's do it tomorrow", the app restarted, and today a bare "Continue." must
resume that exact work. Also covers the derived stores the Mind panel shows.

Every test runs against a throwaway SQLite file + JSON store, so the real user
memory on disk is never touched.
"""
from __future__ import annotations

import pytest

from memory import database as brain_db
from memory import manager as brain_manager
from memory import memory_manager as mm
from memory import service as memory_service


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(brain_db, "DEFAULT_DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("JARVIS_BRAIN_DB", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    mm._memory_cache.clear()
    brain_manager._instances.clear()
    memory_service.reset_memory_service()
    svc = memory_service.MemoryService()
    yield svc
    memory_service.reset_memory_service()
    brain_manager._instances.clear()
    mm._memory_cache.clear()


def _reopen():
    """Simulate an app restart: a brand-new service over the same database."""
    memory_service.reset_memory_service()
    return memory_service.MemoryService()


# ── command detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("line", ["Continue.", "continue", "go on", "carry on",
                                  "keep going", "resume", "as we discussed"])
def test_bare_continuations_are_detected(line):
    cmd = memory_service.detect_memory_command(line)
    assert cmd is not None and cmd["action"] == "recall"
    assert cmd.get("continue") is True


@pytest.mark.parametrize("line", [
    "Upgrade the JARVIS project, but let's do it tomorrow.",
    "Let's do it later.",
    "we'll finish it tomorrow",
    "not now, maybe another time",
])
def test_deferrals_are_detected(line):
    assert memory_service.is_deferral(line)


@pytest.mark.parametrize("line", ["What's the weather?", "Play some music",
                                  "Open the browser and search for cats"])
def test_ordinary_speech_is_not_a_deferral(line):
    assert not memory_service.is_deferral(line)


# ── the yesterday -> today example ────────────────────────────────────────────

def test_deferred_work_is_remembered_as_an_open_thread(store):
    store.persist_turn("user", "Upgrade the JARVIS project, but let's do it tomorrow.")
    store.remember_deferred("Upgrade the JARVIS project, but let's do it tomorrow.")
    assert store.open_threads()
    assert any("JARVIS project" in t for t in store.open_threads())


def test_continue_after_restart_resumes_the_open_work(store):
    store.persist_turn("user", "Upgrade the JARVIS project, but let's do it tomorrow.")
    store.remember_deferred("Upgrade the JARVIS project, but let's do it tomorrow.")
    store.restart_conversation()

    # A brand-new process the next day:
    again = _reopen()
    result = again.on_user_turn("Continue.")
    assert result and result["action"] == "recall"
    assert result["continuing"] is True
    assert any("JARVIS project" in t for t in result["open_threads"])
    again.shutdown()


def test_startup_context_carries_a_continuation_block(store):
    store.persist_turn("user", "Upgrade the JARVIS project, but let's do it tomorrow.")
    store.remember_deferred("Upgrade the JARVIS project, but let's do it tomorrow.")
    store.restart_conversation()

    again = _reopen()
    block = again.build_startup_context()
    assert "[CONTINUATION" in block
    assert "JARVIS project" in block
    assert "PERSISTENT MEMORY" in block        # the archive is still quoted
    again.shutdown()


def test_continue_with_nothing_open_says_so(store):
    again = _reopen()
    result = again.on_user_turn("Continue.")
    assert result and result["action"] == "recall"
    assert result["open_threads"] == []
    assert "continue" in result["instruction"].lower()
    again.shutdown()


# ── beliefs ───────────────────────────────────────────────────────────────────

def test_beliefs_are_consolidated_from_repeated_memories(store):
    brain = brain_manager.get_brain_memory()
    brain.remember("the widget uses python and tkinter", explicit=True)
    brain.remember("the widget uses python and pygame", explicit=True)
    made = brain.consolidate_beliefs(min_evidence=2)
    assert made >= 1
    beliefs = brain.beliefs()
    assert beliefs and beliefs[0]["evidence"] >= 2


def test_a_single_memory_never_becomes_a_belief(store):
    brain = brain_manager.get_brain_memory()
    brain.remember("a wholly unique fact about zzzq", explicit=True)
    brain.consolidate_beliefs(min_evidence=2)
    assert brain.beliefs() == []


def test_remember_belief_reinforces_the_existing_one(store):
    brain = brain_manager.get_brain_memory()
    first = brain.remember_belief("the operator prefers short answers")
    second = brain.remember_belief("the operator prefers short answers")
    assert first == second
    rows = brain.beliefs()
    assert len(rows) == 1
    assert rows[0]["evidence"] >= 2


def test_forget_belief_removes_it(store):
    brain = brain_manager.get_brain_memory()
    brain.remember_belief("temporary belief")
    assert brain.forget_belief("temporary belief")
    assert brain.beliefs() == []


# ── dreams ────────────────────────────────────────────────────────────────────

def test_open_tasks_become_dreams(store):
    brain = brain_manager.get_brain_memory()
    tid = brain.start_task("Upgrade the JARVIS project", steps_remaining=["port the UI"])
    brain.generate_dreams()
    dreams = brain.dreams()
    assert dreams and any("JARVIS project" in d["title"] for d in dreams)
    brain.finish_task(tid)


def test_reflect_consolidates_and_dreams(store):
    brain = brain_manager.get_brain_memory()
    brain.remember("the widget uses python and tkinter", explicit=True)
    brain.remember("the widget uses python and pygame", explicit=True)
    brain.start_task("Ship the Mind panel")
    stats = brain.reflect()
    assert stats["beliefs"] >= 1 and stats["dreams"] >= 1


def test_unfinished_work_names_the_open_task(store):
    brain = brain_manager.get_brain_memory()
    tid = brain.start_task("Upgrade the JARVIS project", steps_remaining=["port the UI"])
    work = brain.unfinished_work()
    assert any("JARVIS project" in w for w in work)
    brain.finish_task(tid)


# ── forgetting reaches the Mind store too ─────────────────────────────────────

def test_forget_removes_beliefs_dreams_and_open_threads(store):
    brain = brain_manager.get_brain_memory()
    brain.remember_belief("the dino game uses pygame")
    brain.dream("Pursue: the dino game sequel", kind="aspiration")
    store.remember_deferred("finish the dino game tomorrow")

    store.on_user_turn("Forget the dino game.")

    assert all("dino" not in b["statement"].lower() for b in brain.beliefs())
    assert all("dino" not in d["title"].lower() for d in brain.dreams())
    assert all("dino" not in t.lower() for t in store.open_threads())


def test_continuity_record_is_tombstone_filtered(store):
    store.remember_deferred("finish the dino game tomorrow")
    store.on_user_turn("Forget the dino game.")
    rec = store.continuity_record()
    assert all("dino" not in t.lower() for t in rec["open_threads"])


# ── service reads used by the Mind panel ──────────────────────────────────────

def test_service_beliefs_and_dreams_are_readable(store):
    brain = brain_manager.get_brain_memory()
    brain.remember_belief("the operator prefers short answers")
    brain.dream("Ship the Mind panel", kind="plan")
    assert store.beliefs()
    assert store.dreams()
    assert isinstance(store.reflect(), dict)
