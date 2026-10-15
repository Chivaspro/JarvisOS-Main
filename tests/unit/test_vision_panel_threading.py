"""Vision-panel thread-affinity regression (no Qt needed).

Root cause of the "what_am_i_holding never opens the Vision Panel" bug: the
vision-tool dispatcher runs on the asyncio BACKEND thread and called
``MainWindow.open_inline_vision_camera()`` directly, but that method drives live
widgets (``win.show(); win.raise_(); activateWindow()`` and the VISION toggle).
A cross-thread ``show()`` is silently ignored by Qt, so the inline stage never
appeared — while ``jarvis_window`` worked because it marshals via ``call_on_ui``.

The fix makes ``open_inline_vision_camera`` marshal itself onto the GUI thread
and keeps every widget operation inside the GUI-thread body. These tests pin that
structure so the raw-cross-thread call can never come back.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

UI = (ROOT / "ui.py").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def _public_body():
    start = UI.index("    def open_inline_vision_camera(self) -> bool:")
    end = UI.index("    def _open_inline_vision_camera_on_ui(self) -> bool:")
    return UI[start:end]


def _worker_body():
    start = UI.index("    def _open_inline_vision_camera_on_ui(self) -> bool:")
    # The next top-level method after the worker.
    end = UI.index("    # ── Image View", start)
    return UI[start:end]


def test_public_method_marshals_to_the_gui_thread():
    body = _public_body()
    assert "self.call_on_ui(self._open_inline_vision_camera_on_ui)" in body, \
        "open_inline_vision_camera must marshal its body onto the GUI thread"


def test_widget_calls_live_only_in_the_gui_thread_body():
    # The dangerous cross-thread calls must NOT sit in the public wrapper...
    public = _public_body()
    for widget_call in ("win.show()", "win.raise_()", "activateWindow()",
                        "_on_vision_toggled(True)", "_ensure_command_window_for_inline_view"):
        assert widget_call not in public, \
            f"{widget_call!r} must be inside the marshalled body, not the wrapper"
    # ...they belong to the GUI-thread worker.
    worker = _worker_body()
    assert "win.show(); win.raise_(); win.activateWindow()" in worker
    assert "_on_vision_toggled(True)" in worker


def test_vision_dispatcher_relies_on_the_marshalling():
    # The dispatcher calls the method directly on the backend thread (it is NOT
    # wrapped in call_on_ui here) — which is exactly why the method must marshal
    # itself. If someone later wraps the dispatcher too, this still stays true.
    start = MAIN.index('elif name in ("vision_look", "recognize_faces", "what_am_i_holding"')
    block = MAIN[start:start + 2200]
    assert "self.ui.open_inline_vision_camera()" in block
    # It must not be the raw start_camera_stream() first — the inline stage wins,
    # and the floating stream is only the fallback.
    arm = block.index("open_inline_vision_camera")
    fallback = block.index("start_camera_stream")
    assert arm < fallback, "inline Vision is the primary path; the stream is the fallback"


def test_vision_panel_arm_failure_is_logged_not_swallowed():
    # The dispatcher used to end in a bare `except Exception: pass`, hiding every
    # reason the panel did not open. It must now leave a trace.
    start = MAIN.index("if not self.ui.open_inline_vision_camera():")
    window = MAIN[start:start + 700]
    assert "Vision panel arm" in window, "a failed panel arm must be written to the log"
