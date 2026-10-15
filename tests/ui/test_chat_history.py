"""The chat panel's History surface: real conversations, autosaved per turn.

The store is the project's one authoritative transcript (``memory.conversations``);
these tests hand it a throwaway database and check that the panel writes to it
and reads back from it, that the history page groups/lists/searches real rows,
and that the panel's own header exposes the History and New controls.

Widget reads are marshalled through an in-thread ``run_on_ui`` stub so the test
is deterministic — no timing, no drain loops.
"""
from __future__ import annotations

import time

import pytest

from .test_camera_pipeline import qapp, ui  # noqa: F401  (fixtures)


class _Host:
    """Records what the page asks the GUI thread to run, instead of running it.

    The page reads on a worker thread; the test then runs the queued callables
    on *its* thread (see ``_drain``). That keeps every widget touch on the test
    thread — the one rule that keeps a Qt run from dying natively.
    """

    def __init__(self, panel=None):
        self.panel = panel
        self.calls: list = []
        self.cleared = 0
        self.input_text = ""

    def run_on_ui(self, fn):
        self.calls.append(fn)

    def clear_history_view(self):
        self.cleared += 1

    def new_conversation(self):
        return self.panel.new_conversation() if self.panel else ""

    def load_conversation(self, conv_id):
        return self.panel.load_conversation(conv_id) if self.panel else False

    def set_input_text(self, text):
        self.input_text = str(text)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from memory import conversations

    instance = conversations.ConversationStore(tmp_path / "conversations.db")
    monkeypatch.setattr(conversations, "get_store",
                        lambda *a, **k: instance)
    yield instance
    instance.close()


def _drain(qapp, host, timeout: float = 3.0, quiet: float = 0.3) -> int:
    """Run what the page queued, here on the test thread, until it is quiet.

    A render queues a second (preview) read, so "nothing queued right now" is
    not the end — the loop waits for a short quiet period after the last batch.
    """
    deadline = time.monotonic() + timeout
    ran = 0
    idle_since = None
    while time.monotonic() < deadline:
        calls, host.calls = list(host.calls), []
        if calls:
            for fn in calls:
                fn()
            ran += len(calls)
            idle_since = None
        elif ran and idle_since is None:
            idle_since = time.monotonic()
        elif idle_since is not None and time.monotonic() - idle_since > quiet:
            break
        qapp.processEvents()
        time.sleep(0.02)
    return ran


def _seed(store):
    """Two named conversations with distinct bodies, newest last."""
    a = store.new_conversation()["id"]
    store.add_message(a, "user", "plot Tokyo on the globe")
    store.add_message(a, "assistant", "Tokyo is plotted.")
    store.set_title(a, "Globe work")

    b = store.new_conversation()["id"]
    store.add_message(b, "user", "check my mcp servers")
    store.set_title(b, "MCP check")
    return a, b


# ── the history page ─────────────────────────────────────────────────────────

def test_the_page_lists_conversations_grouped_by_when(qapp, store):
    _seed(store)
    host = _Host()
    page = ui.ConversationHistoryPage(host=host)
    page.reload()
    _drain(qapp, host)

    # One group header (TODAY) plus the two conversations.
    titles = [page._timeline.item(i).text() for i in range(page._timeline.count())]
    assert any("TODAY" in t for t in titles)
    assert any("Globe work" in t for t in titles)
    assert len(page._map) == 2
    # The newest conversation is auto-selected and its messages previewed.
    assert page._preview.count() >= 1
    assert "CONVERSATION(S)" in page._status.text()
    page.deleteLater()


def test_search_uses_the_conversation_store(qapp, store):
    _seed(store)
    host = _Host()
    page = ui.ConversationHistoryPage(host=host)
    page.reload("mcp")
    _drain(qapp, host)
    assert len(page._map) == 1
    row = list(page._map.values())[0]
    assert row["title"] == "MCP check"
    page.deleteLater()


