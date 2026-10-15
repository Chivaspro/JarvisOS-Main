"""The Evolution Manager and the DGM adapter.

Two properties matter here, and neither needs a model or the network:

* **The manager routes.** A missing capability goes to the Forge, a capability
  that keeps failing goes to the DGM adapter, and something already built is
  *not* rebuilt — it is run.
* **The DGM adapter is safe.** A candidate replaces the incumbent only when it
  measurably beats it, every promoted change leaves a recoverable archive behind,
  and the protected areas (credentials, security, startup, core) are refused with
  a named reason.

The Crucible runs for real in the accept/reject tests — it is a subprocess, not a
model call — so "verified" there means verified.
"""
from __future__ import annotations

import json
import tempfile
import textwrap
from pathlib import Path

import pytest

from evolution import manager
from evolution.dgm import adapter, archive, policy


@pytest.fixture()
def store(monkeypatch):
    """An isolated skill-memory file (and no writes into long_term.json)."""
    with tempfile.TemporaryDirectory(prefix="jarvis-manager-") as folder:
        path = Path(folder) / "forged_skills.json"
        monkeypatch.setattr(manager, "_record_path", lambda: path)
        try:
            import memory.memory_manager as mm
            monkeypatch.setattr(mm, "remember", lambda *a, **k: "ok")
        except Exception:
            pass
        yield path


@pytest.fixture()
def dgm(monkeypatch):
    """An isolated plugins dir and archive dir for the DGM adapter."""
    with tempfile.TemporaryDirectory(prefix="jarvis-dgm-") as folder:
        root = Path(folder)
        plugins = root / "plugins"
        plugins.mkdir()
        monkeypatch.setattr(adapter, "_plugins_dir", lambda: plugins)
        monkeypatch.setattr(archive, "root", lambda: root / "archive")
        yield root, plugins


# ── skill memory ──────────────────────────────────────────────────────────────

def test_a_remembered_skill_is_found_by_its_goal(store):
    manager.remember_skill("temperature_convert",
                           goal="convert a temperature between Celsius and Fahrenheit",
                           description="Converts temperatures.",
                           provider="gemini", path="plugins/temperature_convert")
    found = manager.find_skill("convert celsius to fahrenheit")
    assert found is not None
    assert found["name"] == "temperature_convert"
    assert found["match"] >= 0.6


def test_an_unrelated_goal_does_not_match(store):
    manager.remember_skill("temperature_convert",
                           goal="convert a temperature between Celsius and Fahrenheit")
    assert manager.find_skill("book me a flight to Tokyo") is None


def test_memory_survives_a_reload_and_forget_removes_it(store):
    manager.remember_skill("iss_tracker", goal="track the international space station")
    assert len(manager.skill_memory()) == 1
    assert manager.forget_skill("iss_tracker") is True
    assert manager.skill_memory() == []


def test_invocations_are_counted(store):
    manager.remember_skill("iss_tracker", goal="track the international space station")
    manager.note_invocation("iss_tracker")
    manager.note_invocation("iss_tracker")
    assert manager.skill_memory()[0]["invocations"] == 2


def test_the_same_name_is_never_recorded_twice(store):
    manager.remember_skill("x_skill", goal="first goal")
    manager.remember_skill("x_skill", goal="second goal")
    rows = manager.skill_memory()
    assert len(rows) == 1 and rows[0]["goal"] == "second goal"


# ── routing ───────────────────────────────────────────────────────────────────

def test_a_missing_capability_routes_to_the_forge(store):
    assert manager.route("create a skill that tracks the ISS", missing=True) == "forge"


def test_an_explicit_directive_routes_to_the_forge(store):
    assert manager.route("learn how to parse a CSV file") == "forge"


def test_plain_conversation_routes_nowhere(store):
    assert manager.route("how are you today?") == "none"
    assert manager.route("") == "none"


def test_a_repeatedly_failing_capability_routes_to_improvement(store):
    assert manager.route("fix the failing thing", target="iss_tracker",
                         failures=manager.FAILURE_THRESHOLD) == "improve"


def test_improvement_outranks_a_forge(store):
    """The capability exists and fails, so rebuilding it would lose the parts
    that work — the DGM adapter's job, not the Forge's."""
    assert manager.route("learn how to track the ISS", target="iss_tracker",
                         failures=5, missing=True) == "improve"


def test_a_remembered_skill_is_not_forged_again(store):
    manager.remember_skill("iss_tracker", goal="track the international space station")
    assert manager.route("track the international space station") == "none"


