"""Conversation context ledger — what JARVIS is currently talking about.

The live audio session already keeps the raw back-and-forth, but the *things*
being discussed (the file we edited, the website we found, the two objects you
held up to the camera) were lost between turns, so references like "this",
"that one", "the other one" or "do the same thing" had nothing concrete to
attach to.

This module keeps a small, bounded ledger of:

* the last few turns (who said what),
* the entities mentioned in them — files, folders, URLs, images, detected
  objects (with their left/right position), the current subject,
* which entity a deictic reference most likely points at.

It is deliberately cheap and dependency-free: a ring buffer plus a few
regexes. Persisted to the brain database's key/value store so "what were we
doing" survives a restart. Nothing here fabricates: an entity only enters the
ledger when it was actually seen in the text or reported by the vision stage.

Safe to import from any thread; every public method is a no-op on failure.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any

_MAX_TURNS = 24          # in-memory ring buffer
_PERSIST_TURNS = 8       # turns written to disk (restart continuity)
_KV_KEY = "conv_context"

# Entity kinds, most-specific first. Used by the resolver's tie-breaks.
KINDS = ("file", "folder", "image", "video", "url", "object", "app", "person")

# ── extraction patterns ───────────────────────────────────────────────────────
_RE_URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_RE_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\n\r\t|<>?*\"']+")
_RE_POSIX_PATH = re.compile(r"(?:^|\s)(/(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z]\w{0,5})")
_RE_QUOTED_FILE = re.compile(r"[\"']([^\"']{1,120}\.[A-Za-z]\w{0,5})[\"']")
# App names are at most two words and must not be a determiner/phrase start,
# otherwise "open the report at C:\..." was captured as an app called
# "the report at C".
_RE_APP = re.compile(
    r"\b(?:open|launch|start|close|quit)\s+([a-z][a-z0-9\-]{1,20}(?:\s+[a-z][a-z0-9\-]{1,20})?)",
    re.IGNORECASE)
_APP_STOP = {"the", "a", "an", "my", "your", "his", "her", "their", "it",
             "this", "that", "these", "those", "some", "up", "to", "for",
             "and", "then", "please", "file", "folder", "website", "page",
             "browser", "window", "report", "document", "image", "video"}

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff")
_VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
_DOC_EXT = (".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".csv",
            ".pptx", ".json", ".py", ".js", ".ts", ".html", ".css", ".log")

# ── deictic references ────────────────────────────────────────────────────────
# (pattern, which entity kind it wants, side hint for object pairs)
_REFERENCES: tuple[tuple[re.Pattern, tuple[str, ...], str], ...] = (
    (re.compile(r"\bthe other one\b|\bthe other\b|\bthe second one\b", re.I), ("object", "file", "url", "image"), "other"),
    (re.compile(r"\bthe left one\b|\bon the left\b|\bthe left\b", re.I), ("object",), "left"),
    (re.compile(r"\bthe right one\b|\bon the right\b|\bthe right\b", re.I), ("object",), "right"),
    (re.compile(r"\bthe first one\b|\bthe former\b", re.I), ("object", "file", "url", "image"), "first"),
    (re.compile(r"\bthe bigger one\b|\bthe largest\b", re.I), ("object",), "bigger"),
    (re.compile(r"\bthe smaller one\b|\bthe smallest\b", re.I), ("object",), "smaller"),
    (re.compile(r"\bdo the same\b|\bsame thing\b|\bsame as before\b|\bagain\b", re.I), ("object", "file", "url", "app"), "last"),
    (re.compile(r"\b(this|that|these|those|it)\b", re.I), ("file", "image", "video", "url", "object", "folder"), "last"),
)


def _looks_like_image(path: str) -> bool:
    return path.lower().endswith(_IMAGE_EXT)


def _looks_like_video(path: str) -> bool:
    return path.lower().endswith(_VIDEO_EXT)


def _clock() -> float:
    return time.time()


class ConversationContext:
    """Bounded, thread-safe ledger of the current conversation's subjects."""

    def __init__(self, memory=None):
        self._lock = threading.RLock()
        self._turns: deque = deque(maxlen=_MAX_TURNS)
        self._entities: dict[str, dict] = {}     # kind -> latest entity
        self._objects: list[dict] = []           # last object scan (ordered)
        self._subject = ""
        self._memory = memory
        self._loaded = False

    # ── persistence ───────────────────────────────────────────────────────────

    def _kv(self):
        if self._memory is None:
            return None
        try:
            return self._memory.db
        except Exception:
            return None

    def load(self) -> None:
        """Restore the tail of the previous conversation (once)."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            db = self._kv()
            if db is None:
                return
            try:
                data = db.kv_get(_KV_KEY, None)
                if isinstance(data, str):
                    data = json.loads(data)
                if not isinstance(data, dict):
                    return
                for t in (data.get("turns") or [])[-_PERSIST_TURNS:]:
                    if isinstance(t, dict) and t.get("text"):
                        self._turns.append({"role": str(t.get("role", "user")),
                                            "text": str(t["text"]), "ts": float(t.get("ts", 0)),
                                            "restored": True})
                ents = data.get("entities") or {}
                if isinstance(ents, dict):
                    for kind, ent in ents.items():
                        if isinstance(ent, dict) and ent.get("label"):
                            self._entities[str(kind)] = ent
                self._subject = str(data.get("subject", ""))
            except Exception:
                pass

    def _persist(self) -> None:
        db = self._kv()
        if db is None:
            return
        try:
            turns = [{"role": t["role"], "text": t["text"][:400], "ts": t["ts"]}
                     for t in list(self._turns)[-_PERSIST_TURNS:]]
            db.kv_set(_KV_KEY, json.dumps({
                "turns": turns, "entities": self._entities,
                "subject": self._subject,
            }))
        except Exception:
            pass

    # ── writes ────────────────────────────────────────────────────────────────

    def note_entity(self, kind: str, label: str, *, value: str = "",
                    position: str = "", source: str = "", meta: dict | None = None) -> dict:
        """Record that an entity of `kind` is (now) in play.

        ``position`` ("left"/"right"/"centre") is what lets a later "the left
        one" resolve against a real detection instead of being guessed.
        """
        kind = str(kind or "").strip().lower()
        label = str(label or "").strip()
        if not kind or not label:
            return {}
        ent = {
            "kind": kind, "label": label[:200], "value": str(value or label)[:400],
            "position": str(position or "").lower()[:12],
            "source": str(source or "")[:40], "ts": _clock(),
        }
        if meta:
            try:
                ent.update({k: v for k, v in dict(meta).items() if k not in ent})
            except Exception:
                pass
        with self._lock:
            self._entities[kind] = ent
            if kind == "object":
                # Keep an ordered, de-duplicated list of the last object scan.
                if not any(o.get("label") == label and o.get("position") == ent["position"]
                           for o in self._objects):
                    self._objects.append(ent)
                self._objects = self._objects[-8:]
        return ent

    def _note_reference(self, kind: str, ent: dict) -> None:
        """Remember which item a reference pointed at.

        "the other one" is only meaningful relative to the one just discussed,
        so resolving a reference marks that item as *the one in mind* instead of
        leaving the detector's last output as an accidental default.
        """
        if not kind or not ent:
            return
        with self._lock:
            cur = dict(ent)
            cur["referred"] = True
            self._entities[str(kind).lower()] = cur

    def note_objects(self, objects) -> None:
        """Note the objects from a fresh detection scan, in screen order.

        Accepts either ``[{"label":..., "position":...}]`` or plain label
        strings; order is assumed left→right as reported by the vision stage.
        """
        if not objects:
            return
        with self._lock:
            self._objects = []
        items = list(objects)[:8]
        labels: list[str] = []
        last_pos = ""
        for i, item in enumerate(items):
            if isinstance(item, dict):
                label, pos = item.get("label", ""), item.get("position", "")
            else:
                label, pos = str(item), ""
            if not pos:
                # Left→right fallback for a pair; centre for a single object.
                if len(items) == 1:
                    pos = "centre"
                elif i == 0:
                    pos = "left"
                elif i == 1:
                    pos = "right"
            label = str(label)
            labels.append(label)
            last_pos = str(pos).lower()[:12]
            ent = {"kind": "object", "label": label[:200], "value": label[:400],
                   "position": str(pos).lower()[:12], "source": "vision",
                   "ts": _clock()}
            with self._lock:
                if not any(o.get("label") == label and o.get("position") == ent["position"]
                           for o in self._objects):
                    self._objects.append(ent)
                self._objects = self._objects[-8:]
        # A fresh scan does not by itself mean the user is talking about the
        # last item it happened to see — only replace a stale single-object
        # entity, and leave "the one we were discussing" alone.
        with self._lock:
            cur = self._entities.get("object")
            if cur is None or cur.get("label") not in labels:
                if labels:
                    self._entities["object"] = {
                        "kind": "object", "label": labels[-1][:200],
                        "value": labels[-1][:400],
                        "position": last_pos or ("centre" if len(labels) == 1 else "right"),
                        "source": "vision", "ts": _clock(),
                    }

    def add_turn(self, role: str, text: str, *, entities: bool = True) -> None:
        """Append one conversation turn and harvest entities from it."""
        text = (text or "").strip()
        if not text:
            return
        role = "assistant" if str(role).startswith("a") else "user"
        with self._lock:
            self._turns.append({"role": role, "text": text[:2000], "ts": _clock()})
            if role == "user" and len(text) > 3:
                self._subject = text[:160]
        if entities:
            self.harvest(text)
        self._persist()

    def note_subject(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            with self._lock:
                self._subject = text[:160]

    def harvest(self, text: str) -> int:
        """Pull concrete entities (paths, URLs, apps) out of arbitrary text.

        Only literal, verifiable mentions are recorded — never a guess about
        what the user probably meant.
        """
        if not text:
            return 0
        found = 0
        for m in _RE_URL.finditer(text):
            url = m.group(0).rstrip(".,);")
            self.note_entity("url", url, value=url, source="text")
            found += 1
        for m in _RE_WIN_PATH.finditer(text):
            self._note_path(m.group(0).strip())
            found += 1
        for m in _RE_POSIX_PATH.finditer(text):
            p = m.group(1).strip()
            if len(p) > 3 and "/" in p:
                self._note_path(p)
                found += 1
        for m in _RE_QUOTED_FILE.finditer(text):
            self._note_path(m.group(1).strip())
            found += 1
        for m in _RE_APP.finditer(text):
            name = m.group(1).strip()
            words = name.lower().split()
            # Reject phrases that are really a sentence fragment, not a name.
            if not words or words[0] in _APP_STOP:
                continue
            if any(w in _APP_STOP for w in words[1:]):
                continue
            self.note_entity("app", name, value=name, source="command")
        return found

    def _note_path(self, path: str) -> None:
        path = path.rstrip(".,;:)")
        if not path or len(path) < 4:
            return
        low = path.lower()
        if _looks_like_image(low):
            kind = "image"
        elif _looks_like_video(low):
            kind = "video"
        elif low.endswith(_DOC_EXT):
            kind = "file"
        elif "." in path.split("\\")[-1].split("/")[-1]:
            kind = "file"
        else:
            kind = "folder"
        self.note_entity(kind, path, value=path, source="text")

    # ── reads ─────────────────────────────────────────────────────────────────

    def entity(self, kind: str) -> dict | None:
        with self._lock:
            return dict(self._entities.get(str(kind or "").lower()) or {}) or None

    def entities(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._entities.items()}

    def objects(self) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._objects]

    def recent(self, n: int = 6, role: str = "") -> list[dict]:
        with self._lock:
            turns = list(self._turns)
        if role:
            turns = [t for t in turns if t["role"] == role]
        return turns[-max(1, int(n)):]

    def subject(self) -> str:
        with self._lock:
            return self._subject

    def has_content(self) -> bool:
        with self._lock:
            return bool(self._turns or self._entities or self._objects)

    # ── reference resolution ──────────────────────────────────────────────────

    def resolve(self, text: str) -> dict:
        """Resolve deictic references in `text` against the ledger.

        Returns ``{"references": [...], "notes": [...], "summary": str}`` where
        each reference is ``{"phrase", "kind", "entity", "confidence"}``. An
        empty result means *no* concrete reference was detected — the caller
        should not invent one.
        """
        out = {"references": [], "notes": [], "summary": ""}
        low = str(text or "").lower()
        if not low.strip():
            return out
        with self._lock:
            ents = {k: dict(v) for k, v in self._entities.items()}
            objs = [dict(o) for o in self._objects]

        for pat, kinds, side in _REFERENCES:
            m = pat.search(low)
            if not m:
                continue
            phrase = m.group(0).strip()
            ent = self._pick(kinds, side, objs, ents)
            if not ent:
                continue
            conf = 0.9 if side in ("left", "right", "other") and ent.get("position") else 0.65
            ref = {"phrase": phrase, "kind": ent.get("kind", ""), "entity": ent,
                   "confidence": conf}
            out["references"].append(ref)
            self._note_reference(ent.get("kind", ""), ent)
            hint = ent.get("position") or ""
            note = (f'"{phrase}" → {ent.get("kind", "item")} '
                    f'{ent.get("label", "")}' + (f' ({hint})' if hint else ''))
            out["notes"].append(note)
            break   # one reference per utterance is enough; more invites mistakes
        if out["notes"]:
            out["summary"] = "; ".join(out["notes"])
        return out

    @staticmethod
    def _pick(kinds, side: str, objs: list[dict], ents: dict) -> dict | None:
        if side in ("left", "right", "other", "bigger", "smaller", "first", "last") and objs:
            if side == "left":
                for o in objs:
                    if o.get("position") == "left":
                        return o
            elif side == "right":
                for o in objs:
                    if o.get("position") == "right":
                        return o
            elif side == "other":
                # "the other one" relative to the object the last turn picked,
                # else the right-hand object of the last pair.
                picked = ents.get("object", {})
                for o in objs:
                    if o.get("label") != picked.get("label"):
                        return o
                for o in reversed(objs):
                    if o.get("position") == "right":
                        return o
            elif side == "bigger":
                return max(objs, key=lambda o: float(o.get("size", 0) or 0))
            elif side == "smaller":
                return min(objs, key=lambda o: float(o.get("size", 0) or 0))
            elif side == "first":
                return objs[0]
            elif side == "last":
                return objs[-1]
        for k in kinds:
            ent = ents.get(k)
            if ent and ent.get("label"):
                return ent
        return None

    # ── prompt text ───────────────────────────────────────────────────────────

    def prompt_block(self, turns: int = 4) -> str:
        """Compact context block for the system instruction."""
        with self._lock:
            ents = {k: dict(v) for k, v in self._entities.items()}
            objs = [dict(o) for o in self._objects]
            subject = self._subject
            recent = list(self._turns)[-max(1, int(turns)):]
        if not (ents or objs or recent):
            return ""
        lines = ["[CONVERSATION CONTEXT]"]
        if subject:
            lines.append(f"Current subject: {subject[:160]}")
        order = [k for k in KINDS if k in ents]
        if order:
            lines.append("In play right now:")
            for k in order:
                e = ents[k]
                pos = f" ({e['position']})" if e.get("position") else ""
                lines.append(f"- {k}{pos}: {e.get('label', '')[:150]}")
        if len(objs) > 1:
            lines.append("Detected objects, screen order: "
                         + ", ".join(f"{o.get('label', '')}{' (' + o['position'] + ')' if o.get('position') else ''}"
                                     for o in objs))
        if recent:
            lines.append("Last turns:")
            for t in recent:
                who = "User" if t["role"] == "user" else "JARVIS"
                lines.append(f"- {who}: {t['text'][:150]}")
        lines.append("Resolve \"this/that/the other one/the left one\" against the items "
                     "above. If the item you need is not listed, ask one short question "
                     "instead of guessing.")
        return "\n".join(lines)

    # ── maintenance ───────────────────────────────────────────────────────────

    def reset(self, *, keep_entities: bool = False) -> None:
        with self._lock:
            self._turns.clear()
            if not keep_entities:
                self._entities.clear()
                self._objects = []
                self._subject = ""
        self._persist()


# ── singleton ─────────────────────────────────────────────────────────────────
_ctx: ConversationContext | None = None
_ctx_lock = threading.Lock()


def get_context() -> ConversationContext:
    """Process-wide context ledger (never raises, returns a usable object)."""
    global _ctx
    if _ctx is not None:
        return _ctx
    with _ctx_lock:
        if _ctx is None:
            mem = None
            try:
                from memory.manager import get_brain_memory
                mem = get_brain_memory()
            except Exception:
                mem = None
            _ctx = ConversationContext(memory=mem)
            try:
                _ctx.load()
            except Exception:
                pass
        return _ctx


def reset_context() -> None:
    global _ctx
    with _ctx_lock:
        _ctx = None


__all__ = ["ConversationContext", "get_context", "reset_context", "KINDS"]
