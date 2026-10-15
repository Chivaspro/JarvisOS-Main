"""Local PC / web control tests (Playwright · pywinauto · UI-TARS · PyAutoGUI).

What must be true for this layer to be honest, and is asserted here:

* the action vocabulary the model sees is exactly what the router validates, and
  every declared action really has a handler;
* every permission is a real Settings switch, and a revoked one refuses the
  action *by name* without substituting another controller;
* the approval policy applies the same way it does to every other controller,
  including the consequential-verb scan over the request's own words;
* UI-TARS runs on the Gemini key — there is no UI-TARS credential anywhere —
  and its reply is parsed into data, never executed as code;
* coordinates are converted explicitly (monitor origin + DPI scale);
* a missing engine is reported, never faked.

Nothing in this file opens a browser window, moves the real pointer or touches
the network: controllers are monkeypatched, and the one subprocess check only
imports the layer to prove it stays lazy.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from capabilities import actions, browser_pw, desktop, registry, router  # noqa: E402
from capabilities import uitars                                          # noqa: E402
from security.permissions import permission_refusal                      # noqa: E402
from security.rules import RiskTier, classify                            # noqa: E402


# ── the registry is the single source of truth ───────────────────────────────

def test_every_declared_action_has_a_real_handler():
    """A declared action without a handler would be a silent dead end."""
    assert set(registry.names("pc_control")) <= set(desktop._HANDLERS)
    assert set(registry.names("browser_action")) <= set(browser_pw._HANDLERS)
    assert set(registry.TOOLS) == set(actions._RUNNERS)
    # visual_control is served by the grounding layer; its pointer actions and
    # its text/key actions must all be mapped to something executable.
    for name in ("click", "left_double", "right_single", "hover"):
        assert name in uitars._POINTER_ACTIONS
    for name in ("type", "hotkey", "press", "scroll", "drag", "wait", "finished"):
        assert name in _visual_execute_branches()


def _visual_execute_branches() -> set[str]:
    text = (ROOT / "capabilities" / "uitars.py").read_text(encoding="utf-8")
    found = set()
    for name in ("type", "hotkey", "press", "keydown", "scroll", "drag",
                 "select", "wait", "finished"):
        if f'"{name}"' in text:
            found.add(name)
    return found


def test_action_permissions_are_real_switches_with_human_labels():
    """A typo in a permission name must not create a phantom switch, and every
    refusal must be able to name the setting it belongs to."""
    from config.settings import FEATURE_DEFAULTS
    from security.permissions import _PERMISSION_LABEL
    for action in registry.ACTIONS:
        assert action.permission in _PERMISSION_LABEL, action.name
    for action in registry.ACTIONS:
        if action.tool in ("visual_control", "browser_action"):
            assert action.permission in FEATURE_DEFAULTS, action.name
    for switch in ("playwright_browser", "windows_automation", "uitars_visual",
                   "physical_input"):
        assert FEATURE_DEFAULTS.get(switch) is True
    assert set(registry.TOOLS) == {a.tool for a in registry.ACTIONS}
    assert len(registry.ACTIONS) == len({(a.tool, a.name) for a in registry.ACTIONS})


def test_tool_schemas_carry_the_action_enum_and_no_code_parameter():
    for tool in registry.TOOLS:
        params = registry.parameters(tool)
        assert params["required"] == ["action"]
        enum = params["properties"]["action"]["enum"]
        assert enum == registry.names(tool)
        for banned in ("code", "python", "script", "command_line"):
            assert banned not in params["properties"]


def test_no_action_is_declared_as_its_own_top_level_tool_in_main():
    """Grouped tools only — the enum carries the vocabulary."""
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    for action in registry.names():
        assert f'"name": "{action}"' not in text, action


# ── no separate UI-TARS credential, anywhere ─────────────────────────────────

def test_no_ui_tars_credential_path_exists():
    """No UI-TARS key exists anywhere in the control layer.

    config/settings.py is the one exception: it keeps the historic names in its
    log-redaction set so an old config file is still scrubbed.
    """
    forbidden = ("UI_TARS_API_KEY", "ARK_API_KEY", "TARS_API_KEY",
                 "ui_tars_api_key", "ui_tars_key", "agent_tars_uses_ui_tars")
    offenders = []
    for root in (ROOT / "capabilities", ROOT / "config", ROOT / "security"):
        for path in root.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if rel == "config/settings.py":
                text = text.replace('"ui_tars_api_key", "ui_tars_key",', "")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{rel}: {needle}")
    assert offenders == []


def test_settings_ui_has_no_ui_tars_key_field():
    text = (ROOT / "ui_settings.py").read_text(encoding="utf-8")
    assert "ui_tars_api_key" not in text
    assert "ui_tars_key_edit" not in text
    for switch in ("playwright_browser", "windows_automation", "uitars_visual",
                   "physical_input"):
        assert switch in text


# ── laziness: importing the layer must not touch the devices ────────────────

def test_importing_the_control_layer_stays_lazy():
    code = ("import sys; import capabilities.actions, capabilities.router, "
            "capabilities.registry; "
            "print('pyautogui' in sys.modules, 'pywinauto' in sys.modules, "
            "'playwright' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "False False False"


# ── routing, policy and honest failure ───────────────────────────────────────

def test_router_reports_the_controller_that_ran():
    import time
    result = router._locked_run(
        "browser_action", "playwright",
        lambda: {"ok": True, "controller": "playwright",
                 "output": "Clicked the element matched by role-link",
                 "strategy": "role-link", "matches": 1},
        0.0, time.monotonic())
    text = router.finalize_local(result)
    assert "controller=playwright" in text
    assert "status=DONE" in text
    assert "evidence:" in text and "strategy=role-link" in text


def test_permission_switch_refuses_by_name_and_does_not_substitute(monkeypatch):
    called = []
    monkeypatch.setattr(desktop, "run", lambda *a, **kw: called.append(a))
    off = lambda name: name != "physical_input"      # noqa: E731
    out = actions.handle_pc_control({"action": "mouse_click", "x": 5, "y": 5},
                                    enabled=off)
    assert "Mouse and keyboard control" in out
    assert "switched off in Settings" in out
    assert called == []


@pytest.mark.parametrize("switch,action,args", [
    ("windows_automation", "focus_window", {"title": "Notepad"}),
    ("physical_input", "mouse_click", {"x": 1, "y": 1}),
    ("screen_understanding", "screen_screenshot", {}),
])
def test_pc_control_actions_name_their_own_switch(switch, action, args):
    refusal = permission_refusal(lambda name: name != switch, "pc_control",
                                 {"action": action, **args})
    assert refusal and "switched off in Settings" in refusal


def test_browser_and_visual_switches_are_separate():
    assert "Playwright" in (permission_refusal(
        lambda n: n != "playwright_browser", "browser_action",
        {"action": "browser_click"}) or "")
    assert "UI-TARS" in (permission_refusal(
        lambda n: n != "uitars_visual", "visual_control",
        {"action": "visual_click"}) or "")


def test_approval_policy_runs_over_the_requests_own_words(monkeypatch):
    """A click on a \"Submit\" control is judged like any other submission."""
    seen = {}

    def fake_evaluate(action, params, enabled=None, permission=""):
        seen["action"] = action
        return {"allowed": False, "needs_ui_token": False,
                "refusal": "That needs approval."}
    from security import approvals
    monkeypatch.setattr(approvals, "evaluate", fake_evaluate)

    called = []
    result = router.route_local(
        "browser_action", registry.get("browser_action", "browser_click"),
        {"action": "browser_click", "target": "Submit order"},
        lambda: called.append(1) or {"ok": True}, enabled=lambda n: True)
    assert result.ok is False
    assert called == []
    # The scan saw the element name, so this was judged as a submission.
    assert seen["action"] == "browser_action_submit"


