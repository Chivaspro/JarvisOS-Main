"""Discovery — when does Jarvis decide it needs a brand-new capability?

Two independent signals, and nothing else:

* **An explicit directive.** "learn how to …", "create a skill that …",
  "upgrade yourself with …", "forge a tool for …". These are parsed into a goal
  and a suggested snake_case name.
* **A real capability gap.** A tool call that failed because the tool does not
  exist, or `skill_query` finding no matching skill, is evidence — not a guess.
  ``gap_hint()`` turns that evidence into one sentence the assistant can say,
  offering a forge; it never forges anything by itself.

Both signals feed the same outcome: an assistant that can say "I don't have
that yet — shall I build it?" instead of pretending.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["LEARN_PATTERNS", "analyze_command", "gap_hint", "suggest_name",
           "is_capability_gap"]

#: Directive phrasings. Group 1 and 2 are (descriptor, action) pairs when both
#: are present, otherwise the single group is the whole goal.
LEARN_PATTERNS: tuple[str, ...] = (
    r"(?:jarvis\s*,?\s*)?(?:learn\s+how\s+to|teach\s+yourself\s+(?:how\s+)?to|"
    r"figure\s+out\s+how\s+to)\s+(.+)",
    r"(?:jarvis\s*,?\s*)?(?:create|forge|build|make|synthesize|add|develop|write)\s+"
    r"(?:a\s+)?(?:new\s+)?(?:([\w\s-]{2,30})\s+)?"
    r"(?:skill|tool|feature|capability|plugin|function)\s+"
    r"(?:for|to|that|which|allowing|where)\s+(.+)",
    r"(?:jarvis\s*,?\s*)?(?:create|forge|build|make|synthesize|add|develop|write)\s+"
    r"(?:a\s+)?(?:new\s+)?(.+?)\s+(?:skill|tool|feature|capability|plugin|function)\b",
    r"(?:jarvis\s*,?\s*)?(?:upgrade\s+yourself\s+with|evolve\s+to)\s+(.+)",
)

_STOPWORDS = frozenset({
    "a", "an", "the", "to", "for", "in", "of", "and", "how", "that", "it",
    "will", "allow", "you", "see", "me", "my", "i", "with", "on", "at", "by",
    "from", "is", "are", "be", "this", "which", "where", "allowing",
})

#: Sentences that mean "the tool you asked for does not exist".
_GAP_MARKERS: tuple[str, ...] = (
    "is not runnable by the orchestrator",
    "is not an action of",
    "is not available",
    "unknown tool",
    "no such tool",
    "not a tool",
    "tool not found",
    "plugin '",
    "does not exist",
    "not implemented",
)


def analyze_command(text: str) -> dict[str, str] | None:
    """Parse an explicit learn/forge directive, or None when it is not one."""
    clean = " ".join(str(text or "").split())
    if not clean:
        return None
    for pattern in LEARN_PATTERNS:
        match = re.search(pattern, clean, re.IGNORECASE)
        if not match:
            continue
        groups = [g.strip() for g in match.groups() if g and g.strip()]
        if not groups:
            continue
        if len(groups) == 2:
            descriptor, action = groups
            goal = f"{descriptor}: {action}"
            name_source = descriptor
        else:
            goal = groups[0]
            name_source = goal
        return {
            "goal": goal,
            "suggested_name": suggest_name(name_source),
            "raw_command": clean,
        }
    return None


def suggest_name(text: str, words: int = 4) -> str:
    """A short snake_case name derived from a phrase."""
    tokens = re.findall(r"[a-zA-Z0-9]+", str(text or "").lower())
    meaningful = [t for t in tokens if t not in _STOPWORDS][:max(1, words)]
    name = "_".join(meaningful) or "forged_skill"
    if name[0].isdigit():
        name = f"skill_{name}"
    return name[:64]


def is_capability_gap(result: object) -> bool:
    """Does this tool result show a capability Jarvis simply does not have?"""
    text = str(result or "").lower()
    if not text:
        return False
    return any(marker in text for marker in _GAP_MARKERS)


def gap_hint(request: str = "", result: object = "") -> str:
    """One sentence offering to forge the missing capability ("" when n/a).

    ``request`` is the user's own wording when it is known, so the sentence can
    name what was actually asked for instead of inventing a topic.
    """
    if result and not is_capability_gap(result):
        return ""
    wanted = " ".join(str(request or "").split())[:120]
    if wanted:
        return (f"I don't have that capability yet, sir — I can forge a new skill "
                f"for “{wanted}” and verify it in the Crucible.")
    return ("I don't have that capability yet, sir — I can forge a new skill for "
            "it and verify it in the Crucible.")
