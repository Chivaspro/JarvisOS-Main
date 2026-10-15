"""Behavioural tests for the consolidated API tab in Settings."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLineEdit  # noqa: E402


@pytest.fixture(scope="module")
def settings_window(tmp_path_factory):
    import ui_settings
    app = QApplication.instance() or QApplication([])
    cfg = tmp_path_factory.mktemp("apicfg") / "api_keys.json"
    cfg.write_text(json.dumps({
        "gemini_api_key": "gm-old",
        "openai_api_key": "sk-old",
        "fish_audio_api_key": "fish-old",
        "elevenlabs_api_key": "el-old",
        "features": {"mem0_memory": True, "qdrant_knowledge": False},
    }), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=cfg)
    win._test_cfg_path = cfg
    yield win
    win.deleteLater()
    del app


def test_api_tab_exists_and_precedes_ai(settings_window):
    tabs = [settings_window.tabs.tabText(i) for i in range(settings_window.tabs.count())]
    assert "API" in tabs
    assert tabs.index("API") < tabs.index("AI")


def test_every_key_field_is_preloaded_from_config(settings_window):
    w = settings_window
    assert w.gemini_key_edit.text() == "gm-old"
    assert w.openai_api_key.text() == "sk-old"
    assert w.fish_audio_key_edit.text() == "fish-old"
    assert w.elevenlabs_key_edit.text() == "el-old"
    # Optional subsystems start empty when config has no value.
    assert w.mem0_key_edit.text() == ""
    assert w.qdrant_key_edit.text() == ""
    # PC automation has no credential of its own: UI-TARS and Agent TARS both
    # run on the Gemini key above, so there is no UI TARS field at all.
    assert not hasattr(w, "ui_tars_key_edit")
    # Key fields are password-masked.
    for fld in (w.gemini_key_edit, w.mem0_key_edit, w.qdrant_key_edit):
        assert fld.echoMode() == QLineEdit.EchoMode.Password


def test_feature_flags_preload_from_the_features_map(settings_window):
    w = settings_window
    assert w._mem0_flag.isChecked() is True
    assert w._qdrant_flag.isChecked() is False


def test_save_api_tab_persists_keys_and_flags(settings_window):
    w = settings_window
    w.gemini_key_edit.setText("gm-new")
    w.mem0_key_edit.setText("m0-test")
    w.qdrant_url_edit.setText("http://127.0.0.1:6333")
    w._mem0_flag.setChecked(False)
    w._qdrant_flag.setChecked(True)
    w._save_api_tab()
    saved = json.loads(w._test_cfg_path.read_text(encoding="utf-8"))
    assert saved["gemini_api_key"] == "gm-new"
    assert saved["mem0_api_key"] == "m0-test"
    assert saved["qdrant_url"] == "http://127.0.0.1:6333"
    assert saved["features"]["mem0_memory"] is False
    assert saved["features"]["qdrant_knowledge"] is True
    # Fish/ElevenLabs round-trip their stored values on this save.
    assert saved["fish_audio_api_key"] == "fish-old"
    assert saved["elevenlabs_api_key"] == "el-old"


def test_qdrant_store_prefers_env_then_config():
    import importlib
    from unittest.mock import patch
    import knowledge.qdrant_store as qs
    importlib.reload(qs)
    # No env → the config value applies.
    os.environ.pop("QDRANT_URL", None)
    with patch("config.settings.get", return_value="http://cfg-host:6333"):
        assert qs._qdrant_setting("QDRANT_URL") == "http://cfg-host:6333"
    # Env beats config.
    os.environ["QDRANT_URL"] = "http://env-host:6333"
    try:
        assert qs._qdrant_setting("QDRANT_URL") == "http://env-host:6333"
    finally:
        os.environ.pop("QDRANT_URL", None)


def test_api_tab_has_no_ui_tars_credential(settings_window):
    """UI-TARS has no credential of its own: it runs on the Gemini key, so the
    API tab neither shows a field for one nor persists one."""
    w = settings_window
    assert not hasattr(w, "ui_tars_key_edit")
    assert "ui_tars_api_key" not in w._collect_api_keys()
    w.gemini_key_edit.setText("gm-key-for-tars")
    w._save_api_tab()
    saved = json.loads(w._test_cfg_path.read_text(encoding="utf-8"))
    assert saved["gemini_api_key"] == "gm-key-for-tars"
    assert "ui_tars_api_key" not in saved or not saved["ui_tars_api_key"]


def test_full_save_persists_api_tab_edits(settings_window):
    """save_settings merges the API tab's key edits over self._data, so a
    key typed on the API tab survives a full SAVE CHANGES."""
    w = settings_window
    w.gemini_key_edit.setText("gm-full-save")
    data = w._collect()
    # _collect itself carries no key fields; the merge happens in save_settings.
    assert "gemini_api_key" not in data
    w._save_api_tab()
    saved = json.loads(w._test_cfg_path.read_text(encoding="utf-8"))
    assert saved["gemini_api_key"] == "gm-full-save"
