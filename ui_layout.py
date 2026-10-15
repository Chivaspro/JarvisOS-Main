"""J.A.R.V.I.S. live Layout Studio.

The Studio is a thin, hard-shelled editor over the running Qt widget tree.  It
never replaces application widgets with mock controls: selecting a panel renders
that real widget, property edits are applied to the real object immediately, and
the same runtime objects are saved back into ``config/ui_layout.json``.

Design rules the whole file follows (these are the fixes for the old white-screen
crash and for the "not enough Properties" complaint):

* every widget reference is validated through :func:`_widget_alive` before use,
  and a stale selection is dropped rather than dereferenced;
* ``paintEvent`` (in :class:`ui_layout_parts.ViewportCanvas`) never raises, never
  re-enters ``update()`` and keeps the last good frame, so a rendering failure
  can only ever leave the viewport intact;
* the Properties panel is rebuilt only when the selection changes -- never from
  inside a child control's own signal dispatch, which is what used to delete the
  very widget Qt was executing;
* a continuous drag is one history entry and one debounced save, so mouse
  movement cannot produce hundreds of writes;
* every feature is real: HUD presets, the effect stack, stroke, glow, shadow,
  gradients, text outline, states, scripts and the asset manager all drive actual
  Qt rendering (see :mod:`ui_layout_core`) and none of them are placeholders.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal, QBuffer, QIODevice
from PyQt6.QtGui import QColor, QFont, QImage, QKeySequence, QPainter, QPalette, QPixmap, QRegion, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtSvg import QSvgGenerator
    _FIGMA_QTSVG_AVAILABLE = True
except Exception:
    QSvgGenerator = None
    _FIGMA_QTSVG_AVAILABLE = False

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
    composed_app_css as _composed_app_css,
    qcolor as _qcolor,
    bind_module_globals as _bind_theme_globals,
)

# The Studio paints with the token values it bound at import time, so a live
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
    ELEMENT_SIZES,
    ELEMENT_TYPES,
    FIT_MODES,
    HUD_PRESETS,
    IMAGE_CACHE,
    IMAGE_SUFFIXES,
    LAYOUT_DEFAULTS,
    SCHEMA_VERSION,
    TEXT_PRESETS,
    AssetStore,
    ElementWidget,
    apply_effect_model,
    apply_preset,
    apply_runtime_style,
    apply_widget_state as _core_apply_widget_state,
    as_bool,
    as_color,
    build_qss,
    clamp_float,
    clamp_int,
    class_simple_name,
    default_spec,
    effects_supported,
    elements_from_layout,
    elements_to_layout,
    get_path,
    is_element_widget,
    load_layout,
    normalize_element,
    save_layout,
    set_path,
    widget_alive,
)

from figma_export import (
    FigmaExportError,
    export_panel_to_figma,
    normalize_runtime_svg_fragment,
    parse_qt_paint_value,
    qss_border,
    qss_paint,
    qss_property,
    qss_radius,
    runtime_fragment_has_content,
    split_color,
)

from ui_layout_parts import (
    AlignmentManager,
    AnimationManager,
    AnimationPanel,
    AssetBrowser,
    ClipboardManager,
    CodeEditor,
    ExplorerTree,
    HistoryManager,
    OutputPanel,
    PropertyInspector,
    ScriptManager,
    SelectionManager,
    ViewportCanvas,
)

LAYOUT_VERSION = SCHEMA_VERSION

# The old module exposed ``_widget_alive``; keep the name so any existing caller
# (and the standalone launcher) keeps working.
_widget_alive = widget_alive
_safe_object_name = None       # assigned below (kept for API compatibility)
_class_short = class_simple_name


# ---------------------------------------------------------------------------
# Compatibility helpers (unchanged public surface)
# ---------------------------------------------------------------------------
def clamp_size(width: int, height: int, min_size=(80, 80), max_size=(2000, 1400)) -> QSize:
    w = max(min_size[0], min(max_size[0], int(width)))
    h = max(min_size[1], min(max_size[1], int(height)))
    return QSize(w, h)


def clamp_position(x: int, y: int, screen_rect):
    x = max(screen_rect.left(), min(screen_rect.right(), int(x)))
    y = max(screen_rect.top(), min(screen_rect.bottom(), int(y)))
    return x, y


def _safe_object_name(value: str) -> str:  # noqa: F811 - intentional public name
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip())
    return value.strip("_") or "widget"


def _child_index(widget: QWidget) -> int:
    parent = widget.parentWidget()
    if parent is None:
        return 0
    siblings = [w for w in parent.findChildren(
        QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly) if type(w) is type(widget)]
    try:
        return siblings.index(widget) + 1
    except ValueError:
        return 1


def widget_layout_id(widget: QWidget, root_name: str | None = None) -> str:
    """Return a stable-ish hierarchy identifier suitable for persistence."""
    try:
        custom = widget.property("_jarvis_layout_id")
    except RuntimeError:
        return ""
    if custom:
        return str(custom)
    parts: list[str] = []
    cur = widget
    while isinstance(cur, QWidget):
        try:
            obj = cur.objectName().strip()
        except RuntimeError:
            break
        piece = _safe_object_name(obj) if obj else f"{class_simple_name(cur)}#{_child_index(cur)}"
        parts.append(piece)
        cur = cur.parentWidget()
    parts.reverse()
    if root_name:
        return f"{_safe_object_name(root_name)}/" + "/".join(parts[-6:])
    return "/".join(parts[-8:])


def _root_candidates(main: QWidget) -> list[tuple[str, QWidget]]:
    """Named real panels/sections exposed by the current JARVIS architecture."""
    out: list[tuple[str, QWidget]] = []

    def add(name: str, obj: Any) -> None:
        if isinstance(obj, QWidget) and obj not in {w for _, w in out}:
            out.append((name, obj))

    add("Application", main)
    add("Main Surface", getattr(main, "surface", None))
    add("Top Bar", getattr(main, "_topbar", None))
    add("Status Bar", getattr(main, "_statusbar", None))
    add("Quick Panel", getattr(main, "quick_panel", None))
    add("Main Content Row", getattr(main, "_main_row", None))
    add("Chat Panel", getattr(main, "_command_window", None))
    add("Settings Panel", getattr(main, "_full_settings_window", None))
    add("Command Deck", getattr(main, "_quick_popup", None))

    system = getattr(main, "_system_layout_widgets", {}) or {}
    labels = {
        "Video Preview": "Video Preview",
        "Image Preview": "Image Preview",
        "Image View": "Image View",
        "Activity Log": "Activity Log",
        "Content Surface": "Content Surface",
        "3D Display": "3D Display",
        "Webview": "Webview",
        "Web Task": "Web Task",
        "World Monitor": "World Monitor",
        "Mind": "Mind",
        "Tools Deck": "Tools Deck",
    }
    for key, obj in system.items():
        add(labels.get(key, str(key)), obj)

    # Named floating panels (so the Studio can style the new Image View too).
    for name, attr in (
        ("Image View", "_image_view_panel"),
        ("Image Preview", "_image_panel"),
        ("Video Preview", "_video_panel"),
        ("Content Surface", "_content_panel"),
        ("Activity Log", "_activity_panel"),
        ("Webview", "_webview_panel"),
        ("Web Task", "_web_task_panel"),
        ("World Monitor", "_world_monitor_panel"),
        ("Mind", "_memory_panel"),
        ("Tools Deck", "_tools_panel"),
    ):
        add(name, getattr(main, attr, None))

    # Chat sections inside the live command window.
    cmd = getattr(main, "_command_window", None)
    if isinstance(cmd, QWidget):
        for name, attr in (
            ("Chat Header", "_online"),
            ("Chat Log", "chat"),
            ("Chat Activity", "_chat_activity"),
            ("Chat Metrics", "chips"),
            ("Message Input", "input"),
            ("Microphone Button", "mic_btn"),
            ("Import Button", "import_btn"),
            ("Webcam Button", "_webcam_btn"),
        ):
            add(name, getattr(cmd, attr, None))

    # Important controls in the compact/root UI.
    for name, attr in (
        ("Arc Reactor", "_header_logo"),
        ("Startup Hitbox", "_startup_logo_hitbox"),
        ("Quick Input", "_quick_input"),
        ("Panel Command Input", "_panel_command_input"),
    ):
        add(name, getattr(main, attr, None))

    return out


def build_widget_registry(main: QWidget) -> dict[str, QWidget]:
    """Build the Explorer's flat lookup plus explicit panel names."""
    registry: dict[str, QWidget] = {}
    seen: set[int] = set()
    for root_name, root in _root_candidates(main):
        if id(root) not in seen:
            registry[root_name] = root
            seen.add(id(root))
        for child in root.findChildren(QWidget):
            if id(child) in seen:
                continue
            child_name = child.objectName().strip()
            label = child_name or f"{class_simple_name(child)} · {widget_layout_id(child, root_name)}"
            unique = f"{root_name} / {label}"
            suffix = 2
            base = unique
            while unique in registry:
                unique = f"{base} ({suffix})"
                suffix += 1
            registry[unique] = child
            seen.add(id(child))
    return registry


def find_registered_widget(main: QWidget, name: str) -> QWidget | None:
    if not name:
        return None
    registry = build_widget_registry(main)
    return registry.get(name) or next(
        (w for label, w in registry.items() if label.endswith(f"/ {name}") or label == name), None)


def _widget_text(widget: QWidget) -> str:
    for method_name in ("text", "toPlainText", "placeholderText"):
        method = getattr(widget, method_name, None)
        if callable(method):
            try:
                value = method()
                if value is not None:
                    return str(value)
            except Exception:
                pass
    return ""


# ---------------------------------------------------------------------------
# Runtime widget state capture / apply
# ---------------------------------------------------------------------------
def _figma_has_custom_paint(widget: QWidget) -> bool:
    """Return True when a project widget overrides QWidget.paintEvent.

    Native Qt controls can also override paintEvent, so only project-defined
    classes are treated as exact-capture candidates when possible.
    """
    try:
        cls = type(widget)
        paint = getattr(cls, "paintEvent", None)
        base = getattr(QWidget, "paintEvent", None)
        if paint is None or paint is base:
            return False
        module = str(getattr(cls, "__module__", ""))
        return not module.startswith("PyQt6.")
    except Exception:
        return False


def _figma_svg_capture(widget: QWidget, include_children: bool = False) -> str | None:
    """Capture a live Qt widget as a *fragment* of vector SVG.

    Two rules make this usable in a single exported document:

    * the result is a fragment, never a nested ``<svg>``. ``QSvgGenerator`` emits
      a complete XML document (declaration + ``<svg width="…mm">`` + ``</svg>``),
      and the old offset-based slicing kept the opening tag while discarding the
      closing one, which made the whole export malformed and caused Figma/Penpot
      to stop reading part-way through the file. Normalisation is delegated to
      :func:`figma_export.normalize_runtime_svg_fragment` so the rule is enforced
      in one place and is unit-testable without Qt.
    * a capture that painted nothing returns ``None`` instead of an empty group,
      which lets the caller fall back to an embedded bitmap rather than export a
      blank layer (how pixmap logos used to disappear).

    ``include_children`` is off by default so child widgets can be exported as
    their own editable layers instead of being flattened twice.
    """
    if not _FIGMA_QTSVG_AVAILABLE or QSvgGenerator is None or not widget_alive(widget):
        return None
    buffer = None
    try:
        w, h = max(1, widget.width()), max(1, widget.height())
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.ReadWrite)
        generator = QSvgGenerator()
        generator.setOutputDevice(buffer)
        generator.setSize(QSize(w, h))
        generator.setViewBox(QRect(0, 0, w, h))
        generator.setTitle(widget.objectName() or class_simple_name(widget))
        painter = QPainter(generator)
        flags = QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.IgnoreMask
        if include_children:
            flags |= QWidget.RenderFlag.DrawChildren
        widget.render(painter, QPoint(0, 0), QRegion(), flags)
        painter.end()
        data = bytes(buffer.data())
        buffer.close()
        if not data:
            return None
        fragment = normalize_runtime_svg_fragment(data.decode("utf-8", errors="replace"))
        if not runtime_fragment_has_content(fragment):
            return None

        # Every QSvgGenerator instance can start reusable IDs from the same
        # base (clip paths, masks, gradients, filters). SVG IDs are document
        # global, so namespace this widget's definitions before the fragment
        # joins the single exported document.
        prefix = f"jarvis_{abs(id(widget)):x}_"
        for original in sorted(set(re.findall(r'\bid=["\']([^"\']+)["\']', fragment))):
            replacement = prefix + original
            fragment = re.sub(
                rf'(?P<q>["\'])#{re.escape(original)}(?P=q)',
                lambda m: f'{m.group("q")}#{replacement}{m.group("q")}',
                fragment,
            )
            fragment = re.sub(
                rf'(?P<prefix>url\(\s*)#{re.escape(original)}(?P<suffix>\s*\))',
                lambda m: f'{m.group("prefix")}#{replacement}{m.group("suffix")}',
                fragment,
            )
            fragment = re.sub(
                rf'(\bid=)(["\']){re.escape(original)}(["\'])',
                rf'\1\2{replacement}\3',
                fragment,
            )
        return fragment
    except Exception:
        try:
            if buffer is not None:
                buffer.close()
        except Exception:
            pass
        return None


