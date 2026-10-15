"""Entry point for the System Tools MCP Server: ``python -m mcp_host.servers.system_tools.server``."""

from __future__ import annotations

from mcp_host.server import Tool, serve
from mcp_host.servers.system_tools import api

INSTRUCTIONS = (
    "System tools MCP server for host hardware monitoring, process management, "
    "clipboard interaction, desktop notifications, and network inspection."
)


def tools() -> list[Tool]:
    return api.TOOLS


def main() -> int:
    serve("system-tools", "1.0.0", tools(), instructions=INSTRUCTIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
