"""Memory integrity tests — the full remember → persist → restart → retrieve
→ forget → restart → verify-forgotten lifecycle from the fix brief (§23).

Every test runs against a throwaway SQLite file + JSON store, so the real
user memory on disk is never touched. The service shares its BrainDatabase
singleton by design, so one env var redirects the whole stack.
"""
from __future__ import annotations

import json

import pytest

from memory import backend as memory_backend
from memory import database as brain_db
from memory import memory_manager as mm
from memory import manager as brain_manager
from memory import service as memory_service


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # Patch the module default, not just the env: an earlier test file may
    # already have resolved DEFAULT_DB_PATH to the real data/brain_memory.db,
    # and a leaked singleton pointed there would touch the user's real memory.
    monkeypatch.setattr(brain_db, "DEFAULT_DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("JARVIS_BRAIN_DB", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    # The ACTIVE backend is a module global too: a stale one from an earlier
    # test would both poison later tests (it decides where a fact is written)
    # and — if the user's config selects the vault — point a test at the real
    # Obsidian vault. Pin it to the original store for every test here.
    monkeypatch.setattr(memory_backend, "selected_backend",
                        lambda: memory_backend.BACKEND_ORIGINAL)
    monkeypatch.setattr(memory_backend, "_mem0_configured", lambda: False)
    memory_backend.reset_memory_backend()
    mm._memory_cache.clear()
    brain_manager._instances.clear()
    memory_service.reset_memory_service()
    svc = memory_service.MemoryService()
    yield svc
    memory_service.reset_memory_service()
    memory_backend.reset_memory_backend()
    brain_manager._instances.clear()
    mm._memory_cache.clear()


def _reopen(svc_cls=memory_service.MemoryService):
    """Simulate an app restart: a brand-new service over the same database."""
    memory_service.reset_memory_service()
    return svc_cls()


# ── §19 command detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "Forget the Dino game.",
    "please forget everything about the Dino game",
    "Delete that memory",                      # immediate shape
    "Forget this.",
    "Don't remember the Dino game anymore",
    "Remove the Dino game from your memory",
])
def test_forget_shapes_detected(line):
    cmd = memory_service.detect_memory_command(line)
    assert cmd is not None and cmd["action"] == "forget", line


@pytest.mark.parametrize("line", [
    "Remember that the Dino game uses Python and Pygame.",
    "remember this",
    "don't forget the meeting",
])
def test_remember_shapes_detected(line):
    cmd = memory_service.detect_memory_command(line)
    assert cmd is not None and cmd["action"] == "remember", line


@pytest.mark.parametrize("line", [
    "What did we work on yesterday?",
    "Where did we leave off?",
    "Continue where we left off",
])
def test_recall_shapes_detected(line):
    cmd = memory_service.detect_memory_command(line)
    assert cmd is not None and cmd["action"] == "recall", line


@pytest.mark.parametrize("line", [
    "Tell me about dinosaurs, the dino extinction and games in general",
    "What's the weather?",
    "Do I need to remember my umbrella?",      # question, not a command
])
def test_ordinary_speech_is_not_a_command(line):
    assert memory_service.detect_memory_command(line) is None


# ── §1/§3 persistence across "restarts" ──────────────────────────────────────

def test_turns_persist_across_restart(store):
    store.persist_turn("user", "Let's make a Dino game in Python")
    store.persist_turn("assistant", "Understood, Sir.")
    again = _reopen()
    texts = [m["content"] for m in again.db.recent_messages(limit=10)]
    assert any("Dino game" in t for t in texts)
    again.shutdown()


def test_explicit_remember_survives_restart(store):
    result = store.on_user_turn("Remember that the Dino game uses Python and Pygame")
    assert result and result["action"] == "remember"
    again = _reopen()
    hits = again.search_conversations("Pygame")
    assert hits and all("Dino" in h["content"] or "Pygame" in h["content"]
                        for h in hits)
    # The fact itself is an active brain memory on the fresh process.
    from memory.manager import get_brain_memory
    got = get_brain_memory().retrieve("Dino game Pygame", limit=5)
    assert any("Pygame" in m["content"] for m in got)
    again.shutdown()


# ── §5–§8 forgetting survives restart and cannot resurrect ──────────────────

