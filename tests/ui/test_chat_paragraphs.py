"""Chat log behaviour: long-reply paragraph formatting and file import.

`format_chat_paragraphs` is a pure display transform (the spoken reply is not
touched), so it is asserted directly. The import path is exercised through the
real CommandWindow with a stub host, using the same handler the Import button
and a drag-and-drop both call.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QFrame, QTextEdit  # noqa: E402


@pytest.fixture(scope="module")
def ui_mod():
    """Load the root ``ui.py`` explicitly (``import ui`` resolves to tests/ui)."""
    spec = importlib.util.spec_from_file_location("ui_impl_chat_tests", ROOT / "ui.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ── paragraph formatting ──────────────────────────────────────────────────────

LONG_REPLY = (
    "Understood, sir. Applying the necessary fixes to the Gmail automation "
    "scripts now. It appears I encountered a temporary technical difficulty in "
    "executing the fix. I shall re-attempt the process directly. Sir, I am "
    "unable to proceed with the automated fix at this moment. It may require "
    "manual intervention or resource confirmation. Would you prefer me to "
    "investigate the system diagnostics?"
)


def test_the_reported_long_reply_becomes_separate_paragraphs(ui_mod):
    out = ui_mod.format_chat_paragraphs(LONG_REPLY)
    paras = out.split("\n\n")
    assert len(paras) >= 3
    # The short acknowledgement is its own paragraph.
    assert paras[0] == "Understood, sir."
    assert paras[1].startswith("Applying the necessary fixes to the Gmail automation scripts now.")
    # The closing question stays with its lead-in and ends the reply.
    assert paras[-1].endswith("investigate the system diagnostics?")


def test_a_short_reply_is_left_alone(ui_mod):
    short = "Understood, sir. Everything is in order."
    assert ui_mod.format_chat_paragraphs(short) == short


def test_existing_newlines_are_respected(ui_mod):
    already = "Line one.\nLine two."
    assert ui_mod.format_chat_paragraphs(already) == already


def test_code_blocks_are_never_reflowed(ui_mod):
    code = "Here you go.\n```\nprint('hi')\n```\nDone."
    assert ui_mod.format_chat_paragraphs(code) == code


def test_decimals_are_not_treated_as_sentence_boundaries(ui_mod):
    text = (
        "The value is 3.14 and that is final. Now let me walk through the rest "
        "of the calculation for you. Then we check the second result as well. "
        "Finally I shall report back to you with everything."
    )
    out = ui_mod.format_chat_paragraphs(text)
    assert "3.14" in out
    assert "3.\n\n14" not in out


def test_empty_text_is_empty(ui_mod):
    assert ui_mod.format_chat_paragraphs("") == ""
    assert ui_mod.format_chat_paragraphs(None) == ""


# ── import button + drag-and-drop ─────────────────────────────────────────────

class _StubHost:
    def __init__(self):
        self.imported = []

    def import_file(self, path):
        self.imported.append(path)


class _BareCommandWindow:
    """A CommandWindow without the heavy _build, for handler-level tests.

    ``append_msg`` is replaced with a recorder so the assertions are
    deterministic and do not depend on the typewriter timer or a config file.
    """

    @staticmethod
    def make(ui_mod, host):
        win = ui_mod._CommandWindow.__new__(ui_mod._CommandWindow)
        QFrame.__init__(win, None)
        win._host = host
        win.chat = QTextEdit(win)
        win.chat.setReadOnly(True)
        win.chat.setAcceptDrops(True)
        win.chat.installEventFilter(win)
        win.messages = []
        win.append_msg = lambda who, text, you=False: win.messages.append((who, text))
        return win


def test_import_file_attaches_and_previews_through_the_host(ui_mod, app, tmp_path):
    host = _StubHost()
    win = _BareCommandWindow.make(ui_mod, host)
    target = tmp_path / "notes.txt"
    target.write_text("hello")
    win.import_file(str(target))
    assert host.imported == [str(target)]
    assert any("notes.txt" in text for _who, text in win.messages)


def test_import_file_refuses_a_non_file(ui_mod, app, tmp_path):
    host = _StubHost()
    win = _BareCommandWindow.make(ui_mod, host)
    win.import_file(str(tmp_path / "nope.txt"))
    assert host.imported == []


def _drop_event(mime, kind):
    try:
        if kind == "drag":
            return QDragEnterEvent(QPointF(4, 4).toPoint(), Qt.DropAction.CopyAction,
                                   mime, Qt.MouseButton.LeftButton,
                                   Qt.KeyboardModifier.NoModifier)
        return QDropEvent(QPointF(4, 4), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    except TypeError:
        return None


def test_dropping_a_file_on_the_chat_imports_it(ui_mod, app, tmp_path):
    host = _StubHost()
    win = _BareCommandWindow.make(ui_mod, host)
    target = tmp_path / "dropped.txt"
    target.write_text("data")

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])

    enter = _drop_event(mime, "drag")
    if enter is None:
        pytest.skip("QDropEvent signature differs on this Qt build")
    assert win.eventFilter(win.chat, enter) is True

    drop = _drop_event(mime, "drop")
    assert win.eventFilter(win.chat, drop) is True
    assert host.imported == [str(target)]


def test_dropping_a_non_url_payload_is_ignored(ui_mod, app):
    host = _StubHost()
    win = _BareCommandWindow.make(ui_mod, host)
    mime = QMimeData()
    mime.setText("just some text")
    drop = _drop_event(mime, "drop")
    if drop is None:
        pytest.skip("QDropEvent signature differs on this Qt build")
    win.eventFilter(win.chat, drop)
    assert host.imported == []
