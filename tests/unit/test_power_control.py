"""System power control: real actions, and a gate the model cannot talk past.

The complaint this answers: PC control could open windows and click buttons but
could not do the things a user actually asks a machine-level assistant for —
lock it, sleep it, restart it, change the volume — and the few dangerous paths
that existed relied on a ``confirmed=yes`` TOOL PARAMETER, which the model writes
itself and can therefore always satisfy.

These tests pin both halves:

* ``capabilities/power.py`` really issues the OS command for each action, and
  reports refusal honestly (no test may ever reach a real OS command — the
  fixture below fails the suite if one tries);
* the actions that can end or lose the user's session are declared
  ``gate="confirm"`` and ``capabilities/router.py`` parks them behind
  ``security/confirm.py``, the HUD token issued only after a human press.

Nothing here shuts anything down, sleeps anything, or launches a shell: every
OS call is monkeypatched.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capabilities import power, registry  # noqa: E402

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
SETTINGS_SOURCE = (ROOT / "ui_settings.py").read_text(encoding="utf-8")

#: Actions that end or lose the session: they must ALWAYS wait for a human press.
GATED = ("shutdown", "restart", "sign_out", "sleep", "hibernate",
         "empty_recycle_bin")

#: Actions that are instantly reversible and must NOT add a question.
UNGATED = ("lock", "cancel_shutdown", "volume_up", "volume_down", "mute",
           "set_volume", "set_brightness", "restart_explorer")


@pytest.fixture
def no_os(monkeypatch):
    """Record the commands instead of running them; fail on any real call."""
    calls: list = []

    def fake_run(argv, timeout=power.COMMAND_TIMEOUT_S):
        calls.append(("run", [str(a) for a in argv]))
        return True, ""

    def fake_powershell(script, timeout=power.COMMAND_TIMEOUT_S):
        calls.append(("powershell", str(script)))
        return True, "ok"

    monkeypatch.setattr(power, "_run", fake_run)
    monkeypatch.setattr(power, "_powershell", fake_powershell)
    monkeypatch.setattr(power, "_WIN", True)
    return calls


def _commands(calls, kind="run"):
    return [argv for recorded, argv in calls if recorded == kind]


# ── the commands each action really issues ────────────────────────────────

def test_shutdown_asks_windows_to_shut_down_now(no_os):
    result = power.run("shutdown", {})

    assert result["ok"] is True
    assert _commands(no_os) == [["shutdown.exe", "/s", "/t", "0"]]
    assert "shut" in result["detail"].lower()


def test_restart_uses_the_restart_flag(no_os):
    power.run("restart", {})

    assert _commands(no_os) == [["shutdown.exe", "/r", "/t", "0"]]


def test_sign_out_uses_the_logoff_flag(no_os):
    power.run("sign_out", {})

    assert _commands(no_os) == [["shutdown.exe", "/l"]]


def test_a_pending_shutdown_can_be_cancelled(no_os):
    result = power.run("cancel_shutdown", {})

    assert result["ok"] is True
    assert _commands(no_os) == [["shutdown.exe", "/a"]]


def test_hibernate_uses_the_hibernate_flag(no_os):
    power.run("hibernate", {})

    assert _commands(no_os) == [["shutdown.exe", "/h"]]


def test_lock_calls_the_windows_lock_function(no_os):
    result = power.run("lock", {})

    assert result["ok"] is True
    assert _commands(no_os) == [["rundll32.exe", "user32.dll,LockWorkStation"]]


def test_sleep_goes_through_the_power_api_not_rundll32(no_os):
    # rundll32 powrprof.dll hibernates on a machine with hibernation enabled,
    # which is not what "sleep" means to a user.
    power.run("sleep", {})

    scripts = _commands(no_os, "powershell")
    assert len(scripts) == 1
    assert "SetSuspendState" in scripts[0]


def test_empty_recycle_bin_uses_the_shell_command(no_os):
    result = power.run("empty_recycle_bin", {})

    assert result["ok"] is True
    assert "Clear-RecycleBin" in _commands(no_os, "powershell")[0]


def test_an_already_empty_recycle_bin_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(power, "_WIN", True)
    monkeypatch.setattr(power, "_powershell", lambda script, timeout=None: (True, "already empty"))

    result = power.run("empty_recycle_bin", {})

    assert result["ok"] is True
    assert "already empty" in result["detail"].lower()


def test_restart_explorer_kills_and_restarts_the_shell(monkeypatch):
    killed: list = []
    started: list = []
    monkeypatch.setattr(power, "_WIN", True)
    monkeypatch.setattr(power, "_run",
                        lambda argv, timeout=None: killed.append(list(argv)) or (True, ""))
    monkeypatch.setattr(power.subprocess, "Popen",
                        lambda argv, **kw: started.append(list(argv)))

    result = power.run("restart_explorer", {})

    assert result["ok"] is True
    assert killed == [["taskkill.exe", "/f", "/im", "explorer.exe"]]
    assert started == [["explorer.exe"]]


def test_volume_actions_reuse_the_one_media_key_path(monkeypatch):
    seen: list = []
    monkeypatch.setattr(power, "_WIN", True)
    from capabilities import media
    monkeypatch.setattr(media, "transport",
                        lambda action: seen.append(action) or {"ok": True, "detail": f"Sent {action}"})

    for action in ("volume_up", "volume_down", "mute"):
        assert power.run(action, {})["ok"] is True

    assert seen == ["volume_up", "volume_down", "mute"]


def test_set_volume_needs_a_percentage(no_os):
    result = power.run("set_volume", {"percent": "loud"})

    assert result["ok"] is False
    assert result["reason"] == "bad_percent"
    assert not no_os, "nonsense must not reach the OS"


def test_set_volume_out_of_range_is_refused(no_os):
    assert power.run("set_volume", {"percent": 150})["reason"] == "bad_percent"
    assert not no_os


def test_set_volume_uses_pycaw_when_it_is_available(monkeypatch):
    monkeypatch.setattr(power, "_WIN", True)
    levels: list = []

    class _Endpoint:
        def SetMasterVolumeLevelScalar(self, value, ctx):  # noqa: N802
            levels.append(value)

    class _Speakers:
        EndpointVolume = _Endpoint()

    class _AudioUtilities:
        @staticmethod
        def GetSpeakers():  # noqa: N802
            return _Speakers()

    fake = types.ModuleType("pycaw.pycaw")
    fake.AudioUtilities = _AudioUtilities
    monkeypatch.setitem(sys.modules, "pycaw", types.ModuleType("pycaw"))
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", fake)

    result = power.run("set_volume", {"percent": 40})

    assert result["ok"] is True
    assert levels == [0.4], "40% is 0.4 on the audio endpoint"


def test_set_volume_without_pycaw_says_what_it_cannot_do(no_os, monkeypatch):
    monkeypatch.setitem(sys.modules, "pycaw", None)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", None)

    result = power.run("set_volume", {"percent": 40})

    assert result["ok"] is False
    assert result["reason"] == "set_volume_unavailable"
    assert "pycaw" in result["detail"]


def test_set_brightness_passes_the_validated_percentage(no_os):
    power.run("set_brightness", {"percent": 55})

    script = _commands(no_os, "powershell")[0]
    assert "Brightness=55" in script, "the percentage must be the value, never text"


# ── failures are reported, never hidden or raised ─────────────────────────

def test_a_failed_command_is_reported_with_what_windows_said(monkeypatch):
    monkeypatch.setattr(power, "_WIN", True)
    monkeypatch.setattr(power, "_run", lambda argv, timeout=None: (False, "Access is denied."))

    result = power.run("shutdown", {})

    assert result["ok"] is False
    assert result["reason"] == "shutdown_failed"
    assert "Access is denied." in result["detail"]


def test_an_unexpected_failure_never_escapes_the_controller(monkeypatch):
    """Even a bug inside a handler is reported as a failed action, never raised."""
    monkeypatch.setattr(power, "_WIN", True)

    def boom(argv, timeout=None):
        raise OSError("the OS refused")

    monkeypatch.setattr(power, "_run", boom)

    result = power.run("lock", {})

    assert result["ok"] is False
    assert result["reason"] == "power_failed"
    assert "the OS refused" in result["detail"]


def test_an_unknown_action_lists_what_is_supported(monkeypatch):
    monkeypatch.setattr(power, "_run", lambda *a, **k: pytest.fail("no command"))
    monkeypatch.setattr(power, "_WIN", True)

    result = power.run("explode", {})

    assert result["ok"] is False
    assert result["reason"] == "unknown_power_action"
    assert "shutdown" in result["detail"]


def test_power_control_is_windows_only_and_says_so(monkeypatch):
    monkeypatch.setattr(power, "_WIN", False)
    monkeypatch.setattr(power, "_run", lambda *a, **k: pytest.fail("no command"))

    result = power.run("shutdown", {})

    assert result["ok"] is False
    assert result["reason"] == "unsupported_platform"


def test_status_lists_the_real_actions():
    state = power.status()

    assert "shutdown" in state["actions"]
    assert "set_brightness" in state["actions"]
    assert isinstance(state["detail"], str) and state["detail"]


# ── the registry is the policy ────────────────────────────────────────────

def test_the_registry_vocabulary_and_the_handlers_cannot_drift():
    declared = set(registry.names(registry.TOOL_POWER))

    assert set(power.status()["actions"]) == declared
    assert set(GATED + UNGATED) <= declared


@pytest.mark.parametrize("name", GATED)
def test_session_ending_actions_declare_the_confirmation_gate(name):
    action = registry.get(registry.TOOL_POWER, name)

    assert action is not None
    assert action.gate == "confirm"


@pytest.mark.parametrize("name", UNGATED)
def test_reversible_actions_do_not_ask(name):
    action = registry.get(registry.TOOL_POWER, name)

    assert action is not None
    assert action.gate == ""


def test_the_power_tool_declares_its_parameters_for_the_model():
    params = registry.parameters(registry.TOOL_POWER)

    assert "shutdown" in params["properties"]["action"]["enum"]
    assert "percent" in params["properties"]
    assert params["required"] == ["action"]


def test_set_volume_requires_its_percentage():
    action, error = registry.validate(registry.TOOL_POWER, {"action": "set_volume"})

    assert action is None
    assert "percent" in error


# ── the router enforces the gate before any controller runs ───────────────

def _runner(seen: list):
    def run():
        seen.append("ran")
        return {"ok": True, "output": "done"}
    return run


def test_a_gated_action_is_parked_until_the_hud_confirms(monkeypatch):
    from capabilities import router
    from security import confirm

    seen: list = []
    parked: dict = {}

    def fake_request(key, title, detail, run):
        parked.update({"key": key, "title": title, "run": run})
        return "[CONFIRMATION_PENDING] waiting for the HUD"

    monkeypatch.setattr(confirm, "request", fake_request)
    action = registry.get(registry.TOOL_POWER, "shutdown")

    result = router.route_local(registry.TOOL_POWER, action, {"action": "shutdown"},
                                _runner(seen), enabled=lambda name: True)

    assert result.reason == "awaiting_confirmation"
    assert seen == [], "nothing may run before the human presses CONFIRM"
    assert "power" in parked["key"]
    assert "shutdown" in parked["title"].lower()

    # The callable the HUD runs on CONFIRM is the ONLY way the action runs.
    parked["run"]()
    assert seen == ["ran"]


def test_a_reversible_action_runs_without_asking(monkeypatch):
    from capabilities import router
    from security import confirm

    monkeypatch.setattr(confirm, "request",
                        lambda **kw: pytest.fail("locking must not ask"))
    seen: list = []
    action = registry.get(registry.TOOL_POWER, "lock")

    result = router.route_local(registry.TOOL_POWER, action, {"action": "lock"},
                                _runner(seen), enabled=lambda name: True)

    assert result.ok is True
    assert seen == ["ran"]


def test_the_power_switch_refuses_before_anything_runs(monkeypatch):
    from capabilities import actions
    monkeypatch.setattr(power, "run", lambda *a, **k: pytest.fail("no power action"))

    text = actions.handle_power_control({"action": "lock"},
                                        enabled=lambda name: name != "power_control")

    assert "switched off" in text
    assert "status=NOT_DONE" in text


def test_shutdown_waits_for_the_hud_even_through_the_tool_handler(monkeypatch):
    """Headless runs have no HUD bound, so the gate must refuse, not proceed."""
    from capabilities import actions
    monkeypatch.setattr(power, "run", lambda *a, **k: pytest.fail("no power action"))

    text = actions.handle_power_control({"action": "shutdown"},
                                        enabled=lambda name: True)

    assert "status=NOT_DONE" in text
    assert "interface is not available" in text
    assert "Shutting this PC down" not in text


# ── the approval service agrees, and the wiring exists ────────────────────

def test_the_approval_service_treats_power_actions_as_irreversible():
    from security import approvals

    assert approvals.requires_ui_token("power_control_shutdown") is True
    assert approvals.requires_ui_token("power_control_restart") is True
    assert approvals.requires_ui_token("power_control_lock") is False


def test_power_control_is_declared_and_dispatched_in_main():
    assert '"name": "power_control"' in MAIN_SOURCE
    assert 'parameters("power_control")' in MAIN_SOURCE
    assert '"power_control": _local_actions.handle_power_control' in MAIN_SOURCE
    start = MAIN_SOURCE.index(
        'elif name in ("pc_control", "visual_control", "browser_action",')
    assert '"power_control"' in MAIN_SOURCE[start:start + 220]


def test_the_power_switch_is_a_settings_checkbox_that_saves():
    assert 'self._perm_checks, "power_control"' in SETTINGS_SOURCE
    # _perm_checks is what the SAVE path writes into the features map.
    assert "_perm_checks" in SETTINGS_SOURCE and "isChecked()" in SETTINGS_SOURCE
    assert 'features.update({k: v.isChecked() for k, v in getattr(self, "_perm_checks", {}).items()})' in SETTINGS_SOURCE