def test_forge_refuses_to_rebuild_a_remembered_skill(store, monkeypatch):
    """No synthesis call may happen — proven by making one an outright failure."""
    manager.remember_skill("iss_tracker", goal="track the international space station",
                           description="Reports the ISS position.")
    from evolution import forge as forge_mod
    monkeypatch.setattr(forge_mod, "forge",
                        lambda *a, **k: pytest.fail("the Forge must not run again"))
    reply = manager.forge_now("track the international space station")
    assert "already built" in reply.lower()
    assert "iss_tracker" in reply


def test_the_route_action_answers_with_the_chosen_route(store):
    assert "improve" in manager.handle({"action": "route", "target": "x",
                                        "failures": 9})
    assert "Unknown action" not in manager.handle({"action": "nonsense"})
    assert "not an evolution action" in manager.handle({"action": "nonsense"})


# ── DGM policy: what may and may not be touched ───────────────────────────────

@pytest.mark.parametrize("relpath", [
    "config/api_keys.json",
    "config/settings.py",
    ".env",
    "security/permissions.py",
    "main.py",
    "start_jarvis.py",
    "ui.py",
    "evolution/crucible.py",
    "evolution/vault.py",
    "capabilities/registry.py",
    "core/model_router.py",
    "requirements.txt",
])
def test_protected_areas_are_refused_with_a_reason(relpath):
    assert policy.is_protected(relpath) is True
    reason = policy.refusal(relpath)
    assert reason, f"{relpath} must be refused with a reason"
    assert "reason" not in reason  # a sentence, not a placeholder


@pytest.mark.parametrize("relpath", [
    "plugins/iss_tracker/plugin.py",
    "evolution/adapters/my_adapter.py",
])
def test_the_writable_areas_are_allowed(relpath):
    assert policy.is_protected(relpath) is False
    assert policy.refusal(relpath) == ""


def test_an_unlisted_path_is_refused_rather_than_guessed():
    """Default deny: somewhere not on the list is not an implicit permission."""
    assert policy.refusal("docs/notes.md")
    assert policy.is_protected("docs/notes.md") is False


# ── the keep-or-reject comparison ─────────────────────────────────────────────

def _metrics(passed, total, elapsed, verified=True):
    return {"passed": passed, "total": total, "elapsed_s": elapsed,
            "verified": verified, "detail": ""}


def test_more_tests_passing_wins():
    better, why = adapter._is_better(_metrics(3, 3, 1.0), _metrics(2, 3, 1.0))
    assert better is True and "3/3" in why


def test_fewer_tests_passing_is_rejected():
    better, why = adapter._is_better(_metrics(1, 3, 1.0), _metrics(3, 3, 1.0))
    assert better is False and "fewer" in why


def test_the_same_tests_but_much_slower_is_rejected():
    better, _why = adapter._is_better(_metrics(3, 3, 5.0), _metrics(3, 3, 1.0))
    assert better is False


def test_the_same_tests_and_faster_is_kept():
    better, why = adapter._is_better(_metrics(3, 3, 0.4), _metrics(3, 3, 1.0))
    assert better is True and "faster" in why


def test_an_unverified_candidate_is_always_rejected():
    better, why = adapter._is_better(_metrics(3, 3, 1.0, verified=False),
                                     _metrics(0, 3, 9.0))
    assert better is False and "verification" in why


# ── the archive ───────────────────────────────────────────────────────────────

def test_archive_round_trip(dgm):
    archive.save("iss_tracker", "def run(parameters, player=None, session_memory=None):\n    return 'x'\n",
                 {"passed": 1, "total": 2}, reason="incumbent")
    rows = archive.versions("iss_tracker")
    assert len(rows) == 1 and rows[0]["reason"] == "incumbent"
    source, metrics = archive.latest("iss_tracker")
    assert "def run(" in source and metrics["passed"] == 1


def test_rollback_without_an_archive_says_so(dgm):
    assert "no archived version" in adapter.rollback("nothing_here")


# ── the whole loop, through the real Crucible ─────────────────────────────────

INCUMBENT = textwrap.dedent('''\
    """Fails whenever it is given a value, so the DGM layer has real evidence."""

    def run(parameters: dict, player=None, session_memory=None) -> str:
        if parameters.get("value") is not None:
            raise ValueError("not implemented yet")
        return "Ready."
''')

