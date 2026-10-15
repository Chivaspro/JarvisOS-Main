"""Security approval service unit tests (tasks 3.1-3.3, 11.1).

Proves the deterministic risk-tier table, the four approval modes, the
permission-refusal passthrough, and that irreversible actions always require
the UI-issued token — a model-supplied ``confirmed`` parameter can never
satisfy the gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from security.rules import classify  # noqa: E402
from security import approvals  # noqa: E402


# ── rule table (deterministic classification) ─────────────────────────────────

@pytest.mark.parametrize("action,params,expected", [
    ("web_search", {}, "low"),
    ("system_status", {}, "low"),
    ("jarvis_window", {"window": "webview"}, "low"),
    ("file_controller", {"action": "read"}, "low"),
    ("file_controller", {"action": "write"}, "medium"),
    ("file_controller", {"action": "delete", "count": 3}, "medium"),
    ("file_controller", {"action": "delete"}, "high"),
    ("totally_unknown", {}, "low"),
    ("format_everything", {}, "high"),
])
def test_classification_is_deterministic(action, params, expected):
    assert classify(action, params).value == expected
    # Same inputs → same tier, always.
    assert classify(action, params) == classify(action, params)


# ── approval modes ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_mode():
    yield
    approvals.set_approval_mode("high_risk_only")


def test_default_mode_is_high_risk_only():
    assert approvals.approval_mode() == "high_risk_only"


def test_high_risk_only_waits_for_high_risk_only():
    approvals.set_approval_mode("high_risk_only")
    d_medium = approvals.evaluate("file_controller", {"action": "write"})
    assert d_medium["allowed"] is True and d_medium["needs_ui_token"] is False
    d_high = approvals.evaluate("file_controller", {"action": "delete"})
    assert d_high["allowed"] is False and d_high["needs_ui_token"] is True


def test_always_confirm_waits_for_everything():
    approvals.set_approval_mode("always_confirm")
    d = approvals.evaluate("file_controller", {"action": "read"})
    assert d["needs_ui_token"] is True


def test_never_confirm_allows_high_risk_but_not_irreversible():
    approvals.set_approval_mode("never_confirm")
    d_send = approvals.evaluate("file_controller", {"action": "write"})
    assert d_send["allowed"] is True
    d_del = approvals.evaluate("file_controller", {"action": "delete"})
    assert d_del["needs_ui_token"] is True, "bulk delete is irreversible"


# ── the model cannot forge confirmation ───────────────────────────────────────

@pytest.mark.parametrize("action", ["shutdown", "restart", "toggle_wifi"])
def test_irreversible_always_needs_ui_token(action):
    for mode in ("always_confirm", "high_risk_only", "never_confirm"):
        approvals.set_approval_mode(mode)
        d = approvals.evaluate(action, {"confirmed": "yes"})
        assert d["needs_ui_token"] is True, f"{action} bypassed in {mode}"
        assert d["allowed"] is False


def test_confirmed_parameter_is_ignored():
    d = approvals.evaluate("shutdown", {"confirmed": "true", "confirm": 1})
    assert d["needs_ui_token"] is True


# ── permission refusal passthrough (identical wording to the original gate) ──

def test_permission_gate_refusal_names_the_switch():
    from security.permissions import permission_refusal

    def feature_enabled(name):
        return name != "terminal_control"

    # code_helper 'run' is gated by the terminal permission.
    r = permission_refusal(feature_enabled, "code_helper", {"action": "run"})
    assert r and "Terminal and command execution" in r
    # computer_control has been removed: it is no longer a gated tool at all.
    assert permission_refusal(feature_enabled, "computer_control",
                              {"action": "click"}) is None


def test_main_delegates_to_security_service():
    import main as m

    def feature_enabled(name):
        return name != "file_control"

    r = m._permission_gate(feature_enabled, "file_controller", {"action": "read"})
    assert r and "File control" in r
    assert m._permission_gate(feature_enabled, "web_search", {}) is None


# ── diagnostics surface ───────────────────────────────────────────────────────

def test_approvals_diagnostics_check():
    from core.diagnostics import check
    c = check("approvals")
    assert c.state == "ONLINE" and "mode=" in c.detail
