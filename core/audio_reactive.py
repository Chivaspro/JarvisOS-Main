"""Shared audio-reactive controller — one authoritative speech-energy signal.

J.A.R.V.I.S. has two logos that should breathe with its OWN voice: the compact
startup/main reactor (``MainWindow._header_logo``) and the ReferenceDashboard
logo inside the Chat Panel (``RefinedCommandWindow.globe``). Previously each did
its own smoothing from whatever called ``set_audio_level``, so they drifted out
of step, ran even when silent, and the local/Piper voice path never fed them at
all.

This module fixes that with a single controller that:

* is fed the REAL playback amplitude (0.0-1.0) from the audio pipeline — from
  any thread (the Gemini receive loop and the local-TTS worker are not the GUI
  thread);
* smooths it with fast-attack / slow-release so the motion reads like a voice
  visualiser, never a jitter of raw samples;
* is the ONE source both logos read, so they move together by construction;
* is effectively IDLE when there is no speech: the repaint timer stops once the
  envelope has settled back to zero, so there is no fake pulse while silent and
  no CPU cost at rest;
* owns exactly ONE timer, regardless of how many times the Chat Panel opens or
  closes — subscribers register/unregister and can never spawn a second loop.

The controller does no painting and imports no Qt widgets, so it is trivially
unit-testable offscreen.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class AudioReactive(QObject):
    """A single smoothed speech-energy value broadcast to many subscribers.

    ``push(level)`` may be called from ANY thread; ``subscribe``/``unsubscribe``
    and the value callbacks run on the GUI thread (the controller's timer).
    """

    # Cross-thread wake: emitting from an audio worker marshals onto the GUI
    # thread (Qt auto/queued connection) so the repaint timer is only ever
    # started by the thread that owns it — no singleShot overload pitfalls.
    _wake = pyqtSignal()

    # Tuned for a subtle, futuristic breath — small scale delta, quick attack,
    # gentle release. Values are chosen against the existing logo scale terms
    # (``Rs = 1 + amp*0.07`` etc.), not arbitrary big numbers.
    _ATTACK = 0.55       # toward a louder signal, per GUI tick
    _RELEASE = 0.12      # back toward zero (slower), per GUI tick
    _IDLE_EPS = 0.002    # below this the envelope is "silent"
    _HOLD_TICKS = 6      # ticks to wait after signal stops before going idle
    _TICK_MS = 33          # ~30 fps — smooth but cheap

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._incoming = 0.0        # newest raw push from the audio side
        self._value = 0.0           # smoothed envelope shared with the logos
        self._subscribers: list[Callable[[float], None]] = []
        self._idle_ticks = 0
        self._timer = QTimer(self)  # created on the GUI thread
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._wake.connect(self._ensure_running)

    # ── public API ─────────────────────────────────────────────────────────
    def subscribe(self, cb: Callable[[float], None]) -> None:
        """Register a listener called with the smoothed 0..1 value per tick.

        Idempotent: the same callback is never stored (and therefore never
        invoked) twice, so repeated Chat-Panel opens cannot stack reactions.
        """
        if cb in self._subscribers:
            return
        self._subscribers.append(cb)
        cb(self._value)   # deliver the current value immediately

    def unsubscribe(self, cb: Callable[[float], None]) -> None:
        try:
            self._subscribers.remove(cb)
        except ValueError:
            pass

    def push(self, level: float) -> None:
        """Feed the REAL playback amplitude (thread-safe)."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        with self._lock:
            # Keep the loudest value since the last tick so short frames of a
            # real utterance are not missed between timer steps.
            if lv > self._incoming:
                self._incoming = lv
        # A push during idle must wake the loop again. Emitting is thread-safe
        # and marshals to the GUI thread, which owns the QTimer.
        if not self._timer.isActive():
            self._wake.emit()

    def value(self) -> float:
        return self._value

    def is_active(self) -> bool:
        return self._timer.isActive()

    # ── internals ────────────────────────────────────────────────────────────
    def _ensure_running(self) -> None:
        if (self._incoming > 0.0 or self._value > self._IDLE_EPS) and not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        with self._lock:
            target = self._incoming
            self._incoming = 0.0   # consumed; next push sets a fresh peak
        # Fast attack, slow release for smooth, voice-like motion.
        if target >= self._value:
            self._value += (target - self._value) * self._ATTACK
        else:
            self._value += (target - self._value) * self._RELEASE
        if self._value < self._IDLE_EPS:
            self._value = 0.0

        for cb in list(self._subscribers):
            try:
                cb(self._value)
            except Exception:
                pass

        # Go fully idle (stop the timer) once silent and nothing is pending, so
        # there is no repaint / CPU while JARVIS is quiet — and no fake pulse.
        if self._value <= 0.0 and self._incoming <= 0.0:
            self._idle_ticks += 1
            if self._idle_ticks >= self._HOLD_TICKS:
                self._timer.stop()
                self._idle_ticks = 0
        else:
            self._idle_ticks = 0


# ── process-wide singleton ─────────────────────────────────────────────────
# The UI builds the real QObject on the GUI thread; the audio pipeline reaches
# it through this indirection so voice/tts.py and main.py never import the UI.
_instance: AudioReactive | None = None
_instance_lock = threading.Lock()


def install(controller: AudioReactive) -> None:
    """Register the GUI-thread-owned controller as the global singleton."""
    global _instance
    with _instance_lock:
        _instance = controller


def controller() -> AudioReactive | None:
    with _instance_lock:
        return _instance


def push_output_level(level: float) -> None:
    """Best-effort feed of real playback amplitude from any audio path.

    Never raises: a missing controller (e.g. headless/tests) is simply ignored,
    so callers on the audio thread are free to call it unconditionally.
    """
    inst = controller()
    if inst is None:
        return
    try:
        inst.push(level)
    except Exception:
        pass
