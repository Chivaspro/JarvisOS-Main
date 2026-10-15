"""The Obsidian vault memory backend: index, recall, and note writing.

Everything here runs against a throwaway vault built from scratch, so the user's
real vault is never touched. Three contracts are pinned:

* the index reads real frontmatter, links and tags, and backlinks are the
  reverse of the links actually written;
* the backend writes durable facts *under* ``Memory/`` and never edits the
  user's own notes;
* a correction merges into the one existing note (keeping its previous wording
  as history) instead of piling up near-duplicates.
"""
from __future__ import annotations

import pytest

from memory.obsidian_backend import MEMORY_FOLDER, ObsidianVaultBackend
from memory.vault_store import (
    VaultIndex,
    extract_links,
    extract_tags,
    note_title,
    parse_frontmatter,
)


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "Welcome.md").write_text(
        "# Welcome\n\nHello and welcome to the vault.\n\n"
        "See [[Reference]] for more. #intro\n", encoding="utf-8")
    (root / "Reference.md").write_text(
        "---\ntags: [geo, notes]\n---\n"
        "# Reference\n\nFacts about the globe and its places. #geo\n",
        encoding="utf-8")
    return root


@pytest.fixture()
def index(vault, tmp_path):
    idx = VaultIndex(path=vault, db_path=tmp_path / "index.db")
    assert idx.scan()["ok"]
    return idx


# ── parsing ──────────────────────────────────────────────────────────────────

def test_frontmatter_links_tags_and_title_are_read():
    frontmatter, body = parse_frontmatter(
        "---\ntags:\n  - x\n  - y\n---\n# Title\nBody")
    assert frontmatter["tags"] == ["x", "y"]
    assert body.lstrip().startswith("# Title")
    assert extract_links("a [[One]] b [[Two|alias]] c") == ["One", "Two"]
    assert "geo" in extract_tags("text #geo #second", {"tags": ["front"]})
    assert "front" in extract_tags("text", {"tags": ["front"]})


def test_note_title_prefers_a_heading(tmp_path):
    path = tmp_path / "Some File.md"
    assert note_title(path, "# Real Title\nbody") == "Real Title"
    assert note_title(path, "no heading here") == "Some File"


# ── the index ────────────────────────────────────────────────────────────────

def test_scan_indexes_notes_links_and_backlinks(index):
    stats = index.stats()
    assert stats["notes"] == 2
    ref = index.note("Reference.md")
    assert ref is not None and "geo" in ref["tags"]
    back = index.backlinks("Reference.md")
    assert any("Welcome" in str(row.get("title") or "") or
               "Welcome" in str(row.get("path") or "") for row in back)


def test_search_finds_a_note_by_its_real_body(index):
    hits = index.search("globe")
    assert hits and any("Reference" in hit["title"] for hit in hits)


def test_the_graph_is_built_from_real_relationships(index):
    graph = index.graph()
    assert len(graph["nodes"]) >= 2
    assert graph["links"], "the [[Reference]] link must become an edge"


def test_a_missing_vault_is_reported_not_crashed(tmp_path):
    idx = VaultIndex(path=tmp_path / "nope", db_path=tmp_path / "i.db")
    result = idx.scan()
    assert result["ok"] is False and "does not exist" in result["detail"]


# ── the memory backend ───────────────────────────────────────────────────────

def test_writing_memory_creates_a_note_under_the_memory_folder(index, vault):
    backend = ObsidianVaultBackend(index=index)
    assert backend.available()
    assert backend.write("The user's favourite city is Tokyo",
                         category="CONTEXT", explicit=True, reason="told me")
    notes = [n for n in index.notes() if n["path"].startswith(f"{MEMORY_FOLDER}/")]
    assert len(notes) == 1
    assert "Tokyo" in index.read_note(notes[0]["path"])


def test_recall_returns_the_written_memory(index):
    backend = ObsidianVaultBackend(index=index)
    backend.write("The user's favourite city is Tokyo", category="CONTEXT",
                  explicit=True)
    hits = backend.search("favourite city")
    assert hits and hits[0]["backend"] == "obsidian"
    assert "Tokyo" in hits[0]["content"]


def test_a_correction_merges_into_the_one_note_with_history(index):
    backend = ObsidianVaultBackend(index=index)
    backend.write("The user's favourite city is Tokyo", category="CONTEXT",
                  explicit=True)
    backend.write("The user's favourite city is Kyoto", category="CONTEXT",
                  explicit=True)
    notes = [n for n in index.notes() if n["path"].startswith(f"{MEMORY_FOLDER}/")]
    assert len(notes) == 1, "a correction must not create a second note"
    body = index.read_note(notes[0]["path"])
    assert "Kyoto" in body
    assert "Tokyo" in body, "the previous wording is kept as history"


def test_forget_removes_only_jarvis_memories(index):
    backend = ObsidianVaultBackend(index=index)
    backend.write("The user's favourite city is Tokyo", category="CONTEXT",
                  explicit=True)
    assert backend.forget("favourite city") == 1
    assert not [n for n in index.notes()
                if n["path"].startswith(f"{MEMORY_FOLDER}/")]
    # The user's own notes are still there.
    assert index.note("Welcome.md") is not None


def test_short_or_chatty_content_is_not_stored(index):
    backend = ObsidianVaultBackend(index=index)
    assert backend.write("ok", category="CONTEXT") is False
    assert backend.write("", category="CONTEXT", explicit=True) is False


def test_an_unavailable_vault_refuses_to_write(tmp_path):
    idx = VaultIndex(path=tmp_path / "missing", db_path=tmp_path / "i.db")
    backend = ObsidianVaultBackend(index=idx)
    assert backend.available() is False
    assert backend.write("A durable fact worth keeping", explicit=True) is False
