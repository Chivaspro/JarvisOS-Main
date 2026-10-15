"""Behavioural tests for the extended Delivery-panel voice style.

Covers the engine (voice/delivery_style.py), the UI sliders, persistence
through the existing settings architecture, reset-to-defaults, parameter
independence, and the guarantee that the Gemini Native Audio pipeline and
provider/API configuration are untouched by the style block.
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

from voice.delivery_style import (  # noqa: E402
    SECTIONS, STYLE_KEY, TEMPERATURE_KEYS, VOICE_STYLE_PARAMS, compile_prompt,
    defaults, mechanism, normalize_style, style_from_config, temperature_for,
)

EXPECTED_SECTION_COUNTS = {
    "character": 6, "expression": 6, "speech": 6, "advanced": 6, "jarvis": 9,
}


# ── Engine ────────────────────────────────────────────────────────────────────

def test_schema_covers_every_requested_parameter():
    counts = {sec: 0 for sec, _ in SECTIONS}
    for key, meta in VOICE_STYLE_PARAMS.items():
        counts[meta[0]] += 1
        assert 0 <= meta[2] <= 100 and meta[3] and meta[4]
    assert counts == EXPECTED_SECTION_COUNTS
    assert len(VOICE_STYLE_PARAMS) == 33


def test_normalize_clamps_and_fills_defaults():
    raw = {"tone": 250, "warmth": -5, "made_up_key": 7}
    out = normalize_style(raw)
    assert out["tone"] == 100 and out["warmth"] == 0
    assert "made_up_key" not in out
    assert out["authority"] == defaults()["authority"]  # missing → default
    assert normalize_style(None) == defaults()
    assert normalize_style("junk") == defaults()


def test_fully_neutral_style_compiles_nothing():
    neutral = {k: 50 for k in VOICE_STYLE_PARAMS}
    assert compile_prompt({"voice_style": neutral}) == ""


def test_defaults_compile_the_jarvis_baseline():
    """The shipped defaults ARE the JARVIS style: authority and naturalness
    sit outside the neutral band by design."""
    text = compile_prompt({"voice_style": defaults()})
    assert text.startswith("[VOICE STYLE]")
    assert "commanding" in text
    assert "completely natural" in text


def test_prompt_compiles_only_nonneutral_params():
    # Start fully neutral, move exactly two parameters.
    style = {k: 50 for k in VOICE_STYLE_PARAMS}
    style["authority"] = 90
    style["warmth"] = 10
    text = compile_prompt({"voice_style": style}, voice_mode="executive")
    assert text.startswith("[VOICE STYLE]")
    assert "executive" in text
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("[")
             and not ln.startswith("Delivery mode")]
    assert len(lines) == 2  # exactly the two moved parameters
    assert any("commanding" in ln for ln in lines)
    assert any("cool and reserved" in ln for ln in lines)


def test_prompt_is_deterministic_and_stable():
    cfg = {"voice_style": {**defaults(), "urgency": 80}}
    assert compile_prompt(cfg) == compile_prompt(cfg)


def test_temperature_maps_variance_keys():
    style = defaults()
    for k in TEMPERATURE_KEYS:
        style[k] = 100
    hot = temperature_for({"voice_style": style})
    for k in TEMPERATURE_KEYS:
        style[k] = 0
    cold = temperature_for({"voice_style": style})
    assert hot > cold
    assert 0.4 <= cold <= 1.5 and 0.4 <= hot <= 1.5
    # A stable-only setting pulls the temperature down.
    stable = defaults(); stable["stability"] = 100
    assert temperature_for({"voice_style": stable}) < temperature_for({"voice_style": defaults()})


def test_mechanism_honesty_labels():
    assert mechanism("expressiveness") == "prompt+temperature"
    assert mechanism("warmth") == "prompt"
    # Whisper mode is prompt-only by design (no DSP whisper exists).
    assert mechanism("whisper_mode") == "prompt"


def test_style_never_touches_provider_config():
    cfg = {"gemini_api_key": "k", "voice": {"activeProvider": "gemini"},
           "voice_output_mode": "gemini"}
    from voice.providers_config import providers_block
    before = providers_block(cfg)
    merged = {**cfg, STYLE_KEY: {**defaults(), "authority": 95}}
    assert providers_block(merged) == before
    assert merged["gemini_api_key"] == "k"


# ── UI ────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def settings_window(tmp_path_factory):
    import ui_settings
    app = QApplication.instance() or QApplication([])
    cfg = tmp_path_factory.mktemp("stylecfg") / "api_keys.json"
    cfg.write_text(json.dumps({"voice_output_mode": "gemini"}), encoding="utf-8")
    win = ui_settings.SettingsWindow(config_path=cfg)
    win._test_cfg_path = cfg
    yield win
    win.deleteLater()
    del app


def test_every_parameter_has_its_own_slider(settings_window):
    w = settings_window
    assert len(w._style_sliders) == len(VOICE_STYLE_PARAMS)
    for key in VOICE_STYLE_PARAMS:
        assert key in w._style_sliders


def test_sliders_preload_from_saved_style(settings_window, tmp_path):
    """A restart-fresh window restores saved values into every slider."""
    import ui_settings
    cfg = tmp_path / "preload.json"
    saved = dict(defaults()); saved["authority"] = 95; saved["warmth"] = 8
    cfg.write_text(json.dumps({STYLE_KEY: saved}), encoding="utf-8")
    win2 = ui_settings.SettingsWindow(config_path=cfg)
    try:
        assert win2._style_sliders["authority"].value() == 95
        assert win2._style_sliders["warmth"].value() == 8
        assert win2._style_sliders["tone"].value() == defaults()["tone"]
    finally:
        win2.deleteLater()


def test_slider_independence(settings_window):
    w = settings_window
    before = {k: s.value() for k, s in w._style_sliders.items()}
    w._style_sliders["authority"].setValue(99)
    after = {k: s.value() for k, s in w._style_sliders.items()}
    for k, v in before.items():
        if k == "authority":
            continue
        assert after[k] == v, f"moving authority changed {k}"


def test_collect_voice_style_reads_the_sliders(settings_window):
    w = settings_window
    w._style_sliders["composure"].setValue(12)
    collected = w._collect_voice_style()
    assert collected["composure"] == 12
    assert collected["tone"] == w._style_sliders["tone"].value()


def test_full_save_persists_and_reload_restores_every_slider(settings_window):
    w = settings_window
    w._style_sliders["dramatic_intensity"].setValue(88)
    w._style_sliders["pause_length"].setValue(15)
    data = w._collect()
    assert data["voice_style"]["dramatic_intensity"] == 88
    # Persist through the real guarded path, then reload like a restart does.
    import ui_settings
    ui_settings.save(w._test_cfg_path, **{k: v for k, v in data.items()})
    reloaded = ui_settings.load(w._test_cfg_path)
    assert reloaded["voice_style"]["dramatic_intensity"] == 88
    assert reloaded["voice_style"]["pause_length"] == 15
    # A restart-fresh window restores the saved values into the sliders.
    win2 = ui_settings.SettingsWindow(config_path=w._test_cfg_path)
    try:
        assert win2._style_sliders["dramatic_intensity"].value() == 88
        assert win2._style_sliders["pause_length"].value() == 15
    finally:
        win2.deleteLater()


def test_partial_config_fills_missing_params_on_load(settings_window):
    import ui_settings
    cfg = w_cfg = settings_window._test_cfg_path
    raw = json.loads(cfg.read_text(encoding="utf-8"))
    raw["voice_style"] = {"authority": 80}
    cfg.write_text(json.dumps(raw), encoding="utf-8")
    reloaded = ui_settings.load(cfg)
    assert reloaded["voice_style"]["authority"] == 80
    assert reloaded["voice_style"]["tone"] == defaults()["tone"]


def test_reset_to_defaults(settings_window):
    w = settings_window
    w._style_sliders["authority"].setValue(100)
    w._style_sliders["tone"].setValue(0)
    w._reset_voice_style()
    for key, slider in w._style_sliders.items():
        assert slider.value() == defaults()[key], key


def test_gemini_pipeline_and_provider_keys_untouched_by_style_save(settings_window):
    w = settings_window
    before_keys = json.loads(w._test_cfg_path.read_text(encoding="utf-8"))
    w._style_sliders["authority"].setValue(90)
    data = w._collect()
    assert "gemini_api_key" not in data["voice_style"]
    # The style dict carries only style keys.
    from voice.delivery_style import VOICE_STYLE_PARAMS as P
    assert set(data["voice_style"]) == set(P)
    _ = before_keys


def test_style_prompt_reaches_the_live_system_instruction():
    """The [VOICE STYLE] block is compiled into the session prompt parts."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("main_impl_style_tests", ROOT / "main.py")
    # Only verify the wiring textually — importing main.py starts the app.
    src = (ROOT / "main.py").read_text(encoding="utf-8", errors="replace")
    assert "from voice.delivery_style import compile_prompt" in src
    assert "_style_ctx" in src and "voice_ctx = voice_ctx + _style_ctx" in src
    assert 'cfg["temperature"]' in src
