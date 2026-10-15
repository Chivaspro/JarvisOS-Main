"""Offscreen Qt harness for the HUD tests.

No pytest-qt: the project already builds its Qt widgets offscreen with a plain
``QApplication`` (see ``tests/hybrid_voice/test_settings_default_voice.py``), and
every assertion here is about geometry, tokens or scene state rather than input
event synthesis, so ``qtbot`` would only be an extra dependency.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

# A config that exercises the interesting paths: an accent already stored, panels
# with remembered geometry, and features toggled on.
SAMPLE_CONFIG = {
    "assistant_name": "JARVIS",
    "user_name": "Operator",
    "ui_color": "#00d4ff",
    "ui_font": "Rajdhani",
    "ui_opacity": 82,
    "reactor_opacity": 60,
    "voice_output_mode": "gemini_piper",
    "tts_engine": "onnx",
    "features": {
        "draggable_panels": True,
        "panel_positions": {
            "memory": {"x": 120, "y": 90, "w": 760, "h": 560},
            "web": {"x": 40, "y": 40, "w": 980, "h": 700},
        },
    },
}


@pytest.fixture(scope="session")
def qapp(shared_qapplication):
    """The session's single QApplication, created and owned by ``conftest``.

    It must NOT be created here: PyQt6 destroys the C++ application as soon as
    the last Python reference goes, and this directory's tests are no longer
    the only ones that build widgets — a local reference dropped elsewhere in
    the session used to kill the whole run natively (see ``shared_qapplication``
    in ``tests/conftest.py``).
    """
    if shared_qapplication is None:
        pytest.skip("PyQt6 is not installed")
    return shared_qapplication



@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "api_keys.json"
    path.write_text(json.dumps(SAMPLE_CONFIG, indent=4), encoding="utf-8")
    return path


@pytest.fixture()
def settings_window(qapp, config_file: Path):
    import ui_settings
    win = ui_settings.SettingsWindow(config_path=config_file)
    win.show()
    qapp.processEvents()
    yield win
    win.close()


def all_widgets(root: QWidget):
    """Every descendant of ``root``, including ``root`` itself."""
    yield root
    for child in root.findChildren(QWidget):
        yield child


def widget_stylesheets(root: QWidget):
    """(widget, stylesheet) for every widget that carries one."""
    for widget in all_widgets(root):
        try:
            sheet = widget.styleSheet()
        except RuntimeError:      # already deleted by Qt
            continue
        if sheet:
            yield widget, sheet


def label_texts(root: QWidget):
    for widget in all_widgets(root):
        if isinstance(widget, QLabel) and widget.text():
            yield widget
