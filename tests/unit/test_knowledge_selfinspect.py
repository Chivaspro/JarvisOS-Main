"""Knowledge + self-inspection tests (tasks 7.1, 7.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from knowledge.retriever import knowledge_search, local_search  # noqa: E402
from brain import self_inspect as si  # noqa: E402


# ── 7.1 knowledge (default = local, sources returned) ─────────────────────────

def test_default_backend_is_local_with_sources(tmp_path):
    (tmp_path / "notes.md").write_text(
        "The orchestrator replans after a failed step.", encoding="utf-8")
    r = knowledge_search("orchestrator replan", root=tmp_path, limit=3)
    assert r["backend"] == "local"
    assert r["results"] and "source" in r["results"][0]
    assert "orchestrator" in r["results"][0]["passage"].lower()


def test_local_search_never_raises_on_binary(tmp_path):
    (tmp_path / "blob.pdf").write_bytes(b"\x25\x50\x44\x46-fake")
    r = knowledge_search("anything", root=tmp_path)
    assert r["backend"] == "local"   # honest empty result, no crash


def test_qdrant_disabled_by_default():
    st = __import__("knowledge.retriever", fromlist=["knowledge_status"]) \
        .knowledge_status()
    assert st["qdrant_enabled"] is False


# ── 7.2 self-inspection ───────────────────────────────────────────────────────

def test_inspect_source_prefers_files_over_screenshots():
    out = si.inspect_source("brain/graph.py")
    assert out["ok"] and out["source"] == "filesystem"
    assert any("compiles clean" in f for f in out["findings"])


def test_inspect_source_reports_syntax_error(tmp_path, monkeypatch):
    monkeypatch.setattr(si, "_base", lambda: tmp_path)
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    out = si.inspect_source("broken.py")
    assert any("SYNTAX ERROR" in f for f in out["findings"])


def test_inspect_logs_never_raises():
    out = si.inspect_logs()
    assert out["ok"] in (True, False) and "findings" in out


def test_plan_is_produced_before_any_edit():
    plan = si.build_plan("brain/graph.py")
    assert plan["verification_steps"]
    assert "proposed_changes" in plan


def test_repair_refuses_without_plan():
    r = si.execute_repair(None)
    assert r["ok"] is False and "no repair plan" in r["output"]


def test_handle_tool_contract_actions():
    for action in ("inspect", "plan", "status"):
        out = si.handle_self_inspect({"action": action, "target": "brain/graph.py"})
        assert isinstance(out, str) and out
    out = si.handle_self_inspect({"action": "nonsense"})
    assert "Unknown" in out
