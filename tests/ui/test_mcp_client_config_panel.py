"""The MCP editor's "Add to your client config" panel.

The user could not type or paste in it (it was read-only), had no way to write
the server into a client's own config file, and nothing merged on top of what
that file already contained. These tests drive the real dialog.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QMessageBox  # noqa: E402

from mcp_host import client_configs  # noqa: E402
from ui_mcp_panel import McpEditorDialog  # noqa: E402

from .qt_harness import qapp  # noqa: E402,F401  (fixture)


@pytest.fixture()
def dialog(qapp, monkeypatch):  # noqa: F811
    # A modal box would block an unattended run; the status line still updates.
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dlg = McpEditorDialog()
    yield dlg
    try:
        dlg.deleteLater()
    except Exception:
        pass


def test_the_config_box_accepts_typing_and_pasting(dialog):
    assert dialog._config_text.isReadOnly() is False
    assert dialog._regen_btn.isEnabled()
    assert dialog._save_client_btn.isEnabled()

    typed = '{"mcpServers": {"pasted": {"command": "npx"}}}'
    dialog._config_text.setPlainText(typed)
    assert dialog._config_text.toPlainText() == typed
    assert dialog._config_dirty is True

    # A background regeneration (a transport change) must not clobber the paste…
    dialog._render_config()
    assert dialog._config_text.toPlainText() == typed
    # …until Regenerate is asked for.
    dialog._regenerate_config()
    assert dialog._config_dirty is False
    assert "mcpServers" in dialog._config_text.toPlainText()


def test_saving_writes_the_server_into_the_client_file_and_keeps_the_rest(
        dialog, tmp_path, monkeypatch):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(json.dumps({"mcpServers": {"old": {"command": "old"}},
                                  "theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(client_configs, "candidates", lambda target_name: [target])

    dialog._name.setText("git")
    dialog._command.setText("npx")
    dialog._regenerate_config()
    dialog._save_client_config()

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert "git" in saved["mcpServers"]
    assert saved["mcpServers"]["old"] == {"command": "old"}
    assert saved["theme"] == "dark"


def test_an_existing_client_config_is_shown_with_the_new_server_on_top(
        dialog, tmp_path, monkeypatch):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(json.dumps({"mcpServers": {"already": {"command": "x"}}}),
                      encoding="utf-8")
    monkeypatch.setattr(client_configs, "candidates", lambda target_name: [target])

    dialog._name.setText("mine")
    dialog._command.setText("npx")
    dialog._regenerate_config()

    shown = json.loads(dialog._config_text.toPlainText())
    assert "already" in shown["mcpServers"]
    assert "mine" in shown["mcpServers"]
    assert "adding on top" in dialog._config_desc.text().lower()
