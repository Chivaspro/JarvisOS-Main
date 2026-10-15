"""Capability-layer tests (Agent TARS / Agent Reach integration).

Proves the parts that must be true for the unified router to be honest:
only one engine owns a control domain, a missing requirement is reported as a
missing requirement, an unavailable backend is never substituted, the permission
switches and risk tiers cover the tools, the skill registry routes the spec's own
example phrasings, and the process protocol parses real output.

Nothing here needs the network. Tests that spawn a real engine are skipped when
that engine is not installed.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from capabilities import config as cap_config          # noqa: E402
from capabilities import locks, proc, router           # noqa: E402
from capabilities.locks import ControlBusy, ControlLock  # noqa: E402
from security.approvals import requires_ui_token       # noqa: E402
from security.permissions import permission_refusal    # noqa: E402
from security.rules import RiskTier, classify          # noqa: E402


# ── ownership locks ──────────────────────────────────────────────────────────

def test_lock_refuses_concurrent_acquisition(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "_runtime_dir", lambda: tmp_path)
    lock = ControlLock("desktop")
    lock.acquire(owner="agent_tars")
    try:
        with pytest.raises(ControlBusy):
            ControlLock("desktop").acquire(owner="agent_tars", timeout_s=0.2)
    finally:
        lock.release()


def test_stale_lock_is_broken(tmp_path, monkeypatch):
    import os
    import time
    monkeypatch.setattr(locks, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(locks, "STALE_SECONDS", -1.0)
    stale = tmp_path / "browser.lock"
    stale.write_text(json.dumps({"owner": "dead", "pid": 999999, "at": 0}),
                     encoding="utf-8")
    os.utime(stale, (0, 0))
    lock = ControlLock("browser")
    lock.acquire(owner="agent_tars", timeout_s=0.2)   # must not raise
    assert lock.is_locked()
    lock.release()


def test_router_reports_lock_busy_instead_of_double_control(monkeypatch):
    """Agent TARS holding the desktop must stop a second desktop task."""
    holder = locks.get_lock("desktop")
    holder.acquire(owner="other-task")
    try:
        monkeypatch.setattr(cap_config, "enabled", lambda engine: True)
        result = router.route_computer_control("Open Notepad")
        assert result.ok is False
        assert result.reason == "lock_busy"
    finally:
        holder.release()


def test_desktop_and_browser_locks_are_independent(monkeypatch):
    """A browser workflow may proceed while desktop control is held — and the
    other way round — because they own different targets."""
    desktop = locks.get_lock("desktop")
    desktop.acquire(owner="agent_tars")
    try:
        assert not locks.get_lock("browser").is_locked()
        assert locks.get_lock("desktop").is_locked()
    finally:
        desktop.release()


# ── Agent TARS runtime discovery and honest refusals ─────────────────────────

def test_runtime_reports_every_missing_piece(monkeypatch):
    from capabilities import agent_tars
    monkeypatch.setattr(cap_config, "agent_tars_node", lambda: "")
    monkeypatch.setattr(cap_config, "agent_tars_cli", lambda: "")
    r = agent_tars.runtime()
    assert r["node"] == "" and r["cli"] == ""
    assert len(r["missing"]) == 2
    assert any("Node" in m for m in r["missing"])
    assert any("npm install" in m for m in r["missing"])


def test_run_task_refuses_when_unavailable_without_substitute(monkeypatch):
    """An uninstalled/off engine is reported, never replaced, and never run."""
    from capabilities import agent_tars, tars_server
    monkeypatch.setattr(cap_config, "agent_tars_node", lambda: "")
    monkeypatch.setattr(cap_config, "agent_tars_cli", lambda: "")
    monkeypatch.setattr(cap_config, "enabled", lambda engine: True)
    called = {"ran": False}
    monkeypatch.setattr(tars_server, "submit", lambda *a, **k: called.update(ran=True))

    result = agent_tars.run_task("Open Notepad", mode="computer")
    assert result["ok"] is False
    assert result["reason"] == "unavailable"
    assert result["backend"] == "agent_tars"
    assert "no other control engine was substituted" in result["output"].lower()
    assert called["ran"] is False


def test_router_refuses_unavailable_tars_with_honest_reason(monkeypatch):
    from capabilities import agent_tars
    monkeypatch.setattr(cap_config, "enabled", lambda engine: True)
    monkeypatch.setattr(agent_tars, "available", lambda: False)
    result = router.route_computer_control("Open Notepad and type hello")
    assert result.ok is False
    assert result.reason == "unavailable"
    assert result.backend == "agent_tars"
    assert "substituted" in result.output.lower()


def test_missing_gemini_key_is_reported_for_tars(monkeypatch):
    """Agent TARS runs on Gemini; without a key it must refuse, not fake it."""
    from capabilities import agent_tars
    monkeypatch.setattr(cap_config, "enabled", lambda engine: True)
    monkeypatch.setattr(agent_tars, "available", lambda: True)
    monkeypatch.setattr(cap_config, "gemini_api_key", lambda: "")
    result = agent_tars.run_task("Open https://example.com")
    assert result["ok"] is False
    assert result["reason"] == "no_gemini_key"


def test_ui_tars_has_no_credential_of_its_own(monkeypatch):
    """One model source only: the Gemini key. No UI-TARS key path exists."""
    monkeypatch.setattr(cap_config, "agent_tars_provider", lambda: "gemini")
    monkeypatch.setattr(cap_config, "gemini_api_key", lambda: "gm-key")
    assert cap_config.agent_tars_provider_supported() is True
    assert cap_config.agent_tars_credential() == ("GEMINI_API_KEY", "gm-key")
    assert not hasattr(cap_config, "ui_tars_api_key")
    assert not hasattr(cap_config, "agent_tars_uses_ui_tars")


def test_a_non_gemini_provider_is_reported_as_unsupported(monkeypatch):
    """Pointing TARS at another provider is refused by name — never by silently
    borrowing a different credential."""
    from capabilities import agent_tars
    monkeypatch.setattr(cap_config, "enabled", lambda engine: True)
    monkeypatch.setattr(agent_tars, "available", lambda: True)
    monkeypatch.setattr(cap_config, "agent_tars_provider", lambda: "volcengine")
    monkeypatch.setattr(cap_config, "gemini_api_key", lambda: "gm-key")
    assert cap_config.agent_tars_provider_supported() is False
    result = agent_tars.run_task("Open https://example.com")
    assert result["ok"] is False
    assert result["reason"] == "provider_unsupported"
    assert "Gemini" in result["output"]


def test_tars_server_argv_never_carries_a_second_key(monkeypatch, tmp_path):
    """The Gemini key travels in the child environment; no --model.apiKey."""
    from capabilities import tars_server
    monkeypatch.setattr(cap_config, "agent_tars_workspace", lambda: tmp_path)
    monkeypatch.setattr(cap_config, "gemini_api_key", lambda: "gm-key")
    argv = tars_server._argv("node", "cli.js", 5999, "gemini-flash-latest")
    assert "--model.apiKey" not in argv
    assert "--model.provider" in argv
    assert argv[argv.index("--model.provider") + 1] == cap_config.agent_tars_provider()


def test_oneshot_parser_reads_the_pretty_printed_cli_json():
    """The CLI pretty-prints its result; the parser must still find it."""
    from capabilities import agent_tars
    blob = (
        "some banner\n"
        "{\n"
        '  "sessionId": "abc",\n'
        '  "result": {"type": "assistant_message", "content": "Example Domain",\n'
        '             "finishReason": "stop"}\n'
        "}\n"
    )
    data = agent_tars._extract_cli_json(blob)
    assert isinstance(data, dict)
    assert data["sessionId"] == "abc"
    assert data["result"]["content"] == "Example Domain"


def test_server_event_descriptions_are_human_and_real():
    """Tool events become plain activity lines — no simulated progress."""
    from capabilities import tars_server
    assert tars_server._describe_tool("browser_navigate",
                                      {"url": "https://youtube.com"}) == \
        "Opening https://youtube.com"
    assert "Reading" in tars_server._describe_tool("browser_get_markdown", {})
    assert tars_server._describe_tool("run_command", {"command": "notepad.exe"}).startswith("Running")
    assert tars_server._describe_tool("", {}) == "Working"


def test_transient_provider_errors_are_recognised():
    from capabilities import tars_server
    assert tars_server._transient("Error 503 high demand")
    assert tars_server._transient("429 rate limit exceeded")
    assert not tars_server._transient("Tool failed: element not found")


# ── approvals and risk tiers cover the tools ─────────────────────────────────

@pytest.mark.parametrize("action,expected", [
    ("web_research", RiskTier.LOW),
    ("computer_control", RiskTier.MEDIUM),
    ("browser_control", RiskTier.MEDIUM),
    ("browser_control_send", RiskTier.HIGH),
    ("browser_control_publish", RiskTier.HIGH),
    ("computer_control_delete", RiskTier.HIGH),
    ("browser_control_buy", RiskTier.HIGH),
])
def test_capability_risk_tiers(action, expected):
    assert classify(action, {}) is expected


def test_irreversible_capability_verbs_always_need_the_ui_token():
    assert requires_ui_token("computer_control_delete", {"task": "delete the folder"})
    assert requires_ui_token("computer_control_uninstall", {})
    assert not requires_ui_token("computer_control_open", {"task": "open notepad"})
    assert not requires_ui_token("browser_control", {"task": "read the page"})


@pytest.mark.parametrize("tool,feature", [
    ("computer_control", "desktop_control"),
    ("browser_control", "web_task"),
    ("web_research", "webview"),
])
def test_each_capability_has_its_own_permission_switch(tool, feature):
    on = lambda name: True       # noqa: E731
    off = lambda name: name != feature   # noqa: E731
    assert permission_refusal(on, tool, {}) is None
    refusal = permission_refusal(off, tool, {})
    assert refusal and "switched off in Settings" in refusal


def test_router_refuses_when_engine_switch_is_off(monkeypatch):
    monkeypatch.setattr(cap_config, "enabled", lambda engine: False)
    result = router.route_browser_control("Open example.com", url="https://example.com")
    assert result.ok is False and result.reason == "disabled"
    assert "switched off in Settings" in result.output
    assert "No other engine was substituted" in result.output


def test_missing_input_never_reaches_an_engine(monkeypatch):
    monkeypatch.setattr(cap_config, "enabled", lambda engine: True)
    assert router.handle_computer_control({}).startswith("computer_control needs a task")
    assert router.handle_browser_control({}).startswith("browser_control needs a task")
    assert router.handle_web_research({}).startswith("web_research needs a query")


# ── configuration comes from settings, never hardcoded ───────────────────────

def test_environment_auto_discovery_for_agent_reach():
    """The installed layout is found without any path being written down."""
    if not (ROOT / "AgentReach" / ".venv").exists():
        pytest.skip("AgentReach/.venv is not installed on this machine")
    root = cap_config.env_root("agent_reach")
    assert root is not None
    exe = Path(cap_config.interpreter("agent_reach"))
    assert exe.exists()


def test_override_env_var_wins(monkeypatch, tmp_path):
    fake = tmp_path / "Scripts" / "python.exe"
    fake.parent.mkdir()
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("JARVIS_AGENTREACH_ENV", str(tmp_path))
    assert cap_config.env_root("agent_reach") == tmp_path


def test_agent_tars_cli_override_wins(monkeypatch, tmp_path):
    fake = tmp_path / "cli.js"
    fake.write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(cap_config, "_read_config",
                        lambda key, default=None: str(fake) if key == "agent_tars_cli"
                        else default)
    assert cap_config.agent_tars_cli() == str(fake)


def test_no_secret_is_hardcoded_anywhere_in_the_layer():
    offenders = []
    for path in sorted((ROOT / "capabilities").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("AIza", "sk-", "Bearer "):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []


def test_tars_defaults_are_the_verified_gemini_settings():
    assert cap_config.agent_tars_provider() == "gemini"
    # prompt_engineering is required because the native Gemini function-calling
    # path is rejected by the generativelanguage API.
    assert cap_config.agent_tars_tool_call_engine() == "prompt_engineering"
    assert cap_config.agent_tars_mode() in ("server", "oneshot")


# ── process protocol ─────────────────────────────────────────────────────────

def test_worker_protocol_reads_the_last_json_line():
    stdout = "noise line\nINFO something\n" + json.dumps({"ok": True, "summary": "done"})
    assert proc._parse(stdout) == {"ok": True, "summary": "done"}


def test_worker_protocol_survives_unreadable_output():
    assert proc._parse("traceback, no json here") is None


def test_run_worker_reports_missing_environment_without_raising(monkeypatch):
    monkeypatch.setattr(cap_config, "interpreter", lambda engine: None)
    result = proc.run_worker("agent_reach", {"action": "status"})
    assert result["ok"] is False
    assert result["error_code"] == "environment_missing"


def test_spawn_argv_keeps_stdout_when_the_child_prints_plain_text():
    """Agent TARS `--version` is plain text; the raw output must survive."""
    result = proc.spawn_argv([sys.executable, "-c", "print('agent-tars/0.3.0')"],
                             engine="agent_tars", timeout_s=40.0)
    assert result.get("ok") is False          # not JSON -> reported honestly
    assert "agent-tars/0.3.0" in (result.get("log") or "")


def test_register_shutdown_hook_is_idempotent():
    proc._hook_installed = False
    try:
        proc.register_shutdown_hook()
        proc.register_shutdown_hook()
        assert proc._hook_installed is True
    finally:
        proc._hook_installed = False


# ── the installed engines are really invoked ─────────────────────────────────

def test_agent_reach_worker_runs_inside_its_environment():
    interpreter = cap_config.interpreter("agent_reach")
    if not interpreter:
        pytest.skip("AgentReach/.venv is not installed on this machine")
    script = cap_config.worker("agent_reach")
    assert script.exists()
    proc_result = subprocess.run(
        [interpreter, str(script)],
        input=json.dumps({"action": "status"}), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=240, cwd=str(script.parent),
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}),
    )
    payload = proc._parse(proc_result.stdout or "")
    assert payload is not None, proc_result.stderr[-500:]
    assert payload["engine"] == "agent_reach"
    data = payload.get("data") or {}
    assert payload["engine_version"]
    assert isinstance(data.get("channels"), dict) and data["channels"]


def test_agent_tars_cli_answers_the_version_probe():
    """The pinned runtime is really installed and really answers."""
    if not (cap_config.agent_tars_node() and cap_config.agent_tars_cli()):
        pytest.skip("Agent TARS runtime is not installed on this machine")
    from capabilities import agent_tars
    ver = agent_tars.version(force=True)
    assert ver and ver[0].isdigit(), f"unexpected version string: {ver!r}"


# ── the brain sees the capabilities, routed automatically ────────────────────

def _declared_tool_names() -> set:
    """Tool names in main.py's TOOL_DECLARATIONS literal + its += blocks.
    Read from the AST so the test never imports the whole application."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    names = set()

    def collect(list_node):
        for element in getattr(list_node, "elts", []):
            if not isinstance(element, ast.Dict):
                continue
            for key, value in zip(element.keys, element.values):
                if isinstance(key, ast.Constant) and key.value == "name":
                    if isinstance(value, ast.Constant):
                        names.add(value.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "TOOL_DECLARATIONS" for t in node.targets):
            collect(node.value)
        elif isinstance(node, ast.AugAssign) and getattr(node.target, "id", "") == "TOOL_DECLARATIONS":
            collect(node.value)
    return names


