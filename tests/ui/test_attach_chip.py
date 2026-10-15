"""Pending-attachment chip above the Chat Panel input box (offscreen).

When a file is imported (button) or dropped, the live Chat Panel must show the
real filename right above the input box so the user can confirm the attach
registered. This exercises the actual ``_set_attached`` / ``_clear_attached`` /
``_detach_clicked`` methods on a minimal instance (the chip's own child widgets,
not the whole heavy window) so it stays fast and crash-free offscreen.
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

from PyQt6.QtWidgets import QLabel, QWidget  # noqa: E402

from .qt_harness import qapp  # noqa: F401,E402

_spec = importlib.util.spec_from_file_location("ui_impl_attach_chip_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ui
_spec.loader.exec_module(ui)


class _ChipHost:
    """Mimics MainWindow's real API: `current_file` is a read-only property
    backed by `_current_file` (the attribute the chip code clears)."""
    def __init__(self):
        self._current_file = None
        self.logs = []

    @property
    def current_file(self):
        return self._current_file

    def write_log(self, text):
        self.logs.append(str(text))


def _make_win():
    """A RefinedCommandWindow instance WITHOUT the full constructor: only the
    attributes the chip methods touch are provided (child QWidgets + host)."""
    w = ui.RefinedCommandWindow.__new__(ui.RefinedCommandWindow)
    w._host = _ChipHost()
    w._attached_file = None
    w._attach_bar = QWidget()
    w._attach_bar.setVisible(False)
    w._attach_name_lbl = QLabel()
    w._attach_size_lbl = QLabel()
    return w


def test_set_attached_shows_the_real_name(qapp, tmp_path):
    f = tmp_path / "Quarterly Report.pdf"
    f.write_bytes(b"x" * 2048)
    w = _make_win()
    assert w._attach_bar.isVisible() is False
    ui.RefinedCommandWindow._set_attached(w, str(f))
    assert w._attach_bar.isVisible() is True
    assert "Quarterly Report" in w._attach_name_lbl.text()
    assert w._attach_name_lbl.toolTip() == str(f)
    assert w._attached_file == str(f)
    assert w._attach_size_lbl.text().strip() != ""     # real size shown, not "?"


def test_clear_attached_hides_chip(qapp, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    w = _make_win()
    ui.RefinedCommandWindow._set_attached(w, str(f))
    ui.RefinedCommandWindow._clear_attached(w)
    assert w._attach_bar.isVisible() is False
    assert w._attached_file is None
    assert w._attach_name_lbl.text() == ""


def test_detach_clicked_clears_host_active_file(qapp, tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"0")
    w = _make_win()
    ui.RefinedCommandWindow._set_attached(w, str(f))
    w._host._current_file = str(f)                      # host mirrors the attach
    ui.RefinedCommandWindow._detach_clicked(w)
    assert w._attach_bar.isVisible() is False
    assert w._host.current_file is None                # detached at the host too
    assert any("removed" in line.lower() for line in w._host.logs)


def test_detach_leaves_a_different_host_file_untouched(qapp, tmp_path):
    attached = tmp_path / "a.txt"; attached.write_text("a")
    other = tmp_path / "b.txt";    other.write_text("b")
    w = _make_win()
    ui.RefinedCommandWindow._set_attached(w, str(attached))
    w._host._current_file = str(other)                  # host already moved on
    ui.RefinedCommandWindow._detach_clicked(w)
    assert w._attach_bar.isVisible() is False
    assert w._host.current_file == str(other)          # not cleared


def test_missing_file_still_shows_name_no_crash(qapp, tmp_path):
    ghost = tmp_path / "gone.zip"                       # does not exist
    w = _make_win()
    ui.RefinedCommandWindow._set_attached(w, str(ghost))
    assert w._attach_bar.isVisible() is True
    assert "gone.zip" in w._attach_name_lbl.text()
    assert w._attach_size_lbl.text() == ""              # size blanked, no crash
