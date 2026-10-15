"""MCP credential storage and client-config generation.

Two rules are worth a test each, and both are about *not* leaking or losing a
secret:

* the store touches only the fields it is given — editing a header must not
  erase a bearer token — and an empty value clears exactly that field;
* a generated client configuration shows placeholders by default, never a real
  credential, and a stored secret is redacted before it can reach a log line.

The config file itself is never touched: ``_read``/``_write`` are swapped for an
in-memory dict, so these tests exercise the merge logic and nothing else.
"""
from __future__ import annotations

import json

import pytest

from mcp_host import client_configs, config_gen, secrets


@pytest.fixture()
def fake_config(monkeypatch):
    state = {"data": {}}

    def _read():
        return state["data"]

    def _write(data):
        state["data"] = data
        return True, ""

    monkeypatch.setattr(secrets, "_read", _read)
    monkeypatch.setattr(secrets, "_write", _write)
    return state


# ── secrets ──────────────────────────────────────────────────────────────────

def test_storing_a_bearer_token_and_reading_it_back(fake_config):
    assert secrets.set_server_secrets("github", bearer_token="ghp_secret_value")[0]
    assert secrets.bearer_token("github") == "ghp_secret_value"
    assert secrets.has_secrets("github")
    assert fake_config["data"]["mcp_secrets"]["github"] == \
        {"bearer_token": "ghp_secret_value"}


def test_editing_one_field_does_not_erase_another(fake_config):
    secrets.set_server_secrets("github", bearer_token="ghp_secret_value")
    secrets.set_server_secrets("github", headers={"X-Api-Key": "header_value"})
    entry = secrets.server_secrets("github")
    assert entry["bearer_token"] == "ghp_secret_value"
    assert entry["headers"] == {"X-Api-Key": "header_value"}
    # Clearing the token leaves the header behind.
    secrets.set_server_secrets("github", bearer_token="")
    entry = secrets.server_secrets("github")
    assert entry["bearer_token"] == ""
    assert entry["headers"] == {"X-Api-Key": "header_value"}


def test_a_fully_emptied_entry_is_removed(fake_config):
    secrets.set_server_secrets("github", bearer_token="tok")
    secrets.set_server_secrets("github", bearer_token="")
    assert "github" not in fake_config["data"].get("mcp_secrets", {})
    assert not secrets.has_secrets("github")


def test_rename_carries_credentials_across(fake_config):
    secrets.set_server_secrets("old-name", bearer_token="tok")
    assert secrets.rename_server("old-name", "new-name")[0]
    assert secrets.bearer_token("new-name") == "tok"
    assert not secrets.has_secrets("old-name")


def test_clear_removes_every_credential(fake_config):
    secrets.set_server_secrets("github", bearer_token="tok",
                               env={"GITHUB_TOKEN": "envtok"})
    assert secrets.clear_server_secrets("github")[0]
    assert not secrets.has_secrets("github")


def test_a_nameless_server_is_refused(fake_config):
    ok, message = secrets.set_server_secrets("", bearer_token="tok")
    assert ok is False and "no name" in message


def test_masking_keeps_keys_and_hides_values():
    assert secrets.mask("") == ""
    assert secrets.mask("anything") == secrets.MASK
    masked = secrets.mask_map({"Authorization": "Bearer abcdef", "Empty": ""})
    assert masked == {"Authorization": secrets.MASK, "Empty": ""}


def test_redact_removes_a_stored_secret_from_free_text(fake_config):
    secrets.set_server_secrets("github", bearer_token="ghp_long_secret_value")
    line = "GET https://api.github.com?token=ghp_long_secret_value failed"
    assert "ghp_long_secret_value" not in secrets.redact(line)
    assert secrets.MASK in secrets.redact(line)
    # A short value is left alone: masking spelling mistakes helps nobody.
    assert secrets.redact("short") == "short"


# ── client config generation ─────────────────────────────────────────────────

class _Spec:
    """The small slice of ``ServerSpec`` the generator reads."""

    def __init__(self, name, *, http=False, url="", auth="none", headers=(),
                 command=(), env=(), header_names=()):
        self.name = name
        self.is_http = http
        self.url = url
        self.auth = auth
        self.headers = dict(headers)
        self.command = list(command)
        self.env = dict(env)
        self._header_names = list(header_names)

    def header_name_list(self):
        return list(self._header_names)


def test_placeholder_for_is_obvious_and_safe():
    assert config_gen.placeholder_for("x-consumer-api-key") == \
        "<YOUR_X_CONSUMER_API_KEY>"
    assert config_gen.placeholder_for("") == "<YOUR_CREDENTIAL>"


