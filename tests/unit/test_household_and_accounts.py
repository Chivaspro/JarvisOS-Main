"""Smart home and Instagram: registry vocabulary, permission gates, and the
honest degradation every optional package must have.

No device is switched and no account is contacted: the modules under test are
exercised through their own handlers with the third-party layer stubbed.
"""
from __future__ import annotations

import sys
import types

import pytest

from capabilities import actions, instagram, registry, router, smart_home
from config.settings import FEATURE_DEFAULTS
from security import permissions


def _enabled_with(*off: str):
    def _enabled(feature: str) -> bool:
        return feature not in off
    return _enabled


# ── registry + gates ──────────────────────────────────────────────────────────

def test_both_tools_are_declared_with_actions():
    assert registry.names(registry.TOOL_SMART_HOME)
    assert registry.names(registry.TOOL_INSTAGRAM)
    for tool in (registry.TOOL_SMART_HOME, registry.TOOL_INSTAGRAM):
        params = registry.parameters(tool)
        assert params["required"] == ["action"]
        assert params["properties"]["action"]["enum"]


def test_every_action_declares_a_real_switch_a_controller_and_a_lock():
    for tool in (registry.TOOL_SMART_HOME, registry.TOOL_INSTAGRAM):
        assert tool in router.LOCK_OF and tool in router.ENGINE_OF
        assert tool in actions._RUNNERS
        for action in registry.for_tool(tool):
            assert action.permission in FEATURE_DEFAULTS, action.name
            assert action.controller and action.chain


def test_publishing_to_instagram_always_needs_the_hud_confirmation():
    post = registry.get(registry.TOOL_INSTAGRAM, "post_photo")
    message = registry.get(registry.TOOL_INSTAGRAM, "direct_message")
    profile = registry.get(registry.TOOL_INSTAGRAM, "profile")
    assert post is not None and post.gate == "confirm"
    assert message is not None and message.gate == "confirm"
    assert profile is not None and profile.gate == ""


def test_a_required_parameter_is_enforced_before_any_client_is_built():
    action, error = registry.validate(registry.TOOL_SMART_HOME,
                                      {"action": "set_brightness"})
    assert action is None and "percent" in error
    action, error = registry.validate(registry.TOOL_INSTAGRAM,
                                      {"action": "profile"})
    assert action is None and "user" in error


def test_each_service_has_its_own_switch():
    assert permissions.permission_refusal(
        _enabled_with("smart_home"), "smart_home", {"action": "turn_off"})
    assert permissions.permission_refusal(
        _enabled_with("instagram"), "instagram", {"action": "profile"})
    # Reading the Instagram status is not an account action.
    assert permissions.permission_refusal(
        _enabled_with("instagram"), "instagram", {"action": "status"}) is None
    # ...and revoking desktop control does not revoke the lamp.
    assert permissions.permission_refusal(
        _enabled_with("desktop_control"), "smart_home",
        {"action": "turn_off"}) is None


# ── smart home ────────────────────────────────────────────────────────────────

def test_without_the_package_the_switch_is_refused_and_named(monkeypatch):
    monkeypatch.setattr(smart_home, "_kasa", lambda: None)
    result = smart_home.run("turn_on", {"host": "10.0.0.5"})
    assert not result["ok"]
    assert result["reason"] == "missing_dependency"
    assert "pip install python-kasa" in result["detail"]


def test_an_unknown_action_lists_the_real_ones():
    result = smart_home.run("make_coffee", {})
    assert not result["ok"]
    assert "turn_on" in result["detail"]


def test_a_named_device_is_read_back_not_assumed(monkeypatch):
    class _Device:
        alias = "Desk Lamp"
        host = "10.0.0.5"
        model = "KL130"
        brightness = 40
        is_on = False

        async def turn_on(self):
            self.is_on = True

        async def update(self):
            return True

        async def turn_off(self):
            self.is_on = False

    device = _Device()
    monkeypatch.setattr(smart_home, "_kasa", lambda: object())
    monkeypatch.setattr(smart_home, "_connect_async", lambda host: _await(device))
    result = smart_home.run("turn_on", {"host": "10.0.0.5"})
    assert result["ok"], result["detail"]
    assert "on" in result["detail"] and "Desk Lamp" in result["detail"]
    assert result["evidence"]["changed"] == 1


def test_a_device_that_does_not_answer_is_named(monkeypatch):
    monkeypatch.setattr(smart_home, "_kasa", lambda: object())

    async def _boom(host):
        raise RuntimeError(f"{host} did not answer (timed out)")

    monkeypatch.setattr(smart_home, "_connect_async", _boom)
    result = smart_home.run("turn_off", {"host": "10.0.0.9"})
    assert not result["ok"]
    assert "10.0.0.9" in result["detail"]
    assert result["reason"] == "unreachable"


def test_no_device_named_and_none_configured_is_explained(monkeypatch):
    monkeypatch.setattr(smart_home, "_kasa", lambda: object())
    monkeypatch.setattr(smart_home, "configured_hosts", lambda: [])
    result = smart_home.run("status", {})
    assert not result["ok"]
    assert "configured" in result["detail"]


