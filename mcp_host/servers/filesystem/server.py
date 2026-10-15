"""Entry point for the Filesystem MCP Server: ``python -m mcp_host.servers.filesystem.server``."""

from __future__ import annotations

from mcp_host.server import Tool, serve
from mcp_host.servers.filesystem import api

INSTRUCTIONS = (
    "Native Python filesystem MCP server for reading, writing, searching, and "
    "managing workspace files and directories with zero external dependencies."
)


def tools() -> list[Tool]:
    return api.TOOLS


def main() -> int:
    serve("filesystem-tools", "1.0.0", tools(), instructions=INSTRUCTIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
