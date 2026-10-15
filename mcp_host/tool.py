"""The model-facing ``mcp`` tool — one tool, every MCP server.

This is the thin, honest edge between the assistant and
:mod:`mcp_host.manager`. It owns no protocol logic of its own: it validates what
the model asked for, calls the manager, and flattens the answer into one
sentence the voice layer can speak plus a compact data trailer.

The verbs are deliberately few, and they are all about *management*, not about
running a server's tools — those are published as real tools under their own
``<server>__<tool>`` names, so the model calls them directly:

* ``status``  — which servers exist, which are connected, and what went wrong
* ``list``    — what each connected server offers (tools / resources / prompts)
* ``refresh`` — re-read a server's capabilities after it changed
* ``enable`` / ``disable`` — flip one server's switch, keeping its configuration

There is deliberately no ``call`` verb: a tool from a connected server is already
a first-class J.A.R.V.I.S. tool, and duplicating that path would be the beginning
of a second, divergent way to act.
"""

from __future__ import annotations

import json
from typing import Any, Callable

__all__ = ["DECLARATION", "PARAMS", "VERBS", "handle_mcp"]

VERBS = ("status", "list", "refresh", "enable", "disable", "test")

#: The shared parameter superset, in the Live function-declaration dialect.
PARAMS: dict[str, tuple[str, str]] = {
    "action": ("STRING", "The operation: status, list, refresh, enable, "
                          "disable or test."),
    "server": ("STRING", "The MCP server to act on, by name — 'github', "
                          "'composio', 'filesystem'. Omit for status and list, "
                          "which cover every server."),
}

#: The function declaration main.py hands to the Live API, built from the same
#: parameter map so the schema the model sees and the schema this module
#: validates against cannot drift apart.
DECLARATION: dict = {
    "name": "mcp",
    "description": (
        "Manage J.A.R.V.I.S.'s MCP (Model Context Protocol) servers: see which "
        "are connected, list the tools they expose, refresh their capabilities, "
        "or enable/disable/test one. Tools from a connected MCP server are "
        "separate tools named '<server>__<tool>' and are called directly."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            name: {"type": kind, "description": description}
            for name, (kind, description) in PARAMS.items()
        },
        "required": ["action"],
    },
}

_VERB_HELP = "mcp takes one of: " + ", ".join(VERBS) + "."


