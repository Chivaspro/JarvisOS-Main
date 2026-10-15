"""File attachment card in the Chat Panel (TASK 6) — offscreen.

The bug: dropped/imported files called append_msg('FILE', …), and the native
chat renderer FILTERS any sender that is neither You nor J.A.R.V.I.S., so the
attachment never appeared at all. The fix is a real rounded card that shows the
ACTUAL filename and the ACTUAL file type derived from the real extension.

Covers: the type mapping matches the request's examples; the card renders the
real name/type; it is inserted as a distinct chat row; long names stay bounded;
and multiple attachments remain separate.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QLabel  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_ui_spec = importlib.util.spec_from_file_location("ui_impl_attach_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)


@pytest.mark.parametrize("fname,expected", [
    ("Game.zip", "Zip Archive"),
    ("report.pdf", "PDF Document"),
    ("image.png", "PNG Image"),
    ("notes.txt", "Text Document"),
    ("project.py", "Python Source File"),
])
def test_type_description_matches_real_extension(fname, expected):
    assert ui._file_type_description(Path(fname)) == expected


def test_card_shows_real_name_and_type(qapp, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    card = ui._FileAttachmentCard(f, ui._fmt_size(f.stat().st_size))
    labels = [lbl.text() for lbl in card.findChildren(QLabel)]
    assert "report.pdf" in labels, f"real filename not shown: {labels}"
    assert any(t.startswith("PDF Document") for t in labels), f"real type not shown: {labels}"


def test_append_file_attachment_inserts_distinct_rows(qapp, tmp_path):
    log = ui._RoundedChatLog()
    log.resize(500, 600)
    base = len(log._rows)

    a = tmp_path / "notes.txt"; a.write_text("hi")
    b = tmp_path / "Game.zip"; b.write_bytes(b"PK\x03\x04")
    log.append_file_attachment(a)
    log.append_file_attachment(b)

    assert len(log._rows) == base + 2, "each attachment must be its own distinct row"
    assert all(isinstance(r, ui._FileAttachmentCard) for r in log._rows[-2:])


def test_long_filename_stays_bounded(qapp, tmp_path):
    long_name = "this_is_a_very_long_attachment_filename_that_should_not_break_the_layout_" + "x" * 60 + ".pdf"
    f = tmp_path / long_name
    f.write_bytes(b"%PDF")
    card = ui._FileAttachmentCard(f, ui._fmt_size(f.stat().st_size))
    assert card.maximumWidth() <= 300, "a long filename must not stretch the card"
    name_lbls = [lbl for lbl in card.findChildren(QLabel) if lbl.toolTip() == long_name]
    assert name_lbls, "full name must remain available via tooltip"
    # The visible text is elided (shorter than the full name) but never empty.
    shown = name_lbls[0].text()
    assert shown and len(shown) < len(long_name), "long name should be elided, not overflowed"