def test_irreversible_verbs_are_high_risk_for_the_local_controllers():
    assert classify("browser_action_send", {}) is RiskTier.HIGH
    assert classify("visual_control_delete", {}) is RiskTier.HIGH
    assert classify("pc_control", {}) is RiskTier.MEDIUM
    assert classify("browser_action", {}) is RiskTier.MEDIUM
    from security.approvals import requires_ui_token
    assert requires_ui_token("browser_action_delete", {"task": "delete it"})
    assert not requires_ui_token("pc_control_click", {"target": "Save"})


def test_validation_refuses_unknown_actions_and_missing_parameters():
    out = actions.handle_pc_control({"action": "teleport"})
    assert "not an action of pc_control" in out
    out = actions.handle_pc_control({"action": "mouse_move"})
    assert "needs 'x', 'y'" in out
    out = actions.handle_pc_control({})
    assert "needs an 'action'" in out
    out = actions.handle_browser_action({"action": "browser_open"})
    assert "needs 'url'" in out


def test_missing_local_engines_are_reported_not_substituted(monkeypatch):
    monkeypatch.setattr(desktop, "status",
                        lambda: {"available": False, "missing": ["pyautogui"],
                                 "detail": "missing: pyautogui"})
    monkeypatch.setattr(desktop, "run",
                        lambda *a, **kw: pytest.fail("the engine must not run"))
    out = actions.handle_pc_control({"action": "mouse_click", "x": 1, "y": 1},
                                    enabled=lambda n: True)
    assert "unavailable" in out and "Nothing else was substituted" in out
    assert "status=NOT_DONE" in out

    monkeypatch.setattr(browser_pw, "installed", lambda: False)
    out = actions.handle_browser_action({"action": "browser_open",
                                         "url": "https://example.com"},
                                        enabled=lambda n: True)
    assert "Playwright" in out and "not installed" in out
    assert "status=NOT_DONE" in out


