"""The HUD mic waveform: cheap while idle, immediately live on real audio.

The widget repaints at ~60 fps while the microphone is active, and it is on
screen whenever the chat panel is open. It used to keep that 60 fps repaint
running forever even with a flat, silent trace — constant paint churn for a
static line, on top of everything else the HUD does. It now settles to a slow
tick with no repaints, and snaps back to full rate on the next tick that sees
real audio.

``paintEvent`` is also wrapped: an exception escaping a Qt virtual method aborts
the whole process in PyQt, so a bad frame must stay contained to that frame.
"""
from __future__ import annotations

import pytest

from .test_camera_pipeline import qapp, ui  # noqa: F401  (fixtures + ui alias)


def _settle(wave) -> None:
    wave._env = 0.0
    wave._active = 0.0
    wave._meter = 0.0
    wave._peak = 0.0
    wave._pending = 0.0


def test_a_settled_trace_drops_to_the_idle_rate(qapp):
    wave = ui._MiniWaveform()
    try:
        _settle(wave)
        wave._step()
        assert wave._tmr.interval() == wave._IDLE_MS
    finally:
        wave.deleteLater()
        qapp.processEvents()


def test_real_audio_restores_the_full_rate_on_the_next_tick(qapp):
    wave = ui._MiniWaveform()
    try:
        _settle(wave)
        wave._step()
        assert wave._tmr.interval() == wave._IDLE_MS

        wave.set_audio_level(0.9)
        wave._step()
        assert wave._tmr.interval() == wave._FPS_MS
    finally:
        wave.deleteLater()
        qapp.processEvents()


def test_a_paint_error_is_contained_and_never_kills_the_widget(qapp):
    wave = ui._MiniWaveform()
    try:
        def _boom():
            raise RuntimeError("bad frame")

        wave._paint = _boom
        wave.paintEvent(None)          # must not raise out of Qt's virtual
    finally:
        wave.deleteLater()
        qapp.processEvents()


def test_a_missing_history_still_paints(qapp):
    """n < 2 used to divide by zero inside paintEvent."""
    wave = ui._MiniWaveform()
    try:
        wave._hist = [0.0]
        wave._paint()                  # must return cleanly
        pix = wave.grab()
        assert not pix.isNull()
    finally:
        wave.deleteLater()
        qapp.processEvents()
