"""One MCP session: capability negotiation, discovery, calls, cancellation.

A transport knows how to carry a message; a session knows what to *ask*. It is
the layer that turns the protocol's verbs into the four things the rest of
J.A.R.V.I.S. needs:

* **capabilities** — what this server actually offers (tools / resources /
  prompts), negotiated rather than assumed. A server that declares no resource
  capability is never asked for resources, and a server that declared nothing at
  all (a stateless one that was never discovered) is asked politely and a
  "method not found" is accepted as "it does not have those" rather than an
  error;
* **discovery** — the tool/resource/prompt lists, paginated, and cacheable: the
  current specification lets a list result carry ``ttlMs`` / ``cacheScope``, so
  a list that is still fresh is served from memory instead of re-fetched on
  every render;
* **calls** — ``tools/call``, ``resources/read``, ``prompts/get``, each with the
  request id recorded so the operation can be cancelled, and with any
  ``notifications/progress`` the server emits during the call handed to a
  progress callback;
* **change tracking** — a ``notifications/tools/list_changed`` (or resources /
  prompts) that arrived between calls is reported, so the manager can
  invalidate the cache and re-discover without a restart.

Failure is always a sentence, never an exception crossing into a voice turn:
:meth:`call_tool` returns an ``ok=False`` result carrying the reason.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from mcp_host import protocol
from mcp_host.client import McpTool, ToolCall
from mcp_host.transport import McpTransport, transport_for

__all__ = ["McpSession", "CHANGE_METHODS"]

#: The legacy change notifications a server may send between calls.
CHANGE_METHODS = {
    "notifications/tools/list_changed": "tools",
    "notifications/resources/list_changed": "resources",
    "notifications/prompts/list_changed": "prompts",
}

#: How many pages of a paginated list are walked before giving up. A server that
#: never stops returning a cursor must not spin forever.
_MAX_PAGES = 25


class McpSession:
    """A live, capability-aware conversation with one MCP server."""

    def __init__(self, spec: Any, transport: McpTransport | None = None) -> None:
        self.spec = spec
        self.transport = transport or transport_for(spec)
        self.tools: list[McpTool] = []
        self.resources: list[dict] = []
        self.prompts: list[dict] = []
        self.discovery_error = ""
        self.last_error = ""
        #: Enough about the last call for the developer "Run Tool" panel.
        self.last_call: dict = {}
        self._cache: dict[str, tuple[float, list]] = {}
        self._active: dict[int, str] = {}
        self._next_id = 1
        self._pending_changes: set[str] = set()
        self._progress_calls: list[str] = []

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return str(getattr(self.spec, "name", "") or self.transport.name)

    @property
    def running(self) -> bool:
        return bool(getattr(self.transport, "running", False))

    @property
    def dead(self) -> bool:
        return bool(getattr(self.transport, "dead", False))

    @property
    def error(self) -> str:
        return str(self.last_error or getattr(self.transport, "error", "") or "")

    @property
    def era(self) -> str:
        return str(getattr(self.transport, "era", "legacy"))

    @property
    def protocol_version(self) -> str:
        return str(getattr(self.transport, "protocol_version", "") or "")

    @property
    def server_info(self) -> dict:
        return dict(getattr(self.transport, "server_info", {}) or {})

    def capabilities(self) -> dict:
        return dict(getattr(self.transport, "capabilities", {}) or {})

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Connect, then negotiate capabilities and discover what is offered."""
        ok = False
        try:
            ok = bool(self.transport.start())
        except Exception as exc:                       # never let a binding raise out
            self.last_error = f"the {self.name} server could not be connected: {exc}"
            return False
        if not ok:
            self.last_error = self.transport.error or \
                f"the {self.name} server did not start"
            return False
        self.discover(force=True)
        return True

    def close(self, reason: str = "") -> None:
        try:
            self.transport.close(reason)
        except Exception:
            pass

    def cancel(self, request_id: int) -> bool:
        """Abandon one in-flight request through the binding's own rule."""
        try:
            self.transport.cancel(int(request_id))
            return True
        except Exception:
            return False

    def cancel_all(self) -> None:
        for request_id in list(self._active):
            self.cancel(request_id)

    def _ident(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    # ── capability negotiation ────────────────────────────────────────────────

    def _offers(self, key: str) -> bool:
        """Does the server claim this capability?

        An empty capability map means the server never described itself (a
        stateless server that was not discovered), so the answer is "try it and
        see" — which the discovery methods below implement by tolerating a
        method-not-found. A non-empty map is authoritative: a server that
        declared only tools is not asked about prompts.
        """
        caps = self.capabilities()
        if not caps:
            return True
        if key in caps:
            return True
        # Some servers nest the feature under a differently-named key.
        aliases = {"resources": ("resources", "resource"),
                   "prompts": ("prompts", "prompt"),
                   "tools": ("tools", "tool")}
        return any(alias in caps for alias in aliases.get(key, (key,)))

    # ── discovery ─────────────────────────────────────────────────────────────

    def discover(self, force: bool = False) -> dict:
        """Refresh tools, resources and prompts. Never raises."""
        self.discovery_error = ""
        self.tools = self.refresh_tools(force=force)
        self.resources = self.refresh_resources(force=force)
        self.prompts = self.refresh_prompts(force=force)
        return {"tools": len(self.tools), "resources": len(self.resources),
                "prompts": len(self.prompts), "error": self.discovery_error}

    def _cached(self, kind: str) -> list | None:
        entry = self._cache.get(kind)
        if entry is None:
            return None
        expires, items = entry
        if expires and time.monotonic() < expires:
            return list(items)
        return None

    def _store(self, kind: str, items: list, result: Any) -> None:
        ttl_ms = 0
        if isinstance(result, dict):
            raw = result.get("ttlMs")
            if raw is None:
                meta = result.get("_meta")
                if isinstance(meta, dict):
                    raw = meta.get("ttlMs")
            try:
                ttl_ms = max(0, int(raw or 0))
            except (TypeError, ValueError):
                ttl_ms = 0
        expires = time.monotonic() + (ttl_ms / 1000.0) if ttl_ms else 0.0
        self._cache[kind] = (expires, list(items))

    def _list(self, method: str, key: str, kind: str, force: bool) -> list:
        """Walk a paginated list call, honoring the cache and capability gate."""
        if not force:
            cached = self._cached(kind)
            if cached is not None:
                return cached
        if not self._offers(kind):
            return []
        items: list = []
        cursor = ""
        for _page in range(_MAX_PAGES):
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            try:
                message = self.transport.request(method, params)
            except protocol.JsonRpcError as exc:
                text = str(exc)
                if _no_such_method(text) or "not found" in text.lower():
                    # The server simply does not implement this collection.
                    return []
                self.discovery_error = text
                return items
            if isinstance(message, dict) and message.get("error"):
                self.discovery_error = protocol.error_message(message)
                return items
            result = (message or {}).get("result") or {}
            page = result.get(key)
            if isinstance(page, list):
                items.extend(entry for entry in page if isinstance(entry, dict))
            self._store(kind, items, result)
            cursor = str(result.get("nextCursor") or "")
            if not cursor or not page:
                break
        return items

    def refresh_tools(self, force: bool = True) -> list[McpTool]:
        raw = self._list("tools/list", "tools", "tools", force)
        tools: list[McpTool] = []
        for entry in raw:
            name = str(entry.get("name") or "")
            if not name:
                continue
            tools.append(McpTool(
                name=name,
                description=str(entry.get("description") or ""),
                input_schema=dict(entry.get("inputSchema") or {})))
        return tools

    def refresh_resources(self, force: bool = True) -> list[dict]:
        return self._list("resources/list", "resources", "resources", force)

    def refresh_prompts(self, force: bool = True) -> list[dict]:
        return self._list("prompts/list", "prompts", "prompts", force)

    # ── calls ─────────────────────────────────────────────────────────────────

    def call_tool(self, tool: str, arguments: dict | None = None,
                  timeout: float | None = None,
                  on_progress: Callable[[dict], None] | None = None) -> ToolCall:
        """Call one tool. Never raises: a failure is an ``ok=False`` result."""
        started = time.monotonic()
        if self.dead or not self.running:
            return ToolCall(False, error=self.error or
                            f"the {self.name} server is not running",
                            elapsed_s=round(time.monotonic() - started, 2))
        request_id = self._ident()
        self._active[request_id] = f"tools/call:{tool}"
        try:
            message = self.transport.request(
                "tools/call",
                {"name": str(tool), "arguments": dict(arguments or {})},
                timeout=timeout, request_id=request_id)
        except protocol.JsonRpcError as exc:
            self._active.pop(request_id, None)
            self.last_call = {"method": "tools/call", "tool": str(tool),
                              "ok": False, "error": str(exc),
                              "elapsed_s": round(time.monotonic() - started, 2)}
            return ToolCall(False, error=str(exc),
                            elapsed_s=self.last_call["elapsed_s"])
        finally:
            self._active.pop(request_id, None)
            self._drain_progress(on_progress)
        if isinstance(message, dict) and message.get("error"):
            text = protocol.error_message(message)
            self.last_call = {"method": "tools/call", "tool": str(tool),
                              "ok": False, "error": text,
                              "elapsed_s": round(time.monotonic() - started, 2)}
            return ToolCall(False, error=text,
                            elapsed_s=self.last_call["elapsed_s"])
        result = (message or {}).get("result") or {}
        text = protocol.result_text(result)
        elapsed = round(time.monotonic() - started, 2)
        self.last_call = {"method": "tools/call", "tool": str(tool),
                          "ok": not bool(result.get("isError")), "text": text,
                          "result": result, "elapsed_s": elapsed}
        return ToolCall(not bool(result.get("isError")), text=text,
                        error="" if not result.get("isError") else text,
                        elapsed_s=elapsed)

    def read_resource(self, uri: str, timeout: float | None = None) -> ToolCall:
        """Read one resource by URI. Returns its text, or an honest failure."""
        started = time.monotonic()
        request_id = self._ident()
        self._active[request_id] = f"resources/read:{uri}"
        try:
            message = self.transport.request("resources/read", {"uri": str(uri)},
                                             timeout=timeout,
                                             request_id=request_id)
        except protocol.JsonRpcError as exc:
            return ToolCall(False, error=str(exc),
                            elapsed_s=round(time.monotonic() - started, 2))
        finally:
            self._active.pop(request_id, None)
        if isinstance(message, dict) and message.get("error"):
            return ToolCall(False, error=protocol.error_message(message),
                            elapsed_s=round(time.monotonic() - started, 2))
        result = (message or {}).get("result") or {}
        text = protocol.result_text(result)
        return ToolCall(True, text=text,
                        elapsed_s=round(time.monotonic() - started, 2))

    def get_prompt(self, name: str, arguments: dict | None = None,
                   timeout: float | None = None) -> ToolCall:
        """Resolve one prompt. Returns its rendered text, or a failure."""
        started = time.monotonic()
        params: dict = {"name": str(name)}
        if arguments:
            params["arguments"] = dict(arguments)
        request_id = self._ident()
        self._active[request_id] = f"prompts/get:{name}"
        try:
            message = self.transport.request("prompts/get", params,
                                             timeout=timeout,
                                             request_id=request_id)
        except protocol.JsonRpcError as exc:
            return ToolCall(False, error=str(exc),
                            elapsed_s=round(time.monotonic() - started, 2))
        finally:
            self._active.pop(request_id, None)
        if isinstance(message, dict) and message.get("error"):
            return ToolCall(False, error=protocol.error_message(message),
                            elapsed_s=round(time.monotonic() - started, 2))
        result = (message or {}).get("result") or {}
        return ToolCall(True, text=protocol.result_text(result),
                        elapsed_s=round(time.monotonic() - started, 2))

    # ── notifications ─────────────────────────────────────────────────────────

    def _drain_progress(self, on_progress: Callable[[dict], None] | None) -> None:
        """Hand any progress the server emitted to the caller's callback."""
        for note in self._drain_raw():
            method = str(note.get("method") or "")
            if method == "notifications/progress":
                params = note.get("params") if isinstance(note.get("params"), dict) else {}
                self._progress_calls.append(_progress_line(params))
                if callable(on_progress):
                    try:
                        on_progress(params)
                    except Exception:
                        pass
            elif method in CHANGE_METHODS:
                self._pending_changes.add(CHANGE_METHODS[method])
                self._cache.pop(CHANGE_METHODS[method], None)

    def _drain_raw(self) -> list[dict]:
        drain = getattr(self.transport, "drain_notifications", None)
        if callable(drain):
            try:
                return list(drain() or [])
            except Exception:
                return []
        return []

    def take_changes(self) -> set[str]:
        """Which collections changed since the last call (and clear the flag)."""
        out = set(self._pending_changes)
        self._pending_changes.clear()
        return out

    def progress_lines(self) -> list[str]:
        out = list(self._progress_calls)
        self._progress_calls.clear()
        return out

    def poll(self, on_progress: Callable[[str], None] | None = None) -> None:
        """Process notifications that arrived outside a call (SSE idle stream)."""
        line_cb = None
        if callable(on_progress):
            def line_cb(text: str) -> None:      # noqa: E731 - tiny adapter
                try:
                    on_progress(text)
                except Exception:
                    pass
        for note in self._drain_raw():
            method = str(note.get("method") or "")
            if method == "notifications/progress":
                params = note.get("params") if isinstance(note.get("params"), dict) else {}
                if line_cb:
                    line_cb(_progress_line(params))
            elif method in CHANGE_METHODS:
                self._pending_changes.add(CHANGE_METHODS[method])
                self._cache.pop(CHANGE_METHODS[method], None)

    # ── state ─────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        base = {}
        try:
            base = dict(self.transport.status() or {})
        except Exception:
            base = {}
        base.update({
            "name": self.name,
            "running": self.running,
            "tools": [tool.name for tool in self.tools],
            "resources": len(self.resources),
            "prompts": len(self.prompts),
            "server": self.server_info,
            "protocol": self.protocol_version,
            "era": self.era,
            "error": self.error,
        })
        return base


def _no_such_method(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in (
        "not implemented", "method_not_found", "no such method",
        "does not implement", "unsupported method", "unknown method"))


def _progress_line(params: dict) -> str:
    """Turn one progress notification into a short, human line."""
    message = str(params.get("message") or "").strip()
    total = params.get("total")
    current = params.get("progress")
    try:
        if total:
            percent = max(0, min(100, int(100.0 * float(current) / float(total))))
            base = f"{message} {percent}%" if message else f"{percent}%"
            return base.strip()
        if current is not None:
            value = int(float(current)) if float(current).is_integer() else current
            return f"{message} {value}".strip() if message else str(value)
    except (TypeError, ValueError):
        pass
    return message or "working…"
