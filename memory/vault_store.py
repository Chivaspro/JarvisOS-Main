"""The Obsidian vault index: what the vault contains, and how it connects.

An Obsidian vault is a folder of Markdown files with three things that make it a
useful memory store: frontmatter (structured metadata), wiki links (explicit
relationships), and tags (categories). This module reads those and keeps a
**SQLite index** of them, so J.A.R.V.I.S. can search and reason about hundreds of
notes without loading the vault into memory or the prompt.

Two rules shape everything here:

**Never rewrite the user's vault.** The index is a cache *about* the vault, kept
in ``data/obsidian_index.db``. Notes are only ever read while indexing, and the
backend that writes memory only ever creates its own files under ``Memory/``.
Existing notes, folders, links and frontmatter are left exactly as they are.

**Incremental by default.** A scan compares each file's ``(mtime, size)`` with
what the index already recorded, and only reads (and hashes) a file that looks
changed. Opening J.A.R.V.I.S. therefore costs one ``stat`` per note, not a full
re-read, and a full rebuild is available explicitly.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = ["VaultIndex", "default_vault_path", "parse_frontmatter",
           "extract_links", "extract_tags", "note_title", "get_index",
           "reset_index"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: The vault shipped inside the project. It is only a default: the Settings
#: screen can point J.A.R.V.I.S. at any folder, and nothing assumes this one.
_VAULT_NAME = "Jarvis Obsidian Vault"

#: Files and folders that are never part of the knowledge base.
_SKIP_DIRS = {".obsidian", ".git", ".trash", ".DS_Store", "__pycache__"}
_SKIP_FILES = {"untitled.md"}

_WIKI_LINK = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+?\.md)(?:#[^)]*)?\)")
_TAG = re.compile(r"(?<![\w/])#([A-Za-z][\w\-/]{1,60})")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def default_vault_path() -> Path:
    """Where the vault is unless the user configured another one."""
    try:
        from config.settings import get
        configured = str(get("obsidian_vault_path", "") or "").strip()
        if configured:
            return Path(configured)
    except Exception:
        pass
    return PROJECT_ROOT / _VAULT_NAME


def _clean_scalar(value: str) -> Any:
    text = str(value or "").strip()
    if text.startswith(("[", "{")):
        try:
            return json.loads(text.replace("'", '"'))
        except Exception:
            pass
    if text.startswith(('"', "'")) and text.endswith(('"', "'")) and len(text) > 1:
        text = text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~"):
        return None
    return text


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a note into ``(frontmatter, body)``.

    A deliberately small YAML subset — ``key: value``, inline lists and simple
    nested maps — which is what Obsidian notes actually use. An unparseable
    block returns an empty map rather than raising, because a malformed note must
    never stop a scan.
    """
    match = _FRONTMATTER.match(str(text or ""))
    if not match:
        return {}, str(text or "")
    block, body = match.group(1), str(text or "")[match.end():]
    data: dict[str, Any] = {}
    current_key = ""
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  ", "\t")) and current_key:
            # A nested value under the previous key (``tags:\n  - a``).
            item = line.strip().lstrip("-").strip()
            if not item:
                continue
            existing = data.get(current_key)
            if isinstance(existing, list):
                existing.append(_clean_scalar(item))
            elif existing in ("", None):
                data[current_key] = [_clean_scalar(item)]
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            continue
        current_key = key
        data[key] = _clean_scalar(value)
    return data, body


def extract_links(body: str) -> list[str]:
    """Every note this one links to: wiki links and Markdown file links."""
    out: list[str] = []
    for match in _WIKI_LINK.finditer(str(body or "")):
        target = match.group(1).strip()
        if target and target not in out:
            out.append(target)
    for match in _MD_LINK.finditer(str(body or "")):
        target = match.group(1).strip()
        if target and target not in out:
            out.append(target)
    return out


