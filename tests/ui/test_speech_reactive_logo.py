"""Speech-reactive logo scaling (offscreen).

JARVIS's two logos — the startup reactor (``MainWindow._header_logo``) and the
Chat Panel logo (``RefinedCommandWindow.globe``) — must swell smoothly while he
speaks and ease back to their resting size when he goes quiet, driven by ONE
shared AudioReactive speech-energy signal. The scale is painted independently of
the rotation sweeps and the startup timeline, so talking and animating never
fight (spec: audio-reactive logo subsystem).
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

from PyQt6.QtWidgets import QApplication  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402
from core import audio_reactive  # noqa: E402

# ``tests/ui`` is itself a package named ``ui``, so load the root ui.py
# explicitly under an alias (same pattern as test_startup_logo_animation).
import importlib.util  # noqa: E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_speech_logo_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

LOGO_CONFIG = {
    "assistant_name": "JARVIS",
    "user_name": "Operator",
    "gemini_api_key": "test-key",
    "os_system": "windows",
    "ui_x": 400,
    "ui_y": 300,
    "compact_size": 176,
    "reactor_opacity": 60,
    "voice_output_mode": "gemini_piper",
    "tts_engine": "onnx",
    "features": {},
}


def _ease(logo, ticks: int = 40) -> float:
    """Drive the logo's own smoothing loop manually (no wall-clock waits)."""
    for _ in range(ticks):
        logo._tick()
    return float(logo._speech_scale)


# ── The live logo class (ui._ArcLogo is rebound to RefinedArcLogo) ──────────

def test_arc_logo_eases_up_when_speaking(qapp):
    logo = ui._ArcLogo(size=200)
    assert logo._speech_scale == 1.0          # resting size at idle
    logo.set_speech_energy(1.0)
    assert logo._speech_energy == 1.0
    before = logo._speech_scale
    after = _ease(logo, ticks=3)
    assert after > before, "scale must grow while energy is high"
    target = 1.0 + ui.LOGO_SPEECH_SCALE
    assert after < target, "the attack must be gradual, never a jump-cut"
    assert _ease(logo, ticks=60) == pytest.approx(target, abs=1e-3)


def test_arc_logo_settles_back_to_resting_size(qapp):
    logo = ui._ArcLogo(size=200)
    logo.set_speech_energy(1.0)
    _ease(logo, ticks=60)
    assert logo._speech_scale > 1.02
    logo.set_speech_energy(0.0)               # JARVIS stopped talking
    final = _ease(logo, ticks=80)
    assert final == pytest.approx(1.0, abs=1e-3), "idle must return to normal size"


def test_arc_logo_scale_is_monotonic_up_and_down(qapp):
    logo = ui._ArcLogo(size=200)
    logo.set_speech_energy(1.0)
    rising = [logo._speech_scale]
    for _ in range(30):
        logo._tick()
        rising.append(logo._speech_scale)
    assert all(b >= a for a, b in zip(rising, rising[1:])), "attack never dips"
    logo.set_speech_energy(0.0)
    falling = [logo._speech_scale]
    for _ in range(30):
        logo._tick()
        falling.append(logo._speech_scale)
    assert all(b <= a for a, b in zip(falling, falling[1:])), "release never overshoots"


def test_arc_logo_energy_is_clamped_and_sane(qapp):
    logo = ui._ArcLogo(size=200)
    logo.set_speech_energy(2.5)               # over-range pushes clamp
    assert logo._speech_energy == 1.0
    logo.set_speech_energy(-1.0)
    assert logo._speech_energy == 0.0
    logo.set_speech_energy("bogus")           # garbage never poisons the state
    assert logo._speech_energy == 0.0


