"""SQLite persistence for service-level conversations and audit events."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class Database:
    """Minimal repository boundary that can later be backed by SQLAlchemy."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def add_message(self, role: str, content: str) -> int:
        if not role.strip() or not content.strip():
            raise ValueError("role and content are required")
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                "INSERT INTO conversations(role, content) VALUES (?, ?)",
                (role.strip(), content.strip()),
            )
            return int(cursor.lastrowid)

    def recent_messages(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, role, content, created_at FROM conversations "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]
