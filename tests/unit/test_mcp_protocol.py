"""MCP protocol era, transport selection, and the tool safety gate.

Pure logic only — no process is spawned and no socket is opened. These are the
decisions that are expensive (or dangerous) to get wrong:

* which protocol version the client claims, and that the legacy versions are
  still recognised for a server that predates the stateless spec;
* which transport a spec is bound to (local stdio vs remote HTTP);
* whether a tool is exposed at all, and whether it must stop for a human.
"""
from __future__ import annotations

from mcp_host import catalog, protocol, transport
from mcp_host.manager import classify_tool


class _Tool:
    def __init__(self, name, description=""):
        self.name = name
        self.description = description


# ── protocol versioning ──────────────────────────────────────────────────────

def test_the_modern_version_is_the_latest_date_of_the_spec():
    assert protocol.MODERN_PROTOCOL_VERSION == "2026-07-28"
    assert protocol.LATEST_PROTOCOL_VERSION == protocol.MODERN_PROTOCOL_VERSION
    assert protocol.SUPPORTED_PROTOCOL_VERSIONS[0] == "2026-07-28"


def test_legacy_versions_are_recognised_for_older_servers():
    assert "2025-06-18" in protocol.LEGACY_PROTOCOL_VERSIONS
    assert "2024-11-05" in protocol.LEGACY_PROTOCOL_VERSIONS
    # The modern one is not "legacy".
    assert protocol.MODERN_PROTOCOL_VERSION not in protocol.LEGACY_PROTOCOL_VERSIONS


def test_the_stateless_meta_keys_are_fully_qualified():
    assert protocol.META_PROTOCOL_VERSION == \
        "io.modelcontextprotocol/protocolVersion"
    assert protocol.META_CLIENT_INFO == "io.modelcontextprotocol/clientInfo"
    assert protocol.META_CLIENT_CAPABILITIES == \
        "io.modelcontextprotocol/clientCapabilities"


# ── transport selection ──────────────────────────────────────────────────────

def test_a_local_command_is_bound_to_stdio():
    spec = catalog.ServerSpec(name="filesystem",
                              command=["npx", "-y", "server-filesystem"])
    assert isinstance(transport.transport_for(spec), transport.StdioTransport)


def test_a_remote_spec_is_bound_to_streamable_http():
    from mcp_host.http_client import StreamableHttpTransport

    spec = catalog.ServerSpec(name="github", transport="http",
                              url="https://api.githubcopilot.com/mcp/")
    assert isinstance(transport.transport_for(spec), StreamableHttpTransport)


def test_sse_is_treated_as_http_not_stdio():
    from mcp_host.http_client import StreamableHttpTransport

    spec = catalog.ServerSpec(name="old", transport="sse", url="https://x/sse")
    assert isinstance(transport.transport_for(spec), StreamableHttpTransport)


# ── naming ───────────────────────────────────────────────────────────────────

def test_a_namespace_is_cleaned_and_safe_to_prefix_onto_a_tool():
    spec = catalog.ServerSpec(name="My Server!")
    assert spec.namespace == "My_Server"
    prefixed = catalog.ServerSpec(name="github", prefix="gh")
    assert prefixed.namespace == "gh"


# ── the safety gate ──────────────────────────────────────────────────────────

def test_a_read_tool_is_exposed_by_default():
    spec = catalog.ServerSpec(name="s")
    assert spec.tool_exposed("list_files", "read") is True


def test_a_destructive_tool_stays_hidden_until_it_is_named():
    spec = catalog.ServerSpec(name="s")
    assert spec.tool_exposed("delete_everything", "destructive") is False
    spec.permissions["enabled_tools"] = ["delete_everything"]
    assert spec.tool_exposed("delete_everything", "destructive") is True


def test_a_switched_off_tool_is_never_exposed():
    spec = catalog.ServerSpec(name="s")
    spec.permissions["disabled_tools"] = ["search"]
    assert spec.tool_exposed("search", "read") is False
    assert spec.tool_exposed("other", "read") is True


def test_writes_and_destructive_tools_require_confirmation():
    spec = catalog.ServerSpec(name="s")
    assert spec.needs_approval("read") is False
    assert spec.needs_approval("write") is True
    assert spec.needs_approval("destructive") is True


def test_approval_can_be_switched_off_per_server():
    spec = catalog.ServerSpec(name="s")
    spec.permissions["require_approval_writes"] = False
    spec.permissions["confirm_destructive"] = False
    assert spec.needs_approval("write") is False
    assert spec.needs_approval("destructive") is False


def test_a_per_tool_safety_override_wins():
    spec = catalog.ServerSpec(name="s")
    spec.permissions["tool_safety"] = {"read_only_looking": "destructive"}
    assert spec.safety_of("read_only_looking", "read") == "destructive"
    assert spec.safety_of("other", "read") == "read"


# ── heuristic classification ─────────────────────────────────────────────────

def test_a_destructive_name_is_classified_destructive():
    assert classify_tool(_Tool("delete_file", "remove a file")) == "destructive"
    assert classify_tool(_Tool("drop_table")) == "destructive"


def test_a_write_name_is_classified_write():
    assert classify_tool(_Tool("create_issue", "open a new issue")) == "write"
    assert classify_tool(_Tool("send_message")) == "write"


def test_a_plain_read_name_is_classified_read():
    assert classify_tool(_Tool("list_repos", "list repositories")) == "read"
    assert classify_tool(_Tool("get_file", "return a file")) == "read"


# ── presets ──────────────────────────────────────────────────────────────────

def test_the_github_preset_is_a_remote_https_server():
    presets = {preset["key"]: preset for preset in catalog.presets()}
    assert "github" in presets
    github = presets["github"]
    assert github["transport"] == "http"
    assert github["url"].startswith("https://")
    # The local variant, by contrast, runs a command over stdio.
    assert presets["github_local"]["transport"] == "stdio"
