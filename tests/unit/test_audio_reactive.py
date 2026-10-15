"""AudioReactive controller (Task 2) — deterministic, event-loop-free.

Verifies the ONE shared speech-energy signal without wall-clock timer races by
driving ``_tick`` directly:

* fast attack toward a push, slow release back to silence;
* the SAME value is broadcast to every subscriber (logo synchronization);
* a callback can never subscribe twice (no duplicate reaction / stacked loops
  across repeated Chat-Panel opens);
* idle: once released to zero the envelope reaches exactly 0.0 (no fake pulse);
* push is thread-safe (callable off the GUI thread).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("PyQt6.QtCore")

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from core.audio_reactive import AudioReactive  # noqa: E402


@pytest.fixture()
def app():
    return QCoreApplication.instance() or QCoreApplication(["x"])


def _drive(ctrl, n):
    for _ in range(n):
        ctrl._tick()


def test_attack_then_release(app):
    c = AudioReactive()
    # Sustained speech = the audio thread pushes on every callback; a single push
    # is only a one-frame peak, so drive pushes + ticks together like real use.
    prev = 0.0
    for _ in range(6):
        c.push(0.9)
        c._tick()
        assert c.value() > prev, "energy should climb while speech is sustained"
        prev = c.value()
    assert 0.0 < c.value() <= 0.9

    # Silence: no more pushes -> release falls smoothly back to exactly 0.
    _drive(c, 80)
    assert c.value() == 0.0, "logo must return to normal size when silent"


def test_subscribers_share_one_value(app):
    c = AudioReactive()
    seen_a, seen_b = [], []
    c.subscribe(lambda v: seen_a.append(v))
    c.subscribe(lambda v: seen_b.append(v))
    c.push(0.5)
    _drive(c, 3)
    # Both listeners saw identical values on every tick -> the two logos move in
    # lockstep because there is exactly one source of truth.
    assert seen_a == seen_b and len(seen_a) >= 3


def test_subscribe_is_idempotent(app):
    c = AudioReactive()
    hits = []
    cb = lambda v: hits.append(v)
    c.subscribe(cb)
    c.subscribe(cb)          # a repeated Chat-Panel open must not double-register
    assert len(c._subscribers) == 1
    c.push(0.4)
    _drive(c, 1)
    assert len(hits) == 2     # the initial subscribe delivery + one tick


def test_push_clamps_and_is_thread_safe(app):
    c = AudioReactive()
    t = threading.Thread(target=lambda: c.push(2.0))   # over-range + off-thread
    t.start(); t.join()
    assert c._incoming <= 1.0
    _drive(c, 2)
    assert 0.0 < c.value() <= 1.0


def test_idle_stops_the_timer(app):
    c = AudioReactive()
    c._timer.start()          # simulate an active session
    c.push(0.0)
    c._value = 0.0
    c._incoming = 0.0
    _drive(c, AudioReactive._HOLD_TICKS + 1)
    assert not c._timer.isActive(), "reactive loop must stop when there is no speech"
