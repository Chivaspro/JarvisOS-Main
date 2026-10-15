"""Transport abstraction: how MCP messages leave this process.

The protocol is transport-agnostic. What differs between bindings is *framing*
and *lifecycle*, not meaning:

* ``stdio`` — newline-delimited JSON-RPC over a client-launched subprocess.
* ``Streamable HTTP`` — one HTTP POST per message to a single MCP endpoint,
  whose reply arrives either as a JSON object or as a request-scoped SSE stream.

Everything above this module (the session, the manager, the bridge) talks to a
:class:`McpTransport` and never to a pipe or a socket. That is what lets a future
binding — a WebSocket, a custom transport for some new server — be added without
touching the parts that already work.

The stdio transport is a thin adapter over :class:`mcp_host.client.McpClient`,
the implementation this project has already exercised end-to-end over real pipes.
No framing logic is duplicated here; the adapter only exposes the client through
the shape the transport interface promises.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["McpTransport", "StdioTransport", "TRANSPORT_STDIO", "TRANSPORT_HTTP"]

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"


@runtime_checkable
class McpTransport(Protocol):
    """One live connection to one MCP server.

    A transport owns framing and lifecycle only. It knows nothing about tools,
    permissions or reconnect policy — those belong to the session and the
    manager, so every binding stays interchangeable.
    """

    name: str

    def start(self) -> bool:
        """Connect (spawn the process / open the session). False on failure."""

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None) -> dict:
        """Send a request and return the full JSON-RPC response message."""

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no reply is expected)."""

    def cancel(self, request_id: int) -> None:
        """Abandon one in-flight request, per this binding's cancellation rule."""

    def close(self, reason: str = "") -> None:
        """Tear the connection down and release everything it owned."""

    @property
    def running(self) -> bool:
        """True while the connection is usable."""

    @property
    def dead(self) -> bool:
        """True once the connection has failed or been closed."""

    @property
    def error(self) -> str:
        """The human sentence explaining the last failure ("" when healthy)."""

    @property
    def server_info(self) -> dict:
        """``serverInfo`` as the server declared it (or a name-only stub)."""

    @property
    def protocol_version(self) -> str:
        """The protocol version this connection settled on."""

    @property
    def capabilities(self) -> dict:
        """The server's declared capabilities (tools/resources/prompts/…)."""

    @property
    def era(self) -> str:
        """``"modern"`` (2026-07-28, stateless) or ``"legacy"`` (session-based)."""

    def stderr_tail(self, limit: int = 5) -> str:
        """The last few diagnostic lines, for the log viewer."""

    def status(self) -> dict:
        """A flat, JSON-safe snapshot for the Settings window."""


class StdioTransport:
    """The proven stdio client, behind the transport interface.

    ``allow_stateless`` lets a server that has dropped the ``initialize``
    handshake (the current 2026-07-28 behaviour) be accepted as a modern
    server instead of being declared dead — the spec requires clients to detect
    the counterpart's era and fall back, and this is that fallback.
    """

    def __init__(self, name: str, command: list[str], *, cwd: str | None = None,
                 env: dict | None = None, timeout: float = 30.0,
                 start_timeout: float = 20.0,
                 allow_stateless: bool = True) -> None:
        from mcp_host.client import McpClient

        self._client = McpClient(
            name, command, cwd=cwd, env=env, timeout=timeout,
            start_timeout=start_timeout, allow_stateless=allow_stateless)

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._client.name

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        return self._client.start()

    def close(self, reason: str = "") -> None:
        self._client.stop(reason)

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None,
                request_id: int | None = None) -> dict:
        return self._client.request(method, params, timeout=timeout,
                                    request_id=request_id)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._client.notify(method, params)

    def cancel(self, request_id: int) -> None:
        self._client.cancel(request_id)

    # ── state ─────────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return bool(self._client.process) and not self._client.dead

    @property
    def dead(self) -> bool:
        return bool(self._client.dead)

    @property
    def error(self) -> str:
        return str(self._client.error or "")

    @property
    def server_info(self) -> dict:
        return dict(self._client.server_info or {})

    @property
    def protocol_version(self) -> str:
        return str(self._client.protocol_version or "")

    @property
    def capabilities(self) -> dict:
        return dict(self._client.capabilities or {})

    @property
    def era(self) -> str:
        return "modern" if getattr(self._client, "stateless", False) else "legacy"

    def stderr_tail(self, limit: int = 5) -> str:
        return self._client.stderr_tail(limit)

    def status(self) -> dict:
        row = self._client.status()
        row["transport"] = TRANSPORT_STDIO
        row["era"] = self.era
        return row


def transport_for(spec: Any) -> McpTransport:
    """Build the right transport for one server spec.

    Kept here so the manager has exactly one place that decides which binding a
    server uses, and so adding a transport is one branch, not a search.
    """
    transport = str(getattr(spec, "transport", TRANSPORT_STDIO) or
                    TRANSPORT_STDIO).strip().lower()
    if transport in (TRANSPORT_HTTP, "streamable_http", "streamable-http",
                     "http+sse", "sse", "remote"):
        from mcp_host.http_client import StreamableHttpTransport

        return StreamableHttpTransport(
            name=str(getattr(spec, "name", "") or "mcp"),
            url=str(getattr(spec, "url", "") or ""),
            headers=_resolved_headers(spec),
            timeout=float(getattr(spec, "timeout", 60.0) or 60.0),
            start_timeout=float(getattr(spec, "timeout", 20.0) or 20.0),
            allow_insecure_http=bool(getattr(spec, "allow_insecure_http", False)),
            protocol_override=str(getattr(spec, "protocol_override", "") or ""),
        )
    command = list(getattr(spec, "command", []) or [])
    return StdioTransport(
        name=str(getattr(spec, "name", "") or "mcp"),
        command=command,
        cwd=(str(getattr(spec, "cwd", "") or "") or None),
        env=_resolved_env(spec),
        timeout=float(getattr(spec, "timeout", 60.0) or 60.0),
        start_timeout=float(getattr(spec, "start_timeout", 20.0) or 20.0),
    )


def _resolved_env(spec: Any) -> dict:
    """The environment a stdio server is launched with, secrets included.

    Secret values are read from the vault at launch time and never live on the
    spec object, so a spec can be logged or rendered without leaking anything.
    """
    plain = dict(getattr(spec, "env", {}) or {})
    try:
        from mcp_host import secrets as secret_store

        plain.update(secret_store.server_env(str(getattr(spec, "name", "") or "")))
    except Exception:
        pass
    return plain


def _resolved_headers(spec: Any) -> dict:
    """The HTTP headers for a remote server, with credentials attached.

    Same rule as :func:`_resolved_env`: only the header *names* are stored on the
    spec, so the value is fetched here and never written to the server list.
    """
    headers = dict(getattr(spec, "headers", {}) or {})
    try:
        from mcp_host import secrets as secret_store

        headers.update(secret_store.server_headers(str(getattr(spec, "name", "") or "")))
    except Exception:
        pass
    return {str(k): str(v) for k, v in headers.items() if str(k).strip()}