def test_a_single_configured_device_needs_no_address(monkeypatch):
    class _Device:
        alias = "Printer"
        host = "10.0.0.7"
        model = ""
        is_on = True

        async def update(self):
            return True

    monkeypatch.setattr(smart_home, "_kasa", lambda: object())
    monkeypatch.setattr(smart_home, "configured_hosts", lambda: ["10.0.0.7"])
    monkeypatch.setattr(smart_home, "_connect_async", lambda host: _await(_Device()))
    result = smart_home.run("status", {})
    assert result["ok"], result["detail"]
    assert "Printer" in result["detail"]


def test_brightness_outside_the_range_is_clamped(monkeypatch):
    class _Bulb:
        alias = "Strip"
        host = "10.0.0.8"
        brightness = 0
        is_on = True

        async def set_brightness(self, percent):
            self.brightness = percent

        async def turn_on(self):
            self.is_on = True

        async def update(self):
            return True

    bulb = _Bulb()
    monkeypatch.setattr(smart_home, "_kasa", lambda: object())
    monkeypatch.setattr(smart_home, "_connect_async", lambda host: _await(bulb))
    result = smart_home.run("set_brightness", {"host": "10.0.0.8", "percent": 400})
    assert result["ok"], result["detail"]
    assert bulb.brightness == 100
    assert result["evidence"]["percent"] == 100


def test_the_module_status_reports_availability_without_a_network(monkeypatch):
    monkeypatch.setattr(smart_home, "_kasa", lambda: None)
    state = smart_home.status()
    assert state["available"] is False
    assert "python-kasa" in state["detail"]


# ── instagram ─────────────────────────────────────────────────────────────────

def test_without_the_package_the_status_is_explained(monkeypatch):
    monkeypatch.setattr(instagram, "_instagrapi", lambda: None)
    result = instagram.run("status", {})
    assert result["ok"] is True   # reporting the state is never an error
    assert "pip install instagrapi" in result["detail"]


def test_an_action_without_the_package_fails_with_the_reason(monkeypatch):
    monkeypatch.setattr(instagram, "_instagrapi", lambda: None)
    result = instagram.run("profile", {"user": "someone"})
    assert not result["ok"]
    assert "instagrapi" in result["detail"]


def test_a_missing_username_is_named(monkeypatch):
    monkeypatch.setattr(instagram, "_instagrapi", lambda: object)
    monkeypatch.setattr(instagram, "_credentials", lambda: ("", "", ""))
    with pytest.raises(RuntimeError) as caught:
        instagram.client()
    assert "username" in str(caught.value)


def test_a_profile_is_read_from_the_public_api(monkeypatch):
    class _Info:
        pk = "42"
        username = "someone"
        full_name = "Some One"
        biography = "Hello"
        follower_count = 120
        following_count = 30
        media_count = 5
        is_private = False

    class _Client:
        user_id = "owner"

        def user_info_by_username(self, name):
            assert name == "someone"
            return _Info()

    monkeypatch.setattr(instagram, "client", lambda: _Client())
    result = instagram.run("profile", {"user": "@someone"})
    assert result["ok"], result["detail"]
    assert "120 followers" in result["detail"]
    assert result["evidence"]["pk"] == "42"


def test_a_private_account_is_reported_rather_than_attempted(monkeypatch):
    class _Info:
        pk = "9"
        username = "closed"
        full_name = ""
        biography = ""
        follower_count = 10
        following_count = 1
        media_count = 2
        is_private = True

    class _Client:
        def user_info_by_username(self, name):
            return _Info()

    monkeypatch.setattr(instagram, "client", lambda: _Client())
    result = instagram.run("recent_posts", {"user": "closed"})
    assert not result["ok"]
    assert result["reason"] == "private_account"


def test_a_rate_limit_is_reported_as_a_rate_limit(monkeypatch):
    class _Client:
        def user_info_by_username(self, name):
            raise RuntimeError("HTTP 429 Too Many Requests")

    monkeypatch.setattr(instagram, "client", lambda: _Client())
    result = instagram.run("profile", {"user": "someone"})
    assert not result["ok"]
    assert result["reason"] == "rate_limited"


def test_a_challenge_is_reported_with_the_next_step(monkeypatch):
    class _Client:
        def user_info_by_username(self, name):
            raise RuntimeError("challenge_required")

    monkeypatch.setattr(instagram, "client", lambda: _Client())
    result = instagram.run("profile", {"user": "someone"})
    assert not result["ok"]
    assert result["reason"] == "challenge"
    assert "in its own app" in result["detail"] or "own app" in result["detail"]


def test_posting_needs_an_existing_image(monkeypatch):
    monkeypatch.setattr(instagram, "client", lambda: pytest.fail(
        "no client may be built without an image"))
    result = instagram.run("post_photo", {"path": "C:/nope/missing.jpg"})
    assert not result["ok"]
    assert result["reason"] == "not_found"


def test_the_module_status_exposes_no_password(monkeypatch):
    monkeypatch.setattr(instagram, "_credentials",
                        lambda: ("someone", "hunter2", ""))
    state = instagram.status()
    assert state["password_set"] is True
    assert "hunter2" not in str(state)


# ── helpers ──────────────────────────────────────────────────────────────────

def _await(value):
    """Wrap an awaitable for the sync ``_connect_async`` stubs."""
    import asyncio

    async def _inner():
        return value
    return _inner()
