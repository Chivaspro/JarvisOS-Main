"""requirements.txt reconcile must recognise the capitalised Qt distributions.

Regression: ``_module_for("PyQt6")`` derived the lowercase module "pyqt6",
and ``importlib.util.find_spec`` is case-sensitive, so both PyQt6 and
PyQt6-WebEngine probed as missing on every boot and the startup sync ran
``pip install`` inside the live process ("installed now: PyQt6 (import not
confirmed)" in the runtime log). The mapping now points at the real import
names and the sync leaves an installed Qt alone.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import dependency_ops as dep  # noqa: E402


def test_case_insensitive_dist_names_resolve_to_real_imports():
    assert dep._module_for("PyQt6") == "PyQt6"
    assert dep._module_for("PyQt6-WebEngine") == "PyQt6.QtWebEngineWidgets"


@pytest.mark.skipif(importlib.util.find_spec("PyQt6") is None,
                    reason="PyQt6 not installed in this interpreter")
def test_sync_leaves_installed_qt_alone(monkeypatch):
    """With the mapping fixed, an installed Qt must never reach pip."""
    calls = []

    def _fail_install(package, timeout=600.0):
        calls.append(package)
        return False, "pip must not be called"

    qt_entries = [
        {"dist": "PyQt6", "spec": "PyQt6", "marker": "",
         "module": dep._module_for("PyQt6"), "platform_ok": True},
        {"dist": "PyQt6-WebEngine", "spec": "PyQt6-WebEngine", "marker": "",
         "module": dep._module_for("PyQt6-WebEngine"), "platform_ok": True},
    ]
    monkeypatch.setattr(dep, "parse_requirements", lambda path=None: qt_entries)
    monkeypatch.setattr(dep, "_pip_install", _fail_install)

    report = dep.sync_requirements(allow_install=True)
    assert calls == [], f"pip was invoked for installed packages: {calls}"
    assert sorted(report["already"]) == ["PyQt6", "PyQt6-WebEngine"]
    assert report["installed"] == []
