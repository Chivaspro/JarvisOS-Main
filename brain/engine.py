"""JARVIS Brain - the Cognitive Engine (spec §1, §41, §42, §45, §57).

Responsibilities:
* Coordinate subsystems (memory, personality, learning, config, external
  services) behind one interface.
* Route a request to the right subsystem instead of running everything
  through every tool (spec §1 / §32).
* Assemble "smart context": current request + relevant preference + project +
  tasks + recent solutions/errors (spec §41).
* Track current activity for the 3D brain graph (spec §36).
* Provide knowledge-boundary phrasing so JARVIS never fabricates memory
  (spec §45).
* Gracefully degrade when a subsystem fails (spec §50).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from memory.manager import BrainMemory, get_brain_memory
from brain.personality import Personality
from brain.learning import LearningEngine
from brain.context import ConversationContext, get_context

# Activity keys mirrored to the 3D visualisation via set_brain_activity().
ACTIVITIES = ("idle", "listening", "thinking", "speaking", "seeing",
              "searching", "remembering", "tool", "learning")

# Skill registry key → the subsystem name routing reports (legacy names kept so
# existing call-sites that switch on "web"/"files"/"vision" keep working).
_SKILL_SUBSYSTEM = {
    "engineering": "files", "debugging": "files", "ui_engineering": "ui",
    "system_engineering": "system", "monitoring": "system",
    "planning": "planning", "task_memory": "tasks", "verification": "tasks",
    "research": "web",
    "vision": "vision", "comparison": "vision",
    "memory": "memory", "files": "files", "documents": "files",
    "automation": "automation",
    "diagnostics": "system",
    "voice": "voice", "teaching": "chat", "conversation": "chat",
    "humor": "chat",
}


class Brain:
    """Central orchestrator. One instance shared app-wide (see get_brain)."""

    def __init__(self, memory: BrainMemory | None = None, store_name: str = "default"):
        self._memory = memory or get_brain_memory()
        self._lock = threading.RLock()
        self._personality = Personality(self._memory)
        self._learning = LearningEngine(self._memory)
        self._activity = "idle"
        self._activity_listeners: list[Callable[[str], None]] = []
        self._handlers: dict[str, Callable] = {}
        self._context = get_context()
        self._last_skills: list[str] = []

    # ── accessors ─────────────────────────────────────────────────────────────

    @property
    def memory(self) -> BrainMemory:
        return self._memory

    @property
    def personality(self) -> Personality:
        return self._personality

    @property
    def learning(self) -> LearningEngine:
        return self._learning

    @property
    def context(self) -> ConversationContext:
        """The live conversation ledger (subjects, entities, references)."""
        return self._context

    # ── conversation context (spec §2, §37) ───────────────────────────────────

    def note_turn(self, role: str, text: str) -> None:
        """Record one finished turn and harvest its entities. Never fatal."""
        try:
            self._context.add_turn(role, text)
        except Exception:
            pass

    def note_entity(self, kind: str, label: str, **kw) -> None:
        try:
            self._context.note_entity(kind, label, **kw)
        except Exception:
            pass

    def note_objects(self, objects) -> None:
        """Record a fresh vision scan so "the left one" has something real to
        point at on the next turn."""
        try:
            self._context.note_objects(objects)
        except Exception:
            pass

    def resolve(self, text: str) -> dict:
        """Resolve "this / that / the other one" against the ledger."""
        try:
            return self._context.resolve(text)
        except Exception:
            return {"references": [], "notes": [], "summary": ""}

    def register_handler(self, subsystem: str, fn: Callable) -> None:
        """Register a callable that handles a named capability, e.g.
        'vision', 'web', 'files'. Routing uses it when present; missing
        subsystems degrade gracefully instead of erroring."""
        with self._lock:
            self._handlers[subsystem] = fn

    def handler(self, subsystem: str) -> Callable | None:
        with self._lock:
            return self._handlers.get(subsystem)

    # ── activity feed (spec §36) ──────────────────────────────────────────────

    def set_activity(self, activity: str) -> None:
        with self._lock:
            if activity not in ACTIVITIES:
                activity = "idle"
            if activity == self._activity:
                return
            self._activity = activity
            listeners = list(self._activity_listeners)
        for fn in listeners:
            try:
                fn(activity)
            except Exception:
                pass

    def get_activity(self) -> str:
        with self._lock:
            return self._activity

    def on_activity(self, fn: Callable[[str], None]) -> None:
        with self._lock:
            self._activity_listeners.append(fn)

    # ── smart context (spec §41, §42) ─────────────────────────────────────────

    def _identity_facts(self, limit: int = 6) -> list[dict]:
        """PROFILE / PREFERENCES memories from the ACTIVE store.

        Identity is the one thing that must never depend on the wording of a
        question, so these are fetched by category rather than by keyword luck.
        """
        out: list[dict] = []
        seen: set = set()

        def _add(rows) -> None:
            for m in rows or []:
                category = str(m.get("category", "")).upper()
                content = str(m.get("content", "")).strip()
                if category not in ("PROFILE", "PREFERENCES") or not content:
                    continue
                key = content.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(m)
                if len(out) >= limit:
                    return

        try:
            _add(self._memory.retrieve("", limit=40))
        except Exception:
            pass
        if len(out) < limit:
            try:
                from memory.backend import recall_backend
                _add(recall_backend("my name I prefer I live I work call me", limit=16))
            except Exception:
                pass
        return out[:limit]

    def smart_context(self, request: str, project_id: str = "", limit: int = 10) -> str:
        """Assemble the smallest useful memory context for a request.

        Recall runs through the ACTIVE store (the Obsidian Vault when it is
        selected, the original memory otherwise), and the facts that identify
        the user always ride along: "What's my name?" must be answered from
        memory instead of from nothing.
        """
        parts: list[str] = []
        mem: list = []
        try:
            mem = list(self._memory.retrieve(request, limit=limit,
                                             project_id=project_id) or [])
        except Exception:
            mem = []
        try:
            if request:
                from memory.backend import recall_backend
                seen = {str(m.get("content", "")).strip().lower() for m in mem}
                for hit in (recall_backend(request, limit=limit) or []):
                    content = str(hit.get("content", "")).strip()
                    if content and content.lower() not in seen:
                        seen.add(content.lower())
                        mem.append(hit)
        except Exception:
            pass
        if mem:
            parts.append("RELEVANT MEMORY")
            for m in mem[:6]:
                conf = float(m.get("confidence", 0) or 0)
                parts.append(
                    f"- {m.get('category', 'MEMORY')}: {m.get('content', '')}"
                    + (f" (confidence {conf:.2f})" if conf and conf < 0.7 else "")
                )
        identity = self._identity_facts(limit=6)
        if identity:
            parts.append("WHO THE USER IS (stored facts — use them, "
                         "never say you do not know)")
            for m in identity:
                parts.append(f"- {m.get('content', '')}")
        try:
            tasks = self._memory.task_context(project_id)
            if tasks:
                parts.append(tasks)
        except Exception:
            pass
        try:
            sol = self._memory.db.list_experiences(kind="workflow", outcome="success", limit=3)
            if sol:
                parts.append("RECENT SOLUTIONS")
                for e in sol:
                    parts.append(f"- {e['summary'][:120]}")
        except Exception:
            pass
        return ("SMART CONTEXT\n" + "\n".join(parts)) if parts else ""

    def route_skills(self, request: str, features: dict | None = None,
                     limit: int = 4) -> list:
        """The skills this request needs, strongest first.

        Delegates to the one skill registry (brain/skills.py) so routing,
        the skill map in the prompt and the skill_query tool can never
        disagree with each other. Returns [] when the registry is unavailable.
        """
        try:
            from brain import skills as _skills
            picked = _skills.route(request, features=features or {},
                                   limit=limit, include_baseline=False)
            with self._lock:
                self._last_skills = [s.key for s in picked]
            return picked
        except Exception:
            return []

    def route(self, request: str, capabilities: dict | None = None) -> str:
        """Which subsystem(s) a request needs — local and instant, no model
        round trip. `capabilities` maps subsystem -> bool(enabled) to respect
        user toggles.

        Prefers the skill registry (deterministic word-boundary scoring) and
        falls back to the keyword table below only if the registry cannot load.
        """
        picked = self.route_skills(request, features=capabilities, limit=4)
        if picked:
            subs: list[str] = []
            for s in picked:
                name = _SKILL_SUBSYSTEM.get(s.key)
                if name and name not in subs:
                    subs.append(name)
            if subs:
                return ",".join(subs)
        return self._route_keywords(request, capabilities or {})

    def _route_keywords(self, request: str, caps: dict) -> str:
        """Fallback keyword router (kept for when skills.py cannot import)."""
        low = (request or "").lower()
        hits: list[str] = []

        def _want(cap: str, words: tuple[str, ...]) -> bool:
            if caps.get(cap) is False:
                return False
            return any(w in low for w in words)

        if _want("vision", ("see", "look", "holding", "objects", "faces",
                            "who is", "what is in", "camera", "screen", "this image")):
            hits.append("vision")
        if _want("voice", ("voice", "say", "speak", "talk", "listen")):
            hits.append("voice")
        if _want("web", ("search", "internet", "web", "online", "google", "website", "url")):
            hits.append("web")
        if _want("files", ("file", "folder", "open file", "directory", "path", "code file")):
            hits.append("files")
        if _want("memory", ("remember", "forget", "what do you", "what do you remember",
                            "do you know", "recall", "learned")):
            hits.append("memory")
        if _want("automation", ("automate", "workflow", "schedule", "cron", "click", "type", "run ")):
            hits.append("automation")
        if _want("system", ("system", "cpu", "ram", "gpu", "task manager", "process",
                            "temperature", "battery")):
            hits.append("system")
        if _want("tools", ("tool", "open", "app", "run", "install")):
            hits.append("tools")
        return ",".join(hits) if hits else "chat"

    # ── knowledge boundaries (spec §45) ───────────────────────────────────────

    @staticmethod
    def honesty_hint() -> str:
        return (
            "[KNOWLEDGE BOUNDARIES]\n"
            "Distinguish what you *know*, *remember*, *detect*, *infer* and "
            "do NOT know. Say 'I remember that...' only for stored memory; "
            "'I can currently see...' only for live sensing; 'It appears "
            "that...' for inference; otherwise say plainly you don't have "
            "enough information. Never invent memories.\n"
        )

    # ── prompt assembly ───────────────────────────────────────────────────────

    def build_context_block(self, request: str = "", project_id: str = "") -> str:
        parts = [self._personality.prompt_block()]
        try:
            convo = self._context.prompt_block()
            if convo:
                parts.append(convo)
        except Exception:
            pass
        if not request:
            # A session-level prompt build has no request of its own: the last
            # observed utterance is the best available "what is being asked".
            request = getattr(self, "_last_request", "") or ""
        smart = self.smart_context(request, project_id=project_id)
        if smart:
            parts.append(smart)
        parts.append(self.honesty_hint())
        return "\n".join(parts)

    # ── coordination hooks used by main.py (fire-and-forget, never fatal) ─────

    def observe(self, user_text: str) -> None:
        """Called on every user input: feed the learning loop opportunistically
        without blocking the conversation."""
        try:
            self._personality.observe_user_text(user_text)
        except Exception:
            pass
        try:
            self._last_request = user_text
        except Exception:
            pass
        try:
            self._learning.is_correction(user_text) or None
        except Exception:
            pass

    def auto_learn(self, request: str, outcome_ok: bool, summary: str = "", intent: str = "") -> None:
        """After a significant interaction (spec §42): record outcome + create/
        reinforce a workflow when there is a reusable tool sequence."""
        try:
            self._learning.record_experience(
                summary or (("Completed: " if outcome_ok else "Attempted: ") + request[:100]),
                kind="interaction", outcome="success" if outcome_ok else "failure",
                context=intent or request[:40],
            )
        except Exception:
            pass
        try:
            if outcome_ok:
                self._memory.consolidate()
        except Exception:
            pass

    def plan(self, request: str, features: dict | None = None) -> dict:
        """Turn a request into an ordered plan: skills, chain and next actions.

        Deterministic and offline — a model round trip is not needed to know
        that "look at this, fix it and test it" wants vision → engineering →
        debugging → verification. The plan is advisory context, not a
        fabricated promise of work done.
        """
        picked = self.route_skills(request, features=features, limit=4)
        keys = [s.key for s in picked]
        chain: list[str] = []
        try:
            from brain import skills as _skills
            chain = _skills.chains_for(keys, features=features or {})
        except Exception:
            chain = []
        resolved = ""
        try:
            resolved = self._context.resolve(request).get("summary", "")
        except Exception:
            resolved = ""
        # Step list: the picked skills in order, then the skills they usually
        # chain into. Planning itself is not a step — the plan *is* the plan —
        # and neither the same name nor an already-covered skill is repeated.
        steps: list[str] = []
        for s in picked:
            if s.key in ("planning", "task_memory") or s.name in steps:
                continue
            steps.append(s.name)
        for k in chain[:4]:
            name = _chain_name(k)
            if name not in steps:
                steps.append(name)
        return {
            "request": str(request or "")[:300],
            "skills": keys,
            "skill_names": [s.name for s in picked],
            "chain": chain,
            "steps": steps,
            "resolved_reference": resolved,
        }

    def shutdown(self, summary: str = "") -> None:
        try:
            self._memory.close(summary)
        except Exception:
            pass


def _chain_name(key: str) -> str:
    try:
        from brain.skills import get_skill
        s = get_skill(key)
        return s.name if s else key
    except Exception:
        return key


_singleton = None
_singleton_lock = threading.Lock()


def get_brain() -> Brain:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Brain()
        return _singleton


def reset_brain() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None