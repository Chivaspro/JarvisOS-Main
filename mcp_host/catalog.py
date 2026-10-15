"""Which MCP servers exist, and how they are launched or reached.

One server, one :class:`ServerSpec`. The spec is deliberately generic: a local
process (``transport="stdio"``, a ``command``) and a remote endpoint
(``transport="http"``, a ``url`` + header names) are the same kind of thing to
everything above this module. That is what keeps the system universal — adding a
server is configuration, never a new provider subsystem.

The built-in list lives in code (so a fresh install ships ready-to-run native
servers without anyone editing a file) and a JSON file overrides or extends it
without a code change:

    data/mcp/servers.json
    {
      "servers": [
        {"name": "notes", "transport": "stdio",
         "command": ["node", "C:/mcp/notes/index.js"], "prefix": "notes",
         "enabled": true},
        {"name": "composio", "transport": "http",
         "url": "https://connect.composio.dev/mcp",
         "auth": "custom_header", "header_names": ["x-consumer-api-key"]}
      ]
    }

**Credential values never appear here.** Header *names* and environment variable
*names* are stored; the values live in the guarded credential store
(:mod:`mcp_host.secrets`). That split is why a server list can be exported or
shown without a masking pass being the only thing standing between a key and the
screen.

A server whose command cannot be found (or whose URL is malformed) is reported by
``probe()``, never silently dropped: the Settings window has to be able to say
*why* a link is unavailable.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_PERMISSIONS", "DEFAULT_RECONNECT", "ServerSpec", "builtin_servers",
    "config_path", "load_servers", "mcp_status", "presets", "probe", "save_servers",
    "switches_in_use", "TRANSPORTS",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: The transport bindings this host can drive.
TRANSPORTS = ("stdio", "http")

#: Default per-server permission policy. Read is free, a write needs approval,
#: and a destructive action is not exposed at all until the user explicitly
#: enables that specific tool — the safe default is the one a user never has to
#: think about. ``enabled_tools`` is deliberately an *allowlist* for the tools
#: that are dangerous by default; ``disabled_tools`` is the ordinary off switch
#: for everything else, so the two never have to be reconciled.
DEFAULT_PERMISSIONS: dict = {
    "allow_discovery": True,
    "allow_read": True,
    "require_approval_writes": True,
    "confirm_destructive": True,
    "disabled_tools": [],
    "enabled_tools": [],
    "tool_safety": {},
}

#: Default reconnect policy: bounded, with backoff, never an aggressive loop.
DEFAULT_RECONNECT: dict = {
    "enabled": True,
    "delay_s": 5.0,
    "max_attempts": 5,
}


def config_path() -> Path:
    """Where user overrides live (created on demand, never required)."""
    try:
        from config.settings import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = PROJECT_ROOT / "data"
    target = base / "mcp"
    target.mkdir(parents=True, exist_ok=True)
    return target / "servers.json"


def _clean(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_-" else "_"
                   for ch in str(value or "").strip())
    text = text.strip("_")
    return text.replace("-", "_")


def _merged_permissions(raw: Any) -> dict:
    out = dict(DEFAULT_PERMISSIONS)
    if isinstance(raw, dict):
        for key in ("allow_discovery", "allow_read", "require_approval_writes",
                    "confirm_destructive"):
            if key in raw:
                out[key] = bool(raw[key])
        for key in ("disabled_tools", "enabled_tools"):
            value = raw.get(key)
            if isinstance(value, (list, tuple)):
                out[key] = [str(name) for name in value if str(name)]
        safety = raw.get("tool_safety")
        if isinstance(safety, dict):
            out["tool_safety"] = {str(k): str(v) for k, v in safety.items()}
    return out


def _merged_reconnect(raw: Any) -> dict:
    out = dict(DEFAULT_RECONNECT)
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        try:
            if raw.get("delay_s") is not None:
                out["delay_s"] = max(0.5, float(raw["delay_s"]))
            if raw.get("max_attempts") is not None:
                out["max_attempts"] = max(1, int(raw["max_attempts"]))
        except (TypeError, ValueError):
            pass
    return out


@dataclass
class ServerSpec:
    """One MCP server: what to run or reach, and how its tools appear."""

    name: str
    command: list[str] = field(default_factory=list)
    prefix: str = ""
    enabled: bool = True
    cwd: str = ""
    env: dict = field(default_factory=dict)
    timeout: float = 60.0
    description: str = ""
    #: Settings switch that turns this server's tools off ("" = always on).
    switch: str = ""

    # ── generic transport surface ────────────────────────────────────────────
    transport: str = "stdio"
    url: str = ""
    #: Header *names* (values are empty here and supplied by the secret store).
    headers: dict = field(default_factory=dict)
    #: The header *names* the user configured, in display order.
    header_names: list[str] = field(default_factory=list)
    #: "none" | "bearer" | "custom_header"
    auth: str = "none"
    permissions: dict = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    reconnect: dict = field(default_factory=lambda: dict(DEFAULT_RECONNECT))
    allow_insecure_http: bool = False
    protocol_override: str = ""
    start_timeout: float = 20.0
    #: Free-form provider hint used to pick an icon ("composio", "github", …).
    provider: str = ""

    @property
    def namespace(self) -> str:
        return _clean(self.prefix or self.name) or _clean(self.name) or "mcp"

    @property
    def is_http(self) -> bool:
        return str(self.transport or "stdio").lower() in (
            "http", "streamable_http", "streamable-http", "sse", "remote")

    def header_name_list(self) -> list[str]:
        """Every configured header name, deduplicated and ordered."""
        names = [str(name) for name in (self.header_names or []) if str(name)]
        names += [str(name) for name in (self.headers or {}) if str(name)]
        seen: list[str] = []
        for name in names:
            if name not in seen:
                seen.append(name)
        return seen

    def tool_enabled(self, tool: str) -> bool:
        """Is this tool switched on? (Safety class is applied separately.)"""
        disabled = {str(name) for name in
                    (self.permissions.get("disabled_tools") or [])}
        return str(tool) not in disabled

    def tool_exposed(self, tool: str, safety: str = "read") -> bool:
        """May the agent see and call this tool at all?

        A read or write tool is exposed unless it was switched off. A tool
        classified destructive stays hidden until the user enables *that tool*
        by name, so a server that ships a ``delete_everything`` action cannot
        hand it to the model merely because the server was added.
        """
        if not self.tool_enabled(tool):
            return False
        if str(safety or "").lower() != "destructive":
            return True
        allowed = {str(name) for name in
                   (self.permissions.get("enabled_tools") or [])}
        return str(tool) in allowed

    def needs_approval(self, safety: str) -> bool:
        """Does this safety class require an explicit human confirmation?"""
        safety = str(safety or "read").lower()
        if safety == "destructive":
            return bool(self.permissions.get("confirm_destructive", True))
        if safety == "write":
            return bool(self.permissions.get("require_approval_writes", True))
        return False

    def safety_of(self, tool: str, default: str = "read") -> str:
        override = str((self.permissions.get("tool_safety") or {})
                       .get(str(tool), "")).lower()
        return override if override in ("read", "write", "destructive") else default

    def as_dict(self) -> dict:
        return {
            "name": self.name, "command": list(self.command),
            "prefix": self.namespace, "enabled": bool(self.enabled),
            "cwd": self.cwd, "env": dict(self.env),
            "timeout": float(self.timeout), "description": self.description,
            "switch": self.switch,
            "transport": str(self.transport or "stdio"),
            "url": str(self.url or ""),
            "headers": {str(name): "" for name in self.header_name_list()},
            "header_names": self.header_name_list(),
            "auth": str(self.auth or "none"),
            "permissions": dict(self.permissions or DEFAULT_PERMISSIONS),
            "reconnect": dict(self.reconnect or DEFAULT_RECONNECT),
            "allow_insecure_http": bool(self.allow_insecure_http),
            "protocol_override": str(self.protocol_override or ""),
            "start_timeout": float(self.start_timeout or 20.0),
            "provider": str(self.provider or ""),
        }


def builtin_servers() -> list[ServerSpec]:
    """The servers shipped with the project.

    Every entry runs as a pure-Python MCP server under ``mcp_host/servers`` so
    no Node build step is required, even though the host itself would happily
    drive a Node server if one is configured.
    """
    python = sys.executable or "python"
    return [
        ServerSpec(
            name="system_tools",
            command=[python, "-m", "mcp_host.servers.system_tools.server"],
            prefix="system",
            cwd=str(PROJECT_ROOT),
            description="Native OS hardware metrics, process management, clipboard, and notifications.",
            switch="",
            timeout=30.0,
            provider="system",
        ),
        ServerSpec(
            name="filesystem",
            command=[python, "-m", "mcp_host.servers.filesystem.server"],
            prefix="files",
            cwd=str(PROJECT_ROOT),
            description="Native Python filesystem operations for reading, writing, searching, and managing workspace files.",
            switch="",
            timeout=30.0,
            provider="filesystem",
        ),
        ServerSpec(
            name="web_tools",
            command=[python, "-m", "mcp_host.servers.web_tools.server"],
            prefix="web",
            cwd=str(PROJECT_ROOT),
            description="Web page text extraction, DuckDuckGo web search, and HTTP endpoint tester.",
            switch="",
            timeout=30.0,
            provider="web",
        ),
        ServerSpec(
            name="memory_vault",
            command=[python, "-m", "mcp_host.servers.memory_vault.server"],
            prefix="vault",
            cwd=str(PROJECT_ROOT),
            description="Direct search, read, and write access to the JARVIS Obsidian Vault and knowledge base.",
            switch="",
            timeout=30.0,
            provider="vault",
        ),
        ServerSpec(
            name="dev_tools",
            command=[python, "-m", "mcp_host.servers.dev_tools.server"],
            prefix="dev",
            cwd=str(PROJECT_ROOT),
            description="Developer environment tools: Git inspection, workspace command runner, and Python evaluation.",
            switch="",
            timeout=45.0,
            provider="terminal",
        ),
    ]


def presets() -> list[dict]:
    """Provider presets for the Add flow — a template, never a subsystem.

    Each entry pre-fills the form (transport, URL or command, auth shape) and
    names the icon to draw. Choosing a preset writes configuration; it never
    changes which code path runs, because there is only one.
    """
    python = sys.executable or "python"
    return [
        {
            "key": "system_tools",
            "name": "System Tools (Native)",
            "description": "Native OS system metrics, process management, clipboard, and desktop controls.",
            "icon": "system",
            "transport": "stdio",
            "command": [python],
            "args": ["-m", "mcp_host.servers.system_tools.server"],
            "docs": "mcp_host/servers/system_tools",
        },
        {
            "key": "filesystem_native",
            "name": "Filesystem (Native)",
            "description": "Native Python filesystem operations for reading, writing, and searching files.",
            "icon": "filesystem",
            "transport": "stdio",
            "command": [python],
            "args": ["-m", "mcp_host.servers.filesystem.server"],
            "docs": "mcp_host/servers/filesystem",
        },
        {
            "key": "web_tools",
            "name": "Web Tools (Native)",
            "description": "Web page extraction, DuckDuckGo web search, and HTTP endpoint testing.",
            "icon": "web",
            "transport": "stdio",
            "command": [python],
            "args": ["-m", "mcp_host.servers.web_tools.server"],
            "docs": "mcp_host/servers/web_tools",
        },
        {
            "key": "memory_vault",
            "name": "Memory Vault (Obsidian)",
            "description": "Direct search, read, and write access to the JARVIS Obsidian Vault and notes.",
            "icon": "vault",
            "transport": "stdio",
            "command": [python],
            "args": ["-m", "mcp_host.servers.memory_vault.server"],
            "docs": "mcp_host/servers/memory_vault",
        },
        {
            "key": "dev_tools",
            "name": "Developer Tools (Native)",
            "description": "Terminal command execution, Git repository status, and Python evaluation.",
            "icon": "terminal",
            "transport": "stdio",
            "command": [python],
            "args": ["-m", "mcp_host.servers.dev_tools.server"],
            "docs": "mcp_host/servers/dev_tools",
        },
        {
            "key": "composio",
            "name": "Composio",
            "description": "Composio's hosted MCP endpoint — every connected "
                           "app as tools.",
            "icon": "composio",
            "transport": "http",
            "url": "https://connect.composio.dev/mcp",
            "auth": "custom_header",
            "header_names": ["x-consumer-api-key"],
            "docs": "https://mcp.composio.dev",
        },
        {
            "key": "github",
            "name": "GitHub (Cloud)",
            "description": "GitHub's official MCP server — repositories, issues, "
                           "pull requests and Actions.",
            "icon": "github",
            "transport": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "auth": "bearer",
            "header_names": ["Authorization"],
            "docs": "https://github.com/github/github-mcp-server",
        },
        {
            "key": "github_local",
            "name": "GitHub (Local Docker)",
            "description": "GitHub's official MCP server as a local process "
                           "(Docker), authenticated with a personal token.",
            "icon": "github",
            "transport": "stdio",
            "command": ["docker"],
            "args": ["run", "-i", "--rm", "-e",
                     "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
            "env_names": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
            "docs": "https://github.com/github/github-mcp-server",
        },
        {
            "key": "filesystem_node",
            "name": "Filesystem (Node/npx)",
            "description": "Reference Node filesystem server through npx.",
            "icon": "filesystem",
            "transport": "stdio",
            "command": ["npx"],
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
            "docs": "https://github.com/modelcontextprotocol/servers",
        },
        {
            "key": "custom",
            "name": "Custom server",
            "description": "Any compatible MCP server, local or remote.",
            "icon": "server",
            "transport": "http",
            "url": "",
            "auth": "none",
        },
    ]


def _entry_to_spec(entry: dict, base: ServerSpec | None = None) -> ServerSpec | None:
    """One JSON entry → a spec, merged over its built-in base when it has one."""
    if not isinstance(entry, dict) or not entry.get("name"):
        return None
    name = str(entry["name"])
    base = base or ServerSpec(name=name)
    command = entry.get("command", None)
    if isinstance(command, str):
        command = [command]
    transport = str(entry.get("transport") or base.transport or "stdio").lower()
    if transport in ("streamable_http", "streamable-http", "sse", "remote"):
        transport = "http"
    env_names = entry.get("env_names")
    if not isinstance(env_names, list):
        # An older file may still carry an ``env`` map with values written into
        # it. Only the *names* are honoured now — the values belong in the
        # guarded credential store, and silently keeping plaintext ones would be
        # the exact leak this split exists to prevent.
        legacy_env = entry.get("env")
        env_names = (list(legacy_env.keys()) if isinstance(legacy_env, dict)
                     else list((base.env or {}).keys()))
    env = {str(key): "" for key in env_names if str(key)}
    header_names = entry.get("header_names")
    if not isinstance(header_names, list):
        header_names = list((entry.get("headers") or {}).keys()) or \
            list((base.headers or {}).keys())
    headers = {str(key): "" for key in header_names if str(key)}
    cmd_parts = [str(part) for part in command] if command else list(base.command)
    if cmd_parts and cmd_parts[0].lower().endswith("pythonw.exe"):
        py_cand = cmd_parts[0][:-9] + "python.exe"
        if Path(py_cand).exists():
            cmd_parts[0] = py_cand
    spec = ServerSpec(
        name=name,
        command=cmd_parts,
        prefix=str(entry.get("prefix") or base.prefix or name),
        enabled=bool(entry.get("enabled", base.enabled)),
        cwd=str(entry.get("cwd") or base.cwd or ""),
        env=env,
        timeout=float(entry.get("timeout") or base.timeout),
        description=str(entry.get("description") or base.description),
        switch=str(entry.get("switch") or base.switch or ""),
        transport=transport,
        url=str(entry.get("url") or base.url or ""),
        headers=headers,
        header_names=[str(n) for n in header_names],
        auth=str(entry.get("auth") or base.auth or "none"),
        permissions=_merged_permissions(entry.get("permissions") or base.permissions),
        reconnect=_merged_reconnect(entry.get("reconnect") or base.reconnect),
        allow_insecure_http=bool(entry.get("allow_insecure_http",
                                           base.allow_insecure_http)),
        protocol_override=str(entry.get("protocol_override")
                              or base.protocol_override or ""),
        start_timeout=float(entry.get("start_timeout") or base.start_timeout),
        provider=str(entry.get("provider") or base.provider or ""),
    )
    if spec.is_http:
        if not spec.url:
            return None
    elif not spec.command or not all(spec.command):
        return None
    return spec


def load_servers() -> list[ServerSpec]:
    """Built-ins, with the JSON file merged over them by name."""
    servers = {spec.name: spec for spec in builtin_servers()}
    path = config_path()
    if not path.exists():
        return list(servers.values())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return list(servers.values())
    entries = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return list(servers.values())
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = str(entry["name"])
        spec = _entry_to_spec(entry, servers.get(name))
        if spec is not None:
            servers[name] = spec
    return list(servers.values())


def save_servers(specs: list[ServerSpec]) -> tuple[bool, str]:
    """Write the override file (the whole list, so removals stick)."""
    path = config_path()
    payload = {"servers": [spec.as_dict() for spec in specs]}
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True, f"MCP server list saved to {path}."
    except Exception as exc:
        return False, f"The MCP server list could not be saved: {exc}"


def probe(spec: ServerSpec) -> dict:
    """Can this server be reached at all? No process is started, no socket opened."""
    if spec.is_http:
        url = str(spec.url or "").strip()
        if not url:
            return {"ok": False, "detail": "no server URL is configured"}
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"ok": False,
                    "detail": f"'{url}' is not a usable http:// or https:// address"}
        if parsed.scheme == "http" and not spec.allow_insecure_http:
            return {"ok": False,
                    "detail": "the endpoint is plain HTTP — allow insecure HTTP "
                              "for this server, or use https://"}
        return {"ok": True, "detail": f"{parsed.netloc} configured"}
    if not spec.command:
        return {"ok": False, "detail": "no command is configured for this server"}
    executable = spec.command[0]
    if executable == "python":
        executable = sys.executable or "python"
    if executable.lower().endswith("pythonw.exe"):
        cand = executable[:-9] + "python.exe"
        if Path(cand).exists():
            executable = cand
    resolved = shutil.which(executable) or (
        executable if Path(executable).exists() else "")
    if not resolved:
        return {"ok": False,
                "detail": f"'{spec.command[0]}' was not found on this machine — "
                          f"install it, or point the server at a real executable"}
    if spec.cwd and not Path(spec.cwd).exists():
        return {"ok": False, "detail": f"the working directory {spec.cwd} does "
                                       f"not exist"}
    return {"ok": True, "detail": f"{Path(resolved).name} found"}


def switches_in_use() -> dict[str, list[str]]:
    """Settings switch → the servers that honour it (for the UI)."""
    out: dict[str, list[str]] = {}
    for spec in load_servers():
        if spec.switch:
            out.setdefault(spec.switch, []).append(spec.name)
    return out


def mcp_status() -> dict[str, Any]:
    """Everything the Settings window needs, without starting anything."""
    servers = load_servers()
    return {
        "config_path": str(config_path()),
        "servers": [
            {**spec.as_dict(), "namespace": spec.namespace,
             "is_http": spec.is_http, "probe": probe(spec)}
            for spec in servers
        ],
    }
