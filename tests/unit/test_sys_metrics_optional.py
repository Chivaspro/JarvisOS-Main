"""The HUD's metrics sampler must never be why J.A.R.V.I.S fails to start.

``core/sys_metrics.py`` used to import psutil unconditionally, and ``ui.py``
carried its own (completely unused) ``import psutil``. On a machine whose
launcher picked an interpreter without the project's packages that made a
telemetry package the hard requirement for starting a voice assistant — the
app died before a window was ever drawn.

Now psutil is optional: every reading keeps a neutral default, ``snapshot()``
says ``available: False`` so the HUD can render dashes, and the sampler thread
keeps ticking instead of taking the process down.

psutil itself is not required for these tests — they drive the missing-package
path, which is the one that used to be fatal.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import sys_metrics  # noqa: E402


def _stop(sampler) -> None:
    """Stop the sampler's poll thread so the test leaves nothing running."""
    sampler._running = False


def test_sampler_builds_and_reports_unavailable_without_psutil(monkeypatch):
    monkeypatch.setattr(sys_metrics, "psutil", None)

    sampler = sys_metrics.SysMetrics()
    try:
        snapshot = sampler.snapshot()
        assert snapshot["available"] is False
        # Neutral defaults, not a crash and not invented numbers.
        assert snapshot["cpu"] == 0.0
        assert snapshot["mem"] == 0.0
        assert snapshot["gpu"] == -1.0
        assert snapshot["tmp"] == -1.0
        assert snapshot["uptime"] == 0
        assert snapshot["procs"] == 0
    finally:
        _stop(sampler)


def test_update_is_a_no_op_without_psutil(monkeypatch):
    monkeypatch.setattr(sys_metrics, "psutil", None)
    sampler = sys_metrics.SysMetrics()
    try:
        sampler._update()          # the poll loop calls this every 1.5 s
        snapshot = sampler.snapshot()
        assert snapshot["available"] is False
        assert snapshot["cpu"] == 0.0
    finally:
        _stop(sampler)


def test_network_counters_fall_back_to_zero(monkeypatch):
    """The burst-rate maths needs bytes_sent/bytes_recv even with no psutil."""
    monkeypatch.setattr(sys_metrics, "psutil", None)
    sampler = sys_metrics.SysMetrics()
    try:
        assert sampler._last_net.bytes_sent == 0
        assert sampler._last_net.bytes_recv == 0
    finally:
        _stop(sampler)


def test_availability_follows_whether_psutil_imported():
    """With psutil present the sampler is available; without it, it is not.

    Written as a relationship rather than a hard-coded True so the suite still
    passes on a machine where psutil genuinely is not installed.
    """
    sampler = sys_metrics.SysMetrics()
    try:
        assert sampler.snapshot()["available"] is (sys_metrics.psutil is not None)
    finally:
        _stop(sampler)


def test_ui_no_longer_imports_psutil():
    """ui.py must not re-acquire the hard dependency it never used."""
    source = (ROOT / "ui.py").read_text(encoding="utf-8")
    offenders = [
        line for line in source.splitlines()
        if line.strip() == "import psutil" or line.strip().startswith("import psutil ")
    ]
    assert offenders == [], f"ui.py must not import psutil: {offenders}"


def test_the_nvml_probe_runs_at_most_once_per_process(monkeypatch):
    """NVML is a native library: a failure is final, not worth re-probing.

    The GPU path used to call ``pynvml.nvmlInit()`` on every 1.5 s poll even
    after the first failure, re-loading the native driver library each time —
    meaningless work, and repeated native init on a mismatched driver is the
    kind of call that ends the process without a Python exception.
    """
    calls = {"init": 0}

    class _FakePynvml:
        def nvmlInit(self):
            calls["init"] += 1
            raise RuntimeError("no nvml here")

    monkeypatch.setattr(sys_metrics, "_pynvml_ok", None)
    monkeypatch.setitem(sys.modules, "pynvml", _FakePynvml())

    assert sys_metrics._pynvml_gpu() == -1.0
    assert sys_metrics._pynvml_gpu() == -1.0
    assert calls["init"] == 1
