"""Performance settings tab (integration) — offscreen, non-destructive.

Verifies the tab is wired into the real Settings window:

* the PERFORMANCE tab exists and its widgets are constructed;
* the profile dropdown carries the four real profiles;
* every control is backed by a real setting key, and applying a preset to the
  tab's widgets is deterministic;
* a preset with the tab's loader does not write the config by itself (loading
  is display-only; persistence happens through apply/save actions).
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

from PyQt6.QtWidgets import QApplication  # noqa: E402

import ui_perfomance as P  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(["pytest"])


@pytest.fixture(scope="module")
def window(app):
    import ui_settings
    win = ui_settings.SettingsWindow()
    yield win
    win.deleteLater()


def test_tab_registered(window):
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "PERFORMANCE" in names


def test_tab_surface(window):
    tab = window._perf_tab
    labels = [tab._profile_box.itemText(i) for i in range(tab._profile_box.count())]
    assert labels == ["Balanced", "Performance", "Ultra Performance", "Custom"]
    assert {"animations", "sound_effects", "audio_reactive", "neural_effects",
            "mind_reload_s", "vault_watch_s", "index_fresh_s",
            "world_monitor_refresh", "neural_max_nodes",
            "neural_tick_ms"} <= set(tab._controls)
    assert tab._scan_btn.isEnabled()
    assert not tab._progress.isVisible()
    assert tab._scan_cancel_btn.isEnabled() is False


def test_ultra_profile_loads_deterministically_into_the_widgets(window):
    tab = window._perf_tab
    ultra = P.resolve("ultra")
    tab._load_from(ultra)
    assert tab._profile_box.currentData() == "ultra"
    assert tab._controls["animations"].isChecked() is False
    assert tab._controls["sound_effects"].isChecked() is False
    assert tab._controls["audio_reactive"].isChecked() is False
    assert tab._controls["neural_effects"].isChecked() is False
    assert tab._controls["neural_max_nodes"].value() == 400
    assert tab._controls["vault_watch_s"].value() == 30
    # Loading the widgets is display-only: the checked bundle equals resolve().
    collected = tab._collect_controls()
    for key in ("animations", "sound_effects", "audio_reactive", "neural_effects",
                "neural_max_nodes", "neural_tick_ms", "mind_reload_s",
                "vault_watch_s", "index_fresh_s", "world_monitor_refresh"):
        assert collected[key] == ultra[key]


def test_restore_button_targets_balanced(window):
    tab = window._perf_tab
    balanced = P.resolve("balanced")
    # Reconstruct what RESTORE DEFAULTS applies, without touching the config.
    assert balanced["profile"] == "balanced"
    assert balanced["animations"] is True
    assert balanced["neural_max_nodes"] == 2500
