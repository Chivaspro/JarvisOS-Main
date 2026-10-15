"""Every credential is described once, and the description is complete.

The Settings window does not carry a table of key names, URLs and test targets:
it asks the hub. These tests are what keeps that single table honest — a key with
no row would be invisible on Quick Setup, a row with no URL would show a GET API
KEY button that opens nothing, and a row pointing at a probe that does not exist
would offer a TEST that cannot run.
"""
from __future__ import annotations

import pytest

from services import hub, tool as hub_tool

#: Fields the app actually reads, including the ones no Service owns.
KNOWN_FIELDS = (
    "gemini_api_key", "openrouter_api_key", "anthropic_api_key", "openai_api_key",
    "groq_api_key", "custom_ai_api_key", "fish_audio_api_key", "elevenlabs_api_key",
    "mem0_api_key", "qdrant_url", "qdrant_api_key", "spotify_client_id",
    "spotify_client_secret", "weather_api_key", "radar_api_key",
    "instagram_username", "instagram_password", "smart_home_hosts",
    "smart_home_username", "smart_home_password", "connect_pairing_secret",
    # The connector's project key. Listed here so the invariant holds: a
    # credential the app reads with no row here would be invisible on Quick
    # Setup and untestable from the API tab.
    "composio_api_key",
)


# ── the row table ─────────────────────────────────────────────────────────────

def test_every_credential_the_app_reads_has_a_row():
    described = {row["field"] for row in hub.key_targets()}
    missing = sorted(set(KNOWN_FIELDS) - described)
    assert not missing, f"no hub row for: {missing}"


def test_every_row_names_its_owner_and_its_group():
    for row in hub.key_targets():
        assert row["label"], row
        assert row["owner"], row
        assert row["group"] in ("AI", "VOICE", "MEMORY", "Services"), row
        assert row["summary"], row


def test_rows_are_ordered_by_setup_group():
    groups = [row["group"] for row in hub.key_targets()]
    order = {name: i for i, name in enumerate(("AI", "VOICE", "MEMORY", "Services"))}
    assert [order[g] for g in groups] == sorted(order[g] for g in groups)


def test_a_key_that_has_a_page_has_a_url_and_help_text():
    """GET API KEY opens `key_url`; a row with neither would show a dead button
    (and the UI only builds the button when a URL exists, so this pins the
    metadata the button depends on)."""
    for row in hub.key_targets():
        if row["key_url"]:
            assert row["key_url"].startswith("https://"), row
            assert row["key_help"], f"{row['field']} opens a page with no explanation"


def test_credentials_without_a_key_page_say_so_instead_of_guessing():
    """A device password, a self-invented pairing secret and a keyless endpoint
    have no page to open — they must explain that rather than carry a URL."""
    for field in ("smart_home_password", "connect_pairing_secret", "instagram_password",
                  "weather_api_key", "radar_api_key"):
        meta = hub.key_meta(field)
        assert not meta["key_url"], f"{field} claims to have a key page"
        assert meta["key_help"], f"{field} has nothing to tell the user"


def test_no_key_page_points_at_a_placeholder():
    for row in hub.key_targets():
        for url in (row["key_url"], row["help_url"]):
            assert "example.com" not in url, row
            assert "TODO" not in url, row


# ── the test target ───────────────────────────────────────────────────────────

def test_every_test_target_resolves_to_a_real_probe():
    for row in hub.key_targets():
        target = row["test"]
        if not target:
            continue
        if target in {service.id for service in hub.SERVICES}:
            assert callable(getattr(hub, hub._BY_ID[target].probe_name, None)), target
        else:
            provider = hub._PROVIDER_BY_ID[target]
            assert callable(getattr(hub, provider.probe_name, None)), target


def test_a_row_without_a_test_target_is_a_credential_nothing_can_verify():
    """The honest exceptions: a custom provider key (no fixed endpoint) and
    Mem0 (no read-only call). Both must still be described."""
    untested = {row["field"] for row in hub.key_targets() if not row["test"]}
    assert untested == {"custom_ai_api_key", "qdrant_url"} or untested == {"custom_ai_api_key"}


def test_probe_target_accepts_a_link_a_provider_and_nothing_else(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: pytest.fail("no request may run"))
    assert hub.probe_target("documents", {})["state"] in ("ready", "unavailable")
    assert hub.probe_target("mem0", {})["state"] == "key_optional"
    unknown = hub.probe_target("telegraph", {})
    assert unknown["state"] == "unknown"
    assert "openai" in unknown["detail"]


def test_a_provider_probe_reports_which_provider_it_tested(monkeypatch):
    monkeypatch.setattr(hub, "_get", lambda *a, **k: pytest.fail("no request may run"))
    result = hub.probe_target("groq", {})
    assert result["provider"] == "groq"
    assert result["label"] == "Groq"
    assert result["field"] == "groq_api_key"
    assert result["elapsed_s"] >= 0


# ── offline state ─────────────────────────────────────────────────────────────

def test_row_status_is_offline_and_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = hub.row_status("openai_api_key", {})
    assert state["state"] == "missing_key"
    assert "OpenAI" in state["detail"]
    assert state["ok"] is False


def test_row_status_knows_a_blank_qdrant_url_is_a_choice_not_a_problem(monkeypatch):
    monkeypatch.delenv("QDRANT_URL", raising=False)
    state = hub.row_status("qdrant_url", {})
    assert state["state"] == "key_optional"
    assert "localhost" in state["detail"]


def test_row_status_of_an_unknown_field_is_empty():
    assert hub.row_status("not_a_key", {}) == {}
    assert hub.key_meta("") == {}


def test_status_targets_covers_every_row(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: True)
    rows = hub.status_targets({})
    assert len(rows) == len(hub.key_targets())
    assert all(row["state"] for row in rows)


def test_a_switched_off_service_reports_its_credentials_as_disabled(monkeypatch):
    monkeypatch.setattr(hub, "_feature_on", lambda name: name != "spotify")
    rows = {row["field"]: row for row in hub.status_targets(
        {"spotify_client_id": "id", "spotify_client_secret": "secret"})}
    assert rows["spotify_client_id"]["state"] == "disabled"
    assert "switched off" in rows["spotify_client_id"]["detail"]


def test_provider_summary_names_every_provider():
    text = hub.provider_summary({})
    for provider in hub.PROVIDERS:
        assert provider.label in text
    assert "PROVIDER KEYS" in text


def test_the_summary_carries_both_links_and_keys():
    text = hub.summary({})
    assert "SERVICE LINKS" in text and "PROVIDER KEYS" in text


# ── the tool ──────────────────────────────────────────────────────────────────

def test_the_tool_can_name_a_key_by_its_config_field():
    out = hub_tool.handler({"action": "test", "field": "openai_api_key"})
    assert out.startswith("OpenAI:")
    assert "No OpenAI key is set" in out


def test_the_tool_lists_keys_when_asked_to_test_without_a_name():
    out = hub_tool.handler({"action": "test"})
    assert "Links:" in out and "Keys:" in out
    assert "openai" in out and "spotify" in out


def test_the_tool_still_refuses_an_unknown_action():
    out = hub_tool.handler({"action": "reboot"})
    assert "'reboot' is not a service-hub action" in out
    assert "Links:" in out


def test_the_tool_reports_an_unknown_target_instead_of_inventing_one():
    out = hub_tool.handler({"action": "test", "service": "telegraph"})
    assert "not a link or a provider" in out