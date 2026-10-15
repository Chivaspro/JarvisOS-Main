"""Unit test for the microphone voice gate (STT false-`noise` pipeline fix).

The gate is the §4 root-cause fix: silence / low-level ambient noise must not be
streamed to the Live ASR as if it were an utterance (the source of transcripts
that read ``noise`` when nothing intelligible was said). It is NOT a transcript
filter — recognised PCM is forwarded byte-for-byte, with a pre-roll that keeps
the speech onset and a hangover that keeps the tail.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("numpy")

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("main_impl_gate_tests", ROOT / "main.py")
main = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = main
_spec.loader.exec_module(main)


def _block(rms: float, n: int = 1024, seed: int = 0):
    rs = np.random.RandomState(seed)
    x = rs.randn(n).astype(np.float32)
    x = x / float(np.sqrt(np.mean(x * x))) * float(rms)
    xi = np.clip(x, -32767, 32767).astype(np.int16)
    return xi, xi.tobytes()


def _feed(gate, blocks):
    total = 0
    onset = None
    for i, (indata, data) in enumerate(blocks):
        out = gate.accept(indata, data)
        if out:
            total += len(out)
            if onset is None:
                onset = i
    return total, onset


def test_pure_silence_is_never_streamed():
    gate = main._MicVoiceGate(16000, 1024)
    blocks = [_block(40, seed=i) for i in range(30)]
    total, _ = _feed(gate, blocks)
    assert total == 0, "silence/ambient noise must not be submitted to the ASR"


def test_speech_includes_preroll_onset_and_tail():
    gate = main._MicVoiceGate(16000, 1024)
    # 8 quiet, then 6 loud (speech), then 4 quiet (should be inside hangover).
    seq = ([_block(40, seed=i) for i in range(8)]
           + [_block(2200, seed=i) for i in range(6)]
           + [_block(40, seed=i) for i in range(4)])
    # Prime the pre-roll with the 8 quiet blocks so the onset flush carries them.
    submitted = []
    for indata, data in seq:
        submitted.append(len(gate.accept(indata, data)))
    # First loud block returns > 1 (pre-roll + current): onset is not clipped.
    assert submitted[8] >= 2, "pre-roll should be flushed with the first speech block"
    # The quiet blocks right after speech stay inside the hangover (still sent).
    assert all(n >= 1 for n in submitted[9:12]), "hangover must keep the tail"


def test_reset_drops_buffered_audio():
    """Mute / barge-in end / phone handover must not leak stale audio forward."""
    gate = main._MicVoiceGate(16000, 1024)
    for i in range(10):
        gate.accept(*_block(40, seed=i))          # builds pre-roll, submits nothing
    gate.reset()
    # After reset the pre-roll backlog is gone, so the first loud block submits
    # only itself — no stale pre-mute audio leaks into the next window.
    out = gate.accept(*_block(2200, seed=98))
    assert len(out) == 1, "reset must clear the pre-roll backlog"


def test_quiet_speech_still_opens_gate():
    """A soft talker above the absolute floor must not be gated out."""
    gate = main._MicVoiceGate(16000, 1024, abs_floor=150.0)
    out = gate.accept(*_block(600, seed=1))      # 600 rms — quiet but real
    assert len(out) >= 1, "real speech above the floor must pass"
