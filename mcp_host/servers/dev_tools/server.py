"""Entry point for the Developer Tools MCP Server: ``python -m mcp_host.servers.dev_tools.server``."""

from __future__ import annotations

from mcp_host.server import Tool, serve
from mcp_host.servers.dev_tools import api

INSTRUCTIONS = (
    "Developer tools MCP server for terminal command execution, Git repository "
    "inspection, diffs, and Python expression evaluation."
)


def tools() -> list[Tool]:
    return api.TOOLS


def main() -> int:
    serve("dev-tools", "1.0.0", tools(), instructions=INSTRUCTIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
