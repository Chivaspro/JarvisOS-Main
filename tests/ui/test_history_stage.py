"""The chat archive stage — past conversations and the tasks running now.

Two contracts are worth a test each:

* the stage is a *view* of the archive J.A.R.V.I.S. already keeps (the brain
  database behind ``memory.service`` and ``brain.tasks``), so a test can hand it
  a stub and check that nothing else is invented;
* the reads happen off the GUI thread and the widgets are only touched through
  ``host.run_on_ui`` — the stub host here records the callables instead of
  running them, and the test runs them on its own thread.
"""
from __future__ import annotations

import re
import time

import pytest

from .test_camera_pipeline import _pump, qapp, ui  # noqa: F401  (fixtures)


STAMP = 1700000000.0          # 14 Nov 2023, 22:13 UTC — a fixed clock for rows


class _FakeDB:
    def __init__(self):
        self.searches: list[str] = []

    def search_messages_fts(self, phrase, limit=20):
        self.searches.append(str(phrase))
        return [{"id": 2, "role": "user", "content": f"what about {phrase}?",
                 "created_at": STAMP}]

    def recent_messages(self, limit=40):
        return [{"id": 1, "role": "assistant", "content": "Tokyo is plotted.",
                 "created_at": STAMP}]


class _FakeService:
    def __init__(self):
        self.db = _FakeDB()

    def recent_conversations(self, limit=8):
        return [{"id": 7, "title": "Globe work", "n": 3,
                 "started_at": STAMP, "ended_at": 0.0}]


class _FakeTracker:
    def list_active(self, limit=6):
        return "[1] plot Tokyo — step 2/3: place the marker"


class _Host:
    """Records what the stage asks the GUI thread to run."""

    def __init__(self):
        self.calls: list = []
        self.cleared = 0

    def run_on_ui(self, fn):
        self.calls.append(fn)

    def clear_history_view(self):
        self.cleared += 1


@pytest.fixture()
def archive(monkeypatch):
    """Stub the two stores the stage reads, and nothing else."""
    service = _FakeService()
    import memory.service as memory_service
    import brain.tasks as brain_tasks
    monkeypatch.setattr(memory_service, "get_memory_service", lambda: service)
    monkeypatch.setattr(brain_tasks, "get_task_tracker", lambda: _FakeTracker())
    return service


def _drain(qapp, host, timeout: float = 5.0) -> int:
    """Wait for the reader thread, then run what it queued, on this thread."""
    deadline = time.monotonic() + timeout
    while not host.calls and time.monotonic() < deadline:
        _pump(qapp, 0.05)
    calls, host.calls = list(host.calls), []
    for fn in calls:
        fn()
    return len(calls)


# ── the timestamp helper ─────────────────────────────────────────────────────

def test_timestamps_are_formatted_and_junk_is_refused():
    # The clock is local, so only the shape is asserted — "15 Nov 06:13".
    assert re.fullmatch(r"\d{2} [A-Z][a-z]{2} \d{2}:\d{2}",
                        ui._history_time(STAMP)), ui._history_time(STAMP)
    for junk in (None, "", "yesterday", 0, -5):
        assert ui._history_time(junk) == ""


# ── the stage reads the real stores ──────────────────────────────────────────

def test_the_stage_lists_the_archive_rows(qapp, archive):
    host = _Host()
    stage = ui.HistoryStage(host=host)
    stage.reload("globe")
    assert _drain(qapp, host) == 1, "the read must come back through the GUI thread"

    assert stage._conversations.count() == 1
    assert "Globe work" in stage._conversations.item(0).text()
    assert "3 message(s)" in stage._conversations.item(0).text()
    assert "what about globe?" in stage._messages.item(0).text()
    assert stage._messages.item(0).toolTip() == "what about globe?"
    assert archive.db.searches == ["globe"], "a search must use the archive's index"
    assert "1 MESSAGE(S)" in stage._status.text()
    assert "plot Tokyo" in stage._tasks.toPlainText()
    stage.deleteLater()


def test_a_search_with_no_hits_says_so_instead_of_showing_nothing(qapp, archive):
    class _Empty(_FakeDB):
        def search_messages_fts(self, phrase, limit=20):
            return []

    archive.db = _Empty()
    host = _Host()
    stage = ui.HistoryStage(host=host)
    stage.reload("unicorn")
    _drain(qapp, host)
    assert stage._messages.count() == 0
    assert "NOTHING IN THE ARCHIVE MATCHES" in stage._status.text()
    stage.deleteLater()


def test_a_broken_archive_is_reported_not_hidden(qapp, monkeypatch):
    import memory.service as memory_service

    def _boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(memory_service, "get_memory_service", _boom)
    host = _Host()
    stage = ui.HistoryStage(host=host)
    stage.reload()
    _drain(qapp, host)
    assert "DATABASE IS LOCKED" in stage._status.text()
    stage.deleteLater()


def test_selecting_a_conversation_shows_its_real_columns(qapp, archive):
    host = _Host()
    stage = ui.HistoryStage(host=host)
    stage.reload()
    _drain(qapp, host)
    stage._conversations.setCurrentRow(0)
    shown = stage._tasks.toPlainText()
    assert "Conversation #7" in shown
    assert "still open" in shown, "an open conversation must not claim an end time"
    assert "Title    Globe work" in shown
    stage.deleteLater()


def test_close_asks_the_host_to_return_to_the_reactor(qapp, archive):
    host = _Host()
    stage = ui.HistoryStage(host=host)
    stage._close()
    assert host.cleared == 1
    stage.deleteLater()


# ── the chat panel it belongs to ─────────────────────────────────────────────

@pytest.fixture()
def panel(qapp):
    """The chat panel itself — the surface that owns the archive stage.

    Deliberately built on its own rather than through ``MainWindow``: the HUD is
    not what this file is about, and building one here would put every assertion
    behind a window teardown that this machine's offscreen compositor does not
    always survive (a native access violation, which reports nothing and hides
    the result of every test after it).
    """
    host = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    yield host
    try:
        host.close()
    except Exception:
        pass
    qapp.processEvents()


def test_the_archive_is_a_page_of_the_visual_stack(panel):
    """The stage is wired into the panel's own page stack, not a spare window."""
    visual = panel._vision_visual
    assert visual.indexOf(panel._history_stage) >= 0, (
        "the archive must be one of the pages the reactor can switch to")
    assert visual.indexOf(panel._logo_page) >= 0
    assert visual.currentWidget() is panel._logo_page, (
        "the reactor stays front-most until the archive is asked for")
    assert callable(panel.show_history)
    assert callable(panel.clear_history_view)


def test_show_history_fronts_the_stage_and_close_returns_to_the_reactor(panel, archive, qapp):
    assert panel.show_history() is True
    _pump(qapp, 0.4)
    assert panel._vision_visual.currentWidget() is panel._history_stage
    # The panel itself is never shown in a test, so a shown child reports
    # isVisible() False; isHidden() is what distinguishes shown from hidden.
    assert not panel._history_stage.isHidden()

    panel.clear_history_view()
    _pump(qapp, 0.4)
    assert panel._vision_visual.currentWidget() is panel._logo_page
    assert panel._history_stage.isHidden()
