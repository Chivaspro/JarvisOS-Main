"""Persistent memory service — the backend that makes remember/forget real.

Why this module exists
----------------------
The assistant already had two memory stores (the JSON long-term store and the
SQLite Brain store), but neither kept the *conversation*: turns lived only in
an in-RAM list that died with the process, "forget" depended on the model
choosing to call a tool, and nothing prevented a forgotten topic from
resurfacing through an old summary or the prompt. This service adds the three
missing pieces:

* ConversationStore   — every turn archived to SQLite (survives restarts).
* MemoryDeletionManager — tombstones written by explicit "forget" commands;
  every read path consults them, and the matching archived text is pruned, so
  forgotten information cannot be resurrected.
* MemoryService.on_user_turn — deterministic command detection that executes
  remember/forget/search/recall in the backend BEFORE the model is involved.
  The model reports on the operation; it never decides whether it happened.

Retrieval ranking follows the spec: current conversation first (handled by
the live session), then explicitly saved memories, then recent relevant
history, then older matches — all filtered by tombstones.

Thread-safe; every public entry point degrades to a no-op on failure so the
assistant can never crash because its memory did.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any

from memory.database import BrainDatabase

# ── command detection ─────────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "about", "everything", "all", "of", "our", "my",
    "previous", "conversation", "conversation.", "that", "this", "it",
    "please", "what", "we", "were", "was", "working", "work", "on", "with",
}

_RE_FORGET = re.compile(
    r"^\s*(?:(?:please|now|also|and)\s+)?(?:\bi\s+(?:want|need|would)\s+you\s+to\s+)?"
    r"(?:forget|delete|remove|erase|drop|don'?t\s+remember|stop\s+remembering)\s+"
    r"(?:all\s+|everything\s+)?(?:about\s+|of\s+|that\s+)?"
    r"(?P<topic>.+?)[\s.!,]*$",
    re.IGNORECASE)
_RE_FORGET_IT = re.compile(
    r"^\s*(?:please\s+)?(?:forget\s+(?:this|that|it)|delete\s+that\s+memory"
    r"|remove\s+that\s+from\s+(?:your|the)\s+memory"
    r"|don'?t\s+remember\s+(?:this|that)\s+anymore)[\s.!,]*$",
    re.IGNORECASE)
_RE_REMEMBER = re.compile(
    r"^\s*(?:please\s+)?(?:\bi\s+(?:want|need)\s+you\s+to\s+)?"
    r"(?:remember|keep\s+in\s+mind|save\s+this\s+for\s+later|don'?t\s+forget)\s+"
    r"(?:that\s+|this\s+:?\s*)?(?P<fact>.+?)[\s.]*$",
    re.IGNORECASE)
_RE_REMEMBER_SHORT = re.compile(
    r"^\s*(?:remember\s+(?:this|that|it)|keep\s+this\s+in\s+memory"
    r"|don'?t\s+forget\s+(?:this|that))[\s.!?]*$",
    re.IGNORECASE)
_RE_SEARCH = re.compile(
    r"^\s*(?:search|look\s+(?:for|through)|find)\s+(?:my|the|our)?\s*"
    r"(?:conversations?|memory|memories|history|records?)\s+"
    r"(?:for|about|of)\s+(?P<query>.+?)[\s.?]*$",
    re.IGNORECASE)
_RE_RECALL = re.compile(
    r"(?:what|where)\s+(?:were|did|was|shall)\s+(?:we|i)\b"
    r"|continue\s+(?:from\s+)?(?:where|our)\b"
    r"|do\s+you\s+remember\s+(?:what|our|the)\b"
    r"|(?:yesterday|last\s+(?:night|session|time|week))\b",
    re.IGNORECASE)
_RE_RECALL_TOPIC = re.compile(
    r"(?:about|regarding|with)\s+(?P<topic>.+?)[\s.?]*$",
    re.IGNORECASE)

# Bare continuation: the single most important shape for cross-day memory.
# "Continue." on its own names no topic, so it can only mean "pick up the work
# we left open last time" — that is exactly what the service answers.
_RE_CONTINUE = re.compile(
    r"^\s*(?:please\s+|now\s+|jarvis[,\s]+|sir[,\s]+|ok(?:ay)?[,\s]+|alright[,\s]+|and\s+)?"
    r"(?:continue|go\s+on|carry\s+on|keep\s+going|resume|proceed"
    r"|pick\s+up\s+where\s+we\s+left\s+off|pick\s+up\s+where\s+we\s+were"
    r"|from\s+where\s+we\s+left\s+off|where\s+we\s+left\s+off"
    r"|as\s+we\s+discussed|like\s+we\s+said|let'?s\s+continue|let\s+us\s+continue)"
    r"[\s.!?]*$",
    re.IGNORECASE)

# A deferral is not a memory command by itself — but it is the sentence that
# creates tomorrow's open thread ("upgrade the project, but let's do it
# tomorrow"), so it is detected and recorded, never acted on.
_RE_DEFER = re.compile(
    r"\b(?:tomorrow|later|tonight|next\s+time|another\s+time|not\s+now|some\s+other\s+time"
    r"|do\s+it\s+(?:tomorrow|later|then)|let'?s\s+do\s+it\s+(?:tomorrow|later)"
    r"|we'?ll\s+(?:do|finish|handle|upgrade)\b[^.]*?\b(?:tomorrow|later)"
    r"|pick\s+(?:it|this|that)\s+up\s+(?:tomorrow|later)"
    r"|continue\s+(?:it\s+|this\s+)?(?:tomorrow|later))\b",
    re.IGNORECASE)

# Words that make a "forget" line but are never deletion search terms.
_FILLER = _STOPWORDS | {
    "forget", "forgot", "delete", "remove", "erase", "drop", "memory",
    "memories", "from", "your", "the", "anymore", "ever", "never",
}


def _clean_topic(raw: str) -> str:
    topic = re.split(r"\b(?:that|because|since)\b", raw or "", maxsplit=1)[0]
    topic = topic.strip(" .,!?:;\"'")
    # A leading article is phrasing, not part of the subject name: "Forget
    # the Dino game" must tombstone 'dino game', not 'the dino game'.
    topic = re.sub(r"^(?:the|a|an)\s+", "", topic, flags=re.IGNORECASE)
    return topic[:120]


def topic_terms(topic: str) -> list[str]:
    """Search terms for a topic: significant lowercase words. A short topic
    also yields the whole phrase so multi-word topics match precisely."""
    words = [w for w in re.findall(r"[a-z0-9_]{3,}", (topic or "").lower())
             if w not in _FILLER]
    phrase = " ".join(words)
    terms = []
    if len(words) > 1 and len(phrase) <= 60:
        terms.append(phrase)
    terms.extend(w for w in words if w not in terms)
    return terms[:6]


def detect_memory_command(text: str) -> dict | None:
    """Deterministic memory-command detection (§19 of the brief).

    Returns {"action": forget|remember|search|recall, ...} or None when the
    utterance is ordinary conversation. Deliberately conservative on partial
    matches: an operation that mutates persistent storage fires only on a
    clear command shape, never on a sentence that merely contains 'remember'.
    """
    low = (text or "").strip()
    if not low:
        return None
    if _RE_FORGET_IT.match(low):
        return {"action": "forget", "topic": "", "immediate": True}
    m = _RE_FORGET.match(low)
    if m:
        topic = _clean_topic(m.group("topic"))
        # "forget about it" shapes that the broad regex catches but that name
        # no real topic are still forget-immediate, not a blank-topic delete.
        if not topic or all(w in _STOPWORDS for w in topic.lower().split()):
            return {"action": "forget", "topic": "", "immediate": True}
        return {"action": "forget", "topic": topic,
                "terms": topic_terms(topic), "immediate": False}
    if _RE_REMEMBER_SHORT.match(low):
        return {"action": "remember", "fact": "", "immediate": True}
    m = _RE_REMEMBER.match(low)
    if m:
        fact = _clean_topic(m.group("fact"))
        if fact:
            return {"action": "remember", "fact": fact, "immediate": False}
    m = _RE_SEARCH.match(low)
    if m:
        return {"action": "search", "query": _clean_topic(m.group("query"))}
    if _RE_CONTINUE.match(low):
        # A bare "Continue." carries no topic: the answer is the work that was
        # left open, not a keyword search. Flagged so the executor knows to
        # surface the open threads rather than only matching words.
        return {"action": "recall", "topic": "", "continue": True}
    if _RE_RECALL.search(low) and len(low) < 120:
        mt = _RE_RECALL_TOPIC.search(low)
        return {"action": "recall", "topic": _clean_topic(mt.group("topic")) if mt else ""}
    return None


def is_deferral(text: str) -> bool:
    """True when an utterance schedules the work for later.

    This is detection only — the caller decides to record it. A deferral is how
    "let's do it tomorrow" becomes a real open thread that tomorrow's
    "Continue." can find.
    """
    return bool(_RE_DEFER.search(str(text or "")))


# ── tombstones ────────────────────────────────────────────────────────────────

class MemoryDeletionManager:
    """Persistent deletion records + an in-memory cache, invalidated on every
    write so a stale cache can never resurrect what the user forgot (§15)."""

    def __init__(self, db: BrainDatabase):
        self._db = db
        self._lock = threading.Lock()
        self._cache: list[dict] | None = None   # [{topic, terms(list[str])}]

    def record(self, topic: str, terms: list[str], memory_id: int = 0,
               reason: str = "") -> int:
        tid = self._db.insert_deletion(topic=topic, search_terms=terms,
                                       memory_id=memory_id, reason=reason)
        self.invalidate()
        print(f"[MEMORY] Memory deleted: tombstone #{tid} topic='{topic}' terms={terms}")
        return tid

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _entries(self) -> list[dict]:
        with self._lock:
            if self._cache is None:
                try:
                    self._cache = [
                        {"topic": str(d.get("topic") or "").lower(),
                         "terms": [str(t).lower() for t in (d.get("search_terms") or [])]}
                        for d in self._db.list_deletions()
                    ]
                except Exception:
                    self._cache = []
            return self._cache

    def is_forgotten(self, text: str) -> bool:
        """True when `text` matches a tombstone: either the full topic phrase
        appears, or EVERY significant term of the topic appears. The AND of
        terms keeps unrelated conversations that merely share a common word
        from being flagged, while 'dino game' still matches any phrasing."""
        hay = " " + re.sub(r"[^\w]+", " ", (text or "").lower()) + " "
        for entry in self._entries():
            topic = entry["topic"]
            if topic and f" {topic} " in hay:
                return True
            terms = entry["terms"]
            if terms and all(f" {t} " in hay for t in terms):
                return True
        return False

    def forgotten_topics(self) -> list[str]:
        return [e["topic"] for e in self._entries() if e["topic"]][:8]

    def all_terms(self) -> list[list[str]]:
        return [e["terms"] for e in self._entries() if e["terms"]]


# ── conversation archive ─────────────────────────────────────────────────────

class ConversationStore:
    """Opens one conversation per app session and archives every turn."""

    def __init__(self, db: BrainDatabase):
        self._db = db
        self._lock = threading.Lock()
        self._conversation_id = 0
        self._session_id = 0
        self._last_topic = ""

    def start(self, session_id: int = 0) -> None:
        with self._lock:
            try:
                self._session_id = int(session_id or 0)
                self._conversation_id = self._db.insert_conversation(
                    session_id=self._session_id)
                print(f"[MEMORY] Conversation opened: #{self._conversation_id}")
            except Exception as exc:
                print(f"[MEMORY] conversation open failed: {exc}")
                self._conversation_id = 0

    def note_topic(self, topic: str) -> None:
        if topic:
            with self._lock:
                self._last_topic = topic[:60]

    def append(self, role: str, content: str) -> int:
        content = (content or "").strip()
        if not content or not self._conversation_id:
            return 0
        try:
            mid = self._db.insert_message(
                self._conversation_id, self._session_id,
                role if role in ("user", "assistant") else "note",
                content, self._last_topic)
            if role == "user":
                # First user line titles the conversation — the simplest honest
                # topic label; nothing is invented or summarised here.
                self._db.set_conversation_title(self._conversation_id, content[:80])
            print(f"[MEMORY] Conversation saved: msg #{mid} ({role})")
            return mid
        except Exception as exc:
            print(f"[MEMORY] append failed: {exc}")
            return 0

    def close(self) -> None:
        with self._lock:
            if self._conversation_id:
                try:
                    self._db.close_conversation(self._conversation_id)
                    print(f"[MEMORY] Conversation closed: #{self._conversation_id}")
                except Exception:
                    pass
                self._conversation_id = 0


# ── the service ──────────────────────────────────────────────────────────────

class MemoryService:
    """One facade over conversation archive + tombstones + both memory stores.

    `on_user_turn(text)` is called synchronously on the receive path, so all
    work here is bounded SQLite I/O (sub-millisecond on WAL) — no network, no
    model calls.
    """

    def __init__(self, db: BrainDatabase | None = None):
        # Share the Brain singleton's connection when available: one WAL file,
        # one schema owner. The manager may legitimately be absent (headless
        # tests), hence the lazy import guarded below.
        if db is None:
            db = self._brain_db_or_new()
        self.db = db
        self.conversations = ConversationStore(db)
        self.deletions = MemoryDeletionManager(db)
        self._lock = threading.RLock()
        self._recent: list[tuple[str, str]] = []   # current-session transcript
        self._deferred: list[str] = []             # work deferred to "later"
        try:
            self.conversations.start(self._current_session_id())
        except Exception:
            pass

    @staticmethod
    def _brain_db_or_new() -> BrainDatabase:
        try:
            from memory.manager import get_brain_memory
            return get_brain_memory().db
        except Exception:
            return BrainDatabase()

    @staticmethod
    def _current_session_id() -> int:
        try:
            from memory.manager import get_brain_memory
            return int(get_brain_memory()._session_id or 0)
        except Exception:
            return 0

    # ── turn persistence ─────────────────────────────────────────────────────

    def persist_turn(self, role: str, text: str) -> None:
        if not text:
            return
        self.conversations.append(role, text)
        with self._lock:
            self._recent.append(("user" if role == "user" else "assistant", text))
            if len(self._recent) > 40:
                del self._recent[:len(self._recent) - 40]

    def note_topic(self, topic: str) -> None:
        self.conversations.note_topic(topic)

    def restart_conversation(self) -> None:
        """A live session ended (drop or reconnect): close the archived
        conversation and open the next one. The in-RAM transcript resets —
        the archive, not this buffer, is what survives.

        Before the transcript is dropped, the session's summary and open
        threads are written, so the *next* session knows exactly where this one
        stopped. That record is what a later bare "Continue." reads.
        """
        self._finalise_session()
        with self._lock:
            self._recent = []
        self.conversations.close()
        self.conversations.start(self._current_session_id())

    def shutdown(self) -> None:
        self._finalise_session()
        self.conversations.close()

    # ── continuity: where we left off ────────────────────────────────────────

    _CONTINUITY_KEY = "continuity"

    def _finalise_session(self) -> None:
        """Write the session summary + open threads (never fatal)."""
        try:
            with self._lock:
                turns = list(self._recent)
                deferred = list(self._deferred)
            user_lines = [t for r, t in turns if r == "user"][-6:]
            summary = ("Recent requests: " + " | ".join(x[:80] for x in user_lines)
                       if user_lines else "")
            threads: list[str] = []
            try:
                from memory.manager import get_brain_memory
                threads = list(get_brain_memory().unfinished_work(limit=6))
            except Exception:
                threads = []
            for d in deferred:
                if d and d not in threads:
                    threads.append(d)
            self._record_continuity(summary, threads)
            sid = self._current_session_id()
            if sid:
                self.db.close_session(sid, summary, open_threads=threads)
        except Exception:
            pass

    def _record_continuity(self, summary: str, threads: list[str]) -> None:
        try:
            self.db.kv_set(self._CONTINUITY_KEY, {
                "summary": str(summary or "")[:600],
                "open_threads": [str(t)[:200] for t in (threads or []) if str(t).strip()][:20],
                "ended_at": time.time(),
            })
        except Exception:
            pass

    def remember_deferred(self, text: str, *, subject: str = "") -> None:
        """Record work the user deferred ("let's do it tomorrow").

        Stored as both an open thread (continuity) and a TASKS memory, so the
        continuation is found by the continuity block, by ``unfinished_work``
        and by ordinary recall — three independent paths, one truth. Never a
        command: nothing is scheduled, only written down.
        """
        line = " ".join(str(text or "").split())[:200]
        if not line:
            return
        with self._lock:
            if line not in self._deferred:
                self._deferred.append(line)
        try:
            from memory.manager import get_brain_memory
            brain = get_brain_memory()
            brain.remember(
                line, category="TASKS", source="deferral", importance=0.7,
                confidence=0.85, explicit=True)
            rec = self.continuity_record()
            threads = list(rec.get("open_threads") or [])
            if line not in threads:
                threads.insert(0, line)
            self._record_continuity(rec.get("summary") or "", threads)
        except Exception:
            pass

    def continuity_record(self) -> dict:
        """The best available 'where we left off', merged from real sources.

        Priority: the explicit continuity record, then the newest closed
        session, then the live unfinished work (open tasks). Every line is
        tombstone-filtered, so a forgotten subject can never ride back in on the
        continuation.
        """
        rec: dict = {"summary": "", "open_threads": [], "ended_at": 0.0}
        try:
            raw = self.db.kv_get(self._CONTINUITY_KEY, None)
            if isinstance(raw, dict):
                rec["summary"] = str(raw.get("summary") or "")
                rec["ended_at"] = float(raw.get("ended_at") or 0)
                rec["open_threads"] = [str(t) for t in (raw.get("open_threads") or [])
                                       if str(t).strip()]
        except Exception:
            pass
        if not rec["open_threads"] or not rec["summary"]:
            try:
                sess = self.db.last_session(exclude_id=self._current_session_id())
                if sess:
                    if not rec["summary"]:
                        rec["summary"] = str(sess.get("summary") or "")
                    for t in (sess.get("open_threads") or []):
                        if t not in rec["open_threads"]:
                            rec["open_threads"].append(t)
            except Exception:
                pass
        try:
            from memory.manager import get_brain_memory
            for w in get_brain_memory().unfinished_work(limit=6):
                if w not in rec["open_threads"]:
                    rec["open_threads"].append(w)
        except Exception:
            pass
        rec["open_threads"] = [t for t in rec["open_threads"] if not self.is_forgotten(t)][:12]
        if self.is_forgotten(rec["summary"]):
            rec["summary"] = ""
        return rec

    def open_threads(self, limit: int = 12) -> list[str]:
        """Just the unfinished-work list, for the Mind panel."""
        return list(self.continuity_record().get("open_threads") or [])[:max(1, int(limit))]

    # ── tombstone helpers shared by every read path ──────────────────────────

    def is_forgotten(self, text: str) -> bool:
        return self.deletions.is_forgotten(text)

    # ── command execution (backend-enforced, §5/§6/§19) ─────────────────────

    def on_user_turn(self, text: str) -> dict | None:
        """Detect + EXECUTE a memory command. Returns a result dict when a
        command ran (caller feeds it to the model / UI), None for ordinary
        chat. Persistence of the turn itself is the caller's job and happens
        separately so a detected command never skips archiving."""
        cmd = detect_memory_command(text)
        if not cmd:
            return None
        try:
            if cmd["action"] == "forget":
                return self._exec_forget(cmd, text)
            if cmd["action"] == "remember":
                return self._exec_remember(cmd, text)
            if cmd["action"] == "search":
                return self._exec_search(cmd)
            if cmd["action"] == "recall":
                return self._exec_recall(cmd)
        except Exception as exc:
            print(f"[MEMORY] command '{cmd['action']}' failed: {exc}")
        return None

    def _last_user_text(self) -> str:
        with self._lock:
            for role, txt in reversed(self._recent):
                if role == "user":
                    return txt
        return ""

    def _exec_forget(self, cmd: dict, utterance: str) -> dict:
        topic = cmd.get("topic") or ""
        if not topic and cmd.get("immediate"):
            topic = self._last_topic_for_it()
        terms = topic_terms(topic) if topic else []
        counts = {"memories": 0, "json": 0, "messages": 0, "summaries": 0}

        if topic:
            counts = self.forget_topic(topic, terms)
        else:
            # "Forget this/that" with no resolvable recent topic: acknowledge
            # without nuking unrelated storage — an ambiguous delete is the one
            # thing worse than a slow one.
            self.deletions.record(topic="", terms=[], reason=utterance[:200])

        confirmation = (f"I've forgotten the {topic}." if topic
                        else "I've forgotten that.")
        print(f"[MEMORY] Forget executed: topic='{topic}' {counts}")
        return {"action": "forget", "topic": topic, "counts": counts,
                "confirmation": confirmation,
                "instruction": ("Confirm this deletion in one short sentence, "
                                "in your normal style. Do not explain how your "
                                "memory works and do not ask a question.")}

    def _last_topic_for_it(self) -> str:
        with self._lock:
            if self.conversations._last_topic:
                return self.conversations._last_topic
        return ""

    def forget_topic(self, topic: str, terms: list[str] | None = None) -> dict:
        """The full §6 pipeline: memories in BOTH stores, session summaries,
        archived messages, the live ledger, then a tombstone + cache flush."""
        terms = terms if terms is not None else topic_terms(topic)
        counts = {"memories": 0, "json": 0, "messages": 0, "summaries": 0}
        haystack_terms = terms + ([topic] if topic else [])

        def _matches(text: str) -> bool:
            low = (text or "").lower()
            return any(t in low for t in haystack_terms if t)

        # 1. Brain SQLite memories
        try:
            from memory.manager import get_brain_memory
            brain = get_brain_memory()
            for m in list(brain._db.iterate_active()):
                if _matches(f"{m.get('category', '')} {m.get('content', '')}"):
                    brain._db.mark_deleted(int(m["id"]))
                    counts["memories"] += 1
        except Exception as exc:
            print(f"[MEMORY] brain forget pass skipped: {exc}")

        # 2/3/4. JSON store entries, session summaries — one write, direct.
        try:
            counts.update(self._prune_json(haystack_terms))
        except Exception as exc:
            print(f"[MEMORY] json forget pass skipped: {exc}")

        # 5. Archived conversation messages for the topic.
        try:
            seen = set()
            for t in haystack_terms:
                if t and t not in seen:
                    seen.add(t)
                    counts["messages"] += self.db.prune_messages(t)
        except Exception as exc:
            print(f"[MEMORY] archive prune skipped: {exc}")

        # 6. The restart-continuity ledger in the kv store.
        try:
            self._prune_ledger(haystack_terms)
        except Exception as exc:
            print(f"[MEMORY] ledger prune skipped: {exc}")

        # 6b. Beliefs and dreams grown from the forgotten topic, and the
        #     continuity record's own open threads — a forgotten subject must
        #     leave no trace in the Mind panel or in tomorrow's continuation.
        try:
            for b in self.db.list_beliefs(status="", limit=5000):
                if _matches(str(b.get("statement") or "")):
                    self.db.delete_belief(int(b["id"]))
        except Exception as exc:
            print(f"[MEMORY] belief prune skipped: {exc}")
        try:
            for d in self.db.list_dreams(status="", limit=5000):
                if _matches(f"{d.get('title', '')} {d.get('narrative', '')}"):
                    self.db.delete_dream(int(d["id"]))
        except Exception as exc:
            print(f"[MEMORY] dream prune skipped: {exc}")
        try:
            self._prune_continuity(haystack_terms)
        except Exception as exc:
            print(f"[MEMORY] continuity prune skipped: {exc}")

        # 7. Tombstone LAST so no reader ever sees a tombstone whose pruning
        # failed half-way without also seeing the (idempotent) re-run safety:
        # a failed prune leaves data, but the tombstone still filters it.
        self.deletions.record(topic=topic.lower(), terms=[t.lower() for t in haystack_terms
                                                           if t != topic.lower()],
                              reason=f"forget_topic:{topic}")
        return counts

    def _prune_continuity(self, terms: list[str]) -> None:
        """Drop open threads that match a forgotten topic from the record."""
        raw = self.db.kv_get(self._CONTINUITY_KEY, None)
        if not isinstance(raw, dict):
            return
        threads = [str(t) for t in (raw.get("open_threads") or []) if str(t).strip()]
        kept = [t for t in threads
                if not any(term in t.lower() for term in terms if term)]
        with self._lock:
            self._deferred = [d for d in self._deferred
                              if not any(term in d.lower() for term in terms if term)]
        summary = str(raw.get("summary") or "")
        if any(term in summary.lower() for term in terms if term):
            summary = ""
        if kept != threads or not summary:
            self._record_continuity(summary, kept)

    def _prune_json(self, terms: list[str]) -> dict:
        from memory import memory_manager as mm
        memory = mm.load_memory()
        removed = 0
        def _hit(text: str) -> bool:
            low = (text or "").lower().replace("_", " ")
            return any(t in low for t in terms if t)
        for cat in list(memory.keys()):
            items = memory.get(cat)
            if not isinstance(items, dict):
                continue
            for key in list(items.keys()):
                entry = items.get(key)
                value = mm._entry_value(entry)
                if _hit(f"{key} {value}"):
                    del items[key]
                    removed += 1
        sessions = memory.get("sessions")
        dropped_summaries = 0
        if isinstance(sessions, list):
            keep = []
            for s in sessions:
                if isinstance(s, dict) and _hit(str(s.get("summary", ""))):
                    dropped_summaries += 1
                else:
                    keep.append(s)
            memory["sessions"] = keep
        with mm._lock:
            mm.MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            mm.MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False), encoding="utf-8")
        mm._memory_cache.clear()
        print(f"[MEMORY] JSON pruned: {removed} entries, {dropped_summaries} summaries")
        return {"json": removed, "summaries": dropped_summaries}

    def _prune_ledger(self, terms: list[str]) -> None:
        try:
            raw = self.db.kv_get("conv_context", "")
            if not raw:
                return
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, dict):
                return
            turns = data.get("turns")
            changed = False
            if isinstance(turns, list):
                kept = []
                for t in turns:
                    text = t.get("text", "") if isinstance(t, dict) else str(t)
                    if any(term in text.lower() for term in terms if term):
                        changed = True
                    else:
                        kept.append(t)
                data["turns"] = kept
            ents = data.get("entities")
            if isinstance(ents, list):
                kept_ents = []
                for e in ents:
                    label = str(e.get("label", "")) if isinstance(e, dict) else str(e)
                    if any(term in label.lower() for term in terms if term):
                        changed = True
                    else:
                        kept_ents.append(e)
                data["entities"] = kept_ents
            if changed:
                self.db.kv_set("conv_context", json.dumps(data))
                print("[MEMORY] Ledger pruned")
        except Exception:
            pass

    # ── remember ─────────────────────────────────────────────────────────────

    def _exec_remember(self, cmd: dict, utterance: str) -> dict:
        fact = cmd.get("fact") or ""
        if not fact:
            # "Remember this." — store the substantive previous user turn,
            # not the instruction itself.
            with self._lock:
                prior = [t for r, t in self._recent[:-1] if r == "user"]
            fact = (prior[-1] if prior else utterance).strip()
            fact = fact[:300]
        mid = None
        try:
            from memory.manager import get_brain_memory
            mid = get_brain_memory().remember(fact, explicit=True,
                                              source="user-command",
                                              importance=0.8, confidence=0.9)
        except Exception as exc:
            print(f"[MEMORY] brain remember failed: {exc}")

        # The active backend receives the same fact: with the Obsidian Vault
        # selected this is the write that puts the memory in the vault, and the
        # growth counter ("stronger every time he remembers") is fed here.
        try:
            from memory.backend import store_in_backend
            store_in_backend(fact, explicit=True, source="user-command",
                             confidence=0.9, local_stored=mid is not None)
        except Exception as exc:
            print(f"[MEMORY] backend store skipped: {exc}")
        # Archive the fact as a note line too, so "what do you remember about
        # X" finds it through conversation recall, not only brain retrieval.
        try:
            self.conversations.append("note", fact)
        except Exception:
            pass
        print(f"[MEMORY] Memory created: <{mid}> '{fact[:60]}'")
        return {"action": "remember", "fact": fact, "memory_id": mid,
                "confirmation": "I'll remember that.",
                "instruction": ("Acknowledge remembering this in one short "
                                "sentence. Do not repeat the fact back in full "
                                "and do not explain your memory system.")}

    # ── search / recall ──────────────────────────────────────────────────────

    def search_conversations(self, query: str, limit: int = 8) -> list[dict]:
        """Persistent-archive search, tombstone-filtered (§17)."""
        query = (query or "").strip()
        if not query:
            return []
        try:
            rows = self.db.search_messages_fts(query, limit=limit * 2)
        except Exception:
            rows = []
        out = []
        for r in rows:
            text = r.get("content", "")
            if self.is_forgotten(text):
                continue
            out.append({
                "role": r.get("role"), "content": text,
                "when": datetime.fromtimestamp(float(r.get("created_at", 0))).strftime(
                    "%Y-%m-%d %H:%M"),
            })
            if len(out) >= limit:
                break
        print(f"[MEMORY] Search completed: '{query[:40]}' → {len(out)} hits")
        return out

    def _exec_search(self, cmd: dict) -> dict:
        hits = self.search_conversations(cmd.get("query", ""))
        lines = [f"{h['when']} {h['role']}: {h['content'][:160]}" for h in hits]
        return {"action": "search", "query": cmd.get("query", ""),
                "hits": lines,
                "instruction": (
                    ("Answer from these archived conversation excerpts, citing "
                     "when each was said. If none actually answer the question, "
                     "say you could not find a record — never invent one.")
                    if lines else
                    "Tell the user you could not find a record of that. Never "
                    "invent conversation history.")}

    def _exec_recall(self, cmd: dict) -> dict:
        topic = cmd.get("topic") or ""
        continuing = bool(cmd.get("continue"))
        lines: list[str] = []
        if topic:
            hits = self.search_conversations(topic, limit=6)
            lines = [f"{h['when']} {h['role']}: {h['content'][:160]}" for h in hits]
        if not lines:
            with self._lock:
                prior = list(self._recent)
            rows = [] if prior else self.db.recent_messages(limit=10)
            if prior:
                # The service transcript covers this process; pull older turns
                # from the archive to fill the picture.
                rows = [
                    {"role": r, "content": c,
                     "created_at": None} for r, c in prior[-4:]
                ]
                rows += [m for m in self.db.recent_messages(limit=6)][:6]
            for r in rows:
                text = r.get("content", "")
                if self.is_forgotten(text):
                    continue
                when = (datetime.fromtimestamp(float(r["created_at"])).strftime(
                            "%Y-%m-%d") if r.get("created_at") else "recently")
                lines.append(f"{when} {r.get('role')}: {text[:160]}")
        # The unfinished work — what "continue" must actually pick up. Surfaced
        # separately from `hits` so a bare "Continue." still answers with the
        # open threads even when no archived line matches a keyword.
        open_work: list[str] = []
        record: dict = {}
        if continuing or not topic:
            try:
                record = self.continuity_record()
                open_work = list(record.get("open_threads") or [])
            except Exception:
                open_work = []
        if continuing:
            instruction = (
                "The user asked to continue. Using only the OPEN THREADS and "
                "history below, state in one or two short sentences exactly what "
                "was left in progress and propose the single next step. Never "
                "invent work that is not listed; if nothing is open, say plainly "
                "that nothing was left unfinished.")
        elif lines:
            instruction = ("Continue from this real history; name the unfinished "
                           "work briefly. If the list is empty, say you could not "
                           "find a record — never invent past conversations.")
        else:
            instruction = ("Say you could not find a record of that in your "
                           "conversations. Never invent past conversations.")
        return {"action": "recall", "topic": topic, "hits": lines,
                "open_threads": open_work, "continuing": continuing,
                "summary": str(record.get("summary") or ""),
                "instruction": instruction}

    # ── prompt-time context (startup recall + forgotten guard) ───────────────

    def command_directive(self) -> str:
        """Short always-on rules so memory commands read as actions, never as
        the §11 lecture, and so the model never claims it cannot recall real
        stored history nor invents history it was not given."""
        return (
            "[MEMORY OPERATION RULES]\n"
            "Your backend performs remember/forget/search automatically the "
            "moment the user asks; you do not decide whether it happened. "
            "When the user says forget or remember something, just confirm the "
            "action in one short sentence ('Understood, sir. I've forgotten "
            "the Dino game.') — never explain, qualify, or describe how your "
            "memory works unless asked how it works. "
            "For questions about past conversations, rely on "
            "[PERSISTENT MEMORY] and any [SYSTEM - retrieved...] block as the "
            "authoritative record; if it holds nothing relevant, say plainly "
            "you could not find a record - do not invent one and do not claim "
            "you have no access to previous conversations. "
            "When the user says 'continue', 'carry on', 'go on', 'resume' or "
            "equivalent, resume the work in the [CONTINUATION] block (or the "
            "OPEN THREADS of a retrieved block): name what was left in progress "
            "and the single next step. Never claim that work is already done.\n"
        )

    def build_startup_context(self, max_chars: int = 1800) -> str:
        """The honest 'you remember the past' block for the system prompt.

        Built from the real archive only: where the last session left off (the
        open threads, so a bare "Continue." has something concrete to resume),
        the recent prior-session turns, and the list of explicitly forgotten
        topics that must never be raised again.
        """
        parts: list[str] = []
        try:
            rec = self.continuity_record()
            threads = [t for t in (rec.get("open_threads") or [])
                       if t and not self.is_forgotten(t)]
            summary = str(rec.get("summary") or "").strip()
            if threads or summary:
                body = []
                if summary:
                    body.append(f"- Last session: {summary[:220]}")
                for t in threads[:8]:
                    body.append(f"- Unfinished: {t[:160]}")
                parts.append(
                    "[CONTINUATION — where we left off]\n"
                    "This is the real, stored state of the work from previous "
                    "sessions. If the user says 'continue', 'carry on', 'go on' or "
                    "equivalent, resume from this list and name the next step. "
                    "Never claim the work is already done.\n" + "\n".join(body))
        except Exception:
            pass
        try:
            msgs = [m for m in self.db.recent_messages(limit=14)
                    if not self.is_forgotten(m.get("content", ""))]
            # Drop the current session's rows if any were already written —
            # the live session has those natively; this block is for BEFORE it.
            if msgs:
                lines = []
                for m in msgs[-10:]:
                    day = datetime.fromtimestamp(float(m.get("created_at", 0)))
                    age = (time.time() - float(m.get("created_at", 0))) / 86400.0
                    when = ("today" if age < 1 else
                            "yesterday" if age < 2 else day.strftime("%Y-%m-%d"))
                    who = "User" if m.get("role") == "user" else "JARVIS"
                    lines.append(f"- {when} {who}: {str(m.get('content', ''))[:110]}")
                parts.append(
                    "[PERSISTENT MEMORY — your real conversation archive]\n"
                    "These are actual previous conversations, stored on disk. "
                    "Use them when the user asks what was discussed, what you "
                    "were working on, or to continue unfinished work. Never "
                    "recite them unprompted in a greeting.\n" + "\n".join(lines))
        except Exception:
            pass
        try:
            forgotten = self.deletions.forgotten_topics()
            if forgotten:
                parts.append(
                    "[FORGOTTEN TOPICS — HARD RULE]\n"
                    "The user explicitly made you forget: "
                    + ", ".join(f"'{t}'" for t in forgotten)
                    + ". Never bring these up in any conversation, greeting or "
                      "proactive remark. If asked, confirm they are forgotten.")
        except Exception:
            pass
        if not parts:
            return ""
        text = "\n\n".join(parts)
        return text[:max_chars]

    # ── UI-facing reads (§16) ─────────────────────────────────────────────────

    def recent_conversations(self, limit: int = 8) -> list[dict]:
        try:
            return self.db.list_conversations(limit=limit)
        except Exception:
            return []

    def recent_memories(self, limit: int = 10) -> list[dict]:
        try:
            from memory.manager import get_brain_memory
            return list(get_brain_memory()._db.iterate_active())[:limit]
        except Exception:
            return []

    def deletions_list(self, limit: int = 10) -> list[dict]:
        try:
            return self.db.list_deletions(limit=limit)
        except Exception:
            return []

    def beliefs(self, limit: int = 200) -> list[dict]:
        """JARVIS's beliefs, tombstone-filtered, strongest first."""
        try:
            rows = self.db.list_beliefs(status="active", limit=limit)
        except Exception:
            return []
        return [r for r in rows if not self.is_forgotten(str(r.get("statement") or ""))]

    def dreams(self, limit: int = 200) -> list[dict]:
        """JARVIS's dreams, tombstone-filtered, newest first."""
        try:
            rows = self.db.list_dreams(status="active", limit=limit)
        except Exception:
            return []
        return [r for r in rows
                if not self.is_forgotten(f"{r.get('title', '')} {r.get('narrative', '')}")]

    def reflect(self) -> dict:
        """Run the dream cycle (consolidate beliefs + generate dreams)."""
        try:
            from memory.manager import get_brain_memory
            return dict(get_brain_memory().reflect())
        except Exception as exc:
            print(f"[MEMORY] reflect skipped: {exc}")
            return {"beliefs": 0, "dreams": 0, "linked": 0}


# ── module singleton ─────────────────────────────────────────────────────────

_service: MemoryService | None = None
_service_lock = threading.Lock()


def get_memory_service() -> MemoryService | None:
    """Lazy singleton; None only when the whole memory stack is unavailable
    (callers already treat None as 'memory service off', never fatal)."""
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            try:
                _service = MemoryService()
            except Exception as exc:
                print(f"[MEMORY] service unavailable: {exc}")
                return None
    return _service


def reset_memory_service() -> None:
    """Test hook: drop the singleton so the next get rebuilds on a new path."""
    global _service
    with _service_lock:
        if _service is not None:
            try:
                _service.shutdown()
            except Exception:
                pass
        _service = None
