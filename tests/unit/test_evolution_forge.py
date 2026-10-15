"""The Forge + vault + hot-registration path, with the model stubbed out.

These tests prove the contract that makes self-evolution safe and useful:
a forged skill lands in ``plugins/`` as an ordinary plugin, is registered into
the *live* plugin registry, is dispatchable through it, and is listed with its
metadata — all without a restart and without ever calling a real LLM.
"""
from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path

import pytest

from app import plugin_loader
from evolution import discovery, forge, manager, registry, vault

SKILL_CODE = textwrap.dedent('''
    """Say hello to someone, forged for the tests."""

    def run(parameters: dict, player=None, session_memory=None) -> str:
        try:
            who = str(parameters.get("who") or "world").strip()
        except Exception:
            who = "world"
        return f"Hello, {who}."
''')

PACKAGE = {
    "manifest": {
        "name": "greet_forge_test",
        "description": "Greets a person by name when the user asks for a hello.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"who": {"type": "STRING", "description": "Who to greet"}},
            "required": [],
        },
        "triggers": ["say hello", "greet someone"],
        "aliases": ["greeter"],
    },
    "code": SKILL_CODE,
    "test_cases": [{"input": {}}, {"input": {"who": "sir"}}],
}


@pytest.fixture()
def scratch():
    """A private scratch directory.

    Deliberately ``tempfile`` rather than pytest's ``tmp_path``: this machine's
    pytest temp base is not writable, which breaks the ``tmp_path`` fixture in
    every test that asks for it (pre-existing, unrelated to this file).
    """
    with tempfile.TemporaryDirectory(prefix="jarvis-evolution-") as folder:
        yield Path(folder)


@pytest.fixture()
def sandbox_env(scratch, monkeypatch):
    """A throwaway plugins directory published as the live registry.

    The skill memory is redirected as well: a forge now records what it built, and
    a test must never write into the project's real data directory or its
    long-term memory. Without this the ``registry`` handler tests would pass once
    and then fail, because the second run would find the first run's record.
    """
    plugins = scratch / "plugins"
    plugins.mkdir()
    reg = plugin_loader.discover_plugins(plugins, core_tool_names={"web_search"},
                                         logger=lambda _m: None)
    plugin_loader.set_runtime_registry(reg)
    monkeypatch.setattr(manager, "_record_path",
                        lambda: scratch / "forged_skills.json")
    try:
        import memory.memory_manager as _mm
        monkeypatch.setattr(_mm, "remember", lambda *a, **k: "ok")
    except Exception:
        pass
    yield plugins, reg
    plugin_loader.set_runtime_registry(None)


def _fake_genai_module(monkeypatch, config_cls):
    """Install a stand-in ``google.genai`` so no real SDK call can happen."""
    import sys
    import types as _types
    fake = _types.ModuleType("google.genai")
    fake.Client = lambda **kwargs: _types.SimpleNamespace()
    fake_types = _types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = config_cls
    fake_types.AutomaticFunctionCallingConfig = config_cls
    fake.types = fake_types
    monkeypatch.setitem(sys.modules, "google.genai", fake)
    import google
    monkeypatch.setattr(google, "genai", fake, raising=False)
    return fake


def _stub_model(package: dict, calls: list | None = None):
    def _fake(prompt: str, system: str, config: dict):
        if calls is not None:
            calls.append(prompt)
        return json.dumps(package), "openrouter"
    return _fake


# ── vault ─────────────────────────────────────────────────────────────────────

def test_vault_writes_a_plugin_and_hot_registers_it(sandbox_env):
    plugins, reg = sandbox_env
    result = vault.install(
        name="greet_vault_test", description="Greets people.",
        code=SKILL_CODE, goal="greet someone",
        parameters={"type": "OBJECT", "properties": {}},
        triggers=["greet"], aliases=["hi_tool"],
        test_cases=[{"input": {}}], directory=plugins,
    )
    assert result["ok"], result["message"]
    assert result["registration"] == "hot"

    written = plugins / "greet_vault_test" / "plugin.py"
    assert written.exists()
    assert (plugins / "greet_vault_test" / "plugin.json").exists()
    assert reg.has("greet_vault_test")

    # It is a real plugin: dispatch through the registry the app uses.
    spoken = reg.run("greet_vault_test", {"who": "sir"})
    assert spoken == "Hello, sir."

    # ...and it appears in the Plugin Manager list.
    names = {row["name"] for row in reg.list_for_ui()}
    assert "greet_vault_test" in names


