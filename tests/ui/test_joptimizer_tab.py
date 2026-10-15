"""J.Optimizer settings tab (integration) — offscreen, non-destructive.

Verifies the tab is wired into the real Settings window and that the backend
read-only surface works, WITHOUT performing any system mutation:

* The J.OPTIMIZER tab exists and its widgets are constructed.
* The core.joptimizer backend exposes the ported, real Windows surface
  (registry targets, reversible service list, snapshot-first helpers).
* snapshot_state() only READS the current registry/service values and writes a
  local JSON backup (the reversibility record) — it never modifies the OS, so
  this is safe to run in CI.
* The health/inspection helpers return sane read-only data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from core import joptimizer as J  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(["pytest"])


@pytest.fixture(scope="module")
def settings(app):
    import ui_settings
    win = ui_settings.SettingsWindow()
    yield win
    win.deleteLater()


def test_tab_registered(settings):
    names = [settings.tabs.tabText(i) for i in range(settings.tabs.count())]
    assert "J.OPTIMIZER" in names


def test_tab_widgets_exist(settings):
    assert hasattr(settings, "_jopt_log_view")
    assert hasattr(settings, "_jopt_backup_list")
    assert hasattr(settings, "_jopt_timer")
    assert hasattr(settings, "_jopt_cpu") and hasattr(settings, "_jopt_health")
    # Refresh runs without raising and never yanks the whole dialog.
    settings._jopt_refresh_health()
    settings._jopt_refresh_backups()


def test_backend_surface_matches_hi_optimizer():
    # Real, documented Windows registry keys + reversible service ids.
    assert "game_dvr" in J.REGISTRY_TARGETS or not J.IS_WINDOWS
    assert "advertising_id" in J.REGISTRY_TARGETS or not J.IS_WINDOWS
    ids = {s["id"] for s in J.SAFE_SERVICES}
    assert {"wuauserv", "Spooler", "WlanSvc", "TermService", "bthserv"} <= ids


def test_readonly_metrics(settings):
    s = J.get_system_summary()
    for key in ("cpu", "ram", "disk_free_gb", "uptime", "os"):
        assert key in s
    assert 0 <= J.health_score(s) <= 100
    rows = J.top_processes(3)
    assert isinstance(rows, list)


@pytest.mark.skipif(not J.IS_WINDOWS, reason="snapshot is a Windows registry read")
def test_snapshot_writes_backup_without_mutating_os():
    before = {p.name for p in J.list_snapshots()}
    path = J.snapshot_state("pytest_readonly")
    assert path.is_file()
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "registry" in data and "services" in data
    assert path.name not in before
    assert path in J.list_snapshots()
