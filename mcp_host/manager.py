"""The one MCP Manager: every server, every session, one owner.

This is the layer the rest of J.A.R.V.I.S. talks to. It knows which servers are
configured, which are running, what each one offers, and what went wrong when one
did not come up. Nothing else in the project owns an MCP session.

**The state model is deliberately honest about four different things**, because
collapsing them is how an integration lies:

* ``enabled``      — the user's switch; off means off, with configuration kept;
* ``connected``    — the transport is up (process alive / endpoint answering);
* ``authenticated``— no credential refusal was seen (a 401 is *not* "connected");
* ``tools_ready``  — at least one tool was actually discovered.

So a server can be ``● Connected, 0 tools`` or ``● Connected ⚠ auth needs
attention`` and the Settings window can say exactly that.

Lifecycle: ``disabled → connecting → initializing → discovering → connected``,
with ``error``, ``reconnecting``, ``stopping`` and ``stopped`` as the honest
alternatives. A server is never "Connected" unless its handshake (or modern
capability discovery) actually succeeded.

Reconnect is opt-in per server, bounded, and backed off — never an aggressive
loop. Shutdown is a single call that closes every session and terminates every
managed local process, wired into the application's own close path so no orphan
MCP process outlives the window.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp_host import catalog, secrets as secret_store
from mcp_host.session import McpSession

__all__ = ["McpManager", "get_manager", "reset_manager", "test_connection",
           "STATES"]

#: Every state a server can be in. The UI renders these verbatim.
STATES = ("disabled", "disconnected", "connecting", "initializing",
          "discovering", "connected", "error", "reconnecting", "stopping",
          "stopped")

#: How many log lines per server are retained for the log viewer.
_LOG_LINES = 200

#: How often the monitor thread checks for dead sessions and pending reconnects.
_MONITOR_INTERVAL_S = 3.0


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _looks_like_auth_failure(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in (
        "401", "403", "refused the credentials", "denied access", "unauthorized",
        "invalid_key", "invalid api key", "token", "expired", "forbidden",
        "authentication"))


@dataclass
class ServerRuntime:
    """Everything the manager holds for one server."""

    spec: catalog.ServerSpec
    session: McpSession | None = None
    state: str = "disconnected"
    detail: str = ""
    last_error: str = ""
    connected: bool = False
    authenticated: bool = True
    tools_ready: bool = False
    reconnect_attempts: int = 0
    next_retry_at: float = 0.0
    connected_at: float = 0.0
    log: deque = field(default_factory=lambda: deque(maxlen=_LOG_LINES))

    @property
    def name(self) -> str:
        return self.spec.name

    def note(self, text: str) -> None:
        self.log.append(f"[{_now()}] {secret_store.redact(text)}")


class McpManager:
    """Owns every configured MCP server."""

    def __init__(self, specs: list[catalog.ServerSpec] | None = None, *,
                 on_activity: Callable[[str], None] | None = None,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self._specs: list[catalog.ServerSpec] = list(
            specs if specs is not None else catalog.load_servers())
        self._runtimes: dict[str, ServerRuntime] = {
            spec.name: ServerRuntime(spec=spec) for spec in self._specs}
        self._lock = threading.RLock()
        self._on_activity = on_activity
        self._on_change = on_change
        self._started = False
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self._publish_lock = threading.Lock()
        self._paused = False

    # ── wiring ────────────────────────────────────────────────────────────────

    def set_activity(self, callback: Callable[[str], None] | None) -> None:
        self._on_activity = callback

    def set_change(self, callback: Callable[[dict], None] | None) -> None:
        self._on_change = callback

    def _say(self, text: str) -> None:
        line = secret_store.redact(str(text or ""))
        if callable(self._on_activity):
            try:
                self._on_activity(line)
            except Exception:
                pass

    def _changed(self) -> None:
        if callable(self._on_change):
            try:
                self._on_change(self.status())
            except Exception:
                pass

    # ── startup ───────────────────────────────────────────────────────────────

    def start(self, background: bool = True) -> dict:
        """Connect every enabled server, then keep watch."""
        with self._lock:
            if self._started:
                return self.status()
            self._started = True
            self._stop.clear()
        if background:
            threading.Thread(target=self._connect_all, name="mcp-connect",
                             daemon=True).start()
            self._start_monitor()
            return {"servers": [], "tools": 0,
                    "log": ["MCP startup began in the background"]}
        self._connect_all()
        self._start_monitor()
        return self.status()

    def _connect_all(self) -> None:
        for runtime in list(self._runtimes.values()):
            if self._stop.is_set():
                return
            self._connect(runtime)

    def _start_monitor(self) -> None:
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._monitor = threading.Thread(target=self._monitor_loop,
                                             name="mcp-monitor", daemon=True)
            self._monitor.start()

    def _monitor_loop(self) -> None:
        """Watch for dead sessions and due reconnects; never spin aggressively."""
        while not self._stop.wait(_MONITOR_INTERVAL_S):
            if self._paused:
                continue
            try:
                self.poll_changes()
            except Exception:
                pass
            now = time.monotonic()
            for runtime in list(self._runtimes.values()):
                if not runtime.spec.enabled or self._stop.is_set():
                    continue
                session = runtime.session
                if session is not None and not session.running and \
                        runtime.state == "connected":
                    runtime.note("the server stopped unexpectedly")
                    self._mark_dead(runtime, "the server stopped unexpectedly")
                if runtime.state == "error" and runtime.next_retry_at and \
                        now >= runtime.next_retry_at:
                    self._reconnect(runtime)

    def poll_changes(self) -> dict:
        """Re-discover any server whose tools/resources/prompts changed.

        Both notification eras are handled here: a modern server's
        ``subscriptions/listen`` stream and a legacy server's ``list_changed``
        notification both land in the session's change set, and a change
        triggers the same re-discovery and re-publish.
        """
        changed: dict[str, set] = {}
        for runtime in list(self._runtimes.values()):
            session = runtime.session
            if session is None or not session.running:
                continue
            try:
                events = session.take_changes()
                for line in session.progress_lines():
                    runtime.note(f"progress: {line}")
                    self._say(f"{runtime.name} — {line}")
            except Exception:
                continue
            if events:
                changed[runtime.name] = events
                runtime.note("the server reported changed capabilities — "
                             "re-discovering")
                try:
                    session.discover(force=True)
                    runtime.tools_ready = bool(session.tools)
                except Exception:
                    pass
        if changed:
            try:
                from mcp_host import bridge
                bridge.get_bridge().publish()
            except Exception:
                pass
            self._changed()
        return changed

    # ── connecting ────────────────────────────────────────────────────────────

    def _connect(self, runtime: ServerRuntime) -> bool:
        spec = runtime.spec
        if not spec.enabled:
            runtime.state = "disabled"
            runtime.detail = "disabled by the user"
            runtime.note("disabled — not started")
            return False
        if spec.switch and not self._switch_on(spec.switch):
            runtime.state = "disabled"
            runtime.detail = f"switched off in Settings ({spec.switch})"
            runtime.note(f"switched off in Settings ({spec.switch})")
            return False
        self._set_state(runtime, "connecting", "checking the configuration")
        check = catalog.probe(spec)
        if not check["ok"]:
            self._fail(runtime, check["detail"])
            return False
        self._set_state(runtime, "initializing", "starting the server")
        session = McpSession(spec)
        runtime.session = session
        ok = session.start()
        if not ok:
            runtime.session = None
            self._fail(runtime, session.error or "the server did not start")
            session.close("start failed")
            return False
        self._set_state(runtime, "discovering", "reading the capabilities")
        runtime.connected = True
        runtime.connected_at = time.time()
        runtime.reconnect_attempts = 0
        runtime.tools_ready = bool(session.tools)
        runtime.authenticated = not _looks_like_auth_failure(session.discovery_error)
        runtime.last_error = session.discovery_error or ""
        detail = _describe_connection(session)
        runtime.note(f"connected ({detail})")
        if runtime.tools_ready:
            runtime.note(f"discovered {len(session.tools)} tool(s)"
                         + (f", {len(session.resources)} resource(s)"
                            if session.resources else "")
                         + (f", {len(session.prompts)} prompt(s)"
                            if session.prompts else ""))
        else:
            runtime.note("connected, but the server offers no tools")
        self._set_state(runtime, "connected", detail)
        self._publish()
        return True

    def _publish(self) -> int:
        with self._publish_lock:
            try:
                from mcp_host import bridge
                return bridge.get_bridge().publish()
            except Exception:
                return 0

    def _set_state(self, runtime: ServerRuntime, state: str, detail: str = "") -> None:
        runtime.state = state
        runtime.detail = str(detail or "")
        self._changed()

    def _fail(self, runtime: ServerRuntime, message: str) -> None:
        runtime.connected = False
        runtime.tools_ready = False
        runtime.last_error = str(message)
        runtime.authenticated = not _looks_like_auth_failure(message)
        runtime.detail = str(message)
        runtime.note(f"error: {message}")
        self._set_state(runtime, "error", str(message))
        self._schedule_reconnect(runtime)

    def _mark_dead(self, runtime: ServerRuntime, message: str) -> None:
        runtime.connected = False
        runtime.tools_ready = False
        runtime.last_error = str(message)
        self._set_state(runtime, "error", str(message))
        self._schedule_reconnect(runtime)

    def _schedule_reconnect(self, runtime: ServerRuntime) -> None:
        policy = runtime.spec.reconnect or {}
        if not bool(policy.get("enabled", True)) or self._stop.is_set():
            return
        attempts = int(policy.get("max_attempts", 5) or 5)
        if runtime.reconnect_attempts >= attempts:
            runtime.note("reconnect attempts exhausted — leaving it stopped")
            runtime.next_retry_at = 0.0
            return
        runtime.next_retry_at = time.monotonic() + _backoff_delay(
            runtime.spec, runtime.reconnect_attempts)
        runtime.note(f"reconnect scheduled (attempt "
                     f"{runtime.reconnect_attempts + 1}/{attempts})")

    def _reconnect(self, runtime: ServerRuntime) -> None:
        runtime.reconnect_attempts += 1
        runtime.next_retry_at = 0.0
        self._set_state(runtime, "reconnecting",
                        f"reconnecting (attempt {runtime.reconnect_attempts})")
        self._say(f"{runtime.name} — reconnecting…")
        old = runtime.session
        runtime.session = None
        if old is not None:
            old.close("reconnecting")
        self._connect(runtime)

    # ── public operations ─────────────────────────────────────────────────────

    def start_server(self, name: str, background: bool = True) -> dict:
        runtime = self._runtime(name)
        if runtime is None:
            return {"ok": False, "detail": f"there is no MCP server called '{name}'"}
        runtime.reconnect_attempts = 0
        if background:
            threading.Thread(target=self._connect, args=(runtime,),
                             name=f"mcp-connect-{name}", daemon=True).start()
            return {"ok": True, "detail": f"{name} is connecting"}
        ok = self._connect(runtime)
        return {"ok": ok, "detail": runtime.detail or runtime.last_error}

    def stop_server(self, name: str, reason: str = "stopped by the user") -> dict:
        runtime = self._runtime(name)
        if runtime is None:
            return {"ok": False, "detail": f"there is no MCP server called '{name}'"}
        self._set_state(runtime, "stopping", reason)
        session, runtime.session = runtime.session, None
        if session is not None:
            session.close(reason)
        runtime.connected = False
        runtime.tools_ready = False
        runtime.next_retry_at = 0.0
        runtime.note(reason)
        self._set_state(runtime, "stopped", reason)
        self._publish()
        return {"ok": True, "detail": f"{name} stopped"}

    def restart_server(self, name: str) -> dict:
        self.stop_server(name, "restarting")
        runtime = self._runtime(name)
        if runtime is not None:
            runtime.reconnect_attempts = 0
        return self.start_server(name)

    def refresh_server(self, name: str) -> dict:
        runtime = self._runtime(name)
        if runtime is None:
            return {"ok": False, "detail": f"there is no MCP server called '{name}'"}
        if runtime.session is None or not runtime.session.running:
            return {"ok": False,
                    "detail": f"{name} is not connected, so there is nothing to "
                              f"refresh"}
        try:
            result = runtime.session.discover(force=True)
        except Exception as exc:
            return {"ok": False, "detail": f"{name} could not be refreshed: {exc}"}
        runtime.tools_ready = bool(runtime.session.tools)
        runtime.note(f"capabilities refreshed: {result['tools']} tool(s), "
                     f"{result['resources']} resource(s), {result['prompts']} prompt(s)")
        self._publish()
        self._changed()
        return {"ok": True, "detail": f"{name} refreshed", "counts": result}

    def refresh_all(self) -> dict:
        out = {}
        for name in list(self._runtimes):
            out[name] = self.refresh_server(name)
        return out

    def call_tool(self, server: str, tool: str, arguments: dict | None = None,
                  on_progress: Callable[[dict], None] | None = None,
                  timeout: float | None = None):
        """Call one tool on one server. Returns a ``ToolCall`` (never raises)."""
        runtime = self._runtime(server)
        if runtime is None or runtime.session is None:
            from mcp_host.client import ToolCall
            return ToolCall(False, error=f"the {server} MCP server is not running")
        self._say(f"{runtime.name} — running {tool}…")
        result = runtime.session.call_tool(tool, arguments, timeout=timeout,
                                           on_progress=on_progress)
        for line in runtime.session.progress_lines():
            runtime.note(f"progress: {line}")
        state = "answered" if result.ok else "failed"
        runtime.note(f"{tool} {state} in {result.elapsed_s}s")
        return result

    def cancel(self, server: str = "") -> dict:
        """Cancel in-flight work on one server (or all of them)."""
        targets = [self._runtime(server)] if server else list(self._runtimes.values())
        cancelled = 0
        for runtime in targets:
            if runtime is None or runtime.session is None:
                continue
            runtime.session.cancel_all()
            cancelled += 1
            runtime.note("in-flight request cancelled")
        return {"ok": cancelled > 0, "detail": f"cancelled {cancelled} request(s)"}

    # ── enable / disable ──────────────────────────────────────────────────────

    def set_enabled(self, name: str, enabled: bool) -> dict:
        runtime = self._runtime(name)
        if runtime is None:
            return {"ok": False, "detail": f"there is no MCP server called '{name}'"}
        runtime.spec.enabled = bool(enabled)
        self._persist()
        if enabled:
            runtime.reconnect_attempts = 0
            return self.start_server(name)
        return self.stop_server(name, "disabled by the user")

    # ── configuration CRUD ────────────────────────────────────────────────────

    def add_server(self, spec: catalog.ServerSpec) -> dict:
        with self._lock:
            if spec.name in self._runtimes:
                return {"ok": False,
                        "detail": f"an MCP server called '{spec.name}' already "
                                  f"exists"}
            self._specs.append(spec)
            self._runtimes[spec.name] = ServerRuntime(spec=spec)
        self._persist()
        if self._started:
            self.start_server(spec.name)
        return {"ok": True, "detail": f"{spec.name} was added"}

    def update_server(self, spec: catalog.ServerSpec, *,
                      old_name: str = "") -> dict:
        """Replace a server's configuration, reconnecting when it changed."""
        previous = self._runtime(old_name or spec.name)
        if previous is None:
            return self.add_server(spec)
        changed_identity = bool(old_name and old_name != spec.name)
        if changed_identity and spec.name in self._runtimes:
            return {"ok": False,
                    "detail": f"an MCP server called '{spec.name}' already exists"}
        with self._lock:
            if changed_identity:
                self._runtimes.pop(old_name, None)
                self._specs = [s for s in self._specs if s.name != old_name]
                secret_store.rename_server(old_name, spec.name)
            self._runtimes[spec.name] = ServerRuntime(
                spec=spec, log=previous.log)
            self._specs = [spec if s.name == (old_name or spec.name) else s
                           for s in self._specs]
            if not any(s.name == spec.name for s in self._specs):
                self._specs.append(spec)
        self._persist()
        # A running session was built from the old configuration, so it is
        # closed and rebuilt rather than kept with a stale URL or command.
        if previous.session is not None:
            previous.session.close("configuration changed")
        return {"ok": True,
                "detail": "configuration changed. Reconnect required."}

    def remove_server(self, name: str) -> dict:
        runtime = self._runtime(name)
        if runtime is None:
            return {"ok": False, "detail": f"there is no MCP server called '{name}'"}
        self.stop_server(name, "removed")
        with self._lock:
            self._runtimes.pop(name, None)
            self._specs = [s for s in self._specs if s.name != name]
        secret_store.clear_server_secrets(name)
        self._persist()
        self._publish()
        self._changed()
        return {"ok": True, "detail": f"{name} was removed"}

    def save_server_list(self, specs: list[catalog.ServerSpec]) -> dict:
        """Replace the whole list (used by Import), preserving sessions."""
        ok, message = catalog.save_servers(specs)
        if not ok:
            return {"ok": False, "detail": message}
        with self._lock:
            self._specs = list(specs)
            for spec in specs:
                if spec.name not in self._runtimes:
                    self._runtimes[spec.name] = ServerRuntime(spec=spec)
                else:
                    self._runtimes[spec.name].spec = spec
            for name in list(self._runtimes):
                if name not in {s.name for s in specs}:
                    self.stop_server(name, "removed by import")
                    self._runtimes.pop(name, None)
        self._changed()
        return {"ok": True, "detail": message, "count": len(specs)}

    def _persist(self) -> None:
        try:
            catalog.save_servers(self._specs)
        except Exception:
            pass

    # ── status ────────────────────────────────────────────────────────────────

    def _runtime(self, name: str) -> ServerRuntime | None:
        return self._runtimes.get(str(name or ""))

    def runtimes(self) -> list[ServerRuntime]:
        return list(self._runtimes.values())

    def spec_named(self, name: str) -> catalog.ServerSpec | None:
        runtime = self._runtime(name)
        return runtime.spec if runtime else None

    def status(self) -> dict:
        rows = []
        tools = 0
        for runtime in self._runtimes.values():
            rows.append(self.server_row(runtime))
            tools += len(runtime.session.tools) if runtime.session else 0
        return {"servers": rows, "tools": tools}

    def server_row(self, runtime: ServerRuntime) -> dict:
        session = runtime.session
        return {
            "name": runtime.name,
            "namespace": runtime.spec.namespace,
            "description": runtime.spec.description,
            "provider": runtime.spec.provider,
            "transport": "http" if runtime.spec.is_http else "stdio",
            "url": runtime.spec.url,
            "enabled": bool(runtime.spec.enabled),
            "state": runtime.state,
            "state_label": _state_label(runtime.state),
            "detail": runtime.detail,
            "connected": bool(runtime.connected and runtime.state == "connected"),
            "authenticated": bool(runtime.authenticated),
            "tools_ready": bool(runtime.tools_ready),
            "protocol": session.protocol_version if session else "",
            "era": session.era if session else "",
            "tools": [t.name for t in session.tools] if session else [],
            "tool_details": [
                {
                    "name": t.name,
                    "description": getattr(t, "description", ""),
                    "input_schema": getattr(t, "input_schema", {}) or {},
                }
                for t in session.tools
            ] if session else [],
            "tool_count": len(session.tools) if session else 0,
            "resources": [_resource_label(item) for item in session.resources]
                         if session else [],
            "prompts": [_prompt_label(item) for item in session.prompts]
                       if session else [],
            "resource_count": len(session.resources) if session else 0,
            "prompt_count": len(session.prompts) if session else 0,
            "error": runtime.last_error,
            "log": list(runtime.log)[-40:],
            "permissions": dict(runtime.spec.permissions or {}),
            "reconnect": dict(runtime.spec.reconnect or {}),
            "spec": runtime.spec.as_dict(),
        }

    def log_for(self, name: str) -> list[str]:
        runtime = self._runtime(name)
        return list(runtime.log) if runtime else []

    def tools(self) -> list[dict]:
        """Every discovered tool across every connected server."""
        out: list[dict] = []
        for runtime in self._runtimes.values():
            session = runtime.session
            if session is None or not session.running:
                continue
            for tool in session.tools:
                if not runtime.spec.tool_enabled(tool.name):
                    continue
                out.append({
                    "server": runtime.name,
                    "tool": tool.name,
                    "description": tool.description,
                    "schema": tool.input_schema,
                    "namespace": runtime.spec.namespace,
                    "safety": runtime.spec.safety_of(
                        tool.name, classify_tool(tool)),
                })
        return out

    def resources(self) -> list[dict]:
        out: list[dict] = []
        for runtime in self._runtimes.values():
            session = runtime.session
            if session is None:
                continue
            for resource in session.resources:
                out.append({"server": runtime.name, **resource})
        return out

    def prompts(self) -> list[dict]:
        out: list[dict] = []
        for runtime in self._runtimes.values():
            session = runtime.session
            if session is None:
                continue
            for prompt in session.prompts:
                out.append({"server": runtime.name, **prompt})
        return out

    def _switch_on(self, name: str) -> bool:
        try:
            from config.settings import get_feature
            return bool(get_feature(name, True))
        except Exception:
            return True

    # ── shutdown ──────────────────────────────────────────────────────────────

    def shutdown(self, reason: str = "shutdown") -> None:
        """Close every session and terminate every managed process."""
        self._stop.set()
        for runtime in list(self._runtimes.values()):
            session, runtime.session = runtime.session, None
            if session is None:
                continue
            try:
                session.close(reason)
            except Exception:
                pass
            runtime.connected = False
            runtime.tools_ready = False
            runtime.state = "stopped"
        self._started = False


