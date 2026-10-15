"""J.A.R.V.I.S standalone settings/control center.

The settings window is deliberately independent from the compact Arc Reactor.
Opening it never expands, hides, or replaces the main reactor.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QSize, QRectF, QPointF
from PyQt6.QtGui import (
    QColor, QFont, QBrush, QPainter, QPen, QPainterPath, QLinearGradient, QRadialGradient, QKeySequence, QShortcut, QKeySequence,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QStackedWidget, QTabBar, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget, QFileDialog, QDoubleSpinBox
)

from ui_theme import (
    ACCENT, ACCENT_BRIGHT, ACCENT_DEEP, ACCENT_INK, ACCENT_TEXT, GOLD, GOLD_DEEP,
    GREEN, GREEN_DEEP, AMBER, RED, BG, BG_DEEP, PANEL, CARD, CARD_HI, HEADER,
    BORDER, BORDER_SOFT, BORDER_FAINT, STROKE_STRONG, STROKE_MID, STROKE_HAIR,
    SURFACE, SURFACE_ALT, SURFACE_INSET, SURFACE_RAISED, PANEL_SOFT,
    TEXT, TEXT_BRIGHT, TEXT_MID, TEXT_DIM, TEXT_FAINT,
    RADIUS_LG, RADIUS_MD, RADIUS_SM,
    FONT_UI, FONT_HEAD, FONT_LOG, FONT_SMALL,
    SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
    CONTROL_SM, CONTROL_MD, CONTROL_LG,
    DEFAULT_ACCENT, PALETTE, migrate_accent as _migrate_accent,
    rgba as _rgba, mix as _mix, shade as _shade, qcolor as _qcolor,
    flatten as _flatten, hex_color as _hex_color,
    composed_app_css as _composed_app_css,
    button_css as _button_css, primary_button_css as _primary_button_css,
    field_css as _field_css, checkbox_css as _checkbox_css, slider_css as _slider_css,
    scrollbar_css as _scrollbar_css, tooltip_css as _tooltip_css,
    panel_title_css as _panel_title_css, panel_subtitle_css as _panel_subtitle_css,
    panel_icon_css as _panel_icon_css, micro_label_css as _micro_label_css,
    separator_css as _separator_css, status_color as _status_color,
    bind_module_globals as _theme_bind,
    is_soft as _theme_is_soft, toggle_css as _toggle_css,
    state_pill_css as _state_pill_css,
)

# ui_theme is *also* imported as a module: the painters below read the live token
# at paint time, and only an attribute lookup sees a theme switch. The names in
# this tuple were bound at import time, so they are refreshed on every switch by
# the listener registered at the bottom of this block.
import ui_theme as _tokens

_PAINTER_TOKENS = (
    "ACCENT", "ACCENT_BRIGHT", "ACCENT_DEEP", "ACCENT_INK", "ACCENT_TEXT",
    "GOLD", "GOLD_DEEP", "GREEN", "GREEN_DEEP", "AMBER", "RED",
    "BG", "BG_DEEP", "PANEL", "CARD", "CARD_HI", "HEADER",
    "BORDER", "BORDER_SOFT", "BORDER_FAINT",
    "STROKE_STRONG", "STROKE_MID", "STROKE_HAIR",
    "SURFACE", "SURFACE_ALT", "SURFACE_INSET", "SURFACE_RAISED", "PANEL_SOFT",
    "TEXT", "TEXT_BRIGHT", "TEXT_MID", "TEXT_DIM", "TEXT_FAINT",
    "RADIUS_LG", "RADIUS_MD", "RADIUS_SM",
)
_theme_bind(globals(), _PAINTER_TOKENS)

DEFAULTS = {
    "compact_opacity": 0,
    "compact_size": 176,
    "ui_opacity": 82,
    "reactor_opacity": 100,
    "reactor_stroke_opacity": 100,
    "reactor_animation_speed": 1.0,
    "equalizer_sensitivity": 1.0,
    "voice_speed": 1.0,
    "voice_pitch": 0.0,
    "voice_volume": 1.0,
    "voice_mode": "standard",
    "tts_engine": "edgetts",
    "tts_voice": "en-GB-RyanNeural",
    "voice_name": "Charon",
    # Primary voice pipeline: the realtime Gemini Live native-audio voice —
    # Gemini speaks the reply itself, with no local synthesis hop. The bundled
    # Piper ONNX voice (jarvis-high) is the Gemini → Piper pipeline's renderer;
    # onnx_execution_provider is cpu | gpu | auto; cpu is the safe default and
    # always works.
    "voice_output_mode": "gemini",
    "onnx_voice_model": "config/voices/jarvis-high.onnx",
    "onnx_voice_config": "config/voices/jarvis-high.json",
    "onnx_voice_speaker": "default",
    "onnx_execution_provider": "cpu",
    "fish_audio_api_key": "",
    "fish_audio_model_id": "s2-pro",
    "fish_audio_voice_id": "",
    "fish_audio_endpoint": "https://api.fish.audio/v1/tts",
    "fish_audio_format": "mp3",
    "fish_audio_latency": "normal",
    "gemini_api_key": "",
    "kokoro_voice": "af_heart",
    "elevenlabs_api_key": "",
    "elevenlabs_voice_id": "pNInz6obpgDQGcFmaJgB",
    "elevenlabs_model_id": "eleven_multilingual_v2",
    "assistant_name": "JARVIS",
    "user_name": "",
    "wake_words": ["hey jarvis", "jarvis", "okay jarvis", "wake up jarvis"],
    "ai_provider": "gemini",
    "ai_model": "",
    "openai_api_key": "",
    "anthropic_api_key": "",
    "groq_api_key": "",
    "custom_provider_name": "",
    "custom_ai_base_url": "",
    "custom_ai_api_key": "",
    "custom_ai_model": "",
    # Optional subsystem keys (edited on the API tab).
    # PC automation and the visual layer deliberately have NO key of their own:
    # Agent TARS and UI-TARS both run on the Gemini key above.
    "mem0_api_key": "",
    "qdrant_api_key": "",
    "qdrant_url": "http://localhost:6333",
    # Extended Delivery-panel voice style (voice/delivery_style.py owns the
    # schema; nested dict so provider/API keys stay untouched by it).
    "voice_style": {},
    "ui_font": "Rajdhani",
    "ui_color": DEFAULT_ACCENT,
    "web_homepage": "https://www.google.com",
    "world_monitor_refresh": 60,
    "memory_autosave": True,
    "panel_always_on_top": True,
    "hud_grid": True,
    "ui_x": None,
    "ui_y": None,
    "quick_actions": [],
    "chat_filter_categories": {"sys": True, "vis": True, "jarvis": True, "you": True, "file": True, "err": True, "warn": True},
    "hide_chat_lines": True,
    "features": {
        "audio_reactive": True,
        "smooth_animations": True,
        "chat_history": True,
        "compact_mode": True,
        "drag_reactor": True,
        "remember_position": True,
        "hide_taskbar_button": False,
        "autostart": False,
        "wake_word": True,
        "hands_free": True,
        "push_to_talk": False,
        "voice_activity_detection": True,
        "realtime_stt": True,
        "narrator": True,
        "voice_interruption": True,
        "barge_in": True,
        "background_listening": True,
        "always_on": False,
        "voice_command_recognition": True,
        "desktop_awareness": True,
        "screen_understanding": True,
        "terminal_control": True,
        "file_control": True,
        "webview": True,
        "webview_engine": True,
        "webview_music": True,
        # Play music in the application installed on this PC (Spotify, VLC) when
        # one is available, instead of the internal WebView player.
        "native_music_apps": True,
        "web_task": True,
        "world_monitor": True,
        "floating_windows": True,
        "draggable_panels": True,
        "enable_sfx": True,
        "proactive_speaking": False,
        "mic_access": True,
        "camera_access": True,
        "voice_engine_control": True,
        "self_diagnostics": True,
        "auto_approve_coding": False,
        "document_architect": True,
        "connect": True,
        "laya_ai": True,
    },
}


#: The handful of switches that decide what a fresh install may do, in the
#: order Quick Setup shows them. Every one of them is a real feature flag the
#: rest of the app already gates on — Quick Setup introduces no switch of its own.
_ESSENTIAL_SWITCHES: tuple[tuple[str, str, str], ...] = (
    ("wake_word", 'Wake word: "Hey Jarvis"',
     "Listen for the wake word so you can talk to him without touching the PC."),
    ("hands_free", "Hands-free voice mode",
     "Talk naturally: the reply is spoken, and the microphone stays open."),
    ("realtime_stt", "Live transcript",
     "Show what is being heard in the command box while you are still speaking."),
    ("narrator", "Narrate actions",
     "Say what is being done, as it is done, instead of working silently."),
    ("document_architect", "Documents",
     "Write and read Word, Excel, PowerPoint and PDF files on this machine."),
    ("connect", "JarvisOS Connect",
     "Let a paired phone or browser reach this machine through the gateway."),
)


def load(path: str | Path) -> dict:
    path = Path(path)
    data = dict(DEFAULTS)
    data["features"] = dict(DEFAULTS["features"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            # A pipeline recorded by SAVE AS DEFAULT is the default every
            # fresh session starts on — overriding the DEFAULTS constant.
            try:
                _dp = str(((raw.get("voice") or {}).get("defaultPipeline")) or "")
                if _dp in ("gemini", "gemini_piper", "local"):
                    data["voice_output_mode"] = _dp
            except Exception:
                pass
            data.update(raw)
            if isinstance(raw.get("features"), dict):
                data["features"].update(raw["features"])
    except Exception:
        pass
    # The voice style is normalized at the single read path: a fresh dict
    # (never the DEFAULTS object), every parameter present and clamped, so a
    # partial or hand-edited config still restores every slider.
    try:
        from voice.delivery_style import normalize_style as _norm_style
        data["voice_style"] = _norm_style(data.get("voice_style"))
    except Exception:
        pass
    # A stored accent from the retired palette is mapped onto the current token
    # here, at the single read path, so nothing downstream keeps the old palette
    # alive and the user's own custom colour is passed through untouched.
    data["ui_color"] = _migrate_accent(data.get("ui_color") or DEFAULT_ACCENT)
    # A stale "ui_theme" key from the retired theme picker is dropped on read so
    # it cannot outlive the feature; the window always uses the default look.
    data.pop("ui_theme", None)
    return data


def save(path: str | Path, **updates) -> dict:
    path = Path(path)
    data = load(path)
    for k, v in updates.items():
        if k == "features" and isinstance(v, dict):
            merged = dict(data.get("features", {}))
            merged.update(v)
            data[k] = merged
        else:
            data[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return data


def _default_config_path() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "config" / "api_keys.json"


class HorizontalNavBar(QTabBar):
    """Reference-style left rail with horizontal labels and angular cards."""
    def tabSizeHint(self, index):
        base = super().tabSizeHint(index)
        return QSize(150, max(46, base.height()))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for i in range(self.count()):
            r = self.tabRect(i).adjusted(4, 4, -5, -4)
            cut = 8
            path = QPainterPath()
            path.moveTo(r.left()+cut, r.top())
            path.lineTo(r.right()-cut, r.top())
            path.lineTo(r.right(), r.top()+cut)
            path.lineTo(r.right(), r.bottom()-cut)
            path.lineTo(r.right()-cut, r.bottom())
            path.lineTo(r.left()+cut, r.bottom())
            path.lineTo(r.left(), r.bottom()-cut)
            path.lineTo(r.left(), r.top()+cut)
            path.closeSubpath()
            selected = i == self.currentIndex()
            p.setPen(QPen(QColor(ACCENT if selected else STROKE_HAIR), 1))
            p.fillPath(path, QBrush(_qcolor(ACCENT, 170) if selected else _qcolor(SURFACE_ALT, 92)))
            p.drawPath(path)
            if selected:
                p.setPen(QPen(QColor(ACCENT_BRIGHT), 2))
                p.drawLine(r.left()+2, r.top()+10, r.left()+2, r.bottom()-10)
            p.setPen(QColor(TEXT_BRIGHT if selected else TEXT_FAINT))
            p.setFont(QFont(FONT_SMALL.strip("'"), 8, QFont.Weight.Bold))
            p.drawText(r.adjusted(16, 0, -8, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.tabText(i))
        p.end()


def _style_section_folded_height(box) -> int:
    """Header-only height for a checkable HudGroupBox (collapsed style section)."""
    try:
        return box.fontMetrics().height() + 16
    except Exception:
        return 30


def _rounded_path(rect, radius: float) -> QPainterPath:
    """A soft rounded outline (the soft themes' card shape)."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), float(radius), float(radius))
    return path


def _panel_path(rect, cut: int) -> QPainterPath:
    """The panel outline: a cut-corner HUD shape, or a soft rounded card.

    `cut` of 0 means the active theme has no corner cut (the soft themes), so
    the same painters produce the modern card shape without a second code path.
    """
    if not cut:
        return _rounded_path(rect, float(RADIUS_LG))
    cut = int(max(6, min(cut, min(rect.width(), rect.height()) / 6)))
    path = QPainterPath()
    path.moveTo(QPointF(rect.left() + cut, rect.top()))
    path.lineTo(QPointF(rect.right() - cut, rect.top()))
    path.lineTo(QPointF(rect.right(), rect.top() + cut))
    path.lineTo(QPointF(rect.right(), rect.bottom() - cut))
    path.lineTo(QPointF(rect.right() - cut, rect.bottom()))
    path.lineTo(QPointF(rect.left() + cut, rect.bottom()))
    path.lineTo(QPointF(rect.left(), rect.bottom() - cut))
    path.lineTo(QPointF(rect.left(), rect.top() + cut))
    path.closeSubpath()
    return path


class HudGroupBox(QGroupBox):
    """The shared section container: HUD cut corner, or a soft rounded card."""
    def __init__(self, title='', parent=None):
        super().__init__(title, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setProperty('_jarvis_floating_panel', True)
        self.setProperty('_jarvis_panel_kind', 'control_center')
        self.setProperty('_jarvis_panel_key', 'control_center')
        self.setObjectName('HudGroupBox')

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect().adjusted(1, 1, -1, -1)
        soft = _theme_is_soft()
        path = _panel_path(r, 0 if soft else int(getattr(_tokens, "CORNER_CUT", 12) or 12))
        p.fillPath(path, QBrush(_qcolor(SURFACE, 236 if soft else 158)))
        p.setPen(QPen(QColor(STROKE_MID), 1))
        p.drawPath(path)
        # Title cutout and caption.
        title = self.title()
        if title:
            f = QFont(FONT_HEAD.strip("'"), 7, QFont.Weight.Bold)
            fm = p.fontMetrics(); tw = fm.horizontalAdvance(title) + 18
            tr = QRectF(r.left()+10, r.top()-7, tw, 16)
            p.setPen(QColor(ACCENT_TEXT)); p.setFont(f)
            p.drawText(tr, Qt.AlignmentFlag.AlignCenter, title)
        p.end()


class HudHeader(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('HudHeader')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.rect().adjusted(1,1,-1,-1)
        soft = _theme_is_soft()
        cut = 0 if soft else int(getattr(_tokens, "CORNER_CUT", 12) or 9)
        path = _panel_path(r, cut)
        p.fillPath(path, QBrush(_qcolor(SURFACE, 236 if soft else 160)))
        p.setPen(QPen(QColor(STROKE_STRONG if not soft else STROKE_MID), 1))
        p.drawPath(path)
        p.end()


def _hud_cut_path(rect, cut=14):
    path = QPainterPath()
    cut = max(8, min(cut, min(rect.width(), rect.height()) / 6))
    path.moveTo(QPointF(rect.left() + cut, rect.top()))
    path.lineTo(QPointF(rect.right() - cut, rect.top()))
    path.lineTo(QPointF(rect.right(), rect.top() + cut))
    path.lineTo(QPointF(rect.right(), rect.bottom() - cut))
    path.lineTo(QPointF(rect.right() - cut, rect.bottom()))
    path.lineTo(QPointF(rect.left() + cut, rect.bottom()))
    path.lineTo(QPointF(rect.left(), rect.bottom() - cut))
    path.lineTo(QPointF(rect.left(), rect.top() + cut))
    path.closeSubpath()
    return path


class _ControlCenterBackdrop(QFrame):
    """Angular grid-and-glow background used by the standalone control center."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            w, h = max(1, self.width()), max(1, self.height())
            bounds = QRectF(1.5, 1.5, w - 3.0, h - 3.0)
            # HUD: the cut-corner frame. Soft: the same backdrop, rounded off.
            if _theme_is_soft():
                clip = _rounded_path(bounds, float(RADIUS_LG))
            else:
                cut = max(12, min(18, min(w, h) * 0.03))
                clip = _hud_cut_path(bounds, cut)
            p.setClipPath(clip)
            gradient = QLinearGradient(0, 0, w, h)
            gradient.setColorAt(0.0, _qcolor(SURFACE, 238))
            gradient.setColorAt(0.5, _qcolor(BG_DEEP, 246))
            gradient.setColorAt(1.0, _qcolor(_mix(ACCENT, BG, 0.07), 234))
            p.fillPath(clip, QBrush(gradient))

            glow = QRadialGradient(QPointF(w * 0.80, h * 0.16), max(w, h) * 0.72)
            glow.setColorAt(0.0, _qcolor(ACCENT, int(getattr(_tokens, "GLOW_ALPHA", 40))))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillPath(clip, QBrush(glow))

            # A single warm counterpoint low-left so the cold field has depth.
            gold = QRadialGradient(QPointF(w * 0.10, h * 0.92), max(w, h) * 0.55)
            gold.setColorAt(0.0, _qcolor(GOLD, 16))
            gold.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.fillPath(clip, QBrush(gold))

            p.setPen(QPen(_qcolor(BORDER_FAINT, int(getattr(_tokens, "GRID_ALPHA", 22))), 1))
            for x in range(0, w + 28, 28):
                p.drawLine(x, 0, x, h)
            for y in range(0, h + 28, 28):
                p.drawLine(0, y, w, y)
            # Soft accent atmosphere (bounded to three layers), then one crisp
            # frame. The soft themes drop the halo and keep only the frame.
            if not _theme_is_soft():
                for width, alpha in ((7.0, 10), (4.0, 18), (2.0, 34)):
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(_qcolor(BORDER, alpha), width))
                    p.drawPath(clip)
            p.setPen(QPen(_qcolor(BORDER_SOFT, 72), 1))
            p.drawPath(clip)
            p.setClipping(False)
        except Exception:
            pass
        p.end()


class _SettingsRoot(QFrame):
    """Root shell that keeps the painted backdrop fitted to the dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SettingsRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._futuristic_backdrop = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_backdrop()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_backdrop()

    def _fit_backdrop(self):
        backdrop = self._futuristic_backdrop
        if backdrop is not None:
            backdrop.setGeometry(self.rect())
            backdrop.lower()


class SettingsWindow(QDialog):
    """Full-featured tabbed control center for J.A.R.V.I.S."""

    settings_saved = pyqtSignal(dict)
    voice_test_requested = pyqtSignal(str)
    # JOptimizer progress is emitted from a worker thread and delivered to the
    # GUI thread by Qt's queued connection, so the bar/summary update live.
    jopt_progress = pyqtSignal(int, str)
    jopt_done = pyqtSignal(bool, str, str)

    def __init__(self, config_path: str | Path | None = None, parent=None):
        super().__init__(parent)
        self._config_path = Path(config_path) if config_path else _default_config_path()
        self._data = load(self._config_path)
        self._color = str(self._data.get("ui_color", DEFAULTS["ui_color"]))
        # ── shared registries ────────────────────────────────────────────
        # One key has one value: every edit that shows a key lands in
        # `_key_edits[field]` and all of them mirror each other, so Quick Setup
        # and the API tab can never disagree about a credential.
        self._key_edits: dict[str, list] = {}
        self._key_pills: dict[str, QLineEdit] = {}
        self._key_rows: dict[str, dict] = {}
        # Every setting that can be searched, in tab order: (tab, label, widget).
        self._setting_index: list[tuple[str, str, object]] = []
        self._dirty = False
        self._search_results = None
        self._drag_offset = None
        self._layout_rows: dict[str, dict] = {}
        self._layout_selected_name = None

        self.setWindowTitle("J.A.R.V.I.S Settings")
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setProperty('_jarvis_floating_panel', True)
        self.setProperty('_jarvis_panel_kind', 'control_center')
        self.setProperty('_jarvis_panel_key', 'control_center')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumSize(940, 680)
        self.resize(1120, 760)

        self._build_style()
        self._build_ui()
        self._load_layout_values()
        self.tabs.currentChanged.connect(self._sync_nav_status)
        self._sync_nav_status(self.tabs.currentIndex())

    def _build_style(self):
        # The control center no longer carries its own copy of the visual
        # language: the chrome is composed from the shared component rules, so
        # this window, the floating panels and the main shell cannot drift apart.
        self.setStyleSheet(_composed_app_css())

    # ── key rows (one credential, one value, wherever it is shown) ──────────
    def _new_key_edit(self, field: str, placeholder: str = "", tip: str = "",
                      width: int = 0) -> QLineEdit:
        """Create one editor for *field* and mirror it with every other one.

        A key appears on Quick Setup and again on the tab that owns its
        provider; both are the same value, so they are kept in step instead of
        one of them silently winning at save time.
        """
        field = str(field)
        peers = self._key_edits.setdefault(field, [])
        edit = QLineEdit(str(self._data.get(field, "") or ""))
        if (self._hub_meta(field) or {}).get("secret", True):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        if placeholder:
            edit.setPlaceholderText(placeholder)
        if tip:
            edit.setToolTip(tip)
        if width:
            edit.setMinimumWidth(width)

        def _mirror(text: str, source=edit) -> None:
            for other in list(peers):
                if other is not source and other.text() != text:
                    other.blockSignals(True)
                    other.setText(text)
                    other.blockSignals(False)

        edit.textChanged.connect(_mirror)
        edit.textChanged.connect(lambda _t: self._mark_dirty())
        peers.append(edit)
        return edit

    @staticmethod
    def _hub_module():
        try:
            from services import hub
            return hub
        except Exception:
            return None

    def _hub_meta(self, field: str) -> dict:
        hub = self._hub_module()
        if hub is None:
            return {}
        return hub.key_meta(field)

    def _key_row(self, field: str, label: str = "", placeholder: str = "",
                 tip: str = "", layout=None, with_label: bool = True,
                 tab: str = "QUICK SETUP"):
        """One credential row: [field] [PASTE] [GET API KEY ↗] [TEST] [pill].

        Every button is derived from the hub's own metadata, so a key with no
        key page (a device password, a self-invented pairing secret) shows no
        GET API KEY button and a key with no real test shows no TEST button —
        rather than a button that cannot do what it says.
        """
        meta = self._hub_meta(field)
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        edit = self._new_key_edit(field, placeholder or str(meta.get("label") or label),
                                  tip or self._key_tip(field))
        h.addWidget(edit, 1)
        actions: dict[str, QPushButton] = {}
        if meta.get("key_url") or meta.get("help_url"):
            get_btn = QPushButton("GET API KEY ↗")
            get_btn.setToolTip(self._get_key_tip(field))
            get_btn.clicked.connect(lambda _c=False, f=field: self._open_key_page(f))
            h.addWidget(get_btn)
            actions["get"] = get_btn
        paste_btn = QPushButton("PASTE")
        paste_btn.setToolTip("Paste the key from the clipboard.")
        paste_btn.clicked.connect(lambda _c=False, f=field: self._paste_key(f))
        h.addWidget(paste_btn)
        actions["paste"] = paste_btn
        if meta.get("test"):
            test_btn = QPushButton("TEST")
            test_btn.setToolTip(f"Make one real request with the {meta.get('label')} "
                                f"credential and report exactly what came back.")
            test_btn.clicked.connect(lambda _c=False, f=field: self._test_key(f))
            h.addWidget(test_btn)
            actions["test"] = test_btn
        pill = QLabel("NOT TESTED")
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill.setMinimumWidth(96)
        pill.setStyleSheet(_state_pill_css("idle"))
        h.addWidget(pill)
        self._key_pills[field] = pill
        self._key_rows[field] = {"row": row, "edit": edit, "pill": pill, **actions}
        if layout is not None:
            if with_label and label:
                head = QLabel(label.upper())
                head.setStyleSheet(
                    f"color:{TEXT_DIM};font:700 7pt 'Exo 2';letter-spacing:1px;"
                    "background:transparent;")
                layout.addWidget(head)
            layout.addWidget(row)
        self._index_setting(tab, label or field, edit)
        return row, edit

    def _key_tip(self, field: str) -> str:
        """The hover text for a credential: what it is for, and how to get it."""
        meta = self._hub_meta(field)
        if not meta:
            return ""
        parts = [str(meta.get("summary") or "")]
        if meta.get("note"):
            parts.append(str(meta["note"]))
        if meta.get("key_help"):
            parts.append(str(meta["key_help"]))
        if meta.get("help_url"):
            parts.append(f"Docs: {meta['help_url']}")
        return "\n".join(part for part in parts if part)

    def _get_key_tip(self, field: str) -> str:
        meta = self._hub_meta(field)
        url = str(meta.get("key_url") or meta.get("help_url") or "")
        help_text = str(meta.get("key_help") or "")
        if not url:
            return help_text or "There is no key page for this credential."
        return (f"Open {url} in your browser.\n" + (help_text or "")
                + "\nThen PASTE the value here and press TEST.")

    def _open_key_page(self, field: str) -> None:
        """Open the provider's key page in the default browser."""
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        meta = self._hub_meta(field)
        url = str(meta.get("key_url") or meta.get("help_url") or "")
        if not url:
            self._status.setText(f"NO KEY PAGE FOR {field.upper()}")
            return
        try:
            QDesktopServices.openUrl(QUrl(url))
            self._status.setText(f"OPENED {url}")
        except Exception as exc:
            self._status.setText(f"COULD NOT OPEN THE BROWSER: {exc}")

    def _paste_key(self, field: str) -> None:
        """Paste the clipboard into the field, then test what landed there."""
        row = self._key_rows.get(field) or {}
        edit = row.get("edit")
        if edit is None:
            return
        try:
            text = str(QApplication.clipboard().text() or "").strip()
        except Exception:
            text = ""
        if not text:
            self._status.setText("THE CLIPBOARD IS EMPTY")
            return
        edit.setText(text)
        self._status.setText(f"PASTED {len(text)} CHARACTERS — TESTING…")
        self._test_key(field)

    def _test_key(self, field: str) -> None:
        """TEST one credential: a real request, off the GUI thread."""
        meta = self._hub_meta(field)
        target = str(meta.get("test") or "")
        row = self._key_rows.get(field) or {}
        if not target:
            self._set_key_state(field, "configured",
                                "There is no read-only request to test this value "
                                "with — it is used exactly as entered.")
            return
        self._set_key_state(field, "testing", f"Testing {meta.get('label')}…")
        button = row.get("test")
        if button is not None:
            button.setEnabled(False)
        try:
            import threading as _threading
            _threading.Thread(target=self._probe_key_async, args=(field, target),
                              name=f"key-probe-{target}", daemon=True).start()
        except Exception as exc:
            self._set_key_state(field, "unavailable", str(exc))

    def _probe_key_async(self, field: str, target: str) -> None:
        try:
            hub = self._hub_module()
            result = hub.probe_target(target, self._hub_config()) if hub else {}
        except Exception as exc:
            result = {"state": "unavailable",
                      "detail": f"The test could not run: {exc}"}
        self._key_pending[field] = result

    def _set_key_state(self, field: str, state: str, detail: str = "") -> None:
        pill = self._key_pills.get(field)
        if pill is None:
            return
        pill.setText(str(state or "?").replace("_", " ").upper())
        pill.setStyleSheet(_state_pill_css(state))
        pill.setToolTip(str(detail or ""))
        pill.setAccessibleName(str(state))

    def _refresh_key_states(self) -> None:
        """Cheap, offline state for every credential pill (no sockets)."""
        hub = self._hub_module()
        if hub is None:
            return
        try:
            for row in hub.status_targets(self._hub_config()):
                self._set_key_state(row["field"], row["state"], row["detail"])
        except Exception as exc:
            try:
                self.write_log_safe(f"Key status failed: {exc}")
            except Exception:
                pass
        self._refresh_ready_strip()

    def _drain_key_results(self) -> None:
        pending = getattr(self, "_key_pending", None)
        if not pending:
            return
        for field, result in list(pending.items()):
            pending.pop(field, None)
            row = self._key_rows.get(field) or {}
            button = row.get("test")
            if button is not None:
                button.setEnabled(True)
            self._set_key_state(field, str(result.get("state") or "?"),
                                str(result.get("detail") or ""))
            self._last_test = {**result, "field": field}
            try:
                self.write_log_safe(
                    f"Key test {field}: {result.get('state')} — "
                    f"{str(result.get('detail'))[:160]}")
            except Exception:
                pass
        self._refresh_ready_strip()

    # ── search index ────────────────────────────────────────────────────────
    def _index_setting(self, tab, label: str, widget=None) -> None:
        """Register one searchable setting (tab name, label, widget)."""
        try:
            name = self.tabs.tabText(int(tab)) if isinstance(tab, int) else str(tab)
        except Exception:
            name = str(tab)
        entry = (name, str(label), widget)
        if entry not in self._setting_index:
            self._setting_index.append(entry)

    def _index_children(self, tab, root) -> None:
        """Index every labelled control on a finished page, generically.

        This is what makes search work on the tabs the Quick Setup work never
        touched: a control's label is its placeholder, its text, its tooltip or
        its object name — and a jump still lands on the real widget.
        """
        try:
            from PyQt6.QtWidgets import QAbstractButton, QAbstractSpinBox
        except Exception:
            return
        for widget in root.findChildren(QWidget):
            if isinstance(widget, (QLineEdit, QComboBox, QAbstractSpinBox,
                                   QCheckBox, QAbstractButton)):
                label = self._label_for(widget)
                if label:
                    self._index_setting(tab, label, widget)

    @staticmethod
    def _label_for(widget) -> str:
        for getter in ("placeholderText", "text", "objectName"):
            try:
                text = str(getattr(widget, getter)() or "").strip()
            except Exception:
                text = ""
            if text:
                return text.splitlines()[0][:80]
        try:
            return str(widget.toolTip() or "").splitlines()[0][:80]
        except Exception:
            return ""

    def _on_search_changed(self, text: str) -> None:
        query = str(text or "").strip().lower()
        results = getattr(self, "_search_results", None)
        if results is None:
            return
        if not query:
            results.hide()
            return
        hits = [(tab, label, widget) for tab, label, widget in self._setting_index
                if query in label.lower() or query in tab.lower()]
        results.clear()
        if not hits:
            item = QListWidgetItem("No setting matches that.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            results.addItem(item)
        else:
            for tab, label, _widget in hits[:40]:
                item = QListWidgetItem(f"{tab}  ·  {label}")
                item.setData(Qt.ItemDataRole.UserRole, (tab, label))
                results.addItem(item)
        results.show()

    def _jump_to_setting(self, item) -> None:
        payload = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not payload:
            return
        tab, label = payload
        index = self._tab_index(tab)
        if index < 0:
            return
        self.tabs.setCurrentIndex(index)
        widget = next((w for t, l, w in self._setting_index
                       if t == tab and l == label and w is not None), None)
        if widget is not None:
            try:
                widget.setFocus()
                area = widget.parent()
                while area is not None and not isinstance(area, QScrollArea):
                    area = area.parent()
                if area is not None:
                    area.ensureWidgetVisible(widget, 0, 90)
            except Exception:
                pass
        results = getattr(self, "_search_results", None)
        if results is not None:
            results.hide()
        self._search.clear()
        self._status.setText(f"{tab.upper()} · {label.upper()}")

    def _tab_index(self, name: str) -> int:
        wanted = str(name or "").strip().upper()
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).strip().upper() == wanted:
                return i
        return -1

    # ── unsaved state + the WHAT'S READY strip ──────────────────────────────
    def _mark_dirty(self, *_a) -> None:
        """Show that something changed and has not been saved yet."""
        if getattr(self, "_dirty", False):
            return
        self._dirty = True
        pill = getattr(self, "_save_state", None)
        if pill is not None:
            pill.setText("●  UNSAVED CHANGES")
            pill.setStyleSheet(
                f'color:{AMBER};font:800 8pt "Exo 2";background:{_rgba(AMBER, 40)};'
                f'padding:7px 12px;border:1px solid {_rgba(AMBER, 95)};'
                f'border-radius:{RADIUS_SM}px; letter-spacing:1px;')

    def _clear_dirty(self) -> None:
        self._dirty = False
        pill = getattr(self, "_save_state", None)
        if pill is not None:
            pill.setText("●  SAVED")
            pill.setStyleSheet(
                f'color:{GREEN};font:800 8pt "Exo 2";background:{_rgba(GREEN, 40)};'
                f'padding:7px 12px;border:1px solid {_rgba(GREEN, 95)};'
                f'border-radius:{RADIUS_SM}px; letter-spacing:1px;')

    def _ready_summary(self) -> tuple[int, int, list[str]]:
        """How many credentials are set, and what the first blockers are."""
        hub = self._hub_module()
        if hub is None:
            return 0, 0, ["The service hub module is unavailable."]
        config = self._hub_config()
        ready = blockers = 0
        notes: list[str] = []
        for row in hub.status_targets(config):
            state = str(row.get("state") or "")
            if state in ("configured", "key_optional"):
                ready += 1
            elif state == "missing_key":
                blockers += 1
                if len(notes) < 4:
                    notes.append(f"{row['label']} — {row['detail']}")
        return ready, blockers, notes

    def _refresh_ready_strip(self) -> None:
        label = getattr(self, "_ready_strip", None)
        if label is None:
            return
        try:
            ready, blockers, notes = self._ready_summary()
        except Exception as exc:
            label.setText(f"Could not read the setup state: {exc}")
            return
        line = (f"{ready} credential(s) set · {blockers} still needed"
                if blockers else f"{ready} credential(s) set · nothing missing")
        if notes:
            line += "\n" + "\n".join(f"• {note}" for note in notes)
        label.setText(line)

    def _build_ui(self):
        root = _SettingsRoot(self)
        main = QVBoxLayout(self)
        main.setContentsMargins(12, 12, 12, 12)
        main.addWidget(root)
        backdrop = _ControlCenterBackdrop(root)
        root._futuristic_backdrop = backdrop
        root._fit_backdrop()

        outer = QVBoxLayout(root)
        outer.setContentsMargins(11, 11, 11, 11)
        outer.setSpacing(12)

        header = HudHeader(root)
        header.setFixedHeight(68)
        hb = QHBoxLayout(header)
        hb.setContentsMargins(18, 9, 10, 9)
        hb.setSpacing(14)

        icon = QLabel("◈")
        icon.setStyleSheet(f'color:{ACCENT_TEXT};font:700 20pt "Segoe UI Symbol";background:transparent;')
        hb.addWidget(icon)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("J.A.R.V.I.S  //  CONTROL CENTER")
        title.setMinimumWidth(300)
        title.setWordWrap(False)
        title.setStyleSheet(f'color:{TEXT_BRIGHT};font:800 12pt "Orbitron";background:transparent; letter-spacing:2px;')
        subtitle = QLabel("GENERAL • AI • VOICE • SYSTEM • MEMORY • PERFORMANCE • PLUGINS • EVOLUTION • JOPTIMIZER")
        subtitle.setStyleSheet(f'color:{TEXT_FAINT};font:700 7pt "Exo 2";background:transparent; letter-spacing:1px;')
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        hb.addLayout(title_box)
        hb.addStretch(1)

        # Search: one box that finds a setting by name and jumps to it. It is
        # built before the tabs so the first indexed setting can register into
        # an existing widget rather than a half-built window.
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search settings…")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(210)
        self._search.setToolTip("Type part of a setting's name, then pick it from the list.")
        self._search.textChanged.connect(self._on_search_changed)
        hb.addWidget(self._search)
        self._search_results = QListWidget()
        self._search_results.setMaximumHeight(190)
        self._search_results.hide()
        self._search_results.itemClicked.connect(self._jump_to_setting)
        self._search_results.itemActivated.connect(self._jump_to_setting)

        self._save_state = QLabel("●  READY")
        hb.addWidget(self._save_state)
        self._clear_dirty()

        self._status = QLabel("●  READY  //  CONTROL CENTER")
        hb.addWidget(self._status)
        self._apply_status_style()
        close = QPushButton("×")
        close.setFixedSize(34, 34)
        close.setToolTip("Close control center")
        close.setStyleSheet(f"QPushButton {{ background: transparent; color: {TEXT_DIM}; border: none; border-radius: 7px; font: 700 16pt 'Rajdhani'; padding: 0px; }} QPushButton:hover {{ color: {RED}; background: rgba(60, 10, 20, 130); }}")
        close.clicked.connect(self.close)
        hb.addWidget(close)
        outer.addWidget(header)

        body = QVBoxLayout()
        body.setSpacing(8)
        body.addWidget(self._search_results)
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(14)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabBar(HorizontalNavBar())
        self.tabs.setTabPosition(QTabWidget.TabPosition.West)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        tabs_row.addWidget(self.tabs, 1)
        body.addLayout(tabs_row, 1)
        outer.addLayout(body, 1)

        # QUICK SETUP is built first and shown first: it is the only page a
        # fresh install needs, and the tabs after it are the detail.
        self._build_quick_setup_tab()
        self._build_general_tab()
        self._build_status_tab()
        self._build_api_tab()
        self._build_ai_tab()
        self._build_voice_tab()
        self._build_personality_tab()
        self._build_system_tab()
        self._build_automation_tab()
        self._build_memory_settings_tab()
        self._build_permissions_tab()
        self._build_layout_tab()
        self._build_plugins_tab()
        self._build_evolution_tab()
        self._build_diagnostics_tab()
        self._build_joptimizer_tab()
        self._build_web_tab()
        self._build_3d_tab()
        self._build_video_tab()
        self._build_image_tab()
        self._build_mcp_tab()
        self._build_obsidian_tab()
        self._build_performance_tab()

        # The hub's key timers live with the window, not with one tab: both
        # Quick Setup and the API tab feed the same rows and pills.
        self._key_pending: dict = {}
        self._last_test: dict = {}
        self._key_timer = QTimer(self)
        self._key_timer.setInterval(250)
        self._key_timer.timeout.connect(self._drain_key_results)
        self._key_timer.start()
        self._refresh_key_states()

        footer = QHBoxLayout()
        footer.setSpacing(8)
        hint = QLabel("CHANGES ARE STORED IN CONFIG / API_KEYS.JSON")
        hint.setStyleSheet(f'color:{TEXT_FAINT};font:600 7pt "Exo 2";padding-left:4px;')
        footer.addWidget(hint)
        footer.addStretch(1)
        reset = QPushButton("RESET")
        reset.setToolTip("Restore default J.A.R.V.I.S settings")
        reset.clicked.connect(self.reset_defaults)
        footer.addWidget(reset)
        save_btn = QPushButton("SAVE CHANGES")
        save_btn.setObjectName("PrimaryButton")
        save_btn.setToolTip("Write every tab's changes to config/api_keys.json.")
        save_btn.clicked.connect(self.save_settings)
        footer.addWidget(save_btn)
        self._save_btn = save_btn
        outer.addLayout(footer)

        header.mousePressEvent = self._header_press
        header.mouseMoveEvent = self._header_move
        header.mouseReleaseEvent = self._header_release
        title.mousePressEvent = self._header_press
        title.mouseMoveEvent = self._header_move
        title.mouseReleaseEvent = self._header_release

        self._close_shortcut = QShortcut(QKeySequence("Shift+Home"), self)
        self._close_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._close_shortcut.activated.connect(self.close)

    def _sync_nav_status(self, index: int):
        txt = self.tabs.tabText(index) if 0 <= index < self.tabs.count() else ""
        if txt:
            self._status.setText(f"●  {txt}")
        # The WHAT'S READY strip reads the live field values, so it is refreshed
        # whenever the user arrives at the page that shows it.
        if txt.strip().upper() == "QUICK SETUP":
            try:
                self._refresh_key_states()
            except Exception:
                pass

    def _scroll_tab(self):
        page = QWidget()
        page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        page.setStyleSheet('background: transparent;')
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet('QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget { background: transparent; }')
        area.viewport().setStyleSheet('background: transparent;')
        area.setWidget(page)
        return page, QVBoxLayout(page), area

    def _tab_intro(self, lay, text):
        row = QFrame()
        row.setObjectName("TabIntro")
        # Drawn from the active theme: the strip is a tint of the field, never a
        # dark literal that would sit on a light page as a black band.
        row.setStyleSheet(
            "QFrame#TabIntro {"
            f" background: {PANEL_SOFT};"
            f" border: 1px solid {BORDER_FAINT};"
            f" border-radius: {RADIUS_MD}px;"
            " padding: 2px; }")
        rv = QHBoxLayout(row)
        rv.setContentsMargins(11, 7, 11, 7)
        rv.setSpacing(9)
        marker = QLabel("◈")
        marker.setStyleSheet(f'color:{ACCENT_TEXT};font:700 12pt "Segoe UI Symbol";background:transparent;')
        label = QLabel(text)
        label.setStyleSheet(f'color:{ACCENT_TEXT};font:800 8.5pt "Orbitron";background:transparent;letter-spacing:2px;')
        rv.addWidget(marker)
        rv.addWidget(label, 1)
        telemetry = QLabel("JARVIS CONTROL MATRIX")
        telemetry.setStyleSheet(f'color:{TEXT_FAINT};font:700 6.5pt "Exo 2";background:transparent;letter-spacing:1px;')
        rv.addWidget(telemetry)
        lay.addWidget(row)

    # ── QUICK SETUP (first tab: everything a fresh install needs) ───────────
    def _build_quick_setup_tab(self):
        """One page that takes a fresh machine to a working J.A.R.V.I.S.

        Deliberately short: who he is, how he looks, which voice he uses, the
        handful of switches that decide what he may do, and every credential —
        each with a real TEST. Nothing here is a new setting: the values are the
        same ones the tabs own (the key fields are literally the same widgets'
        peers, mirrored), so this page cannot drift from them.
        """
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "QUICK SETUP  //  FIVE MINUTES TO A WORKING J.A.R.V.I.S")

        # ── 1 · who he is ───────────────────────────────────────────────
        who = HudGroupBox("1 · IDENTITY")
        wv = QFormLayout(who)
        self.assistant_name = self._mirror_field(
            QLineEdit(str(self._data.get("assistant_name", "JARVIS") or "JARVIS")),
            "Assistant name", "JARVIS")
        wv.addRow("Assistant name", self.assistant_name)
        self.user_name = self._mirror_field(
            QLineEdit(str(self._data.get("user_name", "") or "")),
            "Your name", "How should J.A.R.V.I.S address you? (empty = sir)")
        wv.addRow("Your name", self.user_name)
        lay.addWidget(who)

        # ── 2 · the look ────────────────────────────────────────────────
        look = HudGroupBox("2 · LOOK")
        lookv = QVBoxLayout(look)
        lookv.setSpacing(SPACE_SM)
        accent_row = QHBoxLayout()
        self.color_btn = QPushButton(self._color)
        self.color_btn.setToolTip("The working colour of the whole interface.")
        self.color_btn.clicked.connect(self.pick_color)
        accent_row.addWidget(QLabel("Accent"))
        accent_row.addWidget(self.color_btn)
        self._index_setting("QUICK SETUP", "Accent", self.color_btn)
        for label, color in (("CYAN", ACCENT), ("RED", RED), ("GREEN", GREEN),
                             ("GOLD", GOLD), ("WHITE", TEXT_BRIGHT)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, c=color: self._set_color(c))
            accent_row.addWidget(b)
        accent_row.addStretch(1)
        lookv.addLayout(accent_row)
        lay.addWidget(look)

        # ── 3 · voice ──────────────────────────────────────────────────
        voice = HudGroupBox("3 · VOICE")
        vv = QHBoxLayout(voice)
        self._qs_voice_note = QLabel(
            "The voice pipeline and its provider live on the VOICE tab, where "
            "each engine keeps its own settings. TEST VOICE asks the running "
            "session to speak one line — the quickest proof the link works.")
        self._qs_voice_note.setWordWrap(True)
        self._qs_voice_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        vv.addWidget(self._qs_voice_note, 1)
        voice_test = QPushButton("TEST VOICE")
        voice_test.setToolTip("Ask the active J.A.R.V.I.S session to speak a short greeting.")
        voice_test.clicked.connect(self._test_voice_link)
        vv.addWidget(voice_test)
        lay.addWidget(voice)

        # ── 4 · what he may do ─────────────────────────────────────────
        powers = HudGroupBox("4 · WHAT J.A.R.V.I.S MAY DO")
        pv = QVBoxLayout(powers)
        pv.setSpacing(SPACE_SM)
        self._qs_toggles = {}
        for key, title, subtitle in _ESSENTIAL_SWITCHES:
            pv.addWidget(self._toggle_row(key, title, subtitle))
        more = QLabel("Every other switch is on the AUTOMATION and PERMISSIONS tabs.")
        more.setStyleSheet(f"color:{TEXT_FAINT};background:transparent;")
        pv.addWidget(more)
        lay.addWidget(powers)

        # ── 5 · credentials ────────────────────────────────────────────
        keys = HudGroupBox("5 · API KEYS  //  GET · PASTE · TEST")
        kv = QVBoxLayout(keys)
        kv.setSpacing(SPACE_SM)
        intro = QLabel(
            "Each row is one credential. GET API KEY opens the provider's own "
            "page, PASTE drops the clipboard straight in and tests it, and TEST "
            "makes one real request — a key is never shown as working because it "
            "merely exists.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        kv.addWidget(intro)
        hub = self._hub_module()
        if hub is None:
            kv.addWidget(QLabel("The service hub module could not be loaded."))
        else:
            current_group = ""
            for meta in hub.key_targets():
                if meta["group"] != current_group:
                    current_group = str(meta["group"])
                    head = QLabel(current_group.upper())
                    head.setStyleSheet(
                        f"color:{ACCENT_TEXT};font:800 8pt {FONT_HEAD};"
                        "letter-spacing:2px;background:transparent;padding-top:6px;")
                    kv.addWidget(head)
                self._key_row(meta["field"], str(meta.get("field") or "").replace("_", " ").title(),
                              layout=kv)
        lay.addWidget(keys)

        # ── 6 · what's ready ───────────────────────────────────────────
        ready = HudGroupBox("6 · WHAT'S READY")
        rv = QVBoxLayout(ready)
        self._ready_strip = QLabel("Reading the setup state…")
        self._ready_strip.setWordWrap(True)
        self._ready_strip.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        rv.addWidget(self._ready_strip)
        row = QHBoxLayout()
        test_all = QPushButton("TEST EVERYTHING")
        test_all.setToolTip("Test every credential in the background, one at a time.")
        test_all.clicked.connect(self._test_all_keys)
        row.addWidget(test_all)
        refresh = QPushButton("REFRESH STATUS")
        refresh.setToolTip("Re-read the cheap, offline state of every credential.")
        refresh.clicked.connect(self._refresh_key_states)
        row.addWidget(refresh)
        row.addStretch(1)
        save = QPushButton("SAVE NOW")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.save_settings)
        row.addWidget(save)
        rv.addLayout(row)
        lay.addWidget(ready)

        lay.addStretch(1)
        # The page is complete: index what the hand-built rows did not register.
        self._index_children("QUICK SETUP", scroll)
        self.tabs.addTab(scroll, "QUICK SETUP")

    def _mirror_field(self, edit: QLineEdit, label: str, placeholder: str):
        """A named setting that exists on two pages as one value."""
        edit.setPlaceholderText(placeholder)
        edit.textChanged.connect(lambda _t: self._mark_dirty())
        self._index_setting("QUICK SETUP", label, edit)
        return edit

    def _toggle_row(self, key: str, title: str, subtitle: str) -> QPushButton:
        """A switch row for Quick Setup: the whole row states and flips one flag."""
        feats = dict(self._data.get("features") or {})
        try:
            from config.settings import FEATURE_DEFAULTS as _FD
        except Exception:
            _FD = {}
        checked = bool(feats.get(key, _FD.get(key, True)))
        button = QPushButton()
        button.setCheckable(True)
        button.setChecked(checked)
        button.setMinimumHeight(CONTROL_LG)
        button.setStyleSheet(_toggle_css(CONTROL_LG))
        button.setToolTip(subtitle)

        def _paint(on: bool) -> None:
            button.setText(f"{'●' if on else '○'}  {title}" + ("" if on else "   — OFF"))

        button.toggled.connect(_paint)
        _paint(checked)

        def _flip(on: bool) -> None:
            self._qs_toggles[key] = bool(on)
            self._mark_dirty()
            # The same flag is shown as a checkbox on AUTOMATION (voice flags)
            # or SYSTEM (the rest); flipping it here has to flip it there, or
            # SAVE CHANGES would write the other tab's stale value back.
            for store in (getattr(self, "_voice_checks", {}),
                          getattr(self, "_feature_checks", {})):
                cb = (store or {}).get(key)
                if cb is not None and cb.isChecked() != on:
                    cb.blockSignals(True)
                    cb.setChecked(on)
                    cb.blockSignals(False)

        button.toggled.connect(_flip)
        self._qs_toggles[key] = checked
        self._index_setting("QUICK SETUP", title, button)
        return button

    def _test_all_keys(self) -> None:
        for field in list(self._key_rows):
            self._test_key(field)

    def _build_general_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "GENERAL  //  REACTOR · WINDOWS")

        appearance = HudGroupBox("APPEARANCE")
        form = QFormLayout(appearance)
        self.font = QComboBox()
        self.font.setToolTip("The UI face. Rajdhani is the shipped default.")
        fonts = ["Rajdhani", "Orbitron", "Share Tech Mono", "Exo 2", "Segoe UI", "Segoe UI Variable", "Consolas", "Cascadia Code", "JetBrains Mono", "Arial", "Tahoma"]
        self.font.addItems(fonts)
        self.font.setCurrentText(str(self._data.get("ui_font", "Rajdhani")))
        self._index_setting("GENERAL", "Font", self.font)
        form.addRow("Font", self.font)
        # The accent control is the SAME widget on both pages: Quick Setup shows
        # it first, and this row just places it here as well.
        if self.color_btn.parent() is None:
            form.addRow("Accent color", self.color_btn)
        quick = QHBoxLayout()
        for label, color in [("CYAN", ACCENT), ("RED", RED), ("GREEN", GREEN), ("GOLD", GOLD), ("WHITE", TEXT_BRIGHT)]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, c=color: self._set_color(c))
            quick.addWidget(b)
        form.addRow("Presets", quick)
        lay.addWidget(appearance)

        reactor = HudGroupBox("REACTOR / UI")
        rv = QVBoxLayout(reactor)
        self.ui_opacity = self._slider_row(rv, "Glass opacity", int(self._data.get("ui_opacity", 82)), 0, 100, "%")
        self.reactor_opacity = self._slider_row(rv, "Reactor opacity", int(self._data.get("reactor_opacity", 60)), 0, 100, "%")
        self.reactor_stroke_opacity = self._slider_row(rv, "Reactor stroke opacity", int(self._data.get("reactor_stroke_opacity", 100)), 0, 100, "%")
        self.anim_speed = self._slider_row(rv, "Animation speed", int(float(self._data.get("reactor_animation_speed", 1.0))*100), 25, 250, "%")
        self.eq_sens = self._slider_row(rv, "Equalizer sensitivity", int(float(self._data.get("equalizer_sensitivity", 1.0))*100), 10, 300, "%")
        self.reactor_size = self._slider_row(rv, "Arc Reactor size", int(self._data.get("compact_size", 176)), 120, 500, " px")
        # Resizing (or tweaking) auto-saves so the reactor applies + persists live.
        for _sl in (self.ui_opacity, self.reactor_opacity, self.reactor_stroke_opacity,
                    self.anim_speed, self.eq_sens, self.reactor_size):
            try:
                _sl.sliderReleased.connect(self._autosave_quiet)
            except Exception:
                pass
        lay.addWidget(reactor)

        behavior = HudGroupBox("WINDOW BEHAVIOR")
        bv = QVBoxLayout(behavior)
        self._compact = QCheckBox("Always keep the main HUD compact")
        # Loaded from the saved value. It used to be forced to True, so the
        # checkbox in SYSTEM → Features and this one silently overwrote each
        # other and the HUD always started compact whatever you picked.
        self._compact.setChecked(
            bool((self._data.get("features", {}) or {}).get("compact_mode", True)))
        # The compact Arc Reactor is the only HUD this build renders (the old
        # expanded layout was removed), so the switch cannot change anything.
        # It says so instead of pretending to.
        self._compact.setEnabled(False)
        self._compact.setToolTip(
            "The compact Arc Reactor is the only HUD in this build — the "
            "expanded layout was removed, so this cannot be turned off.")
        self._drag = QCheckBox("Allow the small Arc Reactor button to be dragged")
        self._drag.setChecked(bool(self._data.get("features", {}).get("drag_reactor", True)))
        self._remember = QCheckBox("Remember Arc Reactor position")
        self._remember.setChecked(bool(self._data.get("features", {}).get("remember_position", True)))
        self._remember.setToolTip("Save and restore the compact reactor window position.")
        for cb in (self._compact, self._drag, self._remember):
            bv.addWidget(cb)
        lay.addWidget(behavior)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "GENERAL")

    def _build_status_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(12)
        self._tab_intro(lay, "STATUS  //  ACTIVE SUBSYSTEMS & RUNTIME TELEMETRY")

        bar = QFrame()
        bar.setStyleSheet("background:rgba(1,24,38,130);border:1px solid rgba(55,150,181,80);border-radius:8px;")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 8, 12, 8)
        live_dot = QLabel("● SYSTEM ACTIVE")
        live_dot.setStyleSheet(f"color:{ACCENT};font:800 8pt 'Orbitron';letter-spacing:1px;background:transparent;")
        bl.addWidget(live_dot)
        bl.addStretch(1)
        ref_btn = QPushButton("⟳ REFRESH STATUS")
        ref_btn.setObjectName("PrimaryButton")
        ref_btn.setFixedHeight(28)
        ref_btn.setStyleSheet(f"background:rgba(0,96,125,140);color:{TEXT_BRIGHT};border:1px solid {ACCENT};border-radius:5px;font:700 7.5pt 'Orbitron';padding:0 12px;")
        bl.addWidget(ref_btn)
        lay.addWidget(bar)

        audio_grp = HudGroupBox("VOICE & AUDIO SUBSYSTEM")
        al = QVBoxLayout(audio_grp)
        self._status_voice_lbl = QLabel()
        self._status_voice_lbl.setStyleSheet(f"color:{TEXT_BRIGHT};font:8pt 'Consolas','Share Tech Mono';background:transparent;")
        al.addWidget(self._status_voice_lbl)
        lay.addWidget(audio_grp)

        vision_grp = HudGroupBox("VISION & OPTICAL SUBSYSTEM")
        vl = QVBoxLayout(vision_grp)
        self._status_vision_lbl = QLabel()
        self._status_vision_lbl.setStyleSheet(f"color:{TEXT_BRIGHT};font:8pt 'Consolas','Share Tech Mono';background:transparent;")
        vl.addWidget(self._status_vision_lbl)
        lay.addWidget(vision_grp)

        ai_grp = HudGroupBox("AI CORE & INTELLIGENCE")
        ail = QVBoxLayout(ai_grp)
        self._status_ai_lbl = QLabel()
        self._status_ai_lbl.setStyleSheet(f"color:{TEXT_BRIGHT};font:8pt 'Consolas','Share Tech Mono';background:transparent;")
        ail.addWidget(self._status_ai_lbl)
        lay.addWidget(ai_grp)

        perm_grp = HudGroupBox("AUTOMATION & SECURITY MATRIX")
        pl = QVBoxLayout(perm_grp)
        self._status_security_lbl = QLabel()
        self._status_security_lbl.setStyleSheet(f"color:{TEXT_BRIGHT};font:8pt 'Consolas','Share Tech Mono';background:transparent;")
        pl.addWidget(self._status_security_lbl)
        lay.addWidget(perm_grp)

        # Live runtime devices + the on/off state of the key subsystems. Read
        # from the same sources the runtime uses, so this page cannot disagree
        # with what J.A.R.V.I.S. is actually doing.
        dev_grp = HudGroupBox("RUNTIME DEVICES & SUBSYSTEMS")
        dl = QVBoxLayout(dev_grp)
        self._status_devices_lbl = QLabel()
        self._status_devices_lbl.setStyleSheet(f"color:{TEXT_BRIGHT};font:8pt 'Consolas','Share Tech Mono';background:transparent;")
        dl.addWidget(self._status_devices_lbl)
        lay.addWidget(dev_grp)

        lay.addStretch(1)

        def _onoff(value, default=True):
            return "Enabled" if bool(default if value is None else value) else "DISABLED"

        def _refresh_status():
            d = self._data or {}
            feats = d.get("features", {}) if isinstance(d.get("features", {}), dict) else {}
            vpipe = d.get("voice_output_mode") or d.get("voice", {}).get("defaultPipeline", "gemini")
            pipe_display = "Gemini Native Audio (Live)" if vpipe == "gemini" else ("Gemini → Piper ONNX" if vpipe == "gemini_piper" else "Local engine")
            active_p = d.get("voice", {}).get("activeProvider", "gemini")
            vname = d.get("voice_name", "Charon")
            spk_dev = d.get("speaker_device") or "Default System Output"
            mic_dev = d.get("mic_device") or "Default System Microphone"
            barge = _onoff(feats.get("barge_in"), True)
            mic_on = _onoff(feats.get("mic_access"), True)

            self._status_voice_lbl.setText(
                f"• Voice Output:            {pipe_display} [ACTIVE]\n"
                f"• Active Provider:         {active_p}\n"
                f"• Voice / Persona:         {vname}\n"
                f"• Microphone Access:       {mic_on}\n"
                f"• Speaker Output:          {spk_dev}\n"
                f"• Microphone Input:        {mic_dev}\n"
                f"• Barge-In / Interrupt:    {barge}"
            )

            # Webcam: enabled only when BOTH the vision master switch and the
            # Settings camera permission allow it — exactly the runtime rule.
            cam_perm = bool(feats.get("camera_access", True))
            cam_master = True
            try:
                from vision.config import load_vision_config
                cam_master = bool(getattr(load_vision_config(), "camera_enabled", True))
            except Exception:
                cam_master = True
            cam_state = "Enabled" if (cam_perm and cam_master) else "DISABLED"
            cam_note = ""
            if not cam_perm:
                cam_note = " (permission revoked in Settings)"
            elif not cam_master:
                cam_note = " (vision master switch off)"

            self._status_vision_lbl.setText(
                f"• Webcam:                  {cam_state}{cam_note}\n"
                f"• Webcam Device:           {self._camera_device_text()}\n"
                f"• Face Detection:          {_onoff(feats.get('face_tracking'), True)}\n"
                f"• Object Recognition:      {_onoff(feats.get('object_detection'), True)}\n"
                f"• Optical Engine:          OpenCV + MediaPipe"
            )

            provider = d.get("ai_provider", "gemini")
            model = d.get("ai_model") or "(provider default)"
            key_status = "Configured" if bool(d.get("gemini_api_key")) else "Not Set"
            proactive = _onoff(feats.get("proactive_mode"), True)
            self._status_ai_lbl.setText(
                f"• AI Provider:             {str(provider).upper()}\n"
                f"• Live Model:              {model}\n"
                f"• API Key:                 {key_status}\n"
                f"• Proactive Mode:          {proactive}\n"
                f"• Resumption / Context:    Active"
            )

            coding_ok = bool(feats.get("coding_agents", True))
            auto_code = ("Enabled (direct)" if feats.get("auto_approve_coding")
                         else "Approval required")
            mode = "high_risk_only"
            try:
                from security.approvals import approval_mode
                mode = approval_mode()
            except Exception:
                pass
            if not coding_ok:
                auto_code = "DISABLED (coding agents off)"

            defender_line = "not detected on this system"
            try:
                from security import malware_scanner
                ds = malware_scanner.get_defender_status()
                if ds.get("active"):
                    rt = "Active" if ds.get("realtime_protection") else "OFF"
                    av = "Active" if ds.get("antivirus_enabled") else "Disabled"
                    defender_line = f"Windows Defender (antivirus: {av}, real-time: {rt})"
            except Exception:
                pass

            # PC automation runs on Agent TARS; the state below comes from a
            # real probe, so "Enabled" is never shown when it is unusable.
            tars_line = "not checked"
            try:
                from capabilities import agent_tars, tars_server
                st = agent_tars.status()
                if not st.get("enabled"):
                    state_s = "Disabled"
                elif not st.get("available"):
                    state_s = "Unavailable — not installed"
                elif not st.get("usable"):
                    state_s = "Installed (not ready)"
                else:
                    state_s = "Enabled"
                ver = st.get("version") or "?"
                connected = "Connected" if tars_server.health() else "Disconnected (idle)"
                tars_line = f"{state_s} · {connected} · v{ver}"
                if st.get("missing"):
                    tars_line += " — " + "; ".join(str(m)[:70] for m in st["missing"][:2])
            except Exception as exc:
                tars_line = f"unavailable ({type(exc).__name__})"

            # The local controllers (Playwright · pywinauto · UI-TARS · PyAutoGUI)
            # report their own real probe state, one line each, so "Enabled" is
            # never shown for something that cannot actually act.
            local_lines = []
            try:
                from capabilities import router as _cap_router
                for _key, _label in (("desktop", "Local PC (pywinauto/PyAutoGUI)"),
                                     ("playwright", "Browser (Playwright)"),
                                     ("uitars_visual", "Visual (UI-TARS/Gemini)")):
                    _info = (_cap_router.local_status() or {}).get(_key) or {}
                    if _info.get("enabled") is False:
                        _state = "Off in Settings"
                    elif _info.get("usable"):
                        _state = "Ready"
                    else:
                        _state = "Unavailable"
                    local_lines.append(f"• {_label:<25}{_state}")
            except Exception as exc:
                local_lines.append(f"• Local controllers:      unavailable ({type(exc).__name__})")

            self._status_security_lbl.setText(
                f"• Coding Execution:        {auto_code}\n"
                f"• Approval Mode:           {mode}\n"
                f"• Malware Protection:      {defender_line}\n"
                f"• Removal Gate:            On-screen CONFIRM required\n"
                f"• PC Automation (TARS):    {tars_line}\n"
                + "\n".join(local_lines)
            )

            def _row(label, value, default=True):
                return f"• {label:<25}{value}"

            self._status_devices_lbl.setText(
                _row("Wake Word:", _onoff(feats.get("wake_word"), True)) + "\n"
                + _row("Hands-Free Listening:", _onoff(feats.get("hands_free"), True)) + "\n"
                + _row("Web Browser (WebView):", _onoff(feats.get("webview"), True)) + "\n"
                + _row("Proactive Speaking:", _onoff(feats.get("proactive_speaking"), False)) + "\n"
                + _row("Autostart:", _onoff(feats.get("autostart"), False)) + "\n"
                + _row("Config File:", str(self._config_path.name))
            )

        self._refresh_status_cb = _refresh_status
        ref_btn.clicked.connect(_refresh_status)
        _refresh_status()
        self.tabs.addTab(scroll, "STATUS")

    def _camera_device_text(self) -> str:
        """Which webcam is in use — configured index, else auto-detected.

        Probing cameras opens devices, so it runs on a worker thread and the
        label is re-rendered when it answers; the UI never blocks on it.
        """
        configured = "auto"
        try:
            from vision.config import load_vision_config
            configured = load_vision_config().camera_index
        except Exception:
            configured = "auto"
        try:
            return f"Camera index {int(configured)} (configured)"
        except (TypeError, ValueError):
            pass
        if getattr(self, "_camera_probe_done", None) is not None:
            return f"Camera index {self._camera_probe_done} (auto-detected)"
        if not getattr(self, "_camera_probe_running", False):
            self._camera_probe_running = True
            try:
                import threading as _threading
                _threading.Thread(target=self._probe_camera_async,
                                  name="cam-status-probe", daemon=True).start()
            except Exception:
                self._camera_probe_running = False
                return "Auto (probed at capture time)"
        return "Auto-detecting…"

    def _probe_camera_async(self) -> None:
        idx = None
        try:
            from vision.camera import auto_detect_camera_index
            from vision.config import load_vision_config
            _v = load_vision_config()
            _pref = _v.effective_camera_index()
            idx = auto_detect_camera_index(_pref if _pref is not None and _pref >= 0 else None)
        except Exception:
            idx = None
        self._camera_probe_done = idx
        self._camera_probe_running = False
        cb = getattr(self, "_refresh_status_cb", None)
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    def _build_voice_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "VOICE  //  PIPELINE · PROVIDERS · TESTS")
        voice = HudGroupBox("VOICE ASSISTANT")
        vv = QVBoxLayout(voice)

        # ── 0 · Pipeline (who renders the reply, provider-independent) ──
        pipeline_box = HudGroupBox("0 · PIPELINE")
        ef = QFormLayout(pipeline_box)
        self.voice_output_mode = QComboBox()
        self.voice_output_mode.addItem("Gemini Native Audio", userData="gemini")
        self.voice_output_mode.addItem("Gemini → Piper ONNX", userData="gemini_piper")
        self.voice_output_mode.addItem("Local Piper Only", userData="local")
        # Preselect what the RUNTIME resolves, not the raw stored string: the
        # pipeline recorded by SAVE AS DEFAULT (voice.defaultPipeline) wins,
        # then the stored mode with its one-time lifts applied (see
        # normalize_voice_output_mode).
        _cur_vom = str(self._data.get("voice_output_mode", "") or "")
        try:
            from core.hybrid_voice import resolve_voice_mode
            try:
                from voice.providers_config import providers_block as _pb
                _dp = str(_pb(self._data).get("defaultPipeline", "") or "")
            except Exception:
                _dp = ""
            if _dp:
                _cur_vom, _ = resolve_voice_mode({**self._data, "voice_output_mode": _dp})
            else:
                _cur_vom, _ = resolve_voice_mode(self._data)
        except Exception:
            _cur_vom = _cur_vom or "gemini"
        _vi = self.voice_output_mode.findData(_cur_vom)
        self.voice_output_mode.setCurrentIndex(_vi if _vi >= 0 else 0)
        self.voice_output_mode.setToolTip(
            "Gemini Native Audio (default): the realtime Gemini Live voice speaks the "
            "reply itself — lowest latency. Gemini → Piper ONNX: Gemini handles the "
            "conversation and JARVIS speaks the reply through the local Piper voice — "
            "Gemini's own audio is not played. Local Piper Only: every reply is spoken "
            "locally, by the selected provider."
        )
        ef.addRow("Voice output", self.voice_output_mode)
        # SAVE persists the selected pipeline for this install (and syncs the
        # Gemini renderer so the runtime actually follows this combo);
        # SAVE AS DEFAULT additionally records it as the startup default for
        # fresh sessions (config ▸ voice.defaultPipeline, read by load()).
        pipe_row = QHBoxLayout()
        pipe_row.setSpacing(6)
        pipe_save = QPushButton("SAVE")
        pipe_save.setToolTip("Persist the selected pipeline as this install's voice renderer.")
        pipe_save.clicked.connect(self._save_pipeline)
        pipe_row.addWidget(pipe_save)
        pipe_default = QPushButton("SAVE AS DEFAULT")
        pipe_default.setToolTip("Save the pipeline and record it as the default every new session starts on.")
        pipe_default.clicked.connect(self._save_pipeline_as_default)
        pipe_row.addWidget(pipe_default)
        pipe_row.addStretch(1)
        ef.addRow("", pipe_row)
        self._pipeline_status = QLabel("")
        self._pipeline_status.setWordWrap(True)
        self._pipeline_status.setStyleSheet(f"color:{TEXT_DIM};font:7pt 'Exo 2';background:transparent;")
        ef.addRow("", self._pipeline_status)
        test_voice = QPushButton("TEST VOICE LINK")
        test_voice.setToolTip("Ask the active J.A.R.V.I.S session to speak a short professional greeting.")
        test_voice.clicked.connect(self._test_voice_link)
        ef.addRow("", test_voice)
        vv.addWidget(pipeline_box)

        # ── 1 · Provider selector + one isolated panel per provider ──
        provider_box = HudGroupBox("1 · SPEECH PROVIDER (only the selected provider's settings apply)")
        pf = QFormLayout(provider_box)
        self.provider_selector = QComboBox()
        try:
            from voice.providers_config import PROVIDER_IDS as _PIDS
        except Exception:
            _PIDS = ["fishAudio", "elevenLabs", "gemini", "piperOnnx", "edgeTts", "kokoro", "sapi"]
        self._PROVIDER_LABELS = {
            "fishAudio": "Fish Audio",
            "elevenLabs": "ElevenLabs",
            "gemini": "Gemini",
            "piperOnnx": "Piper / ONNX",
            "edgeTts": "Edge TTS",
            "kokoro": "Kokoro",
            "sapi": "Windows SAPI",
        }
        for _pid in _PIDS:
            self.provider_selector.addItem(self._PROVIDER_LABELS.get(_pid, _pid), userData=_pid)
        try:
            from voice.providers_config import active_provider as _active_provider
            _cur_provider = _active_provider(self._data)
        except Exception:
            _cur_provider = str(self._data.get("tts_engine", "edgetts"))
        _pi = self.provider_selector.findData(_cur_provider)
        self.provider_selector.setCurrentIndex(_pi if _pi >= 0 else 4)  # edgeTts
        self.provider_selector.setToolTip(
            "Who renders local speech. Each provider keeps its OWN settings — "
            "switching or saving one never touches another."
        )
        pf.addRow("Provider", self.provider_selector)

        self.provider_stack = QStackedWidget()
        self._provider_panels = {}
        self._build_fish_panel(); self.provider_stack.addWidget(self._provider_panels["fishAudio"])
        self._build_eleven_panel(); self.provider_stack.addWidget(self._provider_panels["elevenLabs"])
        self._build_gemini_panel(); self.provider_stack.addWidget(self._provider_panels["gemini"])
        self._build_piper_panel(); self.provider_stack.addWidget(self._provider_panels["piperOnnx"])
        self._build_edge_panel(); self.provider_stack.addWidget(self._provider_panels["edgeTts"])
        self._build_kokoro_panel(); self.provider_stack.addWidget(self._provider_panels["kokoro"])
        self._build_sapi_panel(); self.provider_stack.addWidget(self._provider_panels["sapi"])
        self.provider_selector.currentIndexChanged.connect(self._on_provider_selected)
        self._on_provider_selected(self.provider_selector.currentIndex())
        pf.addRow("", self.provider_stack)
        vv.addWidget(provider_box)
        lay.addWidget(voice)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "VOICE")

    # -- per-provider panels -------------------------------------------------
    def _panel_buttons(self, pid: str, with_refresh: bool = False) -> QFormLayout:
        """The shared Refresh/Test/Save/Save-as-Default row for one panel."""
        form = self._provider_forms[pid]
        row = QHBoxLayout()
        if with_refresh:
            refresh = QPushButton("REFRESH")
            refresh.setToolTip(f"Fetch the {self._PROVIDER_LABELS[pid]} voice list into this panel only.")
            refresh.clicked.connect(lambda _=False, p=pid: self._refresh_provider_voices(p))
            row.addWidget(refresh)
        test = QPushButton("TEST VOICE")
        test.setToolTip(f"Speak a short test line through {self._PROVIDER_LABELS[pid]} with the values currently in this panel.")
        test.clicked.connect(lambda _=False, p=pid: self._test_provider(p))
        row.addWidget(test)
        save = QPushButton("SAVE")
        save.setToolTip(f"Validate and save ONLY the {self._PROVIDER_LABELS[pid]} settings, and make it the active provider.")
        save.clicked.connect(lambda _=False, p=pid: self._save_provider_panel(p, as_default=False))
        row.addWidget(save)
        default = QPushButton("SAVE AS DEFAULT")
        default.setToolTip(f"Save {self._PROVIDER_LABELS[pid]} and store provider + voice as the validated default pair.")
        default.clicked.connect(lambda _=False, p=pid: self._save_provider_panel(p, as_default=True))
        row.addWidget(default)
        form.addRow("", row)
        status = QLabel("")
        status.setWordWrap(True)
        status.setStyleSheet(f"color:{TEXT_DIM};font:7pt 'Exo 2';background:transparent;")
        form.addRow("", status)
        self._provider_status[pid] = status
        return form

    def _build_fish_panel(self):
        box = HudGroupBox("FISH AUDIO (cloud)")
        ff = QFormLayout(box)
        self._provider_forms = getattr(self, "_provider_forms", {})
        self._provider_status = getattr(self, "_provider_status", {})
        self._provider_forms["fishAudio"] = ff
        self.fish_api_key = QLineEdit(str(self._data.get("fish_audio_api_key", "")))
        self.fish_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.fish_api_key.setPlaceholderText("Fish Audio API key")
        ff.addRow("API key", self.fish_api_key)
        self.fish_model_id = QLineEdit(str(self._data.get("fish_audio_model_id", "s2-pro")))
        self.fish_model_id.setPlaceholderText("Backend: s1 or s2-pro")
        self.fish_model_id.setToolTip("Fish Audio backend model (e.g. s1, s2.1-pro). A voice ID typed here is auto-detected as the voice.")
        ff.addRow("Model", self.fish_model_id)
        self.fish_voice_id = QLineEdit(str(self._data.get("fish_audio_voice_id", "")))
        self.fish_voice_id.setPlaceholderText("Reference voice ID")
        ff.addRow("Voice ID", self.fish_voice_id)
        self.fish_endpoint = QLineEdit(str(self._data.get("fish_audio_endpoint", DEFAULTS["fish_audio_endpoint"])))
        ff.addRow("Endpoint", self.fish_endpoint)
        fish_row = QHBoxLayout()
        self.fish_format = QComboBox()
        self.fish_format.addItems(["mp3", "wav", "opus"])
        self.fish_format.setCurrentText(str(self._data.get("fish_audio_format", "mp3")))
        fish_row.addWidget(self.fish_format)
        self.fish_latency = QComboBox()
        self.fish_latency.addItems(["normal", "balanced"])
        _lat = str(self._data.get("fish_audio_latency", "normal"))
        self.fish_latency.setCurrentText("balanced" if _lat == "low" else _lat)
        fish_row.addWidget(self.fish_latency)
        ff.addRow("Format / latency", fish_row)
        self._panel_buttons("fishAudio", with_refresh=True)
        self._provider_panels["fishAudio"] = box

    def _build_eleven_panel(self):
        box = HudGroupBox("ELEVENLABS (cloud)")
        elf = QFormLayout(box)
        self._provider_forms["elevenLabs"] = elf
        self.elevenlabs_api_key = QLineEdit(str(self._data.get("elevenlabs_api_key", "")))
        self.elevenlabs_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.elevenlabs_api_key.setPlaceholderText("sk_… ElevenLabs API key")
        elf.addRow("API key", self.elevenlabs_api_key)
        self.elevenlabs_voice_id = QLineEdit(str(self._data.get("elevenlabs_voice_id", DEFAULTS["elevenlabs_voice_id"])))
        self.elevenlabs_voice_id.setPlaceholderText("20-char voice ID (default Adam)")
        self.elevenlabs_voice_id.setToolTip("ElevenLabs voice ID — 20 characters, e.g. pNInz6obpgDQGcFmaJgB (Adam). NOT an Edge name like en-US-GuyNeural.")
        elf.addRow("Voice ID", self.elevenlabs_voice_id)
        self.elevenlabs_model_id = QComboBox()
        self.elevenlabs_model_id.addItems(["eleven_v3", "eleven_multilingual_v2", "eleven_flash_v2_5"])
        self.elevenlabs_model_id.setCurrentText(str(self._data.get("elevenlabs_model_id", DEFAULTS["elevenlabs_model_id"])))
        elf.addRow("Model", self.elevenlabs_model_id)
        self._panel_buttons("elevenLabs", with_refresh=True)
        self._provider_panels["elevenLabs"] = box

    def _build_gemini_panel(self):
        box = HudGroupBox("GEMINI (cloud · conversation voice)")
        gf = QFormLayout(box)
        self._provider_forms["gemini"] = gf
        self.live_voice = QComboBox()
        self.live_voice.addItems(["Charon", "Puck", "Kore", "Fenrir", "Aoede"])
        self.live_voice.setCurrentText(str(self._data.get("voice_name", "Charon")))
        self.live_voice.setToolTip("Gemini Live voice used by the real-time voice assistant. Belongs to the Gemini provider only.")
        gf.addRow("Live assistant voice", self.live_voice)
        self._panel_buttons("gemini")
        self._provider_panels["gemini"] = box

    def _build_piper_panel(self):
        box = HudGroupBox("PIPER / ONNX (local · offline)")
        onf = QFormLayout(box)
        self._provider_forms["piperOnnx"] = onf
        self.onnx_voice = QComboBox()
        try:
            from core.tts import list_onnx_voices
            self._onnx_voices = list_onnx_voices()
        except Exception:
            self._onnx_voices = []
        for v in self._onnx_voices:
            self.onnx_voice.addItem(v["label"], userData=v)
        self.onnx_voice.setToolTip(
            "Voices discovered in config/voices — a Piper-format .onnx model plus "
            "its sidecar .json. Drop a new pair in that folder and restart Settings."
        )
        onf.addRow("Piper voice", self.onnx_voice)
        self.onnx_execution_provider = QComboBox()
        self.onnx_execution_provider.addItem("CPU", userData="cpu")
        self.onnx_execution_provider.addItem("GPU", userData="gpu")
        self.onnx_execution_provider.addItem("Auto", userData="auto")
        _cur_ep = str(self._data.get("onnx_execution_provider", "cpu") or "cpu")
        _epi = self.onnx_execution_provider.findData(_cur_ep)
        self.onnx_execution_provider.setCurrentIndex(_epi if _epi >= 0 else 0)
        self.onnx_execution_provider.setToolTip(
            "ONNX Runtime execution provider for the Piper voice. CPU always works. "
            "GPU uses the installed accelerator; if it is unavailable the reason is "
            "logged and CPU is used instead."
        )
        onf.addRow("Execution", self.onnx_execution_provider)
        self.onnx_model_path = QLineEdit(str(self._data.get("onnx_voice_model", "") or ""))
        self.onnx_model_path.setPlaceholderText("config/voices/jarvis-high.onnx (or any absolute path)")
        self.onnx_model_path.setToolTip("Optional override — any local .onnx voice model path.")
        onf.addRow("Model path", self.onnx_model_path)
        self.onnx_speaker = QLineEdit(str(self._data.get("onnx_voice_speaker", "default")))
        self.onnx_speaker.setPlaceholderText("default")
        self.onnx_speaker.setToolTip("Speaker name/index for multi-speaker ONNX models. jarvis-high is single-voice: leave 'default'.")
        onf.addRow("Speaker", self.onnx_speaker)
        onnx_prev = QPushButton("TEST PIPER VOICE")
        onnx_prev.setToolTip(
            "Load the selected Piper voice with the selected execution provider and "
            "speak a short line — without saving or switching the active mode."
        )
        onnx_prev.clicked.connect(self._test_piper_voice)
        onf.addRow("", onnx_prev)
        # _test_piper_voice / _on_onnx_voice_picked report through this label.
        # It was missing, so pressing TEST PIPER VOICE raised AttributeError.
        self.onnx_status = QLabel("")
        self.onnx_status.setWordWrap(True)
        self.onnx_status.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        onf.addRow("", self.onnx_status)
        self._panel_buttons("piperOnnx")
        self._provider_panels["piperOnnx"] = box
        # Preselect the currently configured ONNX voice, if any.
        try:
            _cur = (str(self._data.get("onnx_voice_model", "")).replace("\\", "/").split("/")[-1] or "")
            for _i in range(self.onnx_voice.count()):
                if self.onnx_voice.itemData(_i) and self.onnx_voice.itemData(_i)["model"].replace("\\", "/").endswith(_cur):
                    self.onnx_voice.setCurrentIndex(_i)
                    break
        except Exception:
            pass
        self.onnx_voice.currentIndexChanged.connect(self._on_onnx_voice_picked)
        self._on_onnx_voice_picked(self.onnx_voice.currentIndex())  # fill path field now

    def _build_edge_panel(self):
        box = HudGroupBox("EDGE TTS (free · Microsoft · online)")
        df = QFormLayout(box)
        self._provider_forms["edgeTts"] = df
        self.tts_voice = QLineEdit(str(self._data.get("tts_voice", "en-GB-RyanNeural")))
        self.tts_voice.setPlaceholderText("e.g. en-GB-RyanNeural")
        df.addRow("Edge voice", self.tts_voice)
        self._panel_buttons("edgeTts")
        self._provider_panels["edgeTts"] = box

    def _build_kokoro_panel(self):
        box = HudGroupBox("KOKORO (offline · neural)")
        kf = QFormLayout(box)
        self._provider_forms["kokoro"] = kf
        self.kokoro_voice = QComboBox()
        self.kokoro_voice.addItems([
            "af_heart", "af_bella", "af_nicole", "am_adam", "am_michael",
            "bf_emma", "bm_george", "jf_alpha", "zf_xiaobei", "ef_eva",
        ])
        try:
            from voice.providers_config import providers_block as _pb
            _kv = str(_pb(self._data).get("providers", {}).get("kokoro", {}).get("voice", "af_heart"))
        except Exception:
            _kv = "af_heart"
        _ki = self.kokoro_voice.findText(_kv)
        self.kokoro_voice.setCurrentIndex(_ki if _ki >= 0 else 0)
        self.kokoro_voice.setToolTip("Offline neural voice. The ~330 MB model downloads on first use.")
        kf.addRow("Kokoro voice", self.kokoro_voice)
        self._panel_buttons("kokoro")
        self._provider_panels["kokoro"] = box

    def _build_sapi_panel(self):
        box = HudGroupBox("WINDOWS SAPI (offline · built-in)")
        sf = QFormLayout(box)
        self._provider_forms["sapi"] = sf
        self.sapi_voice = QComboBox()
        try:
            import pyttsx3 as _pt3
            _eng = _pt3.init()
            for _v in _eng.getProperty("voices"):
                self.sapi_voice.addItem(str(getattr(_v, "name", _v.id)), userData=str(getattr(_v, "id", "")))
        except Exception:
            self.sapi_voice.addItem("(no SAPI voices found)", userData="")
        _cur_sapi = str(self._data.get("sapi_voice", "") or "")
        if _cur_sapi:
            _si = self.sapi_voice.findData(_cur_sapi)
            if _si >= 0:
                self.sapi_voice.setCurrentIndex(_si)
        self.sapi_voice.setToolTip("Voices installed in Windows. Instant start, zero network — the fastest engine.")
        sf.addRow("Installed voices", self.sapi_voice)
        sapi_prev = QPushButton("PREVIEW SAPI VOICE")
        sapi_prev.setToolTip("Hear the selected Windows voice immediately.")
        sapi_prev.clicked.connect(self._preview_sapi_voice)
        sf.addRow("", sapi_prev)
        self._panel_buttons("sapi")
        self._provider_panels["sapi"] = box

    def _on_provider_selected(self, index: int) -> None:
        """Show exactly the selected provider's panel (hide — not dim — the rest)."""
        try:
            self.provider_stack.setCurrentIndex(max(0, int(index)))
        except Exception:
            pass

    def _build_personality_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "PERSONALITY  //  IDENTITY · DELIVERY · VOICE TUNE")
        ident = HudGroupBox("IDENTITY")
        form = QFormLayout(ident)
        self.assistant_name = QLineEdit(str(self._data.get("assistant_name", "JARVIS") or "JARVIS"))
        self.assistant_name.setPlaceholderText("JARVIS")
        form.addRow("Assistant name", self.assistant_name)
        self.user_name = QLineEdit(str(self._data.get("user_name", "") or ""))
        self.user_name.setPlaceholderText("How should JARVIS address you? (empty = sir)")
        form.addRow("Your name", self.user_name)
        lay.addWidget(ident)

        delivery = HudGroupBox("DELIVERY")
        dv = QVBoxLayout(delivery)
        form2 = QFormLayout()
        self.voice_mode = QComboBox()
        self.voice_mode.addItems(["standard", "executive", "friendly", "focus", "emergency", "cinematic"])
        self.voice_mode.setCurrentText(str(self._data.get("voice_mode", "standard")))
        form2.addRow("Delivery mode", self.voice_mode)
        dv.addLayout(form2)
        self.voice_speed = self._slider_row(dv, "Speaking speed", int(float(self._data.get("voice_speed", 1.0)) * 100), 50, 200, "%")
        self.voice_pitch = self._slider_row(dv, "Voice pitch", int(float(self._data.get("voice_pitch", 0.0)) * 10), -20, 20, " st")
        self.voice_volume = self._slider_row(dv, "Voice volume", int(float(self._data.get("voice_volume", 1.0)) * 100), 0, 100, "%")
        # ── Extended voice-style sections (voice/delivery_style.py) ──────
        # They live INSIDE the Delivery group: same panel width and overall
        # proportions; the sections scroll internally with the scrollbar
        # visually hidden (mouse-wheel still scrolls). Collapsed sections
        # keep the panel at its familiar size.
        _style_scroll = QScrollArea()
        _style_scroll.setWidgetResizable(True)
        _style_scroll.setFrameShape(QFrame.Shape.NoFrame)
        _style_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _style_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _style_scroll.setMaximumHeight(286)
        _style_scroll.setViewportMargins(0, 0, 0, 0)
        _style_inner = QWidget()
        _style_lay = QVBoxLayout(_style_inner)
        _style_lay.setContentsMargins(0, 0, 0, 0)
        _style_lay.setSpacing(4)
        self._build_style_sections(_style_lay)
        _style_lay.addStretch(1)
        _style_scroll.setWidget(_style_inner)
        dv.addWidget(_style_scroll)
        lay.addWidget(delivery)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "PERSONALITY")

    def _flag_checkbox(self, store: dict, key: str, label: str, default: bool = True,
                       tip: str = "", saved: dict | None = None) -> QCheckBox:
        """A feature-flag checkbox.

        *saved* is the configuration the flag actually comes from. Without it
        the box could only show the built-in default, so the panel would
        contradict the running system — and the next SAVE would silently write
        those defaults back over the user's stored flags."""
        cb = QCheckBox(label)
        source = saved if isinstance(saved, dict) else store
        cb.setChecked(bool(source.get(key, default)))
        if tip:
            cb.setToolTip(tip)
        store[key] = cb
        return cb

    def _build_automation_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "AUTOMATION  //  VOICE HANDS-FREE · COMPUTER · TASKS")
        self._voice_checks = {}
        voice_features = self._data.get("features", {}) or {}
        hands = HudGroupBox("VOICE AUTOMATION")
        hv = QVBoxLayout(hands)
        for key, label in [
            ("wake_word", 'Wake word: "Hey Jarvis"'),
            ("hands_free", "Hands-free voice mode"),
            ("push_to_talk", "Push-to-talk mode (F6)"),
            ("voice_activity_detection", "Voice activity detection"),
            ("realtime_stt", "Realtime STT (live transcript in the command box as you speak)"),
            ("voice_interruption", "Voice interruption"),
            ("barge_in", "Barge-in while JARVIS is speaking"),
            ("background_listening", "Background listening"),
            ("always_on", "Always-on mode"),
            ("voice_command_recognition", "Voice command recognition"),
            ("proactive_speaking", "Speak without being spoken to (proactive voice)"),
            # Speed & narration. Defaults come from FEATURE_DEFAULTS, and the
            # saved values win — these three change behaviour the user can hear
            # within one reply, so they must be switchable without a restart.
            ("narrator", "Narrate actions out loud (say what is being done, as it is done)"),
            ("realtime_low_latency", "Low-latency voice turns (end-of-speech detected locally)"),            ("tts_streaming",
             "Stream cloud voices sentence by sentence (Fish Audio / ElevenLabs)"),
        ]:
            hv.addWidget(self._flag_checkbox(
                self._voice_checks, key, label,
                key not in {"push_to_talk", "always_on", "proactive_speaking"},
                ("" if key != "realtime_stt" else
                 "ON: every in-progress speech result is rendered into the ONE "
                 "existing command textbox (with the same intelligent "
                 "correction the final transcript gets).\n"
                 "OFF: no live/interim display work at all — the microphone, "
                 "wake word, VAD and the FINAL transcript (chat log, memory, "
                 "dashboard, command pipeline) keep working exactly as before. "
                 "This never turns the microphone off."),
                saved=voice_features))
        for _k, _tip in (
            ("narrator",
             "J.A.R.V.I.S. speaks one short sentence while he works — \"Sir, "
             "I'm opening the page.\" Every line comes from a real event and "
             "the engine's own progress text; nothing is scripted, and a step "
             "the engine never reported is never narrated.\n"
             "OFF is pure silence — the microphone, the replies and every "
             "other voice behaviour are unaffected."),
            ("realtime_low_latency",
             "The client tells the Live session the moment your sentence ends, "
             "instead of waiting out the service's ~800 ms silence window. "
             "This is the documented hybrid-VAD arrangement: server-side speech "
             "START detection (so the first syllable is never clipped) plus "
             "client-side end-of-speech at the 500 ms minimum the docs give.\n"
             "OFF = previous behaviour: server-side VAD only."),
            ("tts_streaming",
             "Cloud voices are requested and played sentence by sentence, so "
             "speech starts at the first sentence's time-to-first-byte instead "
             "of after the whole reply is rendered. It also makes an "
             "interruption able to cut a cloud voice off mid-reply.\n"
             "If a provider rejects streaming, the previous whole-reply path is "
             "used automatically."),
        ):
            _cb = (self._voice_checks or {}).get(_k)
            if _cb is not None:
                _cb.setToolTip(_tip)
        # Extra wake words — matched IN ADDITION to the built-ins. Comma- or
        # line-separated, e.g.: jarvis, j.a.r.v.i.s, friday, alfred
        wake_row = QHBoxLayout()
        wake_lab = QLabel("Extra wake words")
        wake_lab.setMinimumWidth(185)
        self.wake_words = QLineEdit(
            ", ".join(str(w) for w in (self._data.get("wake_words") or []) )
        )
        self.wake_words.setPlaceholderText("jarvis, j.a.r.v.i.s, computer, friday …")
        self.wake_words.setToolTip(
            "Comma-separated extra wake words JARVIS answers to, matched in "
            "addition to the built-ins (hey jarvis / okay jarvis / computer)."
        )
        wake_row.addWidget(wake_lab)
        wake_row.addWidget(self.wake_words, 1)
        hv.addLayout(wake_row)
        lay.addWidget(hands)

        computer = HudGroupBox("AWARENESS & ACCESS")
        cv = QVBoxLayout(computer)
        for key, label in [
            ("desktop_awareness", "Desktop awareness and system status"),
            ("screen_understanding", "Screen capture and visual understanding"),
            ("terminal_control", "Execute terminal commands"),
            ("file_control", "Control files and folders"),
        ]:
            cv.addWidget(self._flag_checkbox(self._voice_checks, key, label, True,
                                             saved=voice_features))
        lay.addWidget(computer)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "AUTOMATION")

    def _build_permissions_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "PERMISSIONS  //  STARTUP · DEVICES · ACCESS")
        self._perm_checks = {}
        perms = HudGroupBox("SYSTEM ACCESS")
        pv = QVBoxLayout(perms)
        self._autostart = QCheckBox("Auto-start J.A.R.V.I.S with Windows")
        self._autostart.setChecked(bool(self._data.get("features", {}).get("autostart", False)))
        self._autostart.setToolTip("Registers J.A.R.V.I.S to start when you sign in to Windows.")
        pv.addWidget(self._autostart)
        self._taskbar = QCheckBox("Hide J.A.R.V.I.S from the Windows taskbar")
        feats = self._data.get("features", {}) or {}
        self._taskbar.setChecked(bool(feats.get("hide_taskbar_button", feats.get("hide_taskbar", False))))
        self._taskbar.setEnabled(sys.platform.startswith("win"))
        if not sys.platform.startswith("win"):
            self._taskbar.setToolTip("Windows only. The Windows taskbar itself stays visible; only the J.A.R.V.I.S taskbar button is hidden.")
        pv.addWidget(self._taskbar)
        lay.addWidget(perms)

        devices = HudGroupBox("DEVICE ACCESS")
        dv = QVBoxLayout(devices)
        dv.addWidget(self._flag_checkbox(self._perm_checks, "mic_access", "Microphone access (voice input)",
                                         True, "Off = JARVIS stays muted and cannot unmute.",
                                         saved=feats))
        dv.addWidget(self._flag_checkbox(self._perm_checks, "camera_access", "Camera access (webcam + vision)",
                                         True, "Off = camera captures are refused.",
                                         saved=feats))
        note = QLabel("Revoking a device takes effect on SAVE. The microphone mute (F4) still works as a quick override.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        dv.addWidget(note)
        lay.addWidget(devices)

        self_control = HudGroupBox("AI SELF-CONTROL")
        sv = QVBoxLayout(self_control)
        sv.addWidget(self._flag_checkbox(
            self._perm_checks, "voice_engine_control",
            "Voice engine control (JARVIS may switch its own voice output, e.g. to Gemini native audio)",
            bool(feats.get("voice_engine_control", True)),
            "Off = JARVIS can report its current voice engine but cannot change it itself. "
            "You can still change it here in Settings."))
        sv.addWidget(self._flag_checkbox(
            self._perm_checks, "self_diagnostics",
            "Self-diagnostics (JARVIS may check its own engine, project, and settings)",
            bool(feats.get("self_diagnostics", True)),
            "Off = JARVIS refuses diagnostics/health-check requests about itself."))
        lay.addWidget(self_control)

        coding = HudGroupBox("CODING APPROVAL")
        cdv = QVBoxLayout(coding)
        cdv.addWidget(self._flag_checkbox(
            self._perm_checks, "coding_agents", "Allow coding tasks",
            bool(feats.get("coding_agents", True)),
            "Off = JARVIS refuses to write, edit, build, or run code."))
        cdv.addWidget(self._flag_checkbox(
            self._perm_checks, "auto_approve_coding",
            "Auto-approve coding (skip the approve/decline prompt in chat)",
            bool(feats.get("auto_approve_coding", False)),
            "On = coding tasks run immediately without asking. Off (default) = JARVIS "
            "posts an Approve/Decline card in the chat log before it writes or runs any code."))
        cnote = QLabel("With auto-approve off, JARVIS explains what it's about to code in the chat "
                       "and waits for you to press Approve or Decline before touching any files.")
        cnote.setWordWrap(True)
        cnote.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        cdv.addWidget(cnote)
        lay.addWidget(coding)

        # ── PC AUTOMATION (AGENT TARS) ──────────────────────────────
        # The three switches that actually gate the automation capabilities.
        # Persisted through the existing features map on SAVE.
        tars_grp = HudGroupBox("PC AUTOMATION (AGENT TARS)")
        tv = QVBoxLayout(tars_grp)
        tv.addWidget(self._flag_checkbox(
            self._perm_checks, "agent_tars",
            "Allow PC & browser automation (Agent TARS)",
            bool(feats.get("agent_tars", True)),
            "Off = JARVIS refuses computer_control and browser_control entirely."))
        tv.addWidget(self._flag_checkbox(
            self._perm_checks, "desktop_control",
            "Allow desktop control (launch applications, run commands)",
            bool(feats.get("desktop_control", True)),
            "Off = JARVIS will not open or control applications on this PC."))
        tv.addWidget(self._flag_checkbox(
            self._perm_checks, "web_task",
            "Allow web automation (control a browser)",
            bool(feats.get("web_task", True)),
            "Off = JARVIS will not drive websites in a browser."))
        tars_note = QLabel("Agent TARS runs its own loop on the same Gemini key. Its live "
                           "status (Enabled / Connected / Version) is on the STATUS tab.")
        tars_note.setWordWrap(True)
        tars_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        tv.addWidget(tars_note)
        lay.addWidget(tars_grp)

        # ── LOCAL CONTROLLERS (PLAYWRIGHT · PYWINAUTO · UI-TARS · PYAUTOGUI) ──
        # One switch per engine relationship. Each is a real permission: with a
        # switch off the matching actions are refused by name and no other
        # controller is substituted for them.
        local_grp = HudGroupBox("PC CONTROL  //  LOCAL CONTROLLERS")
        lv = QVBoxLayout(local_grp)
        lv.addWidget(self._flag_checkbox(
            self._perm_checks, "playwright_browser",
            "Allow browser automation (Playwright)",
            bool(feats.get("playwright_browser", True)),
            "Off = JARVIS refuses browser_action; Agent TARS web workflows are "
            "unaffected."))
        lv.addWidget(self._flag_checkbox(
            self._perm_checks, "windows_automation",
            "Allow Windows application control (pywinauto)",
            bool(feats.get("windows_automation", True)),
            "Off = JARVIS will not open, close or focus application windows "
            "through native Windows automation."))
        lv.addWidget(self._flag_checkbox(
            self._perm_checks, "uitars_visual",
            "Allow UI-TARS visual control (Gemini vision)",
            bool(feats.get("uitars_visual", True)),
            "Off = JARVIS will not act on what it sees on screen; it runs on "
            "the Gemini key, with no separate UI-TARS credential."))
        lv.addWidget(self._flag_checkbox(
            self._perm_checks, "physical_input",
            "Allow mouse and keyboard control (PyAutoGUI)",
            bool(feats.get("physical_input", True)),
            "Off = JARVIS will not move the pointer or type on this PC, and "
            "visual clicks are refused too."))
        lv.addWidget(self._flag_checkbox(
            self._perm_checks, "power_control",
            "Allow system power control (lock, sleep, restart, shut down)",
            bool(feats.get("power_control", True)),
            "Off = JARVIS refuses power_control entirely. The actions that end "
            "the session still need a confirmation press on the HUD, whatever "
            "the approval mode is."))
        _local_note = QLabel(
            "One router picks the controller: Playwright for page elements, "
            "pywinauto for Windows windows and controls, UI-TARS (on Gemini) "
            "only when the target can only be recognised visually, and PyAutoGUI "
            "last. Every result names the controller that actually ran, and the "
            "live state of each one is on the STATUS tab.")
        _local_note.setWordWrap(True)
        _local_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        lv.addWidget(_local_note)
        lay.addWidget(local_grp)

        # ── DEEP WINDOWS ORCHESTRATION ──────────────────────────────
        # Four switches, one per relationship: the process list, the machine's
        # own settings, window arrangement and the clipboard. Each refusal
        # names the switch that stopped it, so revoking one never silently
        # leaves another capability acting instead.
        deep_grp = HudGroupBox("DEEP WINDOWS ORCHESTRATION")
        deepv = QVBoxLayout(deep_grp)
        deepv.addWidget(self._flag_checkbox(
            self._perm_checks, "process_control",
            "Allow process control (list, freeze, prioritise, end processes)",
            bool(feats.get("process_control", True)),
            "Off = JARVIS refuses system_control process actions. Ending a "
            "process still needs a confirmation press on the HUD when it is on."))
        deepv.addWidget(self._flag_checkbox(
            self._perm_checks, "system_settings",
            "Allow system settings (Wi-Fi, volume, brightness, theme, metrics)",
            bool(feats.get("system_settings", True)),
            "Off = JARVIS can read nothing of this machine's own settings and "
            "changes none of them. Wi-Fi toggling additionally needs "
            "administrator rights and says so honestly when Windows refuses."))
        deepv.addWidget(self._flag_checkbox(
            self._perm_checks, "window_control",
            "Allow window arrangement (minimize, tile, snap, multi-monitor)",
            bool(feats.get("window_control", True)),
            "Off = JARVIS will not move or resize your windows."))
        deepv.addWidget(self._flag_checkbox(
            self._perm_checks, "clipboard_sentry",
            "Allow clipboard sentry (write to and analyse the clipboard)",
            bool(feats.get("clipboard_sentry", True)),
            "Off = JARVIS may still READ the clipboard out loud, but will not "
            "put text on it and will not send it to a model for analysis."))
        deepv.addWidget(self._flag_checkbox(
            self._perm_checks, "document_architect",
            "Allow the document architect (write Word, Excel, PowerPoint and PDF files)",
            bool(feats.get("document_architect", True)),
            "Off = JARVIS refuses to write deliverable files. Checking which "
            "document engines are installed stays available."))
        _deep_note = QLabel(
            "These actions run on your machine, not in a browser: processes and "
            "settings through psutil/Win32, the clipboard through pyperclip, and "
            "documents through python-docx / openpyxl / python-pptx / pypdf. "
            "Every one is still covered by the master desktop control switch "
            "above, and each result names the real controller that ran.")
        _deep_note.setWordWrap(True)
        _deep_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        deepv.addWidget(_deep_note)
        lay.addWidget(deep_grp)

        # ── LIVE INPUTS & EXTERNAL LINKS ──────────────────────────────
        # The camera, the devices on this network, the user's own accounts and
        # the gateway a phone can reach. Each is its own relationship, so
        # revoking one cannot silently leave another acting instead.
        links_grp = HudGroupBox("LIVE INPUTS  //  DEVICES & ACCOUNTS")
        linksv = QVBoxLayout(links_grp)
        linksv.addWidget(self._flag_checkbox(
            self._perm_checks, "live_vision",
            "Allow live vision (stream the camera to the model while talking)",
            bool(feats.get("live_vision", True)),
            "Off = JARVIS will not open the camera for a conversation. Photos, "
            "face recognition and the vision panel keep working as before."))
        linksv.addWidget(self._flag_checkbox(
            self._perm_checks, "smart_home",
            "Allow smart home control (Kasa/Tapo devices on this network)",
            bool(feats.get("smart_home", True)),
            "Off = JARVIS will not list, switch or dim any smart device."))
        linksv.addWidget(self._flag_checkbox(
            self._perm_checks, "instagram",
            "Allow Instagram (the user's own account via instagrapi)",
            bool(feats.get("instagram", True)),
            "Off = JARVIS will not read or act on any Instagram account. "
            "Publishing and messaging always need a confirmation on the HUD."))
        linksv.addWidget(self._flag_checkbox(
            self._perm_checks, "spotify",
            "Allow Spotify (playback control through the local MCP server)",
            bool(feats.get("spotify", True)),
            "Off = the Spotify MCP server is not started at all, so its tools "
            "never appear to the model."))
        linksv.addWidget(self._flag_checkbox(
            self._perm_checks, "connect",
            "Allow JarvisOS Connect (a paired phone or browser reaching this PC)",
            bool(feats.get("connect", True)),
            "Off = the gateway stays closed and no device may pair. The gateway "
            "is never started automatically: it listens only when you ask."))
        linksv.addWidget(self._flag_checkbox(
            self._perm_checks, "external_apps",
            "Allow External apps (Spotify, Gmail, GitHub and the rest, via "
            "Composio)",
            bool(feats.get("external_apps", True)),
            "Off = the connector refuses every request, so no external account "
            "can be read or acted on. The API key itself lives in "
            "Settings ▸ API ▸ External apps; this switch only decides whether "
            "it may be used."))
        _links_note = QLabel(
            "The camera is shared: live vision subscribes to the same device as "
            "the vision panel instead of opening a second capture, and it stops "
            "when you stop it. Smart-home devices are reached directly on this "
            "network (no cloud), and Connect only ever exposes the commands "
            "listed by the gateway — each of them re-checked against these "
            "switches when a phone asks.")
        _links_note.setWordWrap(True)
        _links_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        linksv.addWidget(_links_note)
        lay.addWidget(links_grp)

        lay.addStretch(1)
        self.tabs.addTab(scroll, "PERMISSIONS")

    def _test_voice_link(self):
        self.voice_test_requested.emit(
            "Please say a brief, professional greeting in refined natural British English."
        )
        self._status.setText("VOICE TEST SENT")

    def _provider_values(self, pid: str) -> dict:
        """Collect ONLY the selected provider's field values from its panel."""
        d = self._data
        if pid == "fishAudio":
            return {
                "apiKey": self.fish_api_key.text().strip(),
                "endpoint": self.fish_endpoint.text().strip() or DEFAULTS["fish_audio_endpoint"],
                "model": self.fish_model_id.text().strip() or DEFAULTS["fish_audio_model_id"],
                "voice": self.fish_voice_id.text().strip(),
                "format": self.fish_format.currentText(),
                "latency": self.fish_latency.currentText(),
            }
        if pid == "elevenLabs":
            return {
                "apiKey": self.elevenlabs_api_key.text().strip(),
                "model": self.elevenlabs_model_id.currentText(),
                "voice": self.elevenlabs_voice_id.text().strip() or DEFAULTS["elevenlabs_voice_id"],
            }
        if pid == "gemini":
            return {"apiKey": str(d.get("gemini_api_key", "") or ""),
                    "model": str(d.get("ai_model", "") or ""),
                    "voice": self.live_voice.currentText()}
        if pid == "piperOnnx":
            data = self.onnx_voice.currentData() or {}
            return {
                "modelPath": self.onnx_model_path.text().strip()
                             or str((data or {}).get("model") or d.get("onnx_voice_model", "")),
                "configPath": str((data or {}).get("config") or d.get("onnx_voice_config", "")),
                "speaker": self.onnx_speaker.text().strip() or "default",
                "executionProvider": str(self.onnx_execution_provider.currentData() or "cpu"),
            }
        if pid == "kokoro":
            return {"voice": self.kokoro_voice.currentText(),
                    "speed": float(d.get("voice_speed", 1.0) or 1.0)}
        if pid == "edgeTts":
            return {"voice": self.tts_voice.text().strip() or DEFAULTS["tts_voice"],
                    "rate": "+0%", "pitch": "+0Hz", "volume": float(d.get("voice_volume", 1.0) or 1.0)}
        if pid == "sapi":
            return {"voice": str(self.sapi_voice.currentData() or "")}
        return {}

    def _selected_provider(self) -> str:
        try:
            return str(self.provider_selector.currentData() or "edgeTts")
        except Exception:
            return "edgeTts"

    def _provider_message(self, pid: str, text: str) -> None:
        try:
            self._provider_status[pid].setText(text)
        except Exception:
            pass
        self._status.setText(text[:160])

    def _save_pipeline(self, as_default: bool = False) -> None:
        """Persist ONLY the pipeline choice (pipeline-scoped save).

        The selected pipeline combo value becomes ``voice_output_mode``, the
        Gemini renderer subtree is synced so the runtime actually follows the
        combo, and the one-time pipeline lift markers are recorded so the
        deliberate choice is never re-lifted. ``as_default`` additionally
        stores ``voice.defaultPipeline`` (read by ``ui_settings.load``), so
        fresh sessions start on this pipeline.
        """
        try:
            from voice.providers_config import (
                ProviderConfigError, providers_block, set_default_pipeline,
            )
        except Exception as exc:
            self._pipeline_status.setText(f"PROVIDER LAYER UNAVAILABLE: {exc}")
            return
        pipeline = str(self.voice_output_mode.currentData() or "")
        if pipeline not in ("gemini", "gemini_piper", "local"):
            self._pipeline_status.setText("NOT SAVED — select a pipeline first.")
            return
        try:
            cfg = dict(self._data or {})
            cfg["voice_output_mode"] = pipeline
            cfg["voice_pipeline_migrated"] = True
            cfg["voice_piper_to_native_lifted"] = True
            # Sync the Gemini renderer subtree so the runtime (which reads the
            # provider block) actually follows this combo.
            block = providers_block(cfg)
            block["geminiRenderer"] = "native" if pipeline == "gemini" else "piper"
            cfg["voice"] = block
            if as_default:
                cfg = set_default_pipeline(cfg, pipeline)
            # One guarded write of the merged config (flat keys + voice block).
            updates = {"voice_output_mode": pipeline,
                       "voice_pipeline_migrated": True,
                       "voice_piper_to_native_lifted": True,
                       "voice": cfg.get("voice", {})}
            try:
                from core import config_guard as _guard
                merged = dict(_guard.read(self._config_path))
            except Exception:
                from ui_settings import load as _load_cfg  # self-import: same module
                merged = dict(_load_cfg(self._config_path))
            merged.update(updates)
            try:
                from core import config_guard as _guard
                _ok, _msg = _guard.guard_write(merged, tag="voice-pipeline", path=self._config_path)
                if not _ok:
                    raise RuntimeError(_msg)
                self._data = _guard.read(self._config_path)
            except Exception:
                from ui_settings import save as _save_cfg
                self._data = _save_cfg(self._config_path,
                                       **{k: v for k, v in merged.items() if k != "features"})
            label = {"gemini": "Gemini Native Audio", "gemini_piper": "Gemini → Piper ONNX",
                     "local": "Local Piper Only"}.get(pipeline, pipeline)
            msg = f"DEFAULT SAVED · {label}" if as_default else f"SAVED · {label}"
            self._pipeline_status.setText(msg)
            self._status.setText(msg)
            self.settings_saved.emit(dict(self._data))
            parent = self.parent()
            method = getattr(parent, "_apply_full_settings", None)
            if callable(method):
                method(dict(self._data))
        except ProviderConfigError as exc:
            self._pipeline_status.setText(f"NOT SAVED — {exc}")
        except Exception as exc:
            self._pipeline_status.setText(f"SAVE FAILED — {str(exc)[:140]}")
            self.write_log_safe(f"Pipeline save failed: {exc}")

    def _save_pipeline_as_default(self) -> None:
        """SAVE AS DEFAULT for the pipeline: persist it and record it as the
        default every new session starts on."""
        self._save_pipeline(as_default=True)

    def _save_provider_panel(self, pid: str, as_default: bool = False) -> None:
        """Validate + persist ONLY this provider's subtree (spec: provider-scoped save).

        Save: providers.<pid> + activeProvider change; every other provider's
        subtree is carried through untouched.  Save as Default: additionally
        stores defaultProvider + defaultVoice as one validated atomic pair.
        """
        try:
            from voice.providers_config import (
                ProviderConfigError, save_provider, set_active_provider,
                set_default_pair, validate_provider,
            )
        except Exception as exc:
            self._provider_message(pid, f"PROVIDER LAYER UNAVAILABLE: {exc}")
            return
        values = self._provider_values(pid)
        if pid == "gemini":
            # The Gemini key lives on the AI page; presence in the loaded
            # config counts (the panel itself has no key field).
            values["_configHasGeminiKey"] = bool(str((self._data or {}).get("gemini_api_key", "") or "").strip())
        err = validate_provider(pid, values)
        if err:
            self._provider_message(pid, f"NOT SAVED — {err}")
            return
        try:
            cfg = dict(self._data or {})
            cfg = save_provider(cfg, pid, values)
            cfg = set_active_provider(cfg, pid)
            if as_default:
                # The pair's voice is the provider's identity: the selected
                # voice for every provider except Piper, whose arrangement is
                # identified by its model file (jarvis-high.onnx IS the voice).
                try:
                    from voice.providers_config import provider_voices as _pv_ids
                    _voices = _pv_ids(cfg, pid)
                except Exception:
                    _voices = ()
                _pair_voice = str(values.get("voice", "") or (_voices[0] if _voices else ""))
                cfg = set_default_pair(cfg, pid, _pair_voice)
            # Merge the nested voice block over the flat keys that the rest of
            # the settings flow persists, then write once through the guard.
            updates = {"voice": cfg.get("voice", {})}
            self._persist_provider_write(pid, values, updates)
            try:
                from core.tts import clear_tts_cache
                clear_tts_cache()
            except Exception:
                pass
            self._data = cfg
            label = self._PROVIDER_LABELS.get(pid, pid)
            if as_default:
                voice = str(values.get("voice", ""))
                self._provider_message(pid, f"DEFAULT SAVED · {label} · {voice}")
            else:
                self._provider_message(pid, f"SAVED · {label}")
            self.settings_saved.emit(dict(self._data))
            parent = self.parent()
            if parent is not None and hasattr(parent, "_apply_full_settings"):
                parent._apply_full_settings(dict(self._data))
        except ProviderConfigError as exc:
            self._provider_message(pid, f"NOT SAVED — {exc}")
        except Exception as exc:
            self._provider_message(pid, f"SAVE FAILED — {str(exc)[:140]}")
            self.write_log_safe(f"Provider save failed ({pid}): {exc}")

    def _persist_provider_write(self, pid: str, values: dict, updates: dict) -> None:
        """One guarded write of the nested voice block + the legacy flat keys
        this provider maps onto (dual-read mirror), leaving other providers'
        flat keys exactly as they are in the file."""
        try:
            from core import config_guard as _guard
            merged = dict(_guard.read(self._config_path))
        except Exception:
            from ui_settings import load as _load_cfg  # self-import: same module
            merged = dict(_load_cfg(self._config_path))
        merged.update(updates)
        # Mirror this provider's values onto the legacy flat keys only.
        if pid == "fishAudio":
            merged.update({"fish_audio_api_key": values.get("apiKey", ""),
                           "fish_audio_endpoint": values.get("endpoint", ""),
                           "fish_audio_model_id": values.get("model", ""),
                           "fish_audio_voice_id": values.get("voice", ""),
                           "fish_audio_format": values.get("format", "mp3"),
                           "fish_audio_latency": values.get("latency", "normal")})
        elif pid == "elevenLabs":
            merged.update({"elevenlabs_api_key": values.get("apiKey", ""),
                           "elevenlabs_model_id": values.get("model", ""),
                           "elevenlabs_voice_id": values.get("voice", "")})
        elif pid == "gemini":
            merged.update({"voice_name": values.get("voice", "")})
        elif pid == "piperOnnx":
            merged.update({"onnx_voice_model": values.get("modelPath", ""),
                           "onnx_voice_config": values.get("configPath", ""),
                           "onnx_voice_speaker": values.get("speaker", "default"),
                           "onnx_execution_provider": values.get("executionProvider", "cpu")})
        elif pid == "edgeTts":
            merged.update({"tts_voice": values.get("voice", "")})
        elif pid == "kokoro":
            merged.update({"tts_engine": "kokoro", "tts_voice": values.get("voice", ""),
                           "kokoro_voice": values.get("voice", "")})
        elif pid == "sapi":
            merged.update({"sapi_voice": values.get("voice", "")})
        # The stored voice_output_mode follows the PIPELINE combo (the user's
        # renderer choice), never the raw on-disk value — the combo already
        # shows the runtime-resolved mode (e.g. the one-time gemini lift).
        # gemini_piper stays gemini_piper even when the Piper provider panel is
        # open — that panel IS its renderer.
        try:
            _vod = str(self.voice_output_mode.currentData() or "")
            if _vod:
                merged["voice_output_mode"] = _vod
            else:
                merged["voice_output_mode"] = str(merged.get("voice_output_mode") or "gemini")
        except Exception:
            pass
        # A deliberate save records that the one-time gemini -> gemini_piper
        # lift has happened, so an explicit Gemini Native Audio choice made
        # from here on is never silently migrated again. It also records the
        # piper-pipeline → native-audio lift, so a deliberate piper choice is
        # never re-flipped to the new default.
        merged["voice_pipeline_migrated"] = True
        merged["voice_piper_to_native_lifted"] = True
        if str(merged.get("tts_engine", "")) not in ("kokoro",):
            merged["tts_engine"] = {"fishAudio": "fish_audio", "elevenLabs": "elevenlabs",
                                    "gemini": "edgetts", "piperOnnx": "onnx",
                                    "edgeTts": "edgetts", "sapi": "sapi"}.get(
                pid, str(merged.get("tts_engine", "edgetts")))
        try:
            from core import config_guard as _guard
            # guard_write (not save_with_history) so the test/self config path
            # is honoured — save_with_history always writes the global path.
            _ok, _msg = _guard.guard_write(merged, tag="voice-provider", path=self._config_path)
            if not _ok:
                raise RuntimeError(_msg)
            self._data = _guard.read(self._config_path)
        except Exception:
            from ui_settings import save as _save_cfg
            self._data = _save_cfg(self._config_path, **{k: v for k, v in merged.items() if k != "features"})

    def _test_provider(self, pid: str) -> None:
        """Speak a test line through the panel's provider with the panel's
        current (unsaved) values; provider-named errors land in the status bar."""
        import threading
        values = self._provider_values(pid)
        try:
            from voice.providers_config import ProviderConfigError, validate_provider
            err = validate_provider(pid, values)
            if err:
                self._provider_message(pid, f"TEST — {err}")
                return
        except Exception:
            pass
        label = self._PROVIDER_LABELS.get(pid, pid)
        self._provider_message(pid, f"TESTING {label.upper()}…")

        # Build a config dict shaped like the persisted one so the factory's
        # provider branch picks up the panel values (not the saved ones).
        cfg = dict(self._data or {})
        try:
            from voice.providers_config import save_provider, set_active_provider
            cfg = set_active_provider(save_provider(cfg, pid, values), pid)
        except Exception:
            pass

        def _say(text):
            QTimer.singleShot(0, self, lambda: self._provider_message(pid, text))

        def _run():
            try:
                import sys as _sys
                _sys.path.insert(0, str(self._config_path.parent.parent))
                from core.tts import clear_tts_cache, create_tts_player
                clear_tts_cache()   # panel values differ from any cached engine
                create_tts_player(cfg).speak("Voice test.")
                _say(f"{label.upper()} TEST OK")
            except Exception as exc:
                _say(f"{label.upper()} TEST FAILED: {str(exc)[:120]}")
                self.write_log_safe(f"Provider test failed ({pid}): {exc}")

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_provider_voices(self, pid: str) -> None:
        """Fetch the provider's voice list into ITS panel only (10 s timeout,
        worker thread; failure never discards the saved selection)."""
        import threading
        label = self._PROVIDER_LABELS.get(pid, pid)
        values = self._provider_values(pid)
        key = str(values.get("apiKey", "") or "")
        if not key:
            self._provider_message(pid, f"REFRESH — {label} API key is missing.")
            return
        self._provider_message(pid, f"REFRESHING {label.upper()}…")

        def _run():
            try:
                import requests
                if pid == "fishAudio":
                    resp = requests.get(
                        "https://api.fish.audio/model",
                        headers={"Authorization": f"Bearer {key}"},
                        params={"page_size": 30, "self": True}, timeout=10)
                    resp.raise_for_status()
                    items = resp.json().get("items", []) or []
                    voices = [(str(i.get("title") or i.get("_id", "")), str(i.get("_id", "")))
                              for i in items if i.get("_id")]
                else:  # elevenLabs
                    resp = requests.get(
                        "https://api.elevenlabs.io/v1/voices",
                        headers={"xi-api-key": key}, timeout=10)
                    resp.raise_for_status()
                    voices = [(str(v.get("name", "")), str(v.get("voice_id", "")))
                              for v in resp.json().get("voices", []) if v.get("voice_id")]
            except Exception as exc:
                QTimer.singleShot(0, self, lambda: self._provider_message(
                    pid, f"REFRESH FAILED — {str(exc)[:120]} (saved voice kept)"))
                return

            def _apply():
                try:
                    combo = self.fish_voice_id if pid == "fishAudio" else self.elevenlabs_voice_id
                    if pid != "fishAudio" and hasattr(self, "_eleven_voice_combo"):
                        combo = self._eleven_voice_combo
                    current = combo.text().strip() if hasattr(combo, "text") else ""
                    # Keep it a plain QLineEdit: refresh offers the fetched ids
                    # as an editable completion list, the current value survives.
                    if hasattr(combo, "completer") and combo.completer() is None and voices:
                        from PyQt6.QtCore import QStringListModel
                        from PyQt6.QtWidgets import QCompleter
                        c = QCompleter([f"{n} ({i})" for n, i in voices], combo)
                        c.setFilterMode(Qt.MatchFlag.Contains)
                        combo.setCompleter(c)
                    if current:
                        combo.setText(current)   # the saved selection is preserved
                    self._provider_message(pid, f"REFRESHED · {len(voices)} voices available")
                except Exception as exc:
                    self._provider_message(pid, f"REFRESH FAILED — {str(exc)[:120]}")

            QTimer.singleShot(0, self, _apply)

        threading.Thread(target=_run, daemon=True).start()

    def _save_default_voice(self) -> None:
        """Legacy entry point: Save as Default for the currently selected provider."""
        self._save_provider_panel(self._selected_provider(), as_default=True)

    def _test_tts_engine(self):
        """Legacy entry point: test the currently selected provider."""
        self._test_provider(self._selected_provider())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        root = self.findChild(QFrame, "SettingsRoot")
        if root is not None and hasattr(root, "_fit_backdrop"):
            root._fit_backdrop()

    def _build_ai_tab(self):
        """AI agent assistant: provider + model dropdowns, custom provider, API keys."""
        from core.ai_providers import PROVIDERS, models_for
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        intro = QLabel("AI AGENT  //  PROVIDER · MODEL · CUSTOM ENDPOINT")
        intro.setStyleSheet(f'color:{ACCENT};font:800 9pt "Orbitron";padding:2px 2px 5px 2px;')
        lay.addWidget(intro)
        hint = QLabel("Gemini drives the voice session. Other providers answer typed text. Custom = any OpenAI-compatible endpoint.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"background:{SURFACE_INSET};border:none;border-radius:9px;padding:10px;color:{TEXT_MID};")
        lay.addWidget(hint)

        prov_box = HudGroupBox("PROVIDER & MODEL")
        pf = QFormLayout(prov_box)
        self.ai_provider = QComboBox()
        for key, meta in PROVIDERS.items():
            self.ai_provider.addItem(str(meta.get("label", key)), key)
        cur_prov = str(self._data.get("ai_provider", "gemini") or "gemini").strip().lower()
        idx = self.ai_provider.findData(cur_prov)
        self.ai_provider.setCurrentIndex(max(0, idx))
        self.ai_provider.currentIndexChanged.connect(self._refresh_ai_fields)
        pf.addRow("Provider", self.ai_provider)
        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        saved_model = str(self._data.get("ai_model", "") or "").strip()
        self.ai_model.addItems(models_for(cur_prov))
        if saved_model:
            self.ai_model.setCurrentText(saved_model)
        self.ai_model.setToolTip("Pick a model or type your own (e.g. a newly released ID).")
        pf.addRow("Model", self.ai_model)
        lay.addWidget(prov_box)

        # ── MODEL ROLES (core/model_router.py) ────────────────────────────
        # Each role answers a different kind of work; blank = built-in default.
        try:
            from core.model_router import ROLES as _ROLES, ROLE_HINTS as _HINTS
        except Exception:
            _ROLES, _HINTS = (), {}
        self._role_edits: dict[str, QLineEdit] = {}
        if _ROLES:
            roles_box = HudGroupBox("MODEL ROLES")
            rf = QFormLayout(roles_box)
            saved_roles = dict(self._data.get("model_roles") or {})
            for _role in _ROLES:
                _edit = QLineEdit(str(saved_roles.get(_role, "") or ""))
                _edit.setPlaceholderText("(auto)")
                _edit.setToolTip(str(_HINTS.get(_role, "")))
                self._role_edits[_role] = _edit
                rf.addRow(_role.capitalize(), _edit)
            _roles_hint = QLabel(
                "Blank = built-in default. A role model that fails or "
                "rate-limits is benched and the fallback role takes over."
            )
            _roles_hint.setWordWrap(True)
            _roles_hint.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
            rf.addRow("", _roles_hint)
            lay.addWidget(roles_box)

        # ── LAYA AI (core/laya_decision.py) ─────────────────────────────────
        # The local decision layer next to Gemini. Every control here writes
        # the real configuration AND the live runtime the moment it changes —
        # the toggle is not decoration: disabling drops the loaded model,
        # enabling re-initializes it in the background.
        try:
            from core import laya_decision as _laya
            _laya_status = _laya.status()
        except Exception:
            _laya = None
            _laya_status = {}
        laya_box = HudGroupBox("Laya AI")
        lv = QVBoxLayout(laya_box)
        _feats = self._data.get("features", {}) or {}
        self._laya_enable = QCheckBox("Enable Laya AI")
        self._laya_enable.setChecked(bool(_feats.get("laya_ai", True)))
        self._laya_enable.setToolTip(
            "Local decision AI (322M, CPU) classifies each request "
            "(SEARCH · CODE · REVIEW · VIEW · FIND · GENERATE · ANALYZE · "
            "AUTOMATE) and feeds the result to the AI task router. When off "
            "or unavailable, Gemini and the existing routing continue "
            "untouched.")
        self._laya_enable.toggled.connect(self._on_laya_enable_toggled)
        lv.addWidget(self._laya_enable)

        def _laya_row(label):
            row = QHBoxLayout()
            cap = QLabel(label)
            cap.setMinimumWidth(120)
            cap.setStyleSheet(f"color:{TEXT_DIM};background:transparent;"
                              f"font:600 8pt '{FONT_UI}';")
            val = QLabel("—")
            val.setWordWrap(True)
            val.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
            row.addWidget(cap)
            row.addWidget(val, 1)
            row.addStretch(1)
            lv.addLayout(row)
            return val

        self._laya_status_label = _laya_row("Model Status")
        self._laya_dir_label = _laya_row("Model Directory")
        self._laya_engine_label = _laya_row("Decision AI")
        self._laya_info_label = _laya_row("Model Info")
        self._laya_state_label = _laya_row("Runtime State")
        # Measured, not estimated: how long this machine actually takes to make
        # one Laya decision (0 until a real decision has run).
        self._laya_latency_label = _laya_row("Decision Latency")

        self._laya_default = QCheckBox("Save as Default")
        self._laya_default.setChecked(bool(_laya_status.get("save_as_default", False)))
        self._laya_default.setToolTip(
            "Persist this Laya configuration: every next launch restores the "
            "enabled state, re-detects laya-multilingual/ and initializes "
            "automatically when available.")
        self._laya_default.toggled.connect(self._on_laya_default_toggled)
        lv.addWidget(self._laya_default)
        lay.addWidget(laya_box)

        # Keep the status readout honest while the AI tab is on screen
        # (model loading finishes asynchronously in the background).
        self._laya_timer = QTimer(self)
        self._laya_timer.setInterval(2000)
        self._laya_timer.timeout.connect(self._refresh_laya_status)
        self._laya_timer.start()
        self._refresh_laya_status()

        # All provider keys live on the dedicated API tab (single editor per
        # key); this tab keeps only the functional provider/model/endpoint UI.
        keys_note = QLabel("All API keys (Gemini, OpenAI, Anthropic, Groq, custom, "
                           "Fish Audio, ElevenLabs, Mem0, Qdrant) are on the API tab.")
        keys_note.setWordWrap(True)
        keys_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        lay.addWidget(keys_note)

        self._custom_box = HudGroupBox("CUSTOM PROVIDER (OpenAI-compatible)")
        cf = QFormLayout(self._custom_box)
        self.custom_provider_name = QLineEdit(str(self._data.get("custom_provider_name", "")))
        self.custom_provider_name.setPlaceholderText("e.g. My Cloud GPU")
        cf.addRow("Name", self.custom_provider_name)
        self.custom_ai_base_url = QLineEdit(str(self._data.get("custom_ai_base_url", "")))
        self.custom_ai_base_url.setPlaceholderText("https://my-server:8000/v1")
        cf.addRow("Base URL", self.custom_ai_base_url)
        self.custom_ai_model = QLineEdit(str(self._data.get("custom_ai_model", "")))
        self.custom_ai_model.setPlaceholderText("e.g. my-model-7b")
        cf.addRow("Model", self.custom_ai_model)
        lay.addWidget(self._custom_box)

        test = QPushButton("TEST CONNECTION")
        test.setToolTip("Send a tiny probe to the selected provider.")
        test.clicked.connect(self._test_ai_connection)
        lay.addWidget(test)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "AI")
        self._refresh_ai_fields(self.ai_provider.currentIndex())

    def _refresh_ai_fields(self, _index=None):
        """Refresh model list + dim the custom group unless provider = custom."""
        try:
            from core.ai_providers import models_for
            from PyQt6.QtWidgets import QGraphicsOpacityEffect as _OE
            prov = str(self.ai_provider.currentData() or "gemini")
            keep = self.ai_model.currentText().strip()
            self.ai_model.blockSignals(True)
            self.ai_model.clear()
            self.ai_model.addItems(models_for(prov))
            if keep:
                self.ai_model.setCurrentText(keep)
            self.ai_model.blockSignals(False)
            box = getattr(self, "_custom_box", None)
            if box is not None:
                eff = box.graphicsEffect()
                if not isinstance(eff, _OE):
                    eff = _OE(box)
                    box.setGraphicsEffect(eff)
                eff.setOpacity(1.0 if prov == "custom" else 0.45)
        except Exception:
            pass

    def _test_ai_connection(self):
        try:
            from core import ai_providers as _aip
            cfg = self._collect()
            reply = _aip.test_connection(cfg)
            self._status.setText("AI LINK OK")
            self.write_log_safe(f"AI test OK: {str(reply)[:120]}")
        except Exception as exc:
            self._status.setText("AI LINK FAILED")
            self.write_log_safe(f"AI test failed: {exc}")

    # ── Laya AI section handlers ────────────────────────────────────────────
    def _on_laya_enable_toggled(self, checked: bool):
        """'Enable Laya AI' changes configuration AND runtime immediately:
        off drops the loaded model, on (re)initializes in the background."""
        try:
            from core import laya_decision
            ok, msg = laya_decision.set_enabled(bool(checked))
            self.write_log_safe(
                ("Laya AI enabled" if checked else "Laya AI disabled")
                + ("" if ok else f" — config write failed: {msg}"))
        except Exception as exc:
            self.write_log_safe(f"Laya toggle failed: {exc}")
        self._refresh_laya_status()

    def _on_laya_default_toggled(self, checked: bool):
        """'Save as Default' persists the current enabled state so the next
        launch restores it (same persistence pattern as the voice pipeline)."""
        try:
            from core import laya_decision
            ok, msg = laya_decision.set_save_as_default(bool(checked))
            self.write_log_safe(
                ("Laya AI saved as default" if checked
                 else "Laya AI default cleared")
                + ("" if ok else f" — config write failed: {msg}"))
        except Exception as exc:
            self.write_log_safe(f"Laya save-as-default failed: {exc}")
        self._refresh_laya_status()

    def _refresh_laya_status(self):
        """Re-read REAL detection + runtime state into the section labels.
        Never raises: a broken service just leaves the last text."""
        try:
            from core import laya_decision
            st = laya_decision.status()
        except Exception:
            return
        try:
            detected = bool(st.get("detected"))
            ready = bool(st.get("ready"))
            enabled = bool(st.get("enabled"))
            state = str(st.get("state") or "")
            self._laya_status_label.setText(
                "● Laya model detected" if detected else "● Laya model not found")
            self._laya_status_label.setStyleSheet(
                f"color:{GREEN if detected else RED};background:transparent;"
                f"font:700 9pt '{FONT_UI}';")
            self._laya_dir_label.setText(str(st.get("model_dir") or "—"))
            self._laya_engine_label.setText(str(st.get("decision_ai") or "—"))
            info = st.get("model_info") or {}
            bits = [str(info.get("params") or "322M"), str(info.get("encoder") or "")]
            self._laya_info_label.setText(" · ".join(b for b in bits if b and b != "—"))
            if not enabled:
                line = "Disabled"
            elif ready:
                line = "Enabled · Ready"
            elif state == "loading":
                line = "Enabled · Loading model…"
            else:
                line = f"Enabled · {st.get('detail_line') or 'Fallback active'}"
            self._laya_state_label.setText(line)
            self._laya_state_label.setStyleSheet(
                f"color:{GREEN if ready and enabled else (TEXT_DIM if not enabled else AMBER)};"
                f"background:transparent;font:700 9pt '{FONT_UI}';")
            # Measured decision latency (spec §4/§26): real numbers from the
            # decision path, or an explicit "nothing measured yet".
            try:
                t = st.get("timing") or {}
                lastt = st.get("last_timing") or {}
                if t.get("decisions") or t.get("cache_hits"):
                    bits = [f"{int(t.get('decisions') or 0)} decided",
                            f"avg {float(t.get('avg_total_ms') or 0):.0f} ms",
                            f"max {float(t.get('max_ms') or 0):.0f} ms"]
                    if t.get("cache_hits"):
                        bits.append(f"{int(t['cache_hits'])} from cache")
                    if t.get("skips"):
                        bits.append(f"{int(t['skips'])} skipped")
                    if t.get("threads"):
                        bits.append(f"torch {int(t['threads'])} threads")
                    if t.get("load_ms"):
                        bits.append(f"load {float(t['load_ms']) / 1000.0:.1f}s")
                    lat = " · ".join(bits)
                    if lastt.get("skipped"):
                        lat += f" — last skipped: {lastt['skipped']}"
                else:
                    lat = "no decision measured yet this session"
                self._laya_latency_label.setText(lat)
                self._laya_latency_label.setToolTip(
                    "Measured on this machine: request received → routing "
                    "completed, averaged over every real Laya decision since "
                    "startup. 0/absent means no request has been decided yet, "
                    "not that decisions are fast.")
            except Exception:
                pass
            last = st.get("last_classification") or {}
            tip = (f"Last decision: {last.get('category')} · "
                   f"{last.get('confidence')} → {last.get('target')}"
                   if last else "No classification made yet this session.")
            skip = st.get("last_skip") or {}
            if skip.get("detail"):
                tip += f"\nLast request not decided: {skip['detail']}"
            detail = str(st.get("state_detail") or "")
            if detail:
                tip += f"\n{detail}"
            for _lab in (self._laya_status_label, self._laya_state_label):
                _lab.setToolTip(tip)
        except Exception:
            pass

    def write_log_safe(self, text: str):
        try:
            parent = self.parent()
            if parent is not None and hasattr(parent, "write_log"):
                parent.write_log(text)
        except Exception:
            pass
        try:
            self._status.setToolTip(str(text)[:300])
        except Exception:
            pass

    def _build_api_tab(self):
        """One place for every provider API key (change: API tab).

        Sections: AI providers (OpenAI/Anthropic/Groq/custom key), voice
        providers (Fish Audio/ElevenLabs/Gemini), optional subsystems
        (Mem0 + the Mem0 feature flag, Qdrant URL/key + its feature flag).
        Qdrant and Mem0 keys have no other editor anywhere in the app; the AI
        and voice keys mirror values edited elsewhere — they are merged back
        over ``self._data`` on SAVE.
        """
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        intro = QLabel("API KEYS  //  AI · VOICE · MEMORY SUBSYSTEMS")
        intro.setStyleSheet(f'color:{ACCENT};font:800 9pt "Orbitron";padding:2px 2px 5px 2px;')
        lay.addWidget(intro)
        hint = QLabel("Keys are stored in config/api_keys.json and take effect on SAVE. "
                      "Mem0/Qdrant also need their feature flag (below) to activate.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"background:{SURFACE_INSET};border:none;border-radius:9px;padding:10px;color:{TEXT_MID};")
        lay.addWidget(hint)

        def _key_field(container_key: str, placeholder: str, tip: str = "") -> QLineEdit:
            """One credential editor, mirrored with every other page that shows it."""
            return self._new_key_edit(container_key, placeholder, tip)

        def _key_row(form, container_key: str, label: str, placeholder: str = "",
                     tip: str = ""):
            """A labelled credential row with GET API KEY / PASTE / TEST."""
            row, edit = self._key_row(container_key, label, placeholder, tip,
                                      layout=None, with_label=False, tab="API")
            form.addRow(label, row)
            return edit

        def _flag_from_data(key: str, default: bool) -> QCheckBox:
            feats = (self._data.get("features") or {})
            cb = QCheckBox()
            cb.setChecked(bool(feats.get(key, default)))
            return cb

        # ── AI PROVIDERS ─────────────────────────────────────────────────
        ai_box = HudGroupBox("AI PROVIDERS")
        af = QFormLayout(ai_box)
        self.gemini_key_edit = _key_row(
            af, "gemini_api_key", "Gemini", "Gemini API key (first-time setup value)",
            "Gemini drives the realtime voice session and the AI agent.")
        self.openai_api_key = _key_row(af, "openai_api_key", "OpenAI", "sk-… OpenAI key")
        self.anthropic_api_key = _key_row(af, "anthropic_api_key", "Anthropic",
                                          "sk-ant-… Anthropic key")
        self.groq_api_key = _key_row(af, "groq_api_key", "Groq", "gsk_… Groq key")
        self.custom_ai_api_key = _key_row(
            af, "custom_ai_api_key", "Custom", "Custom provider API key (if required)")
        lay.addWidget(ai_box)

        # ── PC AUTOMATION (Agent TARS · UI TARS) ─────────────────────────
        # No key belongs here: Agent TARS and the UI-TARS visual layer both run
        # on the Gemini key above. The live state of every controller is on the
        # STATUS tab.
        tars_box = HudGroupBox("PC AUTOMATION  //  AGENT TARS · UI TARS")
        tf = QFormLayout(tars_box)
        _tars_note = QLabel(
            "PC automation and the UI-TARS visual layer use the Gemini key "
            "above — there is no separate UI-TARS credential. The Gemini / UI "
            "TARS chat-panel mode switch is the icon button on the chat panel.")
        _tars_note.setWordWrap(True)
        _tars_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        tf.addRow("", _tars_note)
        lay.addWidget(tars_box)

        # ── VOICE PROVIDERS ──────────────────────────────────────────────
        voice_box = HudGroupBox("VOICE PROVIDERS")
        vf = QFormLayout(voice_box)
        self.fish_audio_key_edit = _key_row(
            vf, "fish_audio_api_key", "Fish Audio", "Fish Audio API key",
            "Used by the Fish Audio cloud voice provider (Settings ▸ Voice).")
        self.elevenlabs_key_edit = _key_row(
            vf, "elevenlabs_api_key", "ElevenLabs", "ElevenLabs API key",
            "Used by the ElevenLabs voice provider (Settings ▸ Voice).")
        _voice_note = QLabel("Voice providers are switched and tested on the VOICE tab.")
        _voice_note.setWordWrap(True)
        _voice_note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        vf.addRow("", _voice_note)
        lay.addWidget(voice_box)

        # ── MEMORY SUBSYSTEMS (optional; flag + key together) ────────────
        mem_box = HudGroupBox("MEMORY SUBSYSTEMS")
        mf = QFormLayout(mem_box)
        self._mem0_flag = _flag_from_data("mem0_memory", False)
        self._mem0_flag.setText("Enable Mem0 memory backend")
        self._mem0_flag.setToolTip(
            "Route long-term memory through Mem0 (needs a Mem0 API key). "
            "Off = the local memory store is used.")
        mf.addRow("", self._mem0_flag)
        self.mem0_key_edit = _key_row(
            mf, "mem0_api_key", "Mem0", "Mem0 API key (m0-…)",
            "Mem0 cloud memory. The MEM0_API_KEY environment variable also works.")
        self._qdrant_flag = _flag_from_data("qdrant_knowledge", False)
        self._qdrant_flag.setText("Enable Qdrant knowledge retrieval")
        self._qdrant_flag.setToolTip(
            "Semantic search over ingested documents with source citations "
            "(needs the optional qdrant-client package).")
        mf.addRow("", self._qdrant_flag)
        self.qdrant_url_edit = _key_row(
            mf, "qdrant_url", "Qdrant URL", "http://localhost:6333",
            "Qdrant server URL. The QDRANT_URL environment variable overrides this.")
        self.qdrant_key_edit = _key_row(
            mf, "qdrant_api_key", "Qdrant",
            "Qdrant API key (cloud; blank = local server)",
            "Leave blank for a local Qdrant server without authentication.")
        lay.addWidget(mem_box)

        # ── SERVICE HUB ──────────────────────────────────────────────────
        # Every external link in one place: the credentials it needs, the real
        # state it is in, and a TEST button that performs a genuine (tiny) round
        # trip instead of claiming success. The state pills are painted from
        # services/hub.py's cheap check; a probe runs on a worker thread and is
        # applied by a timer, so the Settings window never blocks on the network.
        try:
            from services import hub as _hub
        except Exception:
            _hub = None
        if _hub is not None:
            creds_box = HudGroupBox("SERVICE CREDENTIALS")
            cf2 = QFormLayout(creds_box)
            self._hub_fields: dict = {}
            self._hub_rows: dict = {}
            self._hub_pending: dict = {}
            for _service in _hub.SERVICES:
                if not _service.fields:
                    continue
                _head = QLabel(_service.label.upper())
                _head.setStyleSheet(
                    f"color:{ACCENT_TEXT};font:800 8pt {FONT_HEAD};"
                    "letter-spacing:2px;background:transparent;padding-top:6px;")
                cf2.addRow("", _head)
                for _name, _label, _secret, _optional in _service.fields:
                    _row, _edit = self._key_row(
                        _name, _label,
                        _label + (" (optional)" if _optional else ""),
                        layout=None, with_label=False)
                    cf2.addRow(_label, _row)
                    self._hub_fields[_name] = _edit
            lay.addWidget(creds_box)

            hub_box = HudGroupBox("SERVICE HUB  //  STATUS · TEST")
            hubv = QVBoxLayout(hub_box)
            _hub_intro = QLabel(
                "Each line below is a real link on this machine. TEST performs one "
                "small request (or reads the real files and packages it needs) and "
                "reports exactly what came back — a missing credential is never "
                "shown as working.")
            _hub_intro.setWordWrap(True)
            _hub_intro.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
            hubv.addWidget(_hub_intro)
            for _service in _hub.SERVICES:
                _row = QHBoxLayout()
                _name_label = QLabel(_service.label)
                _name_label.setMinimumWidth(150)
                _name_label.setStyleSheet(f"color:{TEXT};background:transparent;")
                _row.addWidget(_name_label)
                _pill = QLabel("NOT CHECKED")
                _pill.setMinimumWidth(120)
                _pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
                _pill.setStyleSheet(
                    f"color:{TEXT_MID};background:{SURFACE_INSET};border:none;"
                    f"border-radius:7px;padding:3px 8px;font:700 8pt 'Orbitron';")
                _row.addWidget(_pill)
                _summary = QLabel(_service.summary)
                _summary.setWordWrap(True)
                _summary.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
                _row.addWidget(_summary, 1)
                _test = QPushButton("TEST")
                _test.setFixedWidth(70)
                _test.setToolTip(f"Test the {_service.label} link for real.")
                _test.clicked.connect(
                    lambda _checked=False, sid=_service.id: self._test_service(sid))
                _row.addWidget(_test)
                hubv.addLayout(_row)
                self._hub_rows[_service.id] = (_pill, _test)
            _actions_row = QHBoxLayout()
            _test_all = QPushButton("TEST ALL LINKS")
            _test_all.setToolTip("Run every service test in the background, one at a time.")
            _test_all.clicked.connect(self._test_all_services)
            _actions_row.addWidget(_test_all)
            # Spotify needs one interactive step that no probe can replace:
            # the user approving access on spotify.com. The browser opens from
            # here and the callback lands on 127.0.0.1, so the token never
            # leaves this machine.
            _authorise = QPushButton("AUTHORISE SPOTIFY")
            _authorise.setToolTip(
                "Open Spotify's consent page in the browser and store the "
                "resulting token locally. Required once per machine.")
            _authorise.clicked.connect(self._authorise_spotify)
            _actions_row.addWidget(_authorise)
            _actions_row.addStretch(1)
            hubv.addLayout(_actions_row)
            lay.addWidget(hub_box)

            # ── MCP servers ─────────────────────────────────────────────
            mcp_box = HudGroupBox("MCP SERVERS  //  LOCAL CHILD PROCESSES")
            mcpv = QVBoxLayout(mcp_box)
            self._mcp_label = QLabel("Reading the server list…")
            self._mcp_label.setWordWrap(True)
            self._mcp_label.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
            mcpv.addWidget(self._mcp_label)
            lay.addWidget(mcp_box)
            self._refresh_mcp_servers()

            # Results arrive from worker threads; a timer in the GUI thread
            # applies them, which keeps widget access on the UI thread.
            self._hub_timer = QTimer(self)
            self._hub_timer.setInterval(250)
            self._hub_timer.timeout.connect(self._drain_hub_results)
            self._hub_timer.start()
            self._refresh_service_states()

        lay.addStretch(1)
        self.tabs.addTab(scroll, "API")

    # ── service hub handlers ────────────────────────────────────────────────
    def _hub_config(self) -> dict:
        """What the fields currently hold, over the saved configuration."""
        data = dict(self._data or {})
        for name, edit in getattr(self, "_hub_fields", {}).items():
            data[name] = edit.text().strip()
        return data

    def _set_hub_state(self, service_id: str, state: str, detail: str,
                       ok: bool = False) -> None:
        rows = getattr(self, "_hub_rows", {})
        if service_id not in rows:
            return
        pill, _button = rows[service_id]
        colours = {
            "ready": (ACCENT, "#06222A"),
            "configured": (TEXT_MID, SURFACE_INSET),
            "key_optional": (TEXT_MID, SURFACE_INSET),
            "testing": (ACCENT, SURFACE_INSET),
            "missing_key": ("#F0C070", "#2A2110"),
            "disabled": (TEXT_MID, SURFACE_INSET),
            "unavailable": ("#E07070", "#2A1414"),
        }
        fg, bg = colours.get(state, (TEXT_MID, SURFACE_INSET))
        pill.setText(str(state or "?").replace("_", " ").upper())
        pill.setStyleSheet(
            f"color:{fg};background:{bg};border:none;border-radius:7px;"
            f"padding:3px 8px;font:700 8pt 'Orbitron';")
        pill.setToolTip(str(detail or ""))
        pill.setAccessibleName(str(state))

    def _refresh_service_states(self) -> None:
        """Cheap, offline state for every pill (no sockets)."""
        try:
            from services import hub
            for row in hub.status_all(self._hub_config(), timeout=3.0):
                self._set_hub_state(row["id"], row["state"], row["detail"],
                                    bool(row.get("ok")))
        except Exception as exc:
            try:
                self.write_log_safe(f"Service hub status failed: {exc}")
            except Exception:
                pass

    def _probe_service_async(self, service_id: str) -> None:
        try:
            from services import hub
            result = hub.probe(service_id, self._hub_config())
        except Exception as exc:
            result = {"state": "unavailable",
                      "detail": f"The test could not run: {exc}"}
        self._hub_pending[service_id] = result

    def _test_service(self, service_id: str) -> None:
        rows = getattr(self, "_hub_rows", {})
        if service_id not in rows:
            return
        pill, button = rows[service_id]
        self._set_hub_state(service_id, "testing", "A real request is in flight…")
        button.setEnabled(False)
        try:
            import threading as _threading
            _threading.Thread(target=self._probe_service_async, args=(service_id,),
                              name=f"hub-probe-{service_id}", daemon=True).start()
        except Exception as exc:
            button.setEnabled(True)
            self._set_hub_state(service_id, "unavailable", str(exc))

    def _test_all_services(self) -> None:
        for service_id in list(getattr(self, "_hub_rows", {})):
            self._test_service(service_id)

    def _authorise_spotify(self) -> None:
        """Run the PKCE flow on a worker thread (it waits for the browser)."""
        self._set_hub_state("spotify", "testing",
                            "Waiting for you to approve access in the browser…")
        try:
            import threading as _threading
            _threading.Thread(target=self._authorise_spotify_async,
                              name="spotify-authorise", daemon=True).start()
        except Exception as exc:
            self._set_hub_state("spotify", "unavailable", str(exc))

    def _authorise_spotify_async(self) -> None:
        try:
            from mcp_host.servers.spotify import auth
        except Exception:
            self._hub_pending["spotify"] = {
                "state": "unavailable", "ok": False, "label": "Spotify",
                "detail": "The Spotify MCP integration has been removed from "
                          "this build; authorisation is not available."}
            return
        try:
            result = auth.authorize()
            detail = str(result.get("detail") or "Spotify is authorised.")
            self._hub_pending["spotify"] = {"state": "ready", "ok": True,
                                             "label": "Spotify",
                                             "detail": detail}
        except Exception as exc:
            self._hub_pending["spotify"] = {
                "state": "unavailable", "ok": False, "label": "Spotify",
                "detail": f"Spotify authorisation failed: {exc}"}

    def _refresh_mcp_servers(self) -> None:
        """The configured MCP servers and what the host actually published."""
        label = getattr(self, "_mcp_label", None)
        if label is None:
            return
        lines: list[str] = []
        try:
            from mcp_host import catalog
            for spec in catalog.load_servers():
                check = catalog.probe(spec)
                state = "enabled" if spec.enabled else "disabled"
                lines.append(f"- {spec.name} (tools appear as {spec.namespace}__*): "
                             f"{state}, {check['detail']}")
                if spec.description:
                    lines.append(f"    {spec.description}")
        except Exception as exc:
            lines.append(f"The MCP server list could not be read: {exc}")
        try:
            from mcp_host import bridge as _mcp_bridge
            live = _mcp_bridge.status()
            for row in live.get("servers") or []:
                if row.get("running"):
                    lines.append(f"- {row['name']}: running with "
                                 f"{len(row.get('tools') or [])} tool(s)")
                elif row.get("error"):
                    lines.append(f"- {row['name']}: {row['error']}")
        except Exception:
            pass
        label.setText("\n".join(lines) or "No MCP servers are configured.")

    def _drain_hub_results(self) -> None:
        pending = getattr(self, "_hub_pending", None)
        if not pending:
            return
        for service_id, result in list(pending.items()):
            pending.pop(service_id, None)
            rows = getattr(self, "_hub_rows", {})
            if service_id in rows:
                rows[service_id][1].setEnabled(True)
            self._set_hub_state(service_id, str(result.get("state") or "?"),
                                str(result.get("detail") or ""),
                                bool(result.get("ok")))
            try:
                self.write_log_safe(
                    f"Service test {result.get('label') or service_id}: "
                    f"{result.get('state')} — {str(result.get('detail'))[:160]}")
            except Exception:
                pass

    def _collect_api_keys(self) -> dict:
        """Key values + subsystem flags edited on the API tab."""
        data = {
            "gemini_api_key": self.gemini_key_edit.text().strip(),
            "openai_api_key": self.openai_api_key.text().strip(),
            "anthropic_api_key": self.anthropic_api_key.text().strip(),
            "groq_api_key": self.groq_api_key.text().strip(),
            "custom_ai_api_key": self.custom_ai_api_key.text().strip(),
            "fish_audio_api_key": self.fish_audio_key_edit.text().strip(),
            "elevenlabs_api_key": self.elevenlabs_key_edit.text().strip(),
            "mem0_api_key": self.mem0_key_edit.text().strip(),
            "qdrant_api_key": self.qdrant_key_edit.text().strip(),
            "qdrant_url": self.qdrant_url_edit.text().strip() or DEFAULTS["qdrant_url"],
        }
        # Service hub credentials (OpenRouter, Spotify, weather, radar,
        # Instagram, smart home, Connect) — one generic pass, so adding a
        # service to services/hub.py needs no change here.
        for name, edit in (getattr(self, "_hub_fields", {}) or {}).items():
            data[name] = edit.text().strip()
        feats = dict((self._data or {}).get("features") or {})
        feats["mem0_memory"] = self._mem0_flag.isChecked()
        feats["qdrant_knowledge"] = self._qdrant_flag.isChecked()
        data["features"] = feats
        self._credential_rejects = self._reject_malformed_credentials(data)
        return data

    def _reject_malformed_credentials(self, data: dict) -> list[tuple[str, str]]:
        """Refuse credential values that are plainly not credentials.

        A key is one unbroken token. This machine stored 116 characters of
        pasted prose in ``openrouter_api_key``; a save must not be able to do
        that again. The field keeps the user's text (nothing is silently
        thrown away) but the value is not persisted — the previous stored
        value stays in place, and :meth:`_report_credential_rejects` says why.
        """
        hub = self._hub_module()
        if hub is None:
            return []
        problems: list[tuple[str, str]] = []
        stored = dict(self._data or {})
        for field, value in list(data.items()):
            if field == "features" or not isinstance(value, str) or not value.strip():
                continue
            meta = hub.key_meta(field) or {}
            if not meta.get("secret"):
                continue
            if any(ch.isspace() for ch in value):
                problems.append((field, "contains spaces — a key is one unbroken token"))
                data[field] = str(stored.get(field, "") or "")
        return problems

    def _report_credential_rejects(self) -> str:
        """Show which credential values were refused, and why ('' when none)."""
        problems = getattr(self, "_credential_rejects", None) or []
        if not problems:
            return ""
        for field, why in problems:
            self._set_key_state(field, "unavailable", f"Not saved: the value {why}.")
        try:
            self.write_log_safe(
                "Refused to save credential value(s) that are not keys: "
                + "; ".join(f"{field} ({why})" for field, why in problems))
        except Exception:
            pass
        return " · NOT SAVED: " + ", ".join(field.upper() for field, _why in problems)

    def _save_api_tab(self) -> None:
        """Persist every key edited on the API tab (single guarded write).

        Merges over the config file rather than ``self._data`` so provider
        panel values written between loads survive; the fields on the other
        tabs keep their own lifecycle (SAVE CHANGES collects them).
        """
        updates = self._collect_api_keys()
        try:
            from core import config_guard as _guard
            try:
                merged = dict(_guard.read(self._config_path))
            except Exception:
                merged = dict(self._data or {})
            feats = dict(merged.get("features") or {})
            feats.update(updates.pop("features", {}))
            merged.update(updates)
            merged["features"] = feats
            _ok, _msg = _guard.guard_write(merged, tag="api-keys", path=self._config_path)
            if not _ok:
                raise RuntimeError(_msg)
            self._data = _guard.read(self._config_path)
        except Exception:
            from ui_settings import save as _save_cfg
            feats = updates.pop("features", {})
            self._data = _save_cfg(self._config_path, features=feats, **updates)
        rejects = self._report_credential_rejects()
        self._status.setText("API KEYS SAVED" + rejects)
        self.settings_saved.emit(dict(self._data))

    def _build_plugins_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "PLUGINS  //  EXTEND JARVIS")
        self._plugin_list = QListWidget()
        self._plugin_list.setStyleSheet(
            f"QListWidget{{background:rgba(0,12,20,130);color:{TEXT_BRIGHT};border:1px solid {STROKE_MID};border-radius:10px;padding:5px;}}"
            "QListWidget::item{padding:7px 8px;border-bottom:1px solid rgba(41,135,170,45);}"
            "QListWidget::item:selected{background:rgba(0,103,143,105);}"
        )
        lay.addWidget(self._plugin_list, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        refresh = QPushButton("REFRESH")
        refresh.clicked.connect(self._reload_plugins)
        row.addWidget(refresh)
        toggle = QPushButton("ENABLE / DISABLE")
        toggle.setToolTip("Toggle the selected plugin. Takes effect on next voice request.")
        toggle.clicked.connect(self._toggle_plugin)
        row.addWidget(toggle)
        folder = QPushButton("OPEN FOLDER")
        folder.clicked.connect(self._open_plugins_folder)
        row.addWidget(folder)
        lay.addLayout(row)
        self._plugin_status = QLabel("")
        self._plugin_status.setWordWrap(True)
        self._plugin_status.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        lay.addWidget(self._plugin_status)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "PLUGINS")
        self._reload_plugins()

    def _iter_plugins(self):
        try:
            fn = getattr(self, "get_plugins", None)
            if callable(fn):
                return list(fn() or [])
        except Exception:
            pass
        return []

    def _reload_plugins(self):
        try:
            self._plugin_list.clear()
            plugins = self._iter_plugins()
            if not plugins:
                self._plugin_list.addItem("No plugins found. Drop a plugin folder into plugins/ and press REFRESH.")
            for p in plugins:
                try:
                    state = "ON" if p.get("enabled", True) else "OFF"
                    problem = "" if p.get("valid", True) else f"  ·  BROKEN: {p.get('error', '?')}"
                    item = QListWidgetItem(f"[{state}]  {p.get('name', '?')}  —  {p.get('description', '')[:90]}{problem}")
                    item.setData(Qt.ItemDataRole.UserRole, p.get("name", ""))
                    self._plugin_list.addItem(item)
                except Exception:
                    pass
            self._plugin_status.setText(f"{len(plugins)} plugin(s) discovered.")
        except Exception as exc:
            self._plugin_status.setText(f"Plugin scan failed: {exc}")

    def _toggle_plugin(self):
        item = self._plugin_list.currentItem()
        if item is None:
            self._plugin_status.setText("Select a plugin first.")
            return
        name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not name:
            return
        try:
            cur = {p.get("name"): bool(p.get("enabled", True)) for p in self._iter_plugins()}
            new_state = not cur.get(name, True)
            fn = getattr(self, "set_plugin_enabled", None)
            if callable(fn):
                fn(name, new_state)
                self._plugin_status.setText(f"Plugin '{name}' {'enabled' if new_state else 'disabled'}. Applies to the next request.")
                self._reload_plugins()
            else:
                self._plugin_status.setText("Plugin control is unavailable while JARVIS is offline.")
        except Exception as exc:
            self._plugin_status.setText(f"Toggle failed: {exc}")

    def _open_plugins_folder(self):
        try:
            fn = getattr(self, "open_plugins_folder", None)
            if callable(fn):
                fn()
                return
            import subprocess
            path = str(Path(__file__).resolve().parent / "plugins")
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606 - local UI action
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            self._plugin_status.setText(f"Could not open folder: {exc}")

    # ── EVOLUTION: the self-forged skills ──────────────────────────────────

    def _evolution_module(self):
        """The registry facade, or None when the engine cannot be imported."""
        try:
            from evolution import registry as skill_registry
            return skill_registry
        except Exception:
            return None

    def _build_evolution_tab(self):
        """Settings ▸ EVOLUTION — where the self-evolution engine is visible.

        Three honest readouts, all from the engine itself: which provider will
        synthesise (and which stored value was skipped because it is not a key),
        what has already been forged, and a forge box that runs on a worker
        thread so the window stays responsive while the Crucible tests.
        """
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "EVOLUTION  //  SELF-FORGED SKILLS")
        intro = QLabel(
            "When a request needs a capability no built-in tool or plugin covers, "
            "J.A.R.V.I.S. can write the tool himself. The goal is synthesised by "
            "the first working provider below, validated by the Skill Crucible "
            "(AST check, dependency resolution, isolated child-process test run) "
            "and only then written into plugins/ and hot-registered — no restart.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"background:{SURFACE_INSET};border:none;border-radius:9px;"
                            f"padding:10px;color:{TEXT_MID};")
        lay.addWidget(intro)

        chain_box = HudGroupBox("SYNTHESIS BRAIN")
        cv = QVBoxLayout(chain_box)
        self._evo_chain_label = QLabel("Reading the provider chain…")
        self._evo_chain_label.setWordWrap(True)
        self._evo_chain_label.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        cv.addWidget(self._evo_chain_label)
        chain_row = QHBoxLayout()
        chain_row.setSpacing(8)
        refresh_chain = QPushButton("REFRESH")
        refresh_chain.setToolTip("Re-read the provider chain and the forged skills.")
        refresh_chain.clicked.connect(self._refresh_evolution)
        chain_row.addWidget(refresh_chain)
        test_or = QPushButton("TEST OPENROUTER")
        test_or.setToolTip("One real request with the stored OpenRouter key "
                           "(the same test as the API tab).")
        test_or.clicked.connect(lambda: self._test_key("openrouter_api_key"))
        chain_row.addWidget(test_or)
        chain_row.addStretch(1)
        cv.addLayout(chain_row)
        lay.addWidget(chain_box)

        forge_box = HudGroupBox("FORGE A NEW SKILL")
        fv = QFormLayout(forge_box)
        self._evo_goal = QLineEdit()
        self._evo_goal.setPlaceholderText(
            "e.g. track the International Space Station and draw its ground track")
        self._evo_goal.setToolTip("Plain language: what the new skill must do. "
                                  "This is the goal the model writes code for.")
        self._evo_goal.returnPressed.connect(self._forge_skill)
        fv.addRow("Goal", self._evo_goal)
        self._evo_name = QLineEdit()
        self._evo_name.setPlaceholderText("optional snake_case name")
        fv.addRow("Name", self._evo_name)
        self._evo_forge_btn = QPushButton("FORGE IT")
        self._evo_forge_btn.setToolTip("Synthesise, sandbox-test and install the "
                                       "skill. Takes a minute or two.")
        self._evo_forge_btn.clicked.connect(self._forge_skill)
        fv.addRow("", self._evo_forge_btn)
        self._evo_status = QLabel("Nothing has been forged from this panel yet.")
        self._evo_status.setWordWrap(True)
        self._evo_status.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        fv.addRow("", self._evo_status)
        lay.addWidget(forge_box)

        skills_box = HudGroupBox("FORGED SKILLS")
        sv = QVBoxLayout(skills_box)
        self._evo_list = QListWidget()
        self._evo_list.setStyleSheet(
            f"QListWidget{{background:rgba(0,12,20,130);color:{TEXT_BRIGHT};"
            f"border:1px solid {STROKE_MID};border-radius:10px;padding:5px;}}"
            "QListWidget::item{padding:7px 8px;border-bottom:1px solid rgba(41,135,170,45);}"
            "QListWidget::item:selected{background:rgba(0,103,143,105);}")
        self._evo_list.itemDoubleClicked.connect(lambda _item: self._run_forged_skill())
        sv.addWidget(self._evo_list, 1)
        row = QHBoxLayout()
        row.setSpacing(8)
        for label, tip, handler in (
                ("RUN", "Run the selected skill now (with no parameters).",
                 self._run_forged_skill),
                ("ENABLE / DISABLE", "Toggle the selected skill; takes effect "
                                     "on the next request.", self._toggle_forged_skill),
                ("RE-VERIFY", "Re-run the Skill Crucible on the selected skill "
                              "(AST, output contract, dependencies, sandbox) and "
                              "refresh its quality record.", self._reverify_forged_skill),
                ("DELETE", "Remove the selected skill from plugins/.",
                 self._delete_forged_skill),
                ("OPEN FOLDER", "Open plugins/ in the file manager.",
                 self._open_forged_folder)):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            row.addWidget(button)
        row.addStretch(1)
        sv.addLayout(row)
        self._evo_skill_status = QLabel("")
        self._evo_skill_status.setWordWrap(True)
        self._evo_skill_status.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        sv.addWidget(self._evo_skill_status)
        lay.addWidget(skills_box)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "EVOLUTION")
        self._index_setting("EVOLUTION", "Forge a new skill", self._evo_goal)

        # Synthesis takes a minute or two, so it runs on a worker thread and the
        # GUI thread applies the verdict from a timer — exactly the pattern the
        # service hub uses. A parked None means "a run is in flight".
        self._evo_pending: dict = {}
        self._evo_timer = QTimer(self)
        self._evo_timer.setInterval(300)
        self._evo_timer.timeout.connect(self._drain_evolution)
        self._evo_timer.start()
        self._refresh_evolution()

    def _refresh_evolution(self):
        """Read the real synthesis chain and the forged skills on disk."""
        label = getattr(self, "_evo_chain_label", None)
        if label is None:
            return
        try:
            from evolution import forge as _forge
            rows = _forge.provider_diagnostics()
            labels = _forge.PROVIDER_LABELS
        except Exception:
            rows, labels = [], {}
        if not rows:
            label.setText("The self-evolution engine could not be read on this "
                          "install, so no provider can be named.")
        else:
            usable = [r for r in rows if r["state"] == "usable"]
            lines: list[str] = []
            if usable:
                order = " → ".join(labels.get(r["provider"], r["provider"])
                                   for r in usable)
                lines.append(f"Will forge with: {order} — the first working one "
                             f"writes the skill, the rest are fallbacks.")
            else:
                lines.append("No provider key is usable right now, so a forge run "
                             "would refuse until one is added on the API tab.")
            for r in rows:
                if r["state"] != "usable":
                    lines.append(f"· {labels.get(r['provider'], r['provider'])}: "
                                 f"{r['state']} — {r['detail']}")
            label.setText("\n".join(lines))
        self._reload_forged_skills()

    def _reload_forged_skills(self):
        lst = getattr(self, "_evo_list", None)
        status = getattr(self, "_evo_skill_status", None)
        if lst is None or status is None:
            return
        lst.clear()
        registry = self._evolution_module()
        if registry is None:
            status.setText("The self-evolution engine is not importable.")
            return
        try:
            rows = registry.list_skills()
        except Exception as exc:
            status.setText(f"Reading the forged skills failed: {exc}")
            return
        for row in rows:
            state = ("LIVE" if row.get("live")
                     else ("DISABLED" if not row.get("enabled") else "ON DISK"))
            goal = f"  ·  {row['goal']}" if row.get("goal") else ""
            score = int(row.get("quality_score") or 0)
            score_text = f"  ·  Q{score}" if score else ""
            item = QListWidgetItem(
                f"[{state}]  {row['name']}  v{row.get('version', '1.0.0')}{score_text}  ·  "
                f"{int(row.get('invocations') or 0)} call(s){goal}")
            item.setData(Qt.ItemDataRole.UserRole, row["name"])
            tips: list[str] = []
            if score:
                tips.append(f"Crucible quality score: {score}/100")
            hazards = row.get("hazards") or []
            if hazards:
                tips.append("Hazard notes: "
                            + ", ".join(str(h.get("hazard")) for h in hazards[:6]))
            if row.get("last_error"):
                tips.append(f"Last error: {row['last_error']}")
            if tips:
                item.setToolTip("\n".join(tips))
            lst.addItem(item)
        status.setText(
            f"{len(rows)} forged skill(s). Double-click a row to run it; each one "
            f"is an ordinary plugin in plugins/ and is listed in the Plugin Manager."
            if rows else
            "Nothing has been forged yet. Describe a capability above and press "
            "FORGE IT — or just ask J.A.R.V.I.S. to learn something new.")

    def _selected_forged_skill(self) -> str:
        item = self._evo_list.currentItem() if getattr(self, "_evo_list", None) else None
        if item is None:
            self._evo_skill_status.setText("Select a forged skill first.")
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    def _run_forged_skill(self):
        name = self._selected_forged_skill()
        registry = self._evolution_module()
        if not name or registry is None:
            return
        try:
            out = registry.run_skill(name)
        except Exception as exc:
            out = f"Running '{name}' failed: {exc}"
        self._evo_skill_status.setText(str(out)[:500])
        self.write_log_safe(f"Forged skill '{name}' ran: {str(out)[:160]}")
        self._reload_forged_skills()

    def _toggle_forged_skill(self):
        name = self._selected_forged_skill()
        registry = self._evolution_module()
        if not name or registry is None:
            return
        try:
            out = registry.toggle_skill(name)
        except Exception as exc:
            out = f"Toggling '{name}' failed: {exc}"
        self._evo_skill_status.setText(str(out)[:400])
        self.write_log_safe(f"Forged skill '{name}': {str(out)[:160]}")
        self._reload_forged_skills()

    def _reverify_forged_skill(self):
        """Re-run the Crucible on the selected skill (off the GUI thread)."""
        name = self._selected_forged_skill()
        registry = self._evolution_module()
        if not name or registry is None:
            return
        if "verify" in self._evo_pending:
            self._evo_skill_status.setText("A verification is already running.")
            return
        self._evo_pending["verify"] = None
        self._evo_skill_status.setText(
            f"Re-verifying '{name}' in the Crucible — AST, output contract, "
            f"dependencies, then an isolated sandbox run…")
        try:
            import threading as _threading
            _threading.Thread(target=self._reverify_async, args=(name,),
                              name="skill-verify", daemon=True).start()
        except Exception as exc:
            self._evo_pending.pop("verify", None)
            self._evo_skill_status.setText(f"The verification could not start: {exc}")

    def _reverify_async(self, name: str):
        try:
            registry = self._evolution_module()
            text = str(registry.verify_skill(name)) if registry is not None else \
                "The self-evolution engine is not importable."
        except Exception as exc:
            text = f"Verification failed: {type(exc).__name__}: {exc}"
        self._evo_pending["verify"] = text

    def _delete_forged_skill(self):
        name = self._selected_forged_skill()
        registry = self._evolution_module()
        if not name or registry is None:
            return
        confirm = QMessageBox.question(
            self, "Delete forged skill",
            f"Delete '{name}' from plugins/? The skill file is removed from disk.")
        if confirm != QMessageBox.StandardButton.Yes:
            self._evo_skill_status.setText(f"'{name}' was not deleted.")
            return
        try:
            out = registry.delete_skill(name)
        except Exception as exc:
            out = f"Deleting '{name}' failed: {exc}"
        self._evo_skill_status.setText(str(out)[:400])
        self.write_log_safe(f"Forged skill '{name}' deleted: {str(out)[:160]}")
        self._reload_forged_skills()

    def _open_forged_folder(self):
        registry = self._evolution_module()
        path = ""
        try:
            from evolution import vault
            path = str(vault.plugins_dir())
        except Exception:
            path = str(Path(__file__).resolve().parent / "plugins")
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606 - local UI action
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
            self._evo_skill_status.setText(f"Opened {path}")
        except Exception as exc:
            self._evo_skill_status.setText(f"Could not open the folder: {exc}")

    def _forge_skill(self):
        """Start one forge run on a worker thread (never on the GUI thread)."""
        if getattr(self, "_evo_forge_btn", None) is None:
            return
        goal = self._evo_goal.text().strip()
        name = self._evo_name.text().strip()
        if len(goal) < 6:
            self._evo_status.setStyleSheet(f"color:{AMBER};background:transparent;")
            self._evo_status.setText("Describe the skill first — plain language, "
                                     "what it should do and with what.")
            return
        if "forge" in self._evo_pending:
            self._evo_status.setText("A forge run is already in progress.")
            return
        self._evo_pending["forge"] = None
        self._evo_forge_btn.setEnabled(False)
        self._evo_status.setStyleSheet(f"color:{ACCENT_TEXT};background:transparent;")
        self._evo_status.setText(
            "FORGING — the model is writing the skill, then the Crucible tests "
            "it in isolation. This takes a minute or two; nothing is installed "
            "unless every check passes.")
        try:
            import threading as _threading
            _threading.Thread(target=self._forge_skill_async, args=(goal, name),
                              name="skill-forge", daemon=True).start()
        except Exception as exc:
            self._evo_pending.pop("forge", None)
            self._evo_forge_btn.setEnabled(True)
            self._evo_status.setText(f"The forge could not start: {exc}")

    def _forge_skill_async(self, goal: str, name: str):
        try:
            registry = self._evolution_module()
            if registry is None:
                result = {"ok": False,
                          "message": "The self-evolution engine is not importable."}
            else:
                text = str(registry.handler({"action": "forge", "goal": goal,
                                             "name": name}))
                ok = " is live" in text or "next launch" in text
                result = {"ok": ok, "message": text}
        except Exception as exc:
            result = {"ok": False,
                      "message": f"The forge failed: {type(exc).__name__}: {exc}"}
        self._evo_pending["forge"] = result

    def _drain_evolution(self):
        """Apply finished forge/verify verdicts on the GUI thread."""
        pending = getattr(self, "_evo_pending", None)
        if not pending:
            return
        self._drain_forge(pending)
        self._drain_verify(pending)

    def _drain_forge(self, pending: dict):
        if "forge" not in pending:
            return
        result = pending.get("forge")
        if result is None:
            return                     # still running
        pending.pop("forge", None)
        try:
            self._evo_forge_btn.setEnabled(True)
        except Exception:
            pass
        message = str((result or {}).get("message") or
                      "The forge finished without a verdict.")
        ok = bool((result or {}).get("ok"))
        self._evo_status.setStyleSheet(
            f"color:{GREEN if ok else AMBER};background:transparent;")
        self._evo_status.setText(message[:700])
        self.write_log_safe(f"Skill forge: {message[:200]}")
        self._reload_forged_skills()

    def _drain_verify(self, pending: dict):
        if "verify" not in pending:
            return
        text = pending.get("verify")
        if text is None:
            return                     # still running
        pending.pop("verify", None)
        self._evo_skill_status.setText(str(text)[:500])
        self.write_log_safe(f"Skill verify: {str(text)[:200]}")
        self._reload_forged_skills()

    def _build_diagnostics_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "DIAGNOSTICS  //  HEALTH · CHECKS · LOG")
        self._diag_box = HudGroupBox("SYSTEM CHECKS")
        dv = QVBoxLayout(self._diag_box)
        self._diag_list = QListWidget()
        self._diag_list.setStyleSheet(
            f"QListWidget{{background:rgba(0,12,20,130);color:{TEXT_BRIGHT};border:1px solid {STROKE_MID};border-radius:10px;padding:5px;}}"
            "QListWidget::item{padding:6px 8px;border-bottom:1px solid rgba(41,135,170,45);}"
        )
        dv.addWidget(self._diag_list)
        run = QPushButton("RUN CHECKS")
        run.setToolTip("Verify engine, keys, files and devices without speaking.")
        run.clicked.connect(self._run_diagnostics)
        dv.addWidget(run)
        lay.addWidget(self._diag_box)
        log_box = HudGroupBox("RECENT ACTIVITY")
        lv = QVBoxLayout(log_box)
        self._diag_log = QTextEdit()
        self._diag_log.setReadOnly(True)
        self._diag_log.setMinimumHeight(150)
        self._diag_log.setStyleSheet(
            f"QTextEdit{{background:rgba(0,12,20,130);color:{ACCENT_BRIGHT};border:1px solid {STROKE_MID};border-radius:10px;padding:8px;font:8pt 'Share Tech Mono';}}"
        )
        lv.addWidget(self._diag_log)
        copy_log = QPushButton("REFRESH LOG")
        copy_log.clicked.connect(self._refresh_diag_log)
        lv.addWidget(copy_log)
        lay.addWidget(log_box)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "DIAG")
        self._refresh_diag_log()

    def _run_diagnostics(self):
        import platform as _plat
        results = []
        def _ok(name, good, detail=""):
            results.append((name, bool(good), str(detail or "")))
        _ok("Python", True, _plat.python_version())
        try:
            import PyQt6.QtCore as _qc
            _ok("PyQt6", True, _qc.QT_VERSION_STR)
        except Exception as exc:
            _ok("PyQt6", False, exc)
        try:
            from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa
            _ok("WebEngine", True, "embedded browser ready")
        except Exception:
            _ok("WebEngine", False, "pip install PyQt6-WebEngine")
        try:
            from core.tts import create_tts_player, voice_status
            _player = create_tts_player(self._collect())
            _player._ensure_engine()   # lazy player: force the real engine build here
            _st = voice_status()
            _ok("TTS engine", _st.state != "ERROR",
                f"{self._selected_provider()}"
                + (f" · {_st.error[:60]}" if _st.error else ""))
        except Exception as exc:
            _ok("TTS engine", False, str(exc)[:100])
        cfg = self._collect()
        try:
            _file_cfg = json.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception:
            _file_cfg = {}
        _ok("Gemini key", bool(str(_file_cfg.get("gemini_api_key", "") or "").strip()), "voice session")
        _ok("ElevenLabs key", str(cfg.get("elevenlabs_api_key", "") or "").startswith("sk_"), "cloud voice")
        _ok("Fish key", bool(str(cfg.get("fish_audio_api_key", "") or "").strip()), "cloud voice")
        try:
            mem = Path(__file__).resolve().parent / "memory" / "long_term.json"
            _ok("Memory file", mem.is_file(), f"{mem.stat().st_size} bytes" if mem.is_file() else "missing")
        except Exception as exc:
            _ok("Memory file", False, exc)
        try:
            import shutil as _sh
            free_gb = _sh.disk_usage(str(Path.home())).free / (1024 ** 3)
            _ok("Disk free", free_gb > 1.0, f"{free_gb:.1f} GB")
        except Exception as exc:
            _ok("Disk free", False, exc)
        try:
            self._diag_list.clear()
            for name, good, detail in results:
                mark = "OK " if good else "FAIL"
                self._diag_list.addItem(f"[{mark}]  {name}" + (f"  —  {detail}" if detail else ""))
            self._status.setText("CHECKS DONE")
        except Exception as exc:
            self._status.setText(f"CHECKS ERROR: {exc}")

    def _refresh_diag_log(self):
        lines = []
        try:
            fn = getattr(self, "get_activity_log", None)
            if callable(fn):
                lines = list(fn(60) or [])
        except Exception:
            pass
        try:
            self._diag_log.setPlainText("\n".join(lines) if lines else "No activity yet — open the app and talk to JARVIS first.")
        except Exception:
            pass

    # ── J.OPTIMIZER (safe, reversible Windows maintenance) ──────────────
    def _build_joptimizer_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "J.OPTIMIZER  //  SAFE · REVERSIBLE · SNAPSHOT-FIRST")
        # Every JOptimizer button is tracked so it disables while an action runs
        # (no double-submit) and re-enables on completion.
        self._jopt_buttons = []

        # Status bar: elevation mode + one-click revert of the latest snapshot.
        bar = QFrame()
        bar.setStyleSheet(f"background:{SURFACE_INSET};border:1px solid {BORDER_FAINT};border-radius:8px;")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(10)
        self._jopt_mode = QLabel("●  DETECTING…")
        self._jopt_mode.setStyleSheet(f"color:{TEXT_MID};font:800 8pt 'Orbitron';background:transparent;")
        bl.addWidget(self._jopt_mode)
        bl.addStretch(1)
        revert = QPushButton("⟲ REVERT LAST SNAPSHOT")
        revert.setToolTip("Undo the most recent JOptimizer change using its stored snapshot. Safe.")
        revert.clicked.connect(self._jopt_revert_last)
        self._jopt_buttons.append(revert)
        bl.addWidget(revert)
        lay.addWidget(bar)

        # ── Live progress + per-action summary ─────────────────────────
        prog = QFrame()
        prog.setStyleSheet(f"background:{SURFACE_INSET};border:1px solid {BORDER_FAINT};border-radius:8px;")
        pl = QVBoxLayout(prog)
        pl.setContentsMargins(12, 9, 12, 9)
        pl.setSpacing(6)
        self._jopt_progress_label = QLabel("Idle — every action reports real progress here.")
        self._jopt_progress_label.setStyleSheet(f"color:{TEXT_MID};font:700 8pt 'Exo 2';background:transparent;letter-spacing:0.5px;")
        self._jopt_progress = QProgressBar()
        self._jopt_progress.setRange(0, 100)
        self._jopt_progress.setValue(0)
        self._jopt_progress.setTextVisible(True)
        self._jopt_progress.setFixedHeight(18)
        self._jopt_progress.setStyleSheet(
            f"QProgressBar{{background:rgba(0,12,20,150);border:1px solid {STROKE_MID};"
            f"border-radius:6px;color:{TEXT_BRIGHT};font:700 7.5pt 'Share Tech Mono';}}"
            f"QProgressBar::chunk{{background:{ACCENT};border-radius:5px;}}")
        self._jopt_summary = QLabel("")
        self._jopt_summary.setWordWrap(True)
        self._jopt_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._jopt_summary.setStyleSheet(
            f"color:{GREEN};font:700 7.8pt 'Share Tech Mono';background:rgba(9,64,50,60);"
            "border:1px solid rgba(93,240,182,60);border-radius:6px;padding:7px 9px;")
        self._jopt_summary.hide()
        pl.addWidget(self._jopt_progress_label)
        pl.addWidget(self._jopt_progress)
        pl.addWidget(self._jopt_summary)
        lay.addWidget(prog)

        # ── System health dashboard ────────────────────────────────────
        health = HudGroupBox("SYSTEM HEALTH  //  LIVE")
        grid = QGridLayout(health)
        grid.setSpacing(8)
        self._jopt_cpu = self._jopt_metric(grid, 0, 0, "CPU", "--")
        self._jopt_ram = self._jopt_metric(grid, 0, 1, "RAM", "--")
        self._jopt_disk = self._jopt_metric(grid, 0, 2, "DISK FREE", "--")
        self._jopt_up = self._jopt_metric(grid, 1, 0, "UPTIME", "--")
        self._jopt_health = self._jopt_metric(grid, 1, 1, "HEALTH", "--")
        self._jopt_gpu = self._jopt_metric(grid, 1, 2, "PROCESSES", "--")
        scan = QPushButton("SMART SCAN (read-only)")
        scan.setToolTip("Sample CPU/RAM/disk and report a health score. Makes no changes.")
        scan.clicked.connect(lambda: self._jopt_async("Smart scan", self._jopt_smart_scan))
        grid.addWidget(scan, 2, 0, 1, 3)
        lay.addWidget(health)

        # ── Optimization profiles ──────────────────────────────────────
        prof = HudGroupBox("OPTIMIZATION PROFILES  //  SNAPSHOT FIRST")
        pv = QVBoxLayout(prof)
        for text, name, tip in (
            ("SAFE PERFORMANCE", "safe", "Power plan + Game DVR off + Game Mode + performance visuals."),
            ("GAMING", "gaming", "High performance + Game Mode + background capture off."),
            ("LOW SPEC", "lowspec", "Lighter UI: visuals + transparency + widgets off, capture disabled."),
        ):
            self._jopt_row(pv, text, tip,
                           lambda n=name: self._jopt_profile(n))
        lay.addWidget(prof)

        # ── J.A.R.V.I.S optimization (this app's own smoothness) ───────
        jr = HudGroupBox("J.A.R.V.I.S OPTIMIZATION  //  NO LAG, NO CRASHES")
        jv = QVBoxLayout(jr)
        jv.addWidget(self._jopt_note(
            "Every action here is reversible from a snapshot. The Ultimate suites "
            "never touch Winsock/TCP, DISM/SFC, or system services."))
        self._jopt_row(jv, "J.A.R.V.I.S ULTIMATE OPTIMIZE", "Snapshot, then apply a curated set of safe, reversible tweaks so JARVIS stays smooth.",
                       lambda: self._jopt_ultimate(auto=False), primary=True)
        self._jopt_row(jv, "⚡ AUTO ULTIMATE OPTIMIZE", "One click: snapshot first, then run the full conservative suite automatically. One-click revert restores it.",
                       lambda: self._jopt_ultimate(auto=True), primary=True)
        self._jopt_row(jv, "MEMORY PRESSURE CHECK", "Read-only: how much RAM is free + the busiest processes. Nothing is terminated.",
                       lambda: self._jopt_async("Memory pressure", self._jopt_jopt_mod().memory_pressure))
        self._jopt_row(jv, "PROCESS INSPECTOR", "List top processes by CPU/RAM. Inspection only — it never kills anything.",
                       lambda: self._jopt_async("Process inspector", lambda: "\n".join(self._jopt_jopt_mod().top_processes(10))))
        self._jopt_row(jv, "JARVIS CACHE PREVIEW", "Show size of auto-regenerable caches (__pycache__ / .pytest_cache). No deletion.",
                       lambda: self._jopt_async("Cache preview", self._jopt_cache_preview))
        self._jopt_row(jv, "CLEAN JARVIS CACHES", "Delete only auto-regenerable JARVIS caches. Safe; they rebuild on next run.",
                       lambda: self._jopt_async("Clean caches", self._jopt_cache_clean))
        lay.addWidget(jr)

        # ── Privacy / debloat (reversible) ─────────────────────────────
        priv = HudGroupBox("PRIVACY / DEBLOAT  //  REVERSIBLE")
        prv = QVBoxLayout(priv)
        self._jopt_row(prv, "DISABLE GAME DVR CAPTURE", "Stops background game recording for this user (HKCU).", self._jopt_gamedvr)
        self._jopt_row(prv, "PRIVACY EXTRAS", "Advertising ID + tailored-experience diagnostics off (HKCU).", self._jopt_privacy)
        self._jopt_row(prv, "HIDE WIDGETS", "Hide the taskbar Widgets button (HKCU).", self._jopt_widgets)
        self._jopt_row(prv, "REMOVE TELEMETRY (ADMIN)", "⚠ HKLM diagnostic policy + services. Needs Administrator.", self._jopt_telemetry)
        lay.addWidget(priv)

        # ── Service manager ────────────────────────────────────────────
        svc = HudGroupBox("SERVICE MANAGER  //  REVERSIBLE TOGGLES")
        sv = QVBoxLayout(svc)
        sv.addWidget(self._jopt_note("Windows Update is high risk — leaving it off blocks security updates."))
        self._jopt_service_labels = {}
        for item in self._jopt_jopt_mod().SAFE_SERVICES:
            row = QHBoxLayout()
            lab = QLabel(item["label"])
            lab.setStyleSheet(f"color:{TEXT_BRIGHT};background:transparent;")
            row.addWidget(lab, 1)
            state = QLabel("--")
            state.setStyleSheet(f"color:{TEXT_DIM};background:transparent;font:8pt 'Share Tech Mono';")
            self._jopt_service_labels[item["id"]] = (item, state)
            row.addWidget(state)
            btn = QPushButton("Toggle")
            btn.setToolTip(f"Reversibly change the startup type of {item['id']}.")
            btn.clicked.connect(lambda _=False, s=item["id"], l=item["label"], r=item["risk"]: self._jopt_toggle_service(s, l, r))
            self._jopt_buttons.append(btn)
            row.addWidget(btn)
            sv.addLayout(row)
        lay.addWidget(svc)

        # ── Network tools ──────────────────────────────────────────────
        net = HudGroupBox("NETWORK  //  DIAGNOSTICS + REPAIRS")
        nv = QHBoxLayout(net)
        self._jopt_row(nv, "FLUSH DNS", "Clear the local resolver cache.", lambda: self._jopt_async("Flush DNS", self._jopt_jopt_mod().flush_dns))
        self._jopt_row(nv, "LATENCY TEST", "Ping 1.1.1.1 — a measurement, not a tuning claim.", lambda: self._jopt_async("Latency", self._jopt_jopt_mod().latency_test))
        self._jopt_row(nv, "NETWORK INFO", "Show adapters, addresses and gateways.", lambda: self._jopt_async("Network info", self._jopt_jopt_mod().network_info))
        lay.addWidget(net)
        nv2 = QHBoxLayout()
        self._jopt_row(nv2, "WINSOCK RESET (ADMIN)", "⚠ Repairs the socket catalog; may need reboot. Reversible only manually.", self._jopt_winsock)
        self._jopt_row(nv2, "IP STACK RESET (ADMIN)", "⚠ Resets TCP/IP to defaults; reboot recommended.", self._jopt_ipreset)
        lay.addLayout(nv2)

        # ── Clean / repair ─────────────────────────────────────────────
        clr = HudGroupBox("CLEAN / REPAIR  //  LOGGED")
        cv = QVBoxLayout(clr)
        cv.addWidget(self._jopt_note("Temp cleanup only targets Windows Temp folders; your Documents/Downloads are never touched."))
        self._jopt_row(cv, "CLEANUP PREVIEW", "Estimate eligible temp files without deleting.", lambda: self._jopt_async("Cleanup preview", self._jopt_temp_preview))
        self._jopt_row(cv, "TEMP CLEANUP", "Delete temp files Windows allows; locked files are skipped.", self._jopt_temp_clean)
        self._jopt_row(cv, "DRIVE OPTIMIZE (ADMIN)", "⚠ defrag /O — Windows chooses the right method per drive type.", self._jopt_defrag)
        self._jopt_row(cv, "DISM REPAIR (ADMIN)", "⚠ Repair the Windows component store.", self._jopt_dism)
        self._jopt_row(cv, "SFC CHECK (ADMIN)", "⚠ Verify protected system files.", self._jopt_sfc)
        self._jopt_row(cv, "DISK SCAN (ADMIN)", "⚠ Online C: file-system scan (non-destructive /scan).", self._jopt_chkdsk)
        lay.addWidget(clr)

        # ── Backups & recovery ─────────────────────────────────────────
        bk = HudGroupBox("BACKUPS & RECOVERY")
        bv = QVBoxLayout(bk)
        self._jopt_backup_list = QListWidget()
        self._jopt_backup_list.setStyleSheet(
            f"QListWidget{{background:rgba(0,12,20,130);color:{TEXT_BRIGHT};border:1px solid {STROKE_MID};border-radius:10px;padding:5px;}}"
            "QListWidget::item{padding:5px 8px;border-bottom:1px solid rgba(41,135,170,45);}")
        self._jopt_backup_list.setMinimumHeight(90)
        bv.addWidget(self._jopt_backup_list)
        brow = QHBoxLayout()
        r1 = QPushButton("REFRESH LIST"); r1.clicked.connect(self._jopt_refresh_backups); brow.addWidget(r1)
        r2 = QPushButton("RESTORE SELECTED"); r2.clicked.connect(self._jopt_restore_selected); brow.addWidget(r2)
        r3 = QPushButton("OPEN BACKUP FOLDER"); r3.clicked.connect(self._jopt_open_folder); brow.addWidget(r3)
        bv.addLayout(brow)
        lay.addWidget(bk)

        # ── Shared log ─────────────────────────────────────────────────
        logbox = HudGroupBox("J.OPTIMIZER LOG")
        lv = QVBoxLayout(logbox)
        self._jopt_log_view = QTextEdit()
        self._jopt_log_view.setReadOnly(True)
        self._jopt_log_view.setMinimumHeight(120)
        self._jopt_log_view.setStyleSheet(
            f"QTextEdit{{background:rgba(0,12,20,130);color:{ACCENT_BRIGHT};border:1px solid {STROKE_MID};"
            f"border-radius:10px;padding:8px;font:8pt 'Share Tech Mono';}}")
        lv.addWidget(self._jopt_log_view)
        lay.addWidget(logbox)

        lay.addStretch(1)
        self._jopt_tab_index = self.tabs.addTab(scroll, "J.OPTIMIZER")

        # Progress + completion signals cross from the worker thread to the GUI.
        self.jopt_progress.connect(self._jopt_on_progress)
        self.jopt_done.connect(self._jopt_on_done)

        # Live health only while this tab is shown (no background cost).
        self._jopt_timer = QTimer(self)
        self._jopt_timer.setInterval(2500)
        self._jopt_timer.timeout.connect(self._jopt_refresh_health)
        self.tabs.currentChanged.connect(self._jopt_on_tab)
        self._jopt_refresh_backups()
        self._jopt_log("J.Optimizer ready. All actions are snapshot-protected and reversible.")

    # ---- JOptimizer helpers ------------------------------------------------
    def _jopt_jopt_mod(self):
        from core import joptimizer
        return joptimizer

    def _jopt_note(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{TEXT_DIM};background:transparent;padding:2px 2px 4px 2px;")
        return lbl

    def _jopt_metric(self, grid, row, col, label, value) -> QLabel:
        frame = QFrame()
        frame.setStyleSheet(f"background:{SURFACE_INSET};border:1px solid {BORDER_FAINT};border-radius:8px;")
        f = QVBoxLayout(frame)
        f.setContentsMargins(12, 8, 12, 8)
        f.setSpacing(2)
        cap = QLabel(label)
        cap.setStyleSheet(f"color:{TEXT_FAINT};font:700 7pt 'Exo 2';background:transparent;letter-spacing:1px;")
        val = QLabel(value)
        val.setStyleSheet(f"color:{ACCENT_BRIGHT};font:800 12pt 'Orbitron';background:transparent;")
        f.addWidget(cap)
        f.addWidget(val)
        grid.addWidget(frame, row, col)
        return val

    def _jopt_row(self, layout, text, tip, slot, primary=False) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tip)
        if primary:
            btn.setObjectName("PrimaryButton")
        btn.clicked.connect(lambda _=False: self._jopt_guarded(slot))
        layout.addWidget(btn)
        self._jopt_buttons.append(btn)
        return btn

    def _jopt_guarded(self, slot):
        # Guard against a non-Windows host where the backend ops can't run.
        try:
            if not self._jopt_jopt_mod().IS_WINDOWS:
                self._jopt_log("✗ JOptimizer actions require Windows.")
                return
        except Exception as exc:
            self._jopt_log(f"✗ Backend unavailable: {exc}")
            return
        try:
            slot()
        except Exception as exc:
            self._jopt_log(f"✗ {exc}")

    def _jopt_log(self, text):
        import time as _t
        try:
            self._jopt_log_view.append(f"[{_t.strftime('%H:%M:%S')}] {text}")
        except Exception:
            pass
        self.write_log_safe(str(text))

    def _jopt_async(self, title, fn, with_progress=False):
        import threading
        self._jopt_begin_action(title)

        def _progress(pct, msg):
            # Emit (not direct-call) so the worker thread never touches widgets.
            try:
                self.jopt_progress.emit(int(pct), str(msg))
            except Exception:
                pass

        def _run():
            try:
                msg = fn(_progress) if with_progress else fn()
                ok = True
            except Exception as exc:
                msg, ok = str(exc), False
            try:
                self.jopt_done.emit(bool(ok), str(title), str(msg or ""))
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _jopt_begin_action(self, title):
        # Show an indeterminate (busy) bar immediately so the click always has a
        # visible effect; real percentages switch it to determinate on first tick.
        for b in getattr(self, "_jopt_buttons", []):
            try:
                b.setEnabled(False)
            except Exception:
                pass
        try:
            self._jopt_progress.setRange(0, 0)
            self._jopt_progress.setFormat("running…")
            self._jopt_progress_label.setText(f"▶  {title} — running…")
            self._jopt_progress_label.setStyleSheet(
                f"color:{ACCENT};font:700 8pt 'Exo 2';background:transparent;letter-spacing:0.5px;")
            self._jopt_summary.hide()
            self._jopt_summary.setText("")
        except Exception:
            pass
        self._jopt_log(f"▶ {title}…")

    def _jopt_on_progress(self, pct, msg):
        try:
            if self._jopt_progress.maximum() == 0:
                self._jopt_progress.setRange(0, 100)   # first real percent
            self._jopt_progress.setFormat("%p%")
            self._jopt_progress.setValue(pct)
            if msg:
                self._jopt_progress_label.setText(f"{pct}%  ·  {msg}")
        except Exception:
            pass

    def _jopt_on_done(self, ok, title, summary):
        try:
            self._jopt_progress.setRange(0, 100)
            self._jopt_progress.setFormat("%p%")
            self._jopt_progress.setValue(100 if ok else 0)
            verdict = ("✓  " if ok else "✗  ") + title + (" complete" if ok else " failed")
            self._jopt_progress_label.setText(verdict)
            self._jopt_progress_label.setStyleSheet(
                f"color:{GREEN if ok else RED};font:700 8pt 'Exo 2';background:transparent;letter-spacing:0.5px;")
            # Summary of what was optimized (real, from the backend's step report).
            body = summary or ("No changes reported." if ok else "Action did not complete.")
            self._jopt_summary.setText(self._jopt_format_summary(title, ok, body))
            self._jopt_summary.show()
        except Exception:
            pass
        self._jopt_log(("✓ " if ok else "✗ ") + title + ((" — " + summary) if summary else ""))
        for b in getattr(self, "_jopt_buttons", []):
            try:
                b.setEnabled(True)
            except Exception:
                pass
        self._jopt_refresh_backups()
        self._jopt_refresh_health()
        # Settle the bar back to empty shortly after finishing.
        QTimer.singleShot(1600, self._jopt_reset_bar)

    def _jopt_reset_bar(self):
        try:
            self._jopt_progress.setRange(0, 100)
            self._jopt_progress.setFormat("%p%")
            self._jopt_progress.setValue(0)
        except Exception:
            pass

    def _jopt_format_summary(self, title, ok, body):
        """Compact 'what was optimized' digest from the backend step report.

        The backend returns '✓ step: result' lines; the summary shows the done
        steps and flags any that were skipped, so it never implies a change that
        did not actually happen.
        """
        lines = [ln.strip() for ln in str(body).replace("\r", "").split("\n") if ln.strip()]
        applied = [ln for ln in lines if ln.startswith("✓")]
        skipped = [ln for ln in lines if ln.startswith("⚠")]
        if not applied and not skipped:
            head = (title + (" — done" if ok else " — failed"))
            return head + ("\n" + body if body else "")
        out = [f"OPTIMIZED ({len(applied)} applied" + (f", {len(skipped)} skipped" if skipped else "") + "):"]
        out.extend(applied[:12])
        out.extend(skipped[:6])
        return "\n".join(out)

    def _jopt_confirm(self, title, text, risk="Low") -> bool:
        icon = QMessageBox.Icon.Warning if risk in ("Medium", "High") else QMessageBox.Icon.Question
        res = QMessageBox.question(
            self, f"JOptimizer — {title}",
            f"{text}\n\nRisk: {risk}. A reversible snapshot is stored first so this can be undone.",
            icon | QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return res == QMessageBox.StandardButton.Yes

    def _jopt_snapshot_then(self, title, label, fn):
        """Take a named snapshot, then run fn() (which does the actual change)."""
        def _fn():
            J = self._jopt_jopt_mod()
            snap = J.snapshot_state(label)
            result = fn()
            return f"{result}  (snapshot: {snap.name})"
        self._jopt_async(title, _fn)

    # ---- JOptimizer actions ------------------------------------------------
    def _jopt_smart_scan(self):
        return self._jopt_jopt_mod().smart_scan()

    def _jopt_profile(self, name):
        if not self._jopt_confirm("Apply Profile", f"Apply the {name.upper()} profile. Every change is reversible from the snapshot.", "Low"):
            return
        self._jopt_async(f"Profile {name}",
                         lambda p: self._jopt_jopt_mod().apply_profile(name, progress=p),
                         with_progress=True)

    def _jopt_ultimate(self, auto):
        label = "Auto Ultimate Optimize" if auto else "J.A.R.V.I.S Ultimate Optimize"
        desc = ("Creates a snapshot, then applies the conservative, reversible suite "
                "(power plan, capture/visuals, privacy, cache clean). No Winsock/DISM/"
                "services are touched. Use REVERT LAST SNAPSHOT to undo.")
        if not self._jopt_confirm(label, desc, "Low"):
            return
        self._jopt_async(label,
                         lambda p: self._jopt_jopt_mod().jarvis_ultimate_optimize(auto=auto, progress=p),
                         with_progress=True)

    def _jopt_revert_last(self):
        if not self._jopt_confirm("Revert", "Restore the most recent JOptimizer snapshot (registry + services back to prior values).", "Low"):
            return
        self._jopt_async("Revert last snapshot", self._jopt_jopt_mod().jarvis_revert_latest)

    def _jopt_cache_preview(self):
        count, total = self._jopt_jopt_mod().jarvis_cache_preview()
        return f"JARVIS caches: {count} files, ~{self._jopt_jopt_mod()._fmt_bytes(total)} reclaimable."

    def _jopt_cache_clean(self):
        if not self._jopt_confirm("Clean caches", "Delete auto-regenerable JARVIS caches (__pycache__ / .pytest_cache). They rebuild on next launch.", "Low"):
            return
        def _fn(p):
            r, s = self._jopt_jopt_mod().jarvis_cache_clean(progress=p)
            return f"JARVIS caches cleared: {r} removed, {s} in use skipped"
        self._jopt_async("Clean caches", _fn, with_progress=True)

    def _jopt_gamedvr(self):
        self._jopt_snapshot_then("Game DVR off", "gamedvr", self._jopt_jopt_mod().disable_game_dvr)

    def _jopt_privacy(self):
        self._jopt_snapshot_then("Privacy extras", "privacy", self._jopt_jopt_mod().privacy_extras)

    def _jopt_widgets(self):
        self._jopt_snapshot_then("Hide widgets", "widgets", self._jopt_jopt_mod().disable_widgets)

    def _jopt_telemetry(self):
        if not self._jopt_confirm("Remove telemetry", "Changes HKLM diagnostic policy and disables telemetry services. Needs Administrator.", "Medium"):
            return
        self._jopt_snapshot_then("Telemetry", "telemetry", self._jopt_jopt_mod().remove_telemetry)

    def _jopt_temp_preview(self):
        count, total = self._jopt_jopt_mod().temp_cleanup_preview()
        return f"Temp: {count} entries, ~{self._jopt_jopt_mod()._fmt_bytes(total)} eligible."

    def _jopt_temp_clean(self):
        if not self._jopt_confirm("Temp cleanup", "Delete eligible files from Windows Temp folders. Locked files are skipped.", "Low"):
            return
        def _fn(p):
            d, s = self._jopt_jopt_mod().clear_temp_files(progress=p)
            return f"Temp cleaned: {d} removed, {s} skipped"
        self._jopt_async("Temp cleanup", _fn, with_progress=True)

    def _jopt_defrag(self):
        if not self._jopt_confirm("Drive optimize", "Runs Windows defrag /O on C:. Needs Administrator.", "Medium"):
            return
        self._jopt_async("Drive optimize", lambda p: self._jopt_jopt_mod().optimize_drive(progress=p), with_progress=True)

    def _jopt_dism(self):
        if not self._jopt_confirm("DISM repair", "Repairs the component store. Needs Administrator and can take many minutes.", "Medium"):
            return
        self._jopt_async("DISM", lambda p: self._jopt_jopt_mod().run_dism(progress=p), with_progress=True)

    def _jopt_sfc(self):
        if not self._jopt_confirm("SFC check", "Verifies protected system files. Needs Administrator.", "Medium"):
            return
        self._jopt_async("SFC", lambda p: self._jopt_jopt_mod().run_sfc(progress=p), with_progress=True)

    def _jopt_chkdsk(self):
        if not self._jopt_confirm("Disk scan", "Online C: file-system scan. Needs Administrator.", "Medium"):
            return
        self._jopt_async("Disk scan", lambda p: self._jopt_jopt_mod().run_chkdsk(progress=p), with_progress=True)

    def _jopt_winsock(self):
        if not self._jopt_confirm("Winsock reset", "Resets the Winsock catalog; may need reboot. Not auto-reversible.", "Medium"):
            return
        self._jopt_async("Winsock reset", self._jopt_jopt_mod().winsock_reset)

    def _jopt_ipreset(self):
        if not self._jopt_confirm("TCP/IP reset", "Resets TCP/IP to defaults; reboot recommended. Not auto-reversible.", "Medium"):
            return
        self._jopt_async("IP reset", self._jopt_jopt_mod().ip_reset)

    def _jopt_toggle_service(self, sid, label, risk):
        J = self._jopt_jopt_mod()
        cur = J.service_start_value(sid)
        target = "auto" if cur == 4 else "disabled"
        word = "enable (Automatic)" if cur == 4 else "disable"
        if not self._jopt_confirm(f"Service · {label}", f"{word.capitalize()} the '{label}' service. Needs Administrator.", risk):
            return
        def _fn():
            snap = J.snapshot_state(f"service_{sid}")
            if not J.is_admin():
                raise PermissionError("Changing service startup type needs Administrator rights.")
            rc, out = J.set_service_start(sid, target)
            if rc != 0:
                raise RuntimeError(out or "sc config failed")
            return f"{label} → {target}  (snapshot: {snap.name})"
        self._jopt_async(f"Service {label}", _fn)

    # ---- JOptimizer live refresh + backups UI ------------------------------
    def _jopt_on_tab(self, index):
        try:
            if index == getattr(self, "_jopt_tab_index", -1):
                self._jopt_timer.start()
                self._jopt_refresh_health()
            else:
                self._jopt_timer.stop()
        except Exception:
            pass

    def _jopt_refresh_health(self):
        try:
            J = self._jopt_jopt_mod()
            s = J.get_system_summary()
            self._jopt_cpu.setText(f"{s['cpu']:.0f}%")
            self._jopt_ram.setText(f"{s['ram']:.0f}%")
            self._jopt_disk.setText(f"{s['disk_free_gb']:.0f} GB")
            self._jopt_up.setText(s["uptime"])
            score = J.health_score(s)
            self._jopt_health.setText(f"{score}/100")
            self._jopt_gpu.setText(f"{len(J.top_processes(12))} hot")
            # Elevation mode badge.
            if J.is_admin():
                self._jopt_mode.setText("●  ADMINISTRATOR")
                self._jopt_mode.setStyleSheet(f"color:{GREEN};font:800 8pt 'Orbitron';background:transparent;")
            else:
                self._jopt_mode.setText("●  USER MODE — ADMIN ACTIONS LIMITED")
                self._jopt_mode.setStyleSheet(f"color:{AMBER};font:800 8pt 'Orbitron';background:transparent;")
            # Refresh service labels from the (fast) registry start type only.
            # service_state() spawns sc.exe, which must never run on the UI
            # thread every couple of seconds; the running/stopped state is
            # surfaced in the log after an explicit toggle instead.
            for sid, (item, lbl) in getattr(self, "_jopt_service_labels", {}).items():
                st = J.service_start_value(sid)
                name = {0: "Boot", 1: "System", 2: "Auto", 3: "Manual", 4: "Disabled"}.get(st, "?")
                lbl.setText(name)
        except Exception:
            pass

    def _jopt_refresh_backups(self):
        try:
            self._jopt_backup_list.clear()
            for path in self._jopt_jopt_mod().list_snapshots()[:40]:
                import datetime as _dt
                when = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                it = QListWidgetItem(f"{when}  ·  {path.name}")
                it.setData(Qt.ItemDataRole.UserRole, str(path))
                self._jopt_backup_list.addItem(it)
        except Exception:
            pass

    def _jopt_restore_selected(self):
        it = self._jopt_backup_list.currentItem()
        if it is None:
            self._jopt_log("Select a snapshot to restore first.")
            return
        from pathlib import Path as _P
        path = _P(it.data(Qt.ItemDataRole.UserRole))
        if not self._jopt_confirm("Restore snapshot", f"Restore registry + services from:\n{path.name}", "Medium"):
            return
        def _fn():
            errors = self._jopt_jopt_mod().restore_snapshot(path)
            return "Restored with warnings: " + " | ".join(errors[:8]) if errors else f"Restored {path.name}"
        self._jopt_async("Restore snapshot", _fn)

    def _jopt_open_folder(self):
        try:
            self._jopt_jopt_mod().open_backup_folder()
        except Exception as exc:
            self._jopt_log(f"✗ Open folder: {exc}")

    def _build_layout_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)

        intro = QLabel(
            "LAYOUT STUDIO · a live visual editor for the running J.A.R.V.I.S. widget tree. "
            "Choose a real panel, inspect its Explorer hierarchy, click widgets in the viewport, "
            "edit Roblox-Studio-style Properties, or work directly in QSS/HTML/CSS."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            f"background:{SURFACE_INSET};border:1px solid {BORDER_FAINT};"
            f"border-radius:10px;padding:12px;color:{TEXT_MID};"
        )
        lay.addWidget(intro)

        studio = HudGroupBox("LAYOUT STUDIO")
        sf = QVBoxLayout(studio)
        sf.setContentsMargins(12, 16, 12, 12)
        sf.setSpacing(8)

        title = QLabel("LIVE PANEL EDITOR")
        title.setStyleSheet(f'color:{ACCENT_BRIGHT};font:900 13pt "Orbitron";background:transparent;letter-spacing:1.8px;')
        sf.addWidget(title)

        detail = QLabel(
            "Explorer · live viewport · drag/resize · advanced Properties · undo/redo · "
            "QSS/HTML/CSS editor · custom elements · Uiverse import."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color:{TEXT_DIM};background:transparent;padding:2px 0 6px 0;")
        sf.addWidget(detail)

        open_editor = QPushButton("OPEN LAYOUT STUDIO")
        open_editor.setMinimumHeight(48)
        open_editor.setStyleSheet(_primary_button_css(48))
        open_editor.clicked.connect(self._open_external_layout_editor)
        sf.addWidget(open_editor)

        targets = QLabel(
            "Available live targets include Main Surface, Top Bar, Status Bar, Quick Panel, "
            "Chat Panel, Chat Log, Message Input, Settings Panel, Webview, Activity Log, "
            "World Monitor, Memory Core, Tools Deck, 3D Display, Video Preview and other runtime widgets."
        )
        targets.setWordWrap(True)
        targets.setStyleSheet(f"color:{TEXT_MID};background:transparent;padding:6px 2px 2px 2px;")
        sf.addWidget(targets)
        lay.addWidget(studio)

        note = QLabel(
            "The Studio edits the actual running widgets. SAVE writes layout overrides and custom elements to config/ui_layout.json. "
            "Uiverse elements can be opened in the embedded browser and imported through their HTML/CSS code."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{TEXT_FAINT};background:transparent;padding:5px 2px;")
        lay.addWidget(note)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "LAYOUT")

    def _build_web_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        intro = QLabel("WEB SURFACE  //  WEBVIEW + TASK QUEUE")
        intro.setStyleSheet(f'color:{ACCENT};font:800 9pt "Orbitron";padding:2px 2px 5px 2px;')
        lay.addWidget(intro)
        box = HudGroupBox("WEBVIEW")
        form = QFormLayout(box)
        self.web_homepage = QLineEdit(str(self._data.get("web_homepage", DEFAULTS["web_homepage"])))
        self.web_homepage.setPlaceholderText("https://")
        form.addRow("Homepage", self.web_homepage)
        self._web_ontop = QCheckBox("Keep HUD windows always on top")
        self._web_ontop.setChecked(bool(self._data.get("panel_always_on_top", True)))
        form.addRow("", self._web_ontop)
        self._web_engine = QCheckBox("Use the internal WebView engine for search and video")
        self._web_engine.setChecked(bool(self._data.get("features", {}).get("webview_engine", True)))
        form.addRow("", self._web_engine)
        self._web_music = QCheckBox("Play music inside the internal WebView")
        self._web_music.setChecked(bool(self._data.get("features", {}).get("webview_music", True)))
        form.addRow("", self._web_music)
        self._native_music = QCheckBox("Use the music app installed on this PC (Spotify, VLC) when one is available")
        self._native_music.setChecked(bool(self._data.get("features", {}).get("native_music_apps", True)))
        form.addRow("", self._native_music)
        lay.addWidget(box)
        tasks = HudGroupBox("WEB TASK DEFAULTS")
        tv = QVBoxLayout(tasks)
        _wfeat = self._data.get("features", {}) or {}
        self._web_js = QCheckBox("Allow JavaScript in Webview")
        self._web_js.setChecked(bool(_wfeat.get("webview_js", True)))
        self._web_js.setToolTip("Off = the embedded browser blocks page scripts.")
        self._web_external = QCheckBox("Open unknown schemes in the system browser")
        self._web_external.setChecked(bool(_wfeat.get("webview_external", True)))
        self._web_external.setToolTip(
            "Off = links the embedded browser cannot load are reported instead "
            "of being handed to your default browser."
        )
        tv.addWidget(self._web_js); tv.addWidget(self._web_external)
        hint = QLabel("Web Task queues search, news, research, price, and browse jobs. Browse opens a dedicated Webview window.")
        hint.setWordWrap(True); hint.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        tv.addWidget(hint)
        lay.addWidget(tasks)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "WEB")

    # ── MCP (universal server manager) ──────────────────────────────────────
    def _build_mcp_tab(self):
        """Settings → MCP: the universal MCP server control centre.

        The whole surface lives in ``ui_mcp_panel`` so this window only places
        one widget; the panel owns its own refresh timer and worker threads, so
        a server that takes seconds to start never blocks this window.
        """
        try:
            from ui_mcp_panel import McpTab
        except Exception as exc:
            page, lay, scroll = self._scroll_tab()
            lay.addWidget(QLabel(f"The MCP panel could not be loaded: {exc}"))
            self.tabs.addTab(scroll, "MCP")
            return
        self._mcp_panel = McpTab(self)
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._mcp_panel)
        self.tabs.addTab(scroll, "MCP")

    # ── OBSIDIAN VAULT (memory backend) ─────────────────────────────────────
    def _build_obsidian_tab(self):
        """Settings → Obsidian Vault: the vault's real state and memory switch."""
        try:
            from ui_vault_panel import ObsidianVaultTab
        except Exception as exc:
            page, lay, scroll = self._scroll_tab()
            lay.addWidget(QLabel(f"The Obsidian Vault panel could not be loaded: {exc}"))
            self.tabs.addTab(scroll, "OBSIDIAN VAULT")
            return
        self._vault_panel = ObsidianVaultTab(self)
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._vault_panel)
        self.tabs.addTab(scroll, "OBSIDIAN VAULT")

    def _build_performance_tab(self):
        """Settings → Performance: presets, real toggles and the scan (ui_perfomance)."""
        try:
            from ui_perfomance import PerformanceTab
        except Exception as exc:
            page, lay, scroll = self._scroll_tab()
            lay.addWidget(QLabel(f"The Performance panel could not be loaded: {exc}"))
            self.tabs.addTab(scroll, "PERFORMANCE")
            return
        self._perf_tab = PerformanceTab(window=self, parent=self)
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._perf_tab)
        self.tabs.addTab(scroll, "PERFORMANCE")

    def _build_memory_settings_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        intro = QLabel("MEMORY CORE  //  LONG-TERM STORE")
        intro.setStyleSheet(f'color:{ACCENT};font:800 9pt "Orbitron";padding:2px 2px 5px 2px;')
        lay.addWidget(intro)
        core = HudGroupBox("CORE STATUS")
        cv = QVBoxLayout(core)
        self._memory_core_state = QLabel("")
        self._memory_core_state.setWordWrap(True)
        self._memory_core_state.setStyleSheet(
            f"color:{TEXT_BRIGHT};background:transparent;font:700 8.5pt 'Exo 2';")
        cv.addWidget(self._memory_core_state)
        self._memory_core_growth = QLabel("")
        self._memory_core_growth.setWordWrap(True)
        self._memory_core_growth.setStyleSheet(
            f"color:{TEXT_MID};background:transparent;")
        cv.addWidget(self._memory_core_growth)
        row = QHBoxLayout()
        test_mem = QPushButton("TEST MEMORY")
        test_mem.setToolTip("Write a probe fact through the active store and "
                            "recall it — reports the store that really served it.")
        test_mem.clicked.connect(self._test_memory_core)
        row.addWidget(test_mem)
        row.addStretch(1)
        cv.addLayout(row)
        lay.addWidget(core)
        box = HudGroupBox("STORE")
        v = QVBoxLayout(box)
        self._memory_autosave = QCheckBox("Auto-save remembered facts to memory/long_term.json")
        self._memory_autosave.setChecked(bool(self._data.get("memory_autosave", True)))
        v.addWidget(self._memory_autosave)
        self._memory_index = QCheckBox("Keep a prompt index so JARVIS can recall facts that are not in the core prompt")
        self._memory_index.setChecked(bool(self._data.get("features", {}).get("memory_index", True)))
        self._memory_index.setToolTip(
            "When on, the [ALSO REMEMBERED] index is added to the prompt so "
            "JARVIS can search local memory for facts that do not fit inline."
        )
        v.addWidget(self._memory_index)
        info = QLabel("The Memory window lists every stored fact, lets you add keys, search, and forget entries. It is a real HUD window — not a popup on the tiny reactor.")
        info.setWordWrap(True); info.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        v.addWidget(info)
        lay.addWidget(box)
        mon = HudGroupBox("WORLD MONITOR")
        mv = QVBoxLayout(mon)
        self.world_refresh = self._slider_row(mv, "Headline refresh", int(self._data.get("world_monitor_refresh", 60)), 15, 600, " s")
        note = QLabel("World Monitor shows global clocks, CPU/RAM/GPU telemetry, a grid sweep, and live world headlines.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        mv.addWidget(note)
        lay.addWidget(mon)
        lay.addStretch(1)
        self._memory_tab_index = self.tabs.addTab(scroll, "MEMORY")
        self.tabs.currentChanged.connect(self._on_memory_tab_shown)
        self._refresh_memory_core()

    # ── memory core status (the MEMORY tab) ──────────────────────────────────
    def _refresh_memory_core(self):
        """Show the store that is REALLY serving memory, plus the growth."""
        try:
            from memory import backend as memory_backend, growth
            label = memory_backend.active_backend_label()
            selected = memory_backend.selected_backend()
            selected_label = ("Obsidian Vault" if selected == "obsidian"
                              else "Original Memory")
            stats = growth.snapshot()
            self._memory_core_state.setText(
                f"Active store: {label}   ·   selected: {selected_label}"
                + ("" if label == selected_label else "   ⚠ the selection is "
                   "not the store in use"))
            self._memory_core_growth.setText(
                f"Core level {stats['level']}  ·  strength "
                f"{int(round(float(stats['strength']) * 100))}%  ·  "
                f"{stats['writes']} fact(s) stored  ·  "
                f"{stats['reinforcements']} reinforced")
        except Exception as exc:
            try:
                self._memory_core_state.setText(f"Memory core unavailable: {exc}")
            except Exception:
                pass

    def _test_memory_core(self):
        """Write + recall through the active store, then report the truth."""
        try:
            from memory import backend as memory_backend
            result = memory_backend.test_roundtrip()
        except Exception as exc:
            result = {"ok": False, "detail": str(exc)}
        try:
            self._status.setText(
                ("MEMORY OK  ·  " if result.get("ok") else "MEMORY TEST FAILED  ·  ")
                + str(result.get("detail") or ""))
        except Exception:
            pass
        self._refresh_memory_core()

    def _on_memory_tab_shown(self, index: int) -> None:
        try:
            if index == getattr(self, "_memory_tab_index", -1):
                self._refresh_memory_core()
        except Exception:
            pass


    def _build_3d_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        status = QLabel("3D PREVIEW — load OBJ/STL/PLY and use the existing JARVIS 3D renderer.")
        status.setWordWrap(True); status.setStyleSheet(f"color:{TEXT_MID};background:transparent;")
        lay.addWidget(status)
        self._3d_host = QFrame(); self._3d_host.setStyleSheet("background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(0,18,28,235),stop:0.5 rgba(0,8,14,245),stop:1 rgba(0,27,38,220));border:none;border-radius:14px;")
        hv = QVBoxLayout(self._3d_host)
        self._3d_status = QLabel("No 3D model loaded")
        self._3d_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._3d_status.setMinimumHeight(300)
        hv.addWidget(self._3d_status, 1)
        lay.addWidget(self._3d_host, 1)
        row = QHBoxLayout()
        load_btn = QPushButton("OPEN 3D MODEL")
        load_btn.clicked.connect(self._open_3d_model)
        row.addWidget(load_btn)
        reset = QPushButton("RESET VIEW")
        reset.clicked.connect(self._reset_3d_view)
        row.addWidget(reset)
        wire = QPushButton("WIREFRAME")
        wire.clicked.connect(self._toggle_3d_wireframe)
        row.addWidget(wire)
        lay.addLayout(row)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "3D")

    def _scan_camera_indexes(self) -> list[int]:
        """Probe short-lived opens so the picker shows only live sources, and
        always expose 0/1 (built-in webcam / OBS virtual camera convention)."""
        idxs: list[int] = []
        try:
            from vision.camera import probe_camera
            for i in range(4):
                try:
                    if probe_camera(i):
                        idxs.append(i)
                except Exception:
                    pass
        except Exception:
            pass
        for fallback in (0, 1):
            if fallback not in idxs:
                idxs.append(fallback)
        return idxs

    def _build_video_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self._tab_intro(lay, "VIDEO  //  CAMERA SOURCE · LOCAL PLAYBACK")

        camera_box = HudGroupBox("CAMERA SOURCE")
        cam_form = QFormLayout(camera_box)
        cam_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.camera_source = QComboBox()
        self.camera_source.setMinimumWidth(250)
        current = str((self._data or {}).get("camera_index", "auto"))
        found = self._scan_camera_indexes()
        source_labels = [("auto", "AUTO DETECT")]
        for idx in found:
            tag = "OBS VIRTUAL CAMERA" if idx == 1 else "WEBCAM" if idx == 0 else "CAMERA"
            if idx != 0 and idx != 1:
                tag = f"DEVICE {idx}"
            source_labels.append((str(idx), f"{tag} (INDEX {idx})"))
        for value, label in source_labels:
            self.camera_source.addItem(label, value)
        set_i = self.camera_source.findData(current)
        if set_i >= 0:
            self.camera_source.setCurrentIndex(set_i)
        elif str(current).lstrip("-").isdigit():
            self.camera_source.addItem(f"CAMERA (INDEX {current})", str(current))
            self.camera_source.setCurrentIndex(self.camera_source.count() - 1)
        cam_form.addRow("Video source", self.camera_source)
        src_hint = QLabel("Choose the live camera for vision and the webcam button. "
                          "Index 0 is the built-in webcam; authors using OBS should "
                          "pick the OBS Virtual Camera entry. Applies after a re-open.")
        src_hint.setWordWrap(True)
        src_hint.setStyleSheet(f"color:{TEXT_MID};background:transparent;padding:2px;")
        cam_form.addRow(src_hint)
        lay.addWidget(camera_box)
        self._video_host = QFrame(); self._video_host.setStyleSheet(f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {BG_DEEP},stop:0.5 {BG},stop:1 {SURFACE_ALT});border:none;border-radius:14px;")
        vv = QVBoxLayout(self._video_host)
        self._video_widget = None
        self._video_status = QLabel("No video loaded")
        self._video_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_status.setMinimumHeight(300)
        vv.addWidget(self._video_status, 1)
        lay.addWidget(self._video_host, 1)
        row = QHBoxLayout()
        open_btn = QPushButton("OPEN VIDEO")
        open_btn.clicked.connect(self._open_video)
        row.addWidget(open_btn)
        self.video_play = QPushButton("PLAY / PAUSE")
        self.video_play.clicked.connect(self._toggle_video)
        row.addWidget(self.video_play)
        back = QPushButton("◀ 5s")
        back.clicked.connect(lambda: self._seek_video(-5000))
        row.addWidget(back)
        fwd = QPushButton("5s ▶")
        fwd.clicked.connect(lambda: self._seek_video(5000))
        row.addWidget(fwd)
        stop = QPushButton("STOP")
        stop.clicked.connect(self._stop_video)
        row.addWidget(stop)
        lay.addLayout(row)
        self.video_seek = QSlider(Qt.Orientation.Horizontal)
        self.video_seek.setRange(0,0); self.video_seek.setTracking(True)
        self.video_seek.sliderMoved.connect(lambda v: self._set_video_position(v))
        lay.addWidget(self.video_seek)
        self._video_time = QLabel("00:00 / 00:00")
        self._video_time.setStyleSheet(f"color:{TEXT_MID};")
        lay.addWidget(self._video_time)
        self._video_file = QLabel("File: —")
        self._video_file.setStyleSheet(f"color:{TEXT_MID};")
        lay.addWidget(self._video_file)
        self.tabs.addTab(scroll, "VIDEO")

    def _build_image_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self._image_preview = QLabel("No image loaded")
        self._image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_preview.setMinimumHeight(430)
        self._image_preview.setStyleSheet(f'background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(1,13,22,235),stop:1 rgba(2,24,34,210));border:none;border-radius:14px;color:{TEXT_FAINT};font:700 10pt "Exo 2";')
        lay.addWidget(self._image_preview, 1)
        row = QHBoxLayout()
        open_btn = QPushButton("OPEN IMAGE")
        open_btn.clicked.connect(self._open_image)
        row.addWidget(open_btn)
        clear = QPushButton("CLEAR")
        clear.clicked.connect(lambda: self._image_preview.clear())
        row.addWidget(clear)
        lay.addLayout(row)
        self.tabs.addTab(scroll, "IMAGE")

    def _build_system_tab(self):
        page, lay, scroll = self._scroll_tab()
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)
        self._tab_intro(lay, "SYSTEM  //  INTERFACE · WINDOWS · SOUND")
        box = HudGroupBox("FEATURES")
        v = QVBoxLayout(box)
        feats = self._data.get("features", {}) or {}
        self._feature_checks = {}
        for key, label in [
            ("audio_reactive", "Audio-reactive Arc Reactor"),
            ("smooth_animations", "Smooth animations"),
            ("chat_history", "Keep chat history"),
            ("compact_mode", "Start in compact mode"),
            ("drag_reactor", "Drag the Arc Reactor"),
            ("remember_position", "Remember window position"),
            ("advanced_layout", "Enable advanced layout editor"),
            ("preview_tabs", "Enable preview tabs"),
            ("webview", "Enable Webview windows"),
            ("web_task", "Enable Web Task queue"),
            ("world_monitor", "Enable World Monitor"),
            ("floating_windows", "Allow multiple independent HUD windows"),
            ("draggable_panels", "Drag windows from the title bar"),
            ("enable_sfx", "Futuristic UI sound effects"),
        ]:
            v.addWidget(self._flag_checkbox(self._feature_checks, key, label, True))
        lay.addWidget(box)

        # ── CHAT LOG ────────────────────────────────────────────────
        chat = HudGroupBox("CHAT LOG")
        cv = QVBoxLayout(chat)
        zr = QHBoxLayout()
        zlab = QLabel("Chat text size"); zlab.setMinimumWidth(185)
        self.chat_zoom = QSlider(Qt.Orientation.Horizontal)
        self.chat_zoom.setRange(80, 200)          # 80% .. 200%
        self.chat_zoom.setValue(int(float(self._data.get("chat_font_scale", 100))))
        zval = QLabel(f"{self.chat_zoom.value()}%"); zval.setMinimumWidth(44)
        self.chat_zoom.valueChanged.connect(lambda val: zval.setText(f"{val}%"))
        zr.addWidget(zlab); zr.addWidget(self.chat_zoom, 1); zr.addWidget(zval)
        cv.addLayout(zr)

        sep_row = QHBoxLayout()
        sep_lab = QLabel("Show message separator lines"); sep_lab.setMinimumWidth(185)
        self.chat_lines = QCheckBox("SHOW")
        self.chat_lines.setChecked(not bool(self._data.get("hide_chat_lines", True)))
        self.chat_lines.setToolTip("Show a subtle separator line between Chat messages. Off keeps the clean bubble layout.")
        sep_row.addWidget(sep_lab); sep_row.addWidget(self.chat_lines); sep_row.addStretch(1)
        cv.addLayout(sep_row)

        frow = QHBoxLayout()
        flab = QLabel("Show in chat"); flab.setMinimumWidth(185)
        self.chat_filter = QComboBox()
        self.chat_filter.addItem("Everything (system + conversation)", userData="all")
        self.chat_filter.addItem("Only JARVIS and me (hide SYS/VIS noise)", userData="chat")
        self.chat_filter.addItem("Only system events (SYS / VIS / ERR)", userData="system")
        _cf = str(self._data.get("chat_filter", "all") or "all")
        _ci = self.chat_filter.findData(_cf)
        self.chat_filter.setCurrentIndex(_ci if _ci >= 0 else 0)
        self.chat_filter.setToolTip("Quick preset for the chat log. Use the individual toggles below for precise SYS/VIS/JARVIS/You control.")
        frow.addWidget(flab); frow.addWidget(self.chat_filter, 1)
        cv.addLayout(frow)

        cat_row = QHBoxLayout()
        cat_label = QLabel("Show specific")
        cat_label.setMinimumWidth(185)
        cat_row.addWidget(cat_label)
        saved_cats = self._data.get("chat_filter_categories", {}) or {}
        self.chat_category_checks = {}
        for key, label in (("sys", "SYS"), ("vis", "VIS"), ("jarvis", "JARVIS"), ("you", "YOU"), ("file", "FILE"), ("err", "ERR"), ("warn", "WARN")):
            cb = QCheckBox(label)
            cb.setChecked(bool(saved_cats.get(key, True)))
            self.chat_category_checks[key] = cb
            cat_row.addWidget(cb)
        cat_row.addStretch(1)
        cv.addLayout(cat_row)
        lay.addWidget(chat)
        info = QLabel("All options are saved to config/api_keys.json. Preview tabs do not create or reveal the old large HUD.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{TEXT_MID};background:transparent;padding:5px;")
        lay.addWidget(info)
        lay.addStretch(1)
        self.tabs.addTab(scroll, "SYSTEM")

    def _slider_row(self, parent_layout, label, value, lo, hi, suffix):
        row = QHBoxLayout()
        lab = QLabel(label); lab.setMinimumWidth(185)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(max(lo, min(hi, int(value))))
        slider.setTracking(True)
        slider.setSingleStep(1)
        slider.setPageStep(max(1, (hi - lo) // 10))
        val = QLabel(f"{slider.value()}{suffix}")
        val.setMinimumWidth(78)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setStyleSheet(f'color:{ACCENT_TEXT};font:800 8pt "Orbitron";'
                          f'background:{SURFACE_INSET};border:none;'
                          f'border-radius:{RADIUS_SM}px;padding:4px 7px;')
        slider.valueChanged.connect(lambda v, out=val, s=suffix: out.setText(f"{int(v)}{s}"))
        row.addWidget(lab)
        row.addWidget(slider, 1)
        row.addWidget(val)
        parent_layout.addLayout(row)
        return slider

    # ── Delivery-panel style sliders (voice/delivery_style.py) ────────────
    def _style_slider_row(self, parent_layout, key: str, label: str, value: int,
                          tip: str = "") -> QSlider:
        """One style slider: existing HUD slider style, compact label, right
        readout with a low/mid/high glyph for instant glanceability.

        State is one-directional UI → config (collect at save), like every
        other slider on this page — changing one slider never writes another
        parameter (independent state).
        """
        row = QHBoxLayout()
        row.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(118)
        lab.setMaximumWidth(118)
        lab.setToolTip(tip)
        lab.setStyleSheet(f'color:{TEXT_MID};font:700 7pt "Exo 2";background:transparent;')
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(max(0, min(100, int(value))))
        slider.setTracking(True)
        slider.setSingleStep(1)
        slider.setPageStep(10)
        val = QLabel()
        val.setMinimumWidth(74)
        val.setMaximumWidth(74)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setStyleSheet(
            f'color:{ACCENT_TEXT};font:800 7pt "Orbitron";background:{SURFACE_INSET};'
            f'border:none;border-radius:{RADIUS_SM}px;padding:2px 5px;')

        def _paint_row(v: int) -> None:
            band = "LOW" if v <= 35 else ("MID" if v <= 64 else "HIGH")
            glyph = "▾" if band == "LOW" else ("◆" if band == "MID" else "▴")
            val.setText(f"{int(v)} {glyph}")

        slider.valueChanged.connect(_paint_row)
        _paint_row(slider.value())
        # Subtle, non-distracting hover affordance on the handle.
        slider.setStyleSheet(
            f'QSlider::groove:horizontal {{ border:none; height:3px;'
            f' background:{_rgba(ACCENT, 80)}; border-radius:1px; }}'
            f'QSlider::sub-page:horizontal {{ background:{_rgba(ACCENT, 190)}; border-radius:1px; }}'
            f'QSlider::handle:horizontal {{ width:10px; margin:-6px 0; border-radius:5px;'
            f'  background:{ACCENT_BRIGHT}; border:1px solid {_rgba(ACCENT_BRIGHT, 160)}; }}'
            f'QSlider::handle:horizontal:hover {{ background:{TEXT_BRIGHT};'
            f' border-color:{_rgba(ACCENT_BRIGHT, 220)}; }}'
            f'QSlider::handle:horizontal:pressed {{ background:{ACCENT_DEEP}; }}')
        row.addWidget(lab)
        row.addWidget(slider, 1)
        row.addWidget(val)
        parent_layout.addLayout(row)
        return slider

    def _build_style_section(self, parent_layout, sec_id: str, title: str) -> None:
        """One collapsible style section: a checkable group header that folds
        its body into itself (sizeConstraint-independent)."""
        from voice.delivery_style import VOICE_STYLE_PARAMS
        body = HudGroupBox(title)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(8, 2, 8, 6)
        bl.setSpacing(3)
        for key, meta in VOICE_STYLE_PARAMS.items():
            if meta[0] != sec_id:
                continue
            saved = int((self._data.get("voice_style") or {}).get(key, meta[2]))
            slider = self._style_slider_row(
                bl, key, meta[1], saved, tip=f"0-100 · default {meta[2]}")
            self._style_sliders[key] = slider
        # Collapsible: checkable title. Folded = header-only (children are
        # clipped, the layout respects the max height).
        body.setCheckable(True)
        body.setChecked(False)
        body.toggled.connect(lambda on, b=body: b.setMaximumHeight(
            16777215 if on else _style_section_folded_height(b)))
        body.setMaximumHeight(_style_section_folded_height(body))
        parent_layout.addWidget(body)

    def _build_style_sections(self, parent_layout) -> None:
        """All five collapsible style sections + the reset-to-defaults row."""
        from voice.delivery_style import SECTIONS
        self._style_sliders = {}
        for sec_id, title in SECTIONS:
            self._build_style_section(parent_layout, sec_id, title)
        row = QHBoxLayout()
        reset_btn = QPushButton("RESET STYLE TO DEFAULTS")
        reset_btn.setToolTip("Restore every voice-style slider to its default value (apply with SAVE CHANGES).")
        reset_btn.clicked.connect(self._reset_voice_style)
        row.addWidget(reset_btn)
        row.addStretch(1)
        parent_layout.addLayout(row)

    def _collect_voice_style(self) -> dict:
        """Slider values → complete style dict (missing keys get defaults)."""
        from voice.delivery_style import VOICE_STYLE_PARAMS
        out: dict[str, int] = {}
        for key, slider in (getattr(self, "_style_sliders", {}) or {}).items():
            try:
                out[key] = max(0, min(100, int(slider.value())))
            except Exception:
                pass
        for key, meta in VOICE_STYLE_PARAMS.items():
            out.setdefault(key, meta[2])
        return out

    def _reset_voice_style(self) -> None:
        """Reset every style slider to its default (unsaved until SAVE)."""
        from voice.delivery_style import VOICE_STYLE_PARAMS, defaults
        for key, slider in (getattr(self, "_style_sliders", {}) or {}).items():
            slider.blockSignals(True)
            slider.setValue(defaults().get(key, 50))
            slider.blockSignals(False)
            meta = VOICE_STYLE_PARAMS.get(key)
            if meta:
                slider.valueChanged.emit(slider.value())  # refresh the readout
        self._status.setText("VOICE STYLE RESET — SAVE CHANGES to apply")

    def _apply_status_style(self) -> None:
        """The header status pill, in the active theme's own colours."""
        label = getattr(self, "_status", None)
        if label is None:
            return
        label.setStyleSheet(
            f'color:{GREEN};font:800 8pt "Exo 2";background:{_rgba(GREEN, 40)};'
            f'padding:7px 12px;border:1px solid {_rgba(GREEN, 95)};'
            f'border-radius:{RADIUS_SM}px; letter-spacing:1px;')

    def _set_color(self, color: str):
        self._color = color
        self.color_btn.setText(color)

    def pick_color(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Choose JARVIS accent color")
        if c.isValid():
            self._set_color(c.name())

    def _host_window(self):
        """The running MainWindow.

        Settings is deliberately created as a top-level window (parent=None) so
        it can be moved independently, which means ``self.parent()`` is None.
        Anything that has to talk to the live UI must look the shell up instead
        of assuming it is this dialog's Qt parent.
        """
        parent = self.parent()
        if parent is not None:
            return parent
        try:
            from PyQt6.QtWidgets import QApplication
            for w in QApplication.topLevelWidgets():
                if hasattr(w, "_layout_elements") or hasattr(w, "_open_layout_editor"):
                    return w
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ layout
    def _load_layout_values(self):
        # The LAYOUT tab is now a launcher for the external Layout Studio and no
        # longer builds the inline element form.  These helpers are kept for the
        # older inline editor, so they must no-op when those widgets are absent
        # instead of aborting SettingsWindow.__init__.
        if getattr(self, "layout_element", None) is None:
            return
        self.layout_element.blockSignals(True)
        self.layout_element.clear()
        self._layout_rows = {}
        parent = self._host_window()
        items = []
        if parent is not None:
            for name, w in getattr(parent, "_system_layout_widgets", {}).items():
                if w is None:
                    continue
                g = w.geometry()
                items.append((name, {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(), "opacity": 100, "visible": w.isVisible()}))
            for name, w in getattr(parent, "_layout_widgets", {}).items():
                if w is None:
                    continue
                g = w.geometry()
                items.append((name, {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(), "opacity": 100, "visible": w.isVisible()}))
            for name, elem in getattr(parent, "_layout_elements", {}).items():
                self._layout_rows[name] = dict(elem)
                items.append((name, dict(elem)))
        seen = set()
        for name, elem in items:
            if name in seen:
                continue
            seen.add(name)
            self.layout_element.addItem(name)
            self._layout_rows[name] = dict(self._layout_rows.get(name, {}))
            self._layout_rows[name].update(elem)
        if self.layout_element.count():
            self.layout_element.setCurrentIndex(0)
        self.layout_element.blockSignals(False)
        self._on_layout_selection_changed(self.layout_element.currentText())

    def _on_layout_selection_changed(self, name):
        if getattr(self, "layout_x", None) is None:
            return
        if not name or name not in self._layout_rows:
            return
        self._layout_selected_name = name
        e = self._layout_rows[name]
        self.layout_x.setValue(int(e.get("x", 50)))
        self.layout_y.setValue(int(e.get("y", 50)))
        self.layout_w.setValue(int(e.get("w", 180)))
        self.layout_h.setValue(int(e.get("h", 50)))
        self.layout_opacity.setValue(int(e.get("opacity", 100)))
        self.layout_visible.setChecked(bool(e.get("visible", True)))

    def _apply_layout_selection(self):
        if getattr(self, "layout_x", None) is None:
            return
        name = self._layout_selected_name
        parent = self._host_window()
        if not name or parent is None:
            return
        elem = self._layout_rows.setdefault(name, {})
        elem.update({"name": name, "x": self.layout_x.value(), "y": self.layout_y.value(), "w": self.layout_w.value(), "h": self.layout_h.value(), "opacity": self.layout_opacity.value(), "visible": self.layout_visible.isChecked()})
        widget = getattr(parent, "_system_layout_widgets", {}).get(name) or getattr(parent, "_layout_widgets", {}).get(name)
        if widget is not None:
            widget.setGeometry(self.layout_x.value(), self.layout_y.value(), self.layout_w.value(), self.layout_h.value())
            widget.setVisible(self.layout_visible.isChecked())
        if hasattr(parent, "_layout_elements"):
            parent._layout_elements[name] = dict(elem)
            if hasattr(parent, "_apply_layout_elements"):
                parent._apply_layout_elements()
            if hasattr(parent, "_save_layout_elements"):
                parent._save_layout_elements()
        self._status.setText("LAYOUT APPLIED")

    def _add_layout_element(self):
        if getattr(self, "new_name", None) is None:
            return
        parent = self._host_window()
        if parent is None or not hasattr(parent, "_layout_elements"):
            return
        name = self.new_name.text().strip() or f"{self.new_kind.currentText()} {len(parent._layout_elements)+1}"
        text = self.new_text.text().strip() or name
        idx = 1
        base = name
        while name in parent._layout_elements:
            idx += 1
            name = f"{base} {idx}"
        parent._layout_elements[name] = {"name": name, "kind": self.new_kind.currentText(), "x": 70, "y": 70, "w": 240, "h": 50, "opacity": 100, "visible": True, "text": text, "color": self._color}
        if hasattr(parent, "_apply_layout_elements"):
            parent._apply_layout_elements()
        if hasattr(parent, "_save_layout_elements"):
            parent._save_layout_elements()
        self.new_name.clear(); self.new_text.clear()
        self._load_layout_values()
        self.layout_element.setCurrentText(name)
        self._status.setText("ELEMENT ADDED")

    def _feature_flag(self, key: str, default: bool = True) -> bool:
        """Current value of one feature switch (saved config first, widgets
        second) — so a guard reads what is actually stored, not what a button
        happens to show.
        """
        try:
            val = (self._data.get("features", {}) or {}).get(key)
            if isinstance(val, bool):
                return val
        except Exception:
            pass
        try:
            cb = (getattr(self, "_feature_checks", {}) or {}).get(key)
            if cb is not None:
                return bool(cb.isChecked())
            cb = (getattr(self, "_voice_checks", {}) or {}).get(key)
            if cb is not None:
                return bool(cb.isChecked())
        except Exception:
            pass
        return bool(default)

    def _open_external_layout_editor(self):
        if not self._feature_flag("advanced_layout", True):
            self._status.setText(
                "ADVANCED LAYOUT EDITOR IS DISABLED — SYSTEM → Enable advanced layout editor")
            return
        parent = self._host_window()
        try:
            if parent is not None and hasattr(parent, "_open_layout_editor"):
                parent._open_layout_editor()
                self._status.setText("LAYOUT STUDIO OPEN")
            else:
                self._status.setText("LAYOUT STUDIO NEEDS THE MAIN JARVIS WINDOW")
        except Exception as exc:
            self._status.setText(f"LAYOUT ERROR: {exc}")

    # ---------------------------------------------------------------- preview tabs
    def _open_3d_model(self):
        if not self._feature_flag("preview_tabs", True):
            self._status.setText(
                "PREVIEW TABS ARE OFF — SYSTEM → Enable preview tabs")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open 3D model", "", "3D Models (*.obj *.stl *.ply *.off *.glb *.gltf);;All Files (*.*)")
        if not path:
            return
        try:
            parent = self.parent()
            viewer_cls = None
            if parent is not None and parent.__class__.__module__:
                from ui import Model3DView as viewer_cls  # late import avoids circular import at module load
            if viewer_cls is None:
                raise RuntimeError("3D renderer unavailable")
            if getattr(self, "_model_view", None) is None:
                self._model_view = viewer_cls(self._3d_host)
                old = self._3d_status
                old.hide()
                self._3d_host.layout().addWidget(self._model_view, 1)
            self._model_view.load_model(path)
            self._3d_file = path
            self._3d_status.hide()
            self._status.setText("3D MODEL LOADED")
        except Exception as exc:
            self._3d_status.setText(f"3D preview error: {exc}")
            self._3d_status.show()

    def _reset_3d_view(self):
        viewer = getattr(self, "_model_view", None)
        if viewer is not None and hasattr(viewer, "reset_view"):
            viewer.reset_view()

    def _toggle_3d_wireframe(self):
        viewer = getattr(self, "_model_view", None)
        if viewer is not None and hasattr(viewer, "toggle_wireframe"):
            viewer.toggle_wireframe()

    def _open_video(self):
        if not self._feature_flag("preview_tabs", True):
            self._status.setText(
                "PREVIEW TABS ARE OFF — SYSTEM → Enable preview tabs")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open video", "", "Video Files (*.mp4 *.avi *.mkv *.mov *.webm *.wmv);;All Files (*.*)")
        if not path:
            return
        self._video_file.setText(f"File: {Path(path).name}")
        try:
            from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PyQt6.QtMultimediaWidgets import QVideoWidget
            if self._video_widget is None:
                self._video_widget = QVideoWidget(self._video_host)
                self._video_host.layout().insertWidget(0, self._video_widget, 1)
                self._video_player = QMediaPlayer(self)
                self._audio_output = QAudioOutput(self)
                self._video_player.setAudioOutput(self._audio_output)
                self._video_player.setVideoOutput(self._video_widget)
                self._video_player.positionChanged.connect(self._settings_video_position_changed)
                self._video_player.durationChanged.connect(lambda d: self.video_seek.setRange(0, int(d)))
            self._video_player.setSource(__import__('PyQt6.QtCore', fromlist=['QUrl']).QUrl.fromLocalFile(path))
            self._video_status.hide()
            self._video_player.play()
            self._status.setText("VIDEO PLAYING")
        except Exception as exc:
            self._video_status.setText(f"Video preview unavailable: {exc}")
            self._video_status.show()

    def _settings_video_position_changed(self, pos):
        if hasattr(self, "video_seek") and not self.video_seek.isSliderDown():
            self.video_seek.setValue(int(pos))
        if hasattr(self, "_video_time"):
            dur = self._video_player.duration() if getattr(self, "_video_player", None) is not None else 0
            self._video_time.setText(f"{self._fmt_ms(pos)} / {self._fmt_ms(dur)}")

    @staticmethod
    def _fmt_ms(ms):
        s=max(0,int(ms)//1000); m,sec=divmod(s,60); h,m=divmod(m,60)
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def _set_video_position(self, value):
        if getattr(self, "_video_player", None) is not None:
            self._video_player.setPosition(int(value))

    def _seek_video(self, delta):
        if getattr(self, "_video_player", None) is not None:
            self._video_player.setPosition(max(0, min(self._video_player.duration(), self._video_player.position()+int(delta))))

    def _toggle_video(self):
        p = getattr(self, "_video_player", None)
        if p is None:
            return
        from PyQt6.QtMultimedia import QMediaPlayer
        p.setPosition(p.position())
        p.play() if p.playbackState() != QMediaPlayer.PlaybackState.PlayingState else p.pause()

    def _stop_video(self):
        p = getattr(self, "_video_player", None)
        if p is not None:
            p.stop()

    def _open_image(self):
        if not self._feature_flag("preview_tabs", True):
            self._status.setText(
                "PREVIEW TABS ARE OFF — SYSTEM → Enable preview tabs")
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All Files (*.*)")
        if not path:
            return
        from PyQt6.QtGui import QPixmap
        px = QPixmap(path)
        if px.isNull():
            self._image_preview.setText("Unable to load image")
            return
        self._image_path = path
        self._image_preview.setPixmap(px.scaled(self._image_preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self._status.setText("IMAGE LOADED")

    def _on_onnx_voice_picked(self, index: int) -> None:
        """Selecting a discovered voice fills the model path automatically —
        and overwrites the typed override so the combo always wins visibly."""
        try:
            data = self.onnx_voice.itemData(index)
            if data:
                self.onnx_model_path.setText(str(data["model"]))
                self.onnx_status.setText(
                    f"Selected: {data['name']} — local, offline, no API key. "
                    "SAVE to apply; TEST SELECTED ENGINE to hear it."
                )
        except Exception:
            pass

    def _test_piper_voice(self) -> None:
        """TEST PIPER VOICE: load the picked Piper voice with the picked
        execution provider and speak one short line.

        Runs off the GUI thread and touches only the cached Piper path — it does
        not save settings and does not switch the active voice output mode. The
        provider ONNX Runtime actually used is reported, so a GPU request that
        fell back to CPU is visible instead of silent.
        """
        import threading
        _vdata = self.onnx_voice.currentData() or {}
        model = self.onnx_model_path.text().strip() or str(_vdata.get("model") or "")
        if not model:
            self.onnx_status.setText("Pick a Piper voice first (config/voices).")
            return
        config = str(_vdata.get("config") or DEFAULTS["onnx_voice_config"])
        provider = str(self.onnx_execution_provider.currentData() or "cpu")
        self.onnx_status.setText(f"Testing {Path(model).name} on {provider.upper()}…")

        def _run():
            try:
                from core.tts import create_tts_player, voice_status
                cfg = dict(self._data or {})
                cfg.update({
                    "tts_engine": "onnx",
                    "onnx_voice_model": model,
                    "onnx_voice_config": config,
                    "onnx_voice_speaker": self.onnx_speaker.text().strip() or "default",
                    "onnx_execution_provider": provider,
                    "voice_speed": float(self.voice_speed.value()) / 100.0,
                    "voice_volume": float(self.voice_volume.value()) / 100.0,
                })
                player = create_tts_player(cfg)
                player.warmup()
                used = voice_status().provider or "?"
                player.speak("Piper voice test. This is the JARVIS high voice model.")
                msg = f"PIPER OK · {Path(model).name} · {used}"
                QTimer.singleShot(0, self, lambda: self.onnx_status.setText(msg))
                QTimer.singleShot(0, self, lambda: self._status.setText(msg))
            except Exception as exc:
                msg = f"PIPER FAILED: {str(exc)[:120]}"
                QTimer.singleShot(0, self, lambda: self.onnx_status.setText(msg))
                QTimer.singleShot(0, self, lambda: self._status.setText(msg))
                self.write_log_safe(f"Piper voice test failed: {exc}")
        threading.Thread(target=_run, daemon=True).start()

    def _preview_sapi_voice(self) -> None:
        """Speak a preview through the selected Windows SAPI voice."""
        import threading
        vid = self.sapi_voice.currentData() or ""
        self._status.setText("PREVIEWING SAPI…")

        def _run():
            try:
                from core.tts import SapiTTSEngine
                SapiTTSEngine(voice=str(vid)).speak("This is a preview of the Windows voice.")
                QTimer.singleShot(0, self, lambda: self._status.setText("SAPI PREVIEW OK"))
            except Exception as exc:
                QTimer.singleShot(0, self, lambda: self._status.setText(f"SAPI PREVIEW FAILED: {str(exc)[:120]}"))
        threading.Thread(target=_run, daemon=True).start()

    # ---------------------------------------------------------------- settings
    def _collect_camera_index(self) -> object:
        value = None
        try:
            value = self.camera_source.currentData()
        except Exception:
            value = None
        if value is None:
            return getattr(self, "_data", {}).get("camera_index", "auto")
        return value

    def _collect(self):
        # Provider isolation: only the SELECTED provider's panel values are
        # harvested into the flat keys it owns; the other providers' legacy
        # keys are carried through from the loaded data untouched.
        try:
            _active_pid = self._selected_provider()
            _panel_values = self._provider_values(_active_pid)
        except Exception:
            _active_pid, _panel_values = "", {}
        _fish = _panel_values if _active_pid == "fishAudio" else None
        _eleven = _panel_values if _active_pid == "elevenLabs" else None
        _piper = _panel_values if _active_pid == "piperOnnx" else None
        _edge = _panel_values if _active_pid == "edgeTts" else None
        _sapi_vals = _panel_values if _active_pid == "sapi" else None
        # Derive voice_output_mode from provider + pipeline selection. The
        # Gemini pipeline modes (gemini / gemini_piper) are pipeline choices
        # that survive regardless of which provider panel is open — the Piper
        # provider IS the gemini_piper renderer — while any other provider
        # selected with a pipeline mode means local rendering.
        try:
            _vod = str(self.voice_output_mode.currentData() or "gemini")
        except Exception:
            _vod = "gemini"
        if _active_pid and _active_pid not in ("gemini", "piperOnnx") and _vod != "local":
            _vod = "local"
        features = {k: v.isChecked() for k, v in getattr(self, "_feature_checks", {}).items()}
        # Quick Setup owns six essential switches; the ones that have a checkbox
        # elsewhere were already kept in step on toggle, and this is the belt to
        # that pair of braces (the checkbox may not exist in a trimmed build).
        for key, value in (getattr(self, "_qs_toggles", {}) or {}).items():
            if key in features:
                features[key] = bool(value)
        features["drag_reactor"] = self._drag.isChecked()
        features["remember_position"] = self._remember.isChecked()
        features["compact_mode"] = self._compact.isChecked()
        features["autostart"] = self._autostart.isChecked()
        features["hide_taskbar_button"] = self._taskbar.isChecked()
        features["hide_taskbar"] = False  # legacy key: never hide the Windows taskbar itself
        features["webview_engine"] = self._web_engine.isChecked()
        features["webview_music"] = self._web_music.isChecked()
        features["native_music_apps"] = self._native_music.isChecked()
        features["webview_js"] = self._web_js.isChecked()
        features["webview_external"] = self._web_external.isChecked()
        # MEMORY → "Keep a prompt index" was not collected at all, so the
        # checkbox was a no-op that reset itself to on every save.
        try:
            features["memory_index"] = self._memory_index.isChecked()
        except Exception:
            pass
        features.update({k: v.isChecked() for k, v in self._voice_checks.items()})
        features.update({k: v.isChecked() for k, v in getattr(self, "_perm_checks", {}).items()})
        # The chat panel's Gemini ⇄ UI TARS switch writes features.ui_tars_mode
        # itself. This window owns no checkbox for it, and the features map is
        # rebuilt here, so carry the saved value across a SAVE CHANGES instead
        # of silently reverting the panel to Gemini mode.
        try:
            _saved = (self._data or {}).get("features") or {}
            if "ui_tars_mode" in _saved:
                features["ui_tars_mode"] = bool(_saved["ui_tars_mode"])
        except Exception:
            pass
        # Laya AI enable lives in the AI tab's "Laya AI" section; it also
        # applies live on toggle, but SAVE CHANGES must never erase it.
        try:
            features["laya_ai"] = self._laya_enable.isChecked()
        except Exception:
            pass
        chat_categories = {k: cb.isChecked() for k, cb in getattr(self, "chat_category_checks", {}).items()}
        return dict(
            ui_font=self.font.currentText(),
            ui_color=self._color,
            ui_opacity=self.ui_opacity.value(),
            compact_size=self.reactor_size.value(),
            reactor_opacity=self.reactor_opacity.value(),
            reactor_stroke_opacity=self.reactor_stroke_opacity.value(),
            reactor_animation_speed=self.anim_speed.value() / 100.0,
            equalizer_sensitivity=self.eq_sens.value() / 100.0,
            voice_speed=self.voice_speed.value() / 100.0,
            voice_pitch=self.voice_pitch.value() / 10.0,
            voice_volume=self.voice_volume.value() / 100.0,
            # Extended voice style: collected from the sliders and carried
            # through the same save path; delivery_style.py validates on read.
            voice_style=self._collect_voice_style(),
            voice_mode=self.voice_mode.currentText(),
            tts_engine={"fishAudio": "fish_audio", "elevenLabs": "elevenlabs", "gemini": "edgetts",
                        "piperOnnx": "onnx", "edgeTts": "edgetts", "kokoro": "kokoro",
                        "sapi": "sapi"}.get(_active_pid,
                        str(self._data.get("tts_engine", "edgetts"))),
            tts_voice=(_edge or {}).get("voice", self._data.get("tts_voice", DEFAULTS["tts_voice"])),
            onnx_voice_model=(_piper or {}).get("modelPath", self._data.get("onnx_voice_model", "")),
            onnx_voice_config=(_piper or {}).get("configPath",
                                self._data.get("onnx_voice_config", DEFAULTS["onnx_voice_config"])),
            onnx_voice_speaker=(_piper or {}).get("speaker", self._data.get("onnx_voice_speaker", "default")),
            onnx_execution_provider=(_piper or {}).get("executionProvider",
                                      self._data.get("onnx_execution_provider", "cpu")),
            # A deliberate save records that the one-time gemini -> gemini_piper
            # lift has happened, so an explicit Gemini Native Audio choice made
            # from here on is never silently migrated again.
            voice_pipeline_migrated=True,
            # voice_name belongs to the GEMINI provider only — a save for any
            # other provider must not rewrite it (the Fish→Charon bug).
            voice_name=(self._data.get("voice_name", DEFAULTS["voice_name"])
                        if _active_pid != "gemini" else self.live_voice.currentText()),
            fish_audio_api_key=(_fish or {}).get("apiKey", self._data.get("fish_audio_api_key", "")),
            fish_audio_model_id=(_fish or {}).get("model", self._data.get("fish_audio_model_id", DEFAULTS["fish_audio_model_id"])),
            fish_audio_voice_id=(_fish or {}).get("voice", self._data.get("fish_audio_voice_id", "")),
            fish_audio_endpoint=(_fish or {}).get("endpoint", self._data.get("fish_audio_endpoint", DEFAULTS["fish_audio_endpoint"])),
            fish_audio_format=(_fish or {}).get("format", self._data.get("fish_audio_format", "mp3")),
            fish_audio_latency=(_fish or {}).get("latency", self._data.get("fish_audio_latency", "normal")),
            elevenlabs_api_key=(_eleven or {}).get("apiKey", self._data.get("elevenlabs_api_key", "")),
            elevenlabs_voice_id=(_eleven or {}).get("voice", self._data.get("elevenlabs_voice_id", DEFAULTS["elevenlabs_voice_id"])),
            elevenlabs_model_id=(_eleven or {}).get("model", self._data.get("elevenlabs_model_id", DEFAULTS["elevenlabs_model_id"])),
            sapi_voice=(_sapi_vals or {}).get("voice", self._data.get("sapi_voice", "")),
            voice_output_mode=_vod,
            assistant_name=self.assistant_name.text().strip() or "JARVIS",
            user_name=self.user_name.text().strip(),
            wake_words=[w.strip().lower() for w in self.wake_words.text().replace("\n", ",").split(",") if w.strip()],
            chat_font_scale=int(self.chat_zoom.value()),
            chat_filter=str(self.chat_filter.currentData() or "all"),
            chat_filter_categories=chat_categories,
            hide_chat_lines=not bool(getattr(self, "chat_lines", None) and self.chat_lines.isChecked()),
            camera_index=self._collect_camera_index(),
            ai_provider=str(self.ai_provider.currentData() or "gemini"),
            ai_model=self.ai_model.currentText().strip(),
            model_roles={
                role: edit.text().strip()
                for role, edit in getattr(self, "_role_edits", {}).items()
                if edit.text().strip()
            },
            # Provider keys (openai/anthropic/groq/custom_ai_api_key and the
            # rest) are collected on the API tab and merged over self._data
            # in save_settings — one editor per key, on the API tab.
            custom_provider_name=self.custom_provider_name.text().strip(),
            custom_ai_base_url=self.custom_ai_base_url.text().strip().rstrip("/"),
            custom_ai_model=self.custom_ai_model.text().strip(),
            web_homepage=self.web_homepage.text().strip() or DEFAULTS["web_homepage"],
            world_monitor_refresh=self.world_refresh.value(),
            memory_autosave=self._memory_autosave.isChecked(),
            panel_always_on_top=self._web_ontop.isChecked(),
            features=features,
        )

    def reset_defaults(self):
        try:
            self._memory_index.setChecked(True)
        except Exception:
            pass
        try:
            self._web_js.setChecked(True)
            self._web_external.setChecked(True)
        except Exception:
            pass
        self.font.setCurrentText(DEFAULTS["ui_font"])
        self._set_color(DEFAULTS["ui_color"])
        self.ui_opacity.setValue(DEFAULTS["ui_opacity"])
        self.reactor_size.setValue(DEFAULTS["compact_size"])
        self.reactor_opacity.setValue(DEFAULTS["reactor_opacity"])
        self.reactor_stroke_opacity.setValue(DEFAULTS["reactor_stroke_opacity"])
        self.anim_speed.setValue(100)
        self.eq_sens.setValue(100)
        self.voice_speed.setValue(100)
        self.voice_pitch.setValue(0)
        self.voice_volume.setValue(100)
        try:
            self._reset_voice_style()
        except Exception:
            pass
        self.voice_mode.setCurrentText(DEFAULTS["voice_mode"])
        self.provider_selector.setCurrentIndex(self.provider_selector.findData("edgeTts"))
        self.tts_voice.setText(DEFAULTS["tts_voice"])
        self.live_voice.setCurrentText(DEFAULTS["voice_name"])
        self.fish_api_key.clear()
        self.fish_model_id.setText(DEFAULTS["fish_audio_model_id"])
        self.fish_voice_id.clear()
        self.fish_endpoint.setText(DEFAULTS["fish_audio_endpoint"])
        self.fish_format.setCurrentText(DEFAULTS["fish_audio_format"])
        self.fish_latency.setCurrentText(DEFAULTS["fish_audio_latency"])
        self.elevenlabs_api_key.clear()
        self.elevenlabs_voice_id.setText(DEFAULTS["elevenlabs_voice_id"])
        self.elevenlabs_model_id.setCurrentText(DEFAULTS["elevenlabs_model_id"])
        self.assistant_name.setText("JARVIS")
        try:
            self.wake_words.clear()
        except Exception:
            pass
        self.user_name.clear()
        self.ai_provider.setCurrentIndex(max(0, self.ai_provider.findData("gemini")))
        self.ai_model.setCurrentText("")
        self.custom_provider_name.clear()
        self.custom_ai_base_url.clear()
        self.custom_ai_model.clear()
        # API keys are reset on the API tab (if it is built).
        for _attr in ("openai_api_key", "anthropic_api_key", "groq_api_key",
                      "custom_ai_api_key", "gemini_key_edit", "fish_audio_key_edit",
                      "elevenlabs_key_edit", "mem0_key_edit", "qdrant_key_edit"):
            _fld = getattr(self, _attr, None)
            if _fld is not None:
                _fld.clear()
        if hasattr(self, "web_homepage"):
            self.web_homepage.setText(DEFAULTS["web_homepage"])
        if hasattr(self, "world_refresh"):
            self.world_refresh.setValue(DEFAULTS["world_monitor_refresh"])
        if hasattr(self, "_memory_autosave"):
            self._memory_autosave.setChecked(True)
        if hasattr(self, "_web_ontop"):
            self._web_ontop.setChecked(True)
        for _key, _cb in getattr(self, "chat_category_checks", {}).items():
            _cb.setChecked(True)
        if hasattr(self, "_web_engine"):
            self._web_engine.setChecked(True)
        if hasattr(self, "_web_music"):
            self._web_music.setChecked(True)
        self._compact.setChecked(True)
        self._drag.setChecked(True)
        self._remember.setChecked(True)
        self._autostart.setChecked(False)
        self._taskbar.setChecked(False)
        for key, cb in self._feature_checks.items():
            cb.setChecked(bool(DEFAULTS["features"].get(key, True)))
        try:
            self._laya_enable.setChecked(bool(DEFAULTS["features"].get("laya_ai", True)))
            self._laya_default.setChecked(False)
        except Exception:
            pass
        for key, cb in self._voice_checks.items():
            cb.setChecked(key not in {"push_to_talk", "always_on"})
        for key, cb in getattr(self, "_perm_checks", {}).items():
            cb.setChecked(bool(DEFAULTS["features"].get(key, True)))
        self._status.setText("DEFAULTS READY — SAVE TO APPLY")

    def save_settings(self):
        try:
            new_data = self._collect()
            # Voice settings only take effect on engines built AFTER the save,
            # so drop the cached engine when a voice key actually changed —
            # otherwise SAVE looked like a no-op for the TEST button.
            _VOICE_KEYS = (
                "tts_engine", "tts_voice", "voice_name", "voice_output_mode",
                "onnx_voice_model", "onnx_voice_speaker",
                "elevenlabs_api_key", "elevenlabs_voice_id", "elevenlabs_model_id",
                "fish_audio_api_key", "fish_audio_model_id", "fish_audio_voice_id",
                "voice_speed", "voice_pitch", "voice_volume",
            )
            voice_changed = any(
                str(new_data.get(k)) != str((self._data or {}).get(k))
                for k in _VOICE_KEYS
            )
            # Guarded write (spec §39): backup, validate, write atomically,
            # re-read and verify, and roll the file back if the save came out
            # unreadable. A bad save used to leave the config broken until the
            # user noticed.
            warnings = ""
            try:
                from core import config_guard as _guard
                # Start from the file ON DISK, not this window's opening
                # snapshot. Panels written since (the memory-backend switch, the
                # vault path) must not be silently reverted by this save.
                _merged = dict(_guard.read(self._config_path) or self._data or {})
                # The API tab's key edits merge over everything (they are the
                # single editor for those keys).
                try:
                    _api = self._collect_api_keys()
                    _api_feats = _api.pop("features", {})
                    _merged_feats = dict(_merged.get("features") or {})
                    _merged_feats.update(_api_feats)
                    _merged["features"] = _merged_feats
                    _merged.update(_api)
                except Exception:
                    pass
                _merged.update(new_data)
                # A retired key still sitting in the file is dropped here as well
                # as on read: load() pops it, but this save starts from the raw
                # file on disk, so without this the stale value would be written
                # straight back and outlive the feature it belonged to.
                _merged.pop("ui_theme", None)
                _ok, _msg, _bak = _guard.save_with_history(
                    _merged, tag="settings", path=self._config_path)
                if not _ok:
                    raise RuntimeError(_msg)
                if _msg.startswith("saved with warnings"):
                    warnings = " · " + _msg.split(":", 1)[-1].strip()
                self._data = _guard.read(self._config_path)
            except Exception as _guard_exc:
                # Guard unavailable or failed: fall back to the plain save so a
                # broken guard can never block the user's settings.
                self._data = save(self._config_path, **new_data)
                warnings = f" · guard: {str(_guard_exc)[:80]}"
            # A credential value that is plainly not a key was refused during
            # _collect_api_keys(); the previous stored value is intact.
            warnings += self._report_credential_rejects()
            if voice_changed:
                try:
                    from core.tts import clear_tts_cache
                    clear_tts_cache()
                except Exception:
                    pass
            # Converge the Laya runtime with the freshly saved configuration
            # (enable loads in the background; disable drops the model).
            try:
                from core import laya_decision
                laya_decision.initialize(background=True)
                self._refresh_laya_status()
            except Exception:
                pass
            self.setWindowTitle("J.A.R.V.I.S Settings — SAVED")
            self._clear_dirty()
            try:
                self._refresh_key_states()
            except Exception:
                pass
            self._status.setText("SAVED"
                                 + (" · voice engine will rebuild" if voice_changed else "")
                                 + warnings)
            self.settings_saved.emit(dict(self._data))
            parent = self.parent()
            if parent is not None and hasattr(parent, "_apply_full_settings"):
                parent._apply_full_settings(dict(self._data))
                if hasattr(parent, '_check_autostart') and hasattr(parent, '_toggle_autostart'):
                    desired = bool(self._autostart.isChecked())
                    if parent._check_autostart() != desired:
                        parent._toggle_autostart()
                    # TASK 8: trust the OS, not the flag. If the real Windows
                    # startup registration still disagrees with what the user
                    # asked for, snap both the checkbox and the persisted config
                    # back to reality so the UI never claims ON while OFF.
                    try:
                        actual = bool(parent._check_autostart())
                        if actual != desired:
                            self._autostart.setChecked(actual)
                            _fix = dict(self._data or {})
                            _fix_feats = dict(_fix.get('features') or {})
                            _fix_feats['autostart'] = actual
                            _fix['features'] = _fix_feats
                            from core import config_guard as _g2
                            _g2.save_with_history(_fix, tag="autostart-rollback")
                            self._data = _fix
                    except Exception:
                        pass
            return self._data
        except Exception as exc:
            self._status.setText(f"SAVE ERROR: {exc}")
            return None

    def _autosave_quiet(self):
        """Auto-save after a slider release (resize/opacity tweaks apply live)."""
        try:
            data = self.save_settings()
            if data is not None:
                self._status.setText("AUTO-SAVED")
        except Exception:
            pass

    def _header_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _header_move(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _header_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        # The window is hidden, not destroyed, on close — so the Laya status
        # poll only runs while the control center is actually on screen.
        try:
            self._laya_timer.start()
            self._refresh_laya_status()
        except Exception:
            pass

    def hideEvent(self, event):
        try:
            self._laya_timer.stop()
        except Exception:
            pass
        super().hideEvent(event)

    def closeEvent(self, event):
        try:
            self._stop_video()
        except Exception:
            pass
        self.hide()
        event.ignore()


def launch_settings(parent=None):
    created = QApplication.instance() is None
    app = QApplication.instance() or QApplication(sys.argv)
    win = SettingsWindow(parent=parent)
    win.show(); win.raise_(); win.activateWindow()
    return app.exec() if created else 0


if __name__ == "__main__":
    raise SystemExit(launch_settings())