"""The Mind panel — the Chat Panel's renamed tab and its four sub-tabs.

The panel was the "Memory Core": a single list with a 3D orb. It is now MIND,
with MEMORIES / BELIEFS / DREAMS / NEURAL MAP — and the map is the 2D graph
only. These tests assert the surface exists, that each view is fed by the real
stores, and that the map is built on demand rather than at construction.

Every test runs against a throwaway SQLite file + JSON store, so the real user
memory on disk is never touched.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QTabWidget  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent

# ``tests/ui`` is a package named ``ui``, so load the app module under an alias.
_ui_spec = importlib.util.spec_from_file_location("ui_impl_mind_tests", ROOT / "ui.py")
ui = importlib.util.module_from_spec(_ui_spec)
sys.modules[_ui_spec.name] = ui
_ui_spec.loader.exec_module(ui)

from .qt_harness import qapp  # noqa: E402,F401  (fixture)


@pytest.fixture()
def mind(qapp, tmp_path, monkeypatch):  # noqa: F811
    import memory.database as brain_db
    import memory.memory_manager as mm
    import memory.service as memory_service
    import memory.manager as brain_manager

    monkeypatch.setattr(brain_db, "DEFAULT_DB_PATH", tmp_path / "brain.db")
    monkeypatch.setenv("JARVIS_BRAIN_DB", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    mm._memory_cache.clear()
    brain_manager._instances.clear()
    memory_service.reset_memory_service()
    widget = ui.MindPane()
    yield widget
    widget.close()
    memory_service.reset_memory_service()
    brain_manager._instances.clear()
    mm._memory_cache.clear()


def _tabs(pane):
    return pane.findChild(QTabWidget, "MindTabs")


def _tab_titles(pane):
    widget = _tabs(pane)
    return [widget.tabText(i) for i in range(widget.count())]


# ── the surface ───────────────────────────────────────────────────────────────

def test_the_panel_exposes_the_four_named_tabs(mind):
    assert _tab_titles(mind) == ["MEMORIES", "BELIEFS", "DREAMS", "NEURAL MAP"]


def test_the_chat_panel_navigation_tab_is_renamed_to_mind(qapp):
    host = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    try:
        assert "MIND" in host._tab_keys
        assert "MEMORY" not in host._tab_keys
    finally:
        host.close()


def test_the_2d_neural_map_is_built_only_when_its_tab_is_opened(mind, qapp):
    assert mind._graph2d is None                    # created on demand
    assert not hasattr(mind, "_view_3d_btn")        # the 3D orb toggle is gone
    widget = _tabs(mind)
    widget.setCurrentIndex(3)
    qapp.processEvents()
    # A real renderer, or an explicit "unavailable" message — never a blank tab.
    assert (mind._graph2d is not None
            or "unavailable" in mind._graph2d_placeholder.text().lower())


# ── beliefs / dreams are fed by the real store ────────────────────────────────

def test_beliefs_are_listed_from_the_store(mind):
    from memory.manager import get_brain_memory
    get_brain_memory().remember_belief("The operator prefers concise answers",
                                       source="test")
    mind.reload()
    texts = [mind._belief_list.item(i).text()
             for i in range(mind._belief_list.count())]
    assert any("concise answers" in t for t in texts)
    assert "1" in mind._belief_stat.text()          # BELIEFS 1


def test_dreams_are_listed_from_the_store(mind):
    from memory.manager import get_brain_memory
    get_brain_memory().dream("Ship the Mind panel", kind="plan", source="test")
    mind.reload()
    texts = [mind._dream_list.item(i).text()
             for i in range(mind._dream_list.count())]
    assert any("Mind panel" in t for t in texts)
    assert "1" in mind._dream_stat.text()           # DREAMS 1


def test_empty_views_say_so_instead_of_showing_nothing(mind):
    mind.reload()
    assert "No beliefs yet" in mind._belief_list.item(0).text()
    assert "No dreams yet" in mind._dream_list.item(0).text()


def test_open_work_is_named_on_the_dreams_tab(mind):
    from memory.manager import get_brain_memory
    brain = get_brain_memory()
    tid = brain.start_task("Upgrade the JARVIS project", steps_remaining=["port the UI"])
    brain.reflect()
    mind.reload()
    assert "Upgrade the JARVIS project" in mind._threads.text()
    brain.finish_task(tid)


# ── reflect (the dream cycle) ─────────────────────────────────────────────────

def test_reflect_runs_and_reports_on_the_gui_thread(mind, qapp):
    from memory.manager import get_brain_memory
    brain = get_brain_memory()
    brain.remember("the widget uses python and tkinter", explicit=True)
    brain.remember("the widget uses python and pygame", explicit=True)
    mind.reflect()
    for _ in range(200):
        qapp.processEvents()
        if not mind._reflecting:
            break
        time.sleep(0.01)
    assert not mind._reflecting
    assert "reflection complete" in mind._dream_note.text().lower()
    assert mind._reflect_btn.isEnabled()


def test_forget_belief_removes_the_isolated_one(mind):
    from memory.manager import get_brain_memory
    brain = get_brain_memory()
    brain.remember_belief("temporary belief for the test", source="test")
    mind.reload()
    row = next(i for i in range(mind._belief_list.count())
               if "temporary belief" in mind._belief_list.item(i).text())
    mind._belief_list.setCurrentRow(row)
    mind.forget_belief()
    assert brain.beliefs() == []
