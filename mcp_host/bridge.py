"""The bridge that makes MCP tools ordinary J.A.R.V.I.S. tools.

Every tool a server offers is published as ``<prefix>__<toolName>`` — a prefix
because two servers may legitimately offer a tool called ``search``, and the
model must be able to tell them apart. Published tools are hot-registered
through ``app.plugin_loader`` so they behave exactly like a forged skill:

* they appear in ``get_tool_declarations()`` at the next connect,
* ``PluginRegistry.run()`` dispatches them,
* the Plugin Manager lists them, and a disabled plugin is refused by name.

Three rules live here, and nowhere else:

**One owner at a time.** The bridge can be *manager-backed* (the application
path: :class:`mcp_host.manager.McpManager` owns every session and the bridge only
publishes what the manager found) or *standalone* (constructed with its own
specs, which is what the tests and any embedding use). It never runs two
connections to the same server.

**Only what is allowed is published.** A tool switched off is not declared at
all, and a destructive tool stays hidden until that specific tool is enabled —
so the model cannot be handed an action the user never sanctioned.

**Risky tools stop for a human.** A tool classified *write* (or *destructive*)
is routed through ``security/confirm.py`` when the server's policy requires it,
which puts a real confirmation on the HUD and returns an honest "waiting for you
to confirm" sentence instead of performing the action. Reading is free; acting
is confirmed.

A server that failed to start stays failed, and every call returns the sentence
explaining why — never a traceback in the middle of a voice turn.
"""

from __future__ import annotations

import atexit
import threading
import time
from typing import Any

from mcp_host import catalog, protocol
from mcp_host.manager import McpManager, classify_tool
from mcp_host.session import McpSession

__all__ = ["Bridge", "get_bridge", "shutdown", "startup", "status"]

_MAX_DESCRIPTION = 900


