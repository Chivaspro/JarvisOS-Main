"""Risk-tier rule table — deterministic classification (security spec).

The tier is derived from the action type and parameters by this rule table,
never by model judgment alone. Unlisted actions classify as LOW (read-only
behavior is the safe default) unless their parameters carry a bulk flag.
"""

from __future__ import annotations

import enum
from typing import Any


class RiskTier(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Tool-name → tier for the whole-call classification.
_TOOL_TIERS: dict[str, RiskTier] = {
    # LOW: observation and lookup
    "screen_process": RiskTier.LOW,
    "close_camera": RiskTier.LOW,
    "web_search": RiskTier.LOW,
    "weather_report": RiskTier.LOW,
    "system_status": RiskTier.LOW,
    "recall_memory": RiskTier.LOW,
    "skill_query": RiskTier.LOW,
    "task_plan": RiskTier.LOW,
    "diagnostics": RiskTier.LOW,
    "performance_scan": RiskTier.LOW,
    "jarvis_window": RiskTier.LOW,
    "brain_summary": RiskTier.LOW,
    "web_research": RiskTier.LOW,      # retrieval only: it never changes anything
    # MEDIUM: reversible state changes
    "file_controller": RiskTier.MEDIUM,
    "code_helper": RiskTier.MEDIUM,
    "dev_agent": RiskTier.MEDIUM,
    "reminder": RiskTier.MEDIUM,
    "youtube_video": RiskTier.MEDIUM,
    "play_music": RiskTier.LOW,
    "computer_control": RiskTier.MEDIUM,   # Agent TARS acting on the desktop
    "browser_control": RiskTier.MEDIUM,    # Agent TARS acting on a website
    "pc_control": RiskTier.MEDIUM,         # local desktop/window/input control
    "visual_control": RiskTier.MEDIUM,     # UI-TARS visual control of the screen
    "browser_action": RiskTier.MEDIUM,     # Playwright acting on a page element
    # HIGH: outward-facing or destructive
    "coding_agent": RiskTier.HIGH,
}

#: Capability verbs that make an otherwise-reversible action outward-facing:
#: something leaves this machine, or is destroyed, and cannot be undone by
#: pressing undo. capabilities/router.py builds action names as
#: "<capability>_<verb>", and the tier is looked up here.
_OUTWARD_VERBS = {"send", "submit", "publish", "post", "reply", "buy",
                  "purchase", "order", "pay", "delete", "remove",
                  "uninstall", "unsubscribe", "book", "reserve", "share",
                  "comment", "apply for"}

_FILE_MEDIUM = {
    "create_file", "create_folder", "move", "copy", "rename", "write",
    "organize_desktop",
}

_DELETE_ACTIONS = {"delete", "bulk_delete", "remove"}


def classify(action: str, params: dict | None = None) -> RiskTier:
    """Deterministic tier for one action. Never raises."""
    try:
        return _classify(action or "", dict(params or {}))
    except Exception:
        return RiskTier.LOW


def _classify(action: str, p: dict) -> RiskTier:
    a = str(action or "").strip().lower()

    # Sub-action refinement for the multi-action tools.
    sub = str(p.get("action", "") or "").strip().lower()

    if a == "file_controller":
        if sub in _DELETE_ACTIONS:
            count = _param_int(p.get("count")) or _param_int(p.get("confirm_count"))
            return RiskTier.HIGH if (count is None or count > 5) else RiskTier.MEDIUM
        if sub in _FILE_MEDIUM:
            return RiskTier.MEDIUM
        return RiskTier.LOW  # list/read/find/info/stats

    if a in ("code_helper", "dev_agent"):
        if sub in ("run", "build", "install", "execute", "terminal"):
            return RiskTier.MEDIUM
        return RiskTier.MEDIUM

    # Control capabilities: the engine decides *how*, this decides how risky.
    # The local controllers are named the same way ("<tool>_<verb>", built by
    # capabilities/router.py from the request's own words), so an action that
    # sends, submits, buys or deletes is HIGH whichever controller performs it.
    for cap in ("computer_control", "browser_control", "pc_control",
                "visual_control", "browser_action"):
        if a.startswith(cap + "_"):
            verb = a[len(cap) + 1:]
            if verb in _OUTWARD_VERBS or sub in _OUTWARD_VERBS:
                return RiskTier.HIGH
            return RiskTier.MEDIUM

    tier = _TOOL_TIERS.get(a)
    if tier is not None:
        return tier

    # Unknown actions with dangerous verbs in the name stay cautious.
    if any(v in a for v in ("delete", "format", "wipe", "purchase", "pay")):
        return RiskTier.HIGH
    if any(v in a for v in ("install", "uninstall", "write", "set_", "toggle")):
        return RiskTier.MEDIUM
    return RiskTier.LOW


def _param_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
