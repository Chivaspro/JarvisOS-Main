"""The service hub: honest states, real probes, and no hang when a link is down.

Nothing here touches the network. Every probe is exercised through a stubbed
transport or a stubbed package check, which is exactly the boundary that decides
what the Settings window (and the assistant) is told.
"""
from __future__ import annotations

import json
import socket
import tempfile
from pathlib import Path

import pytest

from services import hub, tool as hub_tool


class _Response:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ── describing services ───────────────────────────────────────────────────────

def test_every_service_has_a_label_a_summary_and_a_probe():
    assert len(hub.SERVICES) >= 9
    for service in hub.SERVICES:
        assert service.label and service.summary and service.id
        assert callable(getattr(hub, service.probe_name, None)), service.id


def test_describe_returns_ui_metadata_and_none_for_an_unknown_service():
    meta = hub.describe("spotify")
    assert meta["label"] == "Spotify"
    assert meta["switch"] == "spotify"
    assert {field["name"] for field in meta["fields"]} == {
        "spotify_client_id", "spotify_client_secret"}
    assert hub.describe("nope") is None


# ── cheap status (never touches a socket) ─────────────────────────────────────

def test_a_switch_that_is_off_is_reported_as_disabled(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: name != "spotify")
    rows = {row["id"]: row for row in hub.status_all({})}
    assert rows["spotify"]["state"] == "disabled"
    assert "switched off" in rows["spotify"]["detail"]
    # The disabled service is not described as ready by accident.
    assert rows["spotify"]["ok"] is False


def test_a_missing_mandatory_credential_is_reported_by_name(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: True)
    # The hub honours an environment variable as a real credential (that is what
    # config.settings does too), so the test removes it to stay deterministic.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rows = {row["id"]: row for row in hub.status_all({"gemini_api_key": ""})}
    assert rows["gemini"]["state"] == "missing_key"
    assert "Gemini API key" in rows["gemini"]["detail"]


def test_optional_credentials_do_not_block_a_service(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: True)
    rows = {row["id"]: row for row in hub.status_all({})}
    assert rows["weather"]["state"] != "missing_key"


def test_spotify_needs_both_its_credentials(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: True)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    rows = {row["id"]: row for row in hub.status_all({"spotify_client_id": "",
                                                      "spotify_client_secret": ""})}
    assert rows["spotify"]["state"] == "missing_key"
    assert "Spotify client ID" in rows["spotify"]["detail"]


def test_probing_an_unknown_service_names_the_real_ones():
    result = hub.probe("telegraph", {})
    assert result["state"] == "unknown"
    assert "spotify" in result["detail"]


def test_probing_a_switched_off_service_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: False)
    monkeypatch.setattr(hub, "_get", lambda *_a, **_k: pytest.fail("no request may run"))
    result = hub.probe("spotify", {"spotify_client_id": "k",
                                   "spotify_client_secret": "s"})
    assert result["state"] == "disabled"
    assert result["switch"] == "spotify"


# ── real probes (transport stubbed) ───────────────────────────────────────────

#: A real-shaped OpenRouter key (long, one unbroken token).
_OR_KEY = "sk-or-v1-" + "a1b2c3d4" * 7


def _openrouter_transport(payload, status: int = 200, seen: list | None = None):
    """A stub that answers /key for the authenticated path and /models without."""
    def _fake(url: str, headers=None, timeout=8.0):
        if seen is not None:
            seen.append((url, dict(headers or {})))
        return _Response(status, payload)
    return _fake


def test_openrouter_validates_the_stored_key_not_just_the_endpoint(monkeypatch):
    """The /models list is public: it used to call any stored value READY.

    The probe the Settings window runs must answer for the KEY, because that is
    what a forge run will send.
    """
    seen: list = []
    monkeypatch.setattr(hub, "_get", _openrouter_transport(
        {"data": {"label": "desktop", "usage": 1.5, "limit": 10}}, seen=seen))
    result = hub.probe("openrouter", {"openrouter_api_key": _OR_KEY})
    assert result["state"] == "ready" and result["ok"]
    assert seen and seen[0][0].endswith("/api/v1/key")
    assert seen[0][1].get("Authorization") == f"Bearer {_OR_KEY}"
    assert "desktop" in result["detail"] and "1.50 of 10.00" in result["detail"]