def _text(value: Any, limit: int = 3000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _result(sentence: str, **payload: Any) -> str:
    """The one exit shape: a spoken sentence, plus a compact data trailer."""
    text = _text(sentence) or "Done."
    keep = {key: value for key, value in payload.items()
            if value is not None
            and not (isinstance(value, (str, bytes, list, tuple, dict, set))
                     and not value)}
    if not keep:
        return text
    try:
        trailer = json.dumps(keep, ensure_ascii=False, default=str)[:1200]
    except Exception:
        return text
    return f"{text}\n[mcp data: {trailer}]"


def _allowed(enabled: Any) -> bool:
    if callable(enabled):
        try:
            return bool(enabled("mcp_servers"))
        except Exception:
            return True
    if isinstance(enabled, dict):
        return bool(enabled.get("mcp_servers", True))
    try:
        from config.settings import get_feature
        return bool(get_feature("mcp_servers", True))
    except Exception:
        return True


def _state_phrase(row: dict) -> str:
    if not row.get("enabled"):
        return "disabled"
    if row.get("state") == "connected":
        if not row.get("authenticated"):
            return "connected, but its credentials need attention"
        if not row.get("tools_ready"):
            return "connected, but it offers no tools"
        return "connected"
    return str(row.get("state_label") or row.get("state") or "unknown").lower()


def _verb_status(mcp) -> str:
    data = mcp.status()
    rows = data.get("servers") or []
    if not rows:
        return _result("No MCP servers are configured yet. Add one under "
                       "Settings, MCP.")
    connected = [row for row in rows if row.get("state") == "connected"]
    broken = [row for row in rows if row.get("state") == "error"]
    lines = [f"{len(rows)} MCP server(s) configured, {len(connected)} connected, "
             f"{data.get('tools', 0)} tool(s) available."]
    if broken:
        lines.append("Needs attention: "
                     + "; ".join(f"{row['name']} — {row['error'] or 'error'}"
                                 for row in broken[:4]) + ".")
    return _result(" ".join(lines),
                   servers=[{"name": row["name"], "state": _state_phrase(row),
                             "tools": row.get("tool_count", 0),
                             "transport": row.get("transport", "")}
                            for row in rows])


def _verb_list(mcp, server: str) -> str:
    if server:
        row = next((r for r in mcp.status().get("servers") or []
                    if str(r.get("name")) == server), None)
        if row is None:
            return _result(f"There is no MCP server called '{server}'.",
                           reason="unknown_server", server=server)
        names = row.get("tools") or []
        resources = row.get("resource_count", 0)
        prompts = row.get("prompt_count", 0)
        if not names:
            return _result(f"{server} is {_state_phrase(row)} and offers no "
                           f"tools.", server=server)
        return _result(f"{server} offers {len(names)} tool(s)"
                       + (f", {resources} resource(s)" if resources else "")
                       + (f", {prompts} prompt(s)" if prompts else "") + ".",
                       server=server, tools=names[:60])
    tools = mcp.tools()
    if not tools:
        return _result("No MCP tools are available. Check that a server is "
                       "connected under Settings, MCP.")
    by_server: dict[str, list[str]] = {}
    for tool in tools:
        by_server.setdefault(tool["server"], []).append(tool["tool"])
    summary = "; ".join(f"{name}: {len(items)} tool(s)"
                        for name, items in sorted(by_server.items()))
    return _result(f"{len(tools)} MCP tool(s) available — {summary}.",
                   servers={name: items[:40] for name, items in by_server.items()})


def _verb_refresh(mcp, server: str) -> str:
    if not server:
        results = mcp.refresh_all()
        count = sum(1 for r in results.values() if r.get("ok"))
        return _result(f"Refreshed {count} connected MCP server(s).")
    result = mcp.refresh_server(server)
    return _result(result.get("detail") or "Nothing to refresh.")


def _verb_switch(mcp, server: str, enabled: bool) -> str:
    if not server:
        return _result(f"Which MCP server shall I {'enable' if enabled else 'disable'}?",
                       reason="no_target")
    result = mcp.set_enabled(server, enabled)
    verb = "enabled" if enabled else "disabled"
    if not result.get("ok"):
        return _result(result.get("detail") or f"{server} could not be {verb}.",
                       reason="action_failed")
    return _result(f"{server} is now {verb}.", server=server)


def _verb_test(mcp, server: str) -> str:
    if not server:
        return _result("Which MCP server shall I test?", reason="no_target")
    spec = mcp.spec_named(server)
    if spec is None:
        return _result(f"There is no MCP server called '{server}'.",
                       reason="unknown_server", server=server)
    from mcp_host.manager import test_connection
    result = test_connection(spec)
    if not result.get("ok"):
        return _result(f"{server} could not connect: {result.get('detail')}",
                       reason="test_failed", stage=result.get("stage", ""))
    return _result(result.get("detail") or f"{server} connected.",
                   server=server, tools=result.get("tools", 0),
                   resources=result.get("resources", 0),
                   prompts=result.get("prompts", 0))


def handle_mcp(args: dict | None = None, enabled: Any = None,
               on_event: Callable[[str], None] | None = None) -> str:
    """The entry point main.py dispatches to. Always returns a string."""
    args = dict(args or {})
    verb = str(args.get("action") or "").strip().lower()
    if verb not in VERBS:
        return _result(_VERB_HELP, reason="unknown_action")
    if not _allowed(enabled):
        return _result("MCP servers are switched off. Turn them on under "
                       "Settings, MCP.")
    server = str(args.get("server") or "").strip()
    try:
        from mcp_host.manager import get_manager
        mcp = get_manager()
        if verb == "status":
            return _verb_status(mcp)
        if verb == "list":
            return _verb_list(mcp, server)
        if verb == "refresh":
            return _verb_refresh(mcp, server)
        if verb == "enable":
            return _verb_switch(mcp, server, True)
        if verb == "disable":
            return _verb_switch(mcp, server, False)
        if verb == "test":
            return _verb_test(mcp, server)
    except Exception as exc:
        return _result(f"The MCP layer hit a problem: {exc}",
                       reason="mcp_error")
    return _result(_VERB_HELP, reason="unknown_action")
