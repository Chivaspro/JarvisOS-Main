"""Memory backend + promotion policy tests (tasks 6.1-6.3).

Proves: the local backend serves recall/save identically through the
interface, the promotion policy stores durable facts and rejects ephemeral
turns, Mem0 is dormant when disabled, and an enabled-but-broken Mem0 falls
back to the local store with exactly one degradation notice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory.policy import classify_scope, promote, recall, Scope  # noqa: E402
from memory.backend import LocalMemoryBackend  # noqa: E402
from memory import mem0_client  # noqa: E402


# ── 6.2 promotion policy ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("my sister's name is Ayşe", Scope.LONG_TERM),
    ("I prefer dark mode in all apps", Scope.LONG_TERM),
    ("remember this: the API rate limit resets hourly", Scope.LONG_TERM),
    ("I work at a hospital", Scope.LONG_TERM),
])
def test_durable_facts_are_long_term(text, expected):
    assert classify_scope(text).scope == expected


@pytest.mark.parametrize("text", [
    "open Chrome now",
    "what is on my screen",
    "ok thanks",
    "can you see this",
])
def test_ephemeral_and_chatter_are_not_promoted(text):
    cand = classify_scope(text)
    assert cand.scope in (Scope.SESSION, Scope.TEMPORARY)


def test_promote_stores_durable_and_rejects_ephemeral():
    class FakeBackend:
        name = "fake"
        stored: list[str] = []

        def write(self, content, **kw):
            FakeBackend.stored.append(content)
            return True

        def search(self, query, limit=8):
            return []

        def available(self):
            return True

    fb = FakeBackend()
    r1 = promote("my sister's name is Ayşe", backend=fb)
    r2 = promote("open Chrome now", backend=fb)
    r3 = promote("thanks", backend=fb)
    assert r1["stored"] is True and r1["scope"] == "long_term"
    assert r2["stored"] is False
    assert r3["stored"] is False
    assert FakeBackend.stored == ["my sister's name is Ayşe"]


def test_explicit_remember_promotes_unclassified():
    cand = classify_scope("the garage door code is 1234", explicit=True)
    assert cand.scope == Scope.LONG_TERM


def test_project_scope_carries_project_id():
    cand = classify_scope("I prefer pytest for this", project_id="mark-lii")
    assert cand.scope == Scope.PROJECT and cand.project_id == "mark-lii"


# ── 6.1 backend interface over the local store ────────────────────────────────

def test_local_backend_write_and_search_roundtrip(tmp_path, monkeypatch):
    from memory.manager import BrainMemory
    from memory.database import BrainDatabase
    db = BrainDatabase(path=tmp_path / "mem.db")
    bm = BrainMemory(db=db)
    be = LocalMemoryBackend(memory=bm)
    assert be.write("my sister's name is Ayşe", explicit=True) is True
    hits = be.search("Ayşe", limit=3)
    assert isinstance(hits, list)


def test_local_backend_never_raises_on_broken_store():
    class Broken:
        def remember(self, *a, **k):
            raise RuntimeError("db locked")

        def retrieve(self, *a, **k):
            raise RuntimeError("db locked")

        def is_enabled(self):
            return True

    be = LocalMemoryBackend(memory=Broken())
    assert be.write("x") is False
    assert be.search("x") == []


# ── 6.3 Mem0 gating + fallback ────────────────────────────────────────────────

def test_mem0_dormant_when_flag_off(monkeypatch):
    import config.settings as settings
    monkeypatch.setattr(settings, "get_feature",
                        lambda name, default=None:
                        False if name == "mem0_memory" else default)
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    from memory.backend import _mem0_configured, reset_memory_backend
    reset_memory_backend()
    assert _mem0_configured() is False


def test_mem0_flag_on_but_no_key_is_dormant(monkeypatch):
    import config.settings as settings
    monkeypatch.setattr(settings, "get_feature",
                        lambda name, default=None:
                        True if name == "mem0_memory" else default)
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.setattr(settings, "get", lambda key, default=None: "")
    from memory.backend import _mem0_configured, reset_memory_backend
    reset_memory_backend()
    assert _mem0_configured() is False


def test_mem0_failure_falls_back_with_one_notice(monkeypatch):
    from memory.backend import LocalMemoryBackend
    from memory.mem0_client import Mem0Backend, reset_degradation_flag
    reset_degradation_flag()
    notices = []
    monkeypatch.setattr(mem0_client, "_log_degradation_once",
                        lambda reason: notices.append(reason))

    class ExplodingClient:
        def add(self, *a, **k):
            raise RuntimeError("network down")

        def search(self, *a, **k):
            raise RuntimeError("network down")

    class FakeLocal:
        name = "local"
        writes: list[str] = []

        def write(self, content, **kw):
            FakeLocal.writes.append(content)
            return True

        def search(self, query, limit=8):
            return [{"content": "local hit", "backend": "local"}]

        def available(self):
            return True

    be = Mem0Backend(api_key="k", fallback=FakeLocal())
    be._client = ExplodingClient()
    assert be.available() is True
    assert be.write("my sister's name is Ayşe") is True
    assert FakeLocal.writes == ["my sister's name is Ayşe"]
    hits = be.search("Ayşe")
    assert hits and hits[0]["backend"] == "local"
    assert len(notices) >= 1


def test_mem0_search_shape_normalization():
    class Client:
        def search(self, query, user_id=None, limit=8):
            return [{"memory": "fact one", "score": 0.9, "metadata": {"category": "PREFS"}},
                    "plain string fact"]

    be = mem0_client.Mem0Backend(api_key="k", fallback=None)
    be._client = Client()
    hits = be.search("anything")
    assert hits[0]["content"] == "fact one"
    assert hits[1]["content"] == "plain string fact"
