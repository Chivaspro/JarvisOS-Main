"""The active memory backend really receives the facts.

Each test locks one of the failures the user hit:

* a write through the gateway lands in the *selected* backend, so the Obsidian
  vault stops being ignored while the local store quietly takes everything;
* growth is counted on the original store and the vault alike, so "he gets
  stronger every time he remembers" is real on both;
* a switch that cannot be written to the config file says so, instead of
  looking successful and silently reverting on the next start.
"""
from __future__ import annotations

import pytest

from memory import backend as memory_backend
from memory import growth
from memory.obsidian_backend import ObsidianVaultBackend
from memory.vault_store import VaultIndex


class _FakeBrain:
    """The minimal BrainMemory surface LocalMemoryBackend touches."""

    def __init__(self) -> None:
        self.rows: list[str] = []

    def remember(self, content, category=None, project_id="", explicit=False):
        self.rows.append(content)
        return len(self.rows)

    def retrieve(self, query, limit=8):
        return [{"content": row} for row in self.rows][:limit]

    def is_enabled(self):
        return True


@pytest.fixture()
def growth_file(tmp_path, monkeypatch):
    """Growth goes to a throwaway file — never the real data folder."""
    path = tmp_path / "growth.json"
    monkeypatch.setattr(growth, "growth_path", lambda: path)
    growth.reset()
    yield path
    growth.reset()


@pytest.fixture()
def vault_backend(tmp_path, monkeypatch):
    """A real vault backend on a temporary folder, wired as the active store."""
    vault = tmp_path / "Jarvis Obsidian Vault"
    vault.mkdir()
    backend = ObsidianVaultBackend(
        index=VaultIndex(path=vault, db_path=tmp_path / "index.db"))
    monkeypatch.setattr(memory_backend, "get_memory_backend", lambda: backend)
    return vault


def test_a_fact_stored_through_the_gateway_reaches_the_vault(vault_backend,
                                                             growth_file):
    result = memory_backend.store_in_backend(
        "The user prefers one night one piece", category="PREFERENCES",
        explicit=True, source="test", confidence=0.95)
    assert result["stored"] is True
    assert result["backend"] == "obsidian"
    notes = sorted((vault_backend / "Memory").rglob("*.md"))
    assert notes, "the vault received no note"
    text = notes[0].read_text(encoding="utf-8")
    assert "one night one piece" in text
    assert "source: test" in text
    assert growth.snapshot()["backends"]["obsidian"] == 1


def test_the_memory_self_test_reports_the_store_that_served_it(vault_backend,
                                                               growth_file):
    result = memory_backend.test_roundtrip()
    assert result["ok"] is True
    assert result["backend"] == "Obsidian Vault"


def test_the_original_store_grows_too(monkeypatch, growth_file):
    monkeypatch.setattr(memory_backend, "get_memory_backend",
                        lambda: memory_backend.LocalMemoryBackend(_FakeBrain()))
    result = memory_backend.store_in_backend("The user's name is Tony",
                                             category="PROFILE")
    assert result["stored"] is True and result["backend"] == "local"
    stats = growth.snapshot()
    assert stats["writes"] == 1
    assert stats["backends"]["local"] == 1
    assert stats["level"] == 2
    # A fact the store declined counts nothing: growth is never invented.
    declined = memory_backend.store_in_backend("chatter", local_stored=False)
    assert declined["stored"] is False
    assert growth.snapshot()["writes"] == 1


def test_a_switch_that_cannot_be_persisted_says_so(monkeypatch, growth_file):
    import config.settings as settings

    class _FakeObsidian:
        name = "obsidian"

    monkeypatch.setattr(memory_backend, "_build_obsidian",
                        lambda: (_FakeObsidian(), ""))
    monkeypatch.setattr(settings, "set_value", lambda name, value: False)
    monkeypatch.setattr(memory_backend, "_backend", None)

    result = memory_backend.set_memory_backend("obsidian")

    assert result["ok"] is True and result["active"] == "obsidian"
    assert result["persisted"] is False
    assert "config file" in result["detail"]
    memory_backend.reset_memory_backend()


def test_a_switch_that_persists_reports_where_it_landed(monkeypatch, growth_file):
    import config.settings as settings

    class _FakeObsidian:
        name = "obsidian"

    monkeypatch.setattr(memory_backend, "_build_obsidian",
                        lambda: (_FakeObsidian(), ""))
    monkeypatch.setattr(settings, "set_value", lambda name, value: True)
    monkeypatch.setattr(memory_backend, "_backend", None)

    result = memory_backend.set_memory_backend("obsidian")

    assert result["persisted"] is True
    assert result["detail"] == "Obsidian Vault memory is active."
    memory_backend.reset_memory_backend()