def test_main_declares_the_capabilities_and_nothing_else_acts_as_controller():
    names = _declared_tool_names()
    assert {"computer_control", "browser_control", "web_research"} <= names
    # The local controllers are declared as three grouped tools, one per
    # family — never as one tool per action.
    assert {"pc_control", "visual_control", "browser_action"} <= names
    for retired in ("desktop_control", "open_app", "computer_settings",
                    "computer_use", "computer_control_pro", "cua",
                    "browser_control_legacy", "mouse_click", "browser_click",
                    "visual_click", "keyboard_type"):
        assert retired not in names


@pytest.mark.parametrize("phrase,expected_skill,expected_tool", [
    ("Open Notepad and type X", "desktop_control", "computer_control"),
    ("Click the Settings button", "desktop_control", "computer_control"),
    ("Look at my screen and click the correct button", "desktop_control",
     "computer_control"),
    ("Open Gmail and find my latest email", "browser_automation", "browser_control"),
    ("Fill out this website form", "browser_automation", "browser_control"),
    ("Search GitHub for open-source computer use projects", "internet_research",
     "web_research"),
    ("Find information from YouTube about this release", "internet_research",
     "web_research"),
])
def test_skill_router_selects_the_capability_without_the_user_naming_an_engine(
        phrase, expected_skill, expected_tool):
    from brain import skills
    selected = skills.route(phrase)
    keys = [s.key for s in selected]
    assert expected_skill in keys, f"{phrase!r} routed to {keys}"
    skill = next(s for s in selected if s.key == expected_skill)
    assert expected_tool in skill.tools


