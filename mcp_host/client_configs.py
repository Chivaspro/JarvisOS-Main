"""Writing a server into a real MCP client's own configuration file.

The Add/Edit screen's "Add to your client config" panel shows the JSON a client
needs; this module turns that snippet into an actual entry in the file that
client reads, *without* discarding anything already written there.

Rules it holds to:

* **Add, never replace.** The generated entry is merged into the existing
  ``mcpServers`` (or ``servers``) map, so every other server and every unrelated
  key in the file survives the write.
* **One backup, once.** Before the first write the untouched file is copied to
  ``<name>.jarvis-backup``, so a bad merge is always recoverable.
* **Verified or refused.** The rewritten file is read back and parsed before the
  save reports success. A target that is not valid JSON is refused, never
  overwritten.
* **Known locations only.** Claude Desktop, Cline and VS Code have fixed paths
  on this machine; any other client is written where the user points us.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

__all__ = ["candidates", "existing", "merge", "save", "target_label"]

_BACKUP_SUFFIX = ".jarvis-backup"


def _appdata() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base)
    return Path.home() / ".config"


def candidates(target: str) -> list[Path]:
    """The config files this client actually reads, most likely first."""
    key = str(target or "").lower()
    if key == "claude":
        return [_appdata() / "Claude" / "claude_desktop_config.json"]
    if key == "cline":
        return [
            _appdata() / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"
            / "settings" / "cline_mcp_settings.json",
            _appdata() / "Code - Insiders" / "User" / "globalStorage"
            / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        ]
    if key == "vscode":
        return [
            Path.cwd() / ".vscode" / "mcp.json",
            _appdata() / "Code" / "User" / "mcp.json",
        ]
    return []


def target_label(target: str) -> str:
    return {"claude": "Claude Desktop", "cline": "Cline", "vscode": "VS Code",
            "generic": "Generic MCP", "custom": "Generic MCP"}.get(
        str(target or "").lower(), str(target or "this client"))


def existing(target: str) -> dict:
    """The client's current config file, if one is already written."""
    for path in candidates(target):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return {"path": str(path),
                    "mapping": data if isinstance(data, dict) else None,
                    "error": ""}
        except Exception as exc:
            return {"path": str(path), "mapping": None,
                    "error": f"the existing file is not valid JSON: {exc}"}
    paths = candidates(target)
    return {"path": str(paths[0]) if paths else "", "mapping": None, "error": ""}


def merge(existing_mapping: dict | None, generated: dict | None) -> dict:
    """Put ``generated`` on top of what the file already holds."""
    out = dict(existing_mapping or {})
    for root, block in (generated or {}).items():
        if not isinstance(block, dict):
            continue
        current = out.get(root)
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(block)
        out[root] = merged
    return out


def save(target: str, mapping: dict, path: str | Path | None = None) -> dict:
    """Merge ``mapping`` into the target client's config file and verify it.

    Returns ``{ok, path, detail, servers}``. A missing file is created; an
    existing file is backed up once (``<name>.jarvis-backup``) and every entry
    already in it is preserved.
    """
    root = str(next(iter(mapping or {})) or "mcpServers")
    if not isinstance(mapping, dict) or not isinstance(mapping.get(root), dict):
        return {"ok": False, "path": "", "servers": 0,
                "detail": "the generated configuration is empty"}
    if path:
        target_path = Path(path)
    else:
        found = existing(target)
        if not found.get("path"):
            return {"ok": False, "path": "", "servers": 0,
                    "detail": (f"no known configuration file for "
                               f"{target_label(target)} — choose one with "
                               f"Save As")}
        if found.get("error"):
            return {"ok": False, "path": str(found["path"]), "servers": 0,
                    "detail": found["error"]}
        target_path = Path(found["path"])

    current: dict = {}
    if target_path.exists():
        try:
            raw = json.loads(target_path.read_text(encoding="utf-8-sig"))
            current = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            return {"ok": False, "path": str(target_path), "servers": 0,
                    "detail": (f"the existing file is not valid JSON, so it was "
                               f"left untouched: {exc}")}

    merged = merge(current, mapping)
    # One backup, once: the first write keeps the untouched original.
    backup = target_path.with_name(target_path.name + _BACKUP_SUFFIX)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and not backup.exists():
            shutil.copy2(target_path, backup)
    except Exception as exc:
        return {"ok": False, "path": str(target_path), "servers": 0,
                "detail": f"the existing file could not be backed up: {exc}"}

    try:
        tmp = target_path.with_name(target_path.name + ".tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        tmp.replace(target_path)
    except Exception as exc:
        return {"ok": False, "path": str(target_path), "servers": 0,
                "detail": f"the configuration could not be written: {exc}"}

    try:
        check = json.loads(target_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"ok": False, "path": str(target_path), "servers": 0,
                "detail": f"the written file could not be read back: {exc}"}
    servers = len((check.get(root) or {})) if isinstance(check, dict) else 0
    return {"ok": True, "path": str(target_path), "servers": servers,
            "detail": (f"added to {target_label(target)} — {servers} server(s) "
                       f"in {target_path.name}"
                       + (f" (original saved as {backup.name})"
                          if backup.exists() else ""))}