def test_line_of_sight_to_the_visual_layer_is_gemini_only(monkeypatch):
    monkeypatch.setattr(uitars, "status", lambda: {
        "sdk": True, "available": False, "detail": "no Gemini API key is configured"})
    out = actions.handle_visual_control({"action": "visual_click",
                                         "instruction": "the Save button"},
                                        enabled=lambda n: True)
    assert "Gemini" in out
    assert "no separate UI-TARS credential" in out
    assert "status=NOT_DONE" in out


# ── UI-TARS: the SDK format on the Gemini model ─────────────────────────────

def test_prompt_uses_the_vendored_ui_tars_template():
    prompt = uitars._prompt("click the Save button")
    assert "GUI agent" in prompt          # COMPUTER_USE_DOUBAO's own opening
    assert "click the Save button" in prompt
    assert "{instruction}" not in prompt and "{language}" not in prompt
    assert "Action:" in prompt


def test_normalised_points_become_screen_pixels():
    # UI-TARS writes 0-1000 coordinates; the parser turns them into fractions.
    assert uitars._point({"start_box": "[0.5, 0.25, 0.5, 0.25]"}, 1000, 800) == (500, 200)
    assert uitars._point({"start_box": "[1.0, 1.0, 1.0, 1.0]"}, 1000, 800) == (999, 799)
    # An absolute pair is taken as pixels.
    assert uitars._point({"start_box": "(300, 400)"}, 1000, 800) == (300, 400)
    assert uitars._point({}, 1000, 800) is None


def test_execute_maps_ui_tars_actions_to_the_one_audited_executor(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "run_physical",
                        lambda name, args: calls.append((name, args)) or
                        {"ok": True, "controller": "pyautogui", "detail": "did it",
                         "evidence": {}})

    info = {"width": 1000, "height": 1000}
    uitars.execute({"action_type": "click", "action_inputs": {},
                    "point": (10, 20)}, info)
    uitars.execute({"action_type": "left_double", "action_inputs": {},
                    "point": (30, 40)}, info)
    uitars.execute({"action_type": "right_single", "action_inputs": {},
                    "point": (50, 60)}, info)
    uitars.execute({"action_type": "type",
                    "action_inputs": {"content": "hello\\n"}, "point": None}, info)
    uitars.execute({"action_type": "hotkey", "action_inputs": {"key": "ctrl s"},
                    "point": None}, info)
    uitars.execute({"action_type": "press", "action_inputs": {"key": "enter"},
                    "point": None}, info)
    uitars.execute({"action_type": "scroll",
                    "action_inputs": {"direction": "down"}, "point": (70, 80)}, info)
    uitars.execute({"action_type": "drag", "action_inputs": {"end_box": "[0.9, 0.9]"},
                    "point": (11, 12)}, info)

    assert [name for name, _ in calls] == [
        "mouse_click", "mouse_double_click", "mouse_right_click",
        "keyboard_type", "keyboard_hotkey", "keyboard_press",
        "mouse_scroll", "mouse_drag"]
    assert calls[0][1] == {"x": 10, "y": 20}
    assert calls[3][1] == {"text": "hello"}          # the escape is undone
    assert calls[6][1] == {"direction": "down", "amount": 3, "x": 70, "y": 80}
    assert calls[7][1]["to_x"] == 900 and calls[7][1]["to_y"] == 900


