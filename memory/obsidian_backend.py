"""The Obsidian vault as a memory backend.

This implements the same :class:`memory.backend.MemoryBackend` contract the local
SQLite store and Mem0 do, so switching the active memory backend in Settings
changes *where memory lives* without changing a single caller.

What makes it more than a folder dump:

**Conservative promotion.** A candidate only becomes a note when it is durable
(the existing :mod:`memory.policy` gate has already decided that), and even then
it is matched against the vault first: a note that already says this is updated
rather than duplicated. ``write`` refuses obvious chatter even when called
directly, so a bug upstream cannot fill the vault with noise.

**Provenance and a reason.** Every note records where it came from
(``source``, ``conversation_id``, ``message_id``) and *why* it was kept
(``reason``, ``confidence``). That is what makes "why do you remember this?"
answerable instead of a shrug.

**Meaningful links only.** Related notes are discovered by searching the vault
for the text being stored; a link is written only when a real hit exists. No
links are invented to make the graph look richer.

**Corrections win, history survives.** When new explicit information replaces an
older note, the note is updated in place and the previous text is preserved under
a history heading, so the current answer is unambiguous and the old one is not
deleted.

J.A.R.V.I.S. only ever writes under ``Memory/``. Notes the user wrote elsewhere
in the vault are never modified.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from memory.vault_store import VaultIndex, get_index

__all__ = ["ObsidianVaultBackend", "MEMORY_FOLDER"]

#: Where J.A.R.V.I.S. keeps the notes it writes. Everything else in the vault
#: belongs to the user and is only ever read.
MEMORY_FOLDER = "Memory"

#: A note shorter than this, or matching one of these, is not worth a file.
_CHATTER = re.compile(
    r"^(ok|okay|thanks|thank you|yes|no|good|great|nice|perfect|sure|alright|"
    r"yeah|yep|cool|done|got it|hello|hi|hey)[\s.!?]*$", re.IGNORECASE)

#: Backwards-compatibility alias for the similarity threshold used by callers
#: that imported it; duplicate detection itself now uses the overlap
#: coefficient in :func:`_similarity`.
_DUPLICATE_OVERLAP = 0.72

_CATEGORY_FOLDER = {
    "PROFILE": "People",
    "PREFERENCES": "Preferences",
    "PROJECTS": "Projects",
    "CONVERSATIONS": "Conversations",
    "EPISODES": "Events",
    "EVENTS": "Events",
    "TASKS": "Projects",
    "SOLUTIONS": "Knowledge",
    "ERRORS": "Knowledge",
    "WORKFLOWS": "Knowledge",
    "ENVIRONMENT": "Knowledge",
    "CONTEXT": "Knowledge",
    "RELATIONSHIPS": "People",
    "TOOLS": "Knowledge",
}


class ObsidianVaultBackend:
    """A memory backend whose store is the user's Obsidian vault."""

    name = "obsidian"

    def __init__(self, index: VaultIndex | None = None,
                 path: str | Path | None = None) -> None:
        if index is not None:
            self.index = index
        elif path is not None:
            self.index = VaultIndex(path=path)
        else:
            self.index = get_index()
        self.last_error = ""

    # ── the MemoryBackend contract ────────────────────────────────────────────

    def available(self) -> bool:
        """Usable when the vault folder exists and the index can be opened."""
        try:
            if not self.index.path.exists():
                self.last_error = f"the vault folder does not exist: {self.index.path}"
                return False
            self.index.conn.execute("SELECT 1").fetchone()
            return True
        except Exception as exc:
            self.last_error = f"the vault index is unavailable: {exc}"
            return False

    def write(self, content: str, *, category: str = "",
              project_id: str = "", explicit: bool = False,
              source: str = "", conversation_id: str = "",
              message_id: str = "", reason: str = "",
              confidence: float = 0.0) -> bool:
        """Store one durable fact as, or inside, a vault note.

        Extra keyword arguments (``source``, ``conversation_id``, ``reason``…)
        are optional: the backend interface only requires the first four, and the
        memory policy calls it with exactly those. Provenance is recorded when
        the caller has it.
        """
        text = " ".join(str(content or "").split()).strip()
        if not text:
            return False
        if not explicit and (_CHATTER.match(text) or len(text) < 12):
            return False
        if not self.available():
            return False
        category = str(category or "").strip().upper() or "CONTEXT"
        try:
            existing = self._find_duplicate(text, explicit=explicit,
                                            category=category)
            if existing is not None:
                return self._update_note(existing, text, category=category,
                                         source=source, reason=reason,
                                         confidence=confidence,
                                         explicit=explicit)
            return self._create_note(text, category=category,
                                     project_id=project_id, source=source,
                                     conversation_id=conversation_id,
                                     message_id=message_id, reason=reason,
                                     confidence=confidence)
        except Exception as exc:
            self.last_error = f"the memory note could not be written: {exc}"
            return False

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Recall relevant notes in the shape the rest of the app expects."""
        try:
            self.index.ensure_fresh()
            hits = self.index.search(query, limit=max(1, int(limit)))
        except Exception as exc:
            self.last_error = f"the vault could not be searched: {exc}"
            return []
        out: list[dict[str, Any]] = []
        for note in hits:
            out.append({
                "id": note["path"],
                "path": note["path"],
                "title": note["title"],
                "content": self._note_summary(note),
                "category": self._category_of(note),
                "confidence": _as_float(
                    (note["frontmatter"] or {}).get("confidence"), 0.6),
                "importance": 0.6,
                "updated_at": note["mtime"],
                "tags": note["tags"],
                "source": str((note["frontmatter"] or {}).get("source") or "vault"),
                "reason": str((note["frontmatter"] or {}).get("reason") or ""),
                "backend": self.name,
            })
        return out

    # ── note writing ──────────────────────────────────────────────────────────

    def _memory_dir(self, category: str) -> Path:
        folder = _CATEGORY_FOLDER.get(str(category).upper(), "Knowledge")
        target = self.index.path / MEMORY_FOLDER / folder
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _create_note(self, text: str, *, category: str, project_id: str = "",
                     source: str = "", conversation_id: str = "",
                     message_id: str = "", reason: str = "",
                     confidence: float = 0.0) -> bool:
        folder = self._memory_dir(category)
        slug = _slug(text)
        path = folder / f"{slug}.md"
        suffix = 2
        while path.exists():
            path = folder / f"{slug}-{suffix}.md"
            suffix += 1
        related = self._related_titles(text, exclude="")
        body = self._render_note(
            text=text, category=category, project_id=project_id,
            source=source, conversation_id=conversation_id,
            message_id=message_id, reason=reason, confidence=confidence,
            related=related, history=[])
        path.write_text(body, encoding="utf-8")
        self._touch()
        return True

    def _update_note(self, note: dict, text: str, *, category: str,
                     source: str, reason: str, confidence: float,
                     explicit: bool) -> bool:
        """Update a note in place, preserving what it used to say."""
        target = self.index.path / str(note["path"])
        if not str(note["path"]).startswith(f"{MEMORY_FOLDER}/"):
            # Never edit a note the user owns; add the correction to our own area
            # instead, so their note is left byte-for-byte intact.
            return self._create_note(text, category=category, source=source,
                                     reason=reason, confidence=confidence)
        try:
            current = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return self._create_note(text, category=category, source=source,
                                     reason=reason, confidence=confidence)
        front, body = _split_frontmatter(current)
        history = _existing_history(body)
        previous = _body_without_frontmatter(body)
        if _overlap(previous, text) < 1.0 and previous.strip():
            history.append((time.strftime("%Y-%m-%d %H:%M"), previous.strip()))
        merged_reason = reason or str(front.get("reason") or "")
        merged_confidence = max(float(front.get("confidence") or 0.0),
                                float(confidence or 0.0))
        related = self._related_titles(text, exclude=str(note["path"]))
        rendered = self._render_note(
            text=text, category=category,
            project_id=str(front.get("project") or ""),
            source=source or str(front.get("source") or ""),
            conversation_id=str(front.get("conversation_id") or ""),
            message_id=str(front.get("message_id") or ""),
            reason=merged_reason, confidence=merged_confidence,
            related=related, history=history,
            created=str(front.get("created") or ""))
        target.write_text(rendered, encoding="utf-8")
        self._touch()
        return True

    def _render_note(self, *, text: str, category: str, project_id: str,
                     source: str, conversation_id: str, message_id: str,
                     reason: str, confidence: float, related: list[str],
                     history: list[tuple[str, str]], created: str = "") -> str:
        now = time.strftime("%Y-%m-%d %H:%M")
        confidence = float(confidence or 0.6)
        if confidence > 0.8:
            label = "high"
        elif confidence > 0.55:
            label = "medium"
        else:
            label = "low"
        tags = _tags_for(category, text)
        lines = ["---"]
        lines.append(f'title: "{_title_from(text)}"')
        lines.append(f"category: {category.lower()}")
        if project_id:
            lines.append(f"project: {project_id}")
        lines.append("jarvis: true")
        lines.append("source: " + (source or "conversation"))
        if conversation_id:
            lines.append(f"conversation_id: {conversation_id}")
        if message_id:
            lines.append(f"message_id: {message_id}")
        if reason:
            lines.append(f"reason: {_one_line(reason)}")
        # A plain number, so the frontmatter parses back to a float: the label
        # is carried separately rather than gluing text onto the value.
        lines.append(f"confidence: {confidence:.2f}")
        lines.append(f"confidence_label: {label}")
        lines.append("created: " + (created or now))
        lines.append("updated: " + now)
        lines.append("tags: [" + ", ".join(tags) + "]")
        lines.append("---")
        lines.append("")
        lines.append(f"# {_title_from(text)}")
        lines.append("")
        lines.append(text)
        if related:
            lines.append("")
            lines.append("## Related")
            for title in related[:6]:
                lines.append(f"- [[{title}]]")
        if history:
            lines.append("")
            lines.append("## Previous")
            for when, previous in history[-4:]:
                lines.append(f"- **{when}** — {_one_line(previous)[:400]}")
        lines.append("")
        return "\n".join(lines)

    def _related_titles(self, text: str, exclude: str) -> list[str]:
        """Real vault notes this memory relates to, found by searching for it.

        A minimum real overlap is required before a link is written. Without it,
        a common word ("the", "project") makes a search hit that is not a
        relationship, and the graph fills with links that mean nothing. Only a
        note that genuinely shares this memory's subject is linked.
        """
        try:
            hits = self.index.search(text, limit=5)
        except Exception:
            return []
        out: list[str] = []
        for note in hits:
            if str(note["path"]) == str(exclude):
                continue
            body = str(note.get("body_head") or "")
            if _overlap(body, text) < 0.2:
                continue
            title = str(note["title"] or "").strip()
            if title and title not in out:
                out.append(title)
        return out

    def _find_duplicate(self, text: str, explicit: bool = False,
                        category: str = "") -> dict | None:
        """A note that already says this, or is a correction of the same fact.

        Three signals, in order:

        1. **Containment** — the new text already appears in the note (or the
           note in it). That is the same memory, plain and simple.
        2. **Subject overlap** — the significant words of the *shorter* text are
           mostly shared. A correction usually keeps its subject and changes a
           qualifier ("uses X" → "now uses X"), which Jaccard punishes for the
           extra words and an overlap coefficient does not.
        3. **Same category** — required for a non-containment match, so a
           preference can never overwrite a project fact that happens to share
           words.

        An explicit statement is allowed to differ more than an implicit one:
        saying "remember this" is the user correcting the record, and a second
        contradicting note would be worse than an update.
        """
        folder_prefix = f"{MEMORY_FOLDER}/"
        try:
            self.index.ensure_fresh()
            notes = [note for note in self.index.notes()
                     if str(note["path"]).startswith(folder_prefix)]
        except Exception:
            return None
        needle = _normalise(text)
        wanted = str(category or "").strip().upper()
        threshold = 0.6 if explicit else 0.75
        best: tuple[float, dict] | None = None
        for note in notes:
            body = _strip_heading(note["body_head"] or "")
            other = _normalise(body)
            if not other or not needle:
                continue
            if needle in other or other in needle:
                return note
            if wanted and self._category_of(note) != wanted:
                continue
            score = _similarity(body, text)
            if score >= threshold:
                if best is None or score > best[0]:
                    best = (score, note)
        return best[1] if best else None

    def _touch(self) -> None:
        try:
            self.index.scan()
        except Exception:
            pass

    def _note_summary(self, note: dict) -> str:
        text = str(note.get("body_head") or "").strip()
        return text or str(note.get("title") or "")

    def _category_of(self, note: dict) -> str:
        declared = str((note.get("frontmatter") or {}).get("category") or "").strip()
        if declared:
            return declared.upper()
        folder = str(note.get("folder") or "").rsplit("/", 1)[-1]
        for category, name in _CATEGORY_FOLDER.items():
            if name.lower() == folder.lower():
                return category
        return "CONTEXT"

    # ── forgetting ────────────────────────────────────────────────────────────

    def forget(self, query: str, hard: bool = False) -> int:
        """Remove J.A.R.V.I.S. notes matching *query*. User notes are never touched."""
        needle = " ".join(str(query or "").split()).lower()
        if not needle:
            return 0
        removed = 0
        try:
            self.index.ensure_fresh()
            notes = [note for note in self.index.notes()
                     if str(note["path"]).startswith(f"{MEMORY_FOLDER}/")]
        except Exception:
            return 0
        for note in notes:
            hay = f"{note['title']} {note['body_head']} {' '.join(note['tags'])}".lower()
            if needle not in hay:
                continue
            target = self.index.path / str(note["path"])
            try:
                if hard:
                    target.unlink()
                else:
                    trash = self.index.path / ".trash"
                    trash.mkdir(parents=True, exist_ok=True)
                    target.replace(trash / target.name)
                removed += 1
            except Exception:
                continue
        if removed:
            self._touch()
        return removed

    # ── graph ─────────────────────────────────────────────────────────────────

    def graph(self, layers: dict | None = None) -> dict:
        """The Neural Map source: the vault's real relationships."""
        try:
            self.index.ensure_fresh()
            return self.index.graph(layers=layers)
        except Exception as exc:
            self.last_error = f"the vault graph could not be built: {exc}"
            return {"nodes": [], "links": []}


