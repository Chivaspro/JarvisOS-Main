"""A tiny, dependency-free MCP server loop over stdio.

A Python MCP server needs no SDK: the protocol is JSON-RPC 2.0, newline
delimited, on stdin/stdout. This loop implements exactly the part a tool server
must serve — ``initialize``, ``tools/list`` and ``tools/call`` — so a new server
is a list of tools plus one call to :func:`serve`.

Nothing in here knows about the J.A.R.V.I.S. side of the pipe. It is deliberately
the smallest thing that speaks the protocol correctly: unknown methods get a
proper ``METHOD_NOT_FOUND`` error, a crashing handler becomes an ``isError``
result instead of killing the process (a dead server would look like a broken
link to the user, when in fact one call failed), and stdout carries protocol
traffic only — logs go to stderr.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["serve", "tool"]

PROTOCOL_VERSION = "2025-06-18"


def _force_utf8() -> None:
    """Write UTF-8 no matter what code page the console happens to use.

    A Windows console defaults to a legacy code page, and a tool that returns a
    character it cannot encode (a box-drawing arrow in a settings path, for
    instance) would otherwise fail to write that protocol message at all — the
    host would see a silent server and report a timeout for work that had
    actually succeeded. Reconfiguring the streams, with ``errors="replace"`` as
    the backstop, keeps one awkward character from costing a whole answer.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict = field(default_factory=dict)
    handler: Callable[[dict], Any] = None  # type: ignore[assignment]

    def declaration(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema or {"type": "object",
                                                     "properties": {}}}


def tool(name: str, description: str, input_schema: dict | None = None) -> Callable:
    """Decorator: attach MCP metadata to a handler function."""
    def decorate(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
        fn._mcp_tool = Tool(name=name, description=description,
                            input_schema=input_schema or {}, handler=fn)  # type: ignore[attr-defined]
        return fn
    return decorate


def _log(message: str) -> None:
    """Everything that is not protocol goes to stderr."""
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass


def _text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": str(text)}],
            "isError": bool(is_error)}


def serve(server_name: str, server_version: str, tools: list[Tool],
          instructions: str = "") -> None:
    """Run the stdio loop until stdin closes. Never raises out."""
    _force_utf8()
    by_name = {entry.name: entry for entry in tools}
    _log(f"{server_name} {server_version}: {len(tools)} tool(s) ready")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "invalid JSON"}})
            continue
        if not isinstance(message, dict):
            continue
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if method in ("notifications/initialized", "notifications/cancelled"):
            continue
        if method == "initialize":
            _write(_ok(request_id, {
                "protocolVersion": str(params.get("protocolVersion")
                                       or PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server_name, "version": server_version},
                **({"instructions": instructions} if instructions else {}),
            }))
            continue
        if method == "ping":
            _write(_ok(request_id, {}))
            continue
        if method == "tools/list":
            _write(_ok(request_id, {"tools": [t.declaration() for t in tools]}))
            continue
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            entry = by_name.get(name)
            if entry is None:
                _write(_ok(request_id, _text_result(
                    f"{name} is not a tool of {server_name}. Available: "
                    + ", ".join(sorted(by_name)) + ".", is_error=True)))
                continue
            try:
                outcome = entry.handler(arguments)
            except Exception as exc:
                _log(f"{name} failed: {exc}\n{traceback.format_exc()}")
                _write(_ok(request_id, _text_result(
                    f"{name} failed: {type(exc).__name__}: {exc}", is_error=True)))
                continue
            _write(_ok(request_id, _normalise(outcome)))
            continue
        if request_id is None:
            continue
        _write({"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601,
                          "message": f"this server does not implement '{method}'"}})


def _normalise(outcome: Any) -> dict:
    if isinstance(outcome, dict) and "content" in outcome:
        return outcome
    if isinstance(outcome, dict) and "text" in outcome:
        return _text_result(str(outcome.get("text") or ""),
                            bool(outcome.get("is_error")))
    return _text_result("" if outcome is None else str(outcome))


def _ok(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _write(message: dict) -> None:
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    for attempt in (line, json.dumps(message, ensure_ascii=True,
                                     separators=(",", ":"))):
        try:
            sys.stdout.write(attempt + "\n")
            sys.stdout.flush()
            return
        except UnicodeEncodeError:
            continue
        except Exception as exc:
            _log(f"could not write a protocol message: {exc}")
            return
    _log("a protocol message could not be encoded even with escaped characters")