# ── helpers ───────────────────────────────────────────────────────────────────

def _state_label(state: str) -> str:
    return {
        "connected": "Connected",
        "connecting": "Connecting…",
        "initializing": "Initializing…",
        "discovering": "Discovering tools…",
        "reconnecting": "Reconnecting…",
        "stopping": "Stopping…",
        "stopped": "Stopped",
        "disabled": "Disabled",
        "error": "Error",
        "disconnected": "Disconnected",
    }.get(str(state or ""), str(state or "").title())


def _resource_label(item: Any) -> str:
    """One resource, as a single readable line for the Manage view."""
    if not isinstance(item, dict):
        return str(item)[:200]
    name = str(item.get("name") or item.get("title") or "").strip()
    uri = str(item.get("uri") or "").strip()
    description = str(item.get("description") or "").strip()
    text = name or uri or "resource"
    if uri and uri != text:
        text += f"  ({uri})"
    if description:
        text += f" — {description[:140]}"
    return text[:220]


def _prompt_label(item: Any) -> str:
    """One prompt, as a single readable line for the Manage view."""
    if not isinstance(item, dict):
        return str(item)[:200]
    name = str(item.get("name") or "").strip() or "prompt"
    description = str(item.get("description") or "").strip()
    arguments = item.get("arguments")
    if isinstance(arguments, list) and arguments:
        fields = ", ".join(str(arg.get("name")) for arg in arguments
                           if isinstance(arg, dict) and arg.get("name"))
        if fields:
            description = f"{description} ({fields})".strip()
    return (f"{name} — {description[:160]}" if description else name)[:220]


