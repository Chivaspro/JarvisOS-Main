"""The conversation store: J.A.R.V.I.S.'s one authoritative chat history.

These are pure-Python tests (no Qt, no network) over a throwaway SQLite file.
They pin the contracts that matter:

* a message is committed the moment it arrives — durability, not batching;
* a conversation is named from the first real user message, never "Chat 2";
* search finds a chat by its title *and* by what was actually said;
* the branching columns exist from day one even though the first release is
  linear;
* export/import round-trips, and a malformed file is reported, not swallowed.
"""
from __future__ import annotations

import pytest

from memory.conversations import (
    ConversationStore,
    auto_title,
    conversation_to_markdown,
)


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


# ── durability ───────────────────────────────────────────────────────────────

def test_a_message_is_committed_the_moment_it_arrives(store):
    conv = store.new_conversation()
    store.add_message(conv["id"], "user", "hello")
    # A completely separate handle sees the row without any flush/close — that
    # is the whole point of writing the message immediately.
    reopened = ConversationStore(store._path)
    assert [m["text"] for m in reopened.messages(conv["id"])] == ["hello"]


def test_the_store_survives_reopening(store):
    conv = store.new_conversation()
    store.add_message(conv["id"], "user", "durable")
    store.set_last_active(conv["id"])
    store.close()
    reopened = ConversationStore(store._path)
    assert reopened.last_active_id() == conv["id"]
    assert [m["text"] for m in reopened.messages(conv["id"])] == ["durable"]


def test_blank_messages_are_refused(store):
    conv = store.new_conversation()
    assert store.add_message(conv["id"], "user", "   ") == 0
    assert store.messages(conv["id"]) == []


def test_flush_persists_batched_metadata(store):
    conv = store.new_conversation()
    store.add_message(conv["id"], "user", "hi")
    assert store._pending_meta           # the metadata update is batched…
    store.flush()
    assert store._pending_meta == {}     # …and flush clears it


# ── naming ───────────────────────────────────────────────────────────────────

def test_a_conversation_is_named_from_its_first_real_message(store):
    conv = store.new_conversation()
    text = "jarvis, can you help me build a PyQt6 music player?"
    store.add_message(conv["id"], "user", text)
    assert store.maybe_name(conv["id"], text) == "Help me Build a PyQt6 Music Player"
    assert store.conversation(conv["id"])["title"] == "Help me Build a PyQt6 Music Player"
    # A second message never renames it.
    assert store.maybe_name(conv["id"], "something else entirely") == \
        "Help me Build a PyQt6 Music Player"


@pytest.mark.parametrize("text,expected", [
    ("", ""),
    ("hey jarvis, what is the weather in Tokyo?", "What Is the Weather in Tokyo"),
    ("please refactor the parser", "Refactor the Parser"),
    ("I want you to check my MCP servers", "Check my MCP Servers"),
])
def test_auto_title_is_human_not_a_counter(text, expected):
    assert auto_title(text) == expected


# ── search ───────────────────────────────────────────────────────────────────

def test_search_finds_a_chat_by_title_or_by_what_was_said(store):
    a = store.new_conversation()
    store.add_message(a["id"], "user", "plot Tokyo on the globe")
    b = store.new_conversation()
    store.add_message(b["id"], "user", "what is the weather")
    store.set_title(a["id"], "Globe work")
    assert [r["id"] for r in store.list_conversations(query="tokyo")] == [a["id"]]
    assert [r["id"] for r in store.list_conversations(query="Globe")] == [a["id"]]
    assert [r["id"] for r in store.list_conversations(query="weather")] == [b["id"]]


def test_the_summary_carries_a_preview_and_a_count(store):
    conv = store.new_conversation()
    store.add_message(conv["id"], "user", "first thing I said")
    store.add_message(conv["id"], "assistant", "a reply")
    row = store.list_conversations()[0]
    assert row["message_count"] == 2
    assert row["preview"] == "first thing I said"


# ── branching-ready schema ───────────────────────────────────────────────────

def test_branching_columns_exist_from_day_one(store):
    conv = store.new_conversation()
    first = store.add_message(conv["id"], "user", "root")
    second = store.add_message(conv["id"], "assistant", "reply",
                               parent_id=first, branch="alt")
    rows = store.messages(conv["id"])
    assert rows[0]["parent_id"] is None and rows[0]["branch"] is None
    assert rows[1]["parent_id"] == first and rows[1]["branch"] == "alt"
    assert second > first


# ── export / import ──────────────────────────────────────────────────────────

def test_export_markdown_and_json_round_trip(store):
    conv = store.new_conversation()
    store.add_message(conv["id"], "user", "hello")
    store.add_message(conv["id"], "assistant", "hi there")
    store.set_title(conv["id"], "Greeting")

    md = store.export_conversation(conv["id"], "markdown")
    assert md.startswith("# Greeting")
    assert "**You:**" in md and "**J.A.R.V.I.S.:**" in md

    js = store.export_conversation(conv["id"], "json")
    new_id = store.import_conversation(js)
    assert new_id and new_id != conv["id"]
    assert [m["text"] for m in store.messages(new_id)] == ["hello", "hi there"]
    assert store.conversation(new_id)["title"] == "Greeting"


def test_a_bad_import_is_reported_not_swallowed(store):
    assert store.import_conversation("{not json") is None
    assert "could not be read" in store.last_error
    assert store.import_conversation('{"nope": 1}') is None
    assert store.last_error == "that file is not a conversation export"


def test_conversation_to_markdown_is_readable(store):
    conv = store.new_conversation()
    store.add_message(conv["id"], "user", "one")
    text = conversation_to_markdown(store.conversation(conv["id"]),
                                    store.messages(conv["id"]))
    assert text.startswith("# ") and "one" in text


# ── resume ───────────────────────────────────────────────────────────────────

def test_resume_target_is_the_last_active_conversation(store):
    a = store.new_conversation()
    store.add_message(a["id"], "user", "first")
    b = store.new_conversation()
    store.add_message(b["id"], "user", "second")
    store.set_last_active(a["id"])
    row = store.resume_target()
    assert row["id"] == a["id"]
    assert [m["text"] for m in row["messages"]] == ["first"]
    # With no explicit last-active, the most recently updated chat wins.
    store.set_last_active("")
    assert store.resume_target()["id"] == b["id"]


def test_resume_target_is_none_when_history_is_empty(store):
    assert store.resume_target() is None


# ── deletion ─────────────────────────────────────────────────────────────────

def test_delete_and_clear_remove_messages_too(store):
    a = store.new_conversation()
    store.add_message(a["id"], "user", "one")
    b = store.new_conversation()
    store.add_message(b["id"], "user", "two")
    store.delete_conversation(a["id"])
    assert store.conversation(a["id"]) is None
    assert store.messages(a["id"]) == []
    assert store.clear_all() == 1
    assert store.count() == 0 and store.list_conversations() == []
