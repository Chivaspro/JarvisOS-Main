"""Long-term identity + learned rules: persistence and prompt injection.

The two files these modules own (``memory/identity.json`` and
``memory/rules.json``) are redirected into a temporary directory, so the tests
never touch a real profile.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from memory import identity, rules


@pytest.fixture(autouse=True)
def _sandbox_profile(tmp_path_workaround, monkeypatch):
    monkeypatch.setattr(identity, "_path", lambda: tmp_path_workaround / "identity.json")
    monkeypatch.setattr(rules, "_path", lambda: tmp_path_workaround / "rules.json")
    # An untouched profile would otherwise inherit the names already stored in
    # this machine's real config, which is right in production and noise here.
    monkeypatch.setattr(identity, "_legacy_names", lambda: ("", ""))


@pytest.fixture()
def tmp_path_workaround():
    with tempfile.TemporaryDirectory(prefix="jarvis-profile-") as folder:
        yield Path(folder)


# ── identity ──────────────────────────────────────────────────────────────────

def test_defaults_are_sane():
    profile = identity.load()
    assert profile["assistant_name"] == "JARVIS"
    assert profile["owner_name"] == ""
    assert profile["setup_complete"] is False
    assert identity.is_setup_complete() is False


def test_profile_round_trips_through_disk():
    assert identity.set_field("owner_name", "Sir")
    assert identity.set_field("owner_role", "Engineer")
    assert identity.set_field("owner_location", "Kolkata")
    assert identity.set_field("owner_interests", "aviation, cricket , AI")
    profile = identity.load()
    assert profile["owner_name"] == "Sir"
    assert profile["owner_interests"] == ["aviation", "cricket", "AI"]


def test_setup_complete_needs_a_name_or_instructions():
    identity.set_field("owner_name", "Sir")
    assert identity.mark_setup_complete(True) is True
    assert identity.is_setup_complete() is True


def test_prompt_block_carries_identity_and_instructions():
    identity.save({"owner_name": "Sir", "owner_role": "Engineer",
                   "owner_location": "Kolkata", "owner_interests": ["aviation"],
                   "custom_instructions": "Always answer in metric.",
                   "proactive": False, "shared_computer": True})
    block = identity.prompt_block()
    assert "Sir" in block
    assert "Engineer" in block
    assert "Kolkata" in block
    assert "aviation" in block
    assert "Always answer in metric." in block
    assert "shared" in block.lower()
    assert "silent" in block.lower()


def test_prompt_block_is_empty_for_a_blank_profile():
    assert identity.prompt_block() == ""


def test_custom_instructions_are_bounded():
    identity.set_field("custom_instructions", "x" * 5000)
    assert len(identity.load()["custom_instructions"]) <= 1200


def test_summary_mentions_the_owner():
    identity.save({"owner_name": "Sir", "owner_role": "Engineer"})
    text = identity.summary()
    assert "Sir" in text and "Engineer" in text


# ── learned rules ─────────────────────────────────────────────────────────────

def test_a_rule_is_remembered_once():
    ok, message = rules.add("always call me sir")
    assert ok, message
    ok2, message2 = rules.add("Always call me sir")
    assert ok2
    assert "already" in message2.lower()
    assert len(rules.list_rules()) == 1


def test_short_rules_are_refused():
    ok, message = rules.add("hi")
    assert not ok
    assert "too short" in message.lower()


def test_rules_appear_in_the_prompt_block():
    rules.add("never read my messages out loud")
    block = rules.prompt_block()
    assert "LEARNED RULES" in block
    assert "never read my messages out loud" in block


def test_forget_by_index_and_by_text():
    rules.add("always answer in metric")
    rules.add("never use emoji")
    ok, _ = rules.forget("1")
    assert ok
    assert [r["rule"] for r in rules.list_rules()] == ["never use emoji"]
    ok2, _ = rules.forget("emoji")
    assert ok2
    assert rules.list_rules() == []


def test_forgetting_something_absent_is_reported():
    ok, message = rules.forget("nothing like this exists")
    assert not ok
    assert "no rule" in message.lower()


def test_rule_list_is_capped():
    for index in range(75):
        rules.add(f"rule number {index}")
    assert len(rules.list_rules()) <= 60
    # The newest survive; the oldest are dropped.
    assert rules.list_rules()[-1]["rule"] == "rule number 74"
