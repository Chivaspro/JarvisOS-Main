"""Reusable components for the J.A.R.V.I.S. Layout Studio.

The Studio deliberately does not grow into one gigantic dialog class (TXT
section 57).  Everything with real behaviour lives here:

===================  ========================================================
Component            Responsibility
===================  ========================================================
HistoryManager       transactional undo/redo (one drag = one entry)
SelectionManager     multi-selection, marquee results, stale-pointer pruning
ClipboardManager     copy / cut / paste / duplicate with unique naming
AlignmentManager     align, distribute, snap and smart-guide maths
AssetBrowser         imported-image thumbnails and asset operations
ScriptManager        validated, sandboxed, containable element scripts
CodeEditor           native code editor (gutter, highlighting, find/replace)
PropertyInspector    the full 19-section Properties surface
ViewportCanvas       live rasterised viewport with handles, guides, marquee
ExplorerTree         element hierarchy with script nodes and drag-reparent
OutputPanel          OUTPUT / ERRORS / WARNINGS / SCRIPT LOG / LAYOUT LOG
AnimationManager     real QPropertyAnimation playback with safe teardown
AnimationPanel       the ANIMATION tab controls
===================  ========================================================

Every component talks to the Studio through the documented method surface on
``LayoutEditorDialog`` (``log``, ``spec_for``, ``set_property``, ``do_action``,
``request_save``, …) and never stores a widget it cannot re-validate, so a
widget deleted elsewhere can never be dereferenced here.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSequentialAnimationGroup,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRegion,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui_theme import (
    ACCENT,
    ACCENT_BRIGHT,
    BG,
    BG_DEEP,
    BORDER_FAINT,
    BORDER_SOFT,
    CARD,
    FONT_HEAD,
    FONT_LOG,
    FONT_SMALL,
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_SM,
    SURFACE,
    SURFACE_ALT,
    TEXT,
    TEXT_BRIGHT,
    TEXT_DIM,
    TEXT_MID,
    qcolor,
    bind_module_globals as _bind_theme_globals,
)

# The Studio panels paint with the token values bound at import time, so a live
# theme switch has to refresh them (`ui_theme.bind_module_globals`).
_bind_theme_globals(globals(), (
    "ACCENT", "ACCENT_BRIGHT", "BG", "BG_DEEP", "BORDER_FAINT", "BORDER_SOFT",
    "CARD", "SURFACE", "SURFACE_ALT", "TEXT", "TEXT_BRIGHT", "TEXT_DIM",
    "TEXT_MID", "RADIUS_LG", "RADIUS_MD", "RADIUS_SM",
))

from ui_layout_core import (
    ALIGN_H,
    ALIGN_V,
    ANIMATION_NAMES,
    BORDER_STYLES,
    EFFECT_KINDS,
    EFFECT_SCHEMA,
    ELEMENT_TYPES,
    FIT_MODES,
    HUD_PRESETS,
    TEXT_PRESETS,
    AssetStore,
    ElementWidget,
    apply_effect_model,
    as_bool,
    as_color,
    clamp_float,
    clamp_int,
    class_simple_name,
    default_spec,
    effects_supported,
    get_path,
    normalize_element,
    set_path,
    widget_alive,
)

SCRIPT_ALLOWED_BUILTINS = {
    "abs": abs, "bool": bool, "dict": dict, "enumerate": enumerate, "float": float,
    "int": int, "len": len, "list": list, "max": max, "min": min, "print": print,
    "range": range, "round": round, "set": set, "str": str, "sum": sum, "tuple": tuple,
    "zip": zip, "sorted": sorted, "reversed": reversed, "any": any, "all": all,
    "isinstance": isinstance, "repr": repr, "format": format, "map": map, "filter": filter,
    # Exception types only: a script may raise/catch, it still cannot import or
    # touch dunder attributes (validated with ast before anything runs).
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "RuntimeError": RuntimeError, "KeyError": KeyError, "IndexError": IndexError,
    "ZeroDivisionError": ZeroDivisionError, "AttributeError": AttributeError,
    "AssertionError": AssertionError, "StopIteration": StopIteration,
}


def _label(text: str, colour: str = TEXT_DIM, size: str = "7.5pt", weight: int = 700) -> QLabel:
    lab = QLabel(str(text))
    lab.setStyleSheet(
        f"color:{colour};background:transparent;font:{weight} {size} {FONT_SMALL};"
        f"letter-spacing:0.6px;")
    return lab


def _section_style() -> str:
    return (
        f"QGroupBox{{background:{CARD};border:1px solid {BORDER_FAINT};"
        f"border-radius:{RADIUS_MD}px;margin-top:12px;padding:10px 8px 8px 8px;"
        f"color:{TEXT_MID};font:700 8pt {FONT_SMALL};}}"
        f"QGroupBox::title{{subcontrol-origin:margin;left:10px;padding:0 5px;"
        f"color:{ACCENT_BRIGHT};}}"
    )


def _input_style() -> str:
    return (
        f"QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QPlainTextEdit,QTextEdit{{"
        f"background:{BG};color:{TEXT_BRIGHT};border:1px solid {BORDER_FAINT};"
        f"border-radius:{RADIUS_SM}px;padding:4px 6px;font:8pt {FONT_SMALL};}}"
        f"QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus{{"
        f"border-color:{ACCENT};}}"
        f"QComboBox QAbstractItemView{{background:{BG};color:{TEXT_BRIGHT};"
        f"selection-background-color:rgba(0,180,230,90);}}"
    )


# ===========================================================================
# History
# ===========================================================================
class HistoryManager:
    """Undo/redo with transaction semantics.

    ``begin()`` records the state before an interactive operation; ``commit()``
    writes exactly one entry.  A continuous drag therefore produces a single
    history step, and a multi-object operation is pushed as one entry too
    (TXT section 43).  Entries hold renderable data only -- never a QWidget
    pointer as a value -- and replay validates the widget first.
    """

    def __init__(self, apply_fn: Callable[[Any, dict, str], bool] | None = None,
                 log: Callable[[str], None] | None = None, limit: int = 250):
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._pending: dict | None = None
        self._lock = False
        self.apply_fn = apply_fn
        self.log = log
        self.limit = int(limit)

    # -- transactions ----------------------------------------------------
    def begin(self, widget, kind: str = "element") -> None:
        state = None
        if widget_alive(widget):
            try:
                state = self._capture(widget, kind)
            except Exception:
                state = None
        self._pending = {"widget": widget, "kind": kind, "before": state}

    def abort(self) -> None:
        self._pending = None

    def _capture(self, widget, kind: str) -> dict:
        if kind == "element" and isinstance(widget, ElementWidget):
            return copy.deepcopy(widget.spec)
        return copy.deepcopy(self.capture_widget_state(widget))

    capture_widget_state: Callable[[Any], dict] | None = None   # wired by the Studio

    def commit(self, widget, after: dict | None = None, kind: str = "element",
               label: str = "") -> bool:
        """Record one history entry.  ``after`` defaults to the current state."""
        pending = self._pending
        self._pending = None
        if not widget_alive(widget):
            return False
        before = pending.get("before") if pending and pending.get("widget") is widget else None
        if before is None:
            return False
        try:
            current = copy.deepcopy(after) if after is not None else self._capture(widget, kind)
        except Exception:
            return False
        if before == current:
            return False
        self._undo.append({"widget": widget, "kind": kind, "before": before,
                           "after": current, "label": str(label or "")})
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        return True

    def push(self, widget, before: dict, after: dict, kind: str = "element", label: str = "") -> bool:
        if not widget_alive(widget) or before == after:
            return False
        self._undo.append({"widget": widget, "kind": kind, "before": copy.deepcopy(before),
                           "after": copy.deepcopy(after), "label": str(label or "")})
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        return True

    def push_document(self, before: dict, after: dict, label: str = "") -> bool:
        """Record a document-level change (element created / deleted).

        Element creation and deletion change the *document*, not one widget, so
        they are stored as serialisable snapshots and replayed through the host's
        ``_apply_layout_elements()``.  This is what makes Add and Delete undoable
        (TXT section 43) without ever holding a pointer to a destroyed widget.
        """
        if before == after:
            return False
        self._undo.append({"widgetless": True, "kind": "document", "widget": None,
                           "before": copy.deepcopy(before), "after": copy.deepcopy(after),
                           "label": str(label or "")})
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        return True

    def push_many(self, entries: list[dict], label: str = "") -> bool:
        """Push a batch of ``{widget, before, after, kind}`` as one entry."""
        clean = [e for e in entries if widget_alive(e.get("widget")) and e.get("before") != e.get("after")]
        if not clean:
            return False
        self._undo.append({"batch": clean, "kind": "batch", "label": str(label or ""),
                           "before": None, "after": None, "widget": clean[0]["widget"]})
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        return True

    # -- replay ----------------------------------------------------------
    @property
    def locked(self) -> bool:
        return self._lock

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._pending = None

    def undo(self) -> bool:
        while self._undo:
            entry = self._undo.pop()
            if self._apply(entry, "before"):
                self._redo.append(entry)
                return True
            self._say(f"[LAYOUT] skipped undo step — element no longer exists")
        return False

    def redo(self) -> bool:
        while self._redo:
            entry = self._redo.pop()
            if self._apply(entry, "after"):
                self._undo.append(entry)
                return True
            self._say("[LAYOUT] skipped redo step — element no longer exists")
        return False

    def _apply(self, entry: dict, key: str) -> bool:
        if entry.get("batch"):
            applied = False
            for item in entry["batch"]:
                if not widget_alive(item.get("widget")):
                    continue
                applied = self._apply_one(item, key) or applied
            return applied
        if entry.get("widgetless") or entry.get("kind") == "document":
            return self._apply_one(entry, key)
        if not widget_alive(entry.get("widget")):
            return False
        return self._apply_one(entry, key)

    def _apply_one(self, entry: dict, key: str) -> bool:
        widget = entry.get("widget")
        state = entry.get(key)
        if state is None:
            return False
        if not (entry.get("widgetless") or entry.get("kind") == "document"):
            if not widget_alive(widget):
                return False
        if self.apply_fn is None:
            return False
        self._lock = True
        try:
            return bool(self.apply_fn(widget, copy.deepcopy(state), entry.get("kind", "element")))
        except Exception as exc:
            self._say(f"[ERROR] history step failed — {exc}")
            return False
        finally:
            self._lock = False

    def _say(self, message: str) -> None:
        if callable(self.log):
            try:
                self.log(message)
            except Exception:
                pass


# ===========================================================================
# Selection
# ===========================================================================
class SelectionManager:
    """Multi-selection with live staleness pruning (TXT sections 28 and 50)."""

    def __init__(self, log: Callable[[str], None] | None = None):
        self._items: list[Any] = []
        self._primary = None
        self.log = log

    # -- queries ---------------------------------------------------------
    def items(self) -> list:
        self.prune()
        return [w for w in self._items]

    def primary(self):
        if not widget_alive(self._primary):
            self.prune()
        return self._primary if widget_alive(self._primary) else None

    def count(self) -> int:
        return len(self.items())

    def contains(self, widget) -> bool:
        return any(w is widget for w in self._items)

    # -- mutations -------------------------------------------------------
    def set_primary(self, widget) -> None:
        if widget_alive(widget):
            self._primary = widget

    def select(self, widget, additive: bool = False, toggle: bool = False) -> list:
        if not widget_alive(widget):
            return self.items()
        if toggle:
            return self.toggle(widget)
        if additive:
            if not self.contains(widget):
                self._items.append(widget)
            self._primary = widget
        else:
            self._items = [widget]
            self._primary = widget
        return self.items()

    def select_many(self, widgets: Iterable, additive: bool = False) -> list:
        incoming = [w for w in (widgets or []) if widget_alive(w)]
        if not incoming:
            return self.items()
        if additive:
            for widget in incoming:
                if not self.contains(widget):
                    self._items.append(widget)
        else:
            self._items = list(incoming)
        self._primary = incoming[-1]
        return self.items()

    def add(self, widget) -> list:
        return self.select(widget, additive=True)

    def remove(self, widget) -> list:
        self._items = [w for w in self._items if w is not widget]
        if self._primary is widget:
            self._primary = self._items[-1] if self._items else None
        return self.items()

    def toggle(self, widget) -> list:
        if self.contains(widget):
            return self.remove(widget)
        return self.select(widget, additive=True)

    def remove_many(self, widgets: Iterable) -> list:
        drop = {id(w) for w in (widgets or [])}
        self._items = [w for w in self._items if id(w) not in drop]
        if self._primary is not None and id(self._primary) in drop:
            self._primary = self._items[-1] if self._items else None
        return self.items()

    def clear(self) -> list:
        self._items = []
        self._primary = None
        return []

    def prune(self) -> bool:
        """Drop widgets that were destroyed elsewhere.  True if anything went."""
        alive = [w for w in self._items if widget_alive(w)]
        gone = len(alive) != len(self._items)
        self._items = alive
        if not widget_alive(self._primary):
            self._primary = alive[-1] if alive else None
            gone = True
        return gone


# ===========================================================================
# Clipboard
# ===========================================================================
class ClipboardManager:
    """Element clipboard: complete definitions, relative layout, unique names.

    Copies the *spec* (custom elements) or the captured runtime state (runtime
    widgets) so pasting reproduces effects, styles, image references, scripts and
    hierarchy -- never a live widget pointer (TXT sections 27 and 44).
    """

    def __init__(self, editor=None):
        self.editor = editor
        self._payload: list[dict] = []

    def has_payload(self) -> bool:
        return bool(self._payload)

    def payload(self) -> list[dict]:
        return copy.deepcopy(self._payload)

    def copy(self, widgets: Iterable) -> int:
        widgets = [w for w in (widgets or []) if widget_alive(w)]
        if not widgets:
            return 0
        origin_x = min(w.x() for w in widgets)
        origin_y = min(w.y() for w in widgets)
        payload: list[dict] = []
        for widget in widgets:
            spec = self._spec_of(widget)
            if spec is None:
                continue
            entry = copy.deepcopy(spec)
            entry["x"] = int(widget.x() - origin_x)
            entry["y"] = int(widget.y() - origin_y)
            entry["_origin_x"] = int(origin_x)
            entry["_origin_y"] = int(origin_y)
            payload.append(entry)
        if payload:
            self._payload = payload
        return len(payload)

    def cut(self, widgets: Iterable) -> int:
        count = self.copy(widgets)
        if count and self.editor is not None:
            for widget in list(widgets or []):
                self.editor.remove_widget(widget, history=True)
        return count

    def paste(self, parent_name: str = "", offset: int = 24) -> list[dict]:
        if not self._payload:
            return []
        existing = self._existing_names()
        out: list[dict] = []
        for entry in copy.deepcopy(self._payload):
            base = str(entry.get("name") or entry.get("type") or "Element")
            new_name = self.unique_name(existing, base, copy_suffix=True)
            existing.add(new_name)
            entry["name"] = new_name
            entry["object_name"] = new_name
            entry["x"] = int(entry.get("x", 0)) + offset
            entry["y"] = int(entry.get("y", 0)) + offset
            if parent_name:
                entry["parent"] = parent_name
            out.append(entry)
        return out

    def duplicate(self, widgets: Iterable) -> list[dict]:
        """Duplicate in place, preserving relative positions (TXT section 27)."""
        widgets = [w for w in (widgets or []) if widget_alive(w)]
        if not widgets:
            return []
        existing = self._existing_names()
        origin_x = min(w.x() for w in widgets)
        origin_y = min(w.y() for w in widgets)
        _ = (origin_x, origin_y)          # kept for readability of the intent below
        out: list[dict] = []
        for widget in widgets:
            spec = self._spec_of(widget)
            if spec is None:
                continue
            entry = copy.deepcopy(spec)
            base = str(entry.get("name") or entry.get("type") or "Element")
            new_name = self.unique_name(existing, base)
            existing.add(new_name)
            entry["name"] = new_name
            entry["object_name"] = new_name
            entry["x"] = int(widget.x()) + 18
            entry["y"] = int(widget.y()) + 18
            entry["parent"] = self._parent_name(widget) or str(entry.get("parent") or "")
            out.append(entry)
        return out

    @staticmethod
    def unique_name(existing: Iterable[str], base: str, copy_suffix: bool = True) -> str:
        """``HUD_Button`` -> ``HUD_Button_Copy`` -> ``HUD_Button_Copy_2`` (TXT 45)."""
        names = {str(n) for n in (existing or [])}
        root = re.sub(r"_Copy(_\d+)?$", "", str(base or "Element")).strip() or "Element"
        if root in names and not copy_suffix:
            root = f"{root}_Copy"
        candidate = root if root not in names else f"{root}_Copy"
        if candidate not in names:
            return candidate
        index = 2
        while f"{candidate}_{index}" in names:
            index += 1
        return f"{candidate}_{index}"

    # -- helpers ---------------------------------------------------------
    def _spec_of(self, widget) -> dict | None:
        if self.editor is not None:
            spec = self.editor.spec_for(widget)
            if spec is not None:
                return spec
        if isinstance(widget, ElementWidget):
            return copy.deepcopy(widget.spec)
        return None

    def _existing_names(self) -> set[str]:
        if self.editor is not None:
            try:
                return set(self.editor.element_names())
            except Exception:
                return set()
        return set()

    def _parent_name(self, widget) -> str:
        if self.editor is not None:
            try:
                return str(self.editor.parent_name_for(widget) or "")
            except Exception:
                return ""
        return ""


# ===========================================================================
# Alignment / snapping
# ===========================================================================
class AlignmentManager:
    """Align, distribute and smart-guide maths (TXT sections 29, 30, 32)."""

    MODES = ("left", "hcenter", "right", "top", "vcenter", "bottom")

    @staticmethod
    def align(rects: list[tuple[Any, QRect]], mode: str) -> list[tuple[Any, QRect]]:
        if not rects:
            return []
        bounds = AlignmentManager.bounds([r for _, r in rects])
        out: list[tuple[Any, QRect]] = []
        for widget, rect in rects:
            new = QRect(rect)
            if mode == "left":
                new.moveLeft(bounds.left())
            elif mode == "right":
                new.moveRight(bounds.right())
            elif mode == "hcenter":
                new.moveLeft(bounds.center().x() - rect.width() // 2)
            elif mode == "top":
                new.moveTop(bounds.top())
            elif mode == "bottom":
                new.moveBottom(bounds.bottom())
            elif mode == "vcenter":
                new.moveTop(bounds.center().y() - rect.height() // 2)
            out.append((widget, new))
        return out

    @staticmethod
    def distribute(rects: list[tuple[Any, QRect]], axis: str = "horizontal") -> list[tuple[Any, QRect]]:
        if len(rects) < 3:
            return []
        horizontal = axis.startswith("h")
        ordered = sorted(rects, key=lambda item: item[1].x() if horizontal else item[1].y())
        first = ordered[0][1]
        last = ordered[-1][1]
        if horizontal:
            usable = last.x() - first.x()
            step = usable / float(len(ordered) - 1)
            return [(w, QRect(int(first.x() + step * i), r.y(), r.width(), r.height()))
                    for i, (w, r) in enumerate(ordered)]
        usable = last.y() - first.y()
        step = usable / float(len(ordered) - 1)
        return [(w, QRect(r.x(), int(first.y() + step * i), r.width(), r.height()))
                for i, (w, r) in enumerate(ordered)]

    @staticmethod
    def bounds(rects: list[QRect]) -> QRect:
        out = QRect(rects[0])
        for rect in rects[1:]:
            out = out.united(rect)
        return out

    @staticmethod
    def snap_rect(moving: QRect, others: list[QRect], parent_rect: QRect | None,
                  threshold: int, flags: dict) -> tuple[QRect, list[tuple[str, int]]]:
        """Snap ``moving`` to sibling edges/centres and parent edges/centres.

        Returns the adjusted rect plus the guide lines to draw
        (``("v", x)`` / ``("h", y)``) so the viewport can show them in cyan.
        """
        guides: list[tuple[str, int]] = []
        if threshold <= 0:
            return QRect(moving), guides
        result = QRect(moving)
        candidates: list[QRect] = list(others)
        if parent_rect is not None:
            candidates.append(QRect(parent_rect))
        best_dx: tuple[int, int] | None = None
        best_dy: tuple[int, int] | None = None

        moving_v = []
        if flags.get("edges", True):
            moving_v += [(result.left(), 0), (result.right(), 2)]
        if flags.get("center", True):
            moving_v.append((result.center().x(), 1))
        moving_h = []
        if flags.get("edges", True):
            moving_h += [(result.top(), 0), (result.bottom(), 2)]
        if flags.get("center", True):
            moving_h.append((result.center().y(), 1))

        for other in candidates:
            target_v = []
            target_h = []
            if flags.get("edges", True):
                target_v += [(other.left(), 0), (other.right(), 2)]
                target_h += [(other.top(), 0), (other.bottom(), 2)]
            if flags.get("center", True):
                target_v.append((other.center().x(), 1))
                target_h.append((other.center().y(), 1))
            for value, _kind in moving_v:
                for target, _tkind in target_v:
                    delta = target - value
                    if abs(delta) <= threshold and (best_dx is None or abs(delta) < abs(best_dx[0])):
                        best_dx = (delta, target)
            for value, _kind in moving_h:
                for target, _tkind in target_h:
                    delta = target - value
                    if abs(delta) <= threshold and (best_dy is None or abs(delta) < abs(best_dy[0])):
                        best_dy = (delta, target)

        if best_dx is not None:
            result.moveLeft(result.x() + best_dx[0])
            guides.append(("v", best_dx[1]))
        if best_dy is not None:
            result.moveTop(result.y() + best_dy[0])
            guides.append(("h", best_dy[1]))
        return result, guides


# ===========================================================================
# Scripts
# ===========================================================================
class ScriptManager:
    """Validated, sandboxed element scripts (TXT sections 9 and 40).

    * validated with :mod:`ast` before anything runs;
    * executed in a namespace containing only safe builtins, so a pasted script
      cannot import modules, touch dunders or write files;
    * every failure is contained, reported with its line number and logged;
    * a broken script is disabled automatically after the failure so it cannot
      keep repeating.
    """

    def __init__(self, log: Callable[[str], None] | None = None):
        self.log = log
        self._cache: dict[int, Any] = {}
        self.failures: dict[int, str] = {}

    def validate(self, code: str) -> tuple[bool, str, int]:
        text = str(code or "")
        if not text.strip():
            return False, "script is empty", 0
        try:
            tree = ast.parse(text, mode="exec")
        except SyntaxError as exc:
            return False, f"line {exc.lineno or 0}: {exc.msg}", int(exc.lineno or 0)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                return False, f"line {getattr(node, 'lineno', 0)}: imports are not allowed", int(getattr(node, "lineno", 0))
            if isinstance(node, ast.Attribute) and str(node.attr).startswith("__"):
                return False, f"line {getattr(node, 'lineno', 0)}: dunder access is not allowed", int(getattr(node, "lineno", 0))
            if isinstance(node, ast.Name) and str(node.id).startswith("__"):
                return False, f"line {getattr(node, 'lineno', 0)}: dunder names are not allowed", int(getattr(node, "lineno", 0))
        return True, "", 0

    def compile(self, code: str):
        ok, message, _line = self.validate(code)
        if not ok:
            raise ValueError(message)
        return compile(str(code), "<jarvis-element-script>", "exec")

    def run(self, widget, code: str, event: str = "test") -> tuple[bool, str]:
        ok, message, line = self.validate(code)
        if not ok:
            self._fail(widget, f"[SCRIPT] {self._name(widget)} disabled — {message}", line)
            return False, message
        try:
            compiled = compile(str(code), "<jarvis-element-script>", "exec")
            namespace = {
                "__builtins__": SCRIPT_ALLOWED_BUILTINS,
                "element": widget,
                "spec": getattr(widget, "spec", {}) or {},
                "name": str(getattr(widget, "objectName", lambda: "")() or ""),
                "event": event,
                "log": lambda *args: self._say("[SCRIPT] " + " ".join(str(a) for a in args)),
            }
            exec(compiled, namespace)      # noqa: S102 - sandboxed namespace
            self.failures.pop(id(widget), None)
            self._say(f"[SCRIPT] {self._name(widget)} ran ({event})")
            return True, ""
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            line_no = 0
            tb = traceback.extract_tb(exc.__traceback__)
            for frame in tb:
                if frame.filename == "<jarvis-element-script>":
                    line_no = int(frame.lineno or 0)
            self._fail(widget, f"[SCRIPT] {self._name(widget)} error — line {line_no}: {detail}", line_no)
            return False, detail

    def make_click_runner(self, code: str):
        def _runner(widget, event: str = "click"):
            return self.run(widget, code, event)
        return _runner

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _name(widget) -> str:
        try:
            return str(widget.objectName() or class_simple_name(widget))
        except Exception:
            return "element"

    def _fail(self, widget, message: str, line: int) -> None:
        try:
            self.failures[id(widget)] = message
        except Exception:
            pass
        self._say(message)
        if line:
            self._say(f"[SCRIPT]   line {line}")

    def _say(self, message: str) -> None:
        if callable(self.log):
            try:
                self.log(message)
            except Exception:
                pass


# ===========================================================================
# Code editor
# ===========================================================================
class _LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):  # noqa: N802
        self.editor.paint_line_numbers(event)


class _CodeHighlighter(QSyntaxHighlighter):
    """Lightweight highlighter for Python-ish and QSS-ish text."""

    PY_KEYWORDS = (
        "and", "as", "assert", "async", "await", "break", "class", "continue",
        "def", "del", "elif", "else", "except", "finally", "for", "from",
        "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or",
        "pass", "raise", "return", "try", "while", "with", "yield", "True",
        "False", "None", "self",
    )

    def __init__(self, document, language: str = "python"):
        super().__init__(document)
        self.language = str(language or "python").lower()
        self._rules: list[tuple[re.Pattern, QTextCharFormat]] = []
        self._build()

    def _fmt(self, colour: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(qcolor(colour))
        fmt.setFontWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        fmt.setFontItalic(italic)
        return fmt

    def set_language(self, language: str) -> None:
        self.language = str(language or "python").lower()
        self._build()
        self.rehighlight()

    def _build(self) -> None:
        """Compile the rule set for the current language.

        Every rule colours the *whole* match (capturing groups are only used
        where the intent really is to colour one part, such as the name in
        ``def name``).  Optional groups that may not participate are
        non-capturing on purpose: that is what made ``setFormat`` receive a
        ``None`` span and crash the editor.
        """
        self._rules = []
        if self.language in ("qss", "css"):
            self._rules.append((re.compile(r"[^{}\n]+(?=\{)"), self._fmt(ACCENT_BRIGHT, bold=True)))
            self._rules.append((re.compile(r"#[0-9a-fA-F]{3,8}\b"), self._fmt("#ffcf6b", bold=True)))
            self._rules.append((re.compile(r"[\w-]+\s*:"), self._fmt(TEXT_MID)))
            self._rules.append((re.compile(r"\b\d+(?:\.\d+)?(?:px|pt|em|%)?\b"), self._fmt("#ff8ad1")))
            self._rules.append((re.compile(r"//.*$|/\*.*$"), self._fmt(TEXT_DIM, italic=True)))
            return
        if self.language in ("json", "data"):
            self._rules.append((re.compile(r'"[^"\n]*"\s*:'), self._fmt(ACCENT_BRIGHT, bold=True)))
            self._rules.append((re.compile(r'"[^"\n]*"'), self._fmt("#8ff5b0")))
            self._rules.append((re.compile(r"\b(?:true|false|null)\b"), self._fmt("#ffcf6b", bold=True)))
            self._rules.append((re.compile(r"\b-?\d+(?:\.\d+)?\b"), self._fmt("#ff8ad1")))
            return
        if self.language in ("html",):
            self._rules.append((re.compile(r"</?[A-Za-z][\w-]*"), self._fmt(ACCENT_BRIGHT, bold=True)))
            self._rules.append((re.compile(r"[A-Za-z-]+="), self._fmt(TEXT_MID)))
            self._rules.append((re.compile(r'"[^"\n]*"'), self._fmt("#8ff5b0")))
            return
        keywords = "|".join(self.PY_KEYWORDS)
        self._rules.append((re.compile(rf"\b(?:{keywords})\b"), self._fmt(ACCENT_BRIGHT, bold=True)))
        self._rules.append((re.compile(r"#[^\n]*"), self._fmt(TEXT_DIM, italic=True)))
        self._rules.append((re.compile(r"\"[^\"\n]*\"|'[^'\n]*'"), self._fmt("#8ff5b0")))
        self._rules.append((re.compile(r"\b\d+(?:\.\d+)?\b"), self._fmt("#ff8ad1")))
        self._rules.append((re.compile(r"\bdef\s+(\w+)"), self._fmt("#c8a6ff", bold=True)))
        self._rules.append((re.compile(r"\bclass\s+(\w+)"), self._fmt("#c8a6ff", bold=True)))

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        """Colour one block.  Never raises.

        ``highlightBlock`` runs inside Qt's document layout: an exception here
        escapes Python and leaves the text engine in an undefined state, which
        aborts the process (the white-screen crash this editor exists to
        prevent).  A span is therefore only handed to ``setFormat`` when it is a
        real range inside the block, and a failing rule is skipped instead of
        taking the application down.
        """
        length = len(text or "")
        for pattern, fmt in self._rules:
            try:
                for match in pattern.finditer(text or ""):
                    start, end = match.span(0)
                    # Prefer an explicit capturing group (e.g. the name in
                    # "def name"), but only one that really matched.
                    for index in range(1, (match.re.groups or 0) + 1):
                        if match.start(index) >= 0 and match.end(index) > match.start(index):
                            start, end = match.start(index), match.end(index)
                            break
                    start = max(0, min(start, length))
                    end = max(start, min(end, length))
                    if end > start:
                        self.setFormat(start, end - start, fmt)
            except Exception:
                continue


class CodeEditor(QWidget):
    """Native code editor: gutter, highlighting, find/replace, Ctrl+S.

    Everything here is a Qt text control -- no browser, no external editor
    (TXT section 8).  ``saveRequested`` is emitted by Ctrl+S / the SAVE button.
    """

    saveRequested = pyqtSignal()

    def __init__(self, parent=None, language: str = "python"):
        super().__init__(parent)
        self.language = str(language or "python")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        self.find_bar = QWidget()
        find_row = QHBoxLayout(self.find_bar)
        find_row.setContentsMargins(0, 0, 0, 0)
        find_row.setSpacing(5)
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("find…")
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("replace with…")
        self.find_prev = QPushButton("◀")
        self.find_next = QPushButton("▶")
        self.replace_one = QPushButton("REPLACE")
        self.replace_all = QPushButton("ALL")
        self.close_find = QPushButton("✕")
        for widget in (self.find_input, self.replace_input, self.find_prev, self.find_next,
                       self.replace_one, self.replace_all, self.close_find):
            widget.setStyleSheet(_input_style() + "QPushButton{font:700 7.5pt 'Exo 2';padding:4px 8px;}")
        self.find_prev.setFixedWidth(30)
        self.find_next.setFixedWidth(30)
        self.close_find.setFixedWidth(30)
        find_row.addWidget(_label("FIND / REPLACE", ACCENT, "7pt"))
        find_row.addWidget(self.find_input, 2)
        find_row.addWidget(self.replace_input, 2)
        find_row.addWidget(self.find_prev)
        find_row.addWidget(self.find_next)
        find_row.addWidget(self.replace_one)
        find_row.addWidget(self.replace_all)
        find_row.addWidget(self.close_find)
        root.addWidget(self.find_bar)

        self.editor = QPlainTextEdit()
        font = QFont("Share Tech Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.editor.setFont(font)
        self.editor.setTabStopDistance(4 * self.editor.fontMetrics().horizontalAdvance(" "))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setStyleSheet(
            f"QPlainTextEdit{{background:{BG_DEEP};color:{TEXT};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_SM}px;padding:6px;selection-background-color:rgba(0,180,230,110);}}")
        self.gutter = _LineNumberArea(self)
        self.editor.blockCountChanged.connect(lambda _=0: self._refresh_gutter())
        self.editor.updateRequest.connect(self._update_gutter)
        self.editor.cursorPositionChanged.connect(self._highlight_line)
        root.addWidget(self.editor, 1)

        self.highlighter = _CodeHighlighter(self.editor.document(), self.language)
        self._pending_highlight = False
        self._error_line = 0
        self.find_bar.hide()

        for sequence, slot in (("Ctrl+S", self.saveRequested.emit),
                               ("Ctrl+F", self.toggle_find),
                               ("Ctrl+H", self.toggle_find),
                               ("Escape", self.hide_find)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        self.find_input.returnPressed.connect(self.find_next_occurrence)
        self.find_next.clicked.connect(self.find_next_occurrence)
        self.find_prev.clicked.connect(self.find_previous_occurrence)
        self.replace_one.clicked.connect(self.replace_current)
        self.replace_all.clicked.connect(self.replace_everything)
        self.close_find.clicked.connect(self.hide_find)
        self._refresh_gutter()

    # -- API -------------------------------------------------------------
    def set_text(self, text: str, language: str | None = None, highlight: bool = True) -> None:
        """Replace the buffer.  ``highlight=False`` defers syntax colouring

        Highlighting a large JSON/QSS buffer costs real time, and the Studio
        refreshes six editors per selection change.  Hidden tabs therefore load
        instantly and colour themselves when they are first shown.
        """
        text = str(text or "")
        if language and language != self.language:
            self.set_language(language)
        if self.editor.toPlainText() != text:
            if not highlight:
                self.highlighter.setDocument(None)
                try:
                    self.editor.setPlainText(text)
                finally:
                    self._pending_highlight = True
            else:
                self.editor.setPlainText(text)
                self._pending_highlight = False
        elif highlight:
            self.highlighter.rehighlight()
        if highlight:
            self.set_error_line(0)

    def ensure_highlighted(self) -> None:
        if getattr(self, "_pending_highlight", False):
            self._pending_highlight = False
            self.highlighter.setDocument(self.editor.document())

    def showEvent(self, event):  # noqa: N802
        self.ensure_highlighted()
        super().showEvent(event)

    def text(self) -> str:
        return self.editor.toPlainText()

    def set_language(self, language: str) -> None:
        self.language = str(language or "python")
        self.highlighter.set_language(self.language)

    def set_error_line(self, line: int) -> None:
        self._error_line = max(0, int(line or 0))
        self.editor.setExtraSelections([])
        if self._error_line:
            self.mark_error_line(self._error_line)

    def mark_error_line(self, line: int) -> None:
        """Mark a script error line in red (TXT section 40)."""
        block = self.editor.document().findBlockByNumber(max(0, int(line) - 1))
        if not block.isValid():
            return
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(qcolor("#ff4455", 60))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = QTextCursor(block)
        self.editor.setExtraSelections([selection])

    def toggle_find(self) -> None:
        self.find_bar.setVisible(not self.find_bar.isVisible())
        if self.find_bar.isVisible():
            self.find_input.setFocus()
            self.find_input.selectAll()

    def hide_find(self) -> None:
        self.find_bar.hide()

    # -- gutter ----------------------------------------------------------
    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.editor.blockCount()))))
        return 12 + self.editor.fontMetrics().horizontalAdvance("9") * digits

    def _refresh_gutter(self) -> None:
        self.editor.setViewportMargins(self.line_number_width(), 0, 0, 0)
        self.gutter.update()

    def _update_gutter(self, rect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.editor.viewport().rect()):
            self._refresh_gutter()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        rect = self.editor.geometry()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), self.line_number_width(), rect.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), qcolor(BG_DEEP))
        block = self.editor.firstVisibleBlock()
        number = block.blockNumber()
        offset = self.editor.contentOffset()
        top = int(self.editor.blockBoundingGeometry(block).translated(offset).top())
        bottom = top + int(self.editor.blockBoundingRect(block).height())
        painter.setFont(self.editor.font())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                colour = "#ff4455" if (number + 1) == self._error_line else TEXT_DIM
                painter.setPen(qcolor(colour))
                painter.drawText(0, top, self.gutter.width() - 6,
                                 self.editor.fontMetrics().height(),
                                 int(Qt.AlignmentFlag.AlignRight), str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.editor.blockBoundingRect(block).height())
            number += 1
        painter.end()

    def _highlight_line(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        if not self.editor.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(qcolor(ACCENT, 26))
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.editor.textCursor()
            selection.cursor.clearSelection()
            selections.append(selection)
        if self._error_line:
            block = self.editor.document().findBlockByNumber(self._error_line - 1)
            if block.isValid():
                selection = QTextEdit.ExtraSelection()
                selection.format.setBackground(qcolor("#ff4455", 60))
                selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                selection.cursor = QTextCursor(block)
                selections.append(selection)
        self.editor.setExtraSelections(selections)

    # -- search ----------------------------------------------------------
    def find_next_occurrence(self) -> None:
        text = self.find_input.text()
        if not text:
            return
        flags = None
        if not self.editor.find(text, flags):
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            self.editor.find(text, flags)

    def find_previous_occurrence(self) -> None:
        text = self.find_input.text()
        if not text:
            return
        from PyQt6.QtGui import QTextDocument

        flags = QTextDocument.FindFlag.FindBackward
        if not self.editor.find(text, flags):
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.editor.setTextCursor(cursor)
            self.editor.find(text, flags)

    def replace_current(self) -> None:
        cursor = self.editor.textCursor()
        if cursor.hasSelection() and cursor.selectedText() == self.find_input.text():
            cursor.insertText(self.replace_input.text())
        self.find_next_occurrence()

    def replace_everything(self) -> None:
        find = self.find_input.text()
        if not find:
            return
        text = self.editor.toPlainText().replace(find, self.replace_input.text())
        self.editor.setPlainText(text)


# ===========================================================================
# Output panel
# ===========================================================================
class OutputPanel(QWidget):
    """Bottom debug panel with OUTPUT/ERRORS/WARNINGS/SCRIPT/LAYOUT tabs."""

    MAX_BLOCKS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self._views: dict[str, QPlainTextEdit] = {}
        for name in ("OUTPUT", "ERRORS", "WARNINGS", "SCRIPT LOG", "LAYOUT LOG"):
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setMaximumBlockCount(self.MAX_BLOCKS)
            view.setStyleSheet(
                f"QPlainTextEdit{{background:{BG_DEEP};color:{TEXT_MID};border:none;"
                f"padding:6px;font:8pt {FONT_LOG};}}")
            self._views[name] = view
            self.tabs.addTab(view, name)
        root.addWidget(self.tabs, 1)

    def write(self, message: str) -> None:
        text = str(message or "")
        for view in self._views.values():
            view.appendPlainText(text)
        upper = text.upper()
        if "[ERROR" in upper or "ERR:" in upper or "ERROR" in upper:
            self.tabs.setCurrentWidget(self._views["ERRORS"])
        elif "[WARN" in upper or "WARN:" in upper:
            self.tabs.setCurrentWidget(self._views["WARNINGS"])
        elif "[SCRIPT]" in upper:
            self.tabs.setCurrentWidget(self._views["SCRIPT LOG"])
        elif "[LAYOUT]" in upper or "[STYLE]" in upper:
            self.tabs.setCurrentWidget(self._views["LAYOUT LOG"])

    def clear_all(self) -> None:
        for view in self._views.values():
            view.clear()

    def current_text(self) -> str:
        widget = self.tabs.currentWidget()
        return widget.toPlainText() if isinstance(widget, QPlainTextEdit) else ""


# ===========================================================================
# Asset browser
# ===========================================================================
class AssetBrowser(QWidget):
    """Thumbnail asset manager for imported layout images (TXT section 5)."""

    importRequested = pyqtSignal()
    useRequested = pyqtSignal(str)          # asset name -> assign to selection
    replaceRequested = pyqtSignal(str)
    deleteRequested = pyqtSignal(str)
    previewRequested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(_label("IMAGE ASSETS", ACCENT, "8pt", 800))

        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(96, 96))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setGridSize(QSize(112, 132))
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setWordWrap(True)
        self.list.setStyleSheet(
            f"QListWidget{{background:{SURFACE_ALT};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_MD}px;color:{TEXT};padding:4px;}}"
            f"QListWidget::item{{border-radius:6px;padding:4px;}}"
            f"QListWidget::item:selected{{background:rgba(0,180,230,90);color:{TEXT_BRIGHT};}}")
        self.list.itemDoubleClicked.connect(self._use_current)
        self.list.currentItemChanged.connect(lambda *_: self._emit_preview())
        root.addWidget(self.list, 1)

        grid = QGridLayout()
        grid.setSpacing(5)
        buttons = [
            ("IMPORT", self.importRequested.emit, 0, 0),
            ("USE ON SELECTION", self._use_current, 0, 1),
            ("REPLACE", self._replace_current, 1, 0),
            ("RENAME", self._rename_current, 1, 1),
            ("DELETE UNUSED", self._delete_current, 2, 0),
            ("COPY PATH", self._copy_path, 2, 1),
            ("OPEN LOCATION", self._open_location, 3, 0),
            ("PREVIEW", self._emit_preview, 3, 1),
        ]
        for text, slot, row, column in buttons:
            button = QPushButton(text)
            button.setStyleSheet(
                f"QPushButton{{background:rgba(5,22,34,150);color:{TEXT_MID};"
                f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;"
                f"padding:6px 8px;font:700 7.5pt {FONT_SMALL};letter-spacing:0.8px;}}"
                f"QPushButton:hover{{color:{TEXT_BRIGHT};border-color:{ACCENT};"
                f"background:rgba(0,72,98,140);}}")
            button.clicked.connect(slot)
            grid.addWidget(button, row, column)
        root.addLayout(grid)
        self.hint = _label("Drop an image onto the viewport to import it.", TEXT_DIM, "7pt", 600)
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

    # -- data ------------------------------------------------------------
    def refresh(self, store: AssetStore, cache) -> None:
        current = self.current_name()
        self.list.clear()
        for name, path in store.items():
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            thumb = cache.thumbnail(path, 96) if cache is not None else None
            if thumb is not None and not thumb.isNull():
                item.setIcon(thumb)
            else:
                item.setToolTip(f"{path}\n(thumbnail unavailable)")
            item.setToolTip(f"{path}")
            self.list.addItem(item)
        if current:
            self.select_name(current)

    def current_name(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def select_name(self, name: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) == str(name):
                self.list.setCurrentItem(item)
                return

    # -- handlers --------------------------------------------------------
    def _use_current(self) -> None:
        name = self.current_name()
        if name:
            self.useRequested.emit(name)

    def _replace_current(self) -> None:
        name = self.current_name()
        if name:
            self.replaceRequested.emit(name)

    def _rename_current(self) -> None:
        name = self.current_name()
        if not name:
            return
        from PyQt6.QtWidgets import QInputDialog

        new_name, ok = QInputDialog.getText(self, "Rename asset", "New asset name:", text=name)
        if ok and str(new_name).strip():
            self.useRequested.emit(f"__rename__:{name}:{str(new_name).strip()}")

    def _delete_current(self) -> None:
        name = self.current_name()
        if name:
            self.deleteRequested.emit(name)

    def _copy_path(self) -> None:
        name = self.current_name()
        if not name:
            return
        self.useRequested.emit(f"__copypath__:{name}")

    def _open_location(self) -> None:
        name = self.current_name()
        if name:
            self.useRequested.emit(f"__openlocation__:{name}")

    def _emit_preview(self) -> None:
        name = self.current_name()
        if name:
            self.previewRequested.emit(name)


# ===========================================================================
# Explorer
# ===========================================================================
class ExplorerTree(QTreeWidget):
    """Roblox-Studio-style hierarchy with script nodes (TXT sections 10, 11, 46)."""

    widgetSelected = pyqtSignal(object)
    scriptSelected = pyqtSignal(object)
    contextRequested = pyqtSignal(object, object)
    reparentRequested = pyqtSignal(object, object)

    ROLE_KIND = Qt.ItemDataRole.UserRole + 1
    ROLE_WIDGET = Qt.ItemDataRole.UserRole

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAnimated(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setExpandsOnDoubleClick(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context)
        self.itemClicked.connect(self._clicked)
        self.itemDoubleClicked.connect(self._double)
        self._script_names: set[str] = set()

    # -- signals ---------------------------------------------------------
    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        kind = item.data(0, self.ROLE_KIND)
        widget = item.data(0, self.ROLE_WIDGET)
        if not widget_alive(widget):
            return
        if kind == "script":
            self.scriptSelected.emit(widget)
        else:
            self.widgetSelected.emit(widget)

    def _double(self, item: QTreeWidgetItem, _column: int) -> None:
        widget = item.data(0, self.ROLE_WIDGET)
        if widget_alive(widget) and item.data(0, self.ROLE_KIND) == "script":
            self.scriptSelected.emit(widget)

    def _context(self, point: QPoint) -> None:
        item = self.itemAt(point)
        if item is None:
            self.contextRequested.emit(None, self.viewport().mapToGlobal(point))
            return
        widget = item.data(0, self.ROLE_WIDGET)
        self.contextRequested.emit(widget if widget_alive(widget) else None,
                                   self.viewport().mapToGlobal(point))

    def selected_widgets(self) -> list:
        out = []
        for item in self.selectedItems():
            widget = item.data(0, self.ROLE_WIDGET)
            if widget_alive(widget) and widget not in out:
                out.append(widget)
        return out

    # -- drag & drop reparent -------------------------------------------
    def dropEvent(self, event):  # noqa: N802
        target_item = self.itemAt(event.position().toPoint())
        target = target_item.data(0, self.ROLE_WIDGET) if target_item is not None else None
        moved = self.selected_widgets()
        super().dropEvent(event)
        if moved and widget_alive(target):
            for widget in moved:
                if widget is not target:
                    self.reparentRequested.emit(widget, target)

    def items_for(self, widget) -> list[QTreeWidgetItem]:
        out = []
        for item in self.findItems("", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive, 0):
            if item.data(0, self.ROLE_WIDGET) is widget:
                out.append(item)
        return out


# ===========================================================================
# Viewport
# ===========================================================================
class ViewportCanvas(QWidget):
    """Live viewport: real widget tree raster, handles, guides, marquee.

    The canvas renders the *actual* running widgets (never a mock) through a
    cached pixmap, so the frame is reused until something marks it dirty.  Drags
    update only the geometry of the moved widget and emit one ``dragFinished``
    on release, which is what keeps a continuous drag cheap (TXT section 42).
    """

    widgetsPicked = pyqtSignal(list, bool)     # widgets, additive
    geometryChanged = pyqtSignal(object)
    dragStarted = pyqtSignal(object)
    dragFinished = pyqtSignal(object)
    hoverWidget = pyqtSignal(object)
    marqueeFinished = pyqtSignal(list)
    panRequested = pyqtSignal(QPoint)
    zoomStepRequested = pyqtSignal(float)
    contextRequested = pyqtSignal(object, object)
    dropFilesRequested = pyqtSignal(list, object)

    HANDLE = 7
    EDGE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.host: QWidget | None = None
        self.target: QWidget | None = None
        self.selection: list = []
        self.primary = None
        self.zoom = 1.0
        self.grid_enabled = True
        self.snap_enabled = True
        self.grid_size = 8
        self.snap_mode = "grid"            # grid | element | pixel
        self.snap_flags = {"edges": True, "center": True, "pixel": False}
        self.show_guides = True
        self.show_rulers = True
        self.tool = "select"
        self._cache: QPixmap | None = None
        self._cache_key: tuple = ()
        self._dirty = True
        self._drag_mode: str | None = None
        self._drag_start_target = QPoint()
        self._drag_origin_rects: list[tuple[Any, QRect]] = []
        self._guides: list[tuple[str, int]] = []
        self._marquee: QRect | None = None
        self._marquee_origin = QPoint()
        self._panning = False
        self._pan_origin = QPoint()
        self._hover = None
        self._paint_failed = False
        self._last_frame: QPixmap | None = None

        self._render_timer = QTimer(self)
        self._render_timer.setInterval(600)
        self._render_timer.timeout.connect(self._heartbeat)
        self._render_timer.start()

        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(520, 420)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda point: self.contextRequested.emit(self._widget_at(point), self.mapToGlobal(point)))

    # -- state -----------------------------------------------------------
    def set_target(self, host: QWidget, target: QWidget | None) -> None:
        self.host = host
        self.target = target
        self._resize_canvas()
        self.mark_dirty()

    def set_selection(self, widgets: list, primary=None) -> None:
        self.selection = [w for w in (widgets or []) if widget_alive(w)]
        self.primary = primary if widget_alive(primary) else (self.selection[-1] if self.selection else None)
        self.update()

    def set_zoom(self, value: float) -> None:
        self.zoom = max(0.1, min(6.0, float(value)))
        self._resize_canvas()
        self.mark_dirty()

    def set_tool(self, tool: str) -> None:
        self.tool = str(tool or "select")
        cursors = {"select": Qt.CursorShape.ArrowCursor, "move": Qt.CursorShape.SizeAllCursor,
                   "resize": Qt.CursorShape.SizeFDiagCursor, "text": Qt.CursorShape.IBeamCursor,
                   "image": Qt.CursorShape.CrossCursor, "frame": Qt.CursorShape.CrossCursor,
                   "panel": Qt.CursorShape.CrossCursor, "pan": Qt.CursorShape.OpenHandCursor}
        self.setCursor(cursors.get(self.tool, Qt.CursorShape.ArrowCursor))
        self.update()

    def mark_dirty(self) -> None:
        self._dirty = True
        self.update()

    def _heartbeat(self) -> None:
        target = self.target
        if not widget_alive(target):
            if self._cache is not None:
                self._cache = None
                self.update()
            return
        key = (id(target), target.width(), target.height(), self.zoom)
        if key != self._cache_key:
            self.mark_dirty()

    # -- rendering -------------------------------------------------------
    def _render_target(self) -> tuple[QPixmap | None, QRect]:
        target = self.target
        if not widget_alive(target):
            self._cache = None
            return None, QRect()
        if target.width() <= 0 or target.height() <= 0:
            return None, QRect()
        margin = 40
        rect = QRect(margin, margin,
                     max(1, int(target.width() * self.zoom)),
                     max(1, int(target.height() * self.zoom)))
        key = (id(target), target.width(), target.height(), self.zoom)
        if not self._dirty and self._cache is not None and key == self._cache_key:
            return self._cache, rect

        pix = QPixmap(max(1, target.width()), max(1, target.height()))
        pix.fill(qcolor(BG_DEEP))
        painter = QPainter()
        if not painter.begin(pix):
            return self._cache, rect
        try:
            # QWidget.render() needs a QRegion for sourceRegion; a QRect raises
            # TypeError there and an unhandled exception in paintEvent aborts
            # the application.
            target.render(painter, QPoint(0, 0), QRegion(target.rect()),
                          QWidget.RenderFlag.DrawWindowBackground
                          | QWidget.RenderFlag.DrawChildren)
        except Exception:
            if painter.isActive():
                painter.end()
            self._paint_failed = True
            return self._cache, rect
        finally:
            if painter.isActive():
                painter.end()
        self._cache = pix
        self._cache_key = key
        self._dirty = False
        self._last_frame = pix
        return pix, rect

    def _resize_canvas(self) -> None:
        target = self.target
        if not widget_alive(target):
            self.resize(900, 700)
            return
        self.resize(max(900, int(target.width() * self.zoom + 110)),
                    max(680, int(target.height() * self.zoom + 110)))

    def _target_pos(self, pos) -> QPoint | None:
        pix, rect = self._render_target()
        if pix is None or not rect.contains(pos):
            return None
        return QPoint(int((pos.x() - rect.left()) / self.zoom), int((pos.y() - rect.top()) / self.zoom))

    def _canvas_rect(self, target_rect: QRect) -> QRect:
        return QRect(
            40 + int(target_rect.x() * self.zoom),
            40 + int(target_rect.y() * self.zoom),
            max(1, int(target_rect.width() * self.zoom)),
            max(1, int(target_rect.height() * self.zoom)),
        )

    def _widget_rect(self, widget) -> QRect | None:
        if not widget_alive(widget) or not widget_alive(self.target):
            return None
        try:
            top_left = widget.mapTo(self.target, QPoint(0, 0))
            return QRect(top_left, widget.size())
        except Exception:
            return None

    def _pick_child(self, target_pos: QPoint):
        target = self.target
        if target is None:
            return None
        child = target.childAt(target_pos)
        if child is None:
            return target
        current = child
        depth = 0
        while depth < 12:
            local = current.mapFrom(target, target_pos)
            deeper = current.childAt(local)
            if deeper is None or deeper is current:
                return current
            current = deeper
            depth += 1
        return current

    def _widget_at(self, point: QPoint):
        pos = self._target_pos(point)
        return self._pick_child(pos) if pos is not None else None

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillRect(self.rect(), qcolor(BG_DEEP))
            if self.grid_enabled:
                painter.setPen(qcolor("#15353f", 90))
                step = max(4, int(max(1, self.grid_size) * self.zoom))
                for x in range(0, self.width(), step):
                    painter.drawLine(x, 0, x, self.height())
                for y in range(0, self.height(), step):
                    painter.drawLine(0, y, self.width(), y)

            pix, target_rect = self._render_target()
            if pix is None:
                painter.setPen(qcolor(TEXT_DIM))
                painter.setFont(QFont("Exo 2", 9, QFont.Weight.Bold))
                message = ("VIEWPORT RECOVERY · unable to render element"
                           if self.target is not None else "NO PANEL SELECTED")
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, message)
                return

            painter.save()
            painter.setPen(qcolor(ACCENT, 110))
            painter.setBrush(QColor(1, 9, 16, 230))
            painter.drawRoundedRect(target_rect.adjusted(-6, -6, 6, 6), 12, 12)
            scaled = pix.scaled(target_rect.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(target_rect.topLeft(), scaled)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(qcolor(ACCENT_BRIGHT, 150))
            painter.drawRect(target_rect)
            painter.restore()

            if self.show_rulers:
                self._paint_rulers(painter, target_rect)

            for widget in self.selection:
                rect = self._widget_rect(widget)
                if rect is None:
                    continue
                canvas = self._canvas_rect(rect)
                is_primary = widget is self.primary
                painter.setBrush(qcolor(ACCENT_BRIGHT, 26))
                painter.setPen(qcolor(ACCENT_BRIGHT, 230) if is_primary else qcolor(ACCENT, 160))
                painter.drawRect(canvas)
                if is_primary:
                    painter.setBrush(qcolor(ACCENT_BRIGHT))
                    for point in self._handle_points(canvas):
                        painter.drawRect(point.x() - self.HANDLE // 2, point.y() - self.HANDLE // 2,
                                         self.HANDLE, self.HANDLE)
                    painter.setPen(qcolor(ACCENT_BRIGHT, 200))
                    painter.drawEllipse(canvas.center(), 3, 3)
                    self._paint_size_badge(painter, canvas, rect)

            if self.show_guides and self._guides:
                painter.setPen(qcolor("#00ffd5", 220))
                for kind, value in self._guides:
                    if kind == "v":
                        x = self._canvas_rect(QRect(value, 0, 0, 0)).left()
                        painter.drawLine(x, 0, x, self.height())
                    else:
                        y = self._canvas_rect(QRect(0, value, 0, 0)).top()
                        painter.drawLine(0, y, self.width(), y)

            if self._marquee is not None:
                painter.setPen(QPen(qcolor(ACCENT_BRIGHT, 200), 1, Qt.PenStyle.DashLine))
                painter.setBrush(qcolor(ACCENT_BRIGHT, 30))
                painter.drawRect(self._marquee)

            if self._hover is not None and widget_alive(self._hover) and self._hover not in self.selection:
                rect = self._widget_rect(self._hover)
                if rect is not None:
                    painter.setPen(qcolor(ACCENT, 120))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(self._canvas_rect(rect))
        except Exception:
            self._paint_failed = True
        finally:
            if painter.isActive():
                painter.end()

    def _paint_rulers(self, painter: QPainter, target_rect: QRect) -> None:
        painter.setPen(qcolor(ACCENT, 90))
        font = QFont("Share Tech Mono", 7)
        painter.setFont(font)
        for index in range(0, 21):
            x = target_rect.left() + int(index * 100 * self.zoom)
            if x > target_rect.right() + 20:
                break
            painter.drawLine(x, target_rect.top() - 8, x, target_rect.top() - 3)
            if index and index % 2 == 0:
                painter.drawText(x + 2, target_rect.top() - 10, str(index * 100))
        for index in range(0, 21):
            y = target_rect.top() + int(index * 100 * self.zoom)
            if y > target_rect.bottom() + 20:
                break
            painter.drawLine(target_rect.left() - 8, y, target_rect.left() - 3, y)
            if index and index % 2 == 0:
                painter.drawText(target_rect.left() - 40, y + 3, str(index * 100))

    def _paint_size_badge(self, painter: QPainter, canvas: QRect, rect: QRect) -> None:
        text = f"X {rect.x()}  Y {rect.y()}   W {rect.width()}  H {rect.height()}"
        painter.setFont(QFont("Share Tech Mono", 7))
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 12
        badge = QRect(canvas.left(), max(2, canvas.top() - 20), width, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(2, 14, 24, 225))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(qcolor(ACCENT_BRIGHT))
        painter.drawText(badge.adjusted(6, 0, -6, 0), int(Qt.AlignmentFlag.AlignVCenter), text)

    @staticmethod
    def _handle_points(rect: QRect) -> list[QPoint]:
        return [
            QPoint(rect.left(), rect.top()), QPoint(rect.center().x(), rect.top()),
            QPoint(rect.right(), rect.top()), QPoint(rect.left(), rect.center().y()),
            QPoint(rect.right(), rect.center().y()), QPoint(rect.left(), rect.bottom()),
            QPoint(rect.center().x(), rect.bottom()), QPoint(rect.right(), rect.bottom()),
        ]

    # -- snapping --------------------------------------------------------
    def _snap_delta(self, value: int) -> int:
        if not self.snap_enabled or self.snap_mode == "pixel":
            return value
        grid = max(1, int(self.grid_size))
        return int(round(value / grid) * grid)

    def _siblings(self, widget) -> list[QRect]:
        out: list[QRect] = []
        for other in self.selection:
            if other is widget:
                continue
            rect = self._widget_rect(other)
            if rect is not None:
                out.append(rect)
        parent = widget.parentWidget() if widget_alive(widget) else None
        if widget_alive(parent) and parent is not self.target:
            for child in parent.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                if child is widget or child in self.selection:
                    continue
                rect = self._widget_rect(child)
                if rect is not None:
                    out.append(rect)
        return out

    def _parent_rect(self, widget) -> QRect | None:
        if not widget_alive(widget):
            return None
        parent = widget.parentWidget()
        if not widget_alive(parent):
            return None
        try:
            return QRect(parent.mapTo(self.target, QPoint(0, 0)), parent.size())
        except Exception:
            return None

    # -- mouse -----------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802
        point = event.position().toPoint()
        if event.button() == Qt.MouseButton.MiddleButton or self.tool == "pan":
            self._panning = True
            self._pan_origin = point
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if not widget_alive(self.target):
            super().mousePressEvent(event)
            return
        target_pos = self._target_pos(point)
        if target_pos is None:
            super().mousePressEvent(event)
            return

        # resize handle on the primary selection?
        mode = None
        if self.primary is not None and widget_alive(self.primary):
            rect = self._widget_rect(self.primary)
            if rect is not None:
                canvas = self._canvas_rect(rect)
                for index, handle in enumerate(self._handle_points(canvas)):
                    if (point - handle).manhattanLength() <= self.HANDLE + 2:
                        mode = f"resize:{index}"
                        break
                if mode is None and canvas.contains(point):
                    mode = "move"

        additive = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        toggle = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if mode is None:
            picked = self._pick_child(target_pos)
            if picked is None:
                self._marquee_origin = point
                self._marquee = QRect(point, point)
                self._drag_mode = "marquee"
                self.update()
                event.accept()
                return
            self.widgetsPicked.emit([picked], additive or toggle)
            # The Studio owns the selection; adopt whatever it decided and make
            # sure the clicked element is part of it (multi-drag support).
            if widget_alive(picked) and picked not in self.selection:
                self.selection.append(picked)
            self.primary = picked
            self.update()
            mode = "move"

        if mode.startswith("resize") and (self.primary is None or not widget_alive(self.primary)):
            event.accept()
            return
        self._drag_mode = mode
        self._drag_start_target = target_pos
        self._guides = []
        if self.primary is not None and widget_alive(self.primary):
            self._drag_origin_rects = [(self.primary, QRect(self.primary.geometry()))]
            for widget in self.selection:
                if widget_alive(widget) and widget is not self.primary:
                    self._drag_origin_rects.append((widget, QRect(widget.geometry())))
        else:
            self._drag_origin_rects = []
        if self._drag_origin_rects:
            self.dragStarted.emit(self._drag_origin_rects[0][0])
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802
        point = event.position().toPoint()
        if self._panning:
            delta = point - self._pan_origin
            self._pan_origin = point
            self.panRequested.emit(delta)
            event.accept()
            return
        if self._drag_mode == "marquee":
            self._marquee = QRect(self._marquee_origin, point).normalized()
            self.update()
            event.accept()
            return
        if self._drag_mode and self._drag_origin_rects:
            target_pos = self._target_pos(point)
            if target_pos is None:
                return
            dx = target_pos.x() - self._drag_start_target.x()
            dy = target_pos.y() - self._drag_start_target.y()
            moved = []
            for widget, origin in self._drag_origin_rects:
                if not widget_alive(widget):
                    continue
                if self._drag_mode == "move":
                    rect = QRect(origin)
                    rect.moveTo(origin.x() + dx, origin.y() + dy)
                    if self.snap_enabled and self.snap_mode == "grid":
                        rect.moveTo(self._snap_delta(rect.x()), self._snap_delta(rect.y()))
                    else:
                        rect.moveTo(rect.x(), rect.y())
                    moved.append((widget, rect))
                else:
                    rect = QRect(origin)
                    handle = int(self._drag_mode.split(":")[1])
                    sx = self._snap_delta(dx) if self.snap_mode == "grid" else dx
                    sy = self._snap_delta(dy) if self.snap_mode == "grid" else dy
                    if handle in (0, 3, 5):
                        rect.setLeft(origin.left() + sx)
                    if handle in (2, 4, 7):
                        rect.setRight(origin.right() + sx)
                    if handle in (0, 1, 2):
                        rect.setTop(origin.top() + sy)
                    if handle in (5, 6, 7):
                        rect.setBottom(origin.bottom() + sy)
                    if rect.width() < 8:
                        rect.setWidth(8)
                    if rect.height() < 8:
                        rect.setHeight(8)
                    moved.append((widget, rect))

            if moved and self.snap_enabled and self.snap_mode == "element" and self._drag_mode == "move":
                primary_widget, primary_rect = moved[0]
                snapped, guides = AlignmentManager.snap_rect(
                    primary_rect, self._siblings(primary_widget), self._parent_rect(primary_widget),
                    8, self.snap_flags)
                delta = snapped.topLeft() - primary_rect.topLeft()
                self._guides = guides
                moved = [(widget, QRect(rect).translated(delta)) for widget, rect in moved]
            elif self.snap_mode != "element":
                self._guides = []

            for widget, rect in moved:
                try:
                    widget.setGeometry(rect)
                    widget.setProperty("_jarvis_layout_touched", True)
                except RuntimeError:
                    continue
                self.geometryChanged.emit(widget)
            self.mark_dirty()
            event.accept()
            return

        target_pos = self._target_pos(point)
        if target_pos is not None:
            hovered = self._pick_child(target_pos)
            if hovered is not self._hover:
                self._hover = hovered
                self.hoverWidget.emit(hovered)
                self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton or self._panning:
            self._panning = False
            self.set_tool(self.tool)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_mode == "marquee":
                rect = self._marquee or QRect()
                self._marquee = None
                self._guides = []
                self._drag_mode = None
                self.update()
                picked: list = []
                for child in self._children_in_rect(rect):
                    if child not in picked:
                        picked.append(child)
                self.marqueeFinished.emit(picked)
                event.accept()
                return
            was_dragging = self._drag_mode is not None
            self._drag_mode = None
            self._guides = []
            if was_dragging and self._drag_origin_rects:
                moved = [widget for widget, _rect in self._drag_origin_rects if widget_alive(widget)]
                if moved:
                    self.dragFinished.emit(moved)
            self._drag_origin_rects = []
            self.mark_dirty()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _children_in_rect(self, rect: QRect) -> list:
        if rect.isNull() or not widget_alive(self.target):
            return []
        target_rect = QRect(int((rect.x() - 40) / self.zoom), int((rect.y() - 40) / self.zoom),
                            max(1, int(rect.width() / self.zoom)), max(1, int(rect.height() / self.zoom)))
        out = []
        for child in self.target.findChildren(QWidget):
            mapped = self._widget_rect(child)
            if mapped is None:
                continue
            if target_rect.intersects(mapped):
                out.append(child)
        return out

    def wheelEvent(self, event):  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            return
        # Wheel zooms (with or without Ctrl), matching the TXT viewport spec.
        self.zoomStepRequested.emit(0.1 if delta > 0 else -0.1)
        event.accept()

    # -- drag & drop files ----------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            return
        target_pos = self._target_pos(event.position().toPoint())
        self.dropFilesRequested.emit(paths, target_pos)
        event.acceptProposedAction()


# ===========================================================================
# Property inspector
# ===========================================================================
class _DebouncedTextEdit(QPlainTextEdit):
    """Multiline editor that reports its value after a short typing pause.

    The Studio must not re-apply a script or metadata block on every keystroke
    (TXT section 9), and it must never rebuild the Properties panel from inside a
    text signal -- this edit emits once the operator stops typing.
    """

    valueChanged = pyqtSignal(str)

    def __init__(self, text: str = "", delay: int = 300, parent=None):
        super().__init__(str(text or ""), parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(80, int(delay)))
        self._timer.timeout.connect(lambda: self.valueChanged.emit(self.toPlainText()))
        self.textChanged.connect(lambda: self._timer.start())

    def flush(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.valueChanged.emit(self.toPlainText())

    def focusOutEvent(self, event):  # noqa: N802
        self.flush()
        super().focusOutEvent(event)


class PropertyInspector(QScrollArea):
    """The complete Properties surface (TXT sections 36, 37, 20, 23).

    ``propertyEdited`` carries a dotted spec path (``glow.radius``) and the new
    value; the Studio decides whether that lands on a custom element's spec or on
    the safe runtime-widget QSS/effect path.  Buttons emit ``actionRequested``
    with a symbolic action name so the Studio owns the behaviour.
    """

    propertyEdited = pyqtSignal(str, object)
    actionRequested = pyqtSignal(str, object)
    presetApplied = pyqtSignal(str, str)
    effectAdded = pyqtSignal(str)
    effectRemoved = pyqtSignal(int)
    effectToggled = pyqtSignal(int, bool)
    effectMoved = pyqtSignal(int, int)

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self._apply_container_style(self.container)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(9, 7, 9, 10)
        self.layout.setSpacing(8)
        self.setWidget(self.container)
        self._fields: dict[str, QWidget] = {}
        self._loading = False
        self._spec: dict | None = None
        self._subject = None
        self._multi = 0
        self._selected_effect = -1
        self._state_name = "hover"
        self._support = lambda _feature: True
        self._build_chrome()

    def _apply_container_style(self, container: QWidget) -> None:
        """One stylesheet for the whole panel.

        Styling every spin box, combo and group box individually made a rebuild
        cost ~2 seconds.  Qt applies a parent stylesheet to its children, so a
        single call covers the whole surface.
        """
        container.setStyleSheet(_input_style() + _section_style() + (
            f"QPushButton{{background:rgba(6,26,38,170);color:{TEXT_MID};"
            f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;"
            f"padding:5px 8px;font:700 7pt {FONT_SMALL};letter-spacing:0.6px;}}"
            f"QPushButton:hover{{color:{TEXT_BRIGHT};border-color:{ACCENT};"
            f"background:rgba(0,82,110,150);}}"
            f"QListWidget{{background:{BG};color:{TEXT};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_SM}px;padding:3px;}}"
            f"QListWidget::item:selected{{background:rgba(0,180,230,90);}}"
            f"QSlider::groove:horizontal{{height:4px;background:rgba(0,212,255,40);border-radius:2px;}}"
            f"QSlider::handle:horizontal{{background:{ACCENT};width:10px;margin:-4px 0;"
            f"border-radius:5px;}}"))

    # -- chrome ----------------------------------------------------------
    def _build_chrome(self) -> None:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.subject_label = _label("NO SELECTION", ACCENT_BRIGHT, "8pt", 800)
        self.subject_label.setWordWrap(True)
        row.addWidget(self.subject_label, 1)
        self.multi_label = _label("", TEXT_DIM, "7pt", 700)
        row.addWidget(self.multi_label)
        self.layout.addWidget(header)
        self.hint = _label("Select an element in the Explorer or click it in the viewport.",
                           TEXT_DIM, "7.5pt", 600)
        self.hint.setWordWrap(True)
        self.layout.addWidget(self.hint)

    # -- helpers ---------------------------------------------------------
    def clear(self) -> None:
        """Swap in a fresh container instead of unparenting every child.

        The old implementation called ``setParent(None)`` on hundreds of widgets
        per rebuild (~10 seconds of the profiled cost) and could delete a widget
        that was still dispatching its own signal.  Swapping the container is two
        calls, and the old tree is disposed of through ``deleteLater()`` on the
        next event-loop turn -- never inside the current dispatch.
        """
        old = self.container
        self._fields.clear()
        self._spec = None
        self._subject = None
        self.container = QWidget()
        self._apply_container_style(self.container)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(9, 7, 9, 10)
        self.layout.setSpacing(8)
        if old is not None and old is not self.container:
            old.setParent(None)
            old.deleteLater()
        self.setWidget(self.container)

    def _section(self, title: str) -> QFormLayout:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setContentsMargins(7, 12, 7, 7)
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        box._form = form      # type: ignore[attr-defined]
        self.layout.addWidget(box)
        return form

    def _register(self, key: str, widget: QWidget) -> None:
        self._fields[key] = widget

    def _spin(self, form: QFormLayout, key: str, label: str, value: int,
              low: int = -20000, high: int = 20000, step: int = 1, suffix: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setValue(int(clamp_int(value, low, high, 0)))
        if suffix:
            spin.setSuffix(suffix)
        spin.valueChanged.connect(lambda v, k=key: self._emit(k, v))
        form.addRow(label, spin)
        self._register(key, spin)
        return spin

    def _double(self, form: QFormLayout, key: str, label: str, value: float,
                low: float = -360.0, high: float = 360.0, step: float = 1.0,
                suffix: str = "") -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setDecimals(1)
        spin.setValue(clamp_float(value, low, high, 0.0))
        if suffix:
            spin.setSuffix(suffix)
        spin.valueChanged.connect(lambda v, k=key: self._emit(k, float(v)))
        form.addRow(label, spin)
        self._register(key, spin)
        return spin

    def _check(self, form: QFormLayout, key: str, label: str, value: bool) -> QCheckBox:
        box = QCheckBox()
        box.setChecked(as_bool(value, False))
        box.toggled.connect(lambda v, k=key: self._emit(k, bool(v)))
        form.addRow(label, box)
        self._register(key, box)
        return box

    def _text(self, form: QFormLayout, key: str, label: str, value: str,
              multiline: bool = False, placeholder: str = "") -> QWidget:
        if multiline:
            edit = _DebouncedTextEdit(str(value or ""), delay=280)
            edit.setPlaceholderText(placeholder)
            edit.setMaximumHeight(84)
            edit.valueChanged.connect(lambda text, k=key: self._emit(k, text))
            form.addRow(label, edit)
            self._register(key, edit)
            return edit
        edit = QLineEdit(str(value or ""))
        edit.setPlaceholderText(placeholder)
        edit.editingFinished.connect(lambda k=key, e=edit: self._emit(k, e.text()))
        form.addRow(label, edit)
        self._register(key, edit)
        return edit

    def _combo(self, form: QFormLayout, key: str, label: str, value: str,
               options: Iterable[str]) -> QComboBox:
        combo = QComboBox()
        combo.addItems([str(o) for o in options])
        if str(value) in [str(o) for o in options]:
            combo.setCurrentText(str(value))
        combo.currentTextChanged.connect(lambda v, k=key: self._emit(k, v))
        form.addRow(label, combo)
        self._register(key, combo)
        return combo

    def _slider(self, form: QFormLayout, key: str, label: str, value: int,
                low: int = 0, high: int = 100) -> QSlider:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(clamp_int(value, low, high, low))
        readout = _label(str(slider.value()), ACCENT_BRIGHT, "7.5pt", 800)
        readout.setFixedWidth(30)
        row_layout.addWidget(slider, 1)
        row_layout.addWidget(readout)
        slider.valueChanged.connect(lambda v, r=readout: r.setText(str(v)))
        slider.valueChanged.connect(lambda v, k=key: self._emit(k, int(v)))
        form.addRow(label, row)
        self._register(key, slider)
        return slider

    def _color(self, form: QFormLayout, key: str, label: str, value: str) -> QPushButton:
        button = QPushButton(str(value or "#000000"))
        button.setStyleSheet(self._swatch_style(str(value or "#000000")))
        button.clicked.connect(lambda _=False, k=key, b=button: self._pick_color(k, b))
        form.addRow(label, button)
        self._register(key, button)
        return button

    def _buttons(self, form: QFormLayout, label: str, actions: list[tuple[str, str, Any]]) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        for text, action, payload in actions:
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, a=action, p=payload: self.actionRequested.emit(a, p))
            row_layout.addWidget(button)
        if label:
            form.addRow(label, row)
        else:
            form.addRow(row)
        return row

    def _preset_row(self, form: QFormLayout, label: str, group: str, names: Iterable[str]) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        for name in names:
            button = QPushButton(str(name).upper())
            button.clicked.connect(lambda _=False, g=group, n=str(name): self.presetApplied.emit(g, n))
            row_layout.addWidget(button)
        form.addRow(label, row)
        return row

    @staticmethod
    def _mini_style() -> str:  # noqa: D401 - kept for callers outside this class
        return (f"QPushButton{{background:rgba(6,26,38,170);color:{TEXT_MID};"
                f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;"
                f"padding:5px 7px;font:700 7pt {FONT_SMALL};letter-spacing:0.6px;}}"
                f"QPushButton:hover{{color:{TEXT_BRIGHT};border-color:{ACCENT};"
                f"background:rgba(0,82,110,150);}}")

    @staticmethod
    def _swatch_style(colour: str) -> str:
        colour = as_color(colour, "#000000")
        return (f"QPushButton{{background:{colour};color:#02060c;"
                f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;"
                f"padding:6px;font:700 8pt {FONT_LOG};}}"
                f"QPushButton:hover{{border-color:{ACCENT_BRIGHT};}}")

    def _pick_color(self, key: str, button: QPushButton) -> None:
        current = qcolor(as_color(button.text(), "#000000"))
        chosen = QColorDialog.getColor(current, self, f"{key} colour")
        if not chosen.isValid():
            return
        button.setText(chosen.name())
        button.setStyleSheet(self._swatch_style(chosen.name()))
        self._emit(key, chosen.name())

    def _emit(self, key: str, value) -> None:
        if self._loading:
            return
        self.propertyEdited.emit(key, value)

    # -- public ----------------------------------------------------------
    def sync_geometry(self, widget) -> None:
        """Update X/Y/W/H in place during a drag (never rebuilds the panel)."""
        if not widget_alive(widget):
            return
        self._loading = True
        try:
            for key, value in (("x", widget.x()), ("y", widget.y()),
                               ("w", widget.width()), ("h", widget.height())):
                field = self._fields.get(key)
                if field is None or not hasattr(field, "setValue"):
                    continue
                try:
                    field.blockSignals(True)
                    field.setValue(int(value))
                finally:
                    field.blockSignals(False)
        except RuntimeError:
            pass
        finally:
            self._loading = False

    def refresh(self, widget, spec: dict | None, multi: int = 0,
                element_type: str = "Widget", supports: Callable[[str], bool] | None = None) -> None:
        """Rebuild every section for ``widget`` from ``spec``."""
        self._loading = True
        try:
            self.clear()
            self._build_chrome()
            self._multi = int(multi)
            self._subject = widget
            if widget is None or spec is None:
                return
            self._spec = spec
            self.multi_label.setText(f"· {multi} SELECTED" if multi > 1 else "")
            try:
                name = str(widget.objectName() or getattr(widget, "element_name", lambda: "")() or "")
            except Exception:
                name = ""
            self.subject_label.setText(f"{element_type.upper()}{(' · ' + name) if name else ''}")
            self.hint.setText(
                "Custom element — every property below is painted by the element engine."
                if isinstance(widget, ElementWidget) else
                "Runtime J.A.R.V.I.S. widget — styling is applied through QSS and graphics effects.")
            support = supports or (lambda _feature: True)
            self._support = support
            self._build_identity(widget, spec)
            self._build_geometry(widget, spec)
            if support("rotation"):
                self._build_transform(widget, spec)
            self._build_appearance(widget, spec)
            if support("typography"):
                self._build_typography(widget, spec)
            self._build_content(widget, spec)
            if support("image"):
                self._build_image(widget, spec)
            self._build_layout(widget, spec)
            self._build_effects(widget, spec)
            self._build_borders(widget, spec)
            self._build_stroke(widget, spec)
            self._build_shadow(widget, spec)
            self._build_glow(widget, spec)
            self._build_animation(widget, spec)
            self._build_interaction(widget, spec)
            self._build_parenting(widget, spec)
            self._build_script(widget, spec)
            self._build_style(widget, spec)
            self._build_advanced(widget, spec)
            self.layout.addStretch(1)
        finally:
            self._loading = False

    # -- sections --------------------------------------------------------
    def _build_identity(self, widget, spec: dict) -> None:
        form = self._section("IDENTITY")
        self._text(form, "object_name", "Object Name", spec.get("object_name") or widget.objectName())
        self._text(form, "name", "Element Name", spec.get("name", ""))
        type_combo = self._combo(form, "type", "Element Type", spec.get("type", "Panel"), ELEMENT_TYPES)
        type_combo.setEnabled(isinstance(widget, ElementWidget))
        self._text(form, "group", "Group", spec.get("group", ""), placeholder="group name")
        self._check(form, "locked", "Locked", spec.get("locked", False))
        self._check(form, "hidden", "Hidden", spec.get("hidden", False))
        self._spin(form, "z", "Z-Order", spec.get("z", 0), -100000, 100000)
        self._buttons(form, "Order", [
            ("SEND TO BACK", "z_back", None), ("BACKWARD", "z_backward", None),
            ("FORWARD", "z_forward", None), ("BRING TO FRONT", "z_front", None)])
        self._buttons(form, "Element", [
            ("VIEW CODE", "view_code", None), ("DUPLICATE", "duplicate", None),
            ("DELETE", "delete", None)])

    def _build_geometry(self, widget, spec: dict) -> None:
        form = self._section("GEOMETRY")
        self._spin(form, "x", "X", spec.get("x", widget.x()), -20000, 20000)
        self._spin(form, "y", "Y", spec.get("y", widget.y()), -20000, 20000)
        self._spin(form, "w", "Width", spec.get("w", widget.width()), 1, 20000)
        self._spin(form, "h", "Height", spec.get("h", widget.height()), 1, 20000)
        self._spin(form, "min_w", "Min Width", spec.get("min_w", 0), 0, 20000)
        self._spin(form, "min_h", "Min Height", spec.get("min_h", 0), 0, 20000)
        self._spin(form, "max_w", "Max Width", spec.get("max_w", 16777215), 0, 16777215)
        self._spin(form, "max_h", "Max Height", spec.get("max_h", 16777215), 0, 16777215)
        self._buttons(form, "Fit", [("FILL PARENT", "fill_parent", None),
                                    ("CENTER IN PARENT", "center_in_parent", None)])

    def _build_transform(self, widget, spec: dict) -> None:
        form = self._section("TRANSFORM")
        self._double(form, "rotation", "Rotation", spec.get("rotation", 0.0), -360, 360, 1.0, "°")
        center = spec.get("rotation_center") or [0.5, 0.5]
        self._double(form, "rotation_center.0", "Center X (0-1)", center[0], -2.0, 3.0, 0.05)
        self._double(form, "rotation_center.1", "Center Y (0-1)", center[1], -2.0, 3.0, 0.05)
        self._buttons(form, "", [("RESET ROTATION", "reset_rotation", None)])

    def _build_appearance(self, widget, spec: dict) -> None:
        form = self._section("APPEARANCE")
        self._slider(form, "opacity", "Element Opacity", spec.get("opacity", 100))
        background = spec.get("background") or {}
        self._color(form, "background.color", "Background", background.get("color", "#04121b"))
        self._slider(form, "background.opacity", "Background Opacity", background.get("opacity", 72))
        gradient = background.get("gradient") or {}
        self._check(form, "background.gradient.enabled", "Gradient", gradient.get("enabled", False))
        self._combo(form, "background.gradient.kind", "Gradient Kind", gradient.get("kind", "linear"),
                    ("linear", "radial"))
        self._spin(form, "background.gradient.angle", "Gradient Angle", gradient.get("angle", 90), 0, 360, 5, "°")
        stops = gradient.get("stops") or [[0.0, ACCENT], [1.0, BG_DEEP]]
        self._color(form, "background.gradient.stops.0.1", "Gradient Start", stops[0][1] if stops else ACCENT)
        self._color(form, "background.gradient.stops.1.1", "Gradient End",
                    stops[-1][1] if stops else BG_DEEP)
        self._spin(form, "background.gradient.stop_count", "Gradient Stops", len(stops), 2, 8)
        overlay = background.get("overlay") or {}
        self._check(form, "background.overlay.enabled", "Overlay Layer", overlay.get("enabled", False))
        self._combo(form, "background.overlay.kind", "Overlay Kind", overlay.get("kind", "dark"),
                    ("dark", "light", "transparent"))
        self._color(form, "background.overlay.color", "Overlay Colour", overlay.get("color", "#000000"))
        self._slider(form, "background.overlay.opacity", "Overlay Opacity", overlay.get("opacity", 35))
        self._check(form, "image.keep_aspect", "Keep Aspect Ratio",
                    get_path(spec, "image.keep_aspect", True))

    def _build_typography(self, widget, spec: dict) -> None:
        form = self._section("TYPOGRAPHY")
        style = spec.get("text_style") or {}
        self._preset_row(form, "Text Presets", "text", TEXT_PRESETS.keys())
        family = QFontComboBox()
        family.setCurrentFont(QFont(str(style.get("family") or "Exo 2")))
        family.currentFontChanged.connect(lambda f, k="text_style.family": self._emit(k, f.family()))
        form.addRow("Font", family)
        self._register("text_style.family", family)
        self._spin(form, "text_style.size", "Size", style.get("size", 11), 1, 200, 1, "pt")
        self._check(form, "text_style.bold", "Bold", style.get("bold", False))
        self._check(form, "text_style.italic", "Italic", style.get("italic", False))
        self._check(form, "text_style.underline", "Underline", style.get("underline", False))
        self._combo(form, "text_style.align", "Alignment", style.get("align", "left"), ALIGN_H)
        self._combo(form, "text_style.v_align", "Vertical", style.get("v_align", "middle"), ALIGN_V)
        self._check(form, "text_style.wrap", "Word Wrap", style.get("wrap", True))
        self._double(form, "text_style.letter_spacing", "Letter Spacing",
                     style.get("letter_spacing", 0.0), -20.0, 40.0, 0.5, "px")
        self._color(form, "text_style.color", "Text Colour", style.get("color", TEXT))
        self._slider(form, "text_style.opacity", "Text Opacity", style.get("opacity", 100))
        support = getattr(self, "_support", lambda _feature: True)
        if support("text_stroke"):
            form.addRow(_label("─ TEXT STROKE ─", ACCENT, "7pt", 800))
            stroke = style.get("stroke") or {}
            self._check(form, "text_style.stroke.enabled", "Stroke Enabled", stroke.get("enabled", False))
            self._color(form, "text_style.stroke.color", "Stroke Colour", stroke.get("color", ACCENT_BRIGHT))
            self._double(form, "text_style.stroke.width", "Stroke Width", stroke.get("width", 2.0), 0.0, 20.0, 0.5, "px")
            self._slider(form, "text_style.stroke.opacity", "Stroke Opacity", stroke.get("opacity", 100))
        if support("text_effects"):
            glow = style.get("glow") or {}
            self._check(form, "text_style.glow.enabled", "Text Glow", glow.get("enabled", False))
            self._color(form, "text_style.glow.color", "Glow Colour", glow.get("color", ACCENT))
            self._spin(form, "text_style.glow.radius", "Glow Radius", glow.get("radius", 8), 0, 40)
            self._slider(form, "text_style.glow.opacity", "Glow Opacity", glow.get("opacity", 80))
            shadow = style.get("shadow") or {}
            self._check(form, "text_style.shadow.enabled", "Text Shadow", shadow.get("enabled", False))
            self._color(form, "text_style.shadow.color", "Shadow Colour", shadow.get("color", "#000000"))
            self._spin(form, "text_style.shadow.dx", "Shadow X", shadow.get("dx", 1), -20, 20)
            self._spin(form, "text_style.shadow.dy", "Shadow Y", shadow.get("dy", 1), -20, 20)
            self._spin(form, "text_style.shadow.blur", "Shadow Blur", shadow.get("blur", 6), 0, 40)
            self._slider(form, "text_style.shadow.opacity", "Shadow Opacity", shadow.get("opacity", 70))
        else:
            note = _label(
                "Text outline, glow and shadow are painted by the custom element engine, so they "
                "apply to elements created in the Studio (not to runtime J.A.R.V.I.S. widgets).",
                TEXT_DIM, "7pt", 600)
            note.setWordWrap(True)
            form.addRow(note)

    def _build_content(self, widget, spec: dict) -> None:
        form = self._section("CONTENT")
        self._text(form, "text", "Text", spec.get("text", ""), multiline=True,
                   placeholder="element text…")
        self._text(form, "tooltip", "Tooltip", spec.get("tooltip", ""))
        self._text(form, "interaction.click_action", "Click Action",
                   get_path(spec, "interaction.click_action", ""),
                   placeholder="spoken phrase or blank")

    def _build_image(self, widget, spec: dict) -> None:
        form = self._section("IMAGE")
        image = spec.get("image") or {}
        path = str(image.get("path") or "")
        path_line = QLineEdit(path)
        path_line.setReadOnly(True)
        path_line.setStyleSheet(_input_style())
        form.addRow("Image Path", path_line)
        self._register("image.path", path_line)
        self._text(form, "image.asset", "Asset Name", image.get("asset", ""),
                   placeholder="asset registry name")
        self._buttons(form, "Asset", [
            ("IMPORT IMAGE", "import_image", None), ("REPLACE IMAGE", "replace_image", None),
            ("USE SELECTED ASSET", "use_asset", None)])
        self._buttons(form, "Background", [
            ("SET AS BACKGROUND", "set_background", None),
            ("SET AS FOREGROUND", "set_foreground", None)])
        self._combo(form, "image.fit", "Fit Mode", image.get("fit", "Contain"), FIT_MODES)
        self._combo(form, "image.align_h", "Align H", image.get("align_h", "center"), ALIGN_H)
        self._combo(form, "image.align_v", "Align V", image.get("align_v", "middle"), ALIGN_V)
        self._slider(form, "image.opacity", "Image Opacity", image.get("opacity", 100))
        self._check(form, "image.is_background", "Image Is Background", image.get("is_background", False))
        self._check(form, "image.smoothing", "Smoothing", image.get("smoothing", True))
        self._check(form, "image.keep_aspect", "Keep Aspect", image.get("keep_aspect", True))
        self._color(form, "image.tint", "Tint Colour", image.get("tint") or "#00d4ff")
        self._slider(form, "image.tint_strength", "Tint Strength", image.get("tint_strength", 0))
        crop = image.get("crop") or [0, 0, 0, 0]
        for index, label in enumerate(("Crop Left", "Crop Top", "Crop Right", "Crop Bottom")):
            self._spin(form, f"image.crop.{index}", label, crop[index], 0, 100000)
        self._buttons(form, "Crop", [("RESET CROP", "reset_crop", None)])
        self._buttons(form, "Order", [("SEND TO BACK", "z_back", None),
                                      ("BRING TO FRONT", "z_front", None)])

    def _build_layout(self, widget, spec: dict) -> None:
        form = self._section("LAYOUT")
        layout = spec.get("layout") or {}
        self._combo(form, "layout.anchor", "Anchor", layout.get("anchor", "absolute"),
                    ("absolute", "top-left", "top", "top-right", "left", "center", "right",
                     "fill", "bottom-left", "bottom", "bottom-right"))
        self._spin(form, "layout.padding", "Padding", layout.get("padding", 0), 0, 80)
        self._spin(form, "layout.spacing", "Spacing", layout.get("spacing", 0), 0, 80)
        parent = widget.parentWidget()
        summary = "ABSOLUTE / DIRECT"
        if widget_alive(parent):
            layout_obj = parent.layout()
            if layout_obj is not None:
                try:
                    margins = layout_obj.contentsMargins()
                    summary = (f"{type(layout_obj).__name__} · spacing {layout_obj.spacing()} · "
                               f"margins {margins.left()}/{margins.top()}/"
                               f"{margins.right()}/{margins.bottom()}")
                except Exception:
                    summary = type(layout_obj).__name__
        row_label = _label(summary, TEXT_MID, "7pt", 600)
        row_label.setWordWrap(True)
        form.addRow("Parent Layout", row_label)
        self._check(form, "visible", "Visible", spec.get("visible", True))
        self._check(form, "enabled", "Enabled", spec.get("enabled", True))

    def _build_effects(self, widget, spec: dict) -> None:
        form = self._section("EFFECTS")
        effects = spec.get("effects") or []
        self.effect_list = QListWidget()
        self.effect_list.setMaximumHeight(120)
        for index, effect in enumerate(effects):
            item = QListWidgetItem(f"{index + 1}. {effect.get('kind', 'Effect')}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if as_bool(effect.get("enabled", True), True)
                               else Qt.CheckState.Unchecked)
            self.effect_list.addItem(item)
        if effects:
            self.effect_list.setCurrentRow(0)
        self._register("effects", self.effect_list)
        form.addRow(self.effect_list)
        supported, reason = effects_supported()
        add_row = QWidget()
        add_layout = QHBoxLayout(add_row)
        add_layout.setContentsMargins(0, 0, 0, 0)
        add_layout.setSpacing(4)
        self.effect_kind = QComboBox()
        self.effect_kind.addItems([str(k) for k in EFFECT_KINDS])
        add_button = QPushButton("ADD EFFECT")
        add_button.clicked.connect(lambda: self.effectAdded.emit(self.effect_kind.currentText()))
        remove_button = QPushButton("REMOVE")
        remove_button.clicked.connect(lambda: self.effectRemoved.emit(self.effect_list.currentRow()))
        up_button = QPushButton("▲")
        down_button = QPushButton("▼")
        for button in (up_button, down_button):
            button.setFixedWidth(30)
        up_button.clicked.connect(lambda: self.effectMoved.emit(self.effect_list.currentRow(), -1))
        down_button.clicked.connect(lambda: self.effectMoved.emit(self.effect_list.currentRow(), 1))
        add_layout.addWidget(self.effect_kind, 1)
        add_layout.addWidget(add_button)
        add_layout.addWidget(remove_button)
        add_layout.addWidget(up_button)
        add_layout.addWidget(down_button)
        form.addRow("Stack", add_row)
        self.effect_list.itemChanged.connect(self._effect_check_changed)
        self.effect_list.currentRowChanged.connect(self._effect_row_changed)
        if not supported:
            note = _label(reason, "#ffaa22", "7pt", 600)
            note.setWordWrap(True)
            form.addRow(note)
        if not getattr(self, "_support", lambda _f: True)("effects_stack") and effects:
            note = _label(
                "Pixel effects run in the custom element engine; this runtime widget keeps its "
                "existing look. Create a Studio element to use the stack.", TEXT_DIM, "7pt", 600)
            note.setWordWrap(True)
            form.addRow(note)
        self.effect_params = QGroupBox("EFFECT PARAMETERS")
        self.effect_params_form = QFormLayout(self.effect_params)
        self.effect_params_form.setContentsMargins(7, 12, 7, 7)
        self.effect_params_form.setSpacing(6)
        self.layout.addWidget(self.effect_params)
        self._fill_effect_params(widget, spec)

    def _effect_check_changed(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        self.effectToggled.emit(self.effect_list.row(item),
                                item.checkState() == Qt.CheckState.Checked)

    def _effect_row_changed(self, row: int) -> None:
        self._selected_effect = int(row)
        if not self._loading:
            self._fill_effect_params(self._subject, self._spec)

    def _fill_effect_params(self, widget, spec: dict | None) -> None:
        if spec is None or not hasattr(self, "effect_params_form"):
            return
        while self.effect_params_form.count():
            item = self.effect_params_form.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()
        effects = spec.get("effects") or []
        row = self.effect_list.currentRow() if hasattr(self, "effect_list") else -1
        if row < 0 or row >= len(effects):
            self.effect_params_form.addRow(_label("Select an effect to edit its parameters.",
                                                  TEXT_DIM, "7pt", 600))
            return
        effect = effects[row]
        kind = str(effect.get("kind") or "")
        schema = EFFECT_SCHEMA.get(kind, {})
        params = dict(effect.get("params") or {})
        for key, spec_tuple in schema.items():
            path = f"effects.{row}.params.{key}"
            current = params.get(key, spec_tuple[-1])
            if spec_tuple[0] == "int":
                self._spin(self.effect_params_form, path, key.title(), current,
                           spec_tuple[1], spec_tuple[2])
            elif spec_tuple[0] == "color":
                self._color(self.effect_params_form, path, key.title(), current or ACCENT)

    def _build_borders(self, widget, spec: dict) -> None:
        form = self._section("BORDERS")
        borders = spec.get("borders") or {}
        for side in ("top", "right", "bottom", "left"):
            block = borders.get(side) or {}
            form.addRow(_label(side.upper(), ACCENT, "7pt", 800))
            self._spin(form, f"borders.{side}.width", "  Width", block.get("width", 0), 0, 60)
            self._color(form, f"borders.{side}.color", "  Colour", block.get("color", ACCENT))
            self._combo(form, f"borders.{side}.style", "  Style", block.get("style", "Solid"), BORDER_STYLES)
            self._slider(form, f"borders.{side}.opacity", "  Opacity", block.get("opacity", 100))
        self._buttons(form, "All Sides", [("APPLY ALL", "apply_all_borders", None),
                                          ("CLEAR ALL", "clear_borders", None)])
        radius = borders.get("radius") or {}
        for corner, label in (("tl", "Radius Top Left"), ("tr", "Radius Top Right"),
                              ("br", "Radius Bottom Right"), ("bl", "Radius Bottom Left")):
            self._spin(form, f"borders.radius.{corner}", label, radius.get(corner, 0), 0, 400)

    def _build_stroke(self, widget, spec: dict) -> None:
        form = self._section("STROKE / OUTLINE")
        stroke = spec.get("stroke") or {}
        self._check(form, "stroke.enabled", "Stroke Enabled", stroke.get("enabled", False))
        self._color(form, "stroke.color", "Stroke Colour", stroke.get("color", ACCENT))
        self._double(form, "stroke.width", "Stroke Width", stroke.get("width", 2.0), 0.0, 20.0, 0.5, "px")
        self._slider(form, "stroke.opacity", "Stroke Opacity", stroke.get("opacity", 100))
        self._spin(form, "stroke.radius", "Stroke Radius", stroke.get("radius", 10), 0, 400)
        self._preset_row(form, "Presets", "stroke", ("Glowing Panel", "Outlined Button", "Neon Card"))

    def _build_shadow(self, widget, spec: dict) -> None:
        form = self._section("SHADOW")
        shadow = spec.get("shadow") or {}
        self._check(form, "shadow.enabled", "Shadow Enabled", shadow.get("enabled", False))
        self._color(form, "shadow.color", "Shadow Colour", shadow.get("color", "#000000"))
        self._slider(form, "shadow.opacity", "Shadow Opacity", shadow.get("opacity", 60))
        self._spin(form, "shadow.blur", "Shadow Blur", shadow.get("blur", 18), 0, 200)
        self._spin(form, "shadow.dx", "Shadow X Offset", shadow.get("dx", 0), -80, 80)
        self._spin(form, "shadow.dy", "Shadow Y Offset", shadow.get("dy", 6), -80, 80)

    def _build_glow(self, widget, spec: dict) -> None:
        form = self._section("GLOW")
        glow = spec.get("glow") or {}
        self._check(form, "glow.enabled", "Glow Enabled", glow.get("enabled", False))
        self._color(form, "glow.color", "Glow Colour", glow.get("color", ACCENT))
        self._slider(form, "glow.strength", "Glow Strength", glow.get("strength", 60))
        self._spin(form, "glow.radius", "Glow Radius", glow.get("radius", 24), 0, 200)
        self._slider(form, "glow.opacity", "Glow Opacity", glow.get("opacity", 80))
        self._spin(form, "glow.spread", "Glow Spread", glow.get("spread", 0), 0, 40)
        self._buttons(form, "Presets", [
            ("CYAN", "glow_preset", "Cyan"), ("BLUE", "glow_preset", "Blue"),
            ("NEON", "glow_preset", "Neon"), ("OFF", "glow_preset", "Off")])

    def _build_animation(self, widget, spec: dict) -> None:
        form = self._section("ANIMATION")
        animation = spec.get("animation") or {}
        self._combo(form, "animation.name", "Animation", animation.get("name", "None"), ANIMATION_NAMES)
        self._spin(form, "animation.duration", "Duration", animation.get("duration", 600), 30, 20000, 50, " ms")
        self._spin(form, "animation.delay", "Delay", animation.get("delay", 0), 0, 20000, 50, " ms")
        self._spin(form, "animation.loops", "Loop", animation.get("loops", 1), 0, 999)
        self._combo(form, "animation.easing", "Easing", animation.get("easing", "In Out Quad"),
                    ("Linear", "In Quad", "Out Quad", "In Out Quad", "In Cubic", "Out Cubic",
                     "In Out Cubic", "Out Back", "Out Bounce", "Out Elastic"))
        self._check(form, "animation.reverse", "Reverse", animation.get("reverse", False))
        self._buttons(form, "Playback", [("▶ PLAY", "anim_play", None), ("■ STOP", "anim_stop", None),
                                         ("STOP ALL", "anim_stop_all", None)])

    def _build_interaction(self, widget, spec: dict) -> None:
        form = self._section("INTERACTION / STATES")
        self._combo(form, "interaction.cursor", "Cursor", get_path(spec, "interaction.cursor", "arrow"),
                    ("arrow", "pointing", "open hand", "closed hand", "text", "cross", "size all"))
        states = spec.get("states") or {}
        state_row = QWidget()
        state_layout = QHBoxLayout(state_row)
        state_layout.setContentsMargins(0, 0, 0, 0)
        state_layout.setSpacing(4)
        self.state_combo = QComboBox()
        self.state_combo.addItems(["hover", "pressed", "focused", "disabled", "selected"])
        self.state_combo.setCurrentText(self._state_name)
        self.state_combo.currentTextChanged.connect(self._state_changed)
        state_layout.addWidget(self.state_combo, 1)
        form.addRow("Edit State", state_row)
        self._build_state_rows(form, spec, self._state_name)
        self._buttons(form, "", [("CLEAR STATE", "clear_state", None)])

    def _state_changed(self, name: str) -> None:
        self._state_name = str(name)
        if self._loading or self._spec is None:
            return
        # Rebuilding while a child is dispatching its own signal is what caused
        # the old use-after-free; defer to the next event-loop turn.
        spec, widget, multi = self._spec, self._subject, self._multi
        QTimer.singleShot(0, lambda: self.refresh(widget, spec, multi))

    def _build_state_rows(self, form: QFormLayout, spec: dict, state_name: str) -> None:
        state = (spec.get("states") or {}).get(state_name) or {}
        base = f"states.{state_name}"
        self._check(form, f"{base}.enabled", f"{state_name.title()} Enabled", state.get("enabled", False))
        self._color(form, f"{base}.background", f"{state_name.title()} Background",
                    state.get("background") or "#0a2434")
        self._color(form, f"{base}.border_color", f"{state_name.title()} Border",
                    state.get("border_color") or ACCENT)
        self._color(form, f"{base}.text_color", f"{state_name.title()} Text",
                    state.get("text_color") or TEXT_BRIGHT)
        self._slider(form, f"{base}.opacity", f"{state_name.title()} Opacity",
                     state.get("opacity", 100))
        self._spin(form, f"{base}.glow_radius", f"{state_name.title()} Glow Radius",
                   state.get("glow_radius", 0), 0, 64)

    def _build_parenting(self, widget, spec: dict) -> None:
        form = self._section("PARENTING")
        parent_names: list[str] = []
        if self.editor is not None:
            try:
                parent_names = [str(n) for n in self.editor.target_names()]
            except Exception:
                parent_names = []
        combo = QComboBox()
        combo.addItems(parent_names or [str(spec.get("parent") or "")])
        if str(spec.get("parent") or "") in parent_names:
            combo.setCurrentText(str(spec.get("parent")))
        form.addRow("Parent", combo)
        self._register("parent", combo)
        self._buttons(form, "Reparent", [
            ("APPLY PARENT", "reparent", None), ("ADD CHILD", "add_child", None),
            ("GROUP", "group", None), ("UNGROUP", "ungroup", None)])
        parent = widget.parentWidget()
        current = parent.objectName() if widget_alive(parent) else "—"
        form.addRow("Current Parent", _label(current or class_simple_name(parent), TEXT_MID, "7pt", 600))

    def _build_script(self, widget, spec: dict) -> None:
        form = self._section("SCRIPT")
        script = spec.get("script") or {}
        self._check(form, "script.enabled", "Script Enabled", script.get("enabled", False))
        self._text(form, "script.code", "Script Body", script.get("code", ""), multiline=True,
                   placeholder="def on_click(element, event): ...")
        self._buttons(form, "Actions", [
            ("EDIT SCRIPT", "edit_script", None), ("SAVE SCRIPT", "save_script", None),
            ("RUN / TEST", "run_script", None), ("DISABLE", "disable_script", None)])
        lines = len(str(script.get("code") or "").splitlines())
        form.addRow("Script Lines", _label(str(lines), TEXT_MID, "7pt", 600))
    def _build_style(self, widget, spec: dict) -> None:
        form = self._section("STYLE / QSS")
        self._preset_row(form, "HUD Presets", "hud", HUD_PRESETS.keys())
        qss = QPlainTextEdit(str(spec.get("stylesheet") or ""))
        qss.setMaximumHeight(120)
        form.addRow("QSS", qss)
        self._register("stylesheet", qss)
        self._buttons(form, "", [("APPLY QSS", "apply_qss", qss), ("CLEAR STYLE", "clear_style", None)])
    def _build_advanced(self, widget, spec: dict) -> None:
        form = self._section("ADVANCED")
        meta = _DebouncedTextEdit(json.dumps(spec.get("custom") or {}, indent=2), delay=400)
        meta.setMaximumHeight(90)
        form.addRow("Custom Metadata", meta)
        self._register("custom", meta)
        self._buttons(form, "", [("APPLY METADATA", "apply_metadata", meta),
                                 ("COPY JSON", "copy_json", None),
                                 ("RESET ELEMENT", "reset_element", None)])
        raw = QPlainTextEdit(json.dumps(spec, indent=2, default=str)[:4000])
        raw.setReadOnly(True)
        raw.setMaximumHeight(150)
        form.addRow("Element JSON", raw)
        form.addRow("Studio", _label("Properties, effect stack and states are all serialised.",
                                     TEXT_DIM, "7pt", 600))


# ===========================================================================
# Animation
# ===========================================================================
EASINGS = {
    "Linear": QEasingCurve.Type.Linear,
    "In Quad": QEasingCurve.Type.InQuad,
    "Out Quad": QEasingCurve.Type.OutQuad,
    "In Out Quad": QEasingCurve.Type.InOutQuad,
    "In Cubic": QEasingCurve.Type.InCubic,
    "Out Cubic": QEasingCurve.Type.OutCubic,
    "In Out Cubic": QEasingCurve.Type.InOutCubic,
    "Out Back": QEasingCurve.Type.OutBack,
    "Out Bounce": QEasingCurve.Type.OutBounce,
    "Out Elastic": QEasingCurve.Type.OutElastic,
}


class AnimationManager:
    """Owns every animation the Studio starts, so every one can be stopped.

    Two rules drive this design: animations must never run away, and they must
    never outlive the widget they animate -- a QPropertyAnimation holding a
    deleted QWidget is a crash.  Each animation is registered against
    ``id(widget)``, the widget's ``destroyed`` signal tears it down, and starting
    a new animation on a widget stops the previous one first.
    """

    def __init__(self, log=None):
        self._running: dict[int, QAbstractAnimation] = {}
        self._effects: dict[int, Any] = {}
        self._restore: dict[int, dict] = {}
        self._log = log

    def _emit(self, message: str) -> None:
        if callable(self._log):
            try:
                self._log(message)
            except Exception:
                pass

    # -- lifecycle -------------------------------------------------------
    def stop(self, widget, restore: bool = True) -> None:
        if widget is None:
            return
        key = id(widget)
        animation = self._running.pop(key, None)
        if animation is not None:
            try:
                animation.stop()
                animation.deleteLater()
            except RuntimeError:
                pass
        state = self._restore.pop(key, None)
        self._effects.pop(key, None)
        if not widget_alive(widget):
            return
        had_effect = False
        try:
            had_effect = widget.graphicsEffect() is not None
        except RuntimeError:
            return
        try:
            if restore and isinstance(state, dict):
                if "geometry" in state:
                    widget.setGeometry(state["geometry"])
                if "visible" in state:
                    widget.setVisible(bool(state["visible"]))
            # Only clear the effect when this animation installed one; the
            # element engine owns its own glow/shadow otherwise.
            if had_effect and restore and state is not None:
                widget.setGraphicsEffect(None)
                self._emit("[ANIM] effect released (element glow/shadow will re-apply)")
        except RuntimeError:
            pass

    def stop_all(self) -> None:
        for key in list(self._running):
            animation = self._running.pop(key, None)
            if animation is not None:
                try:
                    animation.stop()
                    animation.deleteLater()
                except RuntimeError:
                    pass
        self._running.clear()
        self._effects.clear()
        self._restore.clear()
        self._emit("[ANIM] all animations stopped")

    def is_running(self, widget) -> bool:
        return widget is not None and id(widget) in self._running

    # -- construction ----------------------------------------------------
    def _opacity_effect(self, widget) -> QGraphicsOpacityEffect:
        effect = widget.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
        self._effects[id(widget)] = effect
        return effect

    def _glow_effect(self, widget, colour: str) -> QGraphicsDropShadowEffect:
        effect = QGraphicsDropShadowEffect(widget)
        effect.setOffset(0, 0)
        effect.setColor(qcolor(colour))
        effect.setBlurRadius(0)
        widget.setGraphicsEffect(effect)
        self._effects[id(widget)] = effect
        return effect

    def play(self, widget, name: str, duration: int = 600, delay: int = 0,
             loops: int = 1, easing: str = "In Out Quad", reverse: bool = False,
             glow_color: str = ACCENT) -> bool:
        if not widget_alive(widget) or str(name or "None") == "None":
            return False
        self.stop(widget)

        duration = max(30, min(20000, int(duration)))
        delay = max(0, min(20000, int(delay)))
        loops = int(loops)
        if loops == 0:
            loops = -1                        # 0 in the UI means "loop forever"
        loops = -1 if loops < 0 else max(1, min(9999, loops))
        curve = EASINGS.get(easing, QEasingCurve.Type.InOutQuad)
        key = id(widget)
        self._restore[key] = {"geometry": QRect(widget.geometry()), "visible": widget.isVisible()}

        try:
            animation = self._build(widget, name, duration, curve, reverse, glow_color)
        except Exception as exc:
            self._emit(f"[ANIM] {name} failed to build — {exc}")
            self._restore.pop(key, None)
            return False
        if animation is None:
            self._emit(f"[ANIM] {name} is not supported for this element")
            self._restore.pop(key, None)
            return False

        animation.setLoopCount(loops)
        if delay:
            group = QSequentialAnimationGroup(widget)
            group.addPause(delay)
            group.addAnimation(animation)
            animation = group

        self._running[key] = animation
        try:
            widget.destroyed.connect(lambda *_, k=key: self._forget(k))
        except Exception:
            pass
        animation.finished.connect(lambda k=key: self._finished(k))
        animation.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)
        self._emit(f"[ANIM] {name} · {duration}ms · "
                   f"{'loop' if loops < 0 else f'{loops}x'}")
        return True

    def _build(self, widget, name: str, duration: int, curve, reverse: bool, glow_color: str):
        name = str(name or "").strip()
        geo = QRect(widget.geometry())

        if name in ("Fade In", "Fade Out", "Blink", "Pulse", "Loading"):
            effect = self._opacity_effect(widget)
            animation = QPropertyAnimation(effect, b"opacity", widget)
            animation.setDuration(duration)
            animation.setEasingCurve(curve)
            if name == "Fade In":
                animation.setStartValue(0.0)
                animation.setEndValue(1.0)
            elif name == "Fade Out":
                animation.setStartValue(1.0)
                animation.setEndValue(0.0)
            elif name == "Blink":
                animation.setKeyValueAt(0.0, 1.0)
                animation.setKeyValueAt(0.49, 1.0)
                animation.setKeyValueAt(0.5, 0.0)
                animation.setKeyValueAt(1.0, 0.0)
            else:
                animation.setKeyValueAt(0.0, 1.0)
                animation.setKeyValueAt(0.5, 0.35)
                animation.setKeyValueAt(1.0, 1.0)
            if reverse:
                animation.setDirection(QAbstractAnimation.Direction.Backward)
            return animation

        if name in ("Glow Pulse", "Hover Glow"):
            effect = self._glow_effect(widget, glow_color)
            animation = QPropertyAnimation(effect, b"blurRadius", widget)
            animation.setDuration(duration)
            animation.setEasingCurve(curve)
            if name == "Hover Glow":
                animation.setStartValue(0.0)
                animation.setEndValue(28.0)
            else:
                animation.setKeyValueAt(0.0, 2.0)
                animation.setKeyValueAt(0.5, 30.0)
                animation.setKeyValueAt(1.0, 2.0)
            return animation

        if name == "Hover Brightness":
            effect = QGraphicsColorizeEffect(widget)
            effect.setColor(qcolor(glow_color))
            effect.setStrength(0.0)
            widget.setGraphicsEffect(effect)
            self._effects[id(widget)] = effect
            animation = QPropertyAnimation(effect, b"strength", widget)
            animation.setDuration(duration)
            animation.setEasingCurve(curve)
            animation.setKeyValueAt(0.0, 0.0)
            animation.setKeyValueAt(0.5, 0.65)
            animation.setKeyValueAt(1.0, 0.0)
            return animation

        if name.startswith("Slide"):
            offset = {"Slide Left": (-geo.width(), 0), "Slide Right": (geo.width(), 0),
                      "Slide Up": (0, -geo.height()), "Slide Down": (0, geo.height())}[name]
            start = QRect(geo.x() + offset[0], geo.y() + offset[1], geo.width(), geo.height())
            animation = QPropertyAnimation(widget, b"geometry", widget)
            animation.setDuration(duration)
            animation.setEasingCurve(curve)
            animation.setStartValue(start if not reverse else geo)
            animation.setEndValue(geo if not reverse else start)
            return animation

        if name in ("Scale Up", "Scale Down", "Press"):
            factor = {"Scale Up": 1.18, "Scale Down": 0.82, "Press": 0.94}[name]
            centre = geo.center()
            scaled = QRect(0, 0, max(1, int(geo.width() * factor)), max(1, int(geo.height() * factor)))
            scaled.moveCenter(centre)
            animation = QPropertyAnimation(widget, b"geometry", widget)
            animation.setDuration(duration)
            animation.setEasingCurve(curve)
            animation.setKeyValueAt(0.0, geo)
            animation.setKeyValueAt(0.5, scaled)
            animation.setKeyValueAt(1.0, geo)
            return animation

        return None

    def _forget(self, key: int) -> None:
        animation = self._running.pop(key, None)
        if animation is not None:
            try:
                animation.stop()
            except RuntimeError:
                pass
        self._effects.pop(key, None)
        self._restore.pop(key, None)

    def _finished(self, key: int) -> None:
        self._running.pop(key, None)


class AnimationPanel(QWidget):
    """ANIMATION tab: real QPropertyAnimation playback plus safe teardown."""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        self.subject = _label("No selection", TEXT_DIM, "7.5pt", 700)
        root.addWidget(self.subject)

        box = QGroupBox("ANIMATION")
        box.setStyleSheet(_section_style())
        form = QFormLayout(box)
        form.setContentsMargins(7, 12, 7, 7)
        form.setSpacing(6)
        self.kind = QComboBox()
        self.kind.addItems(ANIMATION_NAMES)
        self.kind.setStyleSheet(_input_style())
        form.addRow("Animation", self.kind)
        self.duration = QSpinBox()
        self.duration.setRange(30, 20000)
        self.duration.setValue(600)
        self.duration.setSingleStep(50)
        self.duration.setSuffix(" ms")
        self.duration.setStyleSheet(_input_style())
        form.addRow("Duration", self.duration)
        self.delay = QSpinBox()
        self.delay.setRange(0, 20000)
        self.delay.setSingleStep(50)
        self.delay.setSuffix(" ms")
        self.delay.setStyleSheet(_input_style())
        form.addRow("Delay", self.delay)
        self.loops = QSpinBox()
        self.loops.setRange(0, 999)
        self.loops.setValue(1)
        self.loops.setToolTip("0 = loop forever (STOP always works)")
        self.loops.setStyleSheet(_input_style())
        form.addRow("Loop", self.loops)
        self.easing = QComboBox()
        self.easing.addItems(list(EASINGS.keys()))
        self.easing.setCurrentText("In Out Quad")
        self.easing.setStyleSheet(_input_style())
        form.addRow("Easing", self.easing)
        self.reverse = QCheckBox("Reverse")
        form.addRow("Direction", self.reverse)
        self.glow_color = QPushButton(ACCENT)
        self.glow_color.clicked.connect(self._pick_color)
        self._glow = ACCENT
        self._paint_swatch()
        form.addRow("Glow Colour", self.glow_color)
        root.addWidget(box)

        buttons = QHBoxLayout()
        self.play_btn = QPushButton("▶  PLAY")
        self.stop_btn = QPushButton("■  STOP")
        self.stop_all_btn = QPushButton("STOP ALL")
        self.save_btn = QPushButton("SAVE TO ELEMENT")
        self.play_btn.clicked.connect(self.play)
        self.stop_btn.clicked.connect(self.stop)
        self.stop_all_btn.clicked.connect(self.stop_all)
        self.save_btn.clicked.connect(self.save_to_element)
        buttons.addWidget(self.play_btn, 1)
        buttons.addWidget(self.stop_btn, 1)
        buttons.addWidget(self.stop_all_btn)
        root.addLayout(buttons)
        root.addWidget(self.save_btn)

        note = _label("Animations run on the real widget. Loop 0 runs until STOP. Slide and Scale "
                      "animate geometry, so the element returns to its saved position when stopped — "
                      "the layout file is never rewritten by a preview.", TEXT_DIM, "7pt", 600)
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch(1)

    def _paint_swatch(self) -> None:
        self.glow_color.setText(self._glow)
        self.glow_color.setStyleSheet(
            f"QPushButton{{background:{self._glow};color:#02060c;"
            f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;"
            f"font:700 8pt {FONT_LOG};}}")

    def _pick_color(self) -> None:
        chosen = QColorDialog.getColor(qcolor(self._glow), self, "Glow Colour")
        if chosen.isValid():
            self._glow = chosen.name()
            self._paint_swatch()

    def set_subject(self, widget) -> None:
        if not widget_alive(widget):
            self.subject.setText("No selection")
            return
        self.subject.setText(f"{class_simple_name(widget)} · {widget.objectName() or 'no objectName'}")

    def play(self) -> None:
        widget = self.editor.selection_primary()
        if not widget_alive(widget):
            self.editor.log("[ANIM] nothing selected")
            return
        ok = self.editor.animations.play(
            widget, self.kind.currentText(), duration=self.duration.value(),
            delay=self.delay.value(), loops=self.loops.value(),
            easing=self.easing.currentText(), reverse=self.reverse.isChecked(),
            glow_color=self._glow)
        if ok:
            self.editor.statusBar().showMessage(f"ANIMATING · {self.kind.currentText()}")

    def stop(self) -> None:
        self.editor.animations.stop(self.editor.selection_primary())
        self.editor.log("[ANIM] stopped")

    def stop_all(self) -> None:
        self.editor.animations.stop_all()

    def save_to_element(self) -> None:
        widget = self.editor.selection_primary()
        if not widget_alive(widget):
            return
        if self.kind.currentText() == "None":
            self.editor.set_property(widget, "animation.name", "None")
            return
        self.editor.set_property(widget, "animation.name", self.kind.currentText())
        self.editor.set_property(widget, "animation.duration", int(self.duration.value()))
        self.editor.set_property(widget, "animation.delay", int(self.delay.value()))
        self.editor.set_property(widget, "animation.loops", int(self.loops.value()))
        self.editor.set_property(widget, "animation.easing", self.easing.currentText())
        self.editor.set_property(widget, "animation.reverse", bool(self.reverse.isChecked()))
        self.editor.statusBar().showMessage("ANIMATION SAVED TO ELEMENT")


__all__ = [
    "HistoryManager",
    "SelectionManager",
    "ClipboardManager",
    "AlignmentManager",
    "ScriptManager",
    "CodeEditor",
    "OutputPanel",
    "AssetBrowser",
    "ExplorerTree",
    "ViewportCanvas",
    "PropertyInspector",
    "AnimationManager",
    "AnimationPanel",
    "EASINGS",
]
