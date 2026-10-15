"""Hybrid-voice test isolation.

These tests build real ``ui_settings`` widgets, so they need a ``QApplication``.
Until this file existed they each created one inside a test or a function-scoped
fixture — ``app = QApplication.instance() or QApplication([])`` in a local
variable. PyQt6 gives Python ownership of the C++ application, so that local was
the last reference: the application was destroyed when the test ended, and the
next Qt call in the session (a later directory's widget, timer or paint) then
took the whole process down with a native fail-fast — exit ``0xC0000409``, no
traceback, no pytest summary, every remaining test silently skipped.

Requesting ``shared_qapplication`` here, for every test in this directory, makes
the session's single application alive before the first widget is built and
keeps it alive until the interpreter ends.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _session_qapplication(shared_qapplication):
    return shared_qapplication
