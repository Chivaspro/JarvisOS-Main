"""Tools for the Native Filesystem MCP Server."""

from __future__ import annotations

import fnmatch
import os
import shutil
import time
from pathlib import Path
from typing import Any

from mcp_host.server import Tool

MAX_READ_BYTES = 5 * 1024 * 1024  # 5MB safety limit


def _resolve_path(raw_path: str) -> Path:
    p = Path(raw_path.strip() or ".").expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    return p


def _read_file(args: dict) -> dict:
    """Read contents of a file."""
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return {"error": "Missing 'path' argument"}
    target = _resolve_path(path_str)
    if not target.exists():
        return {"error": f"File does not exist: {path_str}"}
    if not target.is_file():
        return {"error": f"Path is not a regular file: {path_str}"}
    if target.stat().st_size > MAX_READ_BYTES:
        return {"error": f"File size exceeds 5MB limit: {target.stat().st_size} bytes"}

    offset = max(0, int(args.get("offset", 0)))
    max_lines = max(1, min(int(args.get("max_lines", 300)), 2000))

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        sliced = lines[offset: offset + max_lines]
        return {
            "path": str(target),
            "content": "".join(sliced),
            "total_lines": total_lines,
            "lines_returned": len(sliced),
            "offset": offset,
        }
    except Exception as exc:
        return {"error": f"Failed reading file: {exc}"}


def _write_file(args: dict) -> dict:
    """Write or overwrite a file."""
    path_str = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    overwrite = bool(args.get("overwrite", True))
    if not path_str:
        return {"error": "Missing 'path' argument"}
    target = _resolve_path(path_str)
    if target.exists() and not overwrite:
        return {"error": f"File already exists and overwrite=false: {path_str}"}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "path": str(target),
            "bytes_written": len(content.encode("utf-8")),
            "message": f"Successfully wrote {path_str}",
        }
    except Exception as exc:
        return {"error": f"Failed writing file: {exc}"}


def _append_file(args: dict) -> dict:
    """Append text to an existing or new file."""
    path_str = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if not path_str:
        return {"error": "Missing 'path' argument"}
    target = _resolve_path(path_str)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(content)
        return {
            "ok": True,
            "path": str(target),
            "bytes_appended": len(content.encode("utf-8")),
        }
    except Exception as exc:
        return {"error": f"Failed appending file: {exc}"}


def _list_directory(args: dict) -> dict:
    """List directory entries with metadata."""
    path_str = str(args.get("path", ".")).strip()
    target = _resolve_path(path_str)
    if not target.exists():
        return {"error": f"Directory does not exist: {path_str}"}
    if not target.is_dir():
        return {"error": f"Path is not a directory: {path_str}"}

    max_items = max(1, min(int(args.get("max_items", 100)), 500))
    entries = []
    try:
        for entry in os.scandir(target):
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size_bytes": stat.st_size if entry.is_file() else None,
                    "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                })
            except Exception:
                pass
            if len(entries) >= max_items:
                break
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {
            "path": str(target),
            "count": len(entries),
            "entries": entries,
        }
    except Exception as exc:
        return {"error": f"Failed listing directory: {exc}"}


def _search_files(args: dict) -> dict:
    """Recursively search directory by filename glob pattern."""
    dir_str = str(args.get("directory", ".")).strip()
    pattern = str(args.get("pattern", "*")).strip()
    target = _resolve_path(dir_str)
    if not target.exists() or not target.is_dir():
        return {"error": f"Invalid directory: {dir_str}"}

    matches = []
    max_results = max(1, min(int(args.get("max_results", 50)), 200))
    for root, dirs, files in os.walk(target):
        # Ignore deep system / git / cache folders
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "venv")]
        for filename in files:
            if fnmatch.fnmatch(filename.lower(), pattern.lower()):
                full_path = Path(root) / filename
                rel = full_path.relative_to(target)
                matches.append({
                    "name": filename,
                    "path": str(rel),
                    "size_bytes": full_path.stat().st_size if full_path.exists() else 0,
                })
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
    return {"directory": str(target), "pattern": pattern, "count": len(matches), "matches": matches}


def _get_file_info(args: dict) -> dict:
    """Get file stats and metadata."""
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return {"error": "Missing 'path' argument"}
    target = _resolve_path(path_str)
    if not target.exists():
        return {"error": f"Path does not exist: {path_str}"}
    stat = target.stat()
    return {
        "path": str(target),
        "name": target.name,
        "is_file": target.is_file(),
        "is_dir": target.is_dir(),
        "size_bytes": stat.st_size,
        "extension": target.suffix,
        "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime)),
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
    }


def _delete_file(args: dict) -> dict:
    """Delete a file or empty directory (destructive)."""
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return {"error": "Missing 'path' argument"}
    target = _resolve_path(path_str)
    if not target.exists():
        return {"error": f"Path does not exist: {path_str}"}
    try:
        if target.is_file():
            target.unlink()
            return {"ok": True, "message": f"Deleted file {path_str}"}
        elif target.is_dir():
            target.rmdir()
            return {"ok": True, "message": f"Deleted directory {path_str}"}
        return {"error": "Unsupported file type"}
    except Exception as exc:
        return {"error": f"Failed deleting path: {exc}"}


def _create_directory(args: dict) -> dict:
    """Create a new directory recursively."""
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return {"error": "Missing 'path' argument"}
    target = _resolve_path(path_str)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target), "message": f"Created directory {path_str}"}
    except Exception as exc:
        return {"error": f"Failed creating directory: {exc}"}


TOOLS: list[Tool] = [
    Tool(
        name="readFile",
        description="Read lines from a text file with optional offset and max line count.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "offset": {"type": "integer", "description": "Starting line offset (0-indexed)"},
                "max_lines": {"type": "integer", "description": "Maximum number of lines to read"},
            },
            "required": ["path"],
        },
        handler=_read_file,
    ),
    Tool(
        name="writeFile",
        description="Create or overwrite a file with given content.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "content": {"type": "string", "description": "Content to write"},
                "overwrite": {"type": "boolean", "description": "Whether to overwrite existing file (default true)"},
            },
            "required": ["path", "content"],
        },
        handler=_write_file,
    ),
    Tool(
        name="appendFile",
        description="Append text content to a file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "content": {"type": "string", "description": "Content to append"},
            },
            "required": ["path", "content"],
        },
        handler=_append_file,
    ),
    Tool(
        name="listDirectory",
        description="List files and folders in a directory with file sizes and modified times.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default current dir)"},
                "max_items": {"type": "integer", "description": "Maximum number of items to return"},
            },
        },
        handler=_list_directory,
    ),
    Tool(
        name="searchFiles",
        description="Find files in directory matching a pattern (e.g. '*.py' or '*test*').",
        input_schema={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Root search directory"},
                "pattern": {"type": "string", "description": "Glob pattern (default '*')"},
                "max_results": {"type": "integer", "description": "Maximum results to return"},
            },
            "required": ["pattern"],
        },
        handler=_search_files,
    ),
    Tool(
        name="getFileInfo",
        description="Get file metadata, size, timestamps, and type.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to inspect"},
            },
            "required": ["path"],
        },
        handler=_get_file_info,
    ),
    Tool(
        name="createDirectory",
        description="Create a directory and any necessary parent directories.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create"},
            },
            "required": ["path"],
        },
        handler=_create_directory,
    ),
    Tool(
        name="deleteFile",
        description="Permanently delete a file or empty directory. Destructive.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to delete"},
            },
            "required": ["path"],
        },
        handler=_delete_file,
    ),
]
