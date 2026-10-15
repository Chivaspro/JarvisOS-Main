"""Behavioural regression tests for SAVE AS DEFAULT VOICE (provider-scoped).

Originally written for the bug where the button hard-coded voice_output_mode =
"local". The Voice page now has a provider selector with one panel per
provider; Save as Default persists the selected provider + voice as a
validated pair, scoped to that provider only.
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


@pytest.fixture(scope="module")
def settings_window(tmp_path_factory):
    import ui_settings
    app = QApplication.instance() or QApplication([])
    cfg = tmp_path_factory.mktemp("cfg") / "api_keys.json"
    cfg.write_text(json.dumps({
        # The real-world case: an existing install with an explicit "gemini"
        # value and an engine that is NOT onnx.
        "voice_output_mode": "gemini",
        "tts_engine": "edgetts",
        "tts_voice": "en-GB-RyanNeural",
        "voice_name": "Charon",
        "gemini_api_key": "gm-key",
        "fish_audio_api_key": "fish-key",
        "fish_audio_voice_id": "fishvoice-original",
        "elevenlabs_api_key": "el-key",
        "elevenlabs_voice_id": "pNInz6obpgDQGcFmaJgB",
        "onnx_voice_model": "config/voices/jarvis-high.onnx",
        "onnx_voice_config": "config/voices/jarvis-high.json",
    }), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=cfg)
    win._test_cfg_path = cfg
    yield win
    win.deleteLater()
    del app


def _saved(win) -> dict:
    return json.loads(win._test_cfg_path.read_text(encoding="utf-8"))


def test_voice_output_combo_offers_the_three_modes(settings_window):
    combo = settings_window.voice_output_mode
    items = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
    assert items == [("Gemini Native Audio", "gemini"),
                     ("Gemini → Piper ONNX", "gemini_piper"),
                     ("Local Piper Only", "local")]


def test_existing_gemini_config_opens_as_native_audio(settings_window):
    """The native-audio default: a pre-marker gemini config resolves to the
    realtime native pipeline (no piper lift for the new default)."""
    assert settings_window.voice_output_mode.currentData() == "gemini"


def test_execution_provider_selector_has_cpu_gpu_auto(settings_window):
    combo = settings_window.onnx_execution_provider
    items = [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]
    assert items == [("CPU", "cpu"), ("GPU", "gpu"), ("Auto", "auto")]


def test_the_bundled_piper_voice_is_offerable(settings_window):
    names = [settings_window.onnx_voice.itemData(i)["name"]
             for i in range(settings_window.onnx_voice.count())
             if settings_window.onnx_voice.itemData(i)]
    assert "jarvis-high" in names


def test_provider_selector_lists_every_provider(settings_window):
    combo = settings_window.provider_selector
    ids = [combo.itemData(i) for i in range(combo.count())]
    assert ids == ["fishAudio", "elevenLabs", "gemini", "piperOnnx", "edgeTts", "kokoro", "sapi"]


def test_only_the_selected_panel_is_visible(settings_window):
    settings_window.provider_selector.setCurrentIndex(
        settings_window.provider_selector.findData("fishAudio"))
    assert settings_window.provider_stack.currentWidget() is settings_window._provider_panels["fishAudio"]
    settings_window.provider_selector.setCurrentIndex(
        settings_window.provider_selector.findData("gemini"))
    assert settings_window.provider_stack.currentWidget() is settings_window._provider_panels["gemini"]
    # The Gemini panel shows the Live voice; Fish Audio fields are not on it.
    assert settings_window.live_voice.parent() is settings_window._provider_panels["gemini"]


def test_collect_persists_the_piper_arrangement(settings_window):
    settings_window.provider_selector.setCurrentIndex(
        settings_window.provider_selector.findData("piperOnnx"))
    settings_window.voice_output_mode.setCurrentIndex(
        settings_window.voice_output_mode.findData("gemini_piper"))
    data = settings_window._collect()
    assert data["voice_output_mode"] == "gemini_piper"
    assert data["onnx_execution_provider"] == "cpu"
    assert Path(data["onnx_voice_model"]).name == "jarvis-high.onnx"


def test_save_as_default_fish_does_not_touch_gemini_or_others(settings_window):
    """The Fish→Charon regression: a Fish Audio save must not write a Gemini
    voice anywhere, and every other provider's config must survive verbatim."""
    win = settings_window
    win.provider_selector.setCurrentIndex(win.provider_selector.findData("fishAudio"))
    win.fish_api_key.setText("fish-key-new")
    win.fish_voice_id.setText("fishvoice-2")
    before_gemini_key = _saved(win).get("gemini_api_key", "")
    win._save_provider_panel("fishAudio", as_default=True)
    saved = _saved(win)
    # Fish got its values and the default pair names Fish + Fish voice.
    assert saved["fish_audio_api_key"] == "fish-key-new"
    assert saved["fish_audio_voice_id"] == "fishvoice-2"
    assert saved["voice"]["activeProvider"] == "fishAudio"
    assert saved["voice"]["defaultProvider"] == "fishAudio"
    assert saved["voice"]["defaultVoice"] == "fishvoice-2"
    # No Gemini voice was written by this save.
    assert saved["voice_name"] == "Charon"
    assert saved["voice"]["providers"]["gemini"]["voice"] == "Charon"
    # Other providers' legacy keys are untouched.
    assert saved["elevenlabs_api_key"] == "el-key"
    assert saved["elevenlabs_voice_id"] == "pNInz6obpgDQGcFmaJgB"
    assert saved["onnx_voice_model"] == "config/voices/jarvis-high.onnx"
    assert saved.get("gemini_api_key", before_gemini_key) == before_gemini_key
    # Status reports the provider pair, not Charon.
    assert "DEFAULT SAVED" in win._status.text()
    assert "Fish Audio" in win._status.text()
    assert "fishvoice-2" in win._status.text()
    assert "Charon" not in win._status.text()