def _describe_connection(session: McpSession) -> str:
    info = session.server_info
    server = str(info.get("name") or session.name)
    version = str(info.get("version") or "").strip()
    label = f"{server} {version}".strip()
    era = session.era
    protocol = session.protocol_version or "?"
    return f"{label} · {protocol} ({era})"


def _backoff_delay(spec: catalog.ServerSpec, attempt: int) -> float:
    """Capped exponential backoff, based on the server's own retry delay."""
    base = float((spec.reconnect or {}).get("delay_s", 5.0) or 5.0)
    return min(120.0, base * (2 ** max(0, attempt)))


#: Words that mark a tool as something more than a read. Classification is a
#: heuristic, and the user can always override it per tool — but a delete or a
#: send must never be treated as a harmless read by default.
_DESTRUCTIVE_MARKERS = (
    "delete", "remove", "destroy", "drop", "purge", "truncate", "revoke",
    "uninstall", "terminate", "kill", "reset", "overwrite", "wipe",
)
_WRITE_MARKERS = (
    "create", "update", "edit", "write", "add", "insert", "post", "put",
    "patch", "send", "publish", "upload", "set", "move", "rename", "copy",
    "merge", "close", "reopen", "comment", "reply", "assign", "label",
    "commit", "push", "apply", "run", "execute", "start", "stop", "play",
    "pause", "skip", "schedule", "invite", "share", "approve", "accept",
)


