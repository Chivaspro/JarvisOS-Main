"""Shared pytest setup for the J.A.R.V.I.S. test tree.

Two environment decisions, both about letting a headless run finish:

* ``QT_QPA_PLATFORM=offscreen`` — the UI tests build real PyQt6 widgets and
  assert on geometry/paint state; they never need a display server.
* ``JARVIS_DISABLE_WEBENGINE=1`` — importing QtWebEngine initialises Chromium,
  which hard-aborts (an access violation at process teardown, not a Python
  exception) when it cannot host a GPU/compositor context. Every WebEngine
  surface in the app already degrades gracefully when ``_WEB_OK`` is False, so
  the tests exercise the same code paths the fallback uses.

``sys.path`` is set up here so ``import ui`` / ``import main`` work from any
test directory without each module re-implementing the bootstrap.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")


@pytest.fixture(scope="session")
def shared_qapplication():
    """The one ``QApplication`` for this session, owned here for its whole life.

    PyQt6 destroys the C++ ``QApplication`` the moment the last Python reference
    to it disappears, and the next Qt call then ends the process with a native
    fail-fast (exit code ``0xC0000409``): no traceback, no pytest summary, the
    run simply stops mid-file and every test after that point silently never
    ran. The test directories used to create the application in a local —
    ``app = QApplication.instance() or QApplication([])`` inside a test or a
    function-scoped fixture — so the only reference died with that test and the
    damage landed on whichever later test next built a widget.

    Holding it here (requested directly, or through the autouse hooks the test
    directories declare) keeps exactly one application alive from the first Qt
    test to the end of the session.
    """
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:                 # Qt is optional for the pure-Python tests
        yield None
        return
    app = QApplication.instance()
    if app is None:
        # Non-empty argv (Chromium refuses an empty command line) but WITHOUT
        # pytest's own switches, which Chromium would try to parse.
        app = QApplication([sys.argv[0] if sys.argv else "pytest"])
    yield app
    # Deliberately not deleted: Qt is torn down by interpreter exit, which is
    # the only place that teardown is safe.
