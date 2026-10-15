"""The conversation store: J.A.R.V.I.S.'s chat history, kept for real.

Chat history is not memory. A conversation is *everything that was said*; a
memory is the durable fact extracted from it. They are stored separately on
purpose, and this module owns the first one — one authoritative store, so there
is never a second chat-history system to reconcile.

Design notes that matter:

* **Durable by default.** A message is written and committed as it arrives, not
  when the window closes. A crash one second after a reply must not cost that
  reply. The "sensible batching" in the settings is applied to the *metadata*
  update (a conversation's ``updated_at``), never to the message itself — losing
  the row to save a write is the wrong trade.
* **Branching-ready.** ``messages.parent_id`` and ``messages.branch`` exist and
  are nullable from day one. The first release writes a linear thread, but the
  schema does not have to be migrated to support branching later.
* **Titles are human.** A conversation is named from the first real user
  message ("Build a PyQt6 music player"), never "New Chat" or "Chat 2", and can
  be renamed.
* **Export/import is real.** A conversation can be written out as Markdown or
  JSON and read back in, so history is portable.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

__all__ = ["ConversationStore", "get_store", "reset_store", "auto_title",
           "conversation_to_markdown"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: A title is a label, not a transcript.
_TITLE_MAX = 52


def _default_db_path() -> Path:
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR) / "conversations.db"
    except Exception:
        return PROJECT_ROOT / "data" / "conversations.db"


def auto_title(text: str, fallback: str = "") -> str:
    """A short, human conversation name derived from the first real message.

    Wake words and filler are stripped, the first clause is kept, and the result
    is title-cased — which is what turns "jarvis, can you help me build a PyQt6
    music player?" into "Build a PyQt6 Music Player" instead of "Conversation 1".
    """
    clean = " ".join(str(text or "").split())
    if not clean:
        return fallback or ""
    # Drop a leading address and any filler that carries no subject.
    clean = re.sub(r"^\s*(hey|hi|hello|ok|okay)?\s*,?\s*jarvis\s*[,:!.-]*\s*",
                   "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^\s*(please|can you|could you|would you|will you|"
                   r"i want you to|i need you to|help me)\s+",
                   "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^\s*(to\s+)?", "", clean)
    # Keep the first clause: a title is not a paragraph.
    clean = re.split(r"[.?!;:\n]", clean, maxsplit=1)[0].strip()
    words = clean.split()
    if not words:
        return fallback or ""
    if len(words) > 8:
        words = words[:8]
    title = " ".join(words).strip(" ,.-")
    if not title:
        return fallback or ""
    # Title-case while keeping acronyms and product names recognisable.
    small = {"a", "an", "the", "for", "of", "to", "in", "on", "and", "or",
             "my", "me", "this", "that", "with"}
    out: list[str] = []
    for index, word in enumerate(title.split()):
        if word.isupper() and len(word) > 1:
            out.append(word)
        elif index and word.lower() in small:
            out.append(word.lower())
        else:
            out.append(word[0].upper() + word[1:] if len(word) > 1
                       else word.upper())
    result = " ".join(out)
    return result[:_TITLE_MAX].rstrip()


class ConversationStore:
    """A SQLite store of conversations and their messages."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path else _default_db_path()
        self._lock = threading.RLock()
        self._conn = None
        self._pending_meta: dict[str, float] = {}
        self._flush_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_error = ""

    # ── database ──────────────────────────────────────────────────────────────

    @property
    def conn(self):
        if self._conn is None:
            import sqlite3
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._create_schema()
        return self._conn

    def _create_schema(self) -> None:
        conn = self._conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_active REAL DEFAULT 0,
                meta TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conv_id TEXT NOT NULL,
                parent_id INTEGER,
                branch TEXT,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL,
                meta TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv_id, ts);
            CREATE INDEX IF NOT EXISTS conversations_updated
                ON conversations(updated_at DESC);
            CREATE TABLE IF NOT EXISTS chat_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)
        conn.commit()

    def _meta_get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM chat_meta WHERE key=?",
                                (key,)).fetchone()
        return str(row["value"]) if row else default

    def _meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO chat_meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        self.conn.commit()

    # ── conversations ─────────────────────────────────────────────────────────

    def new_conversation(self, title: str = "") -> dict:
        """Create and return a new conversation (a fresh, unique id)."""
        now = time.time()
        conv_id = uuid.uuid4().hex
        with self._lock:
            self.conn.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at, "
                "last_active, meta) VALUES(?,?,?,?,?,?)",
                (conv_id, str(title or ""), now, now, now, "{}"))
            self.conn.commit()
        return {"id": conv_id, "title": str(title or ""), "created_at": now,
                "updated_at": now, "messages": []}

    def ensure_conversation(self, conv_id: str, title: str = "") -> dict:
        row = self.conversation(conv_id)
        return row or self._insert_with_id(conv_id, title)

    def _insert_with_id(self, conv_id: str, title: str = "") -> dict:
        now = time.time()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO conversations(id, title, created_at, "
                "updated_at, last_active, meta) VALUES(?,?,?,?,?,?)",
                (str(conv_id), str(title or ""), now, now, now, "{}"))
            self.conn.commit()
        return self.conversation(conv_id) or {"id": conv_id, "title": title,
                                             "messages": []}

    def conversation(self, conv_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM conversations WHERE id=?",
                                (str(conv_id),)).fetchone()
        if row is None:
            return None
        return {**dict(row), "meta": _load_json(row["meta"])}

    def list_conversations(self, limit: int = 200, query: str = "") -> list[dict]:
        """Conversations, newest activity first, with a preview and a count.

        ``query`` searches the conversation's **messages** as well as its title:
        finding a chat by something you remember saying is the whole point of a
        search box here.
        """
        with self._lock:
            if str(query or "").strip():
                needle = f"%{str(query).strip()}%"
                rows = self.conn.execute(
                    "SELECT DISTINCT c.* FROM conversations c "
                    "LEFT JOIN messages m ON m.conv_id = c.id "
                    "WHERE c.title LIKE ? OR m.text LIKE ? "
                    "ORDER BY c.updated_at DESC LIMIT ?",
                    (needle, needle, int(limit))).fetchall()
            else:
                # Empty conversations (a "new chat" nobody typed in) are not history.
                rows = self.conn.execute(
                    "SELECT * FROM conversations c WHERE EXISTS "
                    "(SELECT 1 FROM messages m WHERE m.conv_id = c.id) "
                    "ORDER BY c.updated_at DESC LIMIT ?",
                    (int(limit),)).fetchall()
        return [self._summarise(row) for row in rows]

    def _summarise(self, row) -> dict:
        conv_id = row["id"]
        stats = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conv_id=?",
            (conv_id,)).fetchone()
        first = self.conn.execute(
            "SELECT text, role FROM messages WHERE conv_id=? AND role='user' "
            "ORDER BY ts ASC LIMIT 1", (conv_id,)).fetchone()
        last = self.conn.execute(
            "SELECT text FROM messages WHERE conv_id=? ORDER BY ts DESC LIMIT 1",
            (conv_id,)).fetchone()
        preview = ""
        if first is not None:
            preview = " ".join(str(first["text"]).split())[:120]
        elif last is not None:
            preview = " ".join(str(last["text"]).split())[:120]
        return {
            "id": conv_id,
            "title": str(row["title"] or ""),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
            "message_count": int(stats["n"] if stats else 0),
            "preview": preview,
            "last_text": " ".join(str(last["text"]).split())[:120] if last else "",
            "meta": _load_json(row["meta"]),
        }

    def set_title(self, conv_id: str, title: str) -> bool:
        clean = " ".join(str(title or "").split())
        if not clean or not str(conv_id or ""):
            return False
        with self._lock:
            self.conn.execute("UPDATE conversations SET title=?, updated_at=? "
                              "WHERE id=?", (clean[:120], time.time(),
                                             str(conv_id)))
            self.conn.commit()
        return True

    def maybe_name(self, conv_id: str, text: str) -> str:
        """Name an unnamed conversation from its first real user message."""
        row = self.conversation(conv_id)
        if row is None:
            return ""
        if str(row.get("title") or "").strip():
            return str(row["title"])
        title = auto_title(text)
        if title:
            self.set_title(conv_id, title)
        return title

    def delete_conversation(self, conv_id: str) -> bool:
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE conv_id=?",
                              (str(conv_id),))
            self.conn.execute("DELETE FROM conversations WHERE id=?",
                              (str(conv_id),))
            self.conn.commit()
        if self.last_active_id() == str(conv_id):
            self.set_last_active("")
        return True

    def clear_all(self) -> int:
        with self._lock:
            count = self.conn.execute(
                "SELECT COUNT(*) AS n FROM conversations").fetchone()
            self.conn.execute("DELETE FROM messages")
            self.conn.execute("DELETE FROM conversations")
            self.conn.commit()
        self.set_last_active("")
        return int(count["n"] if count else 0)

    def count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM conversations").fetchone()
        return int(row["n"] if row else 0)

    # ── messages ──────────────────────────────────────────────────────────────

    def add_message(self, conv_id: str, role: str, text: str,
                    parent_id: int | None = None, branch: str = "",
                    meta: dict | None = None) -> int:
        """Append one message. Committed immediately: durability over batching."""
        content = str(text or "")
        if not content.strip():
            return 0
        now = time.time()
        with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO messages(conv_id, parent_id, branch, role, text, ts, "
                "meta) VALUES(?,?,?,?,?,?,?)",
                (str(conv_id), parent_id, branch or None, str(role or "user"),
                 content, now, json.dumps(meta or {}, ensure_ascii=False)))
            self.conn.execute("UPDATE conversations SET updated_at=?, last_active=? "
                              "WHERE id=?", (now, now, str(conv_id)))
            self.conn.commit()
            message_id = int(cursor.lastrowid or 0)
        # The conversation's own timestamp is batched: it changes on every turn
        # and does not need a write of its own.
        self._pending_meta[str(conv_id)] = now
        return message_id

    def messages(self, conv_id: str, limit: int = 0) -> list[dict]:
        sql = ("SELECT * FROM messages WHERE conv_id=? ORDER BY ts ASC, id ASC")
        params: list[Any] = [str(conv_id)]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [{"id": row["id"], "conv_id": row["conv_id"],
                 "parent_id": row["parent_id"], "branch": row["branch"],
                 "role": row["role"], "text": row["text"], "ts": float(row["ts"]),
                 "meta": _load_json(row["meta"])}
                for row in rows]

    def recreate(self, conv_id: str, title: str,
                 messages: Iterable[dict]) -> str:
        """Write a conversation and its messages from an import."""
        record = self._insert_with_id(conv_id, title)
        for message in messages:
            self.add_message(record["id"], str(message.get("role") or "user"),
                             str(message.get("text") or ""),
                             meta=message.get("meta") or {})
        if title:
            self.set_title(record["id"], title)
        return record["id"]

    # ── export / import ───────────────────────────────────────────────────────

    def export_conversation(self, conv_id: str, fmt: str = "markdown") -> str:
        """Render one conversation as Markdown or JSON."""
        row = self.conversation(conv_id)
        if row is None:
            return ""
        messages = self.messages(conv_id)
        if str(fmt).lower() == "json":
            return json.dumps({
                "id": row["id"],
                "title": _display_title(row),
                "created_at": row["created_at"],
                "messages": [{"role": message["role"], "text": message["text"],
                              "ts": message["ts"]} for message in messages],
            }, indent=2, ensure_ascii=False)
        return conversation_to_markdown(row, messages)

    def import_conversation(self, text: str) -> str | None:
        """Read a JSON export back in. Returns the new conversation id."""
        try:
            data = json.loads(str(text or ""))
        except Exception as exc:
            self.last_error = f"the conversation could not be read: {exc}"
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            self.last_error = "that file is not a conversation export"
            return None
        return self.recreate(uuid.uuid4().hex, str(data.get("title") or ""),
                             [message for message in data["messages"]
                              if isinstance(message, dict)])

    # ── active conversation ───────────────────────────────────────────────────

    def last_active_id(self) -> str:
        return self._meta_get("last_active", "")

    def set_last_active(self, conv_id: str) -> None:
        try:
            self._meta_set("last_active", str(conv_id or ""))
        except Exception:
            pass

    def resume_target(self) -> dict | None:
        """The conversation to restore on startup (the last one, if it exists)."""
        active = self.last_active_id()
        if active:
            row = self.conversation(active)
            if row is not None:
                messages = self.messages(active)
                if messages:
                    return {**row, "messages": messages}
        # The last-active chat is missing or empty (e.g. a "new chat" that was
        # never typed in). Fall back to the newest conversation that really has
        # messages, instead of restoring nothing.
        rows = self.list_conversations(limit=1)
        if rows:
            return {**rows[0], "messages": self.messages(rows[0]["id"])}
        return None

    # ── flushing ──────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Write any batched metadata. Message rows are already committed."""
        pending, self._pending_meta = dict(self._pending_meta), {}
        if not pending:
            return
        try:
            with self._lock:
                for conv_id, stamp in pending.items():
                    self.conn.execute(
                        "UPDATE conversations SET updated_at=? WHERE id=?",
                        (float(stamp), conv_id))
                self.conn.commit()
        except Exception as exc:
            self.last_error = f"the conversation metadata could not be saved: {exc}"

    def close(self) -> None:
        self._stop.set()
        self.flush()
        try:
            if self._conn is not None:
                self._conn.commit()
        except Exception:
            pass


def conversation_to_markdown(row: dict, messages: list[dict]) -> str:
    """One conversation as Markdown — readable in any editor."""
    lines = [f"# {_display_title(row)}", ""]
    created = float(row.get("created_at") or 0)
    if created:
        lines.append(f"*{time.strftime('%Y-%m-%d %H:%M', time.localtime(created))}*")
        lines.append("")
    for message in messages:
        role = str(message.get("role") or "").lower()
        who = "You" if role == "user" else "J.A.R.V.I.S."
        lines.append(f"**{who}:**")
        lines.append("")
        lines.append(str(message.get("text") or ""))
        lines.append("")
    return "\n".join(lines)


def _display_title(row: dict) -> str:
    title = str(row.get("title") or "").strip()
    if title:
        return title
    created = float(row.get("created_at") or 0)
    return ("Conversation — "
            + time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
            if created else "Conversation")


def _load_json(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


_STORE: ConversationStore | None = None
_STORE_LOCK = threading.Lock()


def get_store(db_path: str | Path | None = None) -> ConversationStore:
    """The process-wide conversation store."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ConversationStore(db_path=db_path)
        return _STORE


def reset_store(store: ConversationStore | None = None) -> None:
    """Test/teardown hook."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
        _STORE = store