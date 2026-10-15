"""Tests for the refactored SetupOverlay initialization panel."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QLabel, QLineEdit, QPushButton
from ui import SetupOverlay


def test_setup_overlay_initialization_fields(shared_qapplication):
    """SetupOverlay includes fields for User Name, Gemini Key, and OpenRouter Key."""
    overlay = SetupOverlay()
    assert hasattr(overlay, "_user_input")
    assert hasattr(overlay, "_key_input")
    assert hasattr(overlay, "_or_key_input")

    assert isinstance(overlay._user_input, QLineEdit)
    assert isinstance(overlay._key_input, QLineEdit)
    assert isinstance(overlay._or_key_input, QLineEdit)

    # Gemini & OpenRouter inputs default to password mode
    assert overlay._key_input.echoMode() == QLineEdit.EchoMode.Password
    assert overlay._or_key_input.echoMode() == QLineEdit.EchoMode.Password


def test_setup_overlay_instructional_section(shared_qapplication):
    """SetupOverlay contains an informative instructional guide explaining credentials."""
    overlay = SetupOverlay()
    labels = overlay.findChildren(QLabel)
    texts = " ".join(lbl.text() for lbl in labels)

    assert "CREDENTIAL ARCHITECTURE" in texts or "SYSTEM GUIDE" in texts
    assert "Operator Identity" in texts or "User Name" in texts
    assert "Gemini API Key" in texts
    assert "OpenRouter API Key" in texts


def test_setup_overlay_validation_on_empty_gemini_key(shared_qapplication):
    """Submitting with an empty Gemini API key shows error and does not emit done."""
    overlay = SetupOverlay()
    overlay._key_input.setText("")

    emitted = []
    overlay.done.connect(lambda *args: emitted.append(args))

    overlay._submit()
    assert len(emitted) == 0
    assert not overlay._status_lbl.isHidden()
    assert "Gemini API Key is required" in overlay._status_lbl.text()


def test_setup_overlay_submit_success_emits_credentials(shared_qapplication):
    """Submitting with valid credentials emits all required arguments."""
    overlay = SetupOverlay()
    overlay._user_input.setText("Tony Stark")
    overlay._key_input.setText("AIzaSyTestGeminiKey123")
    overlay._or_key_input.setText("sk-or-v1-testopenrouter")

    emitted = []
    overlay.done.connect(lambda *args: emitted.append(args))

    overlay._submit()
    assert len(emitted) == 1
    gemini_key, os_name, user_name, or_key = emitted[0]
    assert gemini_key == "AIzaSyTestGeminiKey123"
    assert user_name == "Tony Stark"
    assert or_key == "sk-or-v1-testopenrouter"
    assert os_name in ("windows", "mac", "linux")


def test_setup_overlay_get_api_key_helper(shared_qapplication, monkeypatch):
    """Clicking get API key triggers browser URL helper."""
    overlay = SetupOverlay()
    opened_urls = []
    monkeypatch.setattr(overlay, "_open_url", lambda url: opened_urls.append(url))

    # Find the Get Gemini API Key button
    btns = [b for b in overlay.findChildren(QPushButton) if "GET API KEY" in b.text() or "GET GEMINI" in b.text()]
    assert len(btns) >= 1
    btns[0].click()

    assert len(opened_urls) == 1
    assert "aistudio.google.com" in opened_urls[0]


def test_on_setup_done_saves_all_credentials(tmp_path, monkeypatch):
    """_on_setup_done persists gemini_key, user_name, openrouter_key, and os_system."""
    import json
    import ui

    fake_api_file = tmp_path / "api_keys.json"
    monkeypatch.setattr(ui, "API_FILE", fake_api_file)
    monkeypatch.setattr(ui, "CONFIG_DIR", tmp_path)

    class _MockWindow:
        _overlay = None
        _assistant_name = "JARVIS"
        _ready = False
        def write_log(self, text): pass
        def _restore_window_position(self, animate=False): pass
        def _apply_state(self, state): pass

    win = _MockWindow()
    ui.MainWindow._on_setup_done(
        win,
        "test-gemini-key",
        "windows",
        "Tony Stark",
        "sk-or-v1-test",
    )

    assert win._ready is True
    saved = json.loads(fake_api_file.read_text(encoding="utf-8"))
    assert saved["gemini_api_key"] == "test-gemini-key"
    assert saved["os_system"] == "windows"
    assert saved["user_name"] == "Tony Stark"
    assert saved["openrouter_api_key"] == "sk-or-v1-test"

