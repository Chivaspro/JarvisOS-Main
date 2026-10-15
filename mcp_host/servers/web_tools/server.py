"""Entry point for the Web Tools MCP Server: ``python -m mcp_host.servers.web_tools.server``."""

from __future__ import annotations

from mcp_host.server import Tool, serve
from mcp_host.servers.web_tools import api

INSTRUCTIONS = (
    "Web tools MCP server for fetching webpage contents, DuckDuckGo web searching, "
    "and testing HTTP endpoints."
)


def tools() -> list[Tool]:
    return api.TOOLS


def main() -> int:
    serve("web-tools", "1.0.0", tools(), instructions=INSTRUCTIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