def classify_tool(tool: Any) -> str:
    """Read / write / destructive, from the tool's own name and description."""
    text = f"{getattr(tool, 'name', '')} {getattr(tool, 'description', '')}".lower()
    if any(marker in text for marker in _DESTRUCTIVE_MARKERS):
        return "destructive"
    if any(marker in text for marker in _WRITE_MARKERS):
        return "write"
    return "read"


# ── connection testing (used by the Add/Edit screen) ──────────────────────────

def test_connection(spec: catalog.ServerSpec,
                    timeout: float = 25.0) -> dict:
    """Really connect to a server, discover its capabilities, then close.

    This is the honest version of "Test Connection": it spawns the process or
    opens the endpoint, negotiates, and lists what the server actually offers.
    A syntactically valid URL is not a working server, and this is where that
    distinction is enforced.
    """
    check = catalog.probe(spec)
    if not check["ok"]:
        return {"ok": False, "stage": "configuration",
                "detail": check["detail"]}
    session = McpSession(spec)
    try:
        ok = session.start()
    except Exception as exc:
        return {"ok": False, "stage": "connect",
                "detail": f"the connection attempt failed: {exc}"}
    try:
        if not ok:
            return {"ok": False, "stage": "connect",
                    "detail": secret_store.redact(
                        session.error or "the server did not start")}
        detail = _describe_connection(session)
        return {
            "ok": True, "stage": "connected",
            "detail": f"Connected to {detail}",
            "tools": len(session.tools),
            "resources": len(session.resources),
            "prompts": len(session.prompts),
            "tool_names": [t.name for t in session.tools][:40],
            "protocol": session.protocol_version,
            "era": session.era,
        }
    finally:
        session.close("connection test finished")


# ── the process-wide manager ──────────────────────────────────────────────────

_MANAGER: McpManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> McpManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = McpManager()
        return _MANAGER


def reset_manager(manager: McpManager | None = None) -> None:
    """Test/teardown hook: replace (or drop) the process-wide manager."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = manager