def test_openrouter_rejects_a_key_that_is_really_a_pasted_paragraph(monkeypatch):
    monkeypatch.setattr(hub, "_get",
                        lambda *a, **k: pytest.fail("no request may run"))
    result = hub.probe("openrouter",
                       {"openrouter_api_key": "now  scan or innspect en th"})
    assert result["state"] == "unavailable"
    assert "pasted text" in result["detail"]


def test_openrouter_names_a_key_the_provider_rejects(monkeypatch):
    monkeypatch.setattr(hub, "_get", _openrouter_transport({"error": "no"}, 401))
    result = hub.probe("openrouter", {"openrouter_api_key": _OR_KEY})
    assert result["state"] == "unavailable"
    assert "rejected" in result["detail"] and "401" in result["detail"]


def test_openrouter_without_a_key_still_reports_the_endpoint_works(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: _Response(200, {"data": []}))
    result = hub.probe("openrouter", {})
    assert result["state"] == "key_optional"
    assert "no key set" in result["detail"]


def test_a_http_error_is_reported_with_its_status_code(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: _Response(500, None))
    result = hub.probe("radar", {})
    assert not result["ok"]
    assert "500" in result["detail"]


def test_the_weather_probe_reports_the_temperature_it_received(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: _Response(
        200, {"current": {"temperature_2m": 17.5}}))
    result = hub.probe("weather", {})
    assert result["state"] == "ready"
    assert "17.5" in result["detail"]
    assert result["sample_c"] == 17.5


def test_the_radar_probe_counts_the_frames(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: _Response(
        200, {"radar": {"past": [1, 2, 3, 4]}}))
    result = hub.probe("radar", {})
    assert result["frames"] == 4


def test_anthropic_without_a_key_is_optional_not_broken(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: pytest.fail("no request may run"))
    result = hub.probe("anthropic", {})
    assert result["state"] == "key_optional"