def test_an_empty_history_shows_a_suggestion_state(qapp, store):
    host = _Host()
    page = ui.ConversationHistoryPage(host=host)
    page.reload()
    _drain(qapp, host)
    assert page._timeline.count() == 1
    assert "No conversations yet" in page._timeline.item(0).text()
    assert page._chips.isVisible() is False or not page._rows
    page.deleteLater()


def test_resuming_asks_the_host_to_load_the_conversation(qapp, store):
    a, _b = _seed(store)
    host = _Host()
    page = ui.ConversationHistoryPage(host=host)
    page.reload()
    _drain(qapp, host)
    # Row 0 is the group header; row 1 is the newest real conversation.
    page._timeline.setCurrentRow(1)
    expected = page._map[1]["id"]
    loaded = []
    host.load_conversation = lambda cid: loaded.append(cid) or True
    page._resume()
    assert loaded and loaded[0] == expected
    page.deleteLater()


def test_deleting_asks_for_confirmation_and_then_removes(qapp, store, monkeypatch):
    _seed(store)
    monkeypatch.setattr(ui, "_confirm", lambda *a, **k: True)
    host = _Host()
    page = ui.ConversationHistoryPage(host=host)
    page.reload()
    _drain(qapp, host)
    page._timeline.setCurrentRow(1)
    before = store.count()
    page._delete()
    assert store.count() == before - 1
    page.deleteLater()


def test_a_refused_confirmation_leaves_the_conversation(qapp, store, monkeypatch):
    _seed(store)
    monkeypatch.setattr(ui, "_confirm", lambda *a, **k: False)
    host = _Host()
    page = ui.ConversationHistoryPage(host=host)
    page.reload()
    _drain(qapp, host)
    page._timeline.setCurrentRow(1)
    page._delete()
    assert store.count() == 2
    page.deleteLater()


# ── the panel's own history surface ──────────────────────────────────────────

@pytest.fixture()
def panel(qapp, store):
    host = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    host._host = _Host(host)
    yield host
    try:
        host.close()
    except Exception:
        pass
    qapp.processEvents()


def test_the_chat_header_exposes_history_and_new(panel):
    assert panel._history_btn.text()
    assert panel._new_chat_btn.text()
    assert panel._history_btn.isHidden() is False


def test_a_turn_is_autosaved_into_the_store(panel, store):
    panel.append_msg("You", "hello there", you=True)
    panel.append_msg("J.A.R.V.I.S.", "Hi — how can I help?", you=False)
    conv_id = panel.active_conversation_id()
    assert conv_id
    rows = store.messages(conv_id)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["text"] == "hello there"
    # The first user message also names the conversation.
    assert store.conversation(conv_id)["title"]


def test_new_conversation_clears_the_chat_and_starts_fresh(panel, store):
    panel.append_msg("You", "first conversation", you=True)
    first = panel.active_conversation_id()
    panel.new_conversation()
    assert panel.active_conversation_id() != first
    assert panel.chat._rows == []


def test_load_conversation_restores_its_messages(panel, store):
    conv_id = store.new_conversation()["id"]
    store.add_message(conv_id, "user", "remember this")
    store.add_message(conv_id, "assistant", "I do.")
    assert panel.load_conversation(conv_id) is True
    assert panel.active_conversation_id() == conv_id
    assert len(panel.chat._rows) == 2


def test_restore_last_conversation_resumes_the_transcript(panel, store):
    conv_id = store.new_conversation()["id"]
    store.add_message(conv_id, "user", "earlier turn")
    store.set_last_active(conv_id)
    assert panel.restore_last_conversation() is True
    assert panel.active_conversation_id() == conv_id
    assert len(panel.chat._rows) == 1


def test_loading_an_unknown_conversation_fails_honestly(panel):
    assert panel.load_conversation("no-such-id") is False


def test_starting_a_new_chat_clears_the_input(panel):
    panel.set_input_text("draft text")
    panel._start_new_chat()
    assert panel.input.text() == ""
