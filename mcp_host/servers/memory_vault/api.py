"""Tools for the Memory Vault MCP Server."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from mcp_host.server import Tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _get_vault_dir() -> Path:
    target = PROJECT_ROOT / "Jarvis Obsidian Vault"
    if not target.exists():
        target = PROJECT_ROOT / "documents"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _search_vault(args: dict) -> dict:
    """Search notes in the vault by text query or tag."""
    query = str(args.get("query", "")).strip().lower()
    if not query:
        return {"error": "Missing 'query' argument"}
    limit = max(1, min(int(args.get("limit", 10)), 50))
    vault_dir = _get_vault_dir()

    matches = []
    for root, _, files in os.walk(vault_dir):
        for f in files:
            if not f.endswith((".md", ".txt")):
                continue
            fpath = Path(root) / f
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                if query in content.lower() or query in f.lower():
                    # Extract brief matching snippet
                    snippet = ""
                    for line in content.splitlines():
                        if query in line.lower():
                            snippet = line.strip()[:180]
                            break
                    rel = fpath.relative_to(vault_dir)
                    matches.append({
                        "filename": f,
                        "relative_path": str(rel),
                        "snippet": snippet or content[:150].strip(),
                        "size_bytes": fpath.stat().st_size,
                    })
                    if len(matches) >= limit:
                        break
            except Exception:
                pass
        if len(matches) >= limit:
            break

    return {"query": query, "count": len(matches), "matches": matches}


def _read_note(args: dict) -> dict:
    """Read full note by filename or relative path."""
    name = str(args.get("note_name", "")).strip()
    if not name:
        return {"error": "Missing 'note_name' argument"}
    if not name.endswith((".md", ".txt")):
        name += ".md"
    vault_dir = _get_vault_dir()
    target = vault_dir / name
    if not target.exists():
        # Search recursively
        found = list(vault_dir.rglob(name))
        if found:
            target = found[0]
        else:
            return {"error": f"Note '{name}' not found in vault"}

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        stat = target.stat()
        return {
            "note_name": target.name,
            "path": str(target.relative_to(vault_dir)),
            "content": content,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        }
    except Exception as exc:
        return {"error": f"Failed reading note: {exc}"}


def _write_note(args: dict) -> dict:
    """Create or overwrite a note in the vault."""
    name = str(args.get("note_name", "")).strip()
    content = str(args.get("content", ""))
    folder = str(args.get("folder", "")).strip()
    if not name:
        return {"error": "Missing 'note_name' argument"}
    if not name.endswith((".md", ".txt")):
        name += ".md"

    vault_dir = _get_vault_dir()
    target_dir = vault_dir / folder if folder else vault_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name

    try:
        target.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "note_name": name,
            "path": str(target.relative_to(vault_dir)),
            "bytes_written": len(content.encode("utf-8")),
        }
    except Exception as exc:
        return {"error": f"Failed writing note: {exc}"}


def _append_note(args: dict) -> dict:
    """Append text or a timestamped log to an existing note."""
    name = str(args.get("note_name", "")).strip()
    text = str(args.get("text", "")).strip()
    timestamp = bool(args.get("timestamp", True))
    if not name or not text:
        return {"error": "Both 'note_name' and 'text' are required"}
    if not name.endswith((".md", ".txt")):
        name += ".md"

    vault_dir = _get_vault_dir()
    target = vault_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)

    header = f"\n\n### [{time.strftime('%Y-%m-%d %H:%M:%S')}]\n" if timestamp else "\n"
    payload = f"{header}{text}\n"

    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(payload)
        return {"ok": True, "note_name": name, "message": "Appended to note"}
    except Exception as exc:
        return {"error": f"Failed appending note: {exc}"}


def _list_notes(args: dict) -> dict:
    """List notes in the vault."""
    limit = max(1, min(int(args.get("limit", 25)), 100))
    vault_dir = _get_vault_dir()
    notes = []
    for root, _, files in os.walk(vault_dir):
        for f in files:
            if f.endswith((".md", ".txt")):
                fpath = Path(root) / f
                stat = fpath.stat()
                notes.append({
                    "name": f,
                    "path": str(fpath.relative_to(vault_dir)),
                    "size_bytes": stat.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                })
                if len(notes) >= limit:
                    break
        if len(notes) >= limit:
            break
    notes.sort(key=lambda x: x["modified"], reverse=True)
    return {"vault_path": str(vault_dir), "count": len(notes), "notes": notes}


TOOLS: list[Tool] = [
    Tool(
        name="searchVault",
        description="Search notes in the Obsidian Vault / knowledge base by keyword or tag.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search term or tag"},
                "limit": {"type": "integer", "description": "Maximum results to return (default 10)"},
            },
            "required": ["query"],
        },
        handler=_search_vault,
    ),
    Tool(
        name="readNote",
        description="Read the complete text of a note by filename.",
        input_schema={
            "type": "object",
            "properties": {
                "note_name": {"type": "string", "description": "Name or relative path of the note"},
            },
            "required": ["note_name"],
        },
        handler=_read_note,
    ),
    Tool(
        name="writeNote",
        description="Create or overwrite a markdown note in the Obsidian Vault.",
        input_schema={
            "type": "object",
            "properties": {
                "note_name": {"type": "string", "description": "Note name (e.g. 'ProjectPlan.md')"},
                "content": {"type": "string", "description": "Markdown body content"},
                "folder": {"type": "string", "description": "Subfolder in vault (optional)"},
            },
            "required": ["note_name", "content"],
        },
        handler=_write_note,
    ),
    Tool(
        name="appendNote",
        description="Append a timestamped entry or log line to a note.",
        input_schema={
            "type": "object",
            "properties": {
                "note_name": {"type": "string", "description": "Target note name"},
                "text": {"type": "string", "description": "Text to append"},
                "timestamp": {"type": "boolean", "description": "Include timestamp header (default true)"},
            },
            "required": ["note_name", "text"],
        },
        handler=_append_note,
    ),
    Tool(
        name="listNotes",
        description="List recent notes in the Obsidian Vault.",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max notes to list (default 25)"},
            },
        },
        handler=_list_notes,
    ),
]
