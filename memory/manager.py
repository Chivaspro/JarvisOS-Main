"""Brain memory manager - the high-level, persistent cognitive memory.

Provides:
* Categorised, metadata-rich memories (confidence, importance, status, etc.)
* Smart extraction heuristics (what deserves becoming memory)
* Retrieval with ranking (recency, importance, confidence, access)
* Confidence dynamics (repeat confirm raises, contradiction supersedes)
* Consolidation (merge duplicates, generalise, mark obsolete, decay)
* Task memory ("continue where we left off")
* Experience + workflow memory (learning from success/failure)
* Corrections (learning from user corrections)
* Privacy (privacy_level, local-only mode, clear/export/import)
* A memory graph for the 3D brain visualisation

This is an additive layer. The existing JSON memory store (
memory/memory_manager.py) used by main.py for the system prompt is left
untouched - callers can use either or both.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Iterable

from memory.database import BrainDatabase
from memory.embeddings import embed
from memory import privacy as _privacy

# ── Categories (spec §3) ──────────────────────────────────────────────────────
CATEGORIES = {
    "PROFILE": "who the user is",
    "PREFERENCES": "how the user wants things done",
    "PROJECTS": "the user's projects and goals",
    "CONVERSATIONS": "notable past conversations",
    "EPISODES": "important events/experiences",
    "TASKS": "ongoing or past tasks",
    "EVENTS": "scheduled/calendar-type events",
    "TOOLS": "facts about tools",
    "SOLUTIONS": "known fixes that worked",
    "ERRORS": "approaches that failed",
    "WORKFLOWS": "sequences that consistently worked",
    "ENVIRONMENT": "facts about the user's machine/space",
    "CONTEXT": "short-lived situational context",
    "RELATIONSHIPS": "people/objects and their links",
}

# ── Category keywords for extraction classification ───────────────────────────
_CAT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "PREFERENCES": ("prefer", "like", "don't like", "instead", "rather", "favorite", "favourite",
                    "always use", "never use", "habit", "usually"),
    "PROJECTS": ("project", "repo", "repository", "app", "software", "building", "developing"),
    "SOLUTIONS": ("fixed", "fix", "solved", "solves", "solution", "worked", "resolved"),
    "ERRORS": ("failed", "error", "bug", "broke", "crash", "not working", "issue"),
    "WORKFLOWS": ("workflow", "steps", "process", "sequence", "procedure", "how to"),
    "ENVIRONMENT": ("computer", "machine", "pc", "laptop", "windows", "install", "installed"),
    "RELATIONSHIPS": ("my mother", "my father", "my sister", "my brother", "my wife", "my husband",
                      "my friend", "his", "her", "my colleague"),
    "TASKS": ("working on", "todo", "next step", "task", "we were", "continue"),
    "EVENTS": ("tomorrow", "today at", "appointment", "meeting", "deadline", "birthday"),
    "PROFILE": ("i am", "my name", "i live", "i work", "i study", "my job", "i was born"),
}

_PRIVACY_LEVELS = ("normal", "personal", "sensitive")

# Heuristic: how long before a CONTEXT memory expires (seconds)
_CONTEXT_TTL = 60 * 60 * 24 * 3  # 3 days

_WORKFLOW_MIN_REPEATS = 2


def _plain_text(text: str) -> str:
    """Lowercased words only — the shape two restatements are compared in.

    Punctuation and casing must not decide whether one memory replaces another:
    "My name is X." and "my name is X" are the same statement.
    """
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _orb_event(kind: str, count: int = 1) -> None:
    """Tell live listeners (the memory orb) that a memory moved. Never raises:
    a headless run simply skips the visual side-effect."""
    try:
        from dashboard.brain3d import emit_memory_event
        emit_memory_event(kind, count)
    except Exception:
        pass


class BrainMemory:
    """Thread-safe facade over BrainDatabase with the intelligence layered on top."""

    def __init__(self, db: BrainDatabase | None = None, db_path=None):
        self._db = db or BrainDatabase(path=db_path)
        self._lock = threading.RLock()
        self._enabled = self._db.kv_get("memory_enabled", True)
        self._start_session()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _start_session(self) -> None:
        try:
            self._session_id = self._db.start_session()
        except Exception:
            self._session_id = None

    def close(self, summary: str = "", open_threads=None) -> None:
        try:
            if self._session_id:
                self._db.close_session(self._session_id, summary,
                                       open_threads=open_threads)
        except Exception:
            pass

    @property
    def db(self) -> BrainDatabase:
        return self._db

    # ── privacy & toggles ─────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._db.kv_set("memory_enabled", self._enabled)

    def is_enabled(self) -> bool:
        return bool(self._enabled)

    def privacy_default(self) -> str:
        return str(self._db.kv_get("privacy_default", "normal"))

    def set_privacy_default(self, level: str) -> None:
        if level not in _PRIVACY_LEVELS:
            level = "normal"
        self._db.kv_set("privacy_default", level)

    # ── core write path ───────────────────────────────────────────────────────

    def remember(
        self,
        content: str,
        category: str | None = None,
        source: str = "conversation",
        importance: float = 0.5,
        confidence: float = 0.6,
        project_id: str = "",
        tags: Iterable[str] = (),
        privacy_level: str | None = None,
        explicit: bool = False,
    ) -> int | None:
        """Store one memory. Returns the new id, or None when skipped (disabled/
        empty/low-value). `explicit=True` honours 'remember this' commands even
        for content that would otherwise be judged low-value."""
        content = (content or "").strip()
        if not content:
            return None
        if not self._enabled:
            return None
        if not explicit and self._classify_value(content) == "low-value":
            return None
        # Never store obvious secrets verbatim - redact credential-looking
        # tokens before this content touches the database (privacy.py).
        content = _privacy.redact(content)
        if not content:
            return None

        category = (category or self._classify_category(content)).upper()
        if category not in CATEGORIES:
            category = "CONTEXT"
        if confidence is None:
            confidence = 0.6 if not explicit else 0.85
        privacy_level = privacy_level or self.privacy_default()
        try:
            emb = embed(content)
        except Exception:
            emb = None
        now = time.time()
        norm = re.sub(r"\s+", " ", content.strip().lower())

        # Deduplication: near-identical existing active memory updates it instead.
        dup = self._find_duplicate(content)
        if dup:
            new_conf = min(1.0, float(dup["confidence"]) + 0.12)
            existing = re.sub(r"\s+", " ",
                              str(dup.get("content") or "").strip().lower())
            if existing and norm and norm != existing and norm in existing:
                # A restatement that adds nothing: reinforce the stored fact and
                # keep its fuller wording instead of shrinking it.
                self._db.update_memory(dup["id"], {
                    "updated_at": now, "confidence": new_conf, "relevance": 1.0,
                })
                self._db.touch_memory(dup["id"])
                _orb_event("remember")
                return dup["id"]
            self._db.update_memory(dup["id"], {
                "content": content, "category": category, "updated_at": now,
                "confidence": new_conf, "importance": max(float(dup["importance"]), importance),
                "relevance": 1.0,
            })
            self._db.touch_memory(dup["id"])
            _orb_event("remember")
            return dup["id"]

        # Identity slots: "my name is X" then "actually, call me Y" is one fact
        # being updated, not two facts. The previous row is superseded (kept in
        # history, out of active recall) so the newest wording is what answers.
        slot = ""
        try:
            from memory.policy import slot_key
            slot = slot_key(content)
        except Exception:
            slot = ""
        tag_list = list(tags)
        previous: dict | None = None
        if slot:
            tag = f"slot:{slot}"
            if tag not in tag_list:
                tag_list.append(tag)
            for m in self._db.iterate_active():
                if tag in [str(t) for t in (m.get("tags") or [])]:
                    previous = m
                    break
            if previous is not None:
                old_text = _plain_text(str(previous.get("content") or ""))
                new_text = _plain_text(content)
                if new_text and old_text and new_text != old_text and new_text in old_text:
                    # The restatement says LESS than what is already stored: the
                    # richer wording stays and this counts as reinforcement.
                    new_conf = min(1.0, float(previous.get("confidence") or 0.6) + 0.12)
                    self._db.update_memory(previous["id"], {
                        "updated_at": now, "confidence": new_conf, "relevance": 1.0,
                    })
                    self._db.touch_memory(previous["id"])
                    _orb_event("remember")
                    return previous["id"]

        mid = self._db.insert_memory({
            "category": category, "content": content, "source": source,
            "importance": importance, "confidence": confidence, "relevance": 1.0,
            "project_id": project_id, "tags": tag_list,
            "expiration": (now + _CONTEXT_TTL) if category == "CONTEXT" else 0,
            "privacy_level": privacy_level, "embedding": emb,
        })
        if previous is not None and mid:
            try:
                self._db.mark_superseded(previous["id"], mid)
            except Exception:
                pass
        _orb_event("remember")
        return mid

    def _find_duplicate(self, content: str) -> dict | None:
        """The existing memory this content already *is*.

        Literal repetition and containment only — no similarity score, because
        a score cannot tell "the same fact said again" from "the same shape,
        different value", and merging those silently loses a real memory.
        Superseding for identity slots is handled by ``remember`` instead,
        where the slot a statement fills is known precisely.
        """
        norm = re.sub(r"\s+", " ", content.strip().lower())
        for m in self._db.iterate_active():
            other = re.sub(r"\s+", " ", (m.get("content") or "").strip().lower())
            if not other:
                continue
            if other == norm or (len(norm) > 12 and other in norm):
                return m
            if len(other) > 12 and norm in other:
                # The new statement says LESS than what is already stored; that
                # is a confirmation, and the richer wording is kept.
                return m
        return None

    # ── classification (spec §4) ─────────────────────────────────────────────

    def _classify_value(self, content: str) -> str:
        low = re.compile(
            r"^(ok|okay|thanks|thank you|yes|no|good|great|nice|perfect|understood|"
            r"yeah|sure|alright|fine|done|got it|hey|hi|hello|jarvis"
            r"|ok thanks|ok thank you|okay thanks|yeah sure|alright thanks|thanks a lot"
            r"|thank you very much|no problem|of course|cool|awesome)[\s\.!]*$",
            re.IGNORECASE,
        )
        if low.match(content.strip()):
            return "low-value"
        if content.startswith(("remember", "don't forget", "don't remember")) \
                or "remember this" in content.lower():
            return "permanent"
        if any(w in content.lower() for w in ("fixed", "solved", "worked", "failed")):
            return "useful-solution"
        return "possible"

    def _classify_category(self, content: str) -> str:
        text = content.lower()
        best, best_score = "CONTEXT", 0
        for cat, words in _CAT_KEYWORDS.items():
            score = sum(1 for w in words if w in text)
            if score > best_score:
                best, best_score = cat, score
        return best

    # ── corrections (spec §10) ────────────────────────────────────────────────

    def learn_correction(self, original: str, corrected: str, category: str = "") -> int:
        """Record a user correction. High confidence by definition. Optionally
        supersede an existing contradicting memory when one is found."""
        cid = self._db.insert_correction({
            "original": original, "corrected": corrected,
            "category": category, "confidence": 0.95,
        })
        # If an active memory matches the OLD claim, supersede it with the new one.
        old = self._find_duplicate(original)
        new_id = self.remember(corrected, confidence=0.95, explicit=True)
        if old and new_id and old["id"] != new_id:
            self._db.mark_superseded(old["id"], new_id)
        return cid

    def recent_corrections(self, limit: int = 20) -> list[dict]:
        return self._db.list_corrections(limit)

    # ── associations: the memory web (spec §8) ───────────────────────────────

    _STOP = frozenset(
        "the a an and or of to in on at for with is are was were be been this that "
        "it its as by from not you your i my me we they he she but if then will "
        "would can could should do does did have has had say said there here about "
        "into over under more most very just also than"
        .split())

    @classmethod
    def _keywords(cls, text: str, limit: int = 10) -> set:
        """Significant words of a memory, in first-seen order (stable)."""
        words = re.findall(r"[a-z0-9_]{4,}", (text or "").lower())
        out: list[str] = []
        for w in words:
            if w in cls._STOP or w in out:
                continue
            out.append(w)
            if len(out) >= limit:
                break
        return set(out)

    def auto_link(self, limit: int = 150, min_shared: int = 2, max_links: int = 400) -> int:
        """Link memories that genuinely overlap, so the brain graph is a web.

        Only literal overlap counts: two memories need at least `min_shared`
        significant words in common before a 'related' edge is created. No
        embedding guesswork, no invented connections. Idempotent (the store
        ignores an edge that already exists).
        """
        with self._lock:
            rows = [m for m in list(self._db.iterate_active())[:max(1, int(limit))]
                    if (m.get("content") or "").strip()]
            keys = {m["id"]: self._keywords(m.get("content", "")) for m in rows}
            made = 0
            for i, a in enumerate(rows):
                ka = keys.get(a["id"]) or set()
                if len(ka) < min_shared:
                    continue
                for b in rows[i + 1:]:
                    kb = keys.get(b["id"]) or set()
                    if len(kb) < min_shared:
                        continue
                    shared = ka & kb
                    if len(shared) < min_shared:
                        continue
                    weight = min(1.0, 0.25 + len(shared) / 8.0)
                    try:
                        self._db.add_relationship(a["id"], b["id"], "related", weight)
                    except Exception:
                        continue
                    made += 1
                    if made >= max_links:
                        return made
            return made

    def associations(self, mid: int, limit: int = 6) -> list[dict]:
        """Memories linked to `mid`, strongest link first (for prompts)."""
        try:
            rels = sorted(self._db.relationships_of(mid),
                          key=lambda r: float(r.get("weight", 0) or 0), reverse=True)
        except Exception:
            return []
        out: list[dict] = []
        for r in rels[:max(1, int(limit))]:
            try:
                m = self._db.get_memory(r["other"])
            except Exception:
                m = None
            if not m or m.get("deleted"):
                continue
            out.append({"type": r.get("type", "related"),
                        "weight": float(r.get("weight", 0) or 0),
                        "content": m.get("content", ""),
                        "category": m.get("category", "")})
        return out

    # ── retrieval (spec §7) ───────────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        limit: int = 8,
        project_id: str = "",
        min_confidence: float = 0.0,
    ) -> list[dict]:
        """Rank active memories against `query`. Lexical scoring is cheap and
        deterministic; results are the 'smallest useful context' for prompts."""
        with self._lock:
            words = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]
            scored = []
            now = time.time()
            for m in self._db.iterate_active():
                conf = float(m["confidence"])
                if conf < min_confidence:
                    continue
                if project_id and m.get("project_id") and m["project_id"] != project_id:
                    continue
                s = self._score_memory(words, m, now)
                if s > 0:
                    scored.append((s, m))
            scored.sort(key=lambda t: t[0], reverse=True)
            top = [m for _s, m in scored[:limit]]
            for m in top:
                self._db.touch_memory(m["id"])
            # Recall-driven reinforcement: every hit that actually gets used
            # becomes slightly more confident, so memories the user keeps
            # querying outrank one-off context over time (spec §7 ranking).
            if top:
                bump = min(1.0, float(top[0]["confidence"]) + 0.02)
                if bump > float(top[0]["confidence"]):
                    self._db.update_memory(top[0]["id"], {"confidence": bump})
            return top

    def recall_text(self, query: str, limit: int = 8) -> str:
        rows = self.retrieve(query, limit=limit)
        if not rows:
            return f"Nothing stored about '{query}' in the brain memory."
        lines = [f"{m['category']}/{m['id']} [{m['confidence']:.2f}] {m['content']}"
                 for m in rows]
        return "Brain memory results:\n" + "\n".join(lines)

    @staticmethod
    def _score_memory(words: list[str], m: dict, now: float) -> float:
        text = f"{m.get('category', '')} {m.get('content', '')}".lower()
        content = (m.get("content") or "").lower()
        s = 0.0
        for w in words:
            if w in content:
                s += 3.0
            if w in m.get("category", "").lower():
                s += 1.0
        recency = _sigmoid((now - float(m.get("updated_at", now))) / 86400.0, k=-0.25)
        access = _sigmoid(float(m.get("access_count", 0)) / 20.0, k=1.0)
        s *= (0.5 + 0.5 * float(m.get("importance", 0.5)))
        s *= (0.4 + 0.6 * float(m.get("confidence", 0.6)))
        s += recency * 2.0 + access * 0.5
        return s

    def latest_context(self, limit: int = 6) -> list[dict]:
        return self._db.list_memories(category="CONTEXT", limit=limit)

    # ── confidence & contradiction (spec §5) ─────────────────────────────────

    def reinforce(self, content: str, delta: float = 0.12) -> int | None:
        dup = self._find_duplicate(content)
        if not dup:
            return self.remember(content, explicit=True)
        new_conf = min(1.0, float(dup["confidence"]) + delta)
        self._db.update_memory(dup["id"], {"confidence": new_conf})
        return dup["id"]

    def contradict(self, content: str, replacement: str | None = None) -> None:
        dup = self._find_duplicate(content)
        if not dup:
            return
        if replacement:
            new_id = self.remember(replacement, explicit=True)
            if new_id:
                self._db.mark_superseded(dup["id"], new_id)
        else:
            self._db.update_memory(dup["id"], {"confidence": max(0.05, float(dup["confidence"]) * 0.4)})

    # ── maintenance & consolidation (spec §6) ────────────────────────────────

    def purge_expired(self) -> int:
        now = time.time()
        removed = 0
        for m in self._db.iterate_active():
            exp = float(m.get("expiration", 0))
            if exp and now > exp:
                self._db.mark_deleted(m["id"])
                removed += 1
        return removed

    def decay(self, half_life_days: float = 180.0, min_conf: float = 0.2) -> None:
        """Slowly lower confidence of old, rarely-accessed memories."""
        now = time.time()
        for m in self._db.iterate_active():
            age_days = (now - float(m.get("updated_at", now))) / 86400.0
            if age_days < 14 or float(m["access_count"]) >= 5:
                continue
            factor = 0.5 ** (age_days / half_life_days)
            new_conf = max(min_conf, float(m["confidence"]) * factor)
            self._db.update_memory(m["id"], {"confidence": new_conf})

    def consolidate(self) -> dict:
        """Merge near-duplicate active memories; generalise repeated related
        memories; drop superseded context. Returns a small stats report."""
        stats = {"merged": 0, "generalised": 0, "expired": 0, "active": 0}
        seen: list[dict] = []
        for m in list(self._db.iterate_active()):
            norm = re.sub(r"\s+", " ", (m.get("content") or "").lower())
            for other in seen:
                on = re.sub(r"\s+", " ", (other.get("content") or "").lower())
                if not on or not norm:
                    continue
                if norm == on and other["id"] != m["id"]:
                    # identical duplicate - keep the richer one
                    keeper, dropper = (m, other) if float(other["confidence"]) < float(m["confidence"]) else (other, m)
                    self._db.mark_superseded(dropper["id"], keeper["id"])
                    self._db.update_memory(keeper["id"], {
                        "confidence": min(1.0, float(keeper["confidence"]) + 0.05),
                        "importance": max(float(keeper["importance"]), float(dropper["importance"])),
                    })
                    stats["merged"] += 1
                elif norm in on or on in norm and len(on) > 18 and len(norm) > 18:
                    if on in norm:
                        self._db.mark_superseded(other["id"], m["id"])
                    else:
                        self._db.mark_superseded(m["id"], other["id"])
                    stats["generalised"] += 1
            seen.append(m)
        stats["expired"] = self.purge_expired()
        # Refresh the association web while we are already walking every active
        # memory: this is what makes the 3D brain show relationships between
        # memories instead of a star of spokes from the core.
        try:
            stats["linked"] = self.auto_link()
        except Exception:
            stats["linked"] = 0
        stats["active"] = sum(self._db.count().values())
        return stats

    # ── beliefs (what JARVIS holds as true) ───────────────────────────────────

    def remember_belief(self, statement: str, confidence: float | None = None,
                        importance: float = 0.5, evidence: int = 1,
                        source: str = "reflection") -> int | None:
        """Store or reinforce one belief, keyed on the exact statement.

        A belief is deliberately NOT a memory: it is the generalised thing
        JARVIS has come to hold, with its own confidence that rises with
        repeated evidence and falls when contradicted. Seeing the same
        statement again strengthens the existing belief instead of duplicating
        it, so the Mind panel shows one line per belief, not a hundred echoes.
        """
        text = _privacy.redact((statement or "").strip())
        if not text:
            return None
        if not self._enabled:
            return None
        with self._lock:
            existing = self._db.find_belief(text)
            if existing:
                new_conf = float(existing.get("confidence", 0.6))
                if confidence is not None:
                    new_conf = max(new_conf, float(confidence))
                self._db.update_belief(existing["id"], {
                    "confidence": min(1.0, new_conf + 0.1),
                    "importance": max(float(existing.get("importance", 0.5)), importance),
                    "evidence": int(existing.get("evidence", 0)) + max(1, int(evidence)),
                    "status": "active",
                })
                return existing["id"]
            return self._db.insert_belief({
                "statement": text,
                "confidence": 0.6 if confidence is None else float(confidence),
                "importance": importance, "evidence": max(1, int(evidence)),
                "source": source,
            })

    def adjust_belief(self, statement: str, delta: float = 0.0,
                      evidence: int = 0) -> bool:
        """Nudge one belief's confidence; a belief that falls to ~0 is retired."""
        with self._lock:
            existing = self._db.find_belief(statement)
            if not existing:
                return False
            conf = max(0.0, min(1.0, float(existing.get("confidence", 0.6)) + float(delta)))
            status = "active" if conf > 0.05 else "retired"
            self._db.update_belief(existing["id"], {
                "confidence": conf,
                "evidence": int(existing.get("evidence", 0)) + max(0, int(evidence)),
                "status": status,
            })
            return True

    def beliefs(self, status: str = "active", limit: int = 100) -> list[dict]:
        return self._db.list_beliefs(status=status, limit=limit)

    def forget_belief(self, statement_or_id) -> bool:
        """Forget a belief by id or by its exact statement."""
        with self._lock:
            if isinstance(statement_or_id, int) or str(statement_or_id).isdigit():
                self._db.delete_belief(int(statement_or_id))
                return True
            existing = self._db.find_belief(str(statement_or_id))
            if not existing:
                return False
            self._db.delete_belief(existing["id"])
            return True

    def consolidate_beliefs(self, min_evidence: int = 2) -> int:
        """Derive beliefs from the memories that genuinely support one.

        A belief is only formed when at least ``min_evidence`` active memories
        share a significant subject (never from a single event, spec §13). The
        statement is the strongest evidence sentence; the confidence grows with
        how many memories back it and how confident they are.
        """
        with self._lock:
            rows = [m for m in self._db.iterate_active()
                    if (m.get("content") or "").strip()]
            groups: dict[str, list[dict]] = {}
            for m in rows:
                for kw in self._keywords(m.get("content", ""), limit=6):
                    groups.setdefault(kw, []).append(m)
            made = 0
            for _kw, members in groups.items():
                if len(members) < max(2, int(min_evidence)):
                    continue
                # The best member is the one with the highest confidence: its own
                # sentence is the belief's statement (no invented wording).
                best = max(members, key=lambda m: float(m.get("confidence", 0)))
                statement = (best.get("content") or "").strip()
                if not statement:
                    continue
                conf = min(1.0, 0.45 + 0.15 * len(members))
                self.remember_belief(
                    statement, confidence=conf,
                    importance=max(float(m.get("importance", 0.5)) for m in members),
                    evidence=len(members), source="consolidation")
                made += 1
            return made

    # ── dreams (JARVIS's forward-looking candidates) ──────────────────────────

    def dream(self, title: str, narrative: str = "",
              kind: str = "aspiration", source: str = "reflection") -> int | None:
        """Record one dream (an aspiration, plan or reflection). Never a promise."""
        clean = (title or "").strip()
        if not clean:
            return None
        with self._lock:
            existing = self._db.find_dream(clean)
            if existing:
                if narrative:
                    self._db.update_dream(existing["id"], {
                        "narrative": narrative, "status": "active"})
                return existing["id"]
            return self._db.insert_dream({
                "title": clean, "narrative": (narrative or "").strip(),
                "kind": kind, "source": source,
            })

    def dreams(self, status: str = "active", limit: int = 100) -> list[dict]:
        return self._db.list_dreams(status=status, limit=limit)

    def forget_dream(self, title_or_id) -> bool:
        with self._lock:
            if isinstance(title_or_id, int) or str(title_or_id).isdigit():
                self._db.delete_dream(int(title_or_id))
                return True
            existing = self._db.find_dream(str(title_or_id))
            if not existing:
                return False
            self._db.delete_dream(existing["id"])
            return True

    def generate_dreams(self, limit: int = 6) -> int:
        """Synthesise dreams from real, unfinished material only.

        Sources, in order of priority: open tasks with remaining steps, then
        wishes/goals, then projects. Each dream cites what it grew from
        (``source``), so nothing appears in the Mind panel without a reason, and
        a dream is never presented as an accomplished fact.
        """
        made = 0
        with self._lock:
            for t in self.active_tasks():
                title = f"Finish: {str(t.get('title') or '').strip()[:70]}"
                remaining = [str(s) for s in (t.get("steps_remaining") or []) if str(s).strip()]
                narrative = ("Open work carried over — next steps: "
                             + ("; ".join(remaining[:4]) if remaining else "none listed"))
                if self.dream(title, narrative, kind="plan",
                              source=f"task:{t.get('id')}"):
                    made += 1
                if made >= limit:
                    return made
            for m in self._db.iterate_active():
                cat = str(m.get("category") or "").upper()
                if cat not in ("PROJECTS", "EVENTS", "CONTEXT", "TASKS"):
                    continue
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                title = f"Pursue: {content[:70]}"
                if self.dream(title, content, kind="aspiration",
                              source=f"memory:{m.get('id')}"):
                    made += 1
                if made >= limit:
                    return made
        return made

    def reflect(self, min_evidence: int = 2) -> dict:
        """The dream cycle: consolidate beliefs, then generate dreams.

        Cheap, deterministic and off any user turn — it reads what is already
        stored and writes beliefs/dreams plus the association web. Safe to call
        on a worker thread; never touches the model and never blocks.
        """
        stats = {"beliefs": 0, "dreams": 0, "linked": 0}
        try:
            stats["beliefs"] = self.consolidate_beliefs(min_evidence=min_evidence)
        except Exception:
            stats["beliefs"] = 0
        try:
            stats["dreams"] = self.generate_dreams()
        except Exception:
            stats["dreams"] = 0
        try:
            stats["linked"] = self.auto_link()
        except Exception:
            stats["linked"] = 0
        return stats

    def unfinished_work(self, limit: int = 6) -> list[str]:
        """The things genuinely left open — what "continue" should pick up.

        Drawn from live tasks with remaining steps and TASKS memories. This is
        the same evidence ``generate_dreams`` and the session's open threads
        use, so the panel, the prompt and the spoken answer agree.
        """
        out: list[str] = []
        try:
            for t in self.active_tasks():
                title = str(t.get("title") or "").strip()
                if not title:
                    continue
                remaining = [str(s).strip() for s in (t.get("steps_remaining") or [])
                             if str(s).strip()]
                line = title
                if remaining:
                    line += " — next: " + "; ".join(remaining[:3])
                if line not in out:
                    out.append(line)
                if len(out) >= limit:
                    return out
        except Exception:
            pass
        try:
            for m in self._db.iterate_active():
                if str(m.get("category") or "").upper() != "TASKS":
                    continue
                content = (m.get("content") or "").strip()
                if content and content not in out:
                    out.append(content)
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    # ── forgetting (spec §43, §46) ────────────────────────────────────────────

    def forget_id(self, mid: int) -> bool:
        m = self._db.get_memory(mid)
        if not m:
            return False
        self._db.mark_deleted(mid)
        return True

    def forget_content(self, content: str) -> bool:
        dup = self._find_duplicate(content)
        if not dup:
            return False
        self._db.mark_deleted(dup["id"])
        return True

    def forget_query(self, query: str = "", hard: bool = False) -> int:
        """Forget memories matching `query` (LIKE scan). Empty query clears all.
        Returns how many memories were forgotten."""
        if not (query or "").strip():
            n = 0
            for m in self._db.list_memories(limit=100000):
                if m.get("deleted"):
                    continue
                (self._db.delete_memory(m["id"]) if hard
                 else self._db.mark_deleted(m["id"]))
                n += 1
            return n
        n = 0
        needle = f"%{(query or '').strip()[:200]}%"
        for m in self._db.list_memories(limit=100000):
            if m.get("deleted"):
                continue
            hay = (m.get("content") or "") + " " + (m.get("category") or "")
            if (needle.casefold() in f"%{hay}%".casefold()
                    or (needle[1:-1] or "").casefold() in hay.casefold()):
                (self._db.delete_memory(m["id"]) if hard
                 else self._db.mark_deleted(m["id"]))
                n += 1
        return n

    def clear_category(self, category: str, hard: bool = False) -> int:
        n = 0
        for m in self._db.list_memories(category=category, limit=10000):
            if hard:
                self._db.delete_memory(m["id"])
            else:
                self._db.mark_deleted(m["id"])
            n += 1
        return n

    def clear_all(self, hard: bool = False) -> None:
        self.clear_category("", hard=hard)

    def export(self, dest=None):
        return self._db.export_json(dest)

    def backup(self, dest=None):
        return self._db.backup(dest)

    # ── task memory (spec §39) ────────────────────────────────────────────────

    def start_task(self, title: str, project_id: str = "", steps_remaining=None) -> int:
        return self._db.insert_task({
            "title": title, "project_id": project_id,
            "steps_remaining": steps_remaining or [],
        })

    def update_task(self, tid: int, **fields) -> None:
        self._db.update_task(tid, fields)

    def add_task_step_done(self, tid: int, step: str) -> None:
        t = self._db.get_task(tid)
        if not t:
            return
        done = list(t.get("steps_done", [])) + [step]
        rem = list(t.get("steps_remaining", []))
        if step in rem:
            rem.remove(step)
        self._db.update_task(tid, {"steps_done": done, "steps_remaining": rem})

    def task_status(self, tid: int) -> str:
        t = self._db.get_task(tid)
        return t["status"] if t else "unknown"

    def finish_task(self, tid: int, summary: str = "") -> None:
        self._db.update_task(tid, {"status": "done", "solutions": [summary] if summary else []})

    def active_tasks(self, project_id: str = "") -> list[dict]:
        return self._db.list_tasks(status="active", project_id=project_id)

    def task_context(self, project_id: str = "") -> str:
        tasks = self.active_tasks(project_id)
        if not tasks:
            return ""
        lines = []
        for t in tasks:
            lines.append(f"- {t['title']} ({t['status']})")
            for s in t.get("steps_done", []):
                lines.append(f"    done: {s}")
            for s in t.get("steps_remaining", []):
                lines.append(f"    next: {s}")
            for e in t.get("errors", [])[:3]:
                lines.append(f"    error: {e}")
            for s in t.get("solutions", [])[:3]:
                lines.append(f"    solution: {s}")
        return "Current work:\n" + "\n".join(lines)

    # ── experiences & workflows (spec §16, §11, §12) ─────────────────────────

    def record_experience(self, summary: str, kind: str = "experience",
                          outcome: str = "success", context: str = "", links=None) -> int:
        return self._db.insert_experience({
            "summary": summary, "kind": kind, "outcome": outcome,
            "context": context, "links": links or [],
        })

    def record_task_outcome(self, request: str, intent: str, tool: str,
                            ok: bool, ms: float, steps=None, error: str = "") -> None:
        self._db.record_tool_result(tool, ok, ms, intent)
        key = intent or request[:40]
        self._db.upsert_workflow({
            "intent_key": key, "steps": steps or [tool],
            "success_count": 1 if ok else 0, "fail_count": 0 if ok else 1,
            "description": tool,
        })
        if ok:
            self.record_experience(
                f"{request} succeeded using {tool} ({ms:.0f}ms).",
                kind="workflow", outcome="success", context=key,
            )
        elif error:
            self.record_experience(
                f"{request} failed using {tool}: {error[:200]}",
                kind="workflow", outcome="failure", context=key,
            )

    def preferred_tool(self, intent: str) -> str | None:
        """Adaptive tool selection: most successful tool for an intent."""
        stats = self._db.tool_stats(intent=intent)
        if not stats:
            return None
        best, best_score = None, -1.0
        for s in stats:
            total = s["success"] + s["failed"]
            if total < 2:
                continue  # need enough evidence (§13)
            score = s["success"] / total
            if score > best_score:
                best, best_score = s["tool_name"], score
        return best

    def tool_usage_report(self) -> str:
        stats = self._db.tool_stats()
        if not stats:
            return "No tool statistics recorded yet."
        lines = []
        for s in stats:
            total = s["success"] + s["failed"]
            if total == 0:
                continue
            rate = 100.0 * s["success"] / total
            lines.append(f"- {s['tool_name']}: {rate:.0f}% success ({s['success']} ok / {s['failed']} fail, intent='{s['intent_key'] or 'any'}')")
        if not lines:
            return "No tool statistics recorded yet."
        return "Tool performance:\n" + "\n".join(lines)

    def provider_report(self) -> str:
        stats = self._db.provider_stats()
        if not stats:
            return "No provider statistics recorded yet."
        lines = []
        for s in stats:
            total = s["success"] + s["failed"]
            if total == 0:
                continue
            avg = s["total_ms"] / total if s["total_ms"] else 0
            lines.append(f"- {s['provider_name']}: {100.0*s['success']/total:.0f}% success, avg {avg:.0f}ms")
        return "Provider performance:\n" + "\n".join(lines)

    # ── memory graph (spec §8, §35) ──────────────────────────────────────────

    def graph(self, depth: int = 200) -> dict:
        """Return {nodes:[{id,label,category,activity}], links:[{s,t,type,weight}]}
        built from real memories + relationships - never fabricated.

        Each memory node also carries its own ``content`` and timestamps so the
        Memory Core's detail card can describe the selected memory from the real
        record instead of guessing from the truncated label.
        """
        nodes: list[dict] = [{"id": 0, "label": "JARVIS CORE", "category": "CORE", "activity": 0.0}]
        links: list[dict] = []
        for m in list(self._db.iterate_active())[:depth]:
            nodes.append({
                "id": m["id"], "label": m["content"][:40], "category": m["category"],
                "content": m["content"],
                "importance": m["importance"], "confidence": m["confidence"],
                "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
                "activity": 1.0 if (time.time() - float(m.get("updated_at", 0))) < 3600 else 0.0,
            })
            links.append({"s": 0, "t": m["id"], "type": "has_memory", "weight": 0.6})
        ids = {n.get("id") for n in nodes}
        rels = self._db._query(
            "SELECT DISTINCT from_id AS a, to_id AS b, rel_type AS t, weight AS w "
            "FROM relationships WHERE from_id<>0 AND to_id<>0"
        )
        for r in rels:
            # Only draw edges whose BOTH ends are on screen, otherwise the orb
            # renders lines into empty space.
            if r["a"] not in ids or r["b"] not in ids:
                continue
            links.append({"s": r["a"], "t": r["b"], "type": r["t"], "weight": r["w"]})
        for t in self.active_tasks():
            nodes.append({"id": f"task_{t['id']}", "label": "TASK: " + t["title"][:32],
                          "category": "TASKS", "activity": 1.0})
            links.append({"s": 0, "t": f"task_{t['id']}", "type": "owns_task", "weight": 0.8})
        return {"nodes": nodes, "links": links}

    # ── reports for UI / prompts ─────────────────────────────────────────────

    def summary(self) -> str:
        counts = self._db.count()
        tasks = len(self.active_tasks())
        lines = [f"Brain memory: {len(counts.items()) or 0} categories active.",
                 "Active memories: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none",
                 f"Active tasks: {tasks}"]
        return "\n".join(lines)

    def learned_report(self) -> str:
        parts = []
        workflows = self._db.list_workflows(limit=5)
        if workflows:
            parts.append("Learned workflows:")
            for w in workflows:
                parts.append(f"- {w['intent_key']} ({w['success_count']} ok / {w['fail_count']} fail)")
        corr = self.recent_corrections(limit=5)
        if corr:
            parts.append("Learned corrections:")
            for c in corr:
                parts.append(f"- was '{c['original'][:40]}' now '{c['corrected'][:40]}'")
        exp = self._db.list_experiences(kind="workflow", outcome="success", limit=3)
        if exp:
            parts.append("Recent experience:")
            for e in exp:
                parts.append(f"- {e['summary'][:80]}")
        return "\n".join(parts) if parts else "I haven't learned enough yet to summarise."


def _sigmoid(x: float, k: float = 1.0) -> float:
    try:
        return 1.0 / (1.0 + (2.718281828459045 ** (k * x)))
    except Exception:
        return 0.5


# ── module-level singleton access ─────────────────────────────────────────────
_instances: dict[tuple, BrainMemory] = {}
_inst_lock = threading.Lock()


def get_brain_memory(db_path=None, singleton: bool = True) -> BrainMemory:
    key = (str(db_path or "default"),)
    if singleton:
        with _inst_lock:
            if key not in _instances:
                _instances[key] = BrainMemory(db_path=db_path)
            return _instances[key]
    return BrainMemory(db_path=db_path)