def test_stdio_config_uses_placeholders_by_default():
    spec = _Spec("filesystem", command=["npx", "-y", "@modelcontextprotocol/server"],
                 env={"API_KEY": "value"})
    cfg = config_gen.client_config(spec, "generic")
    entry = cfg["mcpServers"]["filesystem"]
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@modelcontextprotocol/server"]
    assert entry["env"] == {"API_KEY": "<YOUR_API_KEY>"}
    # No real value ever reaches the default output.
    assert "value" not in json.dumps(cfg)


def test_http_bearer_config_and_the_vscode_layout():
    spec = _Spec("github", http=True, url="https://api.githubcopilot.com/mcp/",
                 auth="bearer")
    generic = config_gen.client_config(spec, "generic")
    entry = generic["mcpServers"]["github"]
    assert entry["url"] == "https://api.githubcopilot.com/mcp/"
    assert entry["headers"]["Authorization"] == "Bearer <YOUR_API_KEY>"
    vscode = config_gen.client_config(spec, "vscode")
    assert "servers" in vscode and vscode["servers"]["github"]["type"] == "http"


def test_include_secrets_is_opt_in():
    spec = _Spec("github", http=True, url="https://x/", auth="bearer")
    cfg = config_gen.client_config(spec, "generic", include_secrets=True,
                                   secret_values={"bearer_token": "real_token"})
    assert cfg["mcpServers"]["github"]["headers"]["Authorization"] == \
        "Bearer real_token"


def test_named_headers_become_placeholders():
    spec = _Spec("composio", http=True, url="https://x/",
                 header_names=["X-Consumer-Api-Key"])
    cfg = config_gen.client_config(spec, "generic")
    headers = cfg["mcpServers"]["composio"]["headers"]
    assert headers["X-Consumer-Api-Key"] == "<YOUR_X_CONSUMER_API_KEY>"


def test_pairs_flattens_and_sorts():
    mapping = {"mcpServers": {"s": {"command": "npx", "args": ["a", "b"]}}}
    assert config_gen.pairs(mapping) == [
        ("mcpServers.s.args", '["a", "b"]'),
        ("mcpServers.s.command", "npx"),
    ]


def test_validate_accepts_json_and_refuses_nonsense():
    ok, _ = config_gen.validate('{"mcpServers": {}}')
    assert ok
    assert config_gen.validate("nope")[0] is False
    assert config_gen.validate("{}")[0] is False


def test_every_advertised_target_is_known():
    for target in config_gen.CLIENT_TARGETS:
        spec = _Spec("s", command=["cmd"])
        assert "mcpServers" in config_gen.client_config(spec, target) or \
            "servers" in config_gen.client_config(spec, target)


# ── writing a server into a real client's own config file ────────────────────

def test_merging_into_an_existing_client_config_keeps_everything(tmp_path):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text(json.dumps({
        "mcpServers": {"old": {"command": "old"}},
        "theme": "dark",
    }), encoding="utf-8")

    result = client_configs.save("claude", {
        "mcpServers": {"git": {"command": "npx", "args": ["-y", "server-git"]}}},
        path=target)

    assert result["ok"] is True and result["servers"] == 2
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["mcpServers"]["old"] == {"command": "old"}   # nothing lost
    assert saved["theme"] == "dark"                            # unrelated keys too
    assert saved["mcpServers"]["git"]["command"] == "npx"
    # The untouched original is kept exactly once.
    assert (tmp_path / "claude_desktop_config.json.jarvis-backup").exists()


def test_a_second_save_adds_on_top_without_a_second_backup(tmp_path):
    target = tmp_path / "cline_mcp_settings.json"
    target.write_text("{}", encoding="utf-8")
    client_configs.save("cline", {"mcpServers": {"one": {}}}, path=target)
    client_configs.save("cline", {"mcpServers": {"two": {}}}, path=target)
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(saved["mcpServers"]) == ["one", "two"]
    backups = list(tmp_path.glob("*.jarvis-backup"))
    assert len(backups) == 1


def test_a_broken_target_is_refused_not_overwritten(tmp_path):
    target = tmp_path / "claude_desktop_config.json"
    target.write_text("{not json", encoding="utf-8")

    result = client_configs.save("claude", {"mcpServers": {"x": {}}}, path=target)

    assert result["ok"] is False
    assert "left untouched" in result["detail"]
    assert target.read_text(encoding="utf-8") == "{not json"


def test_merge_puts_the_new_server_on_top_of_the_existing_root():
    merged = client_configs.merge({"mcpServers": {"a": {"command": "a"}},
                                   "other": True},
                                  {"mcpServers": {"b": {"command": "b"}}})
    assert merged["mcpServers"] == {"a": {"command": "a"},
                                    "b": {"command": "b"}}
    assert merged["other"] is True


def test_an_unknown_client_has_no_known_path_and_says_so():
    assert client_configs.candidates("generic") == []
    result = client_configs.save("generic", {"mcpServers": {"x": {}}})
    assert result["ok"] is False
    assert "choose one" in result["detail"]
