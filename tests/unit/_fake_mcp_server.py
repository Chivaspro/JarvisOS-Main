"""A tiny MCP server used by the host tests (never collected by pytest).

It uses the same ``mcp_host.server`` loop the shipped servers use, so the tests
exercise the protocol on both sides of the pipe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_host.server import Tool, serve  # noqa: E402


def _echo(args: dict) -> str:
    return f"echo: {args.get('text', '')}"


def _greet(args: dict) -> dict:
    return {"text": f"Hello {args.get('name') or 'sir'}.", "is_error": False}


def _explode(args: dict) -> str:
    raise RuntimeError("the fake server was asked to explode")


def _crash(args: dict) -> str:
    """Kill the process, so the client has to notice a server that died."""
    os._exit(3)
    return "unreachable"


TOOLS = [
    Tool(name="echo", description="Echo the text back.",
         input_schema={"type": "object",
                       "properties": {"text": {"type": "string",
                                               "description": "Text to echo."}},
                       "required": ["text"]},
         handler=_echo),
    Tool(name="greet", description="Greet somebody by name.",
         input_schema={"type": "object",
                       "properties": {"name": {"type": "string"}}},
         handler=_greet),
    Tool(name="explode", description="Always fails.",
         input_schema={"type": "object", "properties": {}},
         handler=_explode),
    Tool(name="crash", description="Kills the server process.",
         input_schema={"type": "object", "properties": {}},
         handler=_crash),
]

if __name__ == "__main__":
    serve("fake-server", "9.9.9", TOOLS)
