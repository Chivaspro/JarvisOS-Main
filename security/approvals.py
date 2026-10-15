"""Risk-tiered approval service (security-approvals spec).

Tiers (master prompt §22):
* LOW    — read screen, search web, open application, read file, inspect project
* MEDIUM — edit/delete file, install software, change settings, run shell commands
* HIGH   — send email/message, purchase, bulk deletion, security settings,
           dangerous commands

Modes (user-selectable in config features.approval_mode):
* always_confirm  — every gated action waits for a human press
* high_risk_only  — default: only HIGH-risk actions wait
* never_confirm   — nothing waits EXCEPT irreversible actions, which ALWAYS
                    require the UI-issued token (the mode can never bypass it)
* custom          — per-permission overrides in features.approval_overrides

Irreversible actions are executed through the existing core/confirm.py token
mechanism: the token is issued by the interface after a human press. A
model-supplied ``confirmed`` tool parameter is never accepted as proof —
evaluate() deliberately ignores it.

Refusals name the switched-off permission, preserving today's observable
behavior of JARVIS saying what it did not do.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from security.rules import classify, RiskTier, _DELETE_ACTIONS  # re-exported for convenience

_MODES = ("always_confirm", "high_risk_only", "never_confirm", "custom")
_DEFAULT_MODE = "high_risk_only"

_mode_lock = threading.Lock()
_mode_override: str | None = None   # tests / runtime admin override

# Actions that can never be reversed and therefore always need the UI token,
# whatever the mode says (security spec: never bypassable). coding_self_tree
# is a coding-agent run against JARVIS's own live installation.
_IRREVERSIBLE = {
    "shutdown", "restart", "toggle_wifi", "delete", "bulk_delete",
    "organize_desktop", "uninstall", "factory_reset", "coding_self_tree",
}


def _features() -> dict:
    try:
        from core.brain_bridge import current_features
        return current_features()
    except Exception:
        return {}


def _config_value(key: str, default: Any = None) -> Any:
    feats = _features()
    if key in feats:
        return feats[key]
    try:
        from config.settings import get_feature
        return get_feature(key.replace("features.", ""), default)
    except Exception:
        return default


def approval_mode() -> str:
    """Active approval mode (override → config → default)."""
    with _mode_lock:
        if _mode_override:
            return _mode_override
    m = str(_config_value("approval_mode", _DEFAULT_MODE) or _DEFAULT_MODE).strip().lower()
    return m if m in _MODES else _DEFAULT_MODE


def set_approval_mode(mode: str) -> bool:
    """Runtime override (Settings can persist it via the features map)."""
    m = str(mode or "").strip().lower()
    if m not in _MODES:
        return False
    with _mode_lock:
        globals()["_mode_override"] = m
    return True


def requires_ui_token(action: str, params: dict | None = None) -> bool:
    """True when this action is irreversible → always needs a human press.

    Considers both the tool name (shutdown/restart/…) and destructive
    sub-actions of multi-action tools (file_controller delete without a
    small count is a bulk deletion)."""
    a = str(action or "").strip().lower()
    if a in _IRREVERSIBLE:
        return True
    p = dict(params or {})
    sub = str(p.get("action", "") or "").strip().lower()
    if a == "file_controller" and sub in _DELETE_ACTIONS:
        try:
            count = int(p.get("count"))
        except (TypeError, ValueError):
            count = None
        return count is None or count > 5

    # Control capabilities arrive as "<capability>_<verb>" (capabilities/router),
    # including the local controllers (pc_control / visual_control /
    # browser_action). A permanent-deletion or machine-level verb is irreversible
    # whoever performs it, so it always waits for the UI token whatever the
    # approval mode says.
    for cap in ("computer_control", "browser_control", "pc_control",
                "visual_control", "browser_action", "power_control"):
        if a.startswith(cap + "_"):
            verb = a[len(cap) + 1:]
            if verb in _IRREVERSIBLE or verb in _DELETE_ACTIONS:
                return True
            task = str(p.get("task", "") or "").lower()
            if verb in ("empty", "overwrite") or "permanently" in task:
                return True
    return False


def _custom_allows(tier: RiskTier, action: str) -> bool | None:
    """Per-action override from features.approval_overrides; None = unset."""
    overrides = _config_value("approval_overrides")
    if not isinstance(overrides, dict):
        return None
    v = overrides.get(str(action or "").strip().lower())
    if v is None:
        v = overrides.get(f"{tier.value}_risk")
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("allow", "auto", "true", "1")
    return bool(v)


def evaluate(
    action: str,
    params: dict | None = None,
    *,
    enabled: Callable[[str], bool] | None = None,
    permission: str = "",
) -> dict:
    """Gate one action. Returns a decision dict, never raises.

    Keys:
      allowed          — the action may proceed without waiting
      needs_ui_token   — park it behind core/confirm.request()
      refusal          — sentence for the model when blocked (permission off)
      tier / mode      — classification evidence for logs and diagnostics
    """
    p = dict(params or {})
    a = str(action or "").strip()
    tier = classify(a, p)

    # 1) The existing per-permission gate still runs first: a revoked switch
    #    stops the action with a refusal that names the permission.
    if enabled is not None:
        try:
            from security.permissions import permission_refusal
            refusal = permission_refusal(enabled, permission or a, p)
            if refusal:
                return {"allowed": False, "needs_ui_token": False,
                        "refusal": refusal, "tier": tier.value,
                        "mode": approval_mode()}
        except Exception:
            pass  # a broken gate lookup must not block tools (existing contract)

    mode = approval_mode()

    # 2) Irreversible actions always wait for the UI token — even in
    #    never_confirm, and regardless of anything the model passes in args
    #    (`confirmed` is deliberately not read here).
    if requires_ui_token(a, p):
        return {"allowed": False, "needs_ui_token": True, "refusal": "",
                "tier": tier.value, "mode": mode}

    # 3) Mode policy for everything else.
    if mode == "always_confirm":
        wait = True
    elif mode == "never_confirm":
        wait = False
    elif mode == "custom":
        override = _custom_allows(tier, a)
        wait = tier is RiskTier.HIGH if override is None else (not override)
    else:  # high_risk_only (default)
        wait = tier is RiskTier.HIGH

    return {"allowed": not wait, "needs_ui_token": wait, "refusal": "",
            "tier": tier.value, "mode": mode}


def refusal_for(permission_name: str) -> str:
    """Refusal sentence naming the switched-off permission (existing wording)."""
    try:
        from security.permissions import label_for
        return label_for(permission_name)
    except Exception:
        return f"{permission_name} is switched off in Settings, so I have not done that."


def check_permission(enabled: Callable[[str], bool], name: str, args: dict) -> str | None:
    """Direct passthrough to the existing permission gate semantics.

    Returns the refusal sentence when a revoked Settings permission covers
    this tool/action, else None. Kept so callers can use the security service
    with byte-identical behavior to main.py::_permission_gate.
    """
    try:
        from security.permissions import permission_refusal
        return permission_refusal(enabled, name, args)
    except Exception:
        return None


def approvals_check() -> "Check":
    """Diagnostics Check describing the approval service state."""
    try:
        from core.diagnostics import Check, ONLINE
        mode = approval_mode()
        return Check("approvals", ONLINE, f"mode={mode}", "")
    except Exception:
        try:
            from core.diagnostics import Check, ERROR
            return Check("approvals", ERROR, "approval service unavailable", "")
        except Exception:
            class _F:  # last resort: never raise
                name, state, detail, fix = "approvals", "ERROR", "unavailable", ""
            return _F()  # type: ignore[return-value]
