"""MCP servers shipped with J.A.R.V.I.S. (each one a self-contained module)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SERVERS_DIR = Path(__file__).resolve().parent

__all__ = [
    "SERVERS_DIR",
    "list_local_servers",
    "get_local_server_config",
    "save_local_server_config",
]


def list_local_servers() -> list[dict[str, Any]]:
    """Enumerate all runnable local MCP servers in mcp_host/servers."""
    found = []
    for item in SERVERS_DIR.iterdir():
        if not item.is_dir() or item.name.startswith(("_", ".")):
            continue
        server_py = item / "server.py"
        if server_py.exists():
            cfg = get_local_server_config(item.name)
            found.append({
                "name": item.name,
                "path": str(item),
                "server_module": f"mcp_host.servers.{item.name}.server",
                "config": cfg,
                "has_config": (item / "config.json").exists(),
            })
    return sorted(found, key=lambda x: x["name"])


def get_local_server_config(name: str) -> dict[str, Any]:
    """Read the config.json for a local server, if available."""
    cfg_file = SERVERS_DIR / name / "config.json"
    if cfg_file.exists():
        try:
            return json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"name": name, "enabled": True}


def save_local_server_config(name: str, config_dict: dict[str, Any]) -> bool:
    """Save config.json for a local server."""
    cfg_file = SERVERS_DIR / name / "config.json"
    try:
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(config_dict, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False
