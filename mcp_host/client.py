"""One stdio MCP server, from this side of the pipe.

The client spawns the server, performs the ``initialize`` handshake, and then
answers three questions: what tools does it offer, what do they look like, and
what happens when one is called. Everything it returns is data — no exception
ever reaches a tool handler, because a dead server must produce a sentence the
user can act on, not a traceback in the middle of a voice turn.

Failure handling is deliberately blunt: one process, one reader thread, a
per-request timeout, and a ``dead`` flag that turns every later call into the
same honest sentence (so a crashed server is reported once, then remembered).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from mcp_host import protocol

__all__ = ["McpClient", "McpTool", "ToolCall"]

#: The server's stderr is kept for error reporting, bounded, so a chatty server
#: cannot grow the process's memory.
_STDERR_LINES = 40

#: How a server says "I do not implement initialize". Current MCP servers
#: (2026-07-28) retired the handshake entirely, so this exact answer is the
#: signal that the counterpart is modern rather than broken.
_NO_SUCH_METHOD_MARKERS = (
    "not found", "not implemented", "not supported", "unknown method",
    "method_not_found", "no such method", "does not implement",
)


def _looks_like_no_such_method(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _NO_SUCH_METHOD_MARKERS)


#: The client metadata every self-describing request carries in ``_meta`` per the
#: current specification (``io.modelcontextprotocol/*`` namespaced fields).
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"


@dataclass
class McpTool:
    """One tool as the server describes it."""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


@dataclass
class ToolCall:
    """The outcome of one ``tools/call``."""

    ok: bool
    text: str = ""
    error: str = ""
    elapsed_s: float = 0.0


class McpClient:
    """A running MCP server process."""

    def __init__(self, name: str, command: list[str], *, cwd: str | None = None,
                 env: dict | None = None, timeout: float = 30.0,
                 start_timeout: float = 20.0,
                 allow_stateless: bool = False) -> None:
        self.name = name
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env or {})
        self.timeout = float(timeout)
        self.start_timeout = float(start_timeout)
        #: Current MCP servers (2026-07-28) retired the ``initialize`` exchange.
        #: When True, a server that answers ``initialize`` with "method not
        #: found" is accepted as a modern stateless server rather than failed:
        #: the writers of the current spec require clients to detect the
        #: counterpart's era and fall back, and a stdio server that has dropped
        #: the handshake is exactly that case. A timeout or a spawn failure is
        #: still a failure — only an explicit "no such method" answer flips it.
        self.allow_stateless = bool(allow_stateless)
        self.stateless = False

        self.process: subprocess.Popen | None = None
        self.server_info: dict = {}
        self.protocol_version = ""
        self.capabilities: dict = {}
        self.tools: list[McpTool] = []
        self.error = ""

        self._next_id = 1
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ready = threading.Event()
        self._dead = False
        self._stderr: list[str] = []
        #: Server-initiated notifications (progress, list_changed, logging),
        #: captured so the session can act on them instead of dropping them.
        self.notifications: list[dict] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the server and complete the handshake. False on any failure."""
        if self.process is not None:
            return True
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) \
                if os.name == "nt" else 0
            env = dict(os.environ)
            env.update({k: str(v) for k, v in self.env.items()})
            env.setdefault("PYTHONUNBUFFERED", "1")
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, cwd=self.cwd, env=env,
                creationflags=creationflags)
        except Exception as exc:
            self._dead = True
            self.error = f"the {self.name} server could not be started: {exc}"
            return False

        threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}-out",
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err",
                         daemon=True).start()

        try:
            response = self._request("initialize", {
                "protocolVersion": protocol.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jarvis-os", "version": "4.0"},
            }, timeout=self.start_timeout)
        except protocol.JsonRpcError as exc:
            if self.allow_stateless and _looks_like_no_such_method(str(exc)):
                # A modern (2026-07-28) server: no handshake at all. The process
                # is alive and its tools are still discoverable, so the session
                # continues statelessly instead of being declared dead.
                self.stateless = True
                self.protocol_version = protocol.LATEST_PROTOCOL_VERSION
                self.server_info = {"name": self.name, "version": ""}
                self.capabilities = {"tools": {}, "resources": {},
                                     "prompts": {}}
                return self.refresh_tools()
            self._fail(str(exc))
            return False

        result = response.get("result") or {}
        self.protocol_version = str(result.get("protocolVersion") or "")
        self.server_info = dict(result.get("serverInfo") or {})
        self.capabilities = dict(result.get("capabilities") or {})
        if (self.protocol_version
                and self.protocol_version not in protocol.SUPPORTED_PROTOCOL_VERSIONS):
            self.error = (f"the {self.name} server speaks protocol "
                          f"{self.protocol_version}, which this host does not know")
        try:
            self._notify("notifications/initialized")
        except protocol.JsonRpcError:
            pass
        return self.refresh_tools()

    def refresh_tools(self) -> bool:
        """Ask for the tool list. Returns False when the server refuses."""
        if not self.capabilities.get("tools"):
            # A server that declares no tool capability offers nothing to publish;
            # that is not an error, it is simply an empty server.
            self.tools = []
            return True
        try:
            response = self._request("tools/list", {})
        except protocol.JsonRpcError as exc:
            self._fail(str(exc))
            return False
        result = response.get("result") or {}
        tools: list[McpTool] = []
        for raw in result.get("tools") or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            tools.append(McpTool(name=str(raw["name"]),
                                 description=str(raw.get("description") or ""),
                                 input_schema=dict(raw.get("inputSchema") or {})))
        self.tools = tools
        return True

    def call_tool(self, tool: str, arguments: dict | None = None,
                  timeout: float | None = None) -> ToolCall:
        """Call one tool. Never raises: a failure is an ``ok=False`` result."""
        started = time.monotonic()
        if self._dead:
            return ToolCall(False, error=self.error or
                            f"the {self.name} server is not running",
                            elapsed_s=round(time.monotonic() - started, 2))
        try:
            response = self._request("tools/call",
                                     {"name": tool,
                                      "arguments": dict(arguments or {})},
                                     timeout=timeout or self.timeout)
        except protocol.JsonRpcError as exc:
            return ToolCall(False, error=str(exc),
                            elapsed_s=round(time.monotonic() - started, 2))
        if "error" in response:
            return ToolCall(False, error=protocol.error_message(response),
                            elapsed_s=round(time.monotonic() - started, 2))
        result = response.get("result") or {}
        text = protocol.result_text(result)
        return ToolCall(not bool(result.get("isError")), text=text,
                        error="" if not result.get("isError") else text,
                        elapsed_s=round(time.monotonic() - started, 2))

    def stop(self, reason: str = "") -> None:
        """Terminate the server and drop everything it owned."""
        self._dead = True
        process, self.process = self.process, None
        self._ready.set()
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        self.error = self.error or (f"the {self.name} server was stopped"
                                    + (f" ({reason})" if reason else ""))

    @property
    def dead(self) -> bool:
        return self._dead

    def stderr_tail(self, limit: int = 5) -> str:
        with self._lock:
            return "\n".join(self._stderr[-limit:])

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": bool(self.process) and not self._dead,
            "server": dict(self.server_info),
            "protocol": self.protocol_version,
            "tools": [tool.name for tool in self.tools],
            "error": self.error,
            "stderr": self.stderr_tail(3),
        }

    # ── the public transport surface (used by mcp_host.transport) ────────────

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None,
                request_id: int | None = None) -> dict:
        """Raw JSON-RPC request. Raises ``protocol.JsonRpcError`` on failure.

        ``request_id`` lets the caller own the id, which is what makes a request
        cancellable: the session records the id, and a cancel decision can name
        the exact call instead of guessing.
        """
        return self._request(method, params, timeout=timeout,
                             request_id=request_id)

    def notify(self, method: str, params: dict | None = None) -> None:
        """Raw JSON-RPC notification (no reply is expected)."""
        self._notify(method, params)

    def cancel(self, request_id: int) -> None:
        """Abandon an in-flight request the way stdio requires: a notification."""
        try:
            self._notify("notifications/cancelled",
                         {"requestId": int(request_id)})
        except Exception:
            pass

    def _with_meta(self, params: dict | None) -> dict:
        """Add the self-describing ``_meta`` block a stateless server expects.

        Only added in stateless mode: a legacy server would see an unknown
        ``_meta`` member inside a request, but the current specification makes it
        the home for protocol version and client identity, so a modern server
        requires it and a legacy one never notices its absence.
        """
        out = dict(params or {})
        if self.stateless and "_meta" not in out:
            out["_meta"] = {
                _META_PROTOCOL_VERSION: protocol.LATEST_PROTOCOL_VERSION,
                _META_CLIENT_INFO: {"name": "jarvis-os", "version": "4.0"},
            }
        return out

    # ── internals ─────────────────────────────────────────────────────────────

    def _fail(self, message: str) -> None:
        self._dead = True
        self.error = message
        self._ready.set()
        with self._lock:
            for slot in self._pending.values():
                slot["error"] = message
                slot["event"].set()

    def _ident(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _send(self, message: dict) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise protocol.JsonRpcError(f"the {self.name} server has no stdin")
        line = protocol.dumps(message)
        with self._write_lock:
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except Exception as exc:
                self._fail(f"the {self.name} server stopped accepting input: {exc}")
                raise protocol.JsonRpcError(self.error) from exc

    def _request(self, method: str, params: dict | None,
                 timeout: float | None = None,
                 request_id: int | None = None) -> dict:
        params = self._with_meta(params)
        request_id = self._ident() if request_id is None else int(request_id)
        slot = {"event": threading.Event(), "message": None, "error": ""}
        with self._lock:
            self._pending[request_id] = slot
        try:
            self._send(protocol.request(method, params, request_id))
        except protocol.JsonRpcError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        waited = float(timeout if timeout is not None else self.timeout)
        if not slot["event"].wait(waited):
            with self._lock:
                self._pending.pop(request_id, None)
            detail = self.stderr_tail(3)
            self._fail(f"the {self.name} server did not answer '{method}' within "
                       f"{waited:.0f}s"
                       + (f" — it said: {detail}" if detail else ""))
            raise protocol.JsonRpcError(self.error)
        with self._lock:
            self._pending.pop(request_id, None)
        if slot["error"]:
            raise protocol.JsonRpcError(str(slot["error"]))
        return dict(slot["message"] or {})

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send(protocol.notification(method, params))

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = protocol.loads(line)
                except protocol.JsonRpcError:
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    try:
                        request_id = int(message.get("id"))
                    except (TypeError, ValueError):
                        continue
                    with self._lock:
                        slot = self._pending.get(request_id)
                    if slot is not None:
                        slot["message"] = message
                        slot["event"].set()
                    continue
                # A server notification (progress, logging, a changed tool
                # list). These carry no id and are recorded rather than
                # discarded: progress is what the activity feed renders, and a
                # ``list_changed`` notice is what triggers a re-discovery.
                if "method" in message and "id" not in message:
                    self._note_notification(message)
                    continue
                # A server-initiated *request* (the deprecated
                # sampling/elicitation/roots patterns). Nothing in this host
                # serves one, so it is acknowledged inertly rather than guessed
                # at. Multi Round-Trip Requests replace this pattern.
                if "id" in message and "method" in message:
                    try:
                        self._send({"jsonrpc": "2.0", "id": message["id"],
                                    "error": {"code": protocol.METHOD_NOT_FOUND,
                                              "message": "this host does not serve "
                                                         "server-initiated requests"}})
                    except protocol.JsonRpcError:
                        pass
        except Exception:
            pass
        finally:
            if not self._dead:
                self._fail(f"the {self.name} server closed its output")

    def _note_notification(self, message: dict) -> None:
        """Keep a bounded record of what the server told us unprompted."""
        with self._lock:
            self.notifications.append(message)
            if len(self.notifications) > 100:
                del self.notifications[:-100]

    def drain_notifications(self) -> list[dict]:
        """Take the notifications seen so far, leaving the buffer empty."""
        with self._lock:
            out = list(self.notifications)
            self.notifications.clear()
            return out

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                with self._lock:
                    self._stderr.append(line.rstrip())
                    if len(self._stderr) > _STDERR_LINES:
                        del self._stderr[:-_STDERR_LINES]
        except Exception:
            pass
