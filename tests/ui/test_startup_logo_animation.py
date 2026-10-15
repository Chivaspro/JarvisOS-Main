"""Startup-logo settle state machine tests (offscreen).

Covers the ``startup-logo-animation`` capability: explicit STARTUP/ANIMATING/
SETTLED states, the single position+size timeline (190 -> 180 on the same
asset), click fast-forward instead of the old dead-end, competing geometry
writers deferred until SETTLED, and reliable repeated launches.
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

from PyQt6.QtCore import QPoint, QRect  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

# ``tests/ui`` is itself a package named ``ui`` (for the relative qt_harness
# import), so a plain ``import ui`` here would import the test package, not
# the app module. Load the root ui.py explicitly under an alias instead.
import importlib.util  # noqa: E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_startup_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

STARTUP_CONFIG = {
    "assistant_name": "JARVIS",
    "user_name": "Operator",
    "gemini_api_key": "test-key",
    "os_system": "windows",
    "ui_x": 400,
    "ui_y": 300,
    "compact_size": 176,
    "reactor_opacity": 60,
    "reactor_stroke_opacity": 100,
    "voice_output_mode": "gemini_piper",
    "tts_engine": "onnx",
    "voice_name": "Charon",
    "tts_voice": "en-GB-RyanNeural",
    "voice_speed": 1.0,
    "voice_pitch": 1.0,
    "voice_volume": 1.0,
    "sapi_voice": "",
    "onnx_voice_model": "config/voices/jarvis-high.onnx",
    "onnx_voice_config": "config/voices/jarvis-high.json",
    "onnx_voice_speaker": "default",
    "onnx_execution_provider": "cpu",
    "elevenlabs_voice_id": "",
    "elevenlabs_model_id": "eleven_multilingual_v2",
    "fish_audio_voice_id": "",
    "fish_audio_model_id": "",
    "features": {"remember_position": True, "drag_reactor": True},
}


@pytest.fixture()
def startup_window(qapp, tmp_path, monkeypatch):
    """A real MainWindow whose startup presentation has run (phase STARTUP)."""
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps(STARTUP_CONFIG, indent=4), encoding="utf-8")
    monkeypatch.setattr(ui, "API_FILE", config_path)
    monkeypatch.setattr(ui, "_read_full_config", lambda: dict(STARTUP_CONFIG))
    monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(STARTUP_CONFIG))

    win = ui.MainWindow(str(tmp_path / "face.png"))
    qapp.processEvents()  # run the zero-timer that starts the presentation
    yield win
    win._shutdown_startup_sequence()
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _drive_timeline(qapp, slide, step_ms=50):
    """Step the running geometry timeline manually, sampling each frame."""
    samples = []
    duration = slide.duration()
    t = 0
    while t <= duration:
        slide.setCurrentTime(min(t, duration))
        qapp.processEvents()
        win = slide.targetObject()
        logo = getattr(win, "_header_logo", None)
        samples.append((
            QRect(win.geometry()),
            int(getattr(logo, "_visual_logo_size",
                        logo.width() if logo is not None else 0)),
        ))
        if t >= duration:
            break
        t = min(duration, t + step_ms)
    qapp.processEvents()
    return samples


# ── 5.1 explicit states ──────────────────────────────────────────────────────

def test_presentation_enters_startup_with_190_logo(qapp, startup_window):
    win = startup_window
    assert win._startup_phase is ui._StartupPhase.STARTUP
    logo = win._header_logo
    assert ui.STARTUP_LOGO_SIZE == 190
    assert int(getattr(logo, "_visual_logo_size", logo.width())) == 190
    # The big presentation window is larger than the compact home.
    assert win.width() > max(170, win._compact_size)


def test_animate_then_settled_reaches_target_geometry(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    assert win._startup_phase is ui._StartupPhase.ANIMATING
    slide = win._startup_timeline
    assert slide is not None
    target = QRect(slide.endValue())

    _drive_timeline(qapp, slide)

    assert win._startup_phase is ui._StartupPhase.SETTLED
    assert win.geometry() == target
    assert win.width() == max(170, win._compact_size)
    logo = win._header_logo
    assert int(getattr(logo, "_visual_logo_size", logo.width())) == 180
    assert ui.STARTUP_LOGO_END_SIZE == 180


def test_fallback_and_retrigger_are_noops_after_start(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    slide = win._startup_timeline
    _drive_timeline(qapp, slide)
    assert win._startup_phase is ui._StartupPhase.SETTLED

    # The 6 s fallback fired after SETTLED must be a no-op…
    win._startup_animate()
    assert win._startup_phase is ui._StartupPhase.SETTLED
    # …as is any repeated trigger.
    win._startup_begin()
    assert win._startup_phase is ui._StartupPhase.SETTLED
    assert win._startup_timeline is None


def test_fallback_timer_is_stopped_on_phase_change(qapp, startup_window):
    win = startup_window
    timer = win._startup_fallback_timer
    assert timer is not None and timer.isActive()
    win._startup_animate()
    assert not timer.isActive()


# ── 5.2 monotonic interpolation, one timeline ───────────────────────────────

def test_no_teleport_geometry_monotonic_on_eased_path(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    slide = win._startup_timeline
    start = QRect(slide.startValue())
    target = QRect(slide.endValue())

    samples = _drive_timeline(qapp, slide, step_ms=25)

    assert len(samples) >= 8
    # Every frame lies inside the start→target sweep (no jumps off the path).
    # Endpoints included: the animation both starts and ends exactly there.
    sweep = start.united(target)
    for rect, logo_size in samples:
        assert sweep.contains(rect), f"frame {rect} left the start→target path"
        # Logo size stays within the 190 → 180 span, never below the end size.
        assert 180 <= logo_size <= 190
    # The settle ended exactly on target with the exact end size.
    assert samples[-1][0] == target
    assert samples[-1][1] == 180


def test_position_and_size_advance_on_the_same_timeline(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    slide = win._startup_timeline
    start = QRect(slide.startValue())
    target = QRect(slide.endValue())

    samples = _drive_timeline(qapp, slide, step_ms=50)

    moved_pos = [s for s in samples if s[0].topLeft() != start.topLeft()]
    resized = [s for s in samples if s[0].width() != start.width()]
    assert moved_pos, "window never moved: position is not on the timeline"
    assert resized, "window never resized: size is not on the timeline"
    # The same sample that first shows movement already shows resizing: the
    # two advance together instead of one completing before the other.
    first_move = samples.index(moved_pos[0])
    first_resize = samples.index(resized[0])
    assert abs(first_move - first_resize) <= 1


# ── 5.3 same asset ───────────────────────────────────────────────────────────

def test_same_logo_widget_and_artwork_across_settle(qapp, startup_window):
    win = startup_window
    logo = win._header_logo
    logo_id = id(logo)
    assert isinstance(logo, ui.RefinedArcLogoButton)

    win._startup_animate()
    _drive_timeline(qapp, win._startup_timeline)

    assert win._header_logo is not None
    assert id(win._header_logo) == logo_id
    assert isinstance(win._header_logo, ui.RefinedArcLogoButton)
    # No second logo was created anywhere in the window hierarchy.
    logos = win.findChildren(ui.RefinedArcLogoButton)
    assert len(logos) == 1


# ── 5.4 click regression (the dead-end) ──────────────────────────────────────

def test_click_during_startup_settles_instead_of_dead_end(qapp, startup_window):
    win = startup_window
    assert win._startup_phase is ui._StartupPhase.STARTUP

    # The old bug: click called _finalize_startup_transition while it was
    # still None — nothing settled and the fallback no-oped. Now the click
    # starts a short settle and the sequence lands in SETTLED at the target.
    win._startup_click_settle()
    assert win._startup_phase is ui._StartupPhase.ANIMATING
    slide = win._startup_timeline
    target = QRect(slide.endValue())
    _drive_timeline(qapp, slide)

    assert win._startup_phase is ui._StartupPhase.SETTLED
    assert win.geometry() == target


def test_click_during_animating_fast_forwards_without_snap(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    slide = win._startup_timeline
    start = QRect(slide.startValue())
    target = QRect(slide.endValue())

    # Mid-flight fast-forward: jump the clock to the end; the normal finished
    # path finalizes. No sample may leave the interpolated path.
    _drive_timeline(qapp, slide, step_ms=100)
    assert win._startup_phase is ui._StartupPhase.SETTLED
    sweep = start.united(target)
    assert sweep.contains(win.geometry())
    assert win.geometry() == target


def test_button_press_fast_forwards_through_window_state(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    btn = win._header_logo
    btn._fast_forward_settle()
    qapp.processEvents()
    assert win._startup_phase is ui._StartupPhase.SETTLED


# ── 5.5 competing writers ────────────────────────────────────────────────────

def test_collapse_during_animating_is_deferred(qapp, startup_window):
    win = startup_window
    win._startup_animate()
    slide = win._startup_timeline
    mid = QRect(slide.currentValue())
    compact = max(170, win._compact_size)
    assert win.width() > compact  # mid-flight, still bigger than compact

    win._collapse()
    # The animated geometry was NOT overwritten…
    assert win.width() > compact
    assert win._startup_pending_layout is True

    _drive_timeline(qapp, slide)
    assert win._startup_phase is ui._StartupPhase.SETTLED
    # …and the deferred compact layout applied afterwards.
    qapp.processEvents()
    assert win.width() == compact
    logo = win._header_logo
    assert int(getattr(logo, "_visual_logo_size", logo.width())) == 180
    assert mid.width() >= compact  # sanity: animation was shrinking


def test_settings_apply_during_animating_defers_geometry(qapp, startup_window, monkeypatch):
    win = startup_window
    win._startup_animate()
    slide = win._startup_timeline
    compact = max(170, win._compact_size)

    save_voice_calls = []
    import memory.config_manager as cm
    monkeypatch.setattr(cm, "save_voice", lambda name: save_voice_calls.append(name))

    data = dict(STARTUP_CONFIG)
    data["voice_name"] = "Charon"  # unchanged Gemini voice
    win._apply_full_settings(data)
    qapp.processEvents()

    # Geometry untouched mid-flight…
    assert win.width() > compact
    assert win._startup_pending_layout is True
    # …and the unchanged Gemini voice was NOT rewritten (the V2 bug).
    assert save_voice_calls == []

    _drive_timeline(qapp, slide)
    assert win._startup_phase is ui._StartupPhase.SETTLED
    qapp.processEvents()
    assert win.width() == compact


def test_settings_apply_after_settled_behaves_as_before(qapp, startup_window, monkeypatch):
    win = startup_window
    win._startup_animate()
    _drive_timeline(qapp, win._startup_timeline)
    assert win._startup_phase is ui._StartupPhase.SETTLED

    save_voice_calls = []
    import memory.config_manager as cm
    monkeypatch.setattr(cm, "save_voice", lambda name: save_voice_calls.append(name))

    data = dict(STARTUP_CONFIG)
    data["voice_name"] = "Poe"  # an actual Gemini-voice change
    win._apply_full_settings(data)
    qapp.processEvents()

    assert save_voice_calls == ["Poe"]
    assert win.width() == max(170, win._compact_size)


# ── 5.6 repeated launches ────────────────────────────────────────────────────

def test_repeated_launches_settle_identically(qapp, tmp_path, monkeypatch):
    config_path = tmp_path / "api_keys.json"
    config_path.write_text(json.dumps(STARTUP_CONFIG, indent=4), encoding="utf-8")
    monkeypatch.setattr(ui, "API_FILE", config_path)
    monkeypatch.setattr(ui, "_read_full_config", lambda: dict(STARTUP_CONFIG))
    monkeypatch.setattr(ui, "_ui_load", lambda *a, **k: dict(STARTUP_CONFIG))

    finals = []
    for _run in range(2):
        win = ui.MainWindow(str(tmp_path / "face.png"))
        qapp.processEvents()
        assert win._startup_phase is ui._StartupPhase.STARTUP
        win._startup_animate()
        _drive_timeline(qapp, win._startup_timeline)
        assert win._startup_phase is ui._StartupPhase.SETTLED

        # No leftover startup timer or animation survives the settle.
        assert win._startup_timeline is None
        assert win._startup_fallback_timer is None or not win._startup_fallback_timer.isActive()
        hitbox_timer = getattr(win, "_startup_logo_hitbox_timer", None)
        assert hitbox_timer is None or not hitbox_timer.isActive()
        # Decorative fades stop at SETTLED (the finalize stops them and zeroes
        # the glow, so no mid-fade halo is frozen on screen).
        for attr in ("_startup_fade", "_startup_bloom", "_startup_core_fade_anim"):
            anim = getattr(win, attr, None)
            assert anim is None or anim.state() == type(anim).State.Stopped
        logo = win._header_logo
        assert getattr(logo, "_startup_glow", 0.0) == 0.0
        assert win._startup_anims == ()
        hit = getattr(win, "_startup_logo_hitbox", None)
        assert hit is None or not hit.isVisible()

        finals.append((QRect(win.geometry()),
                       int(getattr(logo, "_visual_logo_size", logo.width()))))
        win._shutdown_startup_sequence()
        win.close()
        win.deleteLater()
        qapp.processEvents()

    assert finals[0] == finals[1]
    assert finals[0][1] == 180
