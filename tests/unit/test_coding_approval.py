"""Coding-approval gate unit tests.

Proves: auto-approve short-circuits the chat card entirely; without it the
gate blocks until resolve() is called from the (simulated) UI; a decline
returns False; and the new voice_settings / diagnostics permission wiring
refuses correctly when the matching Settings switch is off.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security import coding_approval  # noqa: E402
from security.permissions import permission_refusal  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    # Every test starts from a clean bind/auto-approve state.
    coding_approval.bind(show=None, log=None)
    monkeypatch.setattr(coding_approval, "auto_approve_enabled", lambda: False)
    yield
    coding_approval.bind(show=None, log=None)


def test_auto_approve_skips_the_card(monkeypatch):
    monkeypatch.setattr(coding_approval, "auto_approve_enabled", lambda: True)
    calls = []
    coding_approval.bind(show=lambda *a: calls.append(a))
    assert coding_approval.request_approval("Build X", "detail") is True
    assert calls == []  # never even shown


def test_no_ui_bound_fails_closed():
    assert coding_approval.request_approval("Build X", "detail") is False


def test_approve_unblocks_the_waiting_call():
    shown = {}

    def _show(req_id, title, detail):
        shown["id"] = req_id

    coding_approval.bind(show=_show)
    result = {}

    def _worker():
        result["ok"] = coding_approval.request_approval("Build X", "detail")

    t = threading.Thread(target=_worker)
    t.start()
    # Wait for the card to be "shown" before resolving it.
    for _ in range(100):
        if "id" in shown:
            break
        time.sleep(0.01)
    assert "id" in shown
    coding_approval.resolve(shown["id"], True)
    t.join(timeout=5)
    assert result["ok"] is True


def test_decline_returns_false():
    shown = {}
    coding_approval.bind(show=lambda req_id, t, d: shown.setdefault("id", req_id))
    result = {}

    def _worker():
        result["ok"] = coding_approval.request_approval("Build X", "detail")

    t = threading.Thread(target=_worker)
    t.start()
    for _ in range(100):
        if "id" in shown:
            break
        time.sleep(0.01)
    coding_approval.resolve(shown["id"], False)
    t.join(timeout=5)
    assert result["ok"] is False


def test_resolve_on_unknown_id_is_a_noop():
    coding_approval.resolve("does-not-exist", True)  # must never raise


# ── permission wiring for voice_settings / diagnostics ─────────────────────

def test_voice_settings_status_is_never_gated():
    off = lambda feat: True  # every switch off
    assert permission_refusal(off, "voice_settings", {"mode": "status"}) is None


def test_voice_settings_set_needs_permission():
    enabled = lambda feat: feat != "voice_engine_control"  # that one switch is off
    refusal = permission_refusal(enabled, "voice_settings", {"mode": "set", "provider": "gemini"})
    assert refusal and "voice engine" in refusal.lower()


def test_voice_settings_set_allowed_when_on():
    on = lambda feat: True
    assert permission_refusal(on, "voice_settings", {"mode": "set"}) is None


def test_diagnostics_needs_self_diagnostics_permission():
    enabled = lambda feat: feat != "self_diagnostics"  # that one switch is off
    refusal = permission_refusal(enabled, "diagnostics", {"mode": "check"})
    assert refusal is not None


def test_diagnostics_allowed_when_on():
    on = lambda feat: True
    assert permission_refusal(on, "diagnostics", {"mode": "check"}) is None
