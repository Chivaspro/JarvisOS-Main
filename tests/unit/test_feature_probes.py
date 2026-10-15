"""The on-demand feature probes: what is installed, and what would add it.

A probe that guesses is worse than no probe — the answer here is what the
Settings card and the diagnostics list repeat to the user — so every state is
asserted from a forced condition rather than from whatever this machine happens
to have.
"""
from __future__ import annotations

import pytest

from app import installer


class _Result:
    def __init__(self, stdout: bytes = b"v20.11.0", returncode: int = 0):
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


# ── the Node runtime the MCP host needs ──────────────────────────────────────

def test_a_missing_node_is_reported_as_missing(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("node")

    monkeypatch.setattr(installer.subprocess, "run", _boom)
    ready, detail = installer.node_status()
    assert ready is False
    assert "PATH" in detail


def test_an_old_node_is_not_good_enough(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result(b"v16.4.0"))
    ready, detail = installer.node_status()
    assert ready is False
    assert "older than" in detail and "18" in detail


def test_node_with_no_version_output_is_not_assumed_to_work(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda *a, **k: _Result(b"", returncode=1))
    ready, detail = installer.node_status()
    assert ready is False and detail


def test_a_modern_node_is_ready_and_its_version_is_quoted(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result(b"v20.11.0"))
    ready, detail = installer.node_status()
    assert ready is True
    assert detail == "node v20.11.0"


# ── the feature table ────────────────────────────────────────────────────────

def test_every_row_names_the_feature_that_needs_it_and_how_to_add_it():
    rows = installer.feature_status()
    assert rows, "the probe must return the table"
    features = {row["feature"] for row in rows}
    assert {"documents", "clipboard", "smart_home", "instagram", "mcp_host"} <= features
    for row in rows:
        assert row["purpose"], row
        if row["ready"]:
            assert row["fix"] == "", "a ready package needs no fix"
        else:
            assert row["fix"], f"{row['package']} is missing and must name a fix"


def test_the_install_never_claims_success_it_cannot_verify(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(installer, "_available", lambda module: False)
    monkeypatch.setattr(installer, "_pip", lambda package, log=None: calls.append(package) or True)
    ok, message = installer.install_feature("documents")
    assert calls, "the missing packages must be pip-installed"
    assert ok is False, "importable is what counts, not that pip returned 0"
    assert "still not importable" in message


def test_installing_a_feature_that_is_already_there_changes_nothing(monkeypatch):
    monkeypatch.setattr(installer, "_available", lambda module: True)
    monkeypatch.setattr(installer, "_pip", lambda *a, **k: pytest.fail("nothing to install"))
    ok, message = installer.install_feature("documents")
    assert ok is True and "already installed" in message


def test_an_unknown_feature_lists_the_real_ones():
    ok, message = installer.install_feature("teleporter")
    assert ok is False
    assert "documents" in message and "smart_home" in message


# ── the diagnostics rows ─────────────────────────────────────────────────────

def test_diagnostics_reports_one_row_per_probe_and_carries_the_fix(monkeypatch):
    from capabilities import health
    monkeypatch.setattr(installer, "feature_status", lambda: [
        {"feature": "documents", "module": "openpyxl", "package": "openpyxl",
         "purpose": "Excel workbooks", "ready": True, "detail": "", "fix": ""},
        {"feature": "documents", "module": "pypdf", "package": "pypdf",
         "purpose": "PDF merge", "ready": False, "detail": "missing pypdf",
         "fix": "pip install pypdf"},
    ])
    checks = health.feature_checks()
    assert len(checks) == 2
    ready, missing = checks
    assert ready.name == "documents · openpyxl" and ready.fix == ""
    assert missing.name == "documents · pypdf"
    assert "pip install pypdf" in missing.fix
    assert "missing pypdf" in missing.detail


def test_a_broken_probe_is_reported_instead_of_raising(monkeypatch):
    from capabilities import health

    def _boom():
        raise RuntimeError("installer is broken")

    monkeypatch.setattr(installer, "feature_status", _boom)
    checks = health.feature_checks()
    assert len(checks) == 1
    assert "installer is broken" in checks[0].detail