CANDIDATE_GOOD = textwrap.dedent('''\
    """Handles the value too, so it passes every case."""

    def run(parameters: dict, player=None, session_memory=None) -> str:
        try:
            value = parameters.get("value")
        except Exception:
            value = None
        if value is None:
            return "Ready."
        return f"Converted {value}."
''')

CANDIDATE_BAD = textwrap.dedent('''\
    """Still raises on the value case — no better than the incumbent."""

    def run(parameters: dict, player=None, session_memory=None) -> str:
        if parameters.get("value") is not None:
            raise ValueError("still not implemented")
        return "Ready."
''')

CASES = [{"input": {}}, {"input": {"value": 100}}]


def _install_incumbent(plugins: Path, name: str = "temp_tool") -> Path:
    folder = plugins / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.py").write_text(INCUMBENT, encoding="utf-8")
    (folder / "plugin.json").write_text(
        '{"name": "%s", "version": "1.0.0", "test_cases": %s}'
        % (name, json.dumps(CASES)), encoding="utf-8")
    return folder


def test_a_better_candidate_is_kept_and_the_old_one_archived(dgm, monkeypatch):
    _root, plugins = dgm
    folder = _install_incumbent(plugins)
    monkeypatch.setattr(adapter, "_ask_candidate",
                        lambda *a, **k: (CANDIDATE_GOOD, "handles the value case", ""))

    reply = adapter.improve_skill("temp_tool")

    assert "improved" in reply.lower() and "temp_tool" in reply
    assert (folder / "plugin.py").read_text(encoding="utf-8") == CANDIDATE_GOOD
    assert json.loads((folder / "plugin.json").read_text(encoding="utf-8"))["version"] == "1.0.1"
    assert len(archive.versions("temp_tool")) == 1, "the incumbent must be recoverable"


def test_a_worse_candidate_is_rejected_and_nothing_changes(dgm, monkeypatch):
    _root, plugins = dgm
    folder = _install_incumbent(plugins)
    monkeypatch.setattr(adapter, "_ask_candidate",
                        lambda *a, **k: (CANDIDATE_BAD, "no change", ""))

    reply = adapter.improve_skill("temp_tool")

    assert "rejected" in reply.lower()
    assert (folder / "plugin.py").read_text(encoding="utf-8") == INCUMBENT, (
        "a rejected candidate must leave the working version byte-for-byte intact")
    assert archive.versions("temp_tool") == []


def test_rollback_restores_the_archived_version(dgm, monkeypatch):
    _root, plugins = dgm
    folder = _install_incumbent(plugins)
    monkeypatch.setattr(adapter, "_ask_candidate",
                        lambda *a, **k: (CANDIDATE_GOOD, "handles the value case", ""))
    adapter.improve_skill("temp_tool")
    assert (folder / "plugin.py").read_text(encoding="utf-8") == CANDIDATE_GOOD

    reply = adapter.rollback("temp_tool")

    assert "restored" in reply.lower()
    assert (folder / "plugin.py").read_text(encoding="utf-8") == INCUMBENT


def test_an_unknown_capability_is_reported_not_invented(dgm):
    assert "no editable capability" in adapter.improve_skill("does_not_exist").lower()


# ── the phases the caller is told about are the phases that ran ───────────────

def test_a_kept_improvement_reports_analyze_evolve_test_keep(dgm, monkeypatch):
    _root, plugins = dgm
    _install_incumbent(plugins)
    monkeypatch.setattr(adapter, "_ask_candidate",
                        lambda *a, **k: (CANDIDATE_GOOD, "handles the value case", ""))
    seen: list[str] = []
    adapter.improve_skill("temp_tool",
                          progress=lambda phase, detail: seen.append(phase))

    assert seen[0] == "ANALYZE"
    assert "EVOLVE" in seen and "TEST" in seen
    assert seen[-1] == "KEEP"
    assert seen.index("ANALYZE") < seen.index("EVOLVE") < seen.index("TEST")


def test_a_rejected_improvement_reports_reject(dgm, monkeypatch):
    _root, plugins = dgm
    _install_incumbent(plugins)
    monkeypatch.setattr(adapter, "_ask_candidate",
                        lambda *a, **k: (CANDIDATE_BAD, "no change", ""))
    seen: list[str] = []
    reply = adapter.improve_skill("temp_tool",
                                  progress=lambda phase, detail: seen.append(phase))

    assert "REJECT" in seen
    assert "rejected" in reply.lower()
    assert "KEEP" not in seen, "a rejected candidate must never be reported as kept"


