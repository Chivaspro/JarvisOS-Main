"""A wake-word detector that cannot be built must not vanish silently.

``WakeWordManager.ensure()`` is reached from the microphone feed path — once per
audio block. It used to swallow the construction failure and return ``None``
with no trace anywhere, so a missing optional dependency (openwakeword,
onnxruntime) or an unusable keyword model left the wake phrase permanently dead
with nothing in the log to explain it, while the app looked healthy.

The failure is now reported through the project logger — ONCE per process, not
once per audio block, because that caller runs at the frame rate.
"""
from __future__ import annotations

import pytest

from voice import wake


@pytest.fixture()
def logged(monkeypatch):
    """Capture project log events and give each test a fresh once-flag."""
    events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(wake, "_detector_failure_logged", False)
    import observability.logger as logger
    monkeypatch.setattr(
        logger, "log_event",
        lambda level, component="app", event="", **_kw: events.append(
            (level, component, event)))
    return events


def test_a_detector_that_cannot_be_built_is_reported_once(monkeypatch, logged):
    def _boom(*_a, **_kw):
        raise RuntimeError("no onnxruntime")

    monkeypatch.setattr(wake, "WakeWordDetector", _boom)
    mgr = wake.WakeWordManager(get_config=lambda: {})

    assert mgr.ensure() is None
    assert mgr.ensure() is None            # the feed path retries every block
    assert len(logged) == 1, logged
    level, component, event = logged[0]
    assert level == "WARNING"
    assert component == "wake"
    assert "wake phrase" in event
    assert "RuntimeError" in event         # the real cause, not a generic line


def test_a_detector_that_builds_is_reused_without_a_warning(monkeypatch, logged):
    class _Detector:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(wake, "WakeWordDetector", _Detector)
    mgr = wake.WakeWordManager(get_config=lambda: {"wake_words": ["jarvis"]})

    first = mgr.ensure()
    assert first is not None
    assert mgr.ensure() is first           # built once, then reused
    assert first.kwargs["words"]           # the configured phrases reached it
    assert logged == []
