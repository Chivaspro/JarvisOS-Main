"""MCP Manage dialog — offscreen.

Regression for the exception in `ui_mcp_panel.py` line 1843 that the auto-heal
sentry captured: `_build()` wired the "Remove Server" button to `self._remove`,
a method McpManageDialog never defined, so evaluating the attribute raised
AttributeError while the dialog was still being constructed — every Manage
window crashed on open. The handler now exists and delegates to the manager.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QLabel, QWidget  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

import ui_mcp_panel  # noqa: E402


class _FakeManager:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def status(self) -> dict:
        return {"servers": [{"name": "demo", "state": "stopped",
                             "enabled": False, "transport": "stdio"}]}

    def server_row(self, runtime):  # pragma: no cover - unused by the dialog fake
        return {}

    def remove_server(self, name: str) -> dict:
        self.removed.append(str(name))
        return {"ok": True, "detail": f"{name} was removed"}


def _make_dialog(monkeypatch, fake: _FakeManager):
    monkeypatch.setattr(ui_mcp_panel, "_manager", lambda: fake)
    panel = QWidget()
    panel._status = QLabel("")
    dialog = ui_mcp_panel.McpManageDialog(name="demo", panel=panel)
    return dialog, panel


def test_manage_dialog_constructs_without_attribute_error(qapp, monkeypatch):
    fake = _FakeManager()
    dialog, _panel = _make_dialog(monkeypatch, fake)
    try:
        assert callable(getattr(dialog, "_remove", None)), \
            "the Remove Server handler must exist"
    finally:
        dialog._timer.stop()
        dialog.deleteLater()
        qapp.processEvents()


def test_remove_disconnects_and_reports(qapp, monkeypatch):
    fake = _FakeManager()
    dialog, panel = _make_dialog(monkeypatch, fake)
    monkeypatch.setattr(ui_mcp_panel.QMessageBox, "question",
                        staticmethod(lambda *a, **k: ui_mcp_panel.QMessageBox.StandardButton.Yes))
    try:
        dialog._remove()
        assert fake.removed == ["demo"], "the manager must be told to remove it"
        assert "removed" in panel._status.text()
        assert dialog.result() == dialog.DialogCode.Accepted
    finally:
        dialog._timer.stop()
        dialog.deleteLater()
        qapp.processEvents()


def test_remove_cancel_keeps_the_server(qapp, monkeypatch):
    fake = _FakeManager()
    dialog, _panel = _make_dialog(monkeypatch, fake)
    monkeypatch.setattr(ui_mcp_panel.QMessageBox, "question",
                        staticmethod(lambda *a, **k: ui_mcp_panel.QMessageBox.StandardButton.Cancel))
    try:
        dialog._remove()
        assert fake.removed == [], "Cancel must not remove anything"
    finally:
        dialog._timer.stop()
        dialog.deleteLater()
        qapp.processEvents()
