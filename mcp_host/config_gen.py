"""Generating the client configuration snippet shown in the Add screen.

The panel at the bottom of the Add/Edit screen ("Add to your client config")
must produce configuration a *different* MCP client can paste in, built from the
values currently entered — the current server name, the current URL, transport,
headers and environment variable names. Nothing here is special-cased to one
provider: the shape follows the transport, and the target client decides only
the small differences that genuinely exist.

**Credentials are never emitted.** A submitted configuration goes into a file the
user may commit or share, so each secret is replaced by an obvious placeholder
(``<YOUR_API_KEY>``); the value only ever lives in J.A.R.V.I.S.'s own guarded
store. That is why this function takes the secret *names* it should mention, not
their values.

The generated mapping is validated as JSON before it is handed to the clipboard —
a copy button that silently copies malformed text is worse than no button.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["CLIENT_TARGETS", "client_config", "pairs", "placeholder_for"]

#: The clients the generator knows about. "generic" is the default and matches
#: the common ``mcpServers`` shape; the others differ only where they really do.
CLIENT_TARGETS = ("generic", "claude", "cline", "vscode", "custom")

#: How each target names the top-level map and whether it needs an explicit
#: transport discriminator per server.
_TARGET_LAYOUT = {
    "generic": {"root": "mcpServers", "type_field": False, "label": "Generic MCP"},
    "custom": {"root": "mcpServers", "type_field": False, "label": "Generic MCP"},
    "claude": {"root": "mcpServers", "type_field": False, "label": "Claude"},
    "cline": {"root": "mcpServers", "type_field": False, "label": "Cline"},
    "vscode": {"root": "servers", "type_field": True, "label": "VS Code"},
}


def placeholder_for(name: str) -> str:
    """An obvious placeholder for one credential, derived from its name.

    ``x-consumer-api-key`` becomes ``<YOUR_X_CONSUMER_API_KEY>``: the reader can
    see exactly which value goes there, and no real secret ever appears.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_").upper()
    return f"<YOUR_{safe or 'CREDENTIAL'}>"


def _server_key(name: str) -> str:
    """A JSON-safe key for the server, matching MCP clients' usual style."""
    key = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip().lower())
    return key.strip("_") or "mcp-server"


def _args(spec: Any) -> list[str]:
    command = [str(part) for part in (getattr(spec, "command", []) or [])]
    return command[1:] if len(command) > 1 else []


def _command(spec: Any) -> str:
    command = [str(part) for part in (getattr(spec, "command", []) or [])]
    return command[0] if command else ""


def client_config(spec: Any, target: str = "generic",
                  include_secrets: bool = False,
                  secret_values: dict | None = None) -> dict:
    """Build the configuration mapping for one server.

    ``include_secrets`` exists so the user can deliberately copy a working
    configuration with real values in it; it defaults to off, and the caller is
    expected to warn first. Placeholders are used otherwise.
    """
    layout = _TARGET_LAYOUT.get(str(target or "generic").lower(),
                                _TARGET_LAYOUT["generic"])
    key = _server_key(getattr(spec, "name", ""))
    entry: dict[str, Any] = {}
    is_http = bool(getattr(spec, "is_http", False))
    values = dict(secret_values or {})

    def _credential(field: str, display: str) -> str:
        if include_secrets and values.get(field):
            return str(values[field])
        return placeholder_for(display)

    if is_http:
        entry["url"] = str(getattr(spec, "url", "") or "")
        headers: dict[str, str] = {}
        auth = str(getattr(spec, "auth", "none") or "none").lower()
        if auth == "bearer":
            headers["Authorization"] = f"Bearer {_credential('bearer_token', 'API_KEY')}"
        for name in (getattr(spec, "header_name_list", lambda: [])() or []):
            if name == "Authorization" and auth == "bearer":
                continue
            headers[str(name)] = _credential(f"header:{name}", str(name))
        if headers:
            entry["headers"] = headers
        if layout["type_field"]:
            entry = {"type": "http", **entry}
    else:
        entry["command"] = _command(spec)
        args = _args(spec)
        if args:
            entry["args"] = args
        env: dict[str, str] = {}
        for name in (getattr(spec, "env", {}) or {}):
            env[str(name)] = _credential(f"env:{name}", str(name))
        if env:
            entry["env"] = env
        if layout["type_field"]:
            entry = {"type": "stdio", **entry}

    return {layout["root"]: {key: entry}}


def pairs(mapping: dict) -> list[tuple[str, str]]:
    """Flatten a nested mapping into ``(dotted path, value)`` pairs, sorted.

    Used by the Import review screen to show exactly what was discovered without
    rendering a tree widget for three levels of JSON.
    """
    out: list[tuple[str, str]] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            out.append((path, json.dumps(node, ensure_ascii=False)))
        else:
            out.append((path, "" if node is None else str(node)))

    _walk(mapping, "")
    return sorted(out)


def validate(text: str) -> tuple[bool, str]:
    """Validate generated configuration text before copying it."""
    try:
        parsed = json.loads(str(text or "{}"))
    except Exception as exc:
        return False, f"the configuration is not valid JSON: {exc}"
    if not isinstance(parsed, dict) or not parsed:
        return False, "the configuration is empty"
    return True, ""
