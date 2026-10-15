"""Mark LII security (change: modernize-jarvis-architecture).

Risk-tiered action classification and approval modes, wrapping the existing
permission gate (`main.py::_permission_gate`) and the UI-issued confirmation
tokens (`core/confirm.py`) in one service that every subsystem — tool
dispatcher, computer facade, coding agents, orchestrator — consumes, so risk
policy is defined once and cannot be bypassed by choosing a different path.
"""

from security.approvals import (
    RiskTier,
    classify,
    approval_mode,
    set_approval_mode,
    evaluate,
    requires_ui_token,
    refusal_for,
    approvals_check,
    check_permission,
)

__all__ = [
    "RiskTier", "classify", "approval_mode", "set_approval_mode", "evaluate",
    "requires_ui_token", "refusal_for", "approvals_check", "check_permission",
]
