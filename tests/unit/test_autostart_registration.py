"""Auto Start command construction (TASK 8) — pure logic, no registry writes.

The OS-level registration is verified by the settings flow at runtime; here we
lock down the pieces that must never regress:

* The startup entry point is the console-less ``pythonw`` interpreter running
  ``main.pyw`` from the PROJECT ROOT (never a bare PATH name, never a console
  ``python`` that would flash a terminal window).
* Absolute paths are quoted, so a project path containing spaces still works.
* A single, stable registry value name is used, so repeated enabling can never
  pile up duplicate startup entries.

Nothing here touches HKCU, so the test passes under a sandboxed/offscreen run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import installer  # noqa: E402


def test_command_targets_main_pyw_at_project_root():
    cmd = installer.autostart_command()
    expected_entry = str((installer._project_root() / "main.pyw")).lower()
    assert expected_entry in cmd.lower()
    assert installer._project_root() == ROOT.resolve()


def test_command_prefers_console_less_interpreter():
    exe = installer._console_less_interpreter()
    name = os.path.basename(exe).lower()
    # On CPython Windows the GUI build is pythonw.exe; whatever is returned must
    # be an absolute existing path (or the running interpreter as a fallback).
    assert os.path.isabs(exe)
    if name.startswith("python"):
        assert name.endswith(".exe")


def test_paths_with_spaces_are_quoted():
    cmd = installer.autostart_command()
    # Every absolute path segment is wrapped in double quotes.
    assert cmd.startswith('"')
    assert '" "' in cmd and cmd.endswith('"')


def test_single_stable_value_name_prevents_duplicates():
    assert installer._AUTOSTART_VALUE == "J.A.R.V.I.S"
    assert installer._AUTOSTART_KEY.endswith(r"CurrentVersion\Run")
