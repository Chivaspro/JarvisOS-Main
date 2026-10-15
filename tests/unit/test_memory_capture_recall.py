"""Persistent memory, end to end: capture from conversation, update in place,
and recall that actually reaches the prompt.

The user's example is the acceptance test:

    "What's my name?"             answered from memory, not from nothing
    "My name is Chivas, but I     stored automatically — original store AND the
     prefer you to call me Sir."  selected backend — as one memory, updated
                                  when it is restated
    "What's my name?"             the prompt carries it, on either backend

Every test runs against throwaway SQLite + JSON stores, so the real user
memory on disk is never touched (same isolation as test_memory_persistence).
"""
from __future__ import annotations

import pytest

from memory import backend as memory_backend
from memory import database as brain_db
from memory import growth
from memory import manager as brain_manager
from memory import memory_manager as mm
from memory import policy
from memory import service as memory_service
from memory.obsidian_backend import ObsidianVaultBackend
from memory.vault_store import VaultIndex


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A throwaway store, pinned to the original backend whatever the config says."""
    monkeypatch.setattr(brain_db, "DEFAULT_DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("JARVIS_BRAIN_DB", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    monkeypatch.setattr(growth, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(memory_backend, "selected_backend",
                        lambda: memory_backend.BACKEND_ORIGINAL)
    monkeypatch.setattr(memory_backend, "_mem0_configured", lambda: False)
    mm._memory_cache.clear()
    brain_manager._instances.clear()
    memory_service.reset_memory_service()
    memory_backend.reset_memory_backend()
    yield tmp_path
    memory_service.reset_memory_service()
    brain_manager._instances.clear()
    mm._memory_cache.clear()
    memory_backend.reset_memory_backend()


def _active(needle: str) -> list[dict]:
    brain = brain_manager.get_brain_memory()
    return [m for m in brain._db.iterate_active()
            if needle in str(m.get("content", "")).lower()]


def _all_rows() -> list[dict]:
    brain = brain_manager.get_brain_memory()
    return list(brain._db._query("SELECT id, content, status FROM memories"))


class _StubBrain:
    """Just the parts of the Brain that observe_utterance touches."""

    def observe(self, text):
        ...

    def note_turn(self, role, text):
        ...

    def route_skills(self, text, features=None, limit=4):
        return []


class _StubVault:
    """A selected backend: proves routing, not the vault itself."""

    name = "obsidian"

    def __init__(self):
        self.writes: list[str] = []

    def write(self, content, **kw):
        self.writes.append(str(content))
        return True

    def search(self, query, limit=8):
        return []


# ── capture ──────────────────────────────────────────────────────────────────

def test_a_stated_fact_is_remembered_without_being_asked(store, monkeypatch):
    from app import brain_bridge

    monkeypatch.setattr(brain_bridge, "get_brain", lambda: _StubBrain())

    brain_bridge.observe_utterance(
        "My name is Chivas, but I prefer you to call me Sir.")

    rows = _active("chivas")
    assert rows, "an ordinary statement about the user must be stored"
    assert rows[0]["category"] == "PROFILE"
    assert growth.snapshot()["writes"] >= 1, "remembering moves the growth meter"


def test_capture_follows_the_selected_backend(store, monkeypatch):
    vault = _StubVault()
    monkeypatch.setattr(memory_backend, "_backend", vault)

    result = policy.promote("I prefer my desk light kept at warm white.")

    assert result["stored"] is True
    assert any("desk light" in w for w in vault.writes)


def test_the_original_store_is_mirrored_while_the_vault_is_selected(store,
                                                                   monkeypatch):
    from app import brain_bridge

    monkeypatch.setattr(memory_backend, "_backend", _StubVault())
    monkeypatch.setattr(brain_bridge, "get_brain", lambda: _StubBrain())

    brain_bridge.observe_utterance("I prefer my desk light kept at warm white.")

    assert _active("desk light"), "switching backends must not lose the fact"


def test_an_explicit_remember_command_is_stored_once(store, monkeypatch):
    from app import brain_bridge

    monkeypatch.setattr(brain_bridge, "get_brain", lambda: _StubBrain())

    brain_bridge.observe_utterance("Remember that my locker code is Umber Lantern.")

    assert not _active("locker"), "the command path owns that write, not capture"


# ── update, not duplicate ────────────────────────────────────────────────────

def test_restating_a_fact_updates_it_instead_of_duplicating(store):
    policy.promote("My name is Chivas.")
    policy.promote("Actually, my name is Chivas Quill.")

    rows = _active("chivas")
    assert len(rows) == 1, "one fact, however often it is restated"
    assert "Quill" in str(rows[0]["content"])
    assert len(_all_rows()) == 2, "the earlier wording stays in history"


def test_a_shorter_restatement_does_not_shrink_the_memory(store):
    policy.promote("My name is Chivas Quill, but you can call me Sir.")
    policy.promote("My name is Chivas Quill.")

    rows = _active("chivas")
    assert len(rows) == 1
    assert "call me Sir" in str(rows[0]["content"]), \
        "a restatement that adds nothing must not delete detail"


def test_two_different_preferences_stay_two_facts(store):
    policy.promote("I prefer tea in the morning.")
    policy.promote("I prefer coffee in the morning.")

    assert len(_active("morning")) == 2, \
        "a new preference is a new fact, not a correction"


# ── recall ───────────────────────────────────────────────────────────────────

def test_whats_my_name_reaches_the_prompt(store, monkeypatch):
    from brain import engine as brain_engine

    class _Context:
        def prompt_block(self):
            return ""

    monkeypatch.setattr(brain_engine, "get_context", lambda: _Context())
    policy.promote("My name is Chivas, but I prefer you to call me Sir.")

    block = brain_engine.Brain().smart_context("What's my name?")

    assert "chivas" in block.lower()
    assert "WHO THE USER IS" in block


def test_the_vault_receives_the_fact_and_answers_with_it(tmp_path):
    vault = tmp_path / "Vault"
    (vault / "Memory").mkdir(parents=True)
    index = VaultIndex(path=vault, db_path=tmp_path / "index.db")
    backend = ObsidianVaultBackend(index=index)

    assert backend.write("My name is Chivas, but I prefer you to call me Sir.",
                         category="PROFILE") is True
    assert list((vault / "Memory").rglob("*.md")), \
        "the fact became a real Markdown note in the vault"

    hits = backend.search("what is my name", limit=5)
    assert hits and "chivas" in str(hits[0]["content"]).lower()
