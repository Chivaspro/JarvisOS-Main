"""Entry point for the Memory Vault MCP Server: ``python -m mcp_host.servers.memory_vault.server``."""

from __future__ import annotations

from mcp_host.server import Tool, serve
from mcp_host.servers.memory_vault import api

INSTRUCTIONS = (
    "Memory vault MCP server for searching, reading, creating, and updating "
    "notes in the J.A.R.V.I.S. Obsidian Vault and knowledge base."
)


def tools() -> list[Tool]:
    return api.TOOLS


def main() -> int:
    serve("memory-vault", "1.0.0", tools(), instructions=INSTRUCTIONS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