def test_a_click_is_never_issued_without_a_grounded_position(monkeypatch):
    monkeypatch.setattr(desktop, "run_physical",
                        lambda *a, **kw: pytest.fail("must not click"))
    out = uitars.execute({"action_type": "click", "action_inputs": {},
                          "point": None}, {"width": 100, "height": 100})
    assert out["ok"] is False
    assert "no usable position" in out["detail"]

    out = uitars.execute({"action_type": "teleport", "action_inputs": {},
                          "point": (1, 1)}, {"width": 100, "height": 100})
    assert out["ok"] is False and "teleport" in out["detail"]


def test_an_unreadable_reply_is_reported_and_nothing_is_clicked(monkeypatch):
    monkeypatch.setattr(uitars, "_call_model", lambda prompt, png: "I cannot help.")
    monkeypatch.setattr(desktop, "run_physical",
                        lambda *a, **kw: pytest.fail("must not click"))
    step = uitars.next_action("click Save", png=b"png",
                              info={"width": 100, "height": 100})
    assert step["ok"] is False
    assert step["evidence"]["reason"] == "unparsed_action"
    assert "Nothing was clicked" in step["detail"]


def test_the_visual_runner_grounds_on_gemini_then_executes(monkeypatch):
    monkeypatch.setattr(uitars, "status", lambda: {
        "sdk": True, "available": True, "missing": [],
        "detail": "ready", "model": "gemini-flash-latest"})
    monkeypatch.setattr(desktop, "grab_png",
                        lambda: (b"png", {"width": 100, "height": 100,
                                          "scale_x": 1.0, "scale_y": 1.0}))
    monkeypatch.setattr(uitars, "next_action", lambda *a, **kw: {
        "ok": True, "controller": "uitars", "action_type": "click",
        "action_inputs": {}, "point": (11, 22), "model": "gemini-flash-latest",
        "thought": "the Save button is here", "evidence": {"point": (11, 22)}})
    executed = []

    def fake_execute(step, info):
        executed.append(step["point"])
        return {"ok": True, "controller": "pyautogui",
                "detail": "Clicked at (11, 22)", "evidence": {"pointer": (11, 22)}}
    monkeypatch.setattr(uitars, "execute", fake_execute)

    out = actions.handle_visual_control({"action": "visual_click",
                                         "instruction": "the Save button"},
                                        enabled=lambda n: True)
    assert executed == [(11, 22)]
    assert "backend=uitars" in out and "controller=pyautogui" in out
    assert "status=DONE" in out


def test_visual_find_reports_a_position_without_clicking(monkeypatch):
    monkeypatch.setattr(uitars, "status", lambda: {
        "sdk": True, "available": True, "missing": [], "detail": "ready"})
    monkeypatch.setattr(desktop, "grab_png",
                        lambda: (b"png", {"width": 200, "height": 100}))
    monkeypatch.setattr(uitars, "next_action", lambda *a, **kw: {
        "ok": True, "action_type": "click", "action_inputs": {}, "point": (5, 6),
        "model": "gemini-flash-latest", "thought": "", "evidence": {}})
    monkeypatch.setattr(uitars, "execute",
                        lambda *a, **kw: pytest.fail("find must not click"))
    out = actions.handle_visual_control({"action": "visual_find",
                                         "instruction": "the download button"},
                                        enabled=lambda n: True)
    assert "Found it at (5, 6)" in out
    assert "Nothing was clicked" in out
    assert "status=DONE" in out