def test_research_can_chain_into_an_action_engine():
    from brain import skills
    selected = {s.key for s in skills.route(
        "Research this software on GitHub and then install it")}
    assert "internet_research" in selected
    chains = skills.chains_for(["internet_research"])
    flat = {c for chain in chains for c in (chain if isinstance(chain, (list, tuple, set))
                                            else [chain])}
    assert flat & {"browser_automation", "desktop_control"}


def test_no_retired_adapter_or_worker_source_survives():
    """§13/§24: the removed PC-control backends left no source behind."""
    for rel in ("capabilities/agent_s.py", "capabilities/browser_use.py",
                "capabilities/workers/agent_s_worker.py",
                "capabilities/workers/browser_use_worker.py"):
        assert not (ROOT / rel).exists(), f"{rel} should have been removed"
    assert (ROOT / "capabilities" / "agent_tars.py").exists()
    assert (ROOT / "capabilities" / "tars_server.py").exists()


def test_retired_pc_control_modules_are_not_importable():
    for module in ("computer.computer_control", "computer.browser_control",
                   "computer.cua_backend", "computer.desktop_ops",
                   "computer.local_backend", "computer.existing_browser"):
        with pytest.raises(ImportError):
            __import__(module)


def test_no_forbidden_framework_was_added():
    needles = ("omniparser", "omnitool", "skyvern", "open-interpreter",
               "open_interpreter", "cua-", "cua_")
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for needle in needles:
        assert needle not in text, f"requirements.txt gained {needle}"