def test_speech_scale_does_not_touch_rotation_or_glow(qapp):
    """Talking must not disturb the mic glow or the state-blend animation — the
    live logo derives rotation from time and paints speech size separately."""
    logo = ui.RefinedArcLogo(size=200)
    logo.set_speech_energy(1.0)
    glow_before = (float(logo._live_amp), float(logo._amp_display))
    blend_before = {k: float(logo._blend[k]) for k in ('energy', 'glow', 'sweep')}
    for _ in range(5):
        logo._tick()
    assert logo._speech_scale > 1.0
    glow_after = (float(logo._live_amp), float(logo._amp_display))
    assert glow_after == glow_before, "speech energy must not move the mic glow"
    blend_after = {k: float(logo._blend[k]) for k in ('energy', 'glow', 'sweep')}
    assert blend_after == blend_before, "speech energy must not retarget the state blend"
    # Activation swell lives on a separate term of the same expansion,
    # so a click during speech adds to the scale instead of fighting it.
    logo.trigger_activation()
    assert logo._activation_at > -1.0


# ── Same contract through the explicit RefinedArcLogo name ──────────────

def test_refined_logo_scales_with_speech(qapp):
    logo = ui.RefinedArcLogo(size=200)
    logo.set_speech_energy(1.0)
    assert _ease(logo, ticks=60) == pytest.approx(1.0 + ui.LOGO_SPEECH_SCALE, abs=1e-3)
    logo.set_speech_energy(0.0)
    assert _ease(logo, ticks=80) == pytest.approx(1.0, abs=1e-3)


# ── Lockstep by construction: the ONE shared value feeds both logos equally ──

def test_both_logos_read_the_same_shared_energy(qapp):
    """The reactor and the Chat Panel globe are the same widget class fed one
    value, so identical input must produce identical size response."""
    reactor = ui.RefinedArcLogo(size=200)
    globe = ui.RefinedArcLogo(size=190)
    for energy in (0.3, 0.7, 1.0):
        reactor.set_speech_energy(energy)
        globe.set_speech_energy(energy)
        for _ in range(40):
            reactor._tick(); globe._tick()
        assert reactor._speech_energy == globe._speech_energy == energy
        assert reactor._speech_scale == pytest.approx(globe._speech_scale, abs=1e-6), \
            "the two logos must move together"
    # Release together returns both to resting size.
    for logo in (reactor, globe):
        logo.set_speech_energy(0.0)
    for _ in range(80):
        reactor._tick(); globe._tick()
    assert reactor._speech_scale == pytest.approx(1.0, abs=1e-3)
    assert globe._speech_scale == pytest.approx(1.0, abs=1e-3)


# ── MainWindow wiring: one controller installed, fan-out subscribed ──────────
# A single MainWindow only — the offscreen suite access-violates once several
# full HUD windows accumulate in one session (documented PyQt6 offscreen pitfall).

@pytest.fixture()
def main_win(qapp, tmp_path, monkeypatch):
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps(LOGO_CONFIG, indent=4), encoding="utf-8")
    monkeypatch.setattr(ui, "API_FILE", config_path)
    monkeypatch.setattr(ui, "_read_full_config", lambda: dict(LOGO_CONFIG))
    monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(LOGO_CONFIG))

    win = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()
    yield win
    win._shutdown_startup_sequence()
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_ui_installs_one_controller_and_fans_out_to_the_reactor(qapp, main_win):
    win = main_win
    controller = audio_reactive.controller()
    assert controller is win._reactive, "the UI installs exactly one controller"
    assert win._apply_speech_energy in controller._subscribers, \
        "the fan-out callback must be subscribed to the shared signal"

    # Speaking: push the real-envelope path and let the shared controller feed
    # the startup reactor through the subscription (no Chat Panel opened here).
    reactor = win._header_logo
    for _ in range(25):
        controller.push(0.9)
        controller._tick()
        win._apply_speech_energy(controller.value())
        reactor._tick()
    assert reactor._speech_energy > 0.5
    assert reactor._speech_scale > 1.0, "the reactor grows while JARVIS speaks"

    # Silence: the controller decays to zero and the reactor eases back to size.
    for _ in range(120):
        controller.push(0.0)
        controller._tick()
        win._apply_speech_energy(controller.value())
        reactor._tick()
    assert controller.value() == 0.0
    assert reactor._speech_scale == pytest.approx(1.0, abs=1e-3)