def test_save_as_default_voice_does_not_switch_to_local(settings_window):
    settings_window.provider_selector.setCurrentIndex(
        settings_window.provider_selector.findData("piperOnnx"))
    settings_window.voice_output_mode.setCurrentIndex(
        settings_window.voice_output_mode.findData("gemini_piper"))
    settings_window._save_provider_panel("piperOnnx", as_default=True)
    saved = _saved(settings_window)
    assert saved["voice_output_mode"] == "gemini_piper"
    assert saved["onnx_execution_provider"] == "cpu"
    assert saved["voice_pipeline_migrated"] is True
    # The default pair names the Piper provider and the Piper voice.
    assert saved["voice"]["defaultProvider"] == "piperOnnx"
    assert "jarvis-high" in saved["voice"]["defaultVoice"]


def test_a_deliberate_gemini_choice_is_preserved_by_saving(settings_window):
    combo = settings_window.voice_output_mode
    combo.setCurrentIndex(combo.findData("gemini"))
    settings_window.provider_selector.setCurrentIndex(
        settings_window.provider_selector.findData("gemini"))
    settings_window._save_provider_panel("gemini", as_default=True)
    saved = _saved(settings_window)
    assert saved["voice_output_mode"] == "gemini"
    assert saved["voice"]["defaultProvider"] == "gemini"
    assert saved["voice"]["defaultVoice"] in {"Charon", "Puck", "Kore", "Fenrir", "Aoede"}
    # Restore the Piper arrangement for any test that runs after this one.
    combo.setCurrentIndex(combo.findData("gemini_piper"))
    settings_window._save_provider_panel("piperOnnx", as_default=True)


def test_switching_providers_does_not_write_config(settings_window):
    win = settings_window
    before = win._test_cfg_path.read_text(encoding="utf-8")
    for pid in ("fishAudio", "elevenLabs", "gemini", "piperOnnx", "edgeTts", "fishAudio"):
        win.provider_selector.setCurrentIndex(win.provider_selector.findData(pid))
    assert win._test_cfg_path.read_text(encoding="utf-8") == before