def test_forget_executes_in_backend(store):
    store.persist_turn("user", "Let's make a Dino game")
    store.persist_turn("assistant", "We're working on the Dino game, Sir.")
    brain_manager.get_brain_memory().remember(
        "we are building the Dino game project", explicit=True)
    mm.update_memory({"projects": {"dino_game": {"value": "Dino game with pygame"}}})

    result = store.on_user_turn("Forget the Dino game.")
    assert result and result["action"] == "forget"
    assert result["topic"].lower() == "dino game"
    assert result["counts"]["messages"] >= 1
    assert result["counts"]["memories"] >= 1
    assert result["counts"]["json"] >= 1


def test_forgotten_topic_stays_forgotten_after_restart(store):
    store.persist_turn("user", "Let's make a Dino game")
    store.on_user_turn("Forget the Dino game.")

    again = _reopen()
    assert again.is_forgotten("we should continue the Dino game project")
    assert not again.is_forgotten("let's build a weather dashboard")
    # Archive recall offers nothing about the forgotten topic.
    assert again.search_conversations("Dino") == []
    block = again.build_startup_context()
    assert "Dino" not in block
    assert "dino game" in block.lower()      # the hard never-raise guard
    again.shutdown()


def test_forgotten_memory_cannot_resurrect_through_stores(store):
    """§6/§7: even if an OLD row survives somewhere (a legacy JSON value the
    pruner missed, a stale cache), no read path may serve it back."""
    store.on_user_turn("Forget the Dino game.")
    # Force a survivor into the JSON store behind the pruner's back, then
    # check every consumer filters it via the tombstone.
    raw = json.loads(mm.MEMORY_PATH.read_text(encoding="utf-8"))
    raw["projects"]["dino_game"] = {"value": "Dino game secret", "updated": "2026-01-01"}
    mm.MEMORY_PATH.write_text(json.dumps(raw), encoding="utf-8")
    mm._memory_cache.clear()

    assert mm.MEMORY_PATH.exists()
    raw_row = mm.load_memory()["projects"].get("dino_game")
    assert raw_row and "Dino" in str(raw_row)      # the raw row really survives
    assert mm._is_forgotten("Dino game secret")
    assert "Dino" not in mm.format_memory_for_prompt(mm.load_memory())
    # search_memory echoes the query, so assert on the secret value, not the
    # word "dino": the forgotten row must not appear, whatever the header says.
    assert "secret" not in mm.search_memory("dino").lower()
    assert mm.all_entries_for_ui() == []


def test_ambiguous_forget_does_not_nuke_store(store):
    store.persist_turn("user", "The Dino game uses pygame")
    store.persist_turn("assistant", "Noted.")
    brain_manager.get_brain_memory().remember("Dino game uses pygame", explicit=True)
    store.on_user_turn("Forget this.")   # no explicit "this" topic ⇒ nothing destructible
    # The rule: an ambiguous delete never wipes unrelated memory.
    assert any("pygame" in m["content"].lower()
               for m in brain_manager.get_brain_memory()._db.iterate_active())
    assert store.db.recent_messages(limit=5)


# ── §9 previous-conversation recall ──────────────────────────────────────────

def test_recall_returns_real_archive(store):
    store.persist_turn("user", "Let's make a Dino game in Python")
    store.persist_turn("assistant", "Understood, Sir. Python it is.")
    result = store.on_user_turn("What were we working on?")
    assert result and result["action"] == "recall"
    assert any("Dino" in line for line in result["hits"])


def test_no_false_memory_on_empty_archive(store):
    result = store.on_user_turn("What did we decide about Project X?")
    assert result and result["action"] == "recall"
    assert result["hits"] == []
    assert "could not find" in result["instruction"]


# ── §13 project separation / §14 ranking ─────────────────────────────────────

def test_unrelated_projects_do_not_mix(store):
    store.persist_turn("user", "The website needs a dark theme")
    store.persist_turn("user", "The Dino game needs collision detection")
    hits = store.search_conversations("dark theme")
    assert hits and all("website" in h["content"].lower() or "theme" in h["content"].lower()
                        for h in hits)


# ── §7 forgotten guard in the prompt block ───────────────────────────────────

def test_startup_context_quotes_real_history(store):
    store.persist_turn("user", "Adjust the Gemini voice settings")
    store.persist_turn("assistant", "The voice settings are updated.")
    again = _reopen()
    block = again.build_startup_context()
    assert "PERSISTENT MEMORY" in block
    assert "Gemini" in block
    again.shutdown()
