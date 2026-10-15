"""A credential field must not be able to store pasted prose.

This machine stored 116 characters of text in ``openrouter_api_key``; every
OpenRouter forge attempt then failed with a 401 while the hub still reported
the link ready. The save path now refuses a credential value that contains
whitespace, keeps the previously stored value in place, and says so — this file
pins that contract.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

GOOD_KEY = "AQ." + "x" * 40


@pytest.fixture()
def settings(shared_qapplication):
    if shared_qapplication is None:
        pytest.skip("PyQt6 is not installed")
    import ui_settings
    with tempfile.TemporaryDirectory(prefix="jarvis-cred-guard-") as folder:
        cfg = Path(folder) / "api_keys.json"
        cfg.write_text(json.dumps({"gemini_api_key": GOOD_KEY,
                                   "features": {}}), encoding="utf-8")
        win = ui_settings.SettingsWindow(config_path=cfg)
        win._test_cfg_path = cfg
        yield win
        win.close()


def _stored(win) -> dict:
    return json.loads(win._test_cfg_path.read_text(encoding="utf-8"))


def test_a_pasted_paragraph_is_not_saved_as_a_key(settings):
    settings.gemini_key_edit.setText("now  scan or innspect en this is prose")
    settings._save_api_tab()
    after = _stored(settings)
    assert after["gemini_api_key"] == GOOD_KEY, \
        "the previously stored key must survive the refusal"
    assert "NOT SAVED" in settings._status.text()
    pill = settings._key_pills["gemini_api_key"]
    assert pill.accessibleName() == "unavailable"
    assert "spaces" in pill.toolTip()
    # The user's text is still in the field, so nothing vanished silently.
    assert settings.gemini_key_edit.text() == "now  scan or innspect en this is prose"


def test_a_clean_key_still_saves_normally(settings):
    settings.gemini_key_edit.setText("AIzaSy" + "y" * 30)
    settings._save_api_tab()
    assert _stored(settings)["gemini_api_key"] == "AIzaSy" + "y" * 30
    assert "NOT SAVED" not in settings._status.text()


def test_the_rejection_is_reported_in_the_window_log(settings):
    seen: list[str] = []
    settings.write_log_safe = lambda text: seen.append(str(text))
    settings.gemini_key_edit.setText("not a key at all")
    settings._save_api_tab()
    assert any("not keys" in line for line in seen)
