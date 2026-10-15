"""A local Model Context Protocol host for J.A.R.V.I.S.

Why this package is called ``mcp_host`` and not ``mcp``: the PyPI distribution
``mcp`` (the official Python SDK) is installed in this environment, and a
top-level ``mcp/`` directory in the project would shadow it for every other
dependency that imports it. The host here is deliberately dependency-free — it
speaks the protocol directly — so the name is the only thing the two share.

Layout:

* ``protocol.py`` — newline-delimited JSON-RPC 2.0 framing and error codes
* ``client.py``   — one stdio MCP server process (initialize / tools / call)
* ``catalog.py``  — which servers exist, and how they are launched
* ``bridge.py``   — connects every enabled server and publishes its tools to the
  assistant as ordinary tools, hot-registered through the plugin loader
* ``server.py``   — the loop a Python MCP server runs on
* ``servers/``    — the servers shipped with the project (a package, which is
  why the catalogue above is not called ``servers.py``)
"""

from __future__ import annotations

__all__ = ["bridge", "catalog", "client", "protocol", "server", "servers"]