def test_vault_refuses_a_reserved_name(sandbox_env):
    plugins, _reg = sandbox_env
    result = vault.install(name="main", description="x", code=SKILL_CODE,
                           directory=plugins)
    assert not result["ok"]
    assert "reserved" in result["message"].lower()


def test_vault_refuses_a_collision_with_a_core_tool(sandbox_env):
    plugins, _reg = sandbox_env
    result = vault.install(name="web_search", description="x", code=SKILL_CODE,
                           directory=plugins)
    assert not result["ok"]
    assert "core tool" in result["message"] or "reserved" in result["message"]


def test_vault_removes_a_broken_skill_instead_of_leaving_it_on_disk(sandbox_env):
    plugins, reg = sandbox_env
    result = vault.install(name="broken_forge_test", description="x",
                           code="def not_the_entry(parameters=None):\n    return 1\n",
                           directory=plugins)
    assert not result["ok"]
    assert not (plugins / "broken_forge_test").exists()
    assert not reg.has("broken_forge_test")


def test_vault_sidecar_carries_routing_metadata(sandbox_env):
    plugins, _reg = sandbox_env
    vault.install(name="meta_forge_test", description="Metadata check.",
                  code=SKILL_CODE, goal="check metadata",
                  triggers=["check meta"], aliases=["meta"], directory=plugins)
    meta = vault.read_sidecar("meta_forge_test", plugins)
    assert meta["origin"] == "forged"
    assert meta["active"] is True
    assert "check meta" in meta["triggers"]
    assert "meta" in meta["aliases"]
    assert meta["goal"] == "check metadata"
    assert meta["author"].startswith("J.A.R.V.I.S.")


def test_toggling_a_forged_skill_persists_and_reports(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr("memory.config_manager.save_plugin_enabled",
                        lambda name, enabled: True, raising=False)
    vault.install(name="toggle_forge_test", description="Toggle me.",
                  code=SKILL_CODE, directory=plugins)
    ok, message = vault.set_active("toggle_forge_test", False, plugins)
    assert ok, message
    assert vault.read_sidecar("toggle_forge_test", plugins)["active"] is False
    assert "disabled" in message


# ── forge end to end (model stubbed) ──────────────────────────────────────────

def test_forge_installs_a_verified_skill_end_to_end(sandbox_env, monkeypatch):
    plugins, reg = sandbox_env
    monkeypatch.setattr(forge, "_ask_model", _stub_model(PACKAGE))

    result = forge.forge("say hello to someone", name="greet_forge_test",
                         directory=plugins, allow_install=False)
    assert result["ok"], result["message"]
    assert result["provider"] == "openrouter"
    assert result["attempts"] == 1
    assert reg.has("greet_forge_test")
    assert "is live" in result["message"]
    # The user hears which brain wrote the skill — "forged" alone hid it.
    assert "forged with OpenRouter" in result["message"]
    assert reg.run("greet_forge_test", {"who": "Buffy"}) == "Hello, Buffy."


def test_forge_repairs_after_a_sandbox_failure(sandbox_env, monkeypatch):
    plugins, reg = sandbox_env
    broken = dict(PACKAGE)
    broken["code"] = ("def run(parameters=None, player=None, session_memory=None):\n"
                      "    return parameters['nope']\n")

    replies = [json.dumps(broken), json.dumps({"code": SKILL_CODE})]
    prompts: list[str] = []

    def _fake(prompt: str, system: str, config: dict):
        prompts.append(prompt)
        return replies[min(len(prompts) - 1, len(replies) - 1)], "openrouter"

    monkeypatch.setattr(forge, "_ask_model", _fake)
    result = forge.forge("say hello to someone", name="greet_repair_test",
                         directory=plugins, allow_install=False)
    assert result["ok"], result["message"]
    assert result["attempts"] == 2
    assert len(prompts) == 2
    assert reg.run("greet_repair_test", {}) == "Hello, world."


def test_forge_gives_up_and_installs_nothing_when_tests_keep_failing(sandbox_env, monkeypatch):
    plugins, reg = sandbox_env
    broken = dict(PACKAGE)
    broken["code"] = ("def run(parameters=None, player=None, session_memory=None):\n"
                      "    raise RuntimeError('always broken')\n")
    monkeypatch.setattr(forge, "_ask_model", _stub_model(broken))

    result = forge.forge("do something impossible", name="never_forge_test",
                         directory=plugins, allow_install=False,
                         max_repair_attempts=1)
    assert not result["ok"]
    assert "sandbox rejected" in result["message"].lower()
    assert not reg.has("never_forge_test")
    assert not (plugins / "never_forge_test").exists()


def test_forge_reports_when_no_provider_key_exists(monkeypatch, scratch):
    monkeypatch.setattr(forge, "synthesis_providers", lambda config=None: [])
    result = forge.forge("do anything", directory=scratch, allow_install=False)
    assert not result["ok"]
    assert ("provider key" in result["message"].lower()
            or "synthesis" in result["message"].lower())


def test_forge_needs_a_real_goal(scratch):
    result = forge.forge("hi", directory=scratch)
    assert not result["ok"]
    assert "clearer" in result["message"].lower()


def test_gemini_synthesis_goes_through_the_model_router(monkeypatch):
    """A 503/429 must fail over, not end the forge.

    The forge used to walk a hard-coded model list blindly; the router owns
    model health (benching a failing model, switching to the fallback family),
    so the synthesis call must be delegated to it with the coding role.
    """
    from app import model_router

    seen: dict = {}

    class _Resp:
        text = '{"ok": true}'

    class _Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _fake_generate(client, *, contents, role="default", config=None,
                       max_attempts=3):
        seen.update({"role": role, "contents": contents, "config": config})
        return _Resp()

    fake_genai = _fake_genai_module(monkeypatch, _Config)
    assert fake_genai.Client is not None
    monkeypatch.setattr(model_router, "generate", _fake_generate)

    raw = forge._gemini_text("write a skill", "system prompt",
                             {"gemini_api_key": "AQ." + "x" * 40})
    assert raw == '{"ok": true}'
    assert seen["role"] == "coding"
    assert seen["contents"] == "write a skill"
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].automatic_function_calling.disable is True