# ── text helpers ──────────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    """Lower-case, whitespace-collapsed, punctuation-free — for containment."""
    return re.sub(r"[^a-z0-9 ]+", " ", " ".join(str(text or "").lower().split())).strip()


def _strip_heading(body: str) -> str:
    """A note body without its heading or its trailing metadata sections.

    Kept as a safety net even though the index already stores a trimmed head:
    an index written by an older build, or a hand-edited database, still compares
    on the note's actual subject.
    """
    lines = str(body or "").splitlines()
    while lines and not lines[0].strip():
        lines = lines[1:]
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    for index, line in enumerate(lines):
        if line.lstrip().startswith("## "):
            lines = lines[:index]
            break
    return "\n".join(lines).strip()


def _as_float(value: Any, default: float) -> float:
    """Read a frontmatter number, tolerating anything a hand-edit produced."""
    try:
        return float(str(value).split()[0])
    except Exception:
        return float(default)


def _slug(text: str) -> str:
    words = re.sub(r"[^A-Za-z0-9\s]", " ", str(text or "")).split()
    slug = "-".join(words[:8]).lower()
    return (slug or "memory")[:72]


def _title_from(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= 70:
        return clean.rstrip(".")
    return clean[:67].rstrip(" ,.;:") + "…"


def _one_line(text: str, limit: int = 400) -> str:
    return " ".join(str(text or "").split())[:limit]


def _tokens(text: str) -> set[str]:
    """Significant words of a text.

    Dotted initialisms are collapsed first: without that, ``J.A.R.V.I.S.``
    tokenises into single letters, disappears, and two memories that are
    obviously about the same subject compare as unrelated. That is not a
    hypothetical — it is this application's own name.
    """
    lowered = str(text or "").lower()
    lowered = re.sub(r"(?<=[a-z])\.(?=[a-z])", "", lowered)
    words = re.findall(r"[a-z0-9]{3,}", lowered)
    stop = {"the", "and", "for", "with", "that", "this", "from", "are", "was",
            "you", "your", "have", "has", "not", "but", "its", "use",
            "using", "uses", "one", "now", "new"}
    return {word for word in words if word not in stop}


def _overlap(a: str, b: str) -> float:
    """Jaccard overlap between two texts, 0.0–1.0 (used for link relevance)."""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def _similarity(a: str, b: str) -> float:
    """Overlap coefficient: how much of the *shorter* text is shared.

    Chosen over Jaccard for duplicate detection because a correction adds words
    ("now", "updated") without changing the subject, and Jaccard would score
    that as a different memory. Dividing by the smaller token set asks the right
    question: is this the same subject, or a new one?
    """
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / float(min(len(left), len(right)))


def _tags_for(category: str, text: str) -> list[str]:
    tags = [str(category or "context").lower(), "jarvis-memory"]
    for token in sorted(_tokens(text))[:4]:
        if token not in tags:
            tags.append(token)
    return tags


def _split_frontmatter(text: str) -> tuple[dict, str]:
    from memory.vault_store import parse_frontmatter
    return parse_frontmatter(text)


def _body_without_frontmatter(body: str) -> str:
    lines = [line for line in str(body or "").splitlines()
             if not line.startswith("#")]
    return "\n".join(lines).strip()


def _existing_history(body: str) -> list[tuple[str, str]]:
    """The entries already recorded under a note's ``## Previous`` heading."""
    out: list[tuple[str, str]] = []
    if "## Previous" not in str(body or ""):
        return out
    section = str(body).split("## Previous", 1)[1]
    for line in section.splitlines():
        text = line.strip()
        if not text.startswith("-"):
            continue
        match = re.match(r"-\s*\*\*([^*]+)\*\*\s*—\s*(.*)", text)
        if match:
            out.append((match.group(1).strip(), match.group(2).strip()))
            continue
        inner = text.lstrip("- ")
        if inner:
            out.append(("earlier", inner.strip()))
    return out