# ── controller internals worth pinning down ─────────────────────────────────

def test_semantic_selectors_come_before_css_and_never_coordinates():
    text = (ROOT / "capabilities" / "browser_pw.py").read_text(encoding="utf-8")
    assert "page.mouse.click" not in text
    assert "role=" in text and "testid=" in text
    order = ["label", "placeholder", "testid", "role-name", "text-exact",
             "text", "css-name"]
    source = text[text.index("def _candidates"):text.index("def _explicit")]
    positions = [source.index(f'"{name}"') for name in order]
    assert positions == sorted(positions)


def test_coordinate_mapping_applies_monitor_origin_and_dpi_scale():
    assert desktop.to_pointer(100, 50, {"left": 0, "top": 0,
                                        "scale_x": 1.0, "scale_y": 1.0}) == (100, 50)
    assert desktop.to_pointer(100, 50, {"left": 1920, "top": 0,
                                        "scale_x": 2.0, "scale_y": 1.0}) == (1010, 50)
    assert desktop.to_pointer(10, 20, {}) == (10, 20)


def test_application_aliases_resolve_to_real_executables():
    assert desktop.APP_ALIASES["notepad"] == "notepad.exe"
    assert desktop.resolve_executable("notepad")
    assert desktop.resolve_executable("definitely-not-an-app-xyz") == ""


def test_a_missing_input_backend_is_a_clean_failure(monkeypatch):
    def boom():
        raise RuntimeError("pyautogui is not available: no display")
    monkeypatch.setattr(desktop, "_pyautogui", boom)
    result = desktop.run("mouse_click", {"x": 1, "y": 1})
    assert result["ok"] is False
    assert "not available" in result["detail"]
    result = desktop.run("keyboard_type", {"text": "hi"})
    assert result["ok"] is False


def test_activity_rows_use_the_real_words_of_the_request():
    assert registry.activity_detail("pc_control",
                                    {"action": "open_application",
                                     "app": "chrome"}) == "Opening chrome"
    assert registry.activity_detail("browser_action",
                                    {"action": "browser_fill",
                                     "target": "Email", "text": "x"}) == "Filling Email"
    assert registry.activity_detail("visual_control",
                                    {"action": "visual_scroll",
                                     "direction": "down"}) == "Scrolling down"
    assert registry.activity_detail("pc_control", {}) == ""


def test_main_maps_the_local_tools_onto_automate_activity():
    text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "elif name in (\"pc_control\", \"visual_control\", \"browser_action\"," in text
    start = text.index("if tool_name in ('pc_control'")
    end = text.index("if tool_name == 'web_research'", start)
    block = text[start:end]
    assert "return 'AUTOMATE'" in block
    assert "activity_detail" in block
    for tool in registry.TOOLS:
        assert f'"{tool}": ' in text or f"'{tool}': " in text


def test_health_and_diagnostics_expose_every_controller():
    from capabilities import health
    from observability import diagnostics
    checks = {c.name: c for c in health.local_controller_checks()}
    assert len(checks) == 4
    for name in ("pc_control (pywinauto + PyAutoGUI)",
                 "browser_action (Playwright)",
                 "visual_control (UI-TARS on Gemini)",
                 "power_control (Windows system power)"):
        assert name in checks
        assert checks[name].state in ("ONLINE", "DEGRADED", "OFFLINE", "ERROR")
    for key in ("pc_control", "browser_action", "visual_control", "power_control"):
        assert key in diagnostics.CHECKS


def test_router_report_names_each_controller_and_its_chain():
    text = registry.report()
    for name in ("pc_control", "visual_control", "browser_action"):
        assert name in text
    assert "pywinauto -> pyautogui" in text or "pywinauto" in text
    assert "uitars" in text and "playwright" in text
    report = router.report()
    assert "pc_control" in report and "browser_action" in report
    assert "visual_control" in report and "ownership" in report
