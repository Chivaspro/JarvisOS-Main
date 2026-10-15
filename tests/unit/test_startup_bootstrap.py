"""The J.A.R.V.I.S launcher must pick an interpreter that actually works.

This locks in the fix for the startup failure where every launcher preferred
``py -3`` (a Python with nothing but pip installed) while a fully provisioned
interpreter sat on PATH, so JARVIS died on its first third-party import with
``ModuleNotFoundError: No module named 'psutil'``.

Two kinds of guarantee are asserted here:

* Behaviour — candidate ordering, first-complete-wins, the install fallback and
  the re-entrancy guard.
* A structural invariant — ``start_jarvis.py`` imports ONLY the standard
  library. It has to run on the bare interpreter that caused the failure, so a
  single third-party import at module level would reproduce the very bug this
  module exists to fix.

No real installation happens: ``JARVIS_SKIP_DEP_INSTALL`` is honoured and the
pip call is monkeypatched wherever it could be reached.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import start_jarvis  # noqa: E402

BOOTSTRAP_SOURCE = ROOT / "start_jarvis.py"


# ── The stdlib-only invariant ─────────────────────────────────────────────

#: The one project module start_jarvis is allowed to import: ``launch_ui``.
#: It is itself stdlib-only (checked below), and start_jarvis imports it inside
#: ``main()`` where an ImportError already falls back to running without a
#: launch UI — so the bare-interpreter guarantee still holds.
_PROJECT_ALLOWED = {"launch_ui"}


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative import — not a module name
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    # Imports inside functions count too: one there would still crash the bare
    # interpreter the moment that code path runs.
    return names


def test_bootstrap_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names) | {"__future__"} | _PROJECT_ALLOWED
    third_party = sorted(_imported_top_level_modules(BOOTSTRAP_SOURCE) - allowed)
    assert third_party == [], (
        "start_jarvis.py must stay stdlib-only so it can run on an interpreter "
        f"with no packages installed; found: {third_party}"
    )
    # The project module it does import must itself stay stdlib-only, or the
    # guarantee above is only moving the third-party import one file over.
    for project in sorted(_PROJECT_ALLOWED):
        extra = sorted(_imported_top_level_modules(ROOT / f"{project}.py") - allowed)
        assert extra == [], f"{project}.py must also be stdlib-only; found: {extra}"


def test_bootstrap_hard_imports_cover_the_ui_requirements():
    # The probe list has to include what `import ui` needs, or a broken
    # interpreter would be selected and JARVIS would fail exactly as before.
    assert "PyQt6" in start_jarvis.HARD_IMPORTS
    assert "numpy" in start_jarvis.HARD_IMPORTS
    # psutil is deliberately NOT required: telemetry degrades without it.
    assert "psutil" not in start_jarvis.HARD_IMPORTS
    assert "psutil" in start_jarvis.SOFT_IMPORTS


# ── Candidate resolution ──────────────────────────────────────────────────

def test_candidates_prefer_the_project_venv_then_path(monkeypatch, tmp_path):
    venv_python = tmp_path / "python.exe"
    venv_python.write_text("", encoding="utf-8")
    on_path = tmp_path / "path-python.exe"
    on_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(start_jarvis, "_venv_python", lambda: venv_python)
    monkeypatch.setattr(start_jarvis, "_py_launcher_interpreters", lambda: [])
    monkeypatch.setattr(start_jarvis.shutil, "which",
                        lambda name: str(on_path) if name.startswith("python") else None)

    candidates = start_jarvis.candidate_interpreters()
    assert candidates[0] == venv_python.resolve()
    assert on_path.resolve() in candidates


def test_candidates_drop_paths_that_are_not_files(monkeypatch, tmp_path):
    monkeypatch.setattr(start_jarvis, "_venv_python", lambda: tmp_path / "nope.exe")
    monkeypatch.setattr(start_jarvis, "_py_launcher_interpreters", lambda: [])
    monkeypatch.setattr(start_jarvis.shutil, "which", lambda name: None)

    assert all(path.is_file() for path in start_jarvis.candidate_interpreters())


def test_first_complete_skips_interpreters_that_cannot_run_the_app():
    incomplete = start_jarvis.Candidate(exe=Path("a"), version="3.11", missing=("psutil",))
    broken = start_jarvis.Candidate(exe=Path("b"), error="FileNotFoundError")
    complete = start_jarvis.Candidate(exe=Path("c"), version="3.12", missing=())

    assert start_jarvis.first_complete([incomplete, broken, complete]) is complete
    assert start_jarvis.first_complete([incomplete, broken]) is None
    # first_usable is the pip target: it only needs to be able to RUN.
    assert start_jarvis.first_usable([broken, incomplete]) is incomplete


def test_install_target_prefers_a_usable_interpreter(monkeypatch):
    broken = start_jarvis.Candidate(exe=Path("a"), error="boom")
    incomplete = start_jarvis.Candidate(exe=Path("b"), missing=("numpy",))

    assert start_jarvis.install_target(results=[broken, incomplete]) == Path("b")
    assert start_jarvis.install_target(results=[broken]) == Path(sys.executable)
    assert start_jarvis.install_target(interpreter="C:/chosen/python.exe") == Path("C:/chosen/python.exe")


# ── Probing the real interpreter ──────────────────────────────────────────

def test_probe_reports_missing_modules_and_the_version():
    result = start_jarvis.probe_interpreter(
        sys.executable, modules=("json", "definitely_not_a_module_abc123"))

    assert result.error is None
    assert result.usable is True
    assert result.complete is False
    assert result.missing == ("definitely_not_a_module_abc123",)
    assert result.version.startswith("3.")


def test_probe_marks_an_unrunnable_interpreter_as_an_error(tmp_path):
    missing_exe = tmp_path / "python.exe"
    result = start_jarvis.probe_interpreter(missing_exe, modules=("json",))

    assert result.usable is False
    assert result.error
    assert result.complete is False


# ── main() control flow ───────────────────────────────────────────────────

@pytest.fixture
def complete_interpreter(monkeypatch, tmp_path):
    """A resolution that finds a working interpreter without spawning one."""
    exe = Path(sys.executable)
    monkeypatch.setattr(start_jarvis, "resolve",
                        lambda interpreter=None, log=None, **kwargs: [
                            start_jarvis.Candidate(exe=exe, version="3.12", missing=())])
    return exe


def test_check_mode_reports_success_without_launching(monkeypatch, complete_interpreter):
    monkeypatch.setattr(start_jarvis, "launch",
                        lambda exe: pytest.fail("--check must never launch JARVIS"))
    monkeypatch.setattr(start_jarvis, "probe_interpreter",
                        lambda exe, modules=(), **kw: start_jarvis.Candidate(exe=Path(exe)))

    assert start_jarvis.main(["--check"]) == 0


def test_check_mode_fails_when_no_interpreter_is_complete(monkeypatch):
    monkeypatch.setattr(start_jarvis, "resolve",
                        lambda interpreter=None, log=None, **kwargs: [
                            start_jarvis.Candidate(exe=Path("a"), missing=("PyQt6",))])

    assert start_jarvis.main(["--check"]) == 1


def test_reentry_guard_launches_directly_without_resolving(monkeypatch):
    monkeypatch.setenv(start_jarvis.ENV_BOOTSTRAPPED, "1")
    monkeypatch.setattr(start_jarvis, "resolve",
                        lambda *a, **kw: pytest.fail("must not resolve when already bootstrapped"))
    monkeypatch.setattr(start_jarvis, "launch", lambda exe: 0)

    assert start_jarvis.main([]) == 0


def test_missing_everywhere_installs_then_launches(monkeypatch, tmp_path):
    target = tmp_path / "python.exe"
    target.write_text("", encoding="utf-8")
    incomplete = start_jarvis.Candidate(exe=target, version="3.11", missing=("PyQt6", "numpy"))
    installed: list = []

    # First resolution finds nothing usable; after the install the same
    # interpreter passes, so JARVIS must actually start.
    monkeypatch.setattr(start_jarvis, "resolve", lambda interpreter=None, log=None, **kwargs: [incomplete])
    monkeypatch.setattr(start_jarvis, "install_requirements",
                        lambda exe, log=None: installed.append(Path(exe)) or True)
    monkeypatch.setattr(start_jarvis, "probe_interpreter",
                        lambda exe, modules=(), **kw: start_jarvis.Candidate(
                            exe=Path(exe), version="3.11", missing=()))
    launched: list = []
    monkeypatch.setattr(start_jarvis, "launch",
                        lambda exe, **kwargs: launched.append(Path(exe)) or 0)
    monkeypatch.setattr(start_jarvis, "dialog", lambda *a, **kw: None)

    assert start_jarvis.main([]) == 0
    assert installed == [target]
    assert launched == [target]


def test_failed_install_reports_instead_of_launching(monkeypatch, tmp_path):
    target = tmp_path / "python.exe"
    target.write_text("", encoding="utf-8")
    incomplete = start_jarvis.Candidate(exe=target, missing=("PyQt6",))
    dialogs: list = []

    monkeypatch.setattr(start_jarvis, "resolve", lambda interpreter=None, log=None, **kwargs: [incomplete])
    monkeypatch.setattr(start_jarvis, "install_requirements", lambda exe, log=None: False)
    monkeypatch.setattr(start_jarvis, "probe_interpreter",
                        lambda exe, modules=(), **kw: start_jarvis.Candidate(
                            exe=Path(exe), missing=("PyQt6",)))
    monkeypatch.setattr(start_jarvis, "launch",
                        lambda exe: pytest.fail("a failed install must not launch JARVIS"))
    monkeypatch.setattr(start_jarvis, "dialog",
                        lambda text, *a, **kw: dialogs.append(text))

    assert start_jarvis.main([]) == 1
    # Two dialogs can fire (a first-launch "installing" notice when there is no
    # console, then the failure) — the failure one must name the package.
    assert any("PyQt6" in text for text in dialogs)


def test_pip_is_skipped_when_the_escape_hatch_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv(start_jarvis.ENV_SKIP_INSTALL, "1")
    launched: list = []
    monkeypatch.setattr(start_jarvis.subprocess, "run",
                        lambda *a, **kw: pytest.fail("pip must not run"))

    assert start_jarvis.install_requirements(tmp_path / "python.exe") is False


# ── --install (install_dependencies.bat) ──────────────────────────────────


def _probe_with_soft_missing(*soft):
    """A probe stub: the hard imports resolve, these optional ones do not."""
    wanted = set(soft)

    def _probe(exe, modules=(), **kwargs):
        missing = tuple(m for m in modules if m in wanted)
        return start_jarvis.Candidate(exe=Path(exe), version="3.12", missing=missing)

    return _probe


def test_install_mode_reports_all_present_without_running_pip(monkeypatch):
    monkeypatch.setattr(start_jarvis, "resolve",
                        lambda interpreter=None, log=None, **kwargs: [
                            start_jarvis.Candidate(exe=Path("python.exe"), version="3.12")])
    monkeypatch.setattr(start_jarvis, "probe_interpreter", _probe_with_soft_missing())
    monkeypatch.setattr(start_jarvis, "install_requirements",
                        lambda exe, log=None: pytest.fail("nothing is missing — no pip"))
    monkeypatch.setattr(start_jarvis, "launch",
                        lambda exe: pytest.fail("--install must not launch JARVIS"))

    assert start_jarvis.main(["--install"]) == 0


def test_install_mode_repairs_a_missing_optional_engine(monkeypatch):
    """psutil missing is exactly the bug this whole change exists for."""
    exe = Path("python.exe")
    installed: list = []
    monkeypatch.setattr(start_jarvis, "resolve",
                        lambda interpreter=None, log=None, **kwargs: [
                            start_jarvis.Candidate(exe=exe, version="3.12")])
    monkeypatch.setattr(start_jarvis, "probe_interpreter",
                        _probe_with_soft_missing("psutil"))
    monkeypatch.setattr(start_jarvis, "install_requirements",
                        lambda target, log=None: installed.append(Path(target)) or True)
    monkeypatch.setattr(start_jarvis, "launch",
                        lambda target: pytest.fail("--install must not launch JARVIS"))
    monkeypatch.setattr(start_jarvis, "dialog", lambda *a, **kw: None)

    assert start_jarvis.main(["--install"]) == 0
    assert installed == [exe]


def test_install_mode_never_launches_the_app(monkeypatch):
    monkeypatch.setattr(start_jarvis, "resolve",
                        lambda interpreter=None, log=None, **kwargs: [
                            start_jarvis.Candidate(exe=Path("python.exe"), missing=("numpy",))])
    monkeypatch.setattr(start_jarvis, "install_requirements", lambda exe, log=None: False)
    monkeypatch.setattr(start_jarvis, "probe_interpreter",
                        lambda exe, modules=(), **kw: start_jarvis.Candidate(
                            exe=Path(exe), missing=("numpy",)))
    monkeypatch.setattr(start_jarvis, "launch",
                        lambda exe: pytest.fail("--install must not launch JARVIS"))

    assert start_jarvis.main(["--install"]) == 1
