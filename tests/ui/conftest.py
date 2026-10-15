"""UI test isolation.

Every HUD test builds a real ``MainWindow``. Its production ``closeEvent``
deliberately ends the whole process (``stability.request_process_exit`` →
``force_exit`` → ``os._exit``) because closing the Chat Panel must not leave an
invisible orphan behind.

That is correct in the app and fatal to a test session: the first
``win.close()`` in a module killed pytest itself, so every test after the first
MainWindow-using one never ran and the file reported no summary at all (a
partial dot line and a bare exit 0). The exit is therefore disabled for this
directory only; the shutdown code path itself still runs, so anything the
window does on close — stopping camera workers, releasing devices — is still
exercised.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _session_qapplication(shared_qapplication):
    """Keep the session's single QApplication alive for every HUD test.

    ``qt_harness.qapp`` returns the same object, but several files in this
    directory create it locally instead; without a session-long owner the last
    reference dies with that test and PyQt6 destroys the C++ application, which
    aborts the process natively on the next Qt call (exit ``0xC0000409``).
    """
    if shared_qapplication is None:
        pytest.skip("PyQt6 is not installed")
    return shared_qapplication


@pytest.fixture(autouse=True)
def _no_process_exit(monkeypatch):
    """Make the shutdown coordinator's hard exit a no-op for UI tests."""
    try:
        from core import stability
    except Exception:
        yield
        return
    for name in ("request_process_exit", "force_exit"):
        monkeypatch.setattr(stability, name, lambda *a, **k: None, raising=False)
    yield
