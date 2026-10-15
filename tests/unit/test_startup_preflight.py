"""main.py's startup preflight must turn a missing import into instructions.

Running ``python main.py`` by hand used to skip the launcher's interpreter
resolution entirely, so the first third-party import raised and the user got a
raw traceback in a message box: no package name, no interpreter, no command to
run. The preflight now checks, installs once, and — when that cannot work —
explains exactly what to do.

Nothing here installs anything or opens a dialog: the pip call is monkeypatched
and the platform is faked so the Windows message box is never reached (it would
block the test run waiting for a click).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as jarvis_main  # noqa: E402

_NON_WINDOWS = SimpleNamespace(system=lambda: "Linux")


@pytest.fixture
def no_dialogs(monkeypatch):
    """Keep the preflight's native message box out of the test run."""
    monkeypatch.setattr(jarvis_main, "_platform", _NON_WINDOWS)


def test_preflight_passes_when_everything_is_importable(monkeypatch):
    monkeypatch.setattr(jarvis_main, "_missing_startup_packages", lambda: [])
    monkeypatch.setattr(jarvis_main, "_pip_install_packages",
                        lambda packages: pytest.fail("nothing is missing — pip must not run"))

    assert jarvis_main._startup_dependency_guard() is True


def test_preflight_installs_once_then_rechecks(monkeypatch, no_dialogs):
    installed: list = []
    missing = [("PyQt6", "PyQt6")]

    monkeypatch.setattr(jarvis_main, "_missing_startup_packages",
                        lambda: list(missing))
    monkeypatch.setattr(jarvis_main, "_pip_install_packages",
                        lambda packages: installed.append(list(packages)) or missing.clear() or True)

    assert jarvis_main._startup_dependency_guard() is True
    assert installed == [["PyQt6"]]


def test_preflight_reports_the_exact_command_when_the_install_fails(monkeypatch, no_dialogs, capsys):
    monkeypatch.setattr(jarvis_main, "_missing_startup_packages",
                        lambda: [("PyQt6", "PyQt6"), ("numpy", "numpy")])
    monkeypatch.setattr(jarvis_main, "_pip_install_packages", lambda packages: False)

    assert jarvis_main._startup_dependency_guard() is False
    report = capsys.readouterr().out
    assert "PyQt6" in report and "numpy" in report
    assert "-m pip install" in report
    assert "run_jarvis.bat" in report


def test_preflight_can_be_skipped(monkeypatch):
    monkeypatch.setenv("JARVIS_SKIP_DEP_PREFLIGHT", "1")
    monkeypatch.setattr(jarvis_main, "_missing_startup_packages",
                        lambda: [("PyQt6", "PyQt6")])
    monkeypatch.setattr(jarvis_main, "_pip_install_packages",
                        lambda packages: pytest.fail("skipped means skipped"))

    assert jarvis_main._startup_dependency_guard() is True


def test_missing_package_detection_reports_nothing_in_a_working_environment():
    # PyQt6 and numpy are what `import ui` needs, and this test suite imports
    # ui, so in a working checkout the list must be empty.
    assert jarvis_main._missing_startup_packages() == []


def test_google_genai_is_preflighted():
    """The logged backend crash was `cannot import name 'genai' from 'google'`.

    It happened AFTER the UI was up, so it could not be a Qt preflight — but it
    is the same class of failure and now gets the same actionable dialog.
    """
    assert ("google.genai", "google-genai") in jarvis_main._STARTUP_PACKAGES


def test_missing_package_detection_reports_a_bogus_module(monkeypatch):
    monkeypatch.setattr(jarvis_main, "_STARTUP_PACKAGES",
                        (("definitely_not_a_module_abc123", "definitely-not-real"),))

    assert jarvis_main._missing_startup_packages() == [
        ("definitely_not_a_module_abc123", "definitely-not-real")]


def test_missing_module_hint_names_the_package_and_the_command():
    traceback_text = (
        'Traceback (most recent call last):\n'
        '  File "main.py", line 5716, in main\n'
        '    from ui import JarvisUI\n'
        '  File "ui.py", line 19, in <module>\n'
        '    import psutil\n'
        "ModuleNotFoundError: No module named 'psutil'\n"
    )
    hint = jarvis_main._missing_module_hint(traceback_text)

    assert "psutil" in hint
    assert sys.executable in hint
    assert "-m pip install psutil" in hint


def test_missing_module_hint_maps_an_aliased_import_to_its_pip_name():
    traceback_text = "ModuleNotFoundError: No module named 'cv2'\n"

    hint = jarvis_main._missing_module_hint(traceback_text)

    # core/dependency_ops knows cv2 ships as opencv-python.
    assert "opencv-python" in hint


def test_missing_module_hint_is_silent_for_other_failures():
    assert jarvis_main._missing_module_hint("RuntimeError: something else broke") == ""
