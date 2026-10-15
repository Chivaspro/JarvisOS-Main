"""JarvisOS Connect: pairing, per-device tokens, command gating and the server.

The gateway is exercised over a real loopback socket (it is a local service, and
a stubbed transport would not prove the routes work), with the device registry and
the pairing secret redirected to a temporary folder. Nothing leaves this machine:
the bind address in every test is 127.0.0.1.
"""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from connect import commands, devices, gateway, tool as connect_tool


@pytest.fixture()
def registry(monkeypatch):
    """An isolated device registry and a known pairing secret."""
    with tempfile.TemporaryDirectory(prefix="jarvis-connect-") as folder:
        path = Path(folder) / "devices.json"
        monkeypatch.setattr(devices, "registry_path", lambda: path)
        monkeypatch.setattr(devices, "pairing_secret", lambda: "correct-horse")
        yield path


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture()
def live_gateway(registry):
    server = gateway.Gateway(host="127.0.0.1", port=_free_port())
    ok, message = server.start()
    assert ok, message
    yield server
    server.stop("test over")


def _post(server, path: str, payload: dict, token: str = ""):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _get(server, path: str, token: str = ""):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


# ── pairing ──────────────────────────────────────────────────────────────────

def test_the_pairing_code_is_derived_and_rotates(monkeypatch):
    monkeypatch.setattr(devices, "pairing_secret", lambda: "s3cret")
    now = time.time()
    first = devices.pairing_code(window=now)
    again = devices.pairing_code(window=now + 10)
    later = devices.pairing_code(window=now + devices.CODE_WINDOW_S + 5)
    assert first == again
    assert first != later
    assert len(first) == 6 and first.isdigit()


def test_without_a_secret_there_is_no_code_and_no_pairing(monkeypatch):
    monkeypatch.setattr(devices, "pairing_secret", lambda: "")
    assert devices.pairing_code() == ""
    device, message = devices.add_device("phone", "android", "123456")
    assert device is None
    assert "pairing secret" in message


def test_pairing_needs_the_current_code(registry):
    device, message = devices.add_device("phone", "android", "000000")
    assert device is None and "not valid" in message
    device, token = devices.add_device("phone", "android", devices.pairing_code())
    assert device and token
    assert devices.find_by_token(token)["name"] == "phone"


def test_the_previous_window_is_accepted_for_clock_slop(monkeypatch):
    monkeypatch.setattr(devices, "pairing_secret", lambda: "s3cret")
    previous = devices.pairing_code(window=time.time() - devices.CODE_WINDOW_S)
    assert devices._code_matches(previous, "s3cret") is True


def test_a_device_can_be_revoked_on_its_own(registry):
    _d1, token1 = devices.add_device("phone", "android", devices.pairing_code())
    _d2, token2 = devices.add_device("tablet", "ipad", devices.pairing_code())
    ok, message = devices.revoke_device("phone")
    assert ok and "phone" in message
    assert devices.find_by_token(token1) is None
    assert devices.find_by_token(token2) is not None
    assert len(devices.list_devices()) == 1


def test_the_registry_never_exposes_tokens(registry):
    devices.add_device("phone", "android", devices.pairing_code())
    assert "token" not in devices.list_devices()[0]
    assert "token" in registry.read_text(encoding="utf-8")


# ── the command catalogue ────────────────────────────────────────────────────

def test_every_command_declares_a_summary_and_an_implementation():
    for command in commands.COMMANDS:
        assert command.summary
        assert command.action in commands._RUNNERS, command.action


def test_an_unknown_command_is_refused_with_the_real_list():
    result = commands.execute("format_disk", {})
    assert not result["ok"]
    assert result["reason"] == "unknown_command"
    assert "ping" in result["detail"]


def test_a_command_without_its_parameter_is_refused():
    result = commands.execute("ask", {})
    assert not result["ok"]
    assert result["reason"] == "missing_parameter"


def test_a_command_honours_the_desktop_permission(monkeypatch):
    monkeypatch.setattr(commands, "_RUNNERS", {**commands._RUNNERS,
                                               "screenshot": lambda args: pytest.fail(
                                                   "must not run when refused")})
    result = commands.execute("screenshot", {},
                              enabled=lambda name: name != "screen_understanding")
    assert not result["ok"]
    assert result["reason"] == "disabled"
    assert "Screen capture" in result["detail"]


def test_ping_needs_no_permission_and_answers():
    result = commands.execute("ping", {}, enabled=lambda name: False)
    assert result["ok"]
    assert "awake" in result["detail"]


def test_a_crashing_command_becomes_a_failure_not_a_traceback(monkeypatch):
    monkeypatch.setattr(commands, "_RUNNERS",
                        {**commands._RUNNERS,
                         "ping": lambda args: (_ for _ in ()).throw(
                             RuntimeError("the machine is on fire"))})
    result = commands.execute("ping", {})
    assert not result["ok"]
    assert "on fire" in result["detail"]


def test_restart_and_shutdown_are_declared_as_confirming_commands():
    for action in ("restart", "shutdown"):
        assert commands.describe(action)["confirm"] is True
    assert commands.describe("lock")["confirm"] is False


def test_the_catalogue_is_describable_for_the_client():
    rows = commands.describe_all()
    assert len(rows) == len(commands.COMMANDS)
    assert all("summary" in row and "required" in row for row in rows)


# ── the gateway ──────────────────────────────────────────────────────────────