def _widget_has_alpha_content(pixmap: QPixmap | None) -> bool:
    """Cheap "did this grab actually contain pixels" test (never trusts a grab)."""
    if pixmap is None or pixmap.isNull():
        return False
    try:
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        stride, height = image.bytesPerLine(), image.height()
        if stride <= 0 or height <= 0:
            return False
        data = image.constBits()
        data.setsize(stride * height)
        view = memoryview(data).cast("B").cast("i", [height, stride // 4])
        # Sample a sparse grid: any non-transparent sample means the widget drew.
        step_x = max(1, (stride // 4) // 16)
        step_y = max(1, height // 16)
        for y in range(0, height, step_y):
            row = view[y]
            for x in range(0, len(row), step_x):
                if (row[x] >> 24) & 0xFF:
                    return True
        return False
    except Exception:
        return True   # cannot prove it empty -> keep the capture rather than drop it


def _figma_pixmap_payload(pixmap: QPixmap | None, width: int, height: int) -> dict | None:
    """Encode an actual runtime pixmap for an editable SVG <image> layer."""
    if pixmap is None or pixmap.isNull():
        return None
    try:
        png = QBuffer()
        png.open(QIODevice.OpenModeFlag.ReadWrite)
        if not pixmap.toImage().save(png, "PNG"):
            png.close()
            return None
        payload = bytes(png.data())
        png.close()
        if not payload:
            return None
        import base64
        return {
            "data": base64.b64encode(payload).decode("ascii"),
            "mime": "image/png",
            "opacity": 100,
            "preserve": "xMidYMid meet",
            "width": int(width),
            "height": int(height),
        }
    except Exception:
        return None


def _figma_runtime_visual(widget: QWidget,
                          include_children: bool = True) -> tuple[str | None, dict | None, str]:
    """Choose the highest-fidelity representation supported by this widget.

    ``include_children`` tells the fallback what ``widget.grab()`` would also
    flatten: when the widget's own descendants are exported as separate editable
    layers, a raster fallback of the whole subtree would duplicate them, so the
    caller passes ``False`` and an unpaintable container degrades to its
    semantic shape instead.
    """
    try:
        cls = class_simple_name(widget)
    except Exception:
        cls = type(widget).__name__

    # Browser/video/compositor surfaces cannot be represented as semantic Qt
    # shapes; capture the actual visible frame so they are no longer missing.
    if cls in {"QWebEngineView", "QVideoWidget"} or bool(widget.property("_jarvis_web_surface")):
        try:
            pixmap = widget.grab()
            return None, _figma_pixmap_payload(pixmap, widget.width(), widget.height()), "image"
        except Exception:
            return None, None, "image"

    # Everything else is rendered by Qt itself into vector SVG. This is the
    # important fidelity rule: QLabel, QPushButton, custom QPainter widgets,
    # QTextBrowser/QTextEdit, styled frames, sliders, checkboxes, icons, and
    # project-specific paint code all follow the exact runtime painting path.
    svg = _figma_svg_capture(widget, include_children=include_children)
    if svg:
        return svg, None, "svg"

    # The vector path produced nothing. Widgets that really did paint (a pixmap
    # logo, an icon, a QOpenGL/native surface, or anything QtSvg cannot express)
    # are embedded as their actual pixels instead of exporting a blank layer —
    # this is the "logos are missing" half of the bug. A grab that is fully
    # transparent is *not* embedded: that would re-create the blank layer.
    if include_children:
        try:
            grabbed = widget.grab()
        except Exception:
            grabbed = None
        if _widget_has_alpha_content(grabbed):
            payload = _figma_pixmap_payload(grabbed, widget.width(), widget.height())
            if payload:
                return None, payload, "image"

    # If QtSvg is unavailable, keep the older semantic/image fallback rather
    # than making Download as Figma fail.
    if isinstance(widget, QLabel):
        try:
            pixmap = widget.pixmap()
        except Exception:
            pixmap = None
        payload = _figma_pixmap_payload(pixmap, widget.width(), widget.height())
        if payload:
            return None, payload, "image"
    return None, None, "semantic"

def capture_widget_state(widget: QWidget, root_name: str = "") -> dict:
    """Capture only stable, editable properties; runtime-only state stays out."""
    try:
        geo = widget.geometry()
        font = widget.font()
    except RuntimeError:
        return {}
    opacity = 100
    glow: dict = {"enabled": False}
    shadow: dict = {"enabled": False}
    effect = widget.graphicsEffect()
    try:
        if effect is not None and hasattr(effect, "opacity") and type(effect).__name__ == "QGraphicsOpacityEffect":
            opacity = int(round(effect.opacity() * 100))
        elif effect is not None and hasattr(effect, "blurRadius"):
            colour = effect.color() if hasattr(effect, "color") else QColor("#000000")
            radius = int(round(effect.blurRadius()))
            offset = effect.offset() if hasattr(effect, "offset") else None
            if offset is not None and (offset.x() or offset.y()):
                shadow = {"enabled": True, "color": colour.name(),
                          "opacity": colour.alpha() * 100 // 255,
                          "blur": radius, "dx": int(offset.x()), "dy": int(offset.y())}
            else:
                glow = {"enabled": True, "color": colour.name(),
                        "radius": radius, "opacity": colour.alpha() * 100 // 255,
                        "strength": 60, "spread": 0}
    except Exception:
        pass
    return {
        "id": widget_layout_id(widget, root_name),
        "object_name": widget.objectName(),
        "x": geo.x(),
        "y": geo.y(),
        "w": geo.width(),
        "h": geo.height(),
        "min_w": widget.minimumWidth(),
        "min_h": widget.minimumHeight(),
        "max_w": widget.maximumWidth(),
        "max_h": widget.maximumHeight(),
        "visible": widget.isVisible(),
        "enabled": widget.isEnabled(),
        "mouse_tracking": widget.hasMouseTracking(),
        "auto_fill_background": widget.autoFillBackground(),
        "opacity": opacity,
        "font_family": font.family(),
        "font_size": font.pointSizeF() if font.pointSizeF() > 0 else float(font.pixelSize() or 9),
        "font_bold": font.bold(),
        "font_italic": font.italic(),
        "text": _widget_text(widget),
        "tooltip": widget.toolTip(),
        "stylesheet": widget.styleSheet(),
        "glow": glow,
        "shadow": shadow,
    }


def apply_widget_state(widget: QWidget, state: dict, live: bool = True) -> None:
    """Apply a captured runtime-widget state (geometry, style, effect)."""
    if not isinstance(state, dict) or not widget_alive(widget):
        return
    try:
        _core_apply_widget_state(widget, state, live=live)
    except Exception:
        return
    # The engine's applier does not know about the captured effect model; apply
    # it here so a runtime widget's glow/shadow survives a restart.
    try:
        model = {"glow": state.get("glow") or {}, "shadow": state.get("shadow") or {}}
        if as_bool(get_path(model, "glow.enabled", False)) or as_bool(get_path(model, "shadow.enabled", False)):
            apply_effect_model(widget, model)
        widget.setProperty("_jarvis_layout_touched", True)
    except Exception:
        pass


def capture_widget_overrides(main: QWidget) -> dict:
    """Capture explicitly edited named roots and their descendants."""
    overrides: dict[str, dict] = {}
    for root_name, root in _root_candidates(main):
        if not isinstance(root, QWidget):
            continue
        try:
            if root.property("_jarvis_layout_touched"):
                overrides[widget_layout_id(root, root_name)] = capture_widget_state(root, root_name)
            for child in root.findChildren(QWidget):
                if child.property("_jarvis_layout_custom"):
                    continue
                if child.property("_jarvis_layout_touched"):
                    overrides[widget_layout_id(child, root_name)] = capture_widget_state(child, root_name)
        except RuntimeError:
            continue
    return overrides


def apply_widget_overrides(main: QWidget, overrides: dict) -> int:
    """Re-apply captured runtime-widget states; returns how many took effect.

    Every override is addressed by its :func:`widget_layout_id`, resolved
    against the live tree with the *same* enumeration order ``capture_widget_
    overrides`` used, so a saved edit can only ever land on the widget it was
    captured from.  A widget that no longer exists is skipped — guessing a
    replacement would smear one widget's style onto an unrelated one.
    """
    if not isinstance(overrides, dict) or not isinstance(main, QWidget):
        return 0
    by_id: dict[str, QWidget] = {}
    for root_name, root in _root_candidates(main):
        if not isinstance(root, QWidget):
            continue
        candidates = [root, *root.findChildren(QWidget)]
        for widget in candidates:
            # Custom elements own their state through their spec, exactly as in
            # ``capture_widget_overrides``, so they are not override targets.
            if widget is not root and widget.property("_jarvis_layout_custom"):
                continue
            try:
                key = widget_layout_id(widget, root_name)
            except RuntimeError:
                continue
            if key:
                by_id[key] = widget
    applied = 0
    for key, state in overrides.items():
        if not isinstance(state, dict):
            continue
        target = by_id.get(str(key)) or by_id.get(str(state.get("id") or ""))
        if target is None:
            continue
        try:
            apply_widget_state(target, state, live=True)
            applied += 1
        except Exception:
            continue
    return applied


def default_layout_path(main: QWidget | None = None) -> Path:
    """Where the layout file lives (matches MainWindow's ``CONFIG_DIR``)."""
    for attr in ("CONFIG_DIR", "_CONFIG_DIR"):
        candidate = getattr(main, attr, None)
        if candidate:
            return Path(candidate) / "ui_layout.json"
    try:
        import ui as _ui  # local import: only used to locate the config folder
        config_dir = getattr(_ui, "CONFIG_DIR", None)
        if config_dir:
            return Path(config_dir) / "ui_layout.json"
    except Exception:
        pass
    return Path("config") / "ui_layout.json"


# ---------------------------------------------------------------------------
# The Studio
# ---------------------------------------------------------------------------
class LayoutEditorDialog(QMainWindow):
    """Visual editor for the real J.A.R.V.I.S. widget tree."""

    def __init__(self, main_window=None):
        super().__init__(main_window)
        self.main = main_window
        self.setWindowTitle("J.A.R.V.I.S. Layout Studio")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(1560, 940)
        self.setMinimumSize(1180, 720)
        self.setStyleSheet(_composed_app_css() + f"""
            QMainWindow{{background:{BG};}}
            QSplitter::handle{{background:{BORDER_FAINT};}}
            QToolButton,QPushButton{{min-height:28px;}}
            QTreeWidget{{background:{SURFACE_ALT};border:1px solid {BORDER_FAINT};
                border-radius:{RADIUS_MD}px;color:{TEXT};padding:3px;}}
            QTreeWidget::item{{padding:5px 4px;border-radius:4px;}}
            QTreeWidget::item:selected{{background:rgba(0,212,255,75);color:{TEXT_BRIGHT};}}
            QComboBox,QLineEdit{{background:{BG};color:{TEXT_BRIGHT};
                border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;padding:5px 7px;}}
            QLabel#StudioCaption{{color:{TEXT_DIM};font:700 7pt {FONT_SMALL};
                letter-spacing:1.2px;background:transparent;}}
            QTabBar::tab{{background:{SURFACE_ALT};color:{TEXT_DIM};
                border:1px solid {BORDER_FAINT};border-radius:6px;padding:6px 10px;
                margin:2px 3px 2px 0;font:700 7.5pt {FONT_SMALL};letter-spacing:0.8px;}}
            QTabBar::tab:selected{{color:{TEXT_BRIGHT};border-color:{ACCENT};
                background:rgba(0,82,110,150);}}
        """)

        self._current_target_name = ""
        self._target_map: list[tuple[str, QWidget]] = []
        self._tree_items: dict[int, QTreeWidgetItem] = {}
        self._live = True
        self._save_state = "SAVED"
        self._runtime_specs: dict[int, dict] = {}
        self._runtime_widgets: dict[int, QWidget] = {}
        self._asset_selection = ""
        self._tool = "select"
        self._loading_code = True
        self._drag_origins: dict[int, dict] = {}

        root_dir = Path(self.main.CONFIG_DIR) if getattr(self.main, "CONFIG_DIR", None) else Path.cwd()
        self.layout_path = default_layout_path(self.main)
        self.assets = AssetStore(root_dir)

        self.animations = AnimationManager(log=self.log)
        self.scripts = ScriptManager(log=self.log)
        self.clipboard = ClipboardManager(self)
        self.selection = SelectionManager(log=self.log)
        self.history = HistoryManager(apply_fn=self._apply_history_state, log=self.log, limit=400)
        self.history.capture_widget_state = lambda widget: capture_widget_state(
            widget, self._current_target_name or "")

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._flush_save)

        self._build_ui()
        self._load_assets()
        self._refresh_targets()
        self.log("[LAYOUT] Studio ready · schema v"
                 f"{SCHEMA_VERSION} · {len(self.assets.entries)} asset(s)")
        supported, reason = effects_supported()
        if not supported:
            self.log(f"[WARN] pixel effects limited — {reason}")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self._build_toolbar()
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)

        header = QHBoxLayout()
        title = QLabel("LAYOUT STUDIO")
        title.setStyleSheet(
            f"color:{ACCENT_BRIGHT};font:900 11pt {FONT_HEAD};background:transparent;"
            f"letter-spacing:2px;")
        header.addWidget(title)
        self.selection_label = QLabel("No selection")
        self.selection_label.setObjectName("StudioCaption")
        header.addStretch(1)
        header.addWidget(self.selection_label)
        header.addWidget(QLabel("·"))
        self.save_state_label = QLabel("SAVED")
        self.save_state_label.setObjectName("StudioCaption")
        header.addWidget(self.save_state_label)
        root.addLayout(header)

        # Dedicated panel selector bar: always visible at the top of the Studio.
        # It is intentionally outside the crowded editing toolbar so the user can
        # switch between any discovered J.A.R.V.I.S. panel without hunting for it.
        target_frame = QFrame()
        target_frame.setObjectName("StudioTargetBar")
        target_frame.setStyleSheet(
            f"QFrame#StudioTargetBar{{background:{SURFACE};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_MD}px;padding:2px;}}")
        target_bar = QHBoxLayout(target_frame)
        target_bar.setContentsMargins(10, 6, 10, 6)
        target_bar.setSpacing(7)

        target_caption = QLabel("TARGET PANEL")
        target_caption.setStyleSheet(
            f"color:{ACCENT};font:900 8pt {FONT_HEAD};background:transparent;"
            f"letter-spacing:1.4px;")
        target_bar.addWidget(target_caption)

        self.panel_select_button = QToolButton()
        self.panel_select_button.setText("SELECT PANEL ▾")
        self.panel_select_button.setToolTip("Select which real J.A.R.V.I.S. panel the Layout Studio edits")
        self.panel_select_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.panel_select_button.setMinimumWidth(300)
        self.panel_select_button.setStyleSheet(
            f"QToolButton{{background:rgba(0,82,110,100);color:{TEXT_BRIGHT};"
            f"border:1px solid {BORDER_SOFT};border-radius:{RADIUS_SM}px;"
            f"padding:7px 10px;font:800 8pt {FONT_SMALL};letter-spacing:0.6px;text-align:left;}}"
            f"QToolButton:hover{{background:rgba(0,120,160,150);border-color:{ACCENT};}}"
            f"QToolButton::menu-indicator{{subcontrol-position:right center;right:8px;}}")
        self.panel_select_menu = QMenu(self.panel_select_button)
        self.panel_select_button.setMenu(self.panel_select_menu)
        target_bar.addWidget(self.panel_select_button)

        self.panel_refresh_button = self._tool_button(
            "↻", "Refresh the available J.A.R.V.I.S. panels", self._refresh_targets)
        self.panel_refresh_button.setFixedSize(34, 30)
        target_bar.addWidget(self.panel_refresh_button)

        # "Download as Figma" lives right next to the Target Panel control so
        # exporting the panel currently being edited never requires hunting
        # through a separate menu.
        self.figma_export_button = QToolButton()
        self.figma_export_button.setText("Download as Figma")
        self.figma_export_button.setToolTip(
            "Export the selected panel as an editable, Figma-importable SVG")
        self.figma_export_button.setStyleSheet(
            f"QToolButton{{background:rgba(0,82,110,100);color:{TEXT_BRIGHT};"
            f"border:1px solid {BORDER_SOFT};border-radius:{RADIUS_SM}px;"
            f"padding:7px 10px;font:800 8pt {FONT_SMALL};letter-spacing:0.6px;}}"
            f"QToolButton:hover{{background:rgba(0,120,160,150);border-color:{ACCENT};}}"
            f"QToolButton:disabled{{color:{TEXT_DIM};background:rgba(255,255,255,10);"
            f"border-color:{BORDER_FAINT};}}")
        self.figma_export_button.setEnabled(False)
        self.figma_export_button.clicked.connect(self._export_target_as_figma)
        target_bar.addWidget(self.figma_export_button)

        self.panel_target_info = QLabel("No panel selected")
        self.panel_target_info.setStyleSheet(
            f"color:{TEXT_DIM};font:700 7.5pt {FONT_SMALL};background:transparent;")
        target_bar.addWidget(self.panel_target_info, 1)
        root.addWidget(target_frame)

        # Internal compatibility selector. The visible control above drives it;
        # keeping the combo preserves the existing target-selection API.
        self.panel_picker = QComboBox()
        self.panel_picker.setVisible(False)
        self.panel_picker.currentIndexChanged.connect(self._set_target_by_index)

        self.panes = QSplitter(Qt.Orientation.Horizontal)
        self.panes.setChildrenCollapsible(False)
        self.panes.addWidget(self._build_explorer_pane())
        self.panes.addWidget(self._build_viewport_pane())
        self.panes.addWidget(self._build_right_pane())
        self.panes.setStretchFactor(0, 1)
        self.panes.setStretchFactor(1, 3)
        self.panes.setStretchFactor(2, 2)

        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.vertical.setChildrenCollapsible(False)
        self.vertical.addWidget(self.panes)
        output_frame = QFrame()
        output_frame.setStyleSheet(
            f"QFrame{{background:{SURFACE};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_LG}px;}}")
        output_layout = QVBoxLayout(output_frame)
        output_layout.setContentsMargins(8, 6, 8, 8)
        output_layout.setSpacing(5)
        row = QHBoxLayout()
        row.addWidget(self._caption("OUTPUT"))
        row.addStretch(1)
        clear_button = QPushButton("CLEAR")
        clear_button.setFixedWidth(70)
        clear_button.setStyleSheet(self._mini_style())
        row.addWidget(clear_button)
        output_layout.addLayout(row)
        self.output = OutputPanel()
        clear_button.clicked.connect(self.output.clear_all)
        output_layout.addWidget(self.output, 1)
        output_frame.setMaximumHeight(220)
        self.vertical.addWidget(output_frame)
        self.vertical.setStretchFactor(0, 6)
        self.vertical.setStretchFactor(1, 1)
        root.addWidget(self.vertical, 1)

        self.setCentralWidget(central)
        self._install_shortcuts()
        self.statusBar().showMessage("READY · select a panel to begin")

    def _caption(self, text: str) -> QLabel:
        label = QLabel(str(text))
        label.setStyleSheet(
            f"color:{ACCENT};font:800 8pt {FONT_HEAD};background:transparent;"
            f"letter-spacing:1.5px;")
        return label

    def _mini_style(self) -> str:
        return (f"QPushButton{{background:rgba(6,26,38,170);color:{TEXT_MID};"
                f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;"
                f"padding:5px 8px;font:700 7.5pt {FONT_SMALL};letter-spacing:0.6px;}}"
                f"QPushButton:hover{{color:{TEXT_BRIGHT};border-color:{ACCENT};"
                f"background:rgba(0,82,110,150);}}"
                f"QPushButton:checked{{color:{TEXT_BRIGHT};border-color:{ACCENT_BRIGHT};"
                f"background:rgba(0,120,160,170);}}")

    def _tool_button(self, text: str, tip: str, slot, checkable: bool = False) -> QToolButton:
        button = QToolButton()
        button.setText(str(text))
        button.setToolTip(str(tip))
        button.setStyleSheet(self._mini_style())
        button.setCheckable(bool(checkable))
        button.clicked.connect(slot)
        return button

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Studio")
        toolbar.setMovable(False)
        toolbar.setStyleSheet(
            f"QToolBar{{background:{SURFACE};border:none;"
            f"border-bottom:1px solid {BORDER_FAINT};spacing:4px;padding:5px;}}")
        self.toolbar = toolbar

        self.undo_btn = self._tool_button("↶", "Undo (Ctrl+Z)", self.undo)
        self.redo_btn = self._tool_button("↷", "Redo (Ctrl+Shift+Z)", self.redo)
        toolbar.addWidget(self.undo_btn)
        toolbar.addWidget(self.redo_btn)
        toolbar.addSeparator()

        # Tool modes (TXT section 54) -- the active tool is visibly indicated.
        self.tool_buttons: dict[str, QToolButton] = {}
        for name, tip in (("select", "Select and move elements (V)"),
                          ("move", "Move elements (M)"),
                          ("resize", "Resize elements (R)"),
                          ("text", "Add a text element (T)"),
                          ("image", "Import an image element (I)"),
                          ("frame", "Add a frame (F)"),
                          ("panel", "Add a panel (P)")):
            button = self._tool_button(name.upper(), tip,
                                       lambda _=False, n=name: self._set_tool(n), checkable=True)
            self.tool_buttons[name] = button
            toolbar.addWidget(button)
        self.tool_buttons["select"].setChecked(True)
        toolbar.addSeparator()

        add_button = QToolButton()
        add_button.setText("ADD")
        add_button.setToolTip("Create an element (TXT: ADD → every element type)")
        add_button.setStyleSheet(self._mini_style())
        add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        add_menu = QMenu(add_button)
        for kind in ELEMENT_TYPES:
            action = add_menu.addAction(kind)
            action.triggered.connect(lambda _=False, k=kind: self.add_element(k))
        add_menu.addSeparator()
        import_action = add_menu.addAction("Import Image…")
        import_action.triggered.connect(self.import_image_as_element)
        uiverse_action = add_menu.addAction("Uiverse Element…")
        uiverse_action.triggered.connect(self.open_uiverse_import)
        add_button.setMenu(add_menu)
        toolbar.addWidget(add_button)

        toolbar.addWidget(self._tool_button("IMPORT IMAGE", "Import an image as an element",
                                            self.import_image_as_element))
        toolbar.addWidget(self._tool_button("ASSETS", "Asset browser (import / replace / rename)",
                                            lambda: self._show_tab(3)))
        toolbar.addWidget(self._tool_button("DELETE", "Delete the selected element (Delete)",
                                            self.delete_selected_element))
        toolbar.addSeparator()

        for text, tip, builder in (
            ("ALIGN", "Align the selection", self._align_menu),
            ("DISTRIBUTE", "Distribute the selection", self._distribute_menu),
            ("ORDER", "Z-order", self._order_menu),
            ("COPY", "Copy (Ctrl+C)", self.copy_selection),
            ("PASTE", "Paste (Ctrl+V)", self.paste_clipboard),
            ("CUT", "Cut (Ctrl+X)", self.cut_selection),
            ("DUPLICATE", "Duplicate (Ctrl+D)", self.duplicate_selection),
        ):
            if builder in (self.copy_selection, self.paste_clipboard, self.cut_selection,
                           self.duplicate_selection):
                toolbar.addWidget(self._tool_button(text, tip, builder))
            else:
                button = QToolButton()
                button.setText(text)
                button.setToolTip(tip)
                button.setStyleSheet(self._mini_style())
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                button.setMenu(builder(button))
                toolbar.addWidget(button)
        toolbar.addSeparator()

        toolbar.addWidget(self._tool_button("SET AS BACKGROUND", "Paint the selected image as its parent's background",
                                            lambda: self.do_action("set_background", True)))
        toolbar.addWidget(self._tool_button("REPLACE IMAGE", "Swap the image asset, keeping everything else",
                                            lambda: self.do_action("replace_image", None)))
        toolbar.addWidget(self._tool_button("VIEW CODE", "Open the element's code (Ctrl+K)",
                                            self._show_code_tab))
        toolbar.addWidget(self._tool_button("LOCK", "Lock the selection", lambda: self.do_action("lock", None)))
        toolbar.addWidget(self._tool_button("UNLOCK", "Unlock the selection", lambda: self.do_action("unlock", None)))
        toolbar.addWidget(self._tool_button("HIDE", "Hide the selection", lambda: self.do_action("hide", None)))
        toolbar.addWidget(self._tool_button("SHOW", "Show the selection", lambda: self.do_action("show", None)))
        toolbar.addSeparator()

        self.live_toggle = QCheckBox("LIVE")
        self.live_toggle.setChecked(True)
        self.live_toggle.setToolTip("Apply edits to the running UI and autosave")
        self.live_toggle.toggled.connect(lambda value: setattr(self, "_live", bool(value)))
        toolbar.addWidget(self.live_toggle)

        self.grid_toggle = QCheckBox("GRID")
        self.grid_toggle.setChecked(True)
        self.grid_toggle.toggled.connect(lambda value: self._set_grid(grid=bool(value)))
        toolbar.addWidget(self.grid_toggle)

        self.grid_size = QComboBox()
        self.grid_size.addItems(["1", "2", "4", "8", "16", "32", "64"])
        self.grid_size.setCurrentText("8")
        self.grid_size.setFixedWidth(58)
        self.grid_size.setToolTip("Grid / snap size in pixels")
        self.grid_size.setStyleSheet(self._combo_style())
        self.grid_size.currentTextChanged.connect(lambda _=None: self._set_grid())
        toolbar.addWidget(self.grid_size)

        self.snap_toggle = QCheckBox("SNAP")
        self.snap_toggle.setChecked(True)
        self.snap_toggle.toggled.connect(self._set_snap)
        toolbar.addWidget(self.snap_toggle)

        self.snap_mode = QComboBox()
        self.snap_mode.addItems(["grid", "element", "pixel"])
        self.snap_mode.setFixedWidth(92)
        self.snap_mode.setToolTip("Snap to grid, to element edges/centres, or to the pixel")
        self.snap_mode.setStyleSheet(self._combo_style())
        self.snap_mode.currentTextChanged.connect(self._set_snap_mode)
        toolbar.addWidget(self.snap_mode)
        toolbar.addSeparator()

        toolbar.addWidget(self._tool_button("−", "Zoom out", self.zoom_out))
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(10, 400)
        self.zoom.setValue(100)
        self.zoom.setFixedWidth(120)
        self.zoom.setToolTip("Viewport zoom")
        self.zoom.valueChanged.connect(lambda value: self.set_zoom(value / 100.0))
        toolbar.addWidget(self.zoom)
        toolbar.addWidget(self._tool_button("+", "Zoom in", self.zoom_in))
        toolbar.addWidget(self._tool_button("FIT", "Fit to screen (Home)", self.zoom_fit))
        toolbar.addWidget(self._tool_button("FOCUS", "Focus selection (F)", self.zoom_focus))
        toolbar.addWidget(self._tool_button("CENTER", "Centre view", self.zoom_center))
        toolbar.addWidget(self._tool_button("RESET", "Reset view", self.zoom_reset))
        toolbar.addSeparator()
        toolbar.addWidget(self._tool_button("SAVE", "Save the layout (Ctrl+S)", self.save_all))
        self.addToolBar(toolbar)

    def _combo_style(self) -> str:
        return (f"QComboBox{{background:{BG};color:{TEXT_BRIGHT};"
                f"border:1px solid {BORDER_FAINT};border-radius:{RADIUS_SM}px;padding:4px 6px;"
                f"font:700 8pt {FONT_SMALL};}}"
                f"QComboBox QAbstractItemView{{background:{BG};color:{TEXT_BRIGHT};"
                f"selection-background-color:rgba(0,180,230,90);}}")

    def _menu_from(self, parent, entries) -> QMenu:
        menu = QMenu(parent)
        for label, slot in entries:
            if label == "-":
                menu.addSeparator()
                continue
            action = menu.addAction(label)
            action.triggered.connect(lambda _=False, s=slot: s())
        return menu

    def _align_menu(self, parent) -> QMenu:
        return self._menu_from(parent, [
            ("Align Left", lambda: self._align("left")),
            ("Align Horizontal Centre", lambda: self._align("hcenter")),
            ("Align Right", lambda: self._align("right")),
            ("Align Top", lambda: self._align("top")),
            ("Align Vertical Centre", lambda: self._align("vcenter")),
            ("Align Bottom", lambda: self._align("bottom")),
        ])

    def _distribute_menu(self, parent) -> QMenu:
        return self._menu_from(parent, [
            ("Distribute Horizontally", lambda: self._distribute("horizontal")),
            ("Distribute Vertically", lambda: self._distribute("vertical")),
        ])

    def _order_menu(self, parent) -> QMenu:
        return self._menu_from(parent, [
            ("Bring to Front", lambda: self.do_action("z_front", None)),
            ("Bring Forward", lambda: self.do_action("z_forward", None)),
            ("Send Backward", lambda: self.do_action("z_backward", None)),
            ("Send to Back", lambda: self.do_action("z_back", None)),
            ("-", None),
            ("Group", lambda: self.do_action("group", None)),
            ("Ungroup", lambda: self.do_action("ungroup", None)),
        ])

    def _build_explorer_pane(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{SURFACE};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_LG}px;}}")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._caption("EXPLORER"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter elements…")
        self.search.setStyleSheet(self._combo_style())
        self.search.textChanged.connect(self._filter_tree)
        layout.addWidget(self.search)
        self.explorer = ExplorerTree()
        self.explorer.widgetSelected.connect(lambda w: self.select_widget(w))
        self.explorer.scriptSelected.connect(self._open_script_for)
        self.explorer.contextRequested.connect(self._explorer_context)
        self.explorer.reparentRequested.connect(self._reparent)
        layout.addWidget(self.explorer, 1)
        hint = QLabel("Drag a row onto a container to re-parent. Right-click for element actions.")
        hint.setWordWrap(True)
        hint.setObjectName("StudioCaption")
        layout.addWidget(hint)
        return frame

    def _build_viewport_pane(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{BG_DEEP};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_LG}px;}}")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.addWidget(self._caption("VIEWPORT"))
        self.viewport_info = QLabel("no target")
        self.viewport_info.setObjectName("StudioCaption")
        row.addStretch(1)
        row.addWidget(self.viewport_info)
        layout.addLayout(row)

        self.viewport_scroll = QScrollArea()
        self.viewport_scroll.setWidgetResizable(False)
        self.viewport_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.viewport_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.viewport = ViewportCanvas()
        self.viewport.widgetsPicked.connect(self._viewport_picked)
        self.viewport.geometryChanged.connect(self._viewport_geometry_changed)
        self.viewport.dragStarted.connect(self._viewport_drag_started)
        self.viewport.dragFinished.connect(self._viewport_drag_finished)
        self.viewport.marqueeFinished.connect(self._viewport_marquee)
        self.viewport.panRequested.connect(self._viewport_pan)
        self.viewport.zoomStepRequested.connect(self._viewport_zoom_step)
        self.viewport.contextRequested.connect(self._viewport_context)
        self.viewport.dropFilesRequested.connect(self._viewport_drop_files)
        self.viewport.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport_scroll.setWidget(self.viewport)
        layout.addWidget(self.viewport_scroll, 1)
        return frame

    def _build_right_pane(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame{{background:{SURFACE};border:1px solid {BORDER_FAINT};"
            f"border-radius:{RADIUS_LG}px;}}")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self.right_tabs = QTabWidget()
        self.right_tabs.setDocumentMode(True)

        self.properties = PropertyInspector(self)
        self.properties.propertyEdited.connect(self._inspector_changed)
        self.properties.actionRequested.connect(self._inspector_action)
        self.properties.presetApplied.connect(self._inspector_preset)
        self.properties.effectAdded.connect(self._add_effect)
        self.properties.effectRemoved.connect(self._remove_effect)
        self.properties.effectToggled.connect(self._toggle_effect)
        self.properties.effectMoved.connect(self._move_effect)
        self.right_tabs.addTab(self.properties, "PROPERTIES")

        # CODE tab: only relevant editors per element (TXT section 38).
        self.code_editors: dict[str, CodeEditor] = {}
        self.code_tabs = QTabWidget()
        self.code_tabs.setDocumentMode(True)
        for key, label in (("qss", "QSS"), ("python", "PYTHON"), ("script", "SCRIPT"),
                           ("html", "HTML"), ("css", "CSS"), ("json", "JSON / DATA")):
            editor = CodeEditor(language={"qss": "qss", "python": "python", "script": "python",
                                          "html": "html", "css": "css", "json": "json"}[key])
            editor.saveRequested.connect(lambda k=key: self._save_code_tab(k))
            editor.editor.textChanged.connect(lambda k=key: self._code_dirty(k))
            self.code_editors[key] = editor
        self.code_tabs.addTab(self.code_editors["qss"], "QSS")
        self.code_tabs.addTab(self.code_editors["python"], "PYTHON / BEHAVIOR")
        self.code_tabs.addTab(self.code_editors["script"], "SCRIPT")
        self.code_tabs.addTab(self.code_editors["html"], "HTML")
        self.code_tabs.addTab(self.code_editors["css"], "CSS")
        self.code_tabs.addTab(self.code_editors["json"], "JSON / DATA")
        self.code_tabs.currentChanged.connect(self._code_tab_changed)
        code_wrap = QWidget()
        code_layout = QVBoxLayout(code_wrap)
        code_layout.setContentsMargins(0, 0, 0, 0)
        code_layout.addWidget(self.code_tabs, 1)
        code_row = QHBoxLayout()
        self.code_target = QLabel("no element")
        self.code_target.setObjectName("StudioCaption")
        code_row.addWidget(self.code_target, 1)
        for text, slot, tip in (("APPLY CODE LIVE", self.apply_code, "Apply the active code tab"),
                                ("SAVE SCRIPT", lambda: self._save_code_tab("script"), "Validate and save the script"),
                                ("RUN / TEST", self._run_script, "Run the script against the element")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setStyleSheet(self._mini_style())
            button.clicked.connect(slot)
            code_row.addWidget(button)
        code_layout.addLayout(code_row)
        self.right_tabs.addTab(code_wrap, "CODE")

        self.animation_panel = AnimationPanel(self)
        self.right_tabs.addTab(self.animation_panel, "ANIMATION")

        self.asset_browser = AssetBrowser()
        self.asset_browser.importRequested.connect(self.import_assets)
        self.asset_browser.useRequested.connect(self._asset_command)
        self.asset_browser.replaceRequested.connect(self._replace_asset)
        self.asset_browser.deleteRequested.connect(self._delete_asset)
        self.asset_browser.previewRequested.connect(self._preview_asset)
        self.right_tabs.addTab(self.asset_browser, "ASSETS")

        self._code_dirty_flag = False
        layout.addWidget(self.right_tabs)
        return frame

    def _install_shortcuts(self) -> None:
        def bind(sequence: str, slot):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)
            return shortcut

        bind("Ctrl+S", self.save_all)
        bind("Ctrl+Z", self.undo)
        bind("Ctrl+Shift+Z", self.redo)
        bind("Ctrl+Y", self.redo)
        bind("Ctrl+C", self.copy_selection)
        bind("Ctrl+V", self.paste_clipboard)
        bind("Ctrl+X", self.cut_selection)
        bind("Ctrl+D", self.duplicate_selection)
        bind("Ctrl+A", self.select_all)
        bind("Ctrl+K", self._show_code_tab)
        bind("F2", lambda: self._focus_field("name"))
        bind("Delete", self._delete_shortcut)
        bind("Backspace", self._delete_shortcut)
        bind("F", self.zoom_focus)
        bind("Home", self.zoom_fit)
        bind("V", lambda: self._set_tool("select"))
        bind("M", lambda: self._set_tool("move"))
        bind("R", lambda: self._set_tool("resize"))
        bind("T", lambda: self._set_tool("text"))
        bind("I", lambda: self._set_tool("image"))
        bind("Left", lambda: self._nudge(-1, 0))
        bind("Right", lambda: self._nudge(1, 0))
        bind("Up", lambda: self._nudge(0, -1))
        bind("Down", lambda: self._nudge(0, 1))
        bind("Shift+Left", lambda: self._nudge(-10, 0))
        bind("Shift+Right", lambda: self._nudge(10, 0))
        bind("Shift+Up", lambda: self._nudge(0, -10))
        bind("Shift+Down", lambda: self._nudge(0, 10))
        bind("Ctrl+Left", lambda: self._nudge(-1, 0, fine=True))
        bind("Ctrl+Right", lambda: self._nudge(1, 0, fine=True))
        bind("Ctrl+Up", lambda: self._nudge(0, -1, fine=True))
        bind("Ctrl+Down", lambda: self._nudge(0, 1, fine=True))

    def _editing_text(self) -> bool:
        focus = QApplication.focusWidget()
        return isinstance(focus, (QLineEdit, QPlainTextEdit))

    def _delete_shortcut(self) -> None:
        if self._editing_text():
            return
        self.delete_selected_element()

    def _focus_field(self, key: str) -> None:
        field = getattr(self.properties, "_fields", {}).get(key)
        if isinstance(field, QLineEdit):
            field.setFocus()
            field.selectAll()

    # ------------------------------------------------------------- targets
    def show(self):  # noqa: N802 - QWidget API
        self._refresh_targets()
        super().show()
        self.raise_()
        self.activateWindow()
        return self

    def target_names(self) -> list[str]:
        return [name for name, _ in getattr(self, "_target_map", [])]

    def _refresh_targets(self) -> None:
        self.panel_picker.blockSignals(True)
        current = self._current_target_name
        self.panel_picker.clear()
        self._target_map = []
        self.panel_select_menu.clear()
        if isinstance(self.main, QWidget):
            for name, widget in _root_candidates(self.main):
                self.panel_picker.addItem(name)
                self._target_map.append((name, widget))
                action = self.panel_select_menu.addAction(name)
                action.setCheckable(True)
                action.triggered.connect(
                    lambda _checked=False, i=len(self._target_map)-1: self._set_target_by_index(i))
        self.panel_picker.blockSignals(False)
        if self._target_map:
            index = next((i for i, (name, _) in enumerate(self._target_map) if name == current), 0)
            self.panel_picker.setCurrentIndex(index)
            self._set_target_by_index(index)
        else:
            self.panel_select_button.setText("SELECT PANEL ▾")
            self.panel_target_info.setText("No panels discovered")
            if hasattr(self, "figma_export_button"):
                self.figma_export_button.setEnabled(False)

    def _set_target_by_index(self, index: int) -> None:
        if index < 0 or index >= len(getattr(self, "_target_map", [])):
            return
        name, target = self._target_map[index]
        self._current_target_name = name
        self.panel_picker.blockSignals(True)
        self.panel_picker.setCurrentIndex(index)
        self.panel_picker.blockSignals(False)
        self.panel_select_button.setText(f"{name} ▾")
        self.panel_target_info.setText(
            f"EDITING · {name} · {class_simple_name(target) if widget_alive(target) else 'unavailable'}")
        # Keep the menu's checkmark synchronized with the active target.
        for i, action in enumerate(self.panel_select_menu.actions()):
            action.setChecked(i == index)
        if self.main is not None:
            try:
                self.main._layout_active_panel = name
            except Exception:
                pass
        self.viewport.set_target(self.main, target)
        self.viewport.grid_enabled = self.grid_toggle.isChecked()
        self.viewport.grid_size = self._grid_size_value()
        self.viewport.snap_enabled = self.snap_toggle.isChecked()
        self.viewport.snap_mode = self.snap_mode.currentText()
        self._rebuild_explorer(target)
        self.set_selection([target] if widget_alive(target) else [])
        kind = class_simple_name(target) if widget_alive(target) else "none"
        self.viewport_info.setText(
            f"{name} · {kind} · {target.width()}×{target.height()}"
            if widget_alive(target) else f"{name} · unavailable")
        self.statusBar().showMessage(f"EDITING · {name}")
        if hasattr(self, "figma_export_button"):
            self.figma_export_button.setEnabled(widget_alive(target))

    def _target_widget(self) -> QWidget | None:
        for name, obj in getattr(self, "_target_map", []):
            if name == self._current_target_name:
                return obj if widget_alive(obj) else None
        return None

    # ------------------------------------------------------------ figma export
    @staticmethod
    def _css_names(widget: QWidget) -> set:
        """Identity tokens a Qt rule is allowed to name this widget with."""
        names: set = set()
        try:
            names.add(class_simple_name(widget))
            for cls in type(widget).__mro__:
                name = getattr(cls, "__name__", "")
                if name and name != "object":
                    names.add(name)
            object_name = str(widget.objectName() or "").strip()
            if object_name:
                names.add(object_name)
        except Exception:
            pass
        return names

    @staticmethod
    def _style_chain(widget: QWidget, depth: int = 4) -> list:
        """This widget plus the ancestors whose stylesheets also apply to it.

        Qt cascades a parent's stylesheet down its subtree, and most HUD widgets
        are styled that way (``ui.py`` puts the panel's rules on the panel). Only
        reading ``widget.styleSheet()`` therefore left the semantic fallback — and
        every capture-free export — without the paint Qt was really showing.
        """
        chain, current = [], widget
        while current is not None and len(chain) < max(1, depth):
            try:
                sheet = str(current.styleSheet() or "")
                if sheet:
                    chain.append(sheet)
                current = current.parentWidget()
            except (RuntimeError, TypeError):
                break
        return chain

    @classmethod
    def _extract_css_paint(cls, widget: QWidget, stylesheet: str) -> dict | None:
        """The widget's own background paint: colour, alpha and gradient.

        The previous version searched the whole stylesheet for the first
        ``background:#hex`` — on a HUD panel whose sheet styles its buttons,
        labels and scrollbars that exported some *descendant's* paint as the
        panel's, and anything written as ``rgba()`` or ``qlineargradient()``
        (which is how this project's QSS is generated) matched nothing at all.
        Matching the cascade properly and parsing Qt's own paint functions keeps
        the real background, its opacity and its gradient in the export.
        """
        names = cls._css_names(widget)
        for sheet in cls._style_chain(widget) or ([stylesheet] if stylesheet else []):
            paint = qss_paint(sheet, names)
            if paint:
                return paint
        if not stylesheet:
            return None
        # Nothing in the cascade names this widget directly; a whole-sheet value
        # is still a better export than no background at all.
        match = re.search(r"background(?:-color)?\s*:\s*([^;{}]+)", stylesheet)
        return parse_qt_paint_value(match.group(1).strip()) if match else None

    @classmethod
    def _extract_css_color(cls, widget: QWidget, stylesheet: str) -> tuple[str, float] | None:
        """Foreground colour + alpha from the rules that address this widget."""
        names = cls._css_names(widget)
        for sheet in cls._style_chain(widget) or ([stylesheet] if stylesheet else []):
            value = qss_property(sheet, names, "color", "foreground")
            if value:
                hex_value, alpha = split_color(value, "")
                if hex_value:
                    return hex_value, alpha
        if not stylesheet:
            return None
        match = re.search(r"(?<!-)\bcolor\s*:\s*([^;{}]+)", stylesheet)
        if not match:
            return None
        hex_value, alpha = split_color(match.group(1).strip(), "")
        return (hex_value, alpha) if hex_value else None

    @classmethod
    def _extract_css_border(cls, widget: QWidget, stylesheet: str) -> dict | None:
        """``{edge: (#color, width, alpha, style)}`` for the widget's real border."""
        names = cls._css_names(widget)
        for sheet in cls._style_chain(widget) or ([stylesheet] if stylesheet else []):
            border = qss_border(sheet, names)
            if border:
                return border
        return None

    @classmethod
    def _extract_css_radius(cls, widget: QWidget, stylesheet: str) -> dict | None:
        """Per-corner radii, so rounded HUD cards stay rounded in the export."""
        names = cls._css_names(widget)
        for sheet in cls._style_chain(widget) or ([stylesheet] if stylesheet else []):
            radius = qss_radius(sheet, names)
            if radius:
                return radius
        return None

    @staticmethod
    def _figma_runtime_kind(widget: QWidget) -> str:
        """Map common Qt controls to the exporter's editable SVG primitives."""
        mapping = {
            "QLabel": "Text",
            "QPushButton": "Button",
            "QToolButton": "Button",
            "QCheckBox": "CheckBox",
            "QRadioButton": "CheckBox",
            "QLineEdit": "Input",
            "QPlainTextEdit": "Input",
            "QTextEdit": "Input",
            "QProgressBar": "ProgressBar",
            "QSlider": "ProgressBar",
        }
        return mapping.get(class_simple_name(widget), "Panel")

    def _figma_runtime_specs(self, target: QWidget, panel_name: str, existing: dict) -> dict:
        """Build editable specs from real widgets inside the selected panel.

        The Layout Editor's ``_layout_elements`` store contains custom
        ElementWidgets only.  Ordinary QLabel/QPushButton/QLineEdit/etc. remain
        live Qt widgets, so they must be captured from the target hierarchy at
        export time or an otherwise valid SVG can contain only its background.
        """
        result: dict[str, dict] = {}
        used_names = {str(name) for name in existing}
        # Custom (engine-drawn) elements are exported from the persisted spec
        # store; a plain widget parented *inside* one used to be dropped on the
        # floor, so anything hosted by a custom card silently disappeared from
        # the export. It is now nested under that element instead of lost.
        element_names = {}
        for name, hosted in (getattr(self.main, "_layout_widgets", {}) or {}).items():
            try:
                if widget_alive(hosted):
                    element_names[id(hosted)] = str(name)
            except RuntimeError:
                continue

        def custom_ancestor_name(widget: QWidget) -> str | None:
            parent = widget.parentWidget()
            while parent is not None and parent is not target:
                try:
                    mapped = element_names.get(id(parent))
                    if mapped is not None:
                        return mapped
                    if isinstance(parent, ElementWidget) or parent.property("_jarvis_layout_custom"):
                        return str(getattr(parent, "_layout_element_name", "")
                                  or parent.objectName() or class_simple_name(parent))
                except RuntimeError:
                    return None
                parent = parent.parentWidget()
            return None

        def unique_name(widget: QWidget, ordinal: int) -> str:
            try:
                base = widget.objectName().strip()
            except RuntimeError:
                base = ""
            base = base or class_simple_name(widget) or "Widget"
            candidate = base
            if candidate not in used_names:
                used_names.add(candidate)
                return candidate
            candidate = f"{base} (runtime {ordinal})"
            while candidate in used_names:
                ordinal += 1
                candidate = f"{base} (runtime {ordinal})"
            used_names.add(candidate)
            return candidate

        try:
            descendants = [w for w in target.findChildren(QWidget) if widget_alive(w)]
        except RuntimeError:
            return result

        # Pass 1 — decide which widgets become their own editable layer.
        # Visibility is measured *relative to the panel*: a panel that is
        # currently closed (the normal state while editing in the Studio) used to
        # report every child as invisible, so the export contained nothing but a
        # background. ``isVisibleTo`` asks "would this show when the panel does",
        # which still excludes genuinely hidden children (inactive stack pages).
        candidates: list[tuple[int, QWidget, str | None]] = []
        for ordinal, widget in enumerate(descendants, start=1):
            if widget is target:
                continue
            try:
                if widget.width() <= 0 or widget.height() <= 0:
                    continue
                if not widget.isVisibleTo(target):
                    continue
                # An engine element is already exported from its own spec (which
                # is the *editable* version). Capturing it again as a runtime
                # layer would draw the same artwork twice in the imported file.
                if id(widget) in element_names or widget.property("_jarvis_layout_custom"):
                    continue
            except RuntimeError:
                continue
            candidates.append((ordinal, widget, custom_ancestor_name(widget)))

        candidate_ids = {id(widget) for _, widget, _ in candidates}

        def has_layer_child(widget: QWidget) -> bool:
            """True when a descendant is exported as its own editable layer."""
            try:
                for child in widget.findChildren(QWidget):
                    if id(child) in candidate_ids:
                        return True
            except RuntimeError:
                return False
            return False

        # Pass 2 — capture each one at the highest fidelity Qt can provide.
        for ordinal, widget, ancestor_name in candidates:
            try:
                point = widget.mapTo(target, QPoint(0, 0))
                state = capture_widget_state(widget, panel_name)
                kind = self._figma_runtime_kind(widget)
                name = unique_name(widget, ordinal)
                spec = default_spec(name, kind)
                spec["parent"] = ancestor_name or panel_name
                spec["x"] = point.x()
                spec["y"] = point.y()
                spec["w"] = widget.width()
                spec["h"] = widget.height()
                spec["z"] = ordinal
                spec["visible"] = True
                spec["enabled"] = bool(widget.isEnabled())
                spec["opacity"] = int(state.get("opacity", 100) or 100)
                spec["text"] = str(state.get("text") or "")
                spec["text_style"]["family"] = str(state.get("font_family") or "Exo 2")
                spec["text_style"]["size"] = max(1, int(round(float(state.get("font_size") or 11))))
                spec["text_style"]["bold"] = bool(state.get("font_bold", False))
                spec["text_style"]["italic"] = bool(state.get("font_italic", False))
                try:
                    alignment = widget.alignment()
                    spec["text_style"]["align"] = (
                        "left" if alignment & Qt.AlignmentFlag.AlignLeft else
                        "right" if alignment & Qt.AlignmentFlag.AlignRight else "center")
                    spec["text_style"]["v_align"] = (
                        "top" if alignment & Qt.AlignmentFlag.AlignTop else
                        "bottom" if alignment & Qt.AlignmentFlag.AlignBottom else "middle")
                except Exception:
                    pass
                # Glow/shadow live on a graphics effect, not in the stylesheet.
                for key in ("glow", "shadow"):
                    block = state.get(key)
                    if isinstance(block, dict) and block.get("enabled"):
                        spec[key] = dict(block)

                # A widget whose subtree is *not* exported separately must be
                # captured whole, otherwise nested content is lost; when its
                # children are their own layers, they stay out of the capture.
                include_children = not has_layer_child(widget)
                runtime_svg, runtime_image, runtime_mode = _figma_runtime_visual(
                    widget, include_children=include_children)
                if runtime_svg:
                    spec["_runtime_svg"] = runtime_svg
                    spec["_runtime_mode"] = runtime_mode
                    spec["text"] = ""
                elif runtime_image:
                    spec["_runtime_image"] = runtime_image
                    spec["_runtime_mode"] = runtime_mode

                stylesheet = str(state.get("stylesheet") or "")
                background = self._extract_css_paint(widget, stylesheet)
                foreground = self._extract_css_color(widget, stylesheet)
                border = self._extract_css_border(widget, stylesheet)
                radius = self._extract_css_radius(widget, stylesheet)

                try:
                    palette = widget.palette()
                    bg_palette = palette.color(QPalette.ColorRole.Base if kind == "Input"
                                               else QPalette.ColorRole.Button if kind == "Button"
                                               else QPalette.ColorRole.Window)
                    fg_palette = palette.color(QPalette.ColorRole.Text if kind == "Input"
                                               else QPalette.ColorRole.ButtonText if kind == "Button"
                                               else QPalette.ColorRole.WindowText)
                    bg_name, bg_alpha = split_color(bg_palette.name(QColor.NameFormat.HexArgb), "#04121b")
                    fg_name, _fg_alpha = split_color(fg_palette.name(QColor.NameFormat.HexArgb), "#e6f4ff")
                except Exception:
                    bg_name, bg_alpha, fg_name = "#04121b", 1.0, "#e6f4ff"

                # Text labels should stay transparent unless their stylesheet
                # explicitly paints a background; controls get their live Qt
                # palette as a useful editable approximation.
                if background is None:
                    if kind == "Text":
                        spec["background"]["opacity"] = 0
                    else:
                        spec["background"]["color"] = bg_name
                        spec["background"]["opacity"] = int(round(bg_alpha * 100))
                        spec["background"]["gradient"] = {"enabled": False}
                else:
                    spec["background"] = {
                        "color": background.get("color", bg_name),
                        "opacity": int(background.get("opacity", 100)),
                        "gradient": background.get("gradient") or {"enabled": False},
                    }
                if foreground is not None:
                    spec["text_style"]["color"] = foreground[0]
                    spec["text_style"]["opacity"] = int(round(foreground[1] * 100))
                else:
                    spec["text_style"]["color"] = fg_name

                if border is not None:
                    edges = border.get("edges") or {}
                    shared = edges.get("")
                    for side in ("top", "right", "bottom", "left"):
                        edge = edges.get(side) or shared
                        if not edge:
                            continue
                        color, width, alpha, style = edge
                        spec["borders"][side]["width"] = width
                        spec["borders"][side]["color"] = color
                        spec["borders"][side]["style"] = style
                        spec["borders"][side]["opacity"] = int(round(alpha * 100))
                if radius is not None:
                    for corner in ("tl", "tr", "br", "bl"):
                        if radius.get(corner) is not None:
                            spec["borders"]["radius"][corner] = radius[corner]

                result[name] = normalize_element(spec, name)
            except (RuntimeError, TypeError, ValueError):
                continue

        return result

    def _figma_export_elements(self, target: QWidget, panel_name: str) -> dict:
        """Combine persisted custom elements with the live selected widget tree."""
        elements = getattr(self.main, "_layout_elements", {}) or {}
        stored = {
            str(name): copy.deepcopy(spec)
            for name, spec in elements.items()
            if isinstance(spec, dict)
        }

        # Keep only custom elements belonging to the selected target.  A live
        # ElementWidget is authoritative when present; saved parent chains are
        # the fallback for elements that are persisted but not instantiated.
        layout_widgets = getattr(self.main, "_layout_widgets", {}) or {}
        hosted_names = set()
        for name, widget in layout_widgets.items():
            if not widget_alive(widget):
                continue
            try:
                cur = widget
                while cur is not None:
                    if cur is target:
                        hosted_names.add(str(name))
                        break
                    cur = cur.parentWidget()
            except RuntimeError:
                continue

        relevant_names = set(hosted_names)
        for name, spec in stored.items():
            parent = str(spec.get("parent") or "").strip()
            seen = {name}
            while parent and parent not in seen:
                if parent == panel_name:
                    relevant_names.add(name)
                    break
                seen.add(parent)
                parent_spec = stored.get(parent)
                parent = str(parent_spec.get("parent") or "").strip() if parent_spec else ""

        combined = {name: spec for name, spec in stored.items() if name in relevant_names}
        combined.update(self._figma_runtime_specs(target, panel_name, combined))
        return combined

    def _panel_background_paint(self, target: QWidget) -> dict:
        """The selected panel's real background: QSS first, live palette second.

        Returned in the exporter's paint-block form so a ``qlineargradient`` /
        ``rgba()`` background keeps its ramp and its alpha all the way into the
        SVG instead of collapsing to a hard-coded navy.
        """
        paint = self._extract_css_paint(target, str(target.styleSheet() or ""))
        if paint:
            return paint
        try:
            colour = target.palette().color(QPalette.ColorRole.Window)
            hex_value, alpha = split_color(colour.name(QColor.NameFormat.HexArgb), "#04121b")
        except Exception:
            hex_value, alpha = "#04121b", 1.0
        return {"color": hex_value, "opacity": int(round(alpha * 100)),
                "gradient": {"enabled": False}}

    def _export_target_as_figma(self) -> None:
        target = self._target_widget()
        if not widget_alive(target):
            self.log("[FIGMA] Export refused — no panel is selected.")
            QMessageBox.warning(self, "Download as Figma",
                                 "Select a panel before exporting it to Figma.")
            return

        panel_name = self._current_target_name or "Panel"
        try:
            width, height = int(target.width()), int(target.height())
            if width <= 0 or height <= 0:
                # A panel that has never been laid out has no real geometry to
                # export; say so instead of delivering a blank 1x1 canvas.
                raise FigmaExportError(
                    f"{panel_name} has no size yet — open it once so the layout "
                    "is built, then export again.")
            elements = self._figma_export_elements(target, panel_name)
            background = self._panel_background_paint(target)
            panel_runtime_svg = _figma_svg_capture(target, include_children=False)
            path = export_panel_to_figma(
                panel_name, (width, height), elements, panel_background=background,
                panel_runtime_svg=panel_runtime_svg)
        except FigmaExportError as exc:
            self.log(f"[FIGMA] Export failed — {exc}")
            QMessageBox.warning(self, "Download as Figma", f"Could not export {panel_name}:\n\n{exc}")
            return
        except Exception as exc:  # never let an export crash the Studio
            self.log(f"[FIGMA] Export failed unexpectedly — {exc}")
            QMessageBox.critical(self, "Download as Figma",
                                  f"Could not export {panel_name} due to an unexpected error:\n\n{exc}")
            return

        self.log(f"[FIGMA] Exported {panel_name} → {path}")
        self.statusBar().showMessage(f"DOWNLOADED · {path.name}")

    # ------------------------------------------------------------ explorer
    def _rebuild_explorer(self, root: QWidget) -> None:
        if not widget_alive(root):
            return
        self.explorer.clear()
        self._tree_items = {}
        item = QTreeWidgetItem([self._friendly(root, self._current_target_name)])
        item.setData(0, ExplorerTree.ROLE_WIDGET, root)
        item.setData(0, ExplorerTree.ROLE_KIND, "element")
        item.setText(0, f"◫  {self._friendly(root, self._current_target_name)}")
        self.explorer.addTopLevelItem(item)
        self._tree_items[id(root)] = item
        self._add_children(item, root, depth=0)
        item.setExpanded(True)

    def _add_children(self, parent_item: QTreeWidgetItem, widget: QWidget, depth: int) -> None:
        if depth >= 8 or not widget_alive(widget):
            return
        try:
            children = [c for c in widget.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly)
                if c.isVisible() or c.property("_jarvis_layout_touched")
                or c.property("_jarvis_layout_custom")]
        except RuntimeError:
            return
        for child in children:
            label = self._friendly(child)
            item = QTreeWidgetItem([f"{self._icon_for(child)}  {label}"])
            item.setData(0, ExplorerTree.ROLE_WIDGET, child)
            item.setData(0, ExplorerTree.ROLE_KIND, "element")
            if is_element_widget(child):
                item.setForeground(0, _qcolor(ACCENT_BRIGHT))
            if not child.isVisible():
                item.setForeground(0, _qcolor(TEXT_DIM))
            parent_item.addChild(item)
            self._tree_items[id(child)] = item
            if self._script_enabled(child):
                script_item = QTreeWidgetItem(["⌁  Script"])
                script_item.setData(0, ExplorerTree.ROLE_WIDGET, child)
                script_item.setData(0, ExplorerTree.ROLE_KIND, "script")
                script_item.setForeground(0, _qcolor("#8ff5b0"))
                item.addChild(script_item)
            if child.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
                self._add_children(item, child, depth + 1)
        if depth < 3 and is_element_widget(widget):
            parent_item.setExpanded(True)

    @staticmethod
    def _icon_for(widget: QWidget) -> str:
        kind = ""
        if is_element_widget(widget):
            kind = str(widget.element_type())
        if not kind:
            kind = class_simple_name(widget)
        icons = {
            "Image": "▣", "Text": "T", "Button": "◉", "Panel": "▤", "Frame": "▢",
            "HUD Card": "▦", "Indicator": "◍", "Input": "▭", "ProgressBar": "▬",
            "Separator": "─", "Spacer": "·", "WebView": "◌", "Toggle": "◑",
            "CheckBox": "☑", "Uiverse": "✦", "Custom": "✧",
        }
        if kind in icons:
            return icons[kind]
        if class_simple_name(widget).endswith("Label"):
            return "T"
        if class_simple_name(widget).endswith("Button"):
            return "◉"
        return "▪"

    def _script_enabled(self, widget: QWidget) -> bool:
        spec = self.spec_for(widget)
        return bool(spec and as_bool(get_path(spec, "script.enabled", False)))

    def _friendly(self, widget: QWidget, root_name: str = "") -> str:
        try:
            name = widget.objectName().strip()
        except RuntimeError:
            return "?"
        if isinstance(widget, ElementWidget):
            return str(widget.element_name() or name or "Element")
        if name:
            return name
        if root_name:
            return root_name
        text = _widget_text(widget).strip().replace("\n", " ")
        if text:
            return f"{class_simple_name(widget)} · {text[:32]}"
        return class_simple_name(widget)

    def _filter_tree(self, text: str) -> None:
        query = str(text or "").strip().lower()
        root = self.explorer.invisibleRootItem()
        for index in range(root.childCount()):
            self._filter_tree_item(root.child(index), query, is_root=True)

    def _filter_tree_item(self, item: QTreeWidgetItem, query: str, is_root: bool = False) -> bool:
        own = (query in item.text(0).lower()) if query else True
        any_child = False
        for index in range(item.childCount()):
            any_child = self._filter_tree_item(item.child(index), query) or any_child
        visible = own or any_child
        item.setHidden(not visible and not is_root)
        if any_child:
            item.setExpanded(True)
        return visible

    def _explorer_context(self, widget, global_pos) -> None:
        menu = QMenu(self)
        if widget_alive(widget):
            entries = [
                ("Select", lambda: self.select_widget(widget)),
                ("Copy", lambda: self.copy_selection()),
                ("Paste", lambda: self.paste_clipboard()),
                ("Duplicate", lambda: self.duplicate_selection()),
                ("Rename (F2)", lambda: self._focus_field("name")),
                ("Delete", lambda: self.delete_selected_element()),
                ("-", None),
                ("View Code", lambda: self._open_code_for(widget)),
                ("Edit Script", lambda: self._open_script_for(widget)),
                ("Properties", lambda: self._show_tab(0)),
                ("-", None),
                ("Hide", lambda: self.do_action("hide", None)),
                ("Show", lambda: self.do_action("show", None)),
                ("Lock", lambda: self.do_action("lock", None)),
                ("Unlock", lambda: self.do_action("unlock", None)),
                ("-", None),
                ("Add Child", lambda: self._add_child(widget)),
                ("Reparent to Target", lambda: self._reparent(widget, self._target_widget())),
                ("-", None),
                ("Bring to Front", lambda: self.do_action("z_front", None)),
                ("Send to Back", lambda: self.do_action("z_back", None)),
                ("Group", lambda: self.do_action("group", None)),
                ("Ungroup", lambda: self.do_action("ungroup", None)),
            ]
            for label, slot in entries:
                if label == "-":
                    menu.addSeparator()
                    continue
                action = menu.addAction(label)
                action.triggered.connect(lambda _=False, s=slot: s())
        else:
            for label, slot in (("Select All", self.select_all),
                                ("Paste", self.paste_clipboard),
                                ("Clear Selection", lambda: self.set_selection([])),
                                ("Fit to Screen", self.zoom_fit),
                                ("Refresh Explorer", lambda: self._rebuild_explorer(self._target_widget()))):
                action = menu.addAction(label)
                action.triggered.connect(lambda _=False, s=slot: s())
        menu.exec(global_pos)

    # ----------------------------------------------------------- viewport
    def _viewport_picked(self, widgets, additive: bool) -> None:
        coming = [w for w in (widgets or []) if widget_alive(w)]
        if not coming:
            return
        if additive:
            merged = list(self.selection.items())
            for widget in coming:
                if widget not in merged:
                    merged.append(widget)
            self.set_selection(merged, primary=coming[-1])
        else:
            self.set_selection(coming, primary=coming[-1])
        if self._tool in ("text", "image", "frame", "panel"):
            self.add_element({"text": "Text", "image": "Image", "frame": "Frame",
                              "panel": "Panel"}[self._tool], parent_name=self.parent_name_for(coming[0]))
            self._set_tool("select")

    def _viewport_marquee(self, widgets) -> None:
        coming = [w for w in (widgets or []) if widget_alive(w)]
        if not coming:
            self.set_selection([])
            return
        self.set_selection(coming, primary=coming[-1])

    def _viewport_pan(self, delta: QPoint) -> None:
        bar = self.viewport_scroll.horizontalScrollBar()
        bar.setValue(bar.value() - delta.x())
        vbar = self.viewport_scroll.verticalScrollBar()
        vbar.setValue(vbar.value() - delta.y())

    def _viewport_zoom_step(self, step: float) -> None:
        self.set_zoom(self.viewport.zoom + step)

    def _viewport_drop_files(self, paths, target_pos) -> None:
        images = [p for p in paths if Path(str(p)).suffix.lower() in IMAGE_SUFFIXES]
        if not images:
            self.log("[LAYOUT] dropped files are not images — nothing created")
            return
        self.import_image_files(images, drop_point=target_pos)

    def _viewport_context(self, widget, global_pos) -> None:
        menu = QMenu(self)
        if widget_alive(widget) and widget not in self.selection.items():
            self.select_widget(widget)
        has_selection = bool(self.selection.items())
        entries = [
            ("Select All", self.select_all),
            ("Clear Selection", lambda: self.set_selection([])),
            ("-", None),
            ("Copy", self.copy_selection),
            ("Cut", self.cut_selection),
            ("Paste", self.paste_clipboard),
            ("Duplicate", self.duplicate_selection),
            ("Delete", self.delete_selected_element),
            ("-", None),
            ("Add Element", lambda: self.add_element("Panel", parent_name=self._current_target_name)),
            ("Import Image", self.import_image_as_element),
            ("Set as Background", lambda: self.do_action("set_background", True)),
            ("Set as Foreground", lambda: self.do_action("set_background", False)),
            ("Replace Image", lambda: self.do_action("replace_image", None)),
            ("-", None),
            ("Bring Forward", lambda: self.do_action("z_forward", None)),
            ("Bring to Front", lambda: self.do_action("z_front", None)),
            ("Send Backward", lambda: self.do_action("z_backward", None)),
            ("Send to Back", lambda: self.do_action("z_back", None)),
            ("-", None),
            ("Align", self._align_menu(menu)),
            ("Distribute", self._distribute_menu(menu)),
            ("Group", lambda: self.do_action("group", None)),
            ("Ungroup", lambda: self.do_action("ungroup", None)),
            ("-", None),
            ("Focus Selection", self.zoom_focus),
            ("Fit to Screen", self.zoom_fit),
            ("View Code", self._show_code_tab),
        ]
        for label, slot in entries:
            if label == "-":
                menu.addSeparator()
                continue
            if isinstance(slot, QMenu):
                menu.addMenu(slot)
                continue
            action = menu.addAction(label)
            if not has_selection and label in ("Copy", "Cut", "Duplicate", "Delete",
                                               "Set as Background", "Set as Foreground",
                                               "Replace Image", "Align", "Distribute"):
                action.setEnabled(False)
            action.triggered.connect(lambda _=False, s=slot: s())
        menu.addSeparator()
        grid_action = menu.addAction("Show Grid")
        grid_action.setCheckable(True)
        grid_action.setChecked(self.grid_toggle.isChecked())
        grid_action.toggled.connect(lambda value: self.grid_toggle.setChecked(value))
        snap_action = menu.addAction("Snap")
        snap_action.setCheckable(True)
        snap_action.setChecked(self.snap_toggle.isChecked())
        snap_action.toggled.connect(lambda value: self.snap_toggle.setChecked(value))
        menu.exec(global_pos)

    def set_zoom(self, value: float) -> None:
        self.viewport.set_zoom(value)
        percent = int(round(self.viewport.zoom * 100))
        self.zoom.blockSignals(True)
        self.zoom.setValue(max(10, min(400, percent)))
        self.zoom.blockSignals(False)

    def zoom_in(self) -> None:
        self.set_zoom(self.viewport.zoom + 0.1)

    def zoom_out(self) -> None:
        self.set_zoom(self.viewport.zoom - 0.1)

    def zoom_fit(self) -> None:
        target = self._target_widget()
        if not widget_alive(target):
            return
        area = self.viewport_scroll.viewport().size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scale = min((area.width() - 90) / target.width(), (area.height() - 90) / target.height())
        self.set_zoom(max(0.1, min(4.0, scale)))

    def zoom_focus(self) -> None:
        widget = self.selection.primary()
        if not widget_alive(widget):
            return
        rect = self.viewport._widget_rect(widget)
        if rect is None:
            return
        canvas = self.viewport._canvas_rect(rect)
        self.viewport_scroll.horizontalScrollBar().setValue(
            max(0, canvas.center().x() - self.viewport_scroll.viewport().width() // 2))
        self.viewport_scroll.verticalScrollBar().setValue(
            max(0, canvas.center().y() - self.viewport_scroll.viewport().height() // 2))

    def zoom_center(self) -> None:
        self.viewport_scroll.horizontalScrollBar().setValue(
            max(0, self.viewport.width() // 2 - self.viewport_scroll.viewport().width() // 2))
        self.viewport_scroll.verticalScrollBar().setValue(
            max(0, self.viewport.height() // 2 - self.viewport_scroll.viewport().height() // 2))

    def zoom_reset(self) -> None:
        self.set_zoom(1.0)
        self.viewport_scroll.horizontalScrollBar().setValue(0)
        self.viewport_scroll.verticalScrollBar().setValue(0)

    def _grid_size_value(self) -> int:
        try:
            return max(1, int(self.grid_size.currentText()))
        except (ValueError, AttributeError):
            return 8

    def _set_grid(self, grid: bool | None = None) -> None:
        if grid is not None:
            self.viewport.grid_enabled = bool(grid)
        self.viewport.grid_size = self._grid_size_value()
        self.viewport.mark_dirty()

    def _set_snap(self, value: bool) -> None:
        self.viewport.snap_enabled = bool(value)

    def _set_snap_mode(self, mode: str) -> None:
        self.viewport.snap_mode = str(mode or "grid")
        self.viewport.snap_flags["pixel"] = self.viewport.snap_mode == "pixel"
        self.viewport.mark_dirty()

    def _set_tool(self, tool: str) -> None:
        self._tool = str(tool or "select")
        for name, button in self.tool_buttons.items():
            button.setChecked(name == self._tool)
        self.viewport.set_tool(self._tool)
        if self._tool in ("text", "image", "frame", "panel"):
            self.statusBar().showMessage(
                f"TOOL · {self._tool.upper()} — click in the viewport to place the element")
        else:
            self.statusBar().showMessage(f"TOOL · {self._tool.upper()}")

    # ---------------------------------------------------------- selection
    def set_selection(self, widgets, primary=None, refresh: bool = True) -> None:
        items = [w for w in (widgets or []) if widget_alive(w)]
        if items:
            self.selection.select_many(items)
            if widget_alive(primary):
                self.selection.set_primary(primary)
        else:
            self.selection.clear()
        self.viewport.set_selection(self.selection.items(), self.selection.primary())
        self._highlight_selection()
        if refresh:
            self._selection_changed()

    def select_widget(self, widget, additive: bool = False, toggle: bool = False) -> None:
        if not isinstance(widget, QWidget) or not widget_alive(widget):
            return
        self.selection.select(widget, additive=additive, toggle=toggle)
        self.viewport.set_selection(self.selection.items(), self.selection.primary())
        self._highlight_selection()
        self._selection_changed()

    def select_all(self) -> None:
        root = self._target_widget()
        if not widget_alive(root):
            return
        candidates = [root] + [c for c in root.findChildren(QWidget)
                               if c.isVisible() or c.property("_jarvis_layout_custom")]
        self.set_selection(candidates, primary=root)

    def _selection_changed(self) -> None:
        """Rebuild Properties/CODE for the new selection (never during a drag)."""
        dropped = self.selection.prune()
        if dropped:
            self.log("[LAYOUT] selection cleared — widget no longer exists")
        primary = self.selection.primary()
        self._prune_runtime_specs()
        if not widget_alive(primary):
            self.properties.refresh(None, None)
            self.animation_panel.set_subject(None)
            self.selection_label.setText("No selection")
            self.code_target.setText("no element")
            self._clear_code_editors()
            return
        self.animation_panel.set_subject(primary)
        self.selection_label.setText(
            f"{class_simple_name(primary)} · {self._friendly(primary)}"
            f"{f' · +{self.selection.count() - 1} more' if self.selection.count() > 1 else ''}")
        item = self._tree_items.get(id(primary))
        if item is not None:
            self.explorer.setCurrentItem(item)
        self.viewport.mark_dirty()
        self._schedule_inspector_refresh()

    def _schedule_inspector_refresh(self) -> None:
        """Rebuild Properties/CODE on the next event-loop turn.

        Building the 19-section Inspector takes real work, so the element's
        visual selection happens first and the panel fills in immediately after.
        Coalescing by stamp means a rapid selection sweep rebuilds once.
        """
        self._inspector_stamp = getattr(self, "_inspector_stamp", 0) + 1
        stamp = self._inspector_stamp
        QTimer.singleShot(0, lambda: self._do_inspector_refresh(stamp))

    def _do_inspector_refresh(self, stamp: int) -> None:
        if stamp != getattr(self, "_inspector_stamp", 0):
            return
        primary = self.selection.primary()
        spec = self.spec_for(primary) if widget_alive(primary) else None
        if not widget_alive(primary) or spec is None:
            return
        self.properties.refresh(primary, spec, self.selection.count(),
                                element_type=class_simple_name(primary),
                                supports=lambda feature, w=primary: self.supports(w, feature))
        self._sync_code_editors(primary, spec)

    def _prune_runtime_specs(self) -> None:
        for key in list(self._runtime_specs):
            widget = self._runtime_widgets.get(key)
            if not widget_alive(widget):
                self._runtime_specs.pop(key, None)
                self._runtime_widgets.pop(key, None)

    def _highlight_selection(self) -> None:
        try:
            if self.main is not None:
                previous = getattr(self.main, "_layout_selected_widget", None)
                if widget_alive(previous) and previous not in self.selection.items():
                    previous.setProperty("_jarvis_layout_selected", False)
                    previous.update()
                primary = self.selection.primary()
                if widget_alive(primary):
                    self.main._layout_selected_widget = primary
                    primary.setProperty("_jarvis_layout_selected", True)
                    primary.update()
        except Exception:
            pass

    def parent_name_for(self, widget) -> str:
        if not widget_alive(widget):
            return ""
        spec = self.spec_for(widget)
        if spec is not None and str(spec.get("parent") or ""):
            return str(spec["parent"])
        if widget is self._target_widget():
            return self._current_target_name
        return self._current_target_name

    def element_names(self) -> list[str]:
        names: list[str] = []
        if self.main is not None:
            names.extend(str(n) for n in getattr(self.main, "_layout_elements", {}) or {})
        for key, widget in list(self._runtime_widgets.items()):
            if widget_alive(widget):
                try:
                    names.append(str(widget.objectName()))
                except RuntimeError:
                    continue
        return names

    # --------------------------------------------------------------- spec
    def spec_for(self, widget) -> dict | None:
        """The editable spec for any widget (custom element or runtime widget)."""
        if not widget_alive(widget):
            return None
        if isinstance(widget, ElementWidget):
            return widget.spec
        key = id(widget)
        spec = self._runtime_specs.get(key)
        if spec is None:
            spec = self._runtime_spec_from_widget(widget)
            self._runtime_specs[key] = spec
            self._runtime_widgets[key] = widget
        return spec

    def _runtime_spec_from_widget(self, widget) -> dict:
        state = capture_widget_state(widget, self._current_target_name or "")
        spec = default_spec(str(widget.objectName() or class_simple_name(widget)), "Panel")
        spec["type"] = class_simple_name(widget)
        spec["kind"] = spec["type"]
        # Never call parent_name_for() from here: it consults spec_for(), which
        # would re-enter this builder.
        spec["parent"] = self._current_target_name or "Main Surface"
        for key in ("x", "y", "w", "h", "min_w", "min_h", "max_w", "max_h",
                    "visible", "enabled", "opacity", "text", "tooltip", "object_name"):
            if key in state:
                spec[key] = state[key]
        spec["stylesheet"] = state.get("stylesheet", "")
        spec["text_style"]["family"] = str(state.get("font_family") or "Exo 2")
        spec["text_style"]["size"] = int(round(float(state.get("font_size") or 11)))
        spec["text_style"]["bold"] = as_bool(state.get("font_bold", False))
        spec["text_style"]["italic"] = as_bool(state.get("font_italic", False))
        if as_bool(get_path(state, "glow.enabled", False)):
            spec["glow"].update({k: v for k, v in (state["glow"] or {}).items() if k in spec["glow"]})
            spec["glow"]["enabled"] = True
        if as_bool(get_path(state, "shadow.enabled", False)):
            spec["shadow"].update({k: v for k, v in (state["shadow"] or {}).items() if k in spec["shadow"]})
            spec["shadow"]["enabled"] = True
        return spec

    def supports(self, widget, feature: str) -> bool:
        """Which features are genuinely available for this widget kind."""
        if not widget_alive(widget):
            return False
        if isinstance(widget, ElementWidget):
            return widget.supports(feature)
        # Runtime J.A.R.V.I.S. widgets: QSS + graphics effects only.
        if feature in ("rotation", "text_stroke", "text_effects", "crop", "effects_stack"):
            return False
        if feature == "image":
            return isinstance(widget, QLabel)
        if feature == "text":
            return hasattr(widget, "setText") or hasattr(widget, "setPlainText")
        return True

    # ------------------------------------------------------- property edit
    def _inspector_changed(self, path: str, value) -> None:
        widget = self.selection.primary()
        if not widget_alive(widget):
            return
        self.set_property(widget, path, value)

    def _inspector_action(self, action: str, payload) -> None:
        self.do_action(action, payload)

    def _inspector_preset(self, group: str, name: str) -> None:
        self._apply_preset(group, name)

    def set_property(self, widget, path: str, value, history: bool = True,
                     persist: bool = True) -> None:
        """Apply one property (``glow.radius``) to the selection, live."""
        if not widget_alive(widget):
            return
        feature = self._feature_for_path(path)
        targets = [w for w in self.selection.items()
                   if widget_alive(w) and self.supports(w, feature)]
        if not targets:
            targets = [widget]
        before: list[tuple[Any, dict]] = []
        after: list[tuple[Any, dict]] = []
        for target in targets:
            snapshot = self.capture_snapshot(target)
            if snapshot is None:
                continue
            before.append((target, snapshot))
            self._apply_path(target, path, value)
            after.append((target, self.capture_snapshot(target) or {}))
        if not before:
            return
        if history and not self.history.locked:
            entries = [{"widget": target, "before": past, "after": future, "kind": "element"
                        if isinstance(target, ElementWidget) else "widget"}
                       for (target, past), (_t, future) in zip(before, after)]
            if len(entries) == 1:
                self.history.push(entries[0]["widget"], entries[0]["before"], entries[0]["after"],
                                  entries[0]["kind"], label=path)
            else:
                self.history.push_many(entries, label=path)
        self._persist_elements()
        if persist:
            self._request_save()
        self.viewport.mark_dirty()
        self._sync_geometry_fields(primary=widget)

    def _feature_for_path(self, path: str) -> str:
        head = str(path or "").split(".", 1)[0]
        if head in ("rotation", "rotation_center"):
            return "rotation"
        if head == "image":
            return "image"
        if head == "text":
            return "text"
        if head == "text_style":
            if ".stroke." in path:
                return "text_stroke"
            if ".glow." in path or ".shadow." in path:
                return "text_effects"
            return "typography"
        if head == "effects":
            return "effects_stack"
        return "appearance"

    def _apply_path(self, widget, path: str, value) -> None:
        spec = self.spec_for(widget)
        if spec is None:
            return
        # Derived/aggregate controls the inspector exposes as a single row.
        if path == "background.gradient.stop_count":
            self._resize_gradient_stops(spec, int(value))
        elif path == "custom":
            try:
                spec["custom"] = json.loads(str(value) or "{}")
            except (ValueError, TypeError):
                self.log("[ERROR] custom metadata is not valid JSON — kept previous value")
                return
        elif path.endswith(".style") and path.startswith("borders."):
            set_path(spec, path, str(value))
        else:
            set_path(spec, path, value)

        if path == "type" and isinstance(widget, ElementWidget):
            spec["type"] = str(value)
            spec["kind"] = str(value)
            widget.set_spec_property("type", str(value))
            spec = widget.spec
        if isinstance(widget, ElementWidget):
            widget.apply_spec(spec)
            self._attach_script(widget)
        else:
            apply_runtime_style(widget, spec)
            widget.setProperty("_jarvis_layout_touched", True)

    def _resize_gradient_stops(self, spec: dict, count: int) -> None:
        count = max(2, min(8, int(count)))
        stops = get_path(spec, "background.gradient.stops", None)
        if not isinstance(stops, list):
            stops = [[0.0, ACCENT], [1.0, BG_DEEP]]
        while len(stops) < count:
            position = len(stops) / max(1, count - 1)
            stops.append([round(position, 3), ACCENT_BRIGHT])
        while len(stops) > count:
            stops.pop()
        set_path(spec, "background.gradient.stops", stops)

    def capture_snapshot(self, widget) -> dict | None:
        if not widget_alive(widget):
            return None
        if isinstance(widget, ElementWidget):
            return copy.deepcopy(widget.spec)
        return capture_widget_state(widget, self._current_target_name or "")

    def _document_snapshot(self) -> dict:
        """A serialisable snapshot of the element document (no widget pointers)."""
        elements = getattr(self.main, "_layout_elements", {}) if self.main is not None else {}
        return {"elements": copy.deepcopy(elements if isinstance(elements, dict) else {})}

    def _restore_document(self, state: dict) -> bool:
        elements = (state or {}).get("elements")
        if not isinstance(elements, dict) or self.main is None:
            return False
        store = getattr(self.main, "_layout_elements", None)
        if not isinstance(store, dict):
            return False
        store.clear()
        store.update(copy.deepcopy(elements))
        try:
            if hasattr(self.main, "_apply_layout_elements"):
                self.main._apply_layout_elements()
        except Exception as exc:
            self.log(f"[ERROR] document restore failed — {exc}")
            return False
        self._rebuild_explorer(self._target_widget())
        self._selection_changed()
        self._request_save()
        return True

    def _apply_history_state(self, widget, state: dict, kind: str = "element") -> bool:
        if kind == "document":
            return self._restore_document(state)
        if not widget_alive(widget):
            return False
        try:
            if kind == "element" and isinstance(widget, ElementWidget):
                widget.apply_spec(copy.deepcopy(state))
                self._attach_script(widget)
            else:
                apply_widget_state(widget, state)
            self._persist_elements()
            if widget is self.selection.primary():
                spec = self.spec_for(widget)
                self.properties.refresh(widget, spec, self.selection.count(),
                                        element_type=class_simple_name(widget),
                                        supports=lambda feature, w=widget: self.supports(w, feature))
                self._sync_code_editors(widget, spec)
            self.viewport.mark_dirty()
            self._request_save()
            return True
        except Exception as exc:
            self.log(f"[ERROR] history step failed — {exc}")
            return False

    def commit_change(self, widget, before: dict | None = None, persist: bool = True,
                      label: str = "") -> None:
        """Public helper kept for compatibility with earlier callers."""
        if not widget_alive(widget):
            self._selection_changed()
            return
        if self.history.locked:
            return
        snapshot = before if before is not None else self._pending_snapshot(widget)
        after = self.capture_snapshot(widget)
        if snapshot == after:
            return
        kind = "element" if isinstance(widget, ElementWidget) else "widget"
        self.history.push(widget, snapshot or {}, after or {}, kind, label=label)
        self._persist_elements()
        if persist:
            self._request_save()

    def _pending_snapshot(self, widget) -> dict:
        return self.capture_snapshot(widget) or {}

    def _viewport_drag_started(self, widget) -> None:
        """Snapshot the pre-drag state so the whole drag is ONE history entry."""
        if not widget_alive(widget):
            return
        if id(widget) not in self._drag_origins:
            self._drag_origins[id(widget)] = self.capture_snapshot(widget) or {}

    def _viewport_geometry_changed(self, widget) -> None:
        if not widget_alive(widget):
            return
        widget.setProperty("_jarvis_layout_touched", True)
        if widget is self.selection.primary():
            self.properties.sync_geometry(widget)
            self.selection_label.setText(
                f"{class_simple_name(widget)} · {self._friendly(widget)} · "
                f"{widget.width()}×{widget.height()}")
        self._set_save_state("UNSAVED")

    def _viewport_drag_finished(self, widget) -> None:
        """A drag is ONE history operation and ONE save (TXT sections 42/43)."""
        if not widget_alive(widget):
            self._selection_changed()
            return
        before = self._drag_origins.pop(id(widget), None)
        after = self.capture_snapshot(widget)
        if before is None or before == after:
            self._persist_elements()
            self._request_save()
            return
        kind = "element" if isinstance(widget, ElementWidget) else "widget"
        self.history.push(widget, before, after or {}, kind, label="move")
        self._persist_elements()
        self._request_save()
        self.log(f"[LAYOUT] {self._friendly(widget)} → {widget.x()},{widget.y()} "
                 f"{widget.width()}×{widget.height()}")

    def _sync_geometry_fields(self, primary=None) -> None:
        widget = primary if widget_alive(primary) else self.selection.primary()
        if widget_alive(widget):
            self.properties.sync_geometry(widget)

    # ------------------------------------------------------------- actions
    def do_action(self, action: str, payload=None) -> None:
        items = [w for w in self.selection.items() if widget_alive(w)]
        primary = self.selection.primary()
        if action == "lock":
            self._set_flag("locked", True)
        elif action == "unlock":
            self._set_flag("locked", False)
        elif action == "hide":
            self._set_flag("hidden", True)
        elif action == "show":
            self._set_flag("hidden", False)
        elif action == "z_front":
            self._z_order("front")
        elif action == "z_back":
            self._z_order("back")
        elif action == "z_forward":
            self._z_order("forward")
        elif action == "z_backward":
            self._z_order("backward")
        elif action == "duplicate":
            self.duplicate_selection()
        elif action == "delete":
            self.delete_selected_element()
        elif action == "copy":
            self.copy_selection()
        elif action == "cut":
            self.cut_selection()
        elif action == "paste":
            self.paste_clipboard()
        elif action == "group":
            self._group(True)
        elif action == "ungroup":
            self._group(False)
        elif action == "import_image":
            self.import_image_as_element()
        elif action == "replace_image":
            self.replace_image(primary)
        elif action == "set_background":
            self.set_as_background(primary, bool(payload if payload is not None else True))
        elif action == "use_asset":
            name = self.asset_browser.current_name()
            if name:
                self._assign_asset(primary, name)
            else:
                self.log("[LAYOUT] pick an asset in the ASSETS tab first")
        elif action == "reset_crop":
            for widget in items:
                self.set_property(widget, "image.crop", [0, 0, 0, 0], history=False)
        elif action == "reset_rotation":
            for widget in items:
                self.set_property(widget, "rotation", 0.0, history=False)
        elif action == "apply_all_borders":
            self._apply_all_borders()
        elif action == "clear_borders":
            for widget in items:
                for side in ("top", "right", "bottom", "left"):
                    self.set_property(widget, f"borders.{side}.width", 0, history=False)
        elif action == "fill_parent":
            self._fill_parent()
        elif action == "center_in_parent":
            self._center_in_parent()
        elif action == "reparent":
            parent_widget = payload if widget_alive(payload) else self._target_widget()
            for widget in items:
                self._reparent(widget, parent_widget)
        elif action == "add_child":
            self._add_child(primary)
        elif action == "edit_script":
            self._show_script_tab()
        elif action == "save_script":
            self._save_code_tab("script")
        elif action == "run_script":
            self._run_script()
        elif action == "disable_script":
            self._disable_script()
        elif action == "view_code":
            self._show_code_tab()
        elif action == "apply_qss":
            editor = self.code_editors["qss"]
            for widget in items:
                self.set_property(widget, "stylesheet", editor.text())
        elif action == "clear_style":
            for widget in items:
                self.set_property(widget, "stylesheet", "")
        elif action == "apply_metadata":
            text = payload.toPlainText() if isinstance(payload, QPlainTextEdit) else "{}"
            for widget in items:
                self.set_property(widget, "custom", text)
        elif action == "copy_json":
            if primary is not None:
                spec = self.spec_for(primary)
                QApplication.clipboard().setText(json.dumps(spec or {}, indent=2, default=str))
                self.log("[LAYOUT] element JSON copied to the clipboard")
        elif action == "reset_element":
            self._reset_element(primary)
        elif action == "clear_state":
            state_name = getattr(self.properties, "_state_name", "hover")
            for widget in items:
                self.set_property(widget, f"states.{state_name}.enabled", False, history=False)
        elif action == "glow_preset":
            self._glow_preset(str(payload))
        elif action == "anim_play":
            self.animation_panel.play()
        elif action == "anim_stop":
            self.animation_panel.stop()
        elif action == "anim_stop_all":
            self.animation_panel.stop_all()
        elif action == "rotate_left":
            for widget in items:
                spec = self.spec_for(widget) or {}
                self.set_property(widget, "rotation",
                                  clamp_float(spec.get("rotation", 0.0) - 15.0, -360, 360, 0.0))
        elif action == "rotate_right":
            for widget in items:
                spec = self.spec_for(widget) or {}
                self.set_property(widget, "rotation",
                                  clamp_float(spec.get("rotation", 0.0) + 15.0, -360, 360, 0.0))
        else:
            self.log(f"[LAYOUT] unknown action '{action}'")

    def _set_flag(self, key: str, value: bool) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        before = [(w, self.capture_snapshot(w) or {}) for w in widgets]
        for widget in widgets:
            spec = self.spec_for(widget)
            if spec is not None:
                spec[key] = bool(value)
            try:
                if key == "hidden":
                    widget.setVisible(not bool(value))
                else:
                    widget.setProperty("_jarvis_layout_locked", bool(value))
                    if isinstance(widget, ElementWidget):
                        widget.setEnabled(not bool(value))
                widget.setProperty("_jarvis_layout_touched", True)
            except RuntimeError:
                continue
            if isinstance(widget, ElementWidget):
                widget.apply_spec(widget.spec)
        self._commit_batch(before, label=key)
        self._rebuild_explorer(self._target_widget())
        self._selection_changed()
        self.log(f"[LAYOUT] {key} = {value} on {len(widgets)} element(s)")

    def _z_order(self, mode: str) -> None:
        """Real stacking-order changes (Qt keeps children in bottom-to-top order)."""
        for widget in self.selection.items():
            if not widget_alive(widget):
                continue
            try:
                parent = widget.parentWidget()
                siblings = [c for c in (parent.children() if parent is not None else [])
                            if isinstance(c, QWidget) and c is not widget]
                if mode == "front":
                    widget.raise_()
                elif mode == "back":
                    widget.lower()
                elif mode == "forward":
                    index = siblings.index(widget) if False else None
                    _ = index
                    ordered = [c for c in (parent.children() if parent is not None else [])
                               if isinstance(c, QWidget)]
                    position = ordered.index(widget) if widget in ordered else -1
                    if 0 <= position < len(ordered) - 1:
                        ordered[position + 1].stackUnder(widget)
                elif mode == "backward":
                    ordered = [c for c in (parent.children() if parent is not None else [])
                               if isinstance(c, QWidget)]
                    position = ordered.index(widget) if widget in ordered else -1
                    if position > 0:
                        widget.stackUnder(ordered[position - 1])
                spec = self.spec_for(widget)
                if spec is not None:
                    spec["z"] = clamp_int(
                        {"front": 9999 - self.selection.count(), "back": -9999}.get(
                            mode, spec.get("z", 0) or 0), -100000, 100000, 0)
            except (RuntimeError, ValueError):
                continue
        self._persist_elements()
        self.viewport.mark_dirty()
        self._rebuild_explorer(self._target_widget())
        self._request_save()
        self._selection_changed()

    def _group(self, group: bool) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        if not widgets:
            return
        if group:
            group_name = f"Group_{len(self.element_names()) + 1}"
            for widget in widgets:
                spec = self.spec_for(widget)
                if spec is None:
                    continue
                previous = str(spec.get("group") or "")
                set_path(spec, "group", group_name)
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
                if previous:
                    continue
            self.log(f"[LAYOUT] grouped {len(widgets)} element(s) as {group_name}")
        else:
            for widget in widgets:
                spec = self.spec_for(widget)
                if spec is None:
                    continue
                set_path(spec, "group", "")
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
            self.log(f"[LAYOUT] ungrouped {len(widgets)} element(s)")
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    def _align(self, mode: str) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        if len(widgets) < 2:
            self.log("[LAYOUT] align needs at least two elements selected")
            return
        rects = [(w, w.geometry()) for w in widgets]
        before = [(w, self.capture_snapshot(w) or {}) for w in widgets]
        for widget, rect in AlignmentManager.align(rects, mode):
            self._set_geometry(widget, rect)
        self._commit_batch(before, label=f"align:{mode}")
        self.log(f"[LAYOUT] aligned {len(widgets)} element(s) · {mode}")

    def _distribute(self, axis: str) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        if len(widgets) < 3:
            self.log("[LAYOUT] distribute needs at least three elements")
            return
        rects = [(w, w.geometry()) for w in widgets]
        before = [(w, self.capture_snapshot(w) or {}) for w in widgets]
        for widget, rect in AlignmentManager.distribute(rects, axis):
            self._set_geometry(widget, rect)
        self._commit_batch(before, label=f"distribute:{axis}")
        self.log(f"[LAYOUT] distributed {len(widgets)} element(s) · {axis}")

    def _set_geometry(self, widget, rect: QRect) -> None:
        if not widget_alive(widget):
            return
        try:
            widget.setGeometry(rect)
        except RuntimeError:
            return
        widget.setProperty("_jarvis_layout_touched", True)
        spec = self.spec_for(widget)
        if spec is not None:
            spec["x"], spec["y"] = rect.x(), rect.y()
            spec["w"], spec["h"] = rect.width(), rect.height()
        self.viewport.mark_dirty()

    def _commit_batch(self, before: list[tuple[Any, dict]], label: str = "") -> None:
        entries = []
        for widget, past in before:
            if not widget_alive(widget):
                continue
            entries.append({"widget": widget, "before": past,
                            "after": self.capture_snapshot(widget) or {},
                            "kind": "element" if isinstance(widget, ElementWidget) else "widget"})
        self.history.push_many(entries, label=label)
        self._persist_elements()
        self._request_save()
        self._sync_geometry_fields()

    def _nudge(self, dx: int, dy: int, fine: bool = False) -> None:
        if self._editing_text():
            return
        step = 1 if fine else 10
        if self.snap_toggle.isChecked() and self.snap_mode.currentText() == "grid" and not fine:
            step = self._grid_size_value()
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        if not widgets:
            return
        before = [(w, self.capture_snapshot(w) or {}) for w in widgets]
        for widget in widgets:
            self._set_geometry(widget, widget.geometry().translated(dx * step, dy * step))
        self._commit_batch(before, label="nudge")

    def _fill_parent(self) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        before = [(w, self.capture_snapshot(w) or {}) for w in widgets]
        for widget in widgets:
            parent = widget.parentWidget()
            if not widget_alive(parent):
                continue
            try:
                widget.setGeometry(0, 0, parent.width(), parent.height())
            except RuntimeError:
                continue
            widget.setProperty("_jarvis_layout_touched", True)
            spec = self.spec_for(widget)
            if spec is not None:
                spec.update({"x": 0, "y": 0, "w": parent.width(), "h": parent.height()})
        self._commit_batch(before, label="fill_parent")

    def _center_in_parent(self) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        before = [(w, self.capture_snapshot(w) or {}) for w in widgets]
        for widget in widgets:
            parent = widget.parentWidget()
            if not widget_alive(parent):
                continue
            rect = QRect(0, 0, widget.width(), widget.height())
            rect.moveCenter(QRect(0, 0, parent.width(), parent.height()).center())
            self._set_geometry(widget, rect)
        self._commit_batch(before, label="center_in_parent")

    def _apply_all_borders(self) -> None:
        primary = self.selection.primary()
        spec = self.spec_for(primary) if widget_alive(primary) else None
        if spec is None:
            return
        top = (spec.get("borders") or {}).get("top") or {}
        merged = {"width": max(1, int(top.get("width", 1))),
                  "color": top.get("color", ACCENT),
                  "style": top.get("style", "Solid"),
                  "opacity": top.get("opacity", 100)}
        for side in ("top", "right", "bottom", "left"):
            for key, value in merged.items():
                self.set_property(primary, f"borders.{side}.{key}", value, history=False)
        self._request_save()

    def _glow_preset(self, name: str) -> None:
        presets = {
            "Cyan": {"enabled": True, "color": ACCENT, "strength": 70, "radius": 26, "opacity": 75, "spread": 2},
            "Blue": {"enabled": True, "color": "#3f7bff", "strength": 65, "radius": 24, "opacity": 70, "spread": 2},
            "Neon": {"enabled": True, "color": "#00ffd5", "strength": 90, "radius": 34, "opacity": 90, "spread": 5},
            "Off": {"enabled": False},
        }
        preset = presets.get(name)
        if preset is None:
            return
        for widget in self.selection.items():
            for key, value in preset.items():
                self.set_property(widget, f"glow.{key}", value, history=False)
        self.log(f"[STYLE] glow preset '{name}' applied")
        self._request_save()

    def _apply_preset(self, group: str, name: str) -> None:
        if group == "hud":
            preset = HUD_PRESETS.get(name)
            if preset is None:
                return
            for widget in self.selection.items():
                spec = self.spec_for(widget)
                if spec is None:
                    continue
                merged = apply_preset(spec, preset)
                spec.clear()
                spec.update(merged)
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
                else:
                    apply_runtime_style(widget, spec)
                    widget.setProperty("_jarvis_layout_touched", True)
            self.log(f"[STYLE] HUD preset '{name}' applied")
        elif group == "text":
            preset = TEXT_PRESETS.get(name)
            if preset is None:
                return
            for widget in self.selection.items():
                spec = self.spec_for(widget)
                if spec is None:
                    continue
                merged = apply_preset(spec.get("text_style") or {}, preset)
                spec["text_style"].clear()
                spec["text_style"].update(merged)
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
                else:
                    apply_runtime_style(widget, spec)
            self.log(f"[STYLE] text preset '{name}' applied")
        elif group == "stroke":
            presets = {
                "Glowing Panel": {"enabled": True, "color": ACCENT, "width": 2.0, "opacity": 90, "radius": 12},
                "Outlined Button": {"enabled": True, "color": ACCENT_BRIGHT, "width": 1.5, "opacity": 100, "radius": 8},
                "Neon Card": {"enabled": True, "color": "#00ffd5", "width": 2.5, "opacity": 100, "radius": 6},
            }
            preset = presets.get(name)
            if preset is None:
                return
            for widget in self.selection.items():
                for key, value in preset.items():
                    self.set_property(widget, f"stroke.{key}", value, history=False)
            self.log(f"[STYLE] stroke preset '{name}' applied")
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    # ---------------------------------------------------------- effect stack
    def _add_effect(self, kind: str) -> None:
        kind = str(kind or "").strip()
        if kind not in EFFECT_SCHEMA:
            return
        defaults = {key: spec[-1] for key, spec in EFFECT_SCHEMA[kind].items()}
        for widget in self.selection.items():
            spec = self.spec_for(widget)
            if spec is None:
                continue
            spec.setdefault("effects", []).append({"kind": kind, "enabled": True, "params": defaults})
            if isinstance(widget, ElementWidget):
                widget.apply_spec(spec)
            else:
                apply_runtime_style(widget, spec)
        self.log(f"[STYLE] effect added · {kind}")
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    def _remove_effect(self, index: int) -> None:
        for widget in self.selection.items():
            spec = self.spec_for(widget)
            if spec is None:
                continue
            effects = spec.get("effects") or []
            if 0 <= index < len(effects):
                effects.pop(index)
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
                else:
                    apply_runtime_style(widget, spec)
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    def _toggle_effect(self, index: int, enabled: bool) -> None:
        for widget in self.selection.items():
            spec = self.spec_for(widget)
            if spec is None:
                continue
            effects = spec.get("effects") or []
            if 0 <= index < len(effects):
                effects[index]["enabled"] = bool(enabled)
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
                else:
                    apply_runtime_style(widget, spec)
        self._persist_elements()
        self._request_save()

    def _move_effect(self, index: int, delta: int) -> None:
        for widget in self.selection.items():
            spec = self.spec_for(widget)
            if spec is None:
                continue
            effects = spec.get("effects") or []
            target = index + int(delta)
            if 0 <= index < len(effects) and 0 <= target < len(effects):
                effects[index], effects[target] = effects[target], effects[index]
                if isinstance(widget, ElementWidget):
                    widget.apply_spec(spec)
                else:
                    apply_runtime_style(widget, spec)
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    # ------------------------------------------------------------ elements
    def spec_of(self, spec: dict) -> dict:
        return normalize_element(spec, str(spec.get("name") or "Element"))

    def add_element(self, kind: str, parent_name: str | None = None,
                    spec_override: dict | None = None, select: bool = True):
        """Create a real element (custom element engine + app persistence)."""
        override = copy.deepcopy(spec_override) if isinstance(spec_override, dict) else {}
        if str(override.get("type") or override.get("kind") or "") in ELEMENT_TYPES:
            kind = str(override.get("type") or override.get("kind"))
        kind = kind if kind in ELEMENT_TYPES else "Panel"
        # A pasted/duplicated element keeps the unique name the clipboard chose.
        requested = str(override.get("name") or "").strip()
        name = self._unique_name(requested) if requested else self._new_element_name(kind)
        parent_name = parent_name or self._current_target_name or "Main Surface"
        spec = default_spec(name, kind)
        spec["parent"] = parent_name
        if override:
            spec.update(override)
            spec["name"] = name
        spec["z"] = self._next_z()
        if kind in ("Separator", "Spacer"):
            spec["background"]["color"] = ACCENT
            spec["background"]["opacity"] = 90
        created = None
        document_before = self._document_snapshot()
        if self.main is not None and hasattr(self.main, "_layout_elements"):
            self.main._layout_elements[name] = spec
            try:
                self.main._apply_layout_elements()
                if hasattr(self.main, "_save_layout_elements"):
                    self.main._save_layout_elements()
            except Exception as exc:
                self.log(f"[ERROR] create failed — {exc}")
                QMessageBox.critical(self, "Add Element", f"Could not create {kind}: {exc}")
                return None
            created = (getattr(self.main, "_layout_widgets", {}) or {}).get(name)
        else:
            created = self._standalone_create(spec)
        if widget_alive(created):
            created.setProperty("_jarvis_layout_custom", True)
            try:
                created._layout_element_name = name
            except Exception:
                pass
            if not isinstance(created, ElementWidget):
                # Runtime-hosting host without the engine: fall back to a real
                # ElementWidget parented to the target so the element still works.
                created = self._standalone_create(spec)
        # The target list only changes when panels appear or disappear, so it is
        # refreshed on show() and on demand -- never per created element (that
        # used to rebuild the whole Inspector twice per add).
        self._rebuild_explorer(self._target_widget())
        if select and widget_alive(created):
            self.select_widget(created)
        self.history.push_document(document_before, self._document_snapshot(), label=f"add:{name}")
        self.log(f"[LAYOUT] Created {name} ({kind})")
        self.statusBar().showMessage(f"ADDED · {name}")
        self._request_save()
        return created

    def _standalone_create(self, spec: dict) -> ElementWidget | None:
        """Fallback used when the host has no ``_layout_elements`` contract."""
        parent = self._parent_widget_for(str(spec.get("parent") or ""))
        if not widget_alive(parent):
            return None
        widget = ElementWidget(copy.deepcopy(spec), parent)
        widget.setProperty("_jarvis_layout_custom", True)
        widget.geometryCommitted.connect(self._viewport_drag_finished)
        widget.show()
        return widget

    def _parent_widget_for(self, parent_name: str) -> QWidget | None:
        for name, widget in _root_candidates(self.main) if isinstance(self.main, QWidget) else []:
            if name == parent_name and widget_alive(widget):
                return widget
        return self._target_widget()

    def _next_z(self) -> int:
        highest = 0
        if self.main is not None:
            for spec in (getattr(self.main, "_layout_elements", {}) or {}).values():
                highest = max(highest, int(spec.get("z", 0) or 0))
        return highest + 1

    def _new_element_name(self, kind: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_ ]+", "", str(kind or "Element")).strip().replace(" ", "_") or "Element"
        return self._unique_name(base, suffix_from=2)

    def _unique_name(self, base: str, suffix_from: int = 2) -> str:
        """Never overwrite an existing element (TXT section 45)."""
        clean = re.sub(r"[^A-Za-z0-9_ ]+", "_", str(base or "Element")).strip() or "Element"
        existing = set(self.element_names())
        if clean not in existing:
            return clean
        index = max(1, int(suffix_from))
        while f"{clean}_{index}" in existing:
            index += 1
        return f"{clean}_{index}"

    def remove_widget(self, widget, history: bool = True) -> None:
        if not widget_alive(widget):
            return
        name = self._element_name_of(widget)
        if not name:
            # Runtime widget: hide instead of destroying something the app owns.
            try:
                widget.setVisible(False)
                widget.setProperty("_jarvis_layout_touched", True)
            except RuntimeError:
                return
            self.log(f"[LAYOUT] hid runtime widget {self._friendly(widget)}")
            self._request_save()
            return
        self.animations.stop(widget, restore=False)
        self.selection.remove(widget)
        document_before = self._document_snapshot()
        if self.main is not None and hasattr(self.main, "_layout_elements"):
            self.main._layout_elements.pop(name, None)
            try:
                self.main._apply_layout_elements()
            except Exception as exc:
                self.log(f"[ERROR] delete failed — {exc}")
                return
        else:
            try:
                widget.setParent(None)
                widget.deleteLater()
            except RuntimeError:
                pass
        self.history.push_document(document_before, self._document_snapshot(), label=f"delete:{name}")
        self.log(f"[LAYOUT] Deleted {name}")
        self._request_save()

    def _element_name_of(self, widget) -> str:
        if isinstance(widget, ElementWidget):
            return str(widget.element_name() or "")
        name = str(getattr(widget, "_layout_element_name", "") or "")
        if name and self.main is not None and name in (getattr(self.main, "_layout_elements", {}) or {}):
            return name
        return ""

    def delete_selected_element(self) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        if not widgets:
            return
        custom = [w for w in widgets if self._element_name_of(w)]
        runtime = [w for w in widgets if not self._element_name_of(w)]
        for widget in custom:
            self.remove_widget(widget, history=True)
        if runtime:
            answer = QMessageBox.question(
                self, "DELETE RUNTIME WIDGET?",
                f"{len(runtime)} selected item(s) belong to the live J.A.R.V.I.S. interface and "
                f"cannot be deleted safely.\n\nHide them instead?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Yes:
                for widget in runtime:
                    try:
                        widget.setVisible(False)
                        widget.setProperty("_jarvis_layout_touched", True)
                    except RuntimeError:
                        continue
                self.log(f"[LAYOUT] hid {len(runtime)} runtime widget(s)")
                self._request_save()
        self.set_selection([])
        self._rebuild_explorer(self._target_widget())
        if self._target_widget() is not None:
            self.select_widget(self._target_widget())

    # --------------------------------------------------------- clipboard
    def copy_selection(self) -> None:
        count = self.clipboard.copy(self.selection.items())
        self.log(f"[LAYOUT] copied {count} element(s)" if count else "[LAYOUT] nothing selected to copy")
        self.statusBar().showMessage(f"COPIED · {count}" if count else "COPY · nothing selected")

    def cut_selection(self) -> None:
        count = self.clipboard.copy(self.selection.items())
        if not count:
            return
        for widget in list(self.selection.items()):
            if self._element_name_of(widget):
                self.remove_widget(widget, history=True)
        self.log(f"[LAYOUT] cut {count} element(s)")
        self._rebuild_explorer(self._target_widget())

    def paste_clipboard(self) -> None:
        if not self.clipboard.has_payload():
            self.log("[LAYOUT] clipboard is empty")
            return
        payload = self.clipboard.paste(self._current_target_name or "")
        created: list = []
        for spec in payload:
            widget = self.add_element(str(spec.get("type") or "Panel"),
                                      parent_name=str(spec.get("parent") or self._current_target_name),
                                      spec_override=spec, select=False)
            if widget_alive(widget):
                created.append(widget)
        if created:
            self.set_selection(created, primary=created[-1])
        self.log(f"[LAYOUT] pasted {len(created)} element(s)")

    def duplicate_selection(self) -> None:
        payload = self.clipboard.duplicate(self.selection.items())
        if not payload:
            self.log("[LAYOUT] nothing selected to duplicate")
            return
        created: list = []
        for spec in payload:
            widget = self.add_element(str(spec.get("type") or "Panel"),
                                      parent_name=str(spec.get("parent") or self._current_target_name),
                                      spec_override=spec, select=False)
            if widget_alive(widget):
                created.append(widget)
                self.log(f"[LAYOUT] Duplicated {spec.get('name')}")
        if created:
            self.set_selection(created, primary=created[-1])

    # -------------------------------------------------------------- images
    def import_image_as_element(self) -> None:
        paths, _ = QFileDialog.getOpenFileName(
            self, "Import image as element", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff *.svg);;All Files (*.*)")
        if not paths:
            return
        self.import_image_files([paths])

    def import_image_files(self, paths: list[str], drop_point=None) -> None:
        """Every dropped/imported image becomes a real element (TXT sections 2, 48)."""
        created: list = []
        offset = 0
        for raw in paths:
            path = Path(str(raw))
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                self.log(f"[WARN] unsupported image type ignored — {path.name}")
                continue
            if not path.is_file():
                self.log(f"[ERROR] image not found — {path}")
                continue
            asset_name = self.assets.register(path, copy_into=True)
            spec = default_spec(self._new_element_name("Image"), "Image")
            spec["image"]["path"] = str(path)
            spec["image"]["asset"] = asset_name
            spec["parent"] = self._current_target_name or "Main Surface"
            size = IMAGE_CACHE.info(path)
            if size.width() > 0 and size.height() > 0:
                max_w, max_h = 420, 320
                scale = min(1.0, max_w / size.width(), max_h / size.height())
                spec["w"] = max(24, int(size.width() * scale))
                spec["h"] = max(24, int(size.height() * scale))
            if drop_point is not None:
                spec["x"] = max(0, int(drop_point.x()) - spec["w"] // 2 + offset)
                spec["y"] = max(0, int(drop_point.y()) - spec["h"] // 2 + offset)
            else:
                spec["x"] = 24 + offset
                spec["y"] = 24 + offset
            offset += 18
            widget = self.add_element("Image", parent_name=spec["parent"], spec_override=spec, select=False)
            if widget_alive(widget):
                created.append(widget)
        self._refresh_assets()
        if created:
            self.set_selection(created, primary=created[-1])
            self.log(f"[LAYOUT] imported {len(created)} image element(s)")
            self.statusBar().showMessage(f"IMPORTED · {len(created)} image(s)")

    def import_assets(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import image assets", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff *.svg);;All Files (*.*)")
        if not paths:
            return
        for raw in paths:
            path = Path(str(raw))
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                name = self.assets.register(path, copy_into=True)
                self.log(f"[LAYOUT] asset imported · {name}")
            else:
                self.log(f"[WARN] skipped non-image {path.name}")
        self._refresh_assets()
        self._request_save()

    def _refresh_assets(self) -> None:
        try:
            self.asset_browser.refresh(self.assets, IMAGE_CACHE)
        except Exception as exc:
            self.log(f"[WARN] asset browser refresh failed — {exc}")

    def _asset_command(self, payload: str) -> None:
        text = str(payload or "")
        if text.startswith("__rename__:"):
            _, old, new = text.split(":", 2)
            final = self.assets.rename(old, new)
            self.log(f"[LAYOUT] asset renamed · {old} → {final}")
        elif text.startswith("__copypath__:"):
            name = text.split(":", 1)[1]
            path = self.assets.path_for(name)
            QApplication.clipboard().setText(path)
            self.log(f"[LAYOUT] asset path copied · {path}")
        elif text.startswith("__openlocation__:"):
            name = text.split(":", 1)[1]
            self._open_location(self.assets.path_for(name))
        else:
            primary = self.selection.primary()
            if widget_alive(primary):
                self._assign_asset(primary, text)
                return
            self._asset_selection = text
            self.log(f"[LAYOUT] asset selected · {text}")
            return
        self._refresh_assets()
        self._request_save()

    def _assign_asset(self, widget, name: str) -> None:
        path = self.assets.path_for(name)
        if not widget_alive(widget) or not path:
            self.log("[ERROR] asset could not be used — it has no file")
            return
        spec = self.spec_for(widget)
        if spec is None:
            return
        spec.setdefault("image", {})
        spec["image"]["path"] = path
        spec["image"]["asset"] = name
        if isinstance(widget, ElementWidget):
            widget.apply_spec(spec)
        else:
            self._set_image_on_widget(widget, path)
        self.log(f"[LAYOUT] asset {name} applied to {self._friendly(widget)}")
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    def _replace_asset(self, name: str) -> None:
        paths, _ = QFileDialog.getOpenFileName(
            self, f"Replace asset {name}", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff *.svg);;All Files (*.*)")
        if not paths:
            return
        old_path = self.assets.path_for(name)
        final = self.assets.replace(name, paths, copy_into=True)
        self.log(f"[LAYOUT] asset replaced · {name} → {os.path.basename(str(paths))}")
        for widget in self.selection.items():
            spec = self.spec_for(widget)
            if spec and str(get_path(spec, "image.path", "")) == old_path:
                self._assign_asset(widget, final)
        self._refresh_assets()
        self._request_save()

    def _delete_asset(self, name: str) -> None:
        unused = self.assets.unused((getattr(self.main, "_layout_elements", {}) or {}).values())
        if name not in unused:
            answer = QMessageBox.question(
                self, "Delete asset",
                f"'{name}' is still referenced by an element.\n\nDelete it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.assets.remove(name, delete_file=True)
        self.log(f"[LAYOUT] asset deleted · {name}")
        self._refresh_assets()
        self._request_save()

    def _preview_asset(self, name: str) -> None:
        path = self.assets.path_for(name)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Asset preview · {name}")
        dialog.resize(760, 600)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = IMAGE_CACHE.pixmap(path)
        if pix is not None:
            label.setPixmap(pix.scaled(700, 520, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
        else:
            label.setText(IMAGE_CACHE.decode_failures.get(str(path), "preview unavailable"))
        label.setStyleSheet(f"color:{TEXT_DIM};background:{BG_DEEP};")
        layout.addWidget(label, 1)
        layout.addWidget(QLabel(str(path)))
        dialog.setStyleSheet(_composed_app_css() + f"QDialog{{background:{BG};}}")
        dialog.exec()

    @staticmethod
    def _open_location(path: str) -> None:
        try:
            target = Path(str(path))
            if target.is_file():
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", "/select,", str(target)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target.parent)])
            elif target.exists():
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", str(target)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
        except Exception:
            pass

    def _set_image_on_widget(self, widget, path: str) -> None:
        """Runtime-widget image support: real pixmap on a QLabel, else log."""
        if not widget_alive(widget):
            return
        setter = getattr(widget, "setPixmap", None)
        if callable(setter):
            pix = IMAGE_CACHE.pixmap(path)
            if pix is not None:
                try:
                    setter(pix)
                    widget.setProperty("_jarvis_layout_touched", True)
                    self.log(f"[LAYOUT] image applied to runtime widget {self._friendly(widget)}")
                    return
                except RuntimeError:
                    return
        for name in ("setIcon",):
            setter = getattr(widget, name, None)
            if callable(setter):
                pix = IMAGE_CACHE.pixmap(path)
                if pix is not None:
                    try:
                        from PyQt6.QtGui import QIcon
                        setter(QIcon(pix))
                        widget.setProperty("_jarvis_layout_touched", True)
                        return
                    except Exception:
                        break
        self.log("[WARN] this runtime widget has no image surface — use a custom element instead")

    def replace_image(self, widget=None) -> None:
        widget = widget if widget_alive(widget) else self.selection.primary()
        if not widget_alive(widget):
            return
        paths, _ = QFileDialog.getOpenFileName(
            self, "Replace image", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff *.svg);;All Files (*.*)")
        if not paths:
            return
        spec = self.spec_for(widget)
        if spec is None:
            return
        asset_name = self.assets.register(paths, copy_into=True)
        spec.setdefault("image", {})
        spec["image"]["path"] = str(paths)
        spec["image"]["asset"] = asset_name
        if isinstance(widget, ElementWidget):
            widget.replace_image(paths, asset_name)
        else:
            self._set_image_on_widget(widget, str(paths))
        self.log(f"[LAYOUT] image replaced on {self._friendly(widget)} (position, size, "
                 f"effects and z-order kept)")
        self._persist_elements()
        self._refresh_assets()
        self._request_save()
        self._selection_changed()

    def set_as_background(self, widget=None, background: bool = True) -> None:
        widgets = [w for w in self.selection.items() if widget_alive(w)]
        if widget_alive(widget) and widget not in widgets:
            widgets.append(widget)
        if not widgets:
            return
        for target in widgets:
            spec = self.spec_for(target)
            if spec is None:
                continue
            spec.setdefault("image", {})
            if not str(spec["image"].get("path") or ""):
                self.log("[WARN] select an element with an image first")
                return
            spec["image"]["is_background"] = bool(background)
            if background:
                parent = target.parentWidget()
                if widget_alive(parent):
                    spec["x"], spec["y"] = 0, 0
                    spec["w"], spec["h"] = parent.width(), parent.height()
            if isinstance(target, ElementWidget):
                target.apply_spec(spec)
            else:
                apply_runtime_style(target, spec)
            try:
                if background:
                    target.lower()
                else:
                    target.raise_()
            except RuntimeError:
                pass
        self.log(f"[LAYOUT] image set as {'background' if background else 'foreground'}")
        self._persist_elements()
        self._request_save()
        self._selection_changed()

    # ---------------------------------------------------------------- code
    def _show_tab(self, index: int) -> None:
        self.right_tabs.setCurrentIndex(int(index))

    def _show_code_tab(self) -> None:
        self._ensure_code_synced()
        self._show_tab(1)

    def _ensure_code_synced(self) -> None:
        """VIEW CODE must describe the element that is selected *right now*.

        The inspector (Properties + CODE) fills in on the next event-loop turn so
        selection stays instant, but an explicit "view code" click is the one
        moment where waiting would show the previous element's code.  Sync here
        if the editors are still attached to a different widget.
        """
        primary = self.selection.primary()
        if not widget_alive(primary):
            self._clear_code_editors()
            self.code_target.setText("no element")
            return
        if getattr(self, "_code_widget", None) is primary:
            return
        spec = self.spec_for(primary)
        if spec is None:
            self._clear_code_editors()
            self.code_target.setText("no element")
            return
        self._sync_code_editors(primary, spec)

    def _open_code_for(self, widget) -> None:
        self.select_widget(widget)
        self._show_code_tab()

    def _open_script_for(self, widget) -> None:
        self.select_widget(widget)
        self._ensure_code_synced()
        self._show_tab(1)
        self.code_tabs.setCurrentWidget(self.code_editors["script"])
        self.code_editors["script"].editor.setFocus()

    def _show_script_tab(self) -> None:
        primary = self.selection.primary()
        if widget_alive(primary) and not isinstance(primary, ElementWidget):
            self.log("[WARN] scripts attach to custom elements — this is a runtime widget")
        self._ensure_code_synced()
        self._show_tab(1)
        self.code_tabs.setCurrentWidget(self.code_editors["script"])
        self.code_editors["script"].editor.setFocus()

    def _visible_code_tabs(self, widget) -> list[str]:
        tabs = ["qss", "json"]
        if isinstance(widget, ElementWidget):
            tabs = ["qss", "python", "script"]
            if str(widget.element_type()) in ("Uiverse", "WebView"):
                tabs += ["html", "css"]
            tabs.append("json")
        return tabs

    def _update_code_tab_visibility(self, widget) -> None:
        visible = set(self._visible_code_tabs(widget))
        mapping = {"qss": self.code_editors["qss"], "python": self.code_editors["python"],
                   "script": self.code_editors["script"], "html": self.code_editors["html"],
                   "css": self.code_editors["css"], "json": self.code_editors["json"]}
        for key, editor in mapping.items():
            index = self.code_tabs.indexOf(editor)
            if index < 0:
                continue
            self.code_tabs.setTabVisible(index, key in visible)

    def _code_tab_changed(self, _index: int) -> None:
        current = self.code_tabs.currentWidget()
        for editor in self.code_editors.values():
            if editor is current:
                editor.ensure_highlighted()

    def _sync_code_editors(self, widget, spec: dict | None) -> None:
        if not widget_alive(widget) or spec is None:
            self._clear_code_editors()
            return
        self._code_widget = widget
        self._loading_code = True
        try:
            self._update_code_tab_visibility(widget)
            current = self.code_tabs.currentWidget()
            payload = {
                "qss": (str(spec.get("stylesheet") or ""), "qss"),
                "script": (str(get_path(spec, "script.code", "") or ""), "python"),
                "html": (str(spec.get("html") or ""), "html"),
                "css": (str(spec.get("css") or ""), "css"),
                "json": (json.dumps(spec, indent=2, default=str), "json"),
                "python": (self.generate_element_code(widget, spec), "python"),
            }
            for key, (text, language) in payload.items():
                editor = self.code_editors[key]
                # Only the visible tab pays for syntax highlighting; the others
                # colour themselves when shown.
                editor.set_text(text, language, highlight=(editor is current))
            self.code_editors["python"].editor.setReadOnly(True)
            self.code_target.setText(
                f"{self._friendly(widget)} · {class_simple_name(widget)}")
        finally:
            self._loading_code = False

    def _clear_code_editors(self) -> None:
        self._code_widget = None
        self._loading_code = True
        try:
            for editor in self.code_editors.values():
                editor.set_text("", editor.language, highlight=False)
        finally:
            self._loading_code = False

    def _code_dirty(self, key: str) -> None:
        if getattr(self, "_loading_code", False):
            return
        self._set_save_state("UNSAVED")
        if key == "script":
            self._set_status_hint("SCRIPT EDITED · press SAVE SCRIPT (Ctrl+S)")

    def _set_status_hint(self, text: str) -> None:
        self.statusBar().showMessage(str(text))

    def generate_element_code(self, widget, spec: dict) -> str:
        """VIEW CODE: the real implementation of the selected element."""
        lines = [
            "# J.A.R.V.I.S. Layout Studio — element implementation",
            f"# element      : {spec.get('name')}",
            f"# type         : {spec.get('type')}",
            f"# parent       : {spec.get('parent') or '(target panel)'}",
            f"# geometry     : x={spec.get('x')} y={spec.get('y')} w={spec.get('w')} h={spec.get('h')}",
            f"# z-order      : {spec.get('z')}",
            f"# locked       : {bool(spec.get('locked'))}   hidden: {bool(spec.get('hidden'))}",
            "",
        ]
        if isinstance(widget, ElementWidget):
            lines += [
                "from ui_layout_core import ElementWidget, normalize_element",
                "",
                f"spec = normalize_element({json.dumps(spec, indent=4, default=str)}, "
                f"{spec.get('name')!r})",
                "widget = ElementWidget(spec, parent)      # paints image, stroke, glow, gradient…",
                "widget.show()",
                "",
                "# what the engine is drawing right now:",
                f"#   background  : {get_path(spec, 'background.color')} "
                f"@ {get_path(spec, 'background.opacity')}% "
                f"(gradient={bool(get_path(spec, 'background.gradient.enabled'))})",
                f"#   image       : {get_path(spec, 'image.path') or '(none)'} "
                f"fit={get_path(spec, 'image.fit')} background={bool(get_path(spec, 'image.is_background'))}",
                f"#   stroke      : {bool(get_path(spec, 'stroke.enabled'))} "
                f"{get_path(spec, 'stroke.width')}px {get_path(spec, 'stroke.color')}",
                f"#   glow        : {bool(get_path(spec, 'glow.enabled'))} "
                f"radius={get_path(spec, 'glow.radius')}",
                f"#   shadow      : {bool(get_path(spec, 'shadow.enabled'))} "
                f"blur={get_path(spec, 'shadow.blur')}",
                f"#   text stroke : {bool(get_path(spec, 'text_style.stroke.enabled'))} "
                f"{get_path(spec, 'text_style.stroke.width')}px",
                f"#   effects     : {[e.get('kind') for e in (spec.get('effects') or [])]}",
                f"#   animation   : {get_path(spec, 'animation.name')}",
                f"#   rotation    : {spec.get('rotation')}°",
            ]
            script = str(get_path(spec, "script.code", "") or "")
            lines.append("")
            lines.append("# ---- element script ----" if script else "# no script attached")
            if script:
                lines += [f"# enabled: {bool(get_path(spec, 'script.enabled'))}"] + script.splitlines()
        else:
            lines += [
                "# Runtime J.A.R.V.I.S. widget — styled through QSS + graphics effects.",
                f"widget.setGeometry({spec.get('x')}, {spec.get('y')}, "
                f"{spec.get('w')}, {spec.get('h')})",
                f"widget.setVisible({bool(spec.get('visible'))})",
                f"widget.setEnabled({bool(spec.get('enabled'))})",
                "",
                "# generated QSS (exactly what the Studio applied):",
            ] + [f"# {line}" for line in build_qss(spec).splitlines()]
            lines += [
                "",
                "# Qt allows ONE graphics effect per widget:",
                f"#   glow   : {bool(get_path(spec, 'glow.enabled'))}",
                f"#   shadow : {bool(get_path(spec, 'shadow.enabled'))}  (glow wins the effect slot)",
                "# text outline / rotation / painted stroke need the custom element engine, so they",
                "# are only offered for elements you create in the Studio.",
            ]
        return "\n".join(lines)

    def apply_code(self) -> None:
        widget = self.selection.primary()
        if not widget_alive(widget):
            return
        current = self.code_tabs.currentWidget()
        key = next((name for name, editor in self.code_editors.items() if editor is current), "")
        if not key:
            return
        self._apply_code_tab(key, widget)

    def _apply_code_tab(self, key: str, widget) -> None:
        editor = self.code_editors.get(key)
        if editor is None:
            return
        text = editor.text()
        spec = self.spec_for(widget)
        if spec is None:
            return
        if key == "qss":
            self.set_property(widget, "stylesheet", text)
            self.log("[STYLE] QSS applied live")
        elif key == "json":
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError) as exc:
                self.log(f"[ERROR] JSON tab is not valid — {exc}")
                return
            if not isinstance(parsed, dict):
                self.log("[ERROR] JSON tab must contain an object")
                return
            merged = normalize_element(parsed, str(spec.get("name") or "Element"))
            before = self.capture_snapshot(widget)
            spec.clear()
            spec.update(merged)
            if isinstance(widget, ElementWidget):
                widget.apply_spec(spec)
            else:
                apply_runtime_style(widget, spec)
            self.history.push(widget, before or {}, self.capture_snapshot(widget) or {},
                              "element" if isinstance(widget, ElementWidget) else "widget",
                              label="json")
            self._persist_elements()
            self._request_save()
            self._selection_changed()
        elif key == "script":
            self._save_code_tab("script")
        elif key in ("html", "css"):
            set_path(spec, key, text)
            if isinstance(widget, ElementWidget):
                widget.apply_spec(spec)
            self.log(f"[STYLE] {key.upper()} stored on the element (used by Uiverse/WebView elements)")
            self._persist_elements()
            self._request_save()
        elif key == "python":
            self.log("[LAYOUT] PYTHON tab is a read-only view of the element implementation")

    def _save_code_tab(self, key: str) -> None:
        widget = self.selection.primary()
        if key != "script":
            if key in ("qss", "json", "html", "css"):
                if widget_alive(widget):
                    self._apply_code_tab(key, widget)
            return
        if not widget_alive(widget):
            return
        editor = self.code_editors["script"]
        code = editor.text()
        ok, message, line = self.scripts.validate(code)
        if not ok:
            editor.set_error_line(line)
            self.log(f"[ERROR] script rejected — {message}")
            QMessageBox.warning(self, "Script validation", f"The script was not saved:\n\n{message}")
            return
        editor.set_error_line(0)
        spec = self.spec_for(widget)
        if spec is None:
            return
        before = self.capture_snapshot(widget)
        set_path(spec, "script.code", code)
        set_path(spec, "script.enabled", True)
        self._attach_script(widget)
        self.history.push(widget, before or {}, self.capture_snapshot(widget) or {},
                          "element" if isinstance(widget, ElementWidget) else "widget", label="script")
        self._persist_elements()
        self._request_save()
        self.log(f"[SCRIPT] saved on {self._friendly(widget)} ({len(code.splitlines())} line(s))")
        self.statusBar().showMessage("SCRIPT SAVED")

    def _attach_script(self, widget) -> None:
        if not isinstance(widget, ElementWidget) or not widget_alive(widget):
            return
        enabled = as_bool(get_path(widget.spec, "script.enabled", False))
        code = str(get_path(widget.spec, "script.code", "") or "")
        if enabled and code.strip():
            widget.script_runner = self.scripts.make_click_runner(code)
        else:
            widget.script_runner = None

    def _run_script(self) -> None:
        widget = self.selection.primary()
        if not widget_alive(widget):
            return
        editor = self.code_editors["script"]
        code = editor.text() or str(get_path(self.spec_for(widget) or {}, "script.code", "") or "")
        self._show_tab(1)
        self.code_tabs.setCurrentWidget(editor)
        ok, message = self.scripts.run(widget, code, "test")
        if ok:
            editor.set_error_line(0)
            self.viewport.mark_dirty()
            self.statusBar().showMessage("SCRIPT OK")
        else:
            _ok, _msg, line = self.scripts.validate(code)
            editor.set_error_line(line)
            self.statusBar().showMessage("SCRIPT ERROR · see OUTPUT")

    def _disable_script(self) -> None:
        for widget in self.selection.items():
            spec = self.spec_for(widget)
            if spec is None:
                continue
            before = self.capture_snapshot(widget)
            set_path(spec, "script.enabled", False)
            if isinstance(widget, ElementWidget):
                widget.script_runner = None
            self.history.push(widget, before or {}, self.capture_snapshot(widget) or {},
                              "element" if isinstance(widget, ElementWidget) else "widget",
                              label="script-disable")
        self._persist_elements()
        self._request_save()
        self.log("[SCRIPT] script disabled — it will not run again")
        self._selection_changed()

    def open_uiverse_import(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("J.A.R.V.I.S. · Uiverse Import")
        dlg.resize(860, 620)
        layout = QVBoxLayout(dlg)
        tip = QLabel("Paste the HTML and CSS from a Uiverse element (or any Qt-WebEngine snippet). "
                     "It becomes a real WebView-backed layout element that you can style, "
                     "position, script and save.")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{TEXT_DIM};background:transparent;")
        layout.addWidget(tip)
        tabs = QTabWidget()
        html = QPlainTextEdit()
        html.setPlaceholderText("<paste HTML here>")
        css = QPlainTextEdit()
        css.setPlaceholderText("/* paste CSS here */")
        for widget in (html, css):
            widget.setStyleSheet(
                f"QPlainTextEdit{{background:{BG_DEEP};color:{TEXT};"
                f"border:1px solid {BORDER_FAINT};padding:7px;font:8pt {FONT_LOG};}}")
        tabs.addTab(html, "HTML")
        tabs.addTab(css, "CSS")
        layout.addWidget(tabs, 1)
        row = QHBoxLayout()
        name_edit = QLineEdit("Uiverse Element")
        row.addWidget(name_edit, 1)
        add_button = QPushButton("ADD TO CURRENT PANEL")
        add_button.setStyleSheet(self._mini_style())
        row.addWidget(add_button)
        layout.addLayout(row)
        dlg.setStyleSheet(_composed_app_css() + f"QDialog{{background:{BG};}}")

        def _add():
            spec = default_spec(name_edit.text().strip() or "Uiverse Element", "Uiverse")
            spec["parent"] = self._current_target_name or "Main Surface"
            spec["x"], spec["y"] = 24, 24
            spec["html"] = html.toPlainText()
            spec["css"] = css.toPlainText()
            self.add_element("Uiverse", parent_name=spec["parent"], spec_override=spec)
            dlg.accept()

        add_button.clicked.connect(_add)
        dlg.exec()

    # --------------------------------------------------------------- saving
    def _persist_elements(self) -> None:
        """Write custom-element specs back into MainWindow's element store."""
        if self.main is None:
            return
        elements = getattr(self.main, "_layout_elements", None)
        if not isinstance(elements, dict):
            return
        for name, widget in list(getattr(self.main, "_layout_widgets", {}).items()):
            if isinstance(widget, ElementWidget) and widget_alive(widget):
                try:
                    elements[name] = widget.element_json()
                except Exception:
                    continue

    def _request_save(self) -> None:
        if not self._live:
            self._set_save_state("UNSAVED")
            return
        self._set_save_state("UNSAVED")
        self._save_timer.start()

    def _flush_save(self) -> None:
        main = self.main
        self._set_save_state("SAVING")
        try:
            if main is not None and hasattr(main, "_save_layout_elements"):
                main._save_layout_elements()
            overrides = capture_widget_overrides(main) if isinstance(main, QWidget) else {}
            payload = {
                "widgets": overrides,
                "assets": self.assets.to_json(),
                "active_panel": self._current_target_name or "Main Surface",
            }
            save_layout(self.layout_path, payload)
            if main is not None and hasattr(main, "_layout_widget_overrides"):
                main._layout_widget_overrides = overrides
            self._set_save_state("SAVED")
        except Exception as exc:
            self._set_save_state("SAVE FAILED")
            self.log(f"[ERROR] save failed — {exc}")

    def _set_save_state(self, state: str) -> None:
        self._save_state = state
        colour = {"SAVED": TEXT_DIM, "UNSAVED": "#ffaa22",
                  "SAVING": ACCENT_BRIGHT, "SAVE FAILED": "#ff4455"}.get(state, TEXT_DIM)
        self.save_state_label.setText(state)
        self.save_state_label.setStyleSheet(
            f"color:{colour};background:transparent;font:700 7pt {FONT_SMALL};"
            f"letter-spacing:1.2px;")

    def save_all(self) -> None:
        self._save_timer.stop()
        self._flush_save()
        if self._save_state == "SAVED":
            self.log(f"[LAYOUT] saved {self.layout_path}")
            self.statusBar().showMessage(f"SAVED · {self.layout_path}")
        self._persist_elements()

    def _load_assets(self) -> None:
        try:
            data = load_layout(self.layout_path)
            self.assets.load(data.get("assets"))
            self._refresh_assets()
        except Exception as exc:
            self.log(f"[WARN] asset registry load failed — {exc}")

    # -------------------------------------------------------------- history
    def undo(self) -> None:
        self._drag_origins = {}
        if not self.history.can_undo():
            self.log("[LAYOUT] nothing to undo")
            return
        if self.history.undo():
            self.log("[LAYOUT] undo")
            self._rebuild_explorer(self._target_widget())

    def redo(self) -> None:
        if not self.history.can_redo():
            self.log("[LAYOUT] nothing to redo")
            return
        if self.history.redo():
            self.log("[LAYOUT] redo")
            self._rebuild_explorer(self._target_widget())

    # ----------------------------------------------------------- parenting
    def _reparent(self, widget, parent_widget) -> None:
        if not widget_alive(widget) or not widget_alive(parent_widget):
            return
        if widget is parent_widget:
            return
        spec = self.spec_for(widget)
        if spec is None:
            return
        parent_name = next((name for name, candidate in _root_candidates(self.main)
                            if candidate is parent_widget), "") if isinstance(self.main, QWidget) else ""
        before = self.capture_snapshot(widget)
        try:
            top_left = widget.mapTo(parent_widget, QPoint(0, 0))
        except Exception:
            top_left = QPoint(widget.x(), widget.y())
        try:
            widget.setParent(parent_widget)
            widget.move(top_left)
            widget.setProperty("_jarvis_layout_touched", True)
        except RuntimeError:
            return
        if isinstance(widget, ElementWidget):
            widget.spec["parent"] = parent_name or str(spec.get("parent") or "")
            widget.spec["x"], widget.spec["y"] = top_left.x(), top_left.y()
            widget.apply_spec(widget.spec)
        else:
            spec["parent"] = parent_name
            spec["x"], spec["y"] = top_left.x(), top_left.y()
        self.history.push(widget, before or {}, self.capture_snapshot(widget) or {},
                          "element" if isinstance(widget, ElementWidget) else "widget",
                          label="reparent")
        self._persist_elements()
        self._request_save()
        self._rebuild_explorer(self._target_widget())
        self.log(f"[LAYOUT] re-parented {self._friendly(widget)} → "
                 f"{self._friendly(parent_widget)} (undoable)")

    def _add_child(self, parent_widget) -> None:
        if not widget_alive(parent_widget):
            return
        name = self._new_element_name("Panel")
        spec = default_spec(name, "Panel")
        spec["parent"] = self.parent_name_for(parent_widget)
        spec["x"], spec["y"] = 12, 12
        created = self.add_element("Panel", parent_name=spec["parent"], spec_override=spec, select=True)
        if widget_alive(created):
            self._reparent(created, parent_widget)

    def _reset_element(self, widget) -> None:
        if not widget_alive(widget) or not isinstance(widget, ElementWidget):
            self.log("[LAYOUT] reset applies to custom elements")
            return
        before = self.capture_snapshot(widget)
        name = widget.element_name()
        parent = str(widget.spec.get("parent") or "")
        fresh = default_spec(name, str(widget.spec.get("type") or "Panel"))
        fresh["parent"] = parent
        fresh["x"], fresh["y"] = widget.x(), widget.y()
        fresh["w"], fresh["h"] = widget.width(), widget.height()
        widget.apply_spec(fresh)
        self.history.push(widget, before or {}, self.capture_snapshot(widget) or {}, "element",
                          label="reset")
        self._persist_elements()
        self._request_save()
        self._selection_changed()
        self.log(f"[LAYOUT] {name} reset to defaults")

    # ---------------------------------------------------------- output/log
    def log(self, message: str) -> None:
        """Studio output panel *and* the main J.A.R.V.I.S. activity log.

        Editor-side failures (script errors, rejected styles, missing assets) are
        only useful if they survive outside the dialog, so every line is
        mirrored into the host's ``write_log`` when the host offers one.
        """
        text = str(message)
        try:
            if hasattr(self, "output"):
                self.output.write(text)
        except Exception:
            pass
        try:
            write = getattr(self.main, "write_log", None)
            if callable(write):
                write(text)
        except Exception:
            pass

    # ------------------------------------------------------------ lifecycle
    def closeEvent(self, event):  # noqa: N802
        try:
            self.animations.stop_all()
        except Exception:
            pass
        try:
            self.viewport._render_timer.stop()
        except Exception:
            pass
        try:
            self.history.clear()
            self.selection.clear()
            self.viewport.set_selection([], None)
            self.viewport.target = None
            self.viewport._cache = None
        except Exception:
            pass
        try:
            self.save_all()
        except Exception:
            pass
        if self.main is not None:
            try:
                self.main._layout_editor = None
            except Exception:
                pass
        super().closeEvent(event)


LayoutEditor = LayoutEditorDialog


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    app = QApplication(sys.argv)
    host = QMainWindow()
    host.resize(1100, 720)
    host.setStyleSheet(_composed_app_css())
    host.setCentralWidget(QFrame())
    host.CONFIG_DIR = Path("config")
    editor = LayoutEditorDialog(host)
    host.show()
    editor.show()
    raise SystemExit(app.exec())