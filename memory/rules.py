"""Learned rules — the corrections the user should only ever have to make once.

"When I say 'the lab', I mean the office."  "Never read my messages out loud."
"Always answer in metric."  Each of those is a *rule*: a persistent behavioural
directive, stored with the moment it was learned and the scope it applies to.

Rules are bounded in number and length on purpose. They are injected into every
system prompt, so an unbounded list would slowly eat the context window — the
cap (and ``prompt_block``'s own limit) keeps the block small and predictable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = ["add", "forget", "list_rules", "prompt_block"]

_MAX_RULES = 60
_MAX_LEN = 240
_DEFAULT_SCOPE = "global"


def _path() -> Path:
    return Path(__file__).resolve().parent / "rules.json"


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list[dict[str, Any]]) -> bool:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def list_rules() -> list[dict[str, Any]]:
    return _load()


def add(rule: str, scope: str = _DEFAULT_SCOPE) -> tuple[bool, str]:
    """Remember one directive. Duplicates (case-insensitive) are refused."""
    text = " ".join(str(rule or "").split())[:_MAX_LEN]
    if len(text) < 4:
        return False, "That rule is too short to remember."
    rows = _load()
    low = text.lower()
    for row in rows:
        if str(row.get("rule") or "").strip().lower() == low:
            return True, f"I already follow that rule, sir: “{text}”."
    rows.append({"rule": text, "scope": str(scope or _DEFAULT_SCOPE)[:40],
                 "learned_at": time.time()})
    del rows[:-_MAX_RULES]
    if not _save(rows):
        return False, "I could not write that rule to disk."
    return True, f"Noted, sir — I will always remember: “{text}”."


def forget(needle: str) -> tuple[bool, str]:
    """Forget by index (1-based) or by substring."""
    rows = _load()
    if not rows:
        return False, "I have no rules to forget, sir."
    key = str(needle or "").strip()
    if key.isdigit():
        index = int(key) - 1
        if 0 <= index < len(rows):
            removed = rows.pop(index)
            _save(rows)
            return True, f"Forgotten: “{removed.get('rule')}”."
        return False, f"There is no rule number {key}, sir."
    low = key.lower()
    for index, row in enumerate(rows):
        if low and low in str(row.get("rule") or "").lower():
            removed = rows.pop(index)
            _save(rows)
            return True, f"Forgotten: “{removed.get('rule')}”."
    return False, f"I have no rule matching “{key}”, sir."


def prompt_block(limit: int = 20) -> str:
    """Rules for the system prompt, newest last ("" when there are none)."""
    rows = _load()[-max(1, int(limit)):]
    if not rows:
        return ""
    lines = [f"- {str(row.get('rule') or '').strip()}" for row in rows
             if str(row.get("rule") or "").strip()]
    if not lines:
        return ""
    return "LEARNED RULES (follow these exactly)\n" + "\n".join(lines)