def test_gemini_synthesis_rejects_an_empty_reply(monkeypatch):
    from app import model_router

    class _Resp:
        text = "   "

    class _Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _fake_genai_module(monkeypatch, _Config)
    monkeypatch.setattr(model_router, "generate", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError) as exc:
        forge._gemini_text("p", "s", {"gemini_api_key": "k"})
    assert "empty" in str(exc.value)


def test_parse_json_response_tolerates_fences_and_trailing_prose():
    raw = 'Sure!\n```json\n{"manifest": {"name": "x"}, "code": "def run(**k): pass"}\n```\nHope that helps.'
    data = forge.parse_json_response(raw)
    assert data["manifest"]["name"] == "x"


# ── registry facade + tool handler ────────────────────────────────────────────

def test_registry_lists_live_state_for_each_forged_skill(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    vault.install(name="listed_forge_test", description="Listed.",
                  code=SKILL_CODE, goal="be listed", directory=plugins)
    rows = registry.list_skills(plugins)
    assert [r["name"] for r in rows] == ["listed_forge_test"]
    assert rows[0]["live"] is True
    assert rows[0]["goal"] == "be listed"


def test_registry_handler_forge_action_returns_the_forge_verdict(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(forge, "_ask_model", _stub_model(PACKAGE))
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    out = registry.handler({"action": "forge", "goal": "say hello to someone",
                            "name": "greet_handler_test"})
    assert "is live" in out or "forged" in out.lower()


def test_registry_handler_reports_the_real_forge_phases(sandbox_env, monkeypatch):
    """The HUD is told the stages that actually ran, in the order they ran.

    Each phase is emitted from the boundary of the work it names, so this is a
    real progress channel — not a timer and not a synthetic percentage.
    """
    plugins, _reg = sandbox_env
    monkeypatch.setattr(forge, "_ask_model", _stub_model(PACKAGE))
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    seen: list[str] = []
    registry.handler({"action": "forge", "goal": "say hello to someone",
                      "name": "greet_phase_test"},
                     progress=lambda phase, detail: seen.append(phase))

    assert seen[0] == "ANALYZE"
    assert "GENERATE" in seen and "VERIFY" in seen
    assert "REGISTER" in seen and seen[-1] == "COMPLETE"
    assert seen.index("GENERATE") < seen.index("VERIFY") < seen.index("REGISTER")


def test_registry_handler_rejects_an_unknown_action(sandbox_env):
    out = registry.handler({"action": "explode"})
    assert "not a skill-forge action" in out.lower()
    assert "improve" in out.lower(), "the improved action list must name 'improve'"


def test_registry_handler_status_never_raises_without_skills(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    out = registry.handler({"action": "status"})
    assert "no new skills" in out.lower()


def test_status_report_names_the_synthesis_brain_and_any_skipped_key(
        sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    monkeypatch.setattr(forge, "synthesis_providers",
                        lambda config=None: [("openrouter", {})])
    monkeypatch.setattr(forge, "skipped_providers", lambda config=None: [
        {"provider": "openai", "detail": "the stored value is only 4 characters long"}])
    out = registry.status_report(plugins)
    assert "Synthesis brain: openrouter" in out
    assert "openai" in out and "not used" in out.lower()


# ── the Crucible report carried by an installed skill ─────────────────────────

def _install_with_report(plugins, name="reported_forge_test"):
    """Install SKILL_CODE with a real Crucible report attached."""
    from evolution import crucible
    verification = {
        "ok": True, "attempts": 1,
        "hazards": crucible.scan_hazards(SKILL_CODE),
        "metrics": crucible.analyze_code(SKILL_CODE),
        "contract": True, "dependencies": [], "verified_at": 1700000000.0,
    }
    return vault.install(name=name, description="Reports its verdict.",
                         code=SKILL_CODE, goal="be verified", directory=plugins,
                         verification=verification)


def test_installed_skill_carries_its_crucible_report(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    result = _install_with_report(plugins)
    assert result["ok"], result["message"]
    sidecar = vault.read_sidecar("reported_forge_test", plugins)
    assert sidecar["verification"]["metrics"]["quality_score"] > 0
    assert sidecar["verification"]["attempts"] == 1


def test_registry_lists_the_quality_score(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    _install_with_report(plugins, name="scored_forge_test")
    rows = registry.list_skills(plugins)
    assert rows and rows[0]["quality_score"] > 0


def test_reverify_re_proves_an_installed_skill(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    _install_with_report(plugins, name="reverify_forge_test")
    out = registry.verify_skill("reverify_forge_test", plugins)
    assert "passed the crucible again" in out.lower()
    assert "quality" in out.lower()


def test_reverify_reports_a_missing_skill(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    result = vault.reverify("nothing_here", plugins)
    assert not result["ok"]
    assert "no installed skill" in result["message"].lower()


def test_registry_handler_verify_action(sandbox_env, monkeypatch):
    plugins, _reg = sandbox_env
    monkeypatch.setattr(vault, "plugins_dir", lambda: plugins)
    _install_with_report(plugins, name="verify_action_test")
    out = registry.handler({"action": "verify", "skill": "verify_action_test"})
    assert "crucible" in out.lower()


# ── discovery ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("learn how to track bitcoin prices", "track_bitcoin_prices"),
    ("create a skill that converts currencies", "converts_currencies"),
    ("forge a tool for reading my calendar", "reading_calendar"),
])
def test_learn_directives_are_parsed(text, expected):
    parsed = discovery.analyze_command(text)
    assert parsed is not None
    assert parsed["suggested_name"] == expected


def test_ordinary_sentences_are_not_directives():
    assert discovery.analyze_command("what is the weather today?") is None
    assert discovery.analyze_command("show me a picture of a cat") is None


def test_capability_gaps_are_recognised():
    assert discovery.is_capability_gap("Unknown tool: joke_teller")
    assert discovery.is_capability_gap("'x' is not an action of pc_control.")
    assert not discovery.is_capability_gap("Opening Chrome\nCompleted")
    hint = discovery.gap_hint("tell me a joke", "Unknown tool: joke_teller")
    assert "forge" in hint
    assert not discovery.gap_hint("tell me a joke", "Opening Chrome")


# ── plugin loader hot registration ────────────────────────────────────────────

def test_hot_registration_without_a_running_registry_is_reported(scratch):
    plugin_loader.set_runtime_registry(None)
    path = scratch / "plugins" / "orphan" / "plugin.py"
    path.parent.mkdir(parents=True)
    path.write_text("PLUGIN = {'name': 'orphan', 'description': 'x'}\n"
                    "def run(parameters, player=None, session_memory=None):\n"
                    "    return 'ok'\n", encoding="utf-8")
    ok, message = plugin_loader.register_runtime_plugin(path, scratch / "plugins")
    assert not ok
    assert "not running" in message
