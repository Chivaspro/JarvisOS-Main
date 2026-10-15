"""Behavioural tests for the AUTOMATION / PERMISSIONS feature checkboxes.

The flag store behind these boxes is the one the runtime reads
(config/api_keys.json → "features"), so two things must hold:

* the box must OPEN showing the stored value — it used to show the built-in
  default instead, which both contradicted the running system on screen and
  silently wrote those defaults back over the user's flags on the next SAVE;
* SAVE must write the flag the box shows (Settings → configuration → runtime).
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication  # noqa: E402

SAVED_FLAGS = {
    "wake_word": False,
    "hands_free": False,
    "push_to_talk": True,
    "realtime_stt": False,
    "voice_activity_detection": False,
    "computer_control": False,
    "desktop_automation": False,
    "mic_access": False,
    "camera_access": False,
    "laya_ai": True,
}


@pytest.fixture(scope="module")
def window_with_flags(tmp_path_factory):
    import ui_settings
    app = QApplication.instance() or QApplication([])
    cfg = tmp_path_factory.mktemp("cfg") / "api_keys.json"
    cfg.write_text(json.dumps({
        "gemini_api_key": "gm-key",
        "features": dict(SAVED_FLAGS),
    }), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=cfg)
    win._test_cfg_path = cfg
    yield win
    win.deleteLater()


def _flags_box(win, key):
    return (getattr(win, "_voice_checks", {}) or {}).get(key) \
        or (getattr(win, "_perm_checks", {}) or {}).get(key)


def test_boxes_open_with_the_stored_flags(window_with_flags):
    """The screen must agree with the configuration it is showing."""
    for key, stored in SAVED_FLAGS.items():
        cb = _flags_box(window_with_flags, key)
        if cb is None:
            continue                      # edited on another tab (laya_ai: AI tab)
        assert cb.isChecked() is bool(stored), \
            f"{key} is stored as {stored} but the Settings box shows {cb.isChecked()}"


def test_realtime_stt_has_its_own_control(window_with_flags):
    cb = _flags_box(window_with_flags, "realtime_stt")
    assert cb is not None, "Realtime STT has no Settings control"
    assert "Realtime STT" in cb.text()
    assert cb.toolTip(), "the control must explain what it does (and does not) switch off"
    assert "microphone" in cb.toolTip().lower()


def test_save_writes_the_flag_the_box_shows(window_with_flags):
    """UI setting → saved configuration (the runtime view is covered by
    tests/unit/test_realtime_stt_toggle.py)."""
    cb = _flags_box(window_with_flags, "realtime_stt")
    cb.setChecked(True)
    collected = window_with_flags._collect()
    assert collected["features"]["realtime_stt"] is True
    cb.setChecked(False)
    collected = window_with_flags._collect()
    assert collected["features"]["realtime_stt"] is False


def test_saving_does_not_reset_the_other_flags(window_with_flags):
    """A save used to overwrite every automation flag with its built-in
    default, because the boxes never loaded the stored values."""
    collected = window_with_flags._collect()
    feats = collected["features"]
    for key, stored in SAVED_FLAGS.items():
        if key in feats:
            expected = feats[key] if key == "realtime_stt" else stored
            assert feats[key] == expected, f"{key} was rewritten by SAVE"


def test_ui_tars_mode_survives_a_settings_save(monkeypatch, tmp_path):
    """The chat panel's Gemini ⇄ UI TARS switch writes features.ui_tars_mode
    itself. SAVE CHANGES rebuilds the features map from its own checkboxes, so
    it must carry the saved mode through instead of reverting to Gemini."""
    import ui_settings
    from app import config_guard as _guard

    app = QApplication.instance() or QApplication([])
    cfg = tmp_path / "api_keys.json"
    cfg.write_text(json.dumps({
        "gemini_api_key": "gm-key",
        "features": {"ui_tars_mode": True, "wake_word": False},
    }), encoding="utf-8")

    captured = {}

    def _fake_save(data, tag="save", **kwargs):
        # The real caller passes ``path=``; without **kwargs this fake raises
        # TypeError, the guarded path falls back to a plain save and the
        # assertion below then fails for the wrong reason.
        captured.update(data)
        return True, "saved", ""

    # Never let a test write the developer's real config file. ui_settings
    # reaches the guard through both module paths, so patch the object itself
    # (also reachable as core.config_guard) and then prove it.
    real_path = _guard.CONFIG_PATH
    before = real_path.read_bytes() if real_path.exists() else None
    monkeypatch.setattr(_guard, "save_with_history", _fake_save)
    import sys as _sys
    core_alias = _sys.modules.get("core.config_guard")
    if core_alias is not None and core_alias is not _guard:
        monkeypatch.setattr(core_alias, "save_with_history", _fake_save)

    win = ui_settings.SettingsWindow(config_path=cfg)
    try:
        assert win._collect()["features"]["ui_tars_mode"] is True
        win.save_settings()
        assert captured["features"]["ui_tars_mode"] is True
        after = real_path.read_bytes() if real_path.exists() else None
        assert after == before, "the test wrote the real config file"
    finally:
        win.deleteLater()


def test_narration_and_latency_controls_exist_and_save(window_with_flags):
    """The three audible behaviour switches must be reachable and persisted."""
    for key, needle in (("narrator", "Narrate"),
                        ("realtime_low_latency", "Low-latency"),
                        ("tts_streaming", "Stream")):
        cb = _flags_box(window_with_flags, key)
        assert cb is not None, f"no Settings control for {key}"
        assert needle in cb.text()
        assert cb.toolTip(), f"{key} must explain what it changes"

    cb = _flags_box(window_with_flags, "narrator")
    cb.setChecked(False)
    assert window_with_flags._collect()["features"]["narrator"] is False
    cb.setChecked(True)
    assert window_with_flags._collect()["features"]["narrator"] is True


def test_missing_flags_fall_back_to_the_documented_default(window_with_flags):
    """Absent keys must land on the same default the app documents, so the
    checkbox never contradicts what the runtime does with no configuration."""
    import ui_settings
    app = QApplication.instance() or QApplication([])
    import tempfile
    from config.settings import FEATURE_DEFAULTS
    documented = dict(ui_settings.DEFAULTS.get("features") or {})
    documented.update({k: v for k, v in FEATURE_DEFAULTS.items()
                       if k not in documented})
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "api_keys.json"
        cfg.write_text(json.dumps({"gemini_api_key": "k"}), encoding="utf-8")
        win = ui_settings.SettingsWindow(config_path=cfg)
        for key in ("realtime_stt", "wake_word", "push_to_talk"):
            cb = _flags_box(win, key)
            if cb is None:
                continue
            assert cb.isChecked() is bool(documented.get(key)), \
                f"{key}: the box defaults to {cb.isChecked()} but the app " \
                f"documents {documented.get(key)}"
        win.deleteLater()