def test_physical_input_is_imported_in_exactly_one_place():
    """The local controllers live behind capabilities/desktop.py only.

    pyautogui and pywinauto touch the user's real machine, so exactly one
    audited module may import them: every PC/visual action goes through it, and
    there is one place to read (and one place to review) for that behaviour.
    The retired adapters stay unimportable too.
    """
    offenders = []
    allowed = "capabilities/desktop.py"
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        if {"AgentReach", "node_modules", ".venv", "venv", "UI-TARS",
                "build", "dist", ".freebuff", ".pytest_cache"} & parts:
            continue
        rel = path.relative_to(ROOT)
        if rel.as_posix().startswith("tests/") or rel.as_posix().startswith("docs/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("import pyautogui", "import pywinauto",
                       "from pyautogui", "from pywinauto",
                       "from capabilities import agent_s",
                       "from capabilities import browser_use"):
            if needle in text and rel.as_posix() != allowed:
                offenders.append(f"{rel}: {needle}")
    assert offenders == []


def test_no_controller_executes_model_text_as_code():
    """Gemini's reply is parsed into data, never run.

    Checked on the parsed syntax tree, not on the text, so a docstring may name
    the SDK's source-generating helper while the code still cannot call it. The
    UI-TARS SDK ships ``parsing_response_to_pyautogui_code`` (it emits Python
    source); nothing in the control path may import, reference or call it, and
    nothing may call exec()/eval()/compile() at all.
    """
    for rel in ("capabilities/uitars.py", "capabilities/actions.py",
                "capabilities/registry.py", "capabilities/browser_pw.py",
                "capabilities/desktop.py", "capabilities/router.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8",
                                               errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in ("exec", "eval", "compile"), f"{rel}: {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr != "parsing_response_to_pyautogui_code", rel
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert "pyautogui_code" not in alias.name, rel


def test_router_report_names_the_real_engines_and_their_state():
    text = router.report()
    assert "computer_control" in text and "Agent TARS" in text
    assert "browser_control" in text and "Agent TARS" in text
    assert "web_research" in text and "Agent Reach" in text
    assert "ownership" in text


def test_finalize_marks_an_unverified_outcome_so_gemini_cannot_claim_success():
    result = router.CapabilityResult(
        ok=False, capability="browser_control", backend="agent_tars",
        output="The page could not be read.", reason="timeout")
    text = router._finalize(result)
    assert "status=NOT_DONE" in text
    assert "never present it as" in text        # ran, but unconfirmed

    blocked = router.CapabilityResult(
        ok=False, capability="computer_control", backend="agent_tars",
        output="Node.js is not available.", reason="node_missing")
    assert "do not retry it through a different control engine" in router._finalize(blocked)


def test_tool_result_status_reads_the_capability_markers():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "status=not_done" in source        # FAILED mapping
    assert "verified=false" in source         # VERIFICATION_FAILED mapping


# ── shutdown ─────────────────────────────────────────────────────────────────

def test_owned_pids_are_tracked_and_cleared(monkeypatch):
    proc._owned.clear()
    proc._register(424242, "agent_tars:test")
    assert proc.owned_pids() == [424242]
    proc._unregister(424242)
    assert proc.owned_pids() == []
    proc._register(424243, "agent_tars:test")
    monkeypatch.setattr(proc, "descendants", lambda pid: [])
    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: called.append(a[0][0] if a else ""))
    proc.terminate_all("test")
    assert called == [] or "taskkill" in called
    assert proc.owned_pids() == []


def test_terminate_pid_ignores_unowned_pids():
    proc._owned.clear()
    assert proc.terminate_pid(999999) is False   # never touches an unknown PID


def test_router_shutdown_releases_every_lock_it_held():
    locks.get_lock("desktop").acquire(owner="agent_tars")
    locks.get_lock("research").acquire(owner="agent_reach")
    assert locks.release_all("test") >= 2
    assert not locks.get_lock("desktop").is_locked()
    assert not locks.get_lock("research").is_locked()


# ── logging ──────────────────────────────────────────────────────────────────

def test_log_lines_never_carry_secrets():
    from config.settings import redact_secrets
    payload = {"api_key": "AIzaTEST", "task": "open notepad"}
    assert redact_secrets(payload)["api_key"] == "[REDACTED]"


def test_engine_status_is_cached_and_secret_free():
    snap = cap_config.summary()
    text = json.dumps(snap)
    assert "AIza" not in text and "api_key\":\"" not in text.replace(" ", "")
    assert "agent_tars" in snap and "agent_reach" in snap
