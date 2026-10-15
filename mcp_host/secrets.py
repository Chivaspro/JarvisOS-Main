"""Where MCP credentials live, and how they are kept out of everything else.

An MCP server that needs a credential — a bearer token, a custom header such as
Composio's ``x-consumer-api-key``, or an environment variable for a local
process — is not unusual; it is the normal case for anything remote. The rule
this module enforces is that the value is stored **once**, in the same
gitignored, atomically-written store every other credential in this project
uses (``config/api_keys.json`` via :mod:`app.config_guard`), and never appears
on the server spec, in a log line, or in an exported configuration.

Structure, under the top-level ``mcp_secrets`` map:

    {
      "mcp_secrets": {
        "composio": {
          "headers": {"x-consumer-api-key": "…"},
          "env": {}
        },
        "github": {"bearer_token": "…", "headers": {}, "env": {}}
      }
    }

Non-secret configuration (name, URL, transport, header *names*, permissions,
reconnect policy) stays in ``data/mcp/servers.json``. That split is what lets a
server list be read, rendered or exported without a masking pass failing open.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

__all__ = [
    "MASK", "bearer_token", "clear_server_secrets", "has_secrets", "mask",
    "mask_map", "redact", "rename_server", "server_env", "server_headers",
    "server_secrets", "set_server_secrets",
]

#: What a stored secret renders as in any UI or generated config.
MASK = "••••••••"

_LOCK = threading.RLock()


def _config_guard():
    from app import config_guard
    return config_guard


def _config_path() -> Path:
    try:
        from app.config_guard import CONFIG_PATH
        return Path(CONFIG_PATH)
    except Exception:
        return Path("config") / "api_keys.json"


def _read() -> dict:
    try:
        from app import config_guard
        data = config_guard.read(_config_path())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> tuple[bool, str]:
    """Write through the guarded writer: backup → atomic write → verify.

    Reusing ``guard_write`` is the whole point: an MCP credential is no less
    important than an API key, so it gets the same crash-safety and rollback.
    """
    try:
        guard = _config_guard()
        ok, message = guard.guard_write(data, tag="mcp-secrets")
        return bool(ok), str(message)
    except Exception as exc:
        return False, f"the MCP credential store could not be written: {exc}"


# ── reading ───────────────────────────────────────────────────────────────────

def server_secrets(name: str) -> dict:
    """Every secret recorded for one server: ``{bearer_token, headers, env}``."""
    key = str(name or "").strip()
    if not key:
        return {}
    with _LOCK:
        block = (_read().get("mcp_secrets") or {})
    entry = block.get(key) if isinstance(block, dict) else None
    if not isinstance(entry, dict):
        return {}
    return {
        "bearer_token": str(entry.get("bearer_token") or ""),
        "headers": {str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
        "env": {str(k): str(v) for k, v in (entry.get("env") or {}).items()},
    }


def server_headers(name: str) -> dict:
    """The credential headers for one server, ready to merge into a request."""
    return dict(server_secrets(name).get("headers") or {})


def server_env(name: str) -> dict:
    """The secret environment variables for one local server process."""
    return dict(server_secrets(name).get("env") or {})


def bearer_token(name: str) -> str:
    return str(server_secrets(name).get("bearer_token") or "")


def has_secrets(name: str) -> bool:
    entry = server_secrets(name)
    return bool(entry.get("bearer_token") or entry.get("headers") or entry.get("env"))


# ── writing ───────────────────────────────────────────────────────────────────

def set_server_secrets(name: str, *, bearer_token: str | None = None,
                       headers: dict | None = None,
                       env: dict | None = None) -> tuple[bool, str]:
    """Store (or clear) the credentials for one server.

    Only the fields passed are touched, so editing a header does not silently
    erase a bearer token. An empty string or an empty dict clears that field —
    which is how the Edit screen removes a credential the user deleted.
    """
    key = str(name or "").strip()
    if not key:
        return False, "the server has no name, so its credentials were not stored"
    with _LOCK:
        data = _read()
        block = data.get("mcp_secrets")
        if not isinstance(block, dict):
            block = {}
        entry = dict(block.get(key) or {})
        if bearer_token is not None:
            entry["bearer_token"] = str(bearer_token or "")
        if headers is not None:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()
                                if str(k).strip()}
        if env is not None:
            entry["env"] = {str(k): str(v) for k, v in env.items()
                            if str(k).strip()}
        entry = {k: v for k, v in entry.items()
                 if v not in ("", {}, None)}
        if entry:
            block[key] = entry
        else:
            block.pop(key, None)
        data["mcp_secrets"] = block
        return _write(data)


def clear_server_secrets(name: str) -> tuple[bool, str]:
    """Remove every credential for a server (used by Remove)."""
    key = str(name or "").strip()
    if not key:
        return True, ""
    with _LOCK:
        data = _read()
        block = data.get("mcp_secrets")
        if not isinstance(block, dict) or key not in block:
            return True, ""
        block.pop(key, None)
        if not block:
            data.pop("mcp_secrets", None)
        else:
            data["mcp_secrets"] = block
        return _write(data)


def rename_server(old: str, new: str) -> tuple[bool, str]:
    """Carry credentials across a rename, so editing a name does not orphan them."""
    old_key, new_key = str(old or "").strip(), str(new or "").strip()
    if not old_key or not new_key or old_key == new_key:
        return True, ""
    with _LOCK:
        data = _read()
        block = data.get("mcp_secrets")
        if not isinstance(block, dict) or old_key not in block:
            return True, ""
        block[new_key] = block.pop(old_key)
        data["mcp_secrets"] = block
        return _write(data)


# ── rendering ─────────────────────────────────────────────────────────────────

def mask(value: Any) -> str:
    """A stored secret, masked. An empty value stays empty (not a fake dots)."""
    text = str(value or "")
    if not text:
        return ""
    return MASK


def mask_map(values: dict) -> dict:
    """Mask every value in a map, keeping the keys readable."""
    return {str(k): mask(v) for k, v in (values or {}).items()}


def redact(text: str) -> str:
    """Replace every stored secret found in *text* with the mask.

    Used before a log line, an activity string or a generated config is written:
    a secret pasted into a URL or an error message must not survive the trip to
    the screen or the log file.
    """
    out = str(text or "")
    try:
        with _LOCK:
            block = (_read().get("mcp_secrets") or {})
    except Exception:
        return out
    if not isinstance(block, dict):
        return out
    for entry in block.values():
        if not isinstance(entry, dict):
            continue
        values = [str(entry.get("bearer_token") or "")]
        values += [str(v) for v in (entry.get("headers") or {}).values()]
        values += [str(v) for v in (entry.get("env") or {}).values()]
        for value in values:
            if len(value) >= 6 and value in out:
                out = out.replace(value, MASK)
    return out