def test_health_and_commands_are_open(live_gateway):
    status, payload = _get(live_gateway, "/health")
    assert status == 200 and payload["ok"] is True
    status, payload = _get(live_gateway, "/commands")
    assert status == 200
    assert any(row["action"] == "ping" for row in payload["commands"])


def test_the_web_client_is_served(live_gateway):
    request = urllib.request.Request(f"http://127.0.0.1:{live_gateway.port}/")
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
    assert response.status == 200
    assert "JarvisOS Connect" in body
    assert "Pairing code" in body


def test_a_device_cannot_pair_without_the_code(live_gateway):
    status, payload = _post(live_gateway, "/pair",
                            {"name": "phone", "code": "000000"})
    assert status == 403
    assert "not valid" in payload["detail"]


def test_pairing_over_http_returns_a_token_and_the_catalogue(live_gateway):
    status, payload = _post(live_gateway, "/pair", {
        "name": "phone", "platform": "android",
        "code": devices.pairing_code()})
    assert status == 200 and payload["ok"], payload
    assert payload["token"]
    assert payload["device"]["name"] == "phone"
    assert any(row["action"] == "ping" for row in payload["commands"])


def test_a_command_without_a_token_is_refused(live_gateway):
    status, payload = _post(live_gateway, "/command", {"action": "ping"})
    assert status == 401
    assert "token" in payload["detail"]


def test_a_paired_device_can_run_an_allowed_command(live_gateway):
    _status, paired = _post(live_gateway, "/pair", {
        "name": "phone", "code": devices.pairing_code()})
    status, payload = _post(live_gateway, "/command", {"action": "ping"},
                            token=paired["token"])
    assert status == 200 and payload["ok"], payload
    assert payload["command"] == "ping"


def test_the_desktop_permission_is_enforced_for_the_phone(live_gateway):
    _status, paired = _post(live_gateway, "/pair", {
        "name": "phone", "code": devices.pairing_code()})
    live_gateway.ui = type("UI", (), {"feature_enabled":
                                      staticmethod(lambda name: False)})()
    status, payload = _post(live_gateway, "/command", {"action": "screenshot"},
                            token=paired["token"])
    assert status == 400
    assert payload["reason"] == "disabled"
    assert "Screen capture" in payload["detail"]


def test_a_destructive_command_is_parked_for_confirmation(live_gateway):
    _status, paired = _post(live_gateway, "/pair", {
        "name": "phone", "code": devices.pairing_code()})
    status, payload = _post(live_gateway, "/command", {"action": "restart"},
                            token=paired["token"])
    # The confirmation layer is not attached in a test process, so the honest
    # answer is "it was not run" rather than a success.
    assert payload["ok"] is False
    assert payload["reason"] in ("awaiting_confirmation", "unavailable")


def test_revoking_a_device_stops_its_token(live_gateway):
    _status, paired = _post(live_gateway, "/pair", {
        "name": "phone", "code": devices.pairing_code()})
    _status, _ = _post(live_gateway, "/revoke", {"device": "phone"},
                       token=paired["token"])
    status, payload = _post(live_gateway, "/command", {"action": "ping"},
                            token=paired["token"])
    assert status == 401


def test_an_unknown_route_is_reported(live_gateway):
    status, payload = _get(live_gateway, "/nope")
    assert status == 404 and "No route" in payload["detail"]


def test_a_second_gateway_on_the_same_port_says_so(live_gateway):
    other = gateway.Gateway(host="127.0.0.1", port=live_gateway.port)
    ok, message = other.start()
    assert ok is False
    assert "could not listen" in message


def test_status_reports_the_listening_address_and_the_code(live_gateway):
    state = live_gateway.status()
    assert state["running"] is True
    assert state["url"] == f"http://127.0.0.1:{live_gateway.port}"
    assert state["pairing_code"] == devices.pairing_code()
    assert state["has_secret"] is True


# ── the tool ─────────────────────────────────────────────────────────────────

def test_the_tool_reports_the_gateway_state(registry):
    out = connect_tool.handler({"action": "status"})
    assert "not running" in out
    assert "pairing secret" in out or "pairing code" in out


def test_the_tool_refuses_to_invent_a_pairing_code(monkeypatch):
    monkeypatch.setattr(devices, "pairing_secret", lambda: "")
    out = connect_tool.handler({"action": "code"})
    assert "no pairing secret" in out


def test_the_tool_hands_out_the_code_and_the_address(registry):
    out = connect_tool.handler({"action": "code"})
    assert devices.pairing_code() in out
    assert "127.0.0.1" in out or "http://" in out


def test_the_tool_lists_the_commands_a_phone_may_use(registry):
    out = connect_tool.handler({"action": "commands"})
    assert "ping" in out and "screenshot" in out
    assert "needs on-screen confirmation" in out


def test_the_tool_lists_and_revokes_devices(registry):
    devices.add_device("phone", "android", devices.pairing_code())
    out = connect_tool.handler({"action": "devices"})
    assert "phone" in out
    out = connect_tool.handler({"action": "revoke", "device": "phone"})
    assert "Revoked" in out
    assert connect_tool.handler({"action": "devices"}).startswith("No device")


def test_the_tool_refuses_an_unknown_action(registry):
    assert "is not a Connect action" in connect_tool.handler({"action": "teleport"})


def test_a_switched_off_gateway_cannot_be_started(monkeypatch, registry):
    monkeypatch.setattr(connect_tool, "_switch_on", lambda: False)
    out = connect_tool.handler({"action": "start"})
    assert "switched off" in out
    # Reporting the state is still allowed: the user has to be able to ask.
    assert "not running" in connect_tool.handler({"action": "status"})