class Bridge:
    """Publishes MCP tools to the assistant, over a manager or on its own."""

    def __init__(self, specs: list[catalog.ServerSpec] | None = None,
                 manager: McpManager | None = None) -> None:
        self._manager = manager
        self._specs = list(specs) if specs is not None else (
            [] if manager is not None else catalog.load_servers())
        self._sessions: dict[str, McpSession] = {}
        self._tools: dict[str, tuple[str, str]] = {}   # published -> (server, tool)
        self._lock = threading.Lock()
        self._started = False
        self.log: list[str] = []

    def bind_manager(self, manager: McpManager) -> None:
        """Attach the application's manager (the app path, not the test path)."""
        self._manager = manager

    # ── startup ───────────────────────────────────────────────────────────────

    def start(self) -> dict:
        """Start every enabled server, then publish what it offers."""
        if self._manager is not None:
            return self._manager.start(background=False)
        with self._lock:
            if self._started:
                return self.status()
            self._started = True
        for spec in self._specs:
            if not spec.enabled:
                self._say(f"{spec.name}: disabled in the MCP server list")
                continue
            if spec.switch and not self._switch_on(spec.switch):
                self._say(f"{spec.name}: switched off in Settings ({spec.switch})")
                continue
            check = catalog.probe(spec)
            if not check["ok"]:
                self._say(f"{spec.name}: {check['detail']}")
                continue
            session = McpSession(spec)
            if not session.start():
                self._say(f"{spec.name}: {session.error or 'the server did not start'}")
                session.close("start failed")
                continue
            self._sessions[spec.name] = session
            for tool in session.tools:
                published = self._published_name(spec, tool.name)
                with self._lock:
                    self._tools[published] = (spec.name, tool.name)
            self._say(f"{spec.name}: {len(session.tools)} tool(s) available")
        self.publish()
        return self.status()

    def publish(self) -> int:
        """Register every allowed tool with the live plugin registry.

        Returns how many were installed. Hot-registration is what makes the
        tools callable in the session that is already running.
        """
        try:
            from app import plugin_loader
        except Exception:
            return 0
        registry = plugin_loader.get_runtime_registry()
        if registry is None:
            return 0
        installed = 0
        for spec, session in self._live_sessions():
            for tool in session.tools:
                safety = spec.safety_of(tool.name, classify_tool(tool))
                if not spec.tool_exposed(tool.name, safety):
                    continue
                published = self._published_name(spec, tool.name)
                with self._lock:
                    self._tools[published] = (spec.name, tool.name)
                record = plugin_loader.PluginRecord(
                    name=published,
                    description=self._describe(spec, tool, safety),
                    parameters=protocol.gemini_schema(tool.input_schema),
                    run=self._make_runner(session, spec, tool.name, published,
                                          safety),
                    file=f"mcp:{spec.name}",
                    valid=True,
                    error="",
                )
                ok, message = registry.register(record)
                if ok or "already" in message:
                    installed += 1
                else:
                    self._say(f"{published}: {message}")
        return installed

    # ── calling ───────────────────────────────────────────────────────────────

    def call(self, published: str, arguments: dict | None = None) -> str:
        """Call a published tool by name. Always answers with text.

        The direct path — used by diagnostics and by anything holding a
        published name. The agent's own path runs through the plugin registry,
        where permission gating and confirmation are applied.
        """
        target = self._tools.get(published)
        if target is None:
            return (f"'{published}' is not a tool from any MCP server that is "
                    f"running.")
        server_name, tool = target
        session = self._session_for(server_name)
        if session is None:
            return (f"The {server_name} MCP server is not running, so I could not "
                    f"call {tool}.")
        result = session.call_tool(tool, dict(arguments or {}))
        head = f"{server_name} · {tool}"
        if result.ok:
            return f"{result.text or 'Done.'}\n[{head} {result.elapsed_s}s]"
        return (f"{result.text or result.error or 'The server returned no answer.'}"
                f"\n[NOT_DONE {head} {result.elapsed_s}s]")

    def tools(self) -> list[dict]:
        """Everything published, for diagnostics and the Settings window."""
        out: list[dict] = []
        for spec, session in self._live_sessions():
            for tool in session.tools:
                safety = spec.safety_of(tool.name, classify_tool(tool))
                out.append({
                    "name": self._published_name(spec, tool.name),
                    "server": spec.name,
                    "tool": tool.name,
                    "description": tool.description,
                    "prefix": spec.namespace,
                    "safety": safety,
                    "exposed": spec.tool_exposed(tool.name, safety),
                })
        return out

    def status(self) -> dict:
        if self._manager is not None:
            return self._manager.status()
        servers_out = []
        for spec in self._specs:
            session = self._sessions.get(spec.name)
            if session is not None:
                row = session.status()
                row["enabled"] = True
                row["prefix"] = spec.namespace
                row["tools"] = [tool.name for tool in session.tools]
            else:
                row = {"name": spec.name, "running": False, "tools": [],
                       "error": ("" if spec.enabled else "disabled in the MCP "
                                                        "server list"),
                       "enabled": bool(spec.enabled), "prefix": spec.namespace}
            row["description"] = spec.description
            servers_out.append(row)
        return {"servers": servers_out,
                "tools": len(self._tools),
                "log": list(self.log[-20:])}

    def shutdown(self, reason: str = "shutdown") -> None:
        if self._manager is not None:
            self._manager.shutdown(reason)
            return
        for session in list(self._sessions.values()):
            session.close(reason)
        self._sessions.clear()
        self._tools.clear()
        self._started = False

    # ── internals ─────────────────────────────────────────────────────────────

    def _live_sessions(self) -> list[tuple[catalog.ServerSpec, McpSession]]:
        """Every (spec, session) currently healthy, manager-backed or not."""
        out: list[tuple[catalog.ServerSpec, McpSession]] = []
        if self._manager is not None:
            for runtime in self._manager.runtimes():
                session = runtime.session
                if session is not None and session.running:
                    out.append((runtime.spec, session))
            return out
        for spec in self._specs:
            session = self._sessions.get(spec.name)
            if session is not None and session.running:
                out.append((spec, session))
        return out

    def _session_for(self, server_name: str) -> McpSession | None:
        if self._manager is not None:
            runtime = self._manager._runtime(server_name)
            return runtime.session if runtime else None
        return self._sessions.get(server_name)

    def _say(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
        try:
            print(f"[MCP] {message}")
        except Exception:
            pass

    def _make_runner(self, session: McpSession, spec: catalog.ServerSpec,
                     tool: str, published: str, safety: str):
        def run(parameters=None, player=None, session_memory=None) -> str:
            args = dict(parameters or {})
            if spec.needs_approval(safety):
                return _confirm_then_run(spec, published, safety, session, tool, args)
            return _run_now(session, spec, tool, args)
        return run

    def _describe(self, spec: catalog.ServerSpec, tool, safety: str) -> str:
        text = str(tool.description or "").strip()
        if not text:
            text = f"{tool.name} on the {spec.name} MCP server."
        label = {"write": "changes data", "destructive": "can destroy data"}
        head = f"[{spec.namespace}] {text}"
        if safety in label:
            head += f" ({label[safety]})"
        if spec.description:
            head += f" (Server: {spec.description})"
        return head[:_MAX_DESCRIPTION]

    def _published_name(self, spec: catalog.ServerSpec, tool: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in tool)
        if not safe or safe[0].isdigit():
            safe = f"t_{safe}"
        return f"{spec.namespace}__{safe}"[:64]

    def _switch_on(self, name: str) -> bool:
        try:
            from config.settings import get_feature
            return bool(get_feature(name, True))
        except Exception:
            return True


def _run_now(session: McpSession, spec: catalog.ServerSpec, tool: str,
             args: dict) -> str:
    """Run one tool and render its outcome as the sentence the model reads."""
    result = session.call_tool(tool, args)
    if result.ok:
        return result.text or "Done."
    return (f"The {spec.name} server refused that: "
            f"{result.text or result.error or 'no reason given'}")


def _confirm_then_run(spec: catalog.ServerSpec, published: str, safety: str,
                      session: McpSession, tool: str, args: dict) -> str:
    """Park a risky MCP action behind the HUD's own confirmation gate.

    The confirmation returns an instruction, not the tool's result — the user
    answers on screen, and the action runs then. That is deliberate: the model
    must never be able to perform a write by simply asking for it twice.
    """
    label = "destructive" if safety == "destructive" else "write"
    title = f"Run '{tool}' on {spec.name}?"
    detail = (f"This MCP tool is classified {label} and the {spec.name} server's "
              f"policy requires your approval before it runs.")

    def _go() -> str:
        return _run_now(session, spec, tool, args)

    try:
        from security import confirm
        return confirm.request(key=f"mcp:{published}", title=title,
                               detail=detail, run=_go)
    except Exception as exc:
        return (f"I need your confirmation before running '{tool}', but I could "
                f"not ask for it ({exc}), so I have not run it.")


# ── the process-wide bridge ───────────────────────────────────────────────────

_BRIDGE: Bridge | None = None
_BRIDGE_LOCK = threading.Lock()
_ATEXIT_INSTALLED = False


def get_bridge() -> Bridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            from mcp_host.manager import get_manager
            _BRIDGE = Bridge(manager=get_manager())
        return _BRIDGE


def startup(background: bool = True) -> dict:
    """Start the servers (in the background by default) and publish their tools.

    Starting a server is a process spawn or an HTTP connect plus discovery — a
    second or two — which must never stand between the user and a working
    window, so the default runs it on a daemon thread and the log reports the
    outcome when it is known.
    """
    from mcp_host.manager import get_manager

    manager = get_manager()
    bridge = get_bridge()
    bridge.bind_manager(manager)
    global _ATEXIT_INSTALLED
    if not _ATEXIT_INSTALLED:
        try:
            atexit.register(shutdown)
        except Exception:
            pass
        _ATEXIT_INSTALLED = True
    return manager.start(background=background)


def status() -> dict:
    return get_bridge().status()


def shutdown(reason: str = "shutdown") -> None:
    bridge = _BRIDGE
    if bridge is not None:
        bridge.shutdown(reason)
