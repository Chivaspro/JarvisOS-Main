"""J.A.R.V.I.S. panel control must open the panel it claims to open.

Regression cover for the "it said it opened Settings/Vision but nothing
appeared" bug. ``control_jarvis_window`` is the single entry point behind the
brain's ``jarvis_window`` tool, so these tests drive it exactly as a spoken
"open Settings" / "open Vision" ends up driving it, and then look at the real
Qt widgets instead of trusting the returned sentence.

The Vision case is the one that was genuinely broken: the window aliases
resolved "vision" to the internal name ``live_camera``, which had no entry in
the ``panels`` map, so the membership guard swallowed the request and answered
"Unknown JARVIS window. Available: Settings, Vision, …" — naming Vision as
available while refusing to open it. The assistant then reported it as done.

The window fixture comes from ``test_image_view_panel`` (a real MainWindow with
a fake camera and the layout file redirected to a temp directory), and the
module-level ``tests/ui/conftest.py`` keeps the window's production
close-means-exit path from killing the pytest process.
"""
from __future__ import annotations

import pytest

from .test_camera_pipeline import (  # noqa: F401
    FakeCameraManager, _NullAnalyzer, _pump, camera, qapp, ui,
)
from .test_image_view_panel import window  # noqa: F401

#: what the user says -> the internal panel the alias must resolve to
VISION_ALIASES = ["vision", "vision panel", "vision_panel", "camera", "webcam"]


@pytest.fixture(autouse=True)
def _quiet_sfx(monkeypatch):
    """Keep the native libraries these tests do not cover out of the way.

    * Opening a panel plays a UI sound. There is no audio device behind an
      offscreen run, and PortAudio dies with a native access violation instead
      of raising.
    * Opening Vision arms the analysis worker, which pulls in MediaPipe/TFLite;
      this environment access-violates inside that library's teardown. These
      tests cover which panel opens, not the analyser, so the worker is stubbed
      at the class exactly as the camera pipeline tests do.
    """
    monkeypatch.setattr(ui, "_sfx", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(ui.RefinedCommandWindow, "_ensure_vision_analyzer",
                        lambda self: _NullAnalyzer())


# ── Vision ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spoken", VISION_ALIASES)
def test_open_vision_really_opens_the_inline_stage(window, qapp, spoken):  # noqa: F811
    reply = window.control_jarvis_window(spoken, "open")
    qapp.processEvents()

    host = window._command_window
    assert host is not None, f"{spoken!r} must bring up the Chat Panel host"
    assert host.isVisible(), f"{spoken!r} answered {reply!r} with no visible host"
    assert window._chat_vision_inline is True, (
        f"{spoken!r} answered {reply!r} but the inline Vision flag is not set")
    assert "Opened the Vision panel" in reply


def test_open_vision_is_never_answered_with_the_unknown_window_error(window, qapp):  # noqa: F811
    reply = window.control_jarvis_window("vision", "open")
    assert "Unknown JARVIS window" not in reply


def test_the_opening_reply_names_the_real_outcome(window, qapp, monkeypatch):  # noqa: F811
    """If the stage cannot come up, JARVIS must say so instead of claiming success."""
    monkeypatch.setattr(window, "open_inline_vision_camera", lambda: False)
    reply = window.control_jarvis_window("vision", "open")
    assert "could not open the Vision panel" in reply
    assert "Opened the Vision panel" not in reply


# ── Settings ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spoken", ["settings", "jarvis settings", "ui settings",
                                    "control center", "preferences"])
def test_open_settings_really_opens_and_is_visible(window, qapp, spoken):  # noqa: F811
    reply = window.control_jarvis_window(spoken, "open")
    qapp.processEvents()

    swin = window._full_settings_window
    assert swin is not None, f"{spoken!r} answered {reply!r} with no Settings window"
    assert swin.isVisible(), f"{spoken!r} answered {reply!r} but nothing is on screen"
    assert reply == "Opened the JARVIS Settings window."


def test_settings_opens_and_closes_repeatedly(window, qapp):  # noqa: F811
    """Repeated open/close must stay honest, reuse one window and never wedge."""
    for _ in range(3):
        opened = window.control_jarvis_window("settings", "open")
        qapp.processEvents()
        assert opened == "Opened the JARVIS Settings window."
        first = window._full_settings_window
        assert first.isVisible()

        closed = window.control_jarvis_window("settings", "close")
        qapp.processEvents()
        assert closed == "Closed the JARVIS Settings window."
        assert not first.isVisible()
        assert window._full_settings_window is first, "a stale copy was created"


def test_settings_close_reports_the_real_state(window, qapp):  # noqa: F811
    """Closing something that is not open must not be reported as success."""
    assert "not open" in window.control_jarvis_window("settings", "close")


# ── unknown windows stay rejected ────────────────────────────────────────────

def test_unknown_window_is_still_refused(window):  # noqa: F811
    assert "Unknown JARVIS window" in window.control_jarvis_window(
        "definitely_not_a_panel", "open")
