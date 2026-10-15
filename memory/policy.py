"""Memory promotion policy (task 6.2) — what deserves long-term storage.

Scopes (memory-policy spec):
* temporary  — this breath of the conversation; never stored
* session    — this conversation only; the session ledger already keeps it
* long_term  — durable user/project facts; promotable
* project    — durable facts tied to a project id; promotable with project_id
* task       — in-flight work; lives in task state, never long-term memory

The classifier is deterministic and offline: regex/rule based, no model call.
``promote()`` is the single gate between a candidate fact and the memory
backend; ephemeral chatter never passes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from memory.backend import get_memory_backend, MemoryBackend


class Scope(str, Enum):
    TEMPORARY = "temporary"
    SESSION = "session"
    LONG_TERM = "long_term"
    PROJECT = "project"
    TASK = "task"


@dataclass
class Candidate:
    content: str
    scope: Scope = Scope.TEMPORARY
    category: str = ""
    project_id: str = ""
    explicit: bool = False   # the user said "remember this"
    reason: str = ""


# Durable-fact markers. Anything matching these is long-term material.
_DURABLE_PATTERNS = (
    re.compile(r"\bmy (sister|brother|mother|father|wife|husband|daughter|son|"
               r"dog|cat|boss|colleague|friend)('?s?| is|)'s? name is\b", re.I),
    re.compile(r"\bi (prefer|like|love|hate|always|never|usually)\b", re.I),
    re.compile(r"\bmy (birthday|anniversary) is\b", re.I),
    re.compile(r"\bi (work|live|study)\b", re.I),
    re.compile(r"\bremember (this|that)\b", re.I),
    re.compile(r"\bdon'?t forget\b", re.I),
    re.compile(r"\bi use\b", re.I),
    re.compile(r"\bmy (name|email|phone|address) is\b", re.I),
    re.compile(r"\bthe (project|api|password policy|convention) uses\b", re.I),
    re.compile(r"\bi'?m (allergic|vegan|vegetarian)\b", re.I),
    # The user's own life, said plainly: how they want to be addressed, the
    # things that are theirs, and where they are based. These are the facts a
    # conversation teaches that must survive the session it was said in.
    re.compile(r"\b(you can |please |just )?call me\b", re.I),
    re.compile(r"\bmy (office|desk|car|home|house|apartment|flat|team|company|"
               r"role|title|manager|pets?|favourite|favorite)\b", re.I),
    re.compile(r"\bi (go by|am based|'m based|live in|work at|work for|"
               r"study at|'m from)\b", re.I),
    re.compile(r"\bmy (?:sister|brother|mother|father|wife|husband|son|"
               r"daughter|friend|colleague)'?s (?:name )?is\b", re.I),
)

# Which category a durable fact belongs to, so the store that receives it
# files it where the Memory tab and the vault show the user's own facts.
_DURABLE_CATEGORY = (
    (re.compile(r"\b(my name|call me|i live|i work|i study|my birthday|"
                r"my anniversary|my email|my phone|my address)\b", re.I), "PROFILE"),
    (re.compile(r"\b(i prefer|i like|i love|i hate|i use|i'?m allergic|"
                r"vegan|vegetarian|usually|always|never)\b", re.I), "PREFERENCES"),
    (re.compile(r"\b(the project|project uses|convention uses)\b", re.I), "PROJECTS"),
)


# Ephemeral markers — commands and transient state, never durable facts.
_EPHEMERAL_PATTERNS = (
    re.compile(r"^(open|close|launch|start|stop|kill|run|play|pause|skip)\b", re.I),
    re.compile(r"^(what|who|when|where|why|how|is|are|do|does|did|can|could|"
               r"will|would|should)\b", re.I),
    re.compile(r"^(now|later|today|tomorrow|then|next)\b", re.I),
    re.compile(r"\b(on the screen|right now|at the moment|currently)\b", re.I),
)

# Low-value chatter (mirrors BrainMemory's own rule).
_CHATTER = re.compile(
    r"^(ok|okay|thanks|thank you|yes|no|good|great|nice|perfect|understood|"
    r"yeah|sure|alright|fine|done|got it|hey|hi|hello|jarvis|cool|awesome)"
    r"[\s\.!]*$", re.I,
)


def classify_scope(content: str, *, explicit: bool = False,
                   project_id: str = "") -> Candidate:
    """One candidate fact classified into its scope."""
    text = (content or "").strip()
    if not text:
        return Candidate(content=text, reason="empty")
    if _CHATTER.match(text) and not explicit:
        return Candidate(text, reason="chatter")
    if any(p.search(text) for p in _EPHEMERAL_PATTERNS) and not explicit:
        return Candidate(text, scope=Scope.SESSION, reason="ephemeral command/question")
    if explicit or any(p.search(text) for p in _DURABLE_PATTERNS):
        scope = Scope.PROJECT if project_id else Scope.LONG_TERM
        category = next((cat for pattern, cat in _DURABLE_CATEGORY
                         if pattern.search(text)), "")
        return Candidate(text, scope=scope, category=category, explicit=explicit,
                         project_id=project_id, reason="durable fact")
    # Unclassified statements stay session-scoped: conservative by default —
    # the local store's own value classifier still gets a say at write time.
    return Candidate(text, scope=Scope.SESSION, reason="unclassified statement")


def promote(content: str, *, explicit: bool = False,
            project_id: str = "", backend: MemoryBackend | None = None) -> dict:
    """Gate one candidate through the policy; store when durable.

    Returns {stored: bool, scope: str, reason: str, backend: str}. Never
    raises: a broken backend or policy bug degrades to a no-op, not an error
    in the conversation path.
    """
    try:
        cand = classify_scope(content, explicit=explicit, project_id=project_id)
        if cand.scope not in (Scope.LONG_TERM, Scope.PROJECT):
            return {"stored": False, "scope": cand.scope.value,
                    "reason": cand.reason or "not durable"}
        be = backend or get_memory_backend()
        ok = be.write(content, category=cand.category,
                      project_id=cand.project_id, explicit=cand.explicit)
        if ok:
            # A fact that was really stored is what moves the growth meter —
            # on the original store and the vault alike.
            try:
                from memory import growth
                growth.record(backend=getattr(be, "name", "local"),
                              category=cand.category)
            except Exception:
                pass
        return {"stored": bool(ok), "scope": cand.scope.value,
                "reason": cand.reason, "backend": getattr(be, "name", "local")}
    except Exception as exc:
        return {"stored": False, "scope": "unknown", "reason": f"policy error: {exc}"}


def recall(query: str, limit: int = 8, backend: MemoryBackend | None = None) -> list[dict]:
    """Backend-agnostic recall (falls back to an empty list, never raises)."""
    try:
        be = backend or get_memory_backend()
        return be.search(query, limit=limit)
    except Exception:
        return []


# ── identity slots: what makes a restatement an update ──────────────────────
#: A slot is the single thing a durable statement is about. "My name is X" and
#: "actually, call me Y" fill the same slot, so the newer wording replaces the
#: older fact instead of piling up beside it. General preferences and ordinary
#: statements have no slot: a second preference is a new fact, not a correction.
_SLOT_PATTERNS = (
    ("address.name", re.compile(
        r"\b(my name|my preferred name|i go by|"
        r"(?:you can |please |just )?call me)\b", re.I)),
    ("home", re.compile(r"\bmy (home|house|apartment|flat)\b", re.I)),
    ("office", re.compile(r"\bmy (office|desk|workplace)\b", re.I)),
    ("job", re.compile(r"\bmy (job|role|title|company|team|manager)\b", re.I)),
    ("vehicle", re.compile(r"\bmy (car|bike|motorcycle|vehicle)\b", re.I)),
    ("pet", re.compile(r"\bmy (dog|cat|pet|pets)\b", re.I)),
)

#: "my favourite <thing> is …" — one slot per thing, so a changed favourite
#: updates that thing and leaves every other favourite alone.
_RE_FAVOURITE = re.compile(r"\bmy (?:favourite|favorite) ([a-z ]{3,24}?) is\b", re.I)


def slot_key(text: str) -> str:
    """The identity slot a statement fills ('' when it fills none).

    Deterministic and narrow on purpose: only slots where a later statement is
    genuinely the same fact re-stated are keyed, so nothing is merged by
    guesswork and no memory is lost to a similarity score.
    """
    value = str(text or "")
    for key, pattern in _SLOT_PATTERNS:
        if pattern.search(value):
            return key
    match = _RE_FAVOURITE.search(value)
    if match:
        thing = " ".join(match.group(1).split()).lower()
        if thing:
            return f"favourite:{thing}"
    return ""