def test_a_gemini_key_that_is_absent_is_named(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = hub.probe("gemini", {"gemini_api_key": ""})
    assert result["state"] == "missing_key"
    assert "Gemini API key" in result["detail"]


def test_the_gemini_probe_does_not_pin_a_retired_text_model():
    """Google retires text models for new accounts ("no longer available to
    new users") while other models keep working. Pinning one name made a valid
    key test red; the probe walks a short list instead. The aliases lead — and
    the cheap lite alias is tried first, because a busy full-size model (live
    measurement: gemini-flash-latest stalling into 503/504) must not decide the
    verdict when the lite model answers the probe in ~2s."""
    assert "gemini-2.5-flash" not in hub._GEMINI_PROBE_MODELS
    assert len(hub._GEMINI_PROBE_MODELS) >= 2
    assert hub._GEMINI_PROBE_MODELS[0] == "gemini-flash-lite-latest"
    assert "gemini-flash-latest" in hub._GEMINI_PROBE_MODELS


# ── the Gemini key test (regression: the 8s deadline bug) ─────────────────────

#: A real-shaped Gemini key (long, one unbroken token).
_GM_KEY = "AQ.Ab8" + "x9Yz0" * 10


class _GeminiClientSpy:
    """Records the http_options the probe hands to google-genai."""

    calls: list = []

    def __init__(self, **kwargs):
        _GeminiClientSpy.calls.append(kwargs)
        self.models = self

    def generate_content(self, *, model, contents, config):
        return type("Resp", (), {"text": " ready "})()


def _install_fake_genai(monkeypatch):
    """Make `from google import genai` resolve to the spy, without the network."""
    import sys
    import types
    google = sys.modules.get("google")
    if google is None:
        google = types.ModuleType("google")
        monkeypatch.setitem(sys.modules, "google", google)
    fake = types.ModuleType("google.genai")
    fake.Client = _GeminiClientSpy
    monkeypatch.setitem(sys.modules, "google.genai", fake)
    monkeypatch.setattr(google, "genai", fake, raising=False)
    monkeypatch.setattr(hub, "_has_module", lambda name: True)
    _GeminiClientSpy.calls = []


def test_the_gemini_probe_never_sets_a_deadline_below_the_api_minimum(monkeypatch):
    """The bug this pins: `timeout=8000` made the API refuse EVERY call with
    400 INVALID_ARGUMENT ("Manually set deadline 8s is too short"), so a valid
    key tested as UNAVAILABLE. The hub's own default timeout is 8s, so the
    probe must raise the deadline itself."""
    _install_fake_genai(monkeypatch)
    result = hub.probe("gemini", {"gemini_api_key": _GM_KEY}, timeout=hub.DEFAULT_TIMEOUT)
    assert result["state"] == "ready" and result["ok"]
    deadline_ms = _GeminiClientSpy.calls[0]["http_options"]["timeout"]
    assert deadline_ms >= 10_000, "10s is the API's minimum manual deadline"


def test_the_gemini_probe_reports_the_model_that_answered(monkeypatch):
    _install_fake_genai(monkeypatch)
    result = hub.probe("gemini", {"gemini_api_key": _GM_KEY}, timeout=9.0)
    assert result["model"] == hub._GEMINI_PROBE_MODELS[0]
    assert result["attempts"] == 1


def test_a_pasted_paragraph_is_never_sent_as_a_gemini_key(monkeypatch):
    _install_fake_genai(monkeypatch)
    result = hub.probe("gemini", {"gemini_api_key": "this is not a key at all"})
    assert result["state"] == "unavailable"
    assert "spaces" in result["detail"]
    assert _GeminiClientSpy.calls == [], "no request may be spent on a non-key"


def test_a_short_gemini_value_is_named_as_too_short(monkeypatch):
    _install_fake_genai(monkeypatch)
    result = hub.probe("gemini", {"gemini_api_key": "abc123"})
    assert result["state"] == "unavailable"
    assert "shorter" in result["detail"]


class _FakeApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def test_gemini_failures_are_told_apart_instead_of_blamed_on_the_key():
    assert hub._gemini_failure_reason(_FakeApiError(403, "PERMISSION_DENIED")) == (
        "the key was rejected by Google", False)
    reason, transient = hub._gemini_failure_reason(
        _FakeApiError(429, "RESOURCE_EXHAUSTED: quota"))
    assert transient and "rate-limited" in reason
    reason, transient = hub._gemini_failure_reason(_FakeApiError(
        400, "Manually set deadline 8s is too short. Minimum allowed deadline is 10s."))
    assert not transient and "timeout" in reason
    reason, transient = hub._gemini_failure_reason(_FakeApiError(404, "model not found"))
    assert not transient and "model" in reason


def test_a_rate_limited_probe_stops_and_says_so(monkeypatch):
    """Walking the fallback models after a 429 would only collect more 429s
    and hide the real reason behind a 404."""
    class _RateLimited(_GeminiClientSpy):
        def generate_content(self, *, model, contents, config):
            _GeminiClientSpy.calls.append({"model": model})
            if model != hub._GEMINI_PROBE_MODELS[0]:
                raise _FakeApiError(404, "model not found")
            raise _FakeApiError(429, "RESOURCE_EXHAUSTED")

    _install_fake_genai(monkeypatch)
    import sys
    sys.modules["google.genai"].Client = _RateLimited
    result = hub.probe("gemini", {"gemini_api_key": _GM_KEY})
    assert result["state"] == "unavailable"
    assert "rate-limited" in result["detail"]
    assert result["transient"] is True
    tried = [entry["model"] for entry in _RateLimited.calls if "model" in entry]
    assert tried == [hub._GEMINI_PROBE_MODELS[0]], "a rate limit must not walk the models"


def test_a_probe_that_raises_becomes_an_honest_failure_not_a_traceback(monkeypatch):
    monkeypatch.setattr(hub, "_probe_gemini",
                        lambda config, timeout: (_ for _ in ()).throw(
                            RuntimeError("connection reset")))
    result = hub.probe("gemini", {"gemini_api_key": "x"})
    assert result["state"] == "unavailable"
    assert "connection reset" in result["detail"]
    assert result["service"] == "gemini"


# ── optional packages ─────────────────────────────────────────────────────────

def test_instagram_says_which_package_is_missing(monkeypatch):
    monkeypatch.setattr(hub, "_has_module", lambda name: False)
    result = hub.probe("instagram", {"instagram_username": "u",
                                     "instagram_password": "p"})
    assert result["state"] == "unavailable"
    assert "instagrapi" in result["detail"]


def test_instagram_without_credentials_asks_for_them(monkeypatch):
    monkeypatch.setattr(hub, "_has_module", lambda name: True)
    monkeypatch.delenv("INSTAGRAM_PASSWORD", raising=False)
    monkeypatch.delenv("INSTAGRAM_SESSION", raising=False)
    result = hub.probe("instagram", {})
    assert result["state"] == "missing_key"


def test_a_saved_instagram_session_is_recognised(monkeypatch):
    monkeypatch.setattr(hub, "_has_module", lambda name: True)
    with tempfile.TemporaryDirectory(prefix="jarvis-ig-") as folder:
        session = Path(folder) / "session.json"
        session.write_text(json.dumps({"username": "sir"}), encoding="utf-8")
        result = hub.probe("instagram", {"instagram_session": str(session)})
    assert result["state"] == "ready"
    assert "sir" in result["detail"]


def test_smart_home_reports_a_device_that_answers_on_the_lan(monkeypatch):
    monkeypatch.setattr(hub, "_has_module", lambda name: True)
    monkeypatch.setattr(hub.socket, "create_connection",
                        lambda address, timeout=0: socket.socket())
    result = hub.probe("smart_home", {"smart_home_hosts": "10.0.0.5,10.0.0.6"})
    assert result["state"] == "ready"
    assert result["reachable"] == 2


def test_smart_home_reports_a_device_that_does_not_answer(monkeypatch):
    monkeypatch.setattr(hub, "_has_module", lambda name: True)

    def _refused(address, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(hub.socket, "create_connection", _refused)
    result = hub.probe("smart_home", {"smart_home_hosts": "10.0.0.9"})
    assert result["state"] == "unavailable"
    assert "10.0.0.9" in result["detail"]


def test_the_document_engine_probe_counts_what_is_installed():
    result = hub.probe("documents", {})
    assert result["state"] in ("ready", "unavailable")
    assert "document engines are ready" in result["detail"]


def test_the_connect_probe_always_answers_something(monkeypatch):
    monkeypatch.setattr(hub.socket, "create_connection",
                        lambda address, timeout=0: socket.socket())
    result = hub.probe("connect", {"connect_pairing_secret": "shh"})
    assert result["state"] in ("ready", "missing_key", "unavailable", "disabled")
    assert result["detail"]
    assert result["ok"] is True or result["state"] != "ready"


def test_the_summary_lists_every_service():
    text = hub.summary({})
    for service in hub.SERVICES:
        assert service.label in text


# ── the tool ─────────────────────────────────────────────────────────────────

def test_the_tool_lists_every_link_for_the_assistant():
    out = hub_tool.handler({"action": "status"})
    assert "SERVICE LINKS" in out
    assert "Spotify" in out


def test_the_tool_tests_one_service_and_names_it():
    out = hub_tool.handler({"action": "test", "service": "documents"})
    assert out.startswith("Document engines:")
    assert "document engines are ready" in out


def test_the_tool_asks_which_link_to_test():
    out = hub_tool.handler({"action": "test"})
    assert "spotify" in out


def test_the_tool_refuses_an_unknown_action():
    out = hub_tool.handler({"action": "reboot"})
    assert "'reboot' is not a service-hub action" in out
