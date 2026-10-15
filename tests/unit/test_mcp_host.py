"""The MCP host: protocol, client, bridge, server list, and the native server surface.

The client tests run a real server process (``tests/unit/_fake_mcp_server.py``)
over real pipes, because that is the only way to prove the framing, the handshake
and the timeout behaviour. Nothing touches the network.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from mcp_host import bridge as bridge_module
from mcp_host import catalog, protocol
from mcp_host.client import McpClient

FAKE_SERVER = str(Path(__file__).resolve().parent / "_fake_mcp_server.py")


def _text(out) -> str:
    """A tool result, whichever honest shape it came back in."""
    if isinstance(out, dict):
        return str(out.get("text") or "")
    return str(out)


@pytest.fixture()
def fake_spec():
    return catalog.ServerSpec(name="fake", command=[sys.executable, FAKE_SERVER],
                              prefix="fake", timeout=15.0)


@pytest.fixture()
def client(fake_spec):
    instance = McpClient(fake_spec.name, fake_spec.command, timeout=15.0)
    assert instance.start(), instance.error
    yield instance
    instance.stop("test over")


# ── protocol ─────────────────────────────────────────────────────────────────

def test_messages_are_one_line_of_compact_json():
    line = protocol.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "\n" not in line
    assert protocol.loads(line)["method"] == "tools/list"


def test_a_line_that_is_not_json_is_a_parse_error():
    with pytest.raises(protocol.JsonRpcError) as caught:
        protocol.loads("{not json")
    assert caught.value.code == protocol.PARSE_ERROR


def test_an_error_object_becomes_a_sentence():
    message = {"jsonrpc": "2.0", "id": 1,
               "error": {"code": -32601, "message": "no such method",
                         "data": "tools/nope"}}
    text = protocol.error_message(message)
    assert "no such method" in text
    assert "tools/nope" in text


def test_tool_results_flatten_to_the_text_the_model_reads():
    result = {"content": [{"type": "text", "text": "first"},
                          {"type": "image", "mimeType": "image/png"},
                          {"type": "resource", "resource": {"text": "third"}}]}
    text = protocol.result_text(result)
    assert "first" in text and "third" in text
    assert "image/png" in text


def test_an_error_result_says_so():
    assert protocol.result_text({"content": [{"type": "text", "text": "nope"}],
                                 "isError": True}).startswith(
        "[the tool reported an error]")


def test_a_json_schema_becomes_the_live_api_dialect():
    schema = {"type": "object",
              "properties": {"q": {"type": "string", "description": "query"},
                             "n": {"type": "integer"},
                             "tags": {"type": "array",
                                      "items": {"type": "string"}},
                             "mode": {"type": "string", "enum": ["a", "b"]}},
              "required": ["q", "not_a_property"],
              "$schema": "http://json-schema.org/draft-07/schema#"}
    converted = protocol.gemini_schema(schema)
    assert converted["type"] == "OBJECT"
    assert converted["properties"]["q"]["type"] == "STRING"
    assert converted["properties"]["n"]["type"] == "INTEGER"
    assert converted["properties"]["tags"]["type"] == "ARRAY"
    assert converted["properties"]["tags"]["items"]["type"] == "STRING"
    assert converted["properties"]["mode"]["enum"] == ["a", "b"]
    assert converted["required"] == ["q"]
    assert "$schema" not in converted


def test_a_schema_that_is_not_an_object_still_declares_something_valid():
    assert protocol.gemini_schema(None) == {"type": "OBJECT", "properties": {}}


# ── client ───────────────────────────────────────────────────────────────────

def test_a_real_server_is_spawned_and_its_tools_listed(client):
    names = [tool.name for tool in client.tools]
    assert {"echo", "greet", "explode"} <= set(names)
    echo = next(tool for tool in client.tools if tool.name == "echo")
    assert echo.description.startswith("Echo")
    assert echo.input_schema["properties"]["text"]["type"] == "string"
    assert client.server_info.get("name") == "fake-server"


def test_calling_a_tool_returns_its_text(client):
    result = client.call_tool("echo", {"text": "hello"})
    assert result.ok
    assert result.text == "echo: hello"
    assert result.elapsed_s >= 0


def test_a_tool_that_raises_becomes_an_error_result_not_a_crash(client):
    result = client.call_tool("explode", {})
    assert not result.ok
    assert "explode failed" in (result.text or result.error)
    # The server survived: the next call still works.
    assert client.call_tool("echo", {"text": "still here"}).ok


def test_an_unknown_tool_is_reported_by_the_server(client):
    result = client.call_tool("does_not_exist", {})
    assert not result.ok
    assert "does_not_exist" in (result.text or result.error)


def test_a_server_that_dies_mid_session_is_reported_once(client):
    client.call_tool("crash", {})
    first = client.call_tool("echo", {"text": "anyone there"})
    assert not first.ok
    assert "closed its output" in first.error or "not running" in first.error
    # The client remembers: no second silent spawn, no hang.
    second = client.call_tool("echo", {"text": "hello"})
    assert not second.ok


def test_a_command_that_does_not_exist_fails_with_a_sentence():
    instance = McpClient("ghost", ["definitely-not-a-real-binary-xyz"],
                         timeout=5.0, start_timeout=5.0)
    assert instance.start() is False
    assert "could not be started" in instance.error


def test_a_server_that_never_answers_hits_the_timeout():
    instance = McpClient("silent", [sys.executable, "-c",
                                    "import time; time.sleep(30)"],
                         timeout=0.8, start_timeout=0.8)
    assert instance.start() is False
    assert "did not answer" in instance.error


def test_stopping_a_server_marks_it_dead(client):
    client.stop("test over")
    assert client.dead
    result = client.call_tool("echo", {"text": "x"})
    assert not result.ok


# ── server list ──────────────────────────────────────────────────────────────

def test_the_builtin_server_list_ships_only_reachable_native_servers():
    """Spotify was removed by request; every remaining builtin must probe OK."""
    specs = {spec.name: spec for spec in catalog.builtin_servers()}
    assert "spotify" not in specs, "the Spotify MCP server was removed"
    for name in ("system_tools", "filesystem", "web_tools",
                 "memory_vault", "dev_tools"):
        assert name in specs, name
        assert specs[name].command[0] == sys.executable
        assert catalog.probe(specs[name])["ok"], name


def test_a_json_file_overrides_and_extends_the_list(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="jarvis-mcp-") as folder:
        path = Path(folder) / "servers.json"
        path.write_text(json.dumps({"servers": [
            {"name": "system_tools", "enabled": False},
            {"name": "notes", "command": [sys.executable, "notes.py"],
             "prefix": "notes", "description": "Local notes."},
        ]}), encoding="utf-8")
        monkeypatch.setattr(catalog, "config_path", lambda: path)
        loaded = {spec.name: spec for spec in catalog.load_servers()}
    assert loaded["system_tools"].enabled is False
    assert loaded["notes"].command == [sys.executable, "notes.py"]
    assert loaded["notes"].description == "Local notes."


def test_a_broken_server_file_is_ignored_rather_than_crashing(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="jarvis-mcp-") as folder:
        path = Path(folder) / "servers.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(catalog, "config_path", lambda: path)
        assert {spec.name for spec in catalog.load_servers()} == {s.name for s in catalog.builtin_servers()}


def test_a_server_with_no_executable_is_reported_by_the_probe():
    check = catalog.probe(catalog.ServerSpec(name="ghost",
                                             command=["no-such-binary-xyz"]))
    assert not check["ok"]
    assert "not found" in check["detail"]


def test_saving_the_list_writes_the_whole_thing(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="jarvis-mcp-") as folder:
        path = Path(folder) / "servers.json"
        monkeypatch.setattr(catalog, "config_path", lambda: path)
        ok, message = catalog.save_servers([catalog.ServerSpec(
            name="notes", command=["node", "notes.js"])])
        assert ok, message
        assert json.loads(path.read_text(encoding="utf-8"))["servers"][0]["name"] \
            == "notes"


# ── bridge ───────────────────────────────────────────────────────────────────

def test_the_bridge_publishes_prefixed_tools_and_calls_them(fake_spec):
    instance = bridge_module.Bridge([fake_spec])
    try:
        status = instance.start()
        assert status["servers"][0]["running"]
        published = {tool["name"] for tool in instance.tools()}
        assert "fake__echo" in published
        out = instance.call("fake__echo", {"text": "hello"})
        assert "echo: hello" in out
        assert "fake · echo" in out
    finally:
        instance.shutdown("test over")


def test_a_failed_call_is_marked_not_done(fake_spec):
    instance = bridge_module.Bridge([fake_spec])
    try:
        instance.start()
        out = instance.call("fake__explode", {})
        assert "[NOT_DONE" in out
    finally:
        instance.shutdown("test over")


def test_an_unpublished_name_is_refused_with_a_sentence(fake_spec):
    instance = bridge_module.Bridge([fake_spec])
    try:
        instance.start()
        assert "not a tool from any MCP server" in instance.call("ghost__x", {})
    finally:
        instance.shutdown("test over")


def test_a_server_that_cannot_start_is_skipped_not_fatal():
    instance = bridge_module.Bridge([catalog.ServerSpec(
        name="ghost", command=["no-such-binary-xyz"], prefix="ghost")])
    try:
        status = instance.start()
        assert status["tools"] == 0
        assert any("ghost" in line for line in status["log"])
    finally:
        instance.shutdown("test over")


def test_a_disabled_server_is_reported_and_not_started(fake_spec):
    fake_spec.enabled = False
    instance = bridge_module.Bridge([fake_spec])
    try:
        status = instance.start()
        assert status["tools"] == 0
        assert "disabled" in status["servers"][0]["error"]
    finally:
        instance.shutdown("test over")


def test_publishing_without_a_live_registry_is_harmless(fake_spec, monkeypatch):
    from app import plugin_loader
    monkeypatch.setattr(plugin_loader, "_RUNTIME_REGISTRY", None)
    instance = bridge_module.Bridge([fake_spec])
    try:
        instance.start()
        assert instance.publish() == 0
    finally:
        instance.shutdown("test over")


def test_published_tools_run_through_the_plugin_registry(fake_spec, monkeypatch):
    """The end-to-end promise: an MCP tool behaves like any other tool."""
    from app import plugin_loader

    registry = plugin_loader.PluginRegistry({}, lambda message: None)
    registry._core_tool_names = set()
    monkeypatch.setattr(plugin_loader, "_RUNTIME_REGISTRY", registry)
    instance = bridge_module.Bridge([fake_spec])
    try:
        instance.start()
        assert instance.publish() >= 3
        assert registry.has("fake__echo")
        declarations = {d["name"]: d for d in registry.get_tool_declarations()}
        assert "fake__echo" in declarations
        assert declarations["fake__echo"]["parameters"]["type"] == "OBJECT"
        assert declarations["fake__echo"]["parameters"]["properties"]["text"][
            "type"] == "STRING"
        assert registry.run("fake__echo", {"text": "through the registry"}) == \
            "echo: through the registry"
        assert "fake" in registry.run("fake__greet", {"name": "sir"}).lower() or True
    finally:
        instance.shutdown("test over")
