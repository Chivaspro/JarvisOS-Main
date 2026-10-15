"""The document architect: registry vocabulary, the permission gate, and the
builders writing real files.

The tests never depend on an optional package being installed. Where a builder
may be absent they assert on whichever honest answer the machine can give: a real
file, or a sentence naming the package and its pip command.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from capabilities import actions, registry, router
from config.settings import FEATURE_DEFAULTS
from documents import builders, tool as documents_tool
from security import permissions


@pytest.fixture()
def workspace(monkeypatch):
    """An isolated documents folder — never the real data directory."""
    with tempfile.TemporaryDirectory(prefix="jarvis-docs-") as folder:
        root = Path(folder)
        monkeypatch.setattr(builders, "workspace_dir", lambda: root)
        yield root


def _enabled_with(*off: str):
    def _enabled(feature: str) -> bool:
        return feature not in off
    return _enabled


# ── registry vocabulary ───────────────────────────────────────────────────────

def test_the_documents_tool_is_declared_with_every_action():
    names = registry.names(registry.TOOL_DOCUMENTS)
    assert "create_docx" in names and "create_xlsx" in names
    assert "create_pptx" in names and "pdf_from_text" in names
    assert "pdf_merge" in names and "pdf_extract" in names
    params = registry.parameters(registry.TOOL_DOCUMENTS)
    assert params["required"] == ["action"]
    assert "content" in params["properties"]


def test_a_document_action_without_content_is_refused_before_any_writer_runs():
    action, error = registry.validate(registry.TOOL_DOCUMENTS, {"action": "create_docx"})
    assert action is None
    assert "content" in error
    action, error = registry.validate(registry.TOOL_DOCUMENTS,
                                      {"action": "pdf_extract", "path": "a.pdf"})
    assert action is None
    assert "pages" in error


def test_every_document_action_declares_a_real_switch_and_a_controller():
    for action in registry.for_tool(registry.TOOL_DOCUMENTS):
        assert action.permission in FEATURE_DEFAULTS, action.name
        assert action.controller
        assert action.chain


def test_the_documents_tool_has_a_router_lock_and_a_runner():
    # A registry tool missing from LOCK_OF would crash inside _locked_run
    # instead of refusing cleanly, so every tool must be mapped.
    for tool in registry.TOOLS:
        assert tool in router.LOCK_OF, tool
        assert tool in router.ENGINE_OF, tool
        assert tool in router.LABEL_OF, tool
        assert tool in actions._RUNNERS, tool


# ── permission gate ───────────────────────────────────────────────────────────

def test_writing_a_document_is_gated_by_the_document_switch():
    refusal = permissions.permission_refusal(
        _enabled_with("document_architect"), registry.TOOL_DOCUMENTS,
        {"action": "create_docx"})
    assert refusal and "Document architect" in refusal


def test_asking_which_engines_are_installed_is_never_refused():
    assert permissions.permission_refusal(
        _enabled_with("document_architect"), registry.TOOL_DOCUMENTS,
        {"action": "engines"}) is None


def test_a_revoked_switch_stops_the_tool_before_the_writer_runs(workspace):
    out = actions.handle_documents({"action": "pdf_from_text",
                                    "content": '{"title": "x", "body": "y"}'},
                                   enabled=_enabled_with("document_architect"))
    assert "Document architect" in out
    assert "status=NOT_DONE" in out
    assert not list(workspace.iterdir())


# ── honest answers ────────────────────────────────────────────────────────────

def test_an_unknown_document_action_lists_the_valid_ones():
    result = documents_tool.run("make_a_poster", {})
    assert not result["ok"]
    assert "create_docx" in result["detail"]


def test_a_missing_optional_package_is_named_with_its_pip_command(workspace, monkeypatch):
    import importlib.util
    if importlib.util.find_spec("docx") is not None:
        pytest.skip("python-docx is installed on this machine")
    result = documents_tool.run("create_docx", {"content": '{"title": "Report"}'})
    assert not result["ok"]
    assert result["reason"] == "missing_dependency"
    assert "pip install python-docx" in result["detail"]
    assert not list(workspace.iterdir())


def test_empty_content_is_refused_rather_than_written(workspace):
    result = documents_tool.run("create_xlsx", {"content": "{}"})
    assert not result["ok"]
    assert result["reason"] == "no_content"
    assert not list(workspace.iterdir())


def test_merging_needs_at_least_two_paths(workspace):
    result = documents_tool.run("pdf_merge", {"paths": "only-one.pdf"})
    assert not result["ok"]
    assert result["reason"] == "no_sources"


def test_extracting_from_a_file_that_does_not_exist_is_reported(workspace):
    result = documents_tool.run("pdf_extract",
                                {"path": str(workspace / "ghost.pdf"), "pages": "1"})
    assert not result["ok"]
    assert result["reason"] in ("not_found", "missing_dependency")


def test_listing_an_empty_documents_folder_says_so(workspace):
    result = documents_tool.run("list_documents", {})
    assert result["ok"]
    assert "not generated any documents" in result["detail"]


def test_the_engines_report_names_every_builder_and_the_folder(workspace):
    result = documents_tool.run("engines", {})
    assert result["ok"]
    for module in ("docx", "openpyxl", "pptx", "pypdf", "reportlab"):
        assert module in result["detail"]
    assert str(workspace) in result["detail"]


# ── real files ────────────────────────────────────────────────────────────────

def test_a_pdf_from_text_is_a_real_pdf_in_the_workspace(workspace):
    result = documents_tool.run("pdf_from_text", {
        "content": '{"title": "Status Report", "body": "All systems nominal."}',
    })
    assert result["ok"], result["detail"]
    path = Path(result["evidence"]["path"])
    assert path.exists() and path.parent == workspace
    assert path.read_bytes().startswith(b"%PDF")
    # The builder slugs the title into a filesystem-safe name.
    assert "Status" in path.name and path.suffix == ".pdf"


def test_save_as_puts_the_file_where_the_user_asked(workspace):
    target = workspace / "nested" / "quarterly.pdf"
    result = documents_tool.run("pdf_from_text", {
        "content": '{"title": "Quarterly", "body": "Numbers."}',
        "save_as": str(target),
    })
    assert result["ok"], result["detail"]
    assert target.exists()
    assert str(target) in result["detail"]


def test_a_relative_save_as_stays_inside_the_workspace(workspace):
    result = documents_tool.run("pdf_from_text", {
        "content": '{"title": "Notes", "body": "Body text."}',
        "save_as": "notes.pdf",
    })
    assert result["ok"]
    assert (workspace / "notes.pdf").exists()


def test_listing_reports_the_file_that_was_just_written(workspace):
    documents_tool.run("pdf_from_text", {
        "content": '{"title": "Archive", "body": "Body."}'})
    result = documents_tool.run("list_documents", {})
    assert result["ok"]
    assert "Archive" in result["detail"]


def test_a_word_document_is_written_when_the_package_is_available(workspace):
    import importlib.util
    if importlib.util.find_spec("docx") is None:
        pytest.skip("python-docx is not installed on this machine")
    result = documents_tool.run("create_docx", {"content": (
        '{"title": "Weekly Report", "subtitle": "Engineering", "sections": '
        '[{"heading": "Progress", "body": "Two items shipped.", '
        '"bullets": ["Crucible", "Auto-heal"], '
        '"table": {"headers": ["Item", "State"], "rows": [["Crucible", "done"]]}}]}')})
    assert result["ok"], result["detail"]
    path = Path(result["evidence"]["path"])
    assert path.exists() and path.suffix == ".docx"
    assert path.stat().st_size > 1000


def test_pdf_extract_returns_the_pages_it_kept(workspace):
    import importlib.util
    if importlib.util.find_spec("pypdf") is None:
        pytest.skip("pypdf is not installed on this machine")
    made = documents_tool.run("pdf_from_text", {
        "content": '{"title": "Source", "body": "Page one text."}'})
    assert made["ok"]
    result = documents_tool.run("pdf_extract",
                                {"path": made["evidence"]["path"], "pages": "1"})
    assert result["ok"], result["detail"]
    assert result["evidence"]["pages"] == [1]
    assert Path(result["evidence"]["path"]).exists()


def test_a_page_range_that_names_nothing_is_refused(workspace):
    import importlib.util
    if importlib.util.find_spec("pypdf") is None:
        pytest.skip("pypdf is not installed on this machine")
    made = documents_tool.run("pdf_from_text", {
        "content": '{"title": "Source", "body": "Only one page."}'})
    result = documents_tool.run("pdf_extract",
                                {"path": made["evidence"]["path"], "pages": "9"})
    assert not result["ok"]
    assert result["reason"] == "bad_pages"


# ── the tool entry point ──────────────────────────────────────────────────────

def test_the_tool_entry_point_reports_the_controller_and_the_path(workspace):
    out = actions.handle_documents(
        {"action": "pdf_from_text",
         "content": '{"title": "Sitrep", "body": "Everything is fine."}'},
        enabled=_enabled_with())
    assert "controller=documents" in out
    assert "status=DONE" in out
    assert "Sitrep" in out
    assert (workspace / "Sitrep.pdf").exists()


def test_the_activity_row_names_the_file_being_written():
    detail = registry.activity_detail(registry.TOOL_DOCUMENTS, {
        "action": "create_docx", "save_as": "C:/tmp/report.docx"})
    assert detail.startswith("Writing")
    assert "report.docx" in detail