def test_invalid_provider_save_is_rejected_without_side_effects(settings_window):
    win = settings_window
    win.provider_selector.setCurrentIndex(win.provider_selector.findData("fishAudio"))
    win.fish_api_key.setText("")            # invalid: missing key
    before = win._test_cfg_path.read_text(encoding="utf-8")
    win._save_provider_panel("fishAudio", as_default=False)
    assert win._test_cfg_path.read_text(encoding="utf-8") == before
    assert "NOT SAVED" in win._status.text()


def test_pipeline_save_persists_choice_and_syncs_renderer(settings_window):
    """SAVE on the pipeline combo persists the selected pipeline and syncs the
    Gemini renderer subtree so the runtime actually follows the combo."""
    win = settings_window
    win.voice_output_mode.setCurrentIndex(win.voice_output_mode.findData("gemini_piper"))
    win._save_pipeline()
    saved = _saved(win)
    assert saved["voice_output_mode"] == "gemini_piper"
    assert saved["voice"]["geminiRenderer"] == "piper"
    # A deliberate save records both one-time lift markers.
    assert saved["voice_pipeline_migrated"] is True
    assert saved["voice_piper_to_native_lifted"] is True


def test_pipeline_save_as_default_records_the_startup_default(settings_window):
    win = settings_window
    win.voice_output_mode.setCurrentIndex(win.voice_output_mode.findData("gemini"))
    win._save_pipeline(as_default=True)
    saved = _saved(win)
    assert saved["voice_output_mode"] == "gemini"
    assert saved["voice"]["geminiRenderer"] == "native"
    assert saved["voice"]["defaultPipeline"] == "gemini"
    assert "DEFAULT SAVED" in win._status.text()


def test_load_honors_the_recorded_default_pipeline(settings_window, tmp_path):
    import ui_settings
    cfg = tmp_path / "default_pipeline.json"
    cfg.write_text(json.dumps({"voice": {"defaultPipeline": "local"}}), encoding="utf-8")
    assert ui_settings.load(cfg)["voice_output_mode"] == "local"
    # Without a recorded default, the DEFAULTS constant applies (native audio).
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    assert ui_settings.load(empty)["voice_output_mode"] == "gemini"


def test_command_window_reopens_on_the_chat_surface():
    """Circle-logo contract: opening the command window lands on the chat log.

    The CHAT *navigation tab* was removed from the design — the chat panel is
    simply the main surface — so the contract is now "focus the chat input and
    leave the tab strip alone". Asserting a checked tab here would be asserting
    chrome that no longer exists, and the base/override pair used to disagree
    about exactly this.
    """
    # Load the root ui.py explicitly — ``import ui`` here resolves to the
    # tests/ui package once that suite has been imported.
    import importlib.util  # noqa: E402
    _ui_spec = importlib.util.spec_from_file_location(
        "ui_impl_chat_tab_tests", ROOT / "ui.py")
    ui_mod = importlib.util.module_from_spec(_ui_spec)
    _ui_spec.loader.exec_module(ui_mod)

    class _FakeBtn:
        def __init__(self):
            self.checked = None

        def setChecked(self, v):
            self.checked = v

    class _FakeInput:
        def __init__(self):
            self.focused = False

        def setFocus(self):
            self.focused = True

    class _FakeWin:
        def __init__(self):
            self._tab_btns = [_FakeBtn() for _ in range(7)]
            self.input = _FakeInput()

    for cls in (ui_mod._CommandWindow, ui_mod.RefinedCommandWindow):
        win = _FakeWin()
        cls._show_chat_tab(win)
        assert win.input.focused is True, f"{cls.__name__} must focus the chat input"
        assert all(b.checked is None for b in win._tab_btns), \
            f"{cls.__name__} still drives a tab strip that no longer exists"

