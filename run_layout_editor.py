"""Standalone Layout Studio launcher.

Run just the visual editor — no J.A.R.V.I.S. runtime, no `ui.py`, no
`ui_settings.py`. This is useful for building/testing layouts fast, or on a
machine that doesn't have the rest of the assistant wired up yet.

    python run_layout_editor.py

Requires only, in the same folder:
    ui_layout.py        (the Studio itself)
    ui_layout_core.py   (the element engine — images, effects, glow, stroke…)
    ui_theme.py          (colours/fonts the Studio styles itself with)

Everything you create here is written to config/ui_layout.json in the exact
same schema the full J.A.R.V.I.S. app reads, using ui_layout.py's own
load_layout/save_layout — so a layout built in this standalone mode loads
straight into the real app later, and vice versa.

What this is NOT
-----------------
It is not a mock. There is no fake data and no widget that only pretends to
work. `_StudioHost` below implements the exact minimal surface ui_layout.py's
`_root_candidates()` looks for (see that function if you want to add more
named panels to point the Studio at) with real QWidgets, so every Studio
feature — select, drag, resize, effects, animations, undo, save — operates on
real widgets exactly as it would inside the full app. The only thing missing
is the actual J.A.R.V.I.S. behaviour (chat, camera, voice, etc.) because that
lives in ui.py, which this script deliberately does not import.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / "config"

# Import order matters: ui_theme first (ui_layout.py needs it at import time),
# then the element engine, then the Studio itself.
try:
    from ui_theme import (
        ACCENT, ACCENT_BRIGHT, BG, BG_DEEP, BORDER_FAINT, FONT_HEAD, FONT_SMALL,
        RADIUS_LG, SURFACE, TEXT_DIM, composed_app_css,
    )
except ImportError as exc:  # pragma: no cover
    print(f"FATAL: could not import ui_theme.py — {exc}\n"
         f"Make sure ui_theme.py is in the same folder as this script:\n{HERE}")
    raise SystemExit(1)

try:
    from ui_layout_core import ElementWidget, normalize_element
except ImportError as exc:  # pragma: no cover
    print(f"FATAL: could not import ui_layout_core.py — {exc}\n"
         f"Make sure ui_layout_core.py is in the same folder as this script:\n{HERE}")
    raise SystemExit(1)

try:
    from ui_layout import LayoutEditorDialog, load_layout, save_layout
except ImportError as exc:  # pragma: no cover
    print(f"FATAL: could not import ui_layout.py — {exc}")
    raise SystemExit(1)


def _named_panel(parent: QWidget, title: str, x: int, y: int, w: int, h: int) -> QFrame:
    """A real, visible placeholder panel — something to point the Studio at
    and drop elements onto, styled to roughly match the J.A.R.V.I.S. surface."""
    panel = QFrame(parent)
    panel.setObjectName(title.replace(" ", ""))
    panel.setGeometry(x, y, w, h)
    panel.setStyleSheet(
        f"QFrame{{background:rgba(0,14,24,140);border:1px solid {BORDER_FAINT};"
        f"border-radius:{RADIUS_LG}px;}}")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(10, 8, 10, 8)
    label = QLabel(title.upper())
    label.setStyleSheet(
        f"color:{TEXT_DIM};background:transparent;font:800 8pt {FONT_SMALL};"
        f"letter-spacing:1.5px;")
    layout.addWidget(label)
    layout.addStretch(1)
    panel.show()
    return panel


class _StudioHost(QMainWindow):
    """Minimal stand-in for J.A.R.V.I.S.'s MainWindow.

    Implements exactly what ui_layout.py needs: the attributes
    `_root_candidates()` looks for, plus `_layout_elements` /
    `_layout_widgets` / `_apply_layout_elements` / `_save_layout_elements`
    with the same contract MainWindow uses in the full app (see ui.py's
    `_load_layout_elements` / `_save_layout_elements` / `_apply_layout_elements`
    if you're comparing). Element rendering goes through
    `ui_layout_core.ElementWidget`, the same real engine, so every effect,
    image and animation behaves identically to the full application.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("J.A.R.V.I.S. Layout Studio — Standalone")
        self.resize(1500, 940)
        self.setStyleSheet(composed_app_css() + f"QMainWindow{{background:{BG};}}")

        # -- the real widget tree the Studio edits ------------------------
        self.surface = QWidget()
        self.surface.setObjectName("Surface")
        self.surface.setFixedSize(1400, 860)
        self.surface.setStyleSheet(f"background:{BG_DEEP};")
        self.setCentralWidget(self.surface)

        self._topbar = _named_panel(self.surface, "Top Bar", 0, 0, 1400, 64)
        self._statusbar = _named_panel(self.surface, "Status Bar", 0, 812, 1400, 48)

        # A handful of named content areas — enough to give the target picker
        # real, distinct panels without needing the full application.
        self._system_layout_widgets = {
            "Content Surface": _named_panel(self.surface, "Content Surface", 24, 88, 900, 700),
            "Activity Log": _named_panel(self.surface, "Activity Log", 948, 88, 428, 340),
            "Video Preview": _named_panel(self.surface, "Video Preview", 948, 444, 428, 344),
        }

        for panel in (self._topbar, self._statusbar,
                     *self._system_layout_widgets.values()):
            panel.show()

        # -- element storage, same contract as ui.py's MainWindow ---------
        self._layout_elements: dict[str, dict] = {}
        self._layout_widgets: dict[str, ElementWidget] = {}
        self._layout_selected_widget = None
        self._load_layout_elements()

    # -- persistence -------------------------------------------------------
    def _load_layout_elements(self) -> None:
        try:
            path = CONFIG_DIR / "ui_layout.json"
            data = load_layout(path)
            elements = data.get("elements", []) if isinstance(data, dict) else []
            self._layout_elements = {}
            for raw in elements:
                if isinstance(raw, dict) and raw.get("name"):
                    self._layout_elements[str(raw["name"])] = normalize_element(
                        raw, str(raw["name"]))
            self._apply_layout_elements()
            self.write_log(f"[LAYOUT] loaded {len(self._layout_elements)} "
                           f"element(s) from {path}")
        except Exception as exc:
            self.write_log(f"ERR: layout load — {exc}")

    def _save_layout_elements(self) -> None:
        try:
            for name, widget in list(self._layout_widgets.items()):
                element = self._layout_elements.get(name)
                if element is None:
                    continue
                try:
                    if bool(element.get("locked")):
                        continue
                    geo = widget.geometry()
                    element.update({"x": geo.x(), "y": geo.y(),
                                    "w": geo.width(), "h": geo.height(),
                                    "visible": widget.isVisible()})
                except RuntimeError:
                    self._layout_widgets.pop(name, None)
            save_layout(CONFIG_DIR / "ui_layout.json",
                       {"elements": list(self._layout_elements.values())})
        except Exception as exc:
            self.write_log(f"ERR: layout save — {exc}")

    # -- the element factory, same contract as ui.py's MainWindow ---------
    def _parent_for_element(self, element: dict) -> QWidget:
        wanted = str((element or {}).get("parent", "") or "").strip()
        if wanted:
            for name, widget in self._root_candidates_local():
                if name == wanted:
                    return widget
        return self.surface

    def _root_candidates_local(self):
        from ui_layout import _root_candidates
        return _root_candidates(self)

    def _apply_layout_elements(self) -> None:
        for name, widget in list(self._layout_widgets.items()):
            if name not in self._layout_elements:
                try:
                    widget.setParent(None)
                    widget.deleteLater()
                except RuntimeError:
                    pass
                self._layout_widgets.pop(name, None)

        for name, raw in list(self._layout_elements.items()):
            try:
                spec = normalize_element(raw, name)
                self._layout_elements[name] = spec
                parent = self._parent_for_element(spec)

                widget = self._layout_widgets.get(name)
                if widget is not None:
                    try:
                        widget.objectName()
                    except RuntimeError:
                        widget = None
                if widget is None:
                    widget = ElementWidget(spec, parent)
                    self._layout_widgets[name] = widget
                elif widget.parentWidget() is not parent:
                    widget.setParent(parent)

                widget.apply_spec(spec)
                widget.show()
            except Exception as exc:
                self.write_log(f"ERR: layout element {name} — {exc}")

        ordered = sorted(
            self._layout_widgets.items(),
            key=lambda kv: int((self._layout_elements.get(kv[0]) or {}).get("z", 0) or 0))
        for _, widget in ordered:
            try:
                widget.raise_()
            except RuntimeError:
                pass

    # -- logging ------------------------------------------------------------
    def write_log(self, message: str) -> None:
        print(str(message))


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        host = _StudioHost()
        editor = LayoutEditorDialog(host)
    except Exception:
        traceback.print_exc()
        QMessageBox.critical(
            None, "Layout Studio",
            "Could not start the standalone Studio. See the terminal for the "
            "full error.")
        return 1

    # Closing the Studio window ends the standalone session; the host window
    # behind it is only a canvas and isn't meant to be used on its own.
    def _on_close(event):
        app.quit()
        event.accept()
    editor.closeEvent = _on_close  # type: ignore[method-assign]

    host.show()
    editor.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