def extract_tags(body: str, frontmatter: dict | None = None) -> list[str]:
    """Inline ``#tags`` plus any ``tags:`` declared in the frontmatter."""
    out: list[str] = []
    declared = (frontmatter or {}).get("tags")
    if isinstance(declared, str):
        declared = [declared]
    if isinstance(declared, list):
        for item in declared:
            tag = str(item or "").strip().lstrip("#")
            if tag and tag not in out:
                out.append(tag)
    # Strip fenced code blocks first: a '#' in a shell snippet is not a tag.
    stripped = re.sub(r"```.*?```", " ", str(body or ""), flags=re.DOTALL)
    for match in _TAG.finditer(stripped):
        tag = match.group(1)
        if tag and tag not in out and not tag.isdigit():
            out.append(tag)
    return out


def note_title(path: Path, body: str, frontmatter: dict | None = None) -> str:
    """A note's display title: frontmatter, then its first heading, then its name."""
    declared = str((frontmatter or {}).get("title") or "").strip()
    if declared:
        return declared
    for line in str(body or "").splitlines():
        text = line.strip()
        if text.startswith("#"):
            title = text.lstrip("#").strip()
            if title:
                return title
    return path.stem


class VaultIndex:
    """A SQLite-backed index of one vault."""

    def __init__(self, path: str | Path | None = None,
                 db_path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_vault_path()
        self._db_path = Path(db_path) if db_path else _default_db_path()
        self._lock = threading.RLock()
        self._conn = None
        self._last_scan = 0.0
        self.last_error = ""

    # ── database ──────────────────────────────────────────────────────────────

    @property
    def conn(self):
        if self._conn is None:
            import sqlite3
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path),
                                         check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema()
        return self._conn

    def _create_schema(self) -> None:
        conn = self._conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                path TEXT PRIMARY KEY,
                title TEXT,
                folder TEXT,
                frontmatter TEXT,
                tags TEXT,
                links TEXT,
                mtime REAL,
                size INTEGER,
                hash TEXT,
                body_head TEXT,
                indexed_at REAL
            );
            CREATE INDEX IF NOT EXISTS notes_folder ON notes(folder);
            CREATE TABLE IF NOT EXISTS vault_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)
        conn.commit()

    def _get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM vault_meta WHERE key=?",
                                (key,)).fetchone()
        return str(row["value"]) if row else default

    def _set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO vault_meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        self.conn.commit()

    # ── scanning ──────────────────────────────────────────────────────────────

    def _note_files(self) -> Iterable[Path]:
        if not self.path.exists():
            return []
        out: list[Path] = []
        try:
            for file in self.path.rglob("*.md"):
                if any(part in _SKIP_DIRS for part in file.parts):
                    continue
                if file.name.lower() in _SKIP_FILES:
                    continue
                out.append(file)
        except Exception as exc:
            self.last_error = f"the vault could not be listed: {exc}"
        return out

    def scan(self, force: bool = False) -> dict:
        """Bring the index up to date. Incremental unless ``force`` is given."""
        started = time.monotonic()
        with self._lock:
            if not self.path.exists():
                self.last_error = "the vault folder does not exist"
                return {"ok": False, "detail": self.last_error, "added": 0,
                        "updated": 0, "removed": 0, "total": 0}
            known = {row["path"]: row for row in self.conn.execute(
                "SELECT path, mtime, size FROM notes")}
            files = list(self._note_files())
            seen: set[str] = set()
            added = updated = 0
            for file in files:
                try:
                    stat = file.stat()
                except Exception:
                    continue
                key = str(file.relative_to(self.path)).replace("\\", "/")
                seen.add(key)
                row = known.get(key)
                if (not force and row is not None
                        and float(row["mtime"] or 0) == stat.st_mtime
                        and int(row["size"] or 0) == stat.st_size):
                    continue
                if self._index_one(file, key, stat):
                    if row is None:
                        added += 1
                    else:
                        updated += 1
            removed = 0
            for key in list(known):
                if key not in seen:
                    self.conn.execute("DELETE FROM notes WHERE path=?", (key,))
                    removed += 1
            self.conn.commit()
            self._last_scan = time.time()
            self._set("last_scan", str(self._last_scan))
            self.last_error = ""
            return {"ok": True, "detail": f"{len(seen)} note(s) indexed",
                    "added": added, "updated": updated, "removed": removed,
                    "total": len(seen),
                    "elapsed_s": round(time.monotonic() - started, 3)}

    def _index_one(self, file: Path, key: str, stat: Any) -> bool:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self.last_error = f"{key} could not be read: {exc}"
            return False
        frontmatter, body = parse_frontmatter(text)
        links = extract_links(body)
        tags = extract_tags(body, frontmatter)
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        # The searchable head starts after a leading heading, so a note's own
        # title is not mistaken for content when comparing it to new memory.
        head_lines = body.splitlines()
        while head_lines and not head_lines[0].strip():
            head_lines = head_lines[1:]
        if head_lines and head_lines[0].lstrip().startswith("#"):
            head_lines = head_lines[1:]
        # Stop at the first ``##`` section: links and history are metadata about
        # the note, not the note's subject, and including them dilutes every
        # comparison made against this text.
        cut = [index for index, line in enumerate(head_lines)
               if line.lstrip().startswith("## ")]
        if cut:
            head_lines = head_lines[:cut[0]]
        body_head = " ".join("\n".join(head_lines).split())[:400]
        folder = str(Path(key).parent).replace("\\", "/")
        if folder == ".":
            folder = ""
        self.conn.execute(
            "INSERT INTO notes(path, title, folder, frontmatter, tags, links, "
            "mtime, size, hash, body_head, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "title=excluded.title, folder=excluded.folder, "
            "frontmatter=excluded.frontmatter, tags=excluded.tags, "
            "links=excluded.links, mtime=excluded.mtime, size=excluded.size, "
            "hash=excluded.hash, body_head=excluded.body_head, "
            "indexed_at=excluded.indexed_at",
            (key, note_title(file, body, frontmatter), folder,
             json.dumps(frontmatter, ensure_ascii=False),
             json.dumps(tags, ensure_ascii=False),
             json.dumps(links, ensure_ascii=False),
             float(stat.st_mtime), int(stat.st_size), digest,
             body_head, time.time()))
        return True

    # ── reading ───────────────────────────────────────────────────────────────

    def ensure_fresh(self) -> None:
        """Cheap catch-up: scan if the index is older than its freshness window."""
        window = 5.0
        try:
            from ui_perfomance import effective
            window = max(1.0, float(effective().get("index_fresh_s") or 5.0))
        except Exception:
            pass
        if time.time() - self._last_scan > window:
            try:
                self.scan()
            except Exception as exc:
                self.last_error = f"the vault index could not be updated: {exc}"

    def use_path(self, path: str | Path) -> None:
        """Point the index at a (possibly different) vault.

        A different folder invalidates every indexed row, so the freshness
        marker is reset: the next ``ensure_fresh`` rescans before the rows are
        trusted, and rows another vault left behind are never served as this
        one's.
        """
        new_path = Path(path)
        with self._lock:
            if str(new_path) == str(self.path):
                return
            self.path = new_path
            self._last_scan = 0.0
            self.last_error = ""

    def _row_to_note(self, row) -> dict:
        return {
            "path": row["path"],
            "title": row["title"],
            "folder": row["folder"],
            "frontmatter": _json(row["frontmatter"], {}),
            "tags": _json(row["tags"], []),
            "links": _json(row["links"], []),
            "mtime": float(row["mtime"] or 0),
            "size": int(row["size"] or 0),
            "hash": row["hash"],
            "body_head": row["body_head"] or "",
            "indexed_at": float(row["indexed_at"] or 0),
        }

    def notes(self, folder: str = "") -> list[dict]:
        if folder:
            rows = self.conn.execute("SELECT * FROM notes WHERE folder=?",
                                     (folder,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM notes").fetchall()
        return [self._row_to_note(row) for row in rows]

    def note(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM notes WHERE path=?",
                                (str(path),)).fetchone()
        return self._row_to_note(row) if row else None

    def note_by_title(self, title: str) -> dict | None:
        wanted = str(title or "").strip().lower()
        if not wanted:
            return None
        for row in self.conn.execute("SELECT * FROM notes"):
            note = self._row_to_note(row)
            if str(note["title"]).strip().lower() == wanted:
                return note
            if str(note["path"]).rsplit("/", 1)[-1].removesuffix(".md").lower() == wanted:
                return note
        return None

    def read_note(self, path: str, limit: int = 20000) -> str:
        """The note's text, or an honest sentence when it cannot be read.

        The path is resolved inside the vault and rejected otherwise: a memory
        store must not become a way to read arbitrary files on the machine.
        """
        target = (self.path / str(path)).resolve()
        try:
            target.relative_to(self.path.resolve())
        except Exception:
            return "that note is outside the vault"
        try:
            return target.read_text(encoding="utf-8", errors="replace")[:limit]
        except Exception as exc:
            return f"the note could not be read: {exc}"

    def backlinks(self, path: str) -> list[dict]:
        """Notes that link to this one, by name or by file."""
        note = self.note(path)
        if note is None:
            return []
        candidates = {str(note["path"])}
        candidates.add(str(note["path"]).rsplit("/", 1)[-1].removesuffix(".md"))
        if note["title"]:
            candidates.add(str(note["title"]))
        wanted = {name.strip().lower() for name in candidates if name}
        out: list[dict] = []
        for other in self.notes():
            if other["path"] == note["path"]:
                continue
            for link in other["links"]:
                name = str(link).strip().rsplit("/", 1)[-1].removesuffix(".md")
                if str(link).strip().lower() in wanted or name.lower() in wanted:
                    out.append({"path": other["path"], "title": other["title"]})
                    break
        return out

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()
        folders = self.conn.execute(
            "SELECT COUNT(DISTINCT folder) AS n FROM notes").fetchone()
        return {
            "notes": int(total["n"] if total else 0),
            "folders": int(folders["n"] if folders else 0),
            "last_scan": float(self._get("last_scan", "0") or 0),
            "path": str(self.path),
        }

    # ── search ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """Rank notes against a query: title > tag > path > body.

        Lexical and deterministic on purpose — the same query always returns the
        same notes, and every hit is a real file with a real reason.
        """
        text = " ".join(str(query or "").split()).lower()
        if not text:
            return []
        words = [word for word in re.split(r"[^a-z0-9]+", text) if len(word) > 1]
        scored: list[tuple[float, dict]] = []
        for note in self.notes():
            haystack = " ".join([
                str(note["title"]).lower(), str(note["path"]).lower(),
                " ".join(str(tag) for tag in note["tags"]).lower(),
                str(note["body_head"]).lower(),
            ])
            score = 0.0
            for word in words:
                if word in str(note["title"]).lower():
                    score += 5.0
                if word in " ".join(str(t) for t in note["tags"]).lower():
                    score += 3.0
                if word in str(note["path"]).lower():
                    score += 2.0
                if word in str(note["body_head"]).lower():
                    score += 1.0
            if score > 0:
                folder = str(note.get("folder") or "").rsplit("/", 1)[-1].lower()
                if folder in ("people", "preferences"):
                    # Who the user is outranks incidental matches, so a stored
                    # profile fact surfaces for "what is my name" even when the
                    # question and the note share only a word or two.
                    score += 2.0
            if score <= 0:
                continue
            # Recency is a tie-break, never the reason a note is returned.
            score += min(1.0, float(note["mtime"]) / max(1.0, time.time())) * 0.5
            scored.append((score, note))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [note for _score, note in scored[:max(1, int(limit))]]

    # ── graph ─────────────────────────────────────────────────────────────────

    def graph(self, layers: dict | None = None, limit: int = 400) -> dict:
        """Nodes and edges built from real vault relationships only.

        Each layer is switchable so the Neural Map can show exactly the
        relationships the user asked for, and a node is never added merely to
        make the picture busier.
        """
        active = dict(layers or {})
        want = {
            "links": bool(active.get("links", True)),
            "backlinks": bool(active.get("backlinks", True)),
            "tags": bool(active.get("tags", True)),
            "folders": bool(active.get("folders", True)),
            "semantic": bool(active.get("semantic", False)),
        }
        notes = self.notes()
        notes.sort(key=lambda note: float(note["mtime"]), reverse=True)
        notes = notes[:max(1, int(limit))]
        by_title: dict[str, str] = {}
        nodes: list[dict] = []
        links: list[dict] = []
        seen_tags: dict[str, str] = {}
        seen_folders: dict[str, str] = {}
        for note in notes:
            node_id = f"note:{note['path']}"
            nodes.append({
                "id": node_id, "label": str(note["title"])[:60],
                "type": "note", "path": note["path"],
                "folder": note["folder"], "tags": list(note["tags"]),
                "mtime": note["mtime"], "body_head": note["body_head"],
            })
            base = str(note["path"]).rsplit("/", 1)[-1].removesuffix(".md")
            by_title[base.lower()] = node_id
            by_title.setdefault(str(note["title"]).lower(), node_id)
        for note in notes:
            node_id = f"note:{note['path']}"
            if want["links"]:
                for link in note["links"]:
                    target = str(link).strip().rsplit("/", 1)[-1].removesuffix(".md")
                    other = by_title.get(target.lower())
                    if other and other != node_id:
                        links.append({"s": node_id, "t": other, "type": "link",
                                      "weight": 1.0})
            if want["tags"]:
                for tag in note["tags"]:
                    tag_id = seen_tags.get(tag)
                    if tag_id is None:
                        tag_id = f"tag:{tag}"
                        seen_tags[tag] = tag_id
                        nodes.append({"id": tag_id, "label": f"#{tag}",
                                      "type": "tag"})
                    links.append({"s": node_id, "t": tag_id, "type": "tag",
                                  "weight": 0.6})
            if want["folders"] and note["folder"]:
                folder_id = seen_folders.get(note["folder"])
                if folder_id is None:
                    folder_id = f"folder:{note['folder']}"
                    seen_folders[note["folder"]] = folder_id
                    nodes.append({"id": folder_id, "label": note["folder"],
                                  "type": "folder"})
                links.append({"s": node_id, "t": folder_id, "type": "folder",
                              "weight": 0.4})
        if want["backlinks"]:
            # A backlink is the same relationship as a link, seen from the other
            # end, so it is drawn as an explicit typed edge rather than a guess.
            for note in notes:
                for other in self.backlinks(note["path"]):
                    source = f"note:{other['path']}"
                    target = f"note:{note['path']}"
                    if any(edge["s"] == target and edge["t"] == source
                           for edge in links):
                        continue
                    links.append({"s": source, "t": target, "type": "backlink",
                                  "weight": 0.8})
        # Deduplicate edges: the same pair may be reached from both directions.
        unique: dict[tuple[str, str, str], dict] = {}
        for edge in links:
            unique.setdefault((edge["s"], edge["t"], edge["type"]), edge)
        return {"nodes": nodes, "links": list(unique.values())}


def _json(text: str, default):
    try:
        value = json.loads(text or "")
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def _default_db_path() -> Path:
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR) / "obsidian_index.db"
    except Exception:
        return PROJECT_ROOT / "data" / "obsidian_index.db"


_INDEX: VaultIndex | None = None
_INDEX_LOCK = threading.Lock()


def get_index(path: str | Path | None = None) -> VaultIndex:
    """The process-wide index (one vault at a time)."""
    global _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = VaultIndex(path=path)
        return _INDEX


def reset_index(index: VaultIndex | None = None) -> None:
    """Test/teardown hook."""
    global _INDEX
    with _INDEX_LOCK:
        _INDEX = index
