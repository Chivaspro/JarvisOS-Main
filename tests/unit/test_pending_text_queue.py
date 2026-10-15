"""Typed commands typed before the Live session exists — regression.

The chat window is usable seconds before the Live session connects (UI ~2 s,
connect ~15 s). `_on_text_command` returned silently when no session existed,
so the FIRST typed "Hi Jarvis" got no reply while the second attempt worked.
It now queues the text (bounded, newest-last) and reports the wait once.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as jarvis_main  # noqa: E402


class _UI:
    def __init__(self) -> None:
        self.current_file = ""
        self.logs: list[str] = []

    def write_log(self, text) -> None:
        self.logs.append(str(text))


def _stub() -> SimpleNamespace:
    return SimpleNamespace(
        ui=_UI(),
        _loop=None,
        session=None,
        _pending_text_commands=[],
        _last_user_utterance="",
        _live_user_utterance="",
    )


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.brain_bridge",
                        SimpleNamespace(observe_utterance=lambda text: None))
    monkeypatch.setitem(sys.modules, "memory.service", SimpleNamespace(
        detect_memory_command=lambda text: False,
        get_memory_service=lambda: None))


def test_first_message_is_queued_and_announced_once():
    stub = _stub()
    jarvis_main.JarvisLive._on_text_command(stub, "Hi Jarvis")
    assert stub._pending_text_commands == ["Hi Jarvis"]
    assert len(stub.ui.logs) == 1
    assert "connecting" in stub.ui.logs[0]

    jarvis_main.JarvisLive._on_text_command(stub, "You there?")
    assert stub._pending_text_commands == ["Hi Jarvis", "You there?"]
    assert len(stub.ui.logs) == 1, "the notice must not repeat per message"


def test_queue_keeps_only_the_newest_five():
    stub = _stub()
    for i in range(8):
        jarvis_main.JarvisLive._on_text_command(stub, f"msg {i}")
    assert stub._pending_text_commands == [f"msg {i}" for i in range(3, 8)]


def test_empty_text_is_never_queued():
    stub = _stub()
    jarvis_main.JarvisLive._on_text_command(stub, "   ")
    assert stub._pending_text_commands == []


def test_flush_delivers_in_order_and_clears():
    stub = _stub()
    stub._pending_text_commands = ["one", "two"]
    sent: list[str] = []
    stub._on_text_command = lambda text: sent.append(text)
    jarvis_main.JarvisLive._flush_pending_text_commands(stub)
    assert sent == ["one", "two"]
    assert stub._pending_text_commands == []


def test_flush_is_a_noop_when_empty():
    stub = _stub()
    jarvis_main.JarvisLive._flush_pending_text_commands(stub)  # must not raise
    assert stub._pending_text_commands == []
