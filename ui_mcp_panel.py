"""Settings → MCP: the universal MCP server control centre.

This module owns the whole MCP surface — the dashboard, the Add/Edit screen, the
Manage view and the Run Tool tester — so the (already very large) settings window
only has to place one widget.

The design rules it holds to:

* **One server, one card.** Real icon, name, composite state, counts, transport
  and the two actions a user needs (Manage, Enable/Disable). Nothing about a
  server is hidden behind a second window.
* **Honest state.** A card shows *Connected*, *Connected — auth needs
  attention*, *0 tools*, *Error*, *Reconnecting…*, exactly as the manager
  reports it. Colour follows meaning: green is usable, amber is working or
  degraded, red is stopped.
* **No blocking.** Connecting, testing, refreshing and running a tool all happen
  on worker threads; the panel reads results on a timer, so the window never
  freezes while a server starts.
* **Credentials stay masked.** The editor shows a credential as set or not set;
  the generated client config substitutes a placeholder rather than the value.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QTimer, QSize, QPointF, QRectF
from PyQt6.QtGui import (QColor, QFont, QPainter, QPainterPath, QBrush, QPen, QPixmap,
                        QLinearGradient, QRadialGradient)
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget, QFileDialog,
)

import ui_theme as _t
from ui_theme import (
    ACCENT, ACCENT_TEXT, AMBER, GREEN, RED, TEXT, TEXT_BRIGHT, TEXT_DIM,
    TEXT_FAINT, TEXT_MID, SURFACE, SURFACE_ALT, BORDER, BORDER_SOFT,
    STROKE_MID, STROKE_HAIR, RADIUS_LG, RADIUS_MD, RADIUS_SM,
    FONT_UI, FONT_HEAD, FONT_SMALL, SPACE_SM,
    qcolor as _qcolor, rgba as _rgba, button_css as _button_css,
    primary_button_css as _primary_button_css, field_css as _field_css,
    scrollbar_css as _scrollbar_css, state_pill_css as _state_pill_css,
)

from mcp_host import (catalog, client_configs, config_gen, icons,
                      secrets as secret_store)

__all__ = ["McpTab", "McpEditorDialog", "McpManageDialog"]

_STATE_COLORS = {
    "connected": GREEN,
    "connecting": AMBER,
    "initializing": AMBER,
    "discovering": AMBER,
    "reconnecting": AMBER,
    "stopping": AMBER,
    "error": RED,
    "disabled": TEXT_DIM,
    "stopped": TEXT_DIM,
    "disconnected": TEXT_DIM,
}


def _manager():
    from mcp_host.manager import get_manager
    return get_manager()


def _card_path(rect, cut: int = 12) -> QPainterPath:
    path = QPainterPath()
    cut = max(6, min(cut, min(rect.width(), rect.height()) / 5))
    path.moveTo(rect.left() + cut, rect.top())
    path.lineTo(rect.right() - cut, rect.top())
    path.lineTo(rect.right(), rect.top() + cut)
    path.lineTo(rect.right(), rect.bottom() - cut)
    path.lineTo(rect.right() - cut, rect.bottom())
    path.lineTo(rect.left() + cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom() - cut)
    path.lineTo(rect.left(), rect.top() + cut)
    path.closeSubpath()
    return path


# ── the server card ───────────────────────────────────────────────────────────

class McpServerCard(QFrame):
    """One server: icon, identity, composite state, counts and direct controls."""

    def __init__(self, row: dict, panel: "McpTab", parent=None) -> None:
        super().__init__(parent)
        self.row = dict(row)
        self._panel = panel
        self._hover = False
        self._testing = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumHeight(104)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(14)

        # ── the icon frame with HUD styling ──────────────────────────────────
        self._icon_box = QFrame()
        self._icon_box.setFixedSize(46, 46)
        self._icon_box.setStyleSheet(
            f"QFrame {{ background: {_rgba(ACCENT, 22)}; "
            f"border: 1px solid {_rgba(ACCENT, 80)}; "
            f"border-radius: {RADIUS_SM}px; }}"
        )
        ib_lay = QVBoxLayout(self._icon_box)
        ib_lay.setContentsMargins(0, 0, 0, 0)
        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ib_lay.addWidget(self._icon)
        outer.addWidget(self._icon_box, 0, Qt.AlignmentFlag.AlignTop)
        self._apply_icon()

        # ── identity + state + metadata ──────────────────────────────────────
        info = QVBoxLayout()
        info.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        name = QLabel(str(row.get("name") or "MCP server"))
        name.setStyleSheet(
            f"color:{TEXT_BRIGHT};font:700 11.5pt '{FONT_HEAD.strip(chr(39))}';"
            "background:transparent;letter-spacing:1px;"
        )
        top_row.addWidget(name)

        ns = QLabel(f"[{row.get('namespace', '')}::]")
        ns.setStyleSheet(
            f"color:{ACCENT_TEXT};font:600 7.5pt '{FONT_SMALL}';"
            f"background:{_rgba(ACCENT, 20)};border:1px solid {_rgba(ACCENT, 50)};"
            f"border-radius:3px;padding:1px 5px;"
        )
        top_row.addWidget(ns)

        is_http = row.get("transport") == "http"
        trans_text = "HTTP [Remote]" if is_http else "STDIO [Local]"
        trans_color = AMBER if is_http else GREEN
        transport_tag = QLabel(trans_text)
        transport_tag.setStyleSheet(
            f"color:{trans_color};font:700 7pt '{FONT_SMALL}';"
            f"background:{_rgba(trans_color, 20)};border:1px solid {_rgba(trans_color, 60)};"
            f"border-radius:3px;padding:1px 5px;"
        )
        top_row.addWidget(transport_tag)
        top_row.addStretch(1)
        info.addLayout(top_row)

        state_row = QHBoxLayout()
        state_row.setSpacing(10)
        self._state = QLabel(self._state_text())
        self._state.setStyleSheet(
            f"color:{self._state_color()};font:600 8.5pt '{FONT_UI}';"
            "background:transparent;"
        )
        state_row.addWidget(self._state)

        self._test_label = QLabel("")
        self._test_label.setStyleSheet(
            f"color:{TEXT_BRIGHT};font:600 8pt '{FONT_SMALL}';"
            f"background:{_rgba(SURFACE_ALT, 220)};border:1px solid {_rgba(BORDER, 100)};"
            f"border-radius:3px;padding:1px 6px;"
        )
        self._test_label.hide()
        state_row.addWidget(self._test_label)
        state_row.addStretch(1)
        info.addLayout(state_row)

        meta = QLabel(self._meta_text())
        meta.setWordWrap(True)
        meta.setStyleSheet(
            f"color:{TEXT_DIM};font:500 7.5pt '{FONT_UI}';background:transparent;"
        )
        info.addWidget(meta)

        if self.row.get("detail"):
            detail = QLabel(str(self.row["detail"])[:240])
            detail.setWordWrap(True)
            detail.setStyleSheet(
                f"color:{TEXT_FAINT};font:500 7.5pt '{FONT_UI}';background:transparent;"
            )
            info.addWidget(detail)
        outer.addLayout(info, 1)

        # ── capability summary ───────────────────────────────────────────────
        caps_box = QVBoxLayout()
        caps_box.setSpacing(2)
        caps = QLabel(self._capabilities_text())
        caps.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        caps.setStyleSheet(
            f"color:{TEXT_MID};font:600 7.5pt '{FONT_SMALL}';background:transparent;"
        )
        caps_box.addWidget(caps)
        outer.addLayout(caps_box, 0)

        # ── direct in-card action buttons ────────────────────────────────────
        actions = QVBoxLayout()
        actions.setSpacing(4)

        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(4)
        is_conn = bool(row.get("connected") and row.get("state") == "connected")
        self._restart_btn = QPushButton("Restart" if is_conn else "Start")
        self._restart_btn.setToolTip("Start or restart this server connection")
        self._restart_btn.clicked.connect(self._on_restart_clicked)
        btn_row1.addWidget(self._restart_btn)

        self._test_btn = QPushButton("Test")
        self._test_btn.setToolTip("Test connection and discover tools in real-time")
        self._test_btn.clicked.connect(self._on_test_clicked)
        btn_row1.addWidget(self._test_btn)
        actions.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(4)
        manage = QPushButton("Manage")
        manage.setToolTip("Inspect server tools, tester, logs, and permissions")
        manage.clicked.connect(lambda: self._panel.open_manage(self.row["name"]))
        btn_row2.addWidget(manage)

        edit = QPushButton("Edit")
        edit.setToolTip("Edit server configuration")
        edit.clicked.connect(lambda: self._panel.open_editor(self.row["name"]))
        btn_row2.addWidget(edit)
        actions.addLayout(btn_row2)

        toggle = QPushButton("Disable" if self.row.get("enabled") else "Enable")
        toggle_color = RED if self.row.get("enabled") else GREEN
        toggle.setStyleSheet(
            f"QPushButton {{ background: {_rgba(toggle_color, 24)}; "
            f"border: 1px solid {_rgba(toggle_color, 80)}; "
            f"color: {TEXT_BRIGHT}; font: 600 8pt '{FONT_UI}'; padding: 3px; border-radius: {RADIUS_SM}px; }}"
            f"QPushButton:hover {{ background: {_rgba(toggle_color, 45)}; }}"
        )
        toggle.clicked.connect(
            lambda: self._panel.toggle_server(self.row["name"],
                                              not self.row.get("enabled", True))
        )
        actions.addWidget(toggle)
        outer.addLayout(actions, 0)

    # ── action handlers ───────────────────────────────────────────────────────

    def _on_restart_clicked(self) -> None:
        self._panel.restart_server(self.row["name"])

    def _on_test_clicked(self) -> None:
        self._test_label.show()
        self._test_label.setText("Testing…")
        self._test_label.setStyleSheet(
            f"color:{AMBER};font:600 8pt '{FONT_SMALL}';"
            f"background:{_rgba(AMBER, 20)};border:1px solid {_rgba(AMBER, 60)};"
            f"border-radius:3px;padding:1px 6px;"
        )

        def _cb(res: dict):
            if res.get("ok"):
                txt = f"✓ {res.get('tools', 0)} tool(s) ready"
                color = GREEN
            else:
                txt = f"✕ {str(res.get('detail') or 'Failed')[:32]}"
                color = RED

            def _ui():
                self._test_label.setText(txt)
                self._test_label.setStyleSheet(
                    f"color:{color};font:600 8pt '{FONT_SMALL}';"
                    f"background:{_rgba(color, 25)};border:1px solid {_rgba(color, 75)};"
                    f"border-radius:3px;padding:1px 6px;"
                )
            QTimer.singleShot(0, _ui)

        self._panel.test_server(self.row["name"], _cb)

    # ── rendering ─────────────────────────────────────────────────────────────

    def _apply_icon(self) -> None:
        colour = ACCENT if self.row.get("enabled") else TEXT_DIM
        pixmap = icons.pixmap(icons.icon_for_spec(_spec_from_row(self.row)), colour)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            self._icon.setPixmap(pixmap)
        else:
            self._icon.setText("◆")
            self._icon.setStyleSheet(f"color:{ACCENT_TEXT};font:700 16pt;"
                                     "background:transparent;")

    def _state_text(self) -> str:
        row = self.row
        if not row.get("enabled"):
            return "○  Disabled"
        state = str(row.get("state") or "")
        if state == "connected":
            if not row.get("authenticated", True):
                return "●  Connected  ⚠  auth needs attention"
            if not row.get("tools_ready"):
                return "●  Connected  ·  0 tools"
            return "●  Connected"
        return f"{'◐' if state in ('connecting', 'initializing', 'discovering', 'reconnecting', 'stopping') else '⚠'}  {row.get('state_label') or state}"

    def _state_color(self) -> str:
        if not self.row.get("enabled"):
            return TEXT_DIM
        return _STATE_COLORS.get(str(self.row.get("state") or ""), TEXT_MID)

    def _meta_text(self) -> str:
        row = self.row
        bits = [f"{row.get('transport', 'stdio').upper()}"]
        if row.get("transport") == "http" and row.get("url"):
            bits.append(str(row["url"]))
        elif row.get("server_info", {}).get("name"):
            bits.append(str(row["server_info"]["name"]))
        if row.get("protocol"):
            bits.append(f"protocol {row['protocol']}")
        if row.get("error"):
            bits.append(str(row["error"])[:160])
        return "  ·  ".join(bits)

    def _capabilities_text(self) -> str:
        row = self.row
        lines = [f"{row.get('tool_count', 0)} tools"]
        if row.get("resource_count"):
            lines.append(f"{row['resource_count']} resources")
        if row.get("prompt_count"):
            lines.append(f"{row['prompt_count']} prompts")
        summary = self._safety_summary()
        if summary:
            lines.append(summary)
        return "\n".join(lines)

    def _safety_summary(self) -> str:
        spec = _spec_from_row(self.row)
        classes = set()
        for tool in self.row.get("tools") or []:
            classes.add(spec.safety_of(tool, "read"))
        bits = []
        if "read" in classes:
            bits.append("Read ✓")
        if "write" in classes:
            bits.append("Write ⚠")
        if "destructive" in classes:
            bits.append("Destructive ⚠")
        return " · ".join(bits)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect().adjusted(1, 1, -1, -1)
            path = _card_path(rect, 12)
            base = SURFACE_ALT if self._hover else SURFACE
            p.fillPath(path, QBrush(_qcolor(base, 230)))
            edge = _qcolor(ACCENT, 160) if self._hover else _qcolor(BORDER_SOFT, 120)
            p.setPen(QPen(edge, 1.2 if self._hover else 1.0))
            p.drawPath(path)
            # A thin accent rail on the left, coloured by state.
            if self.row.get("enabled"):
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(self._state_color())))
                p.drawRoundedRect(rect.left() + 3, rect.top() + 14, 3,
                                  rect.height() - 28, 1.5, 1.5)
        except Exception:
            pass
        p.end()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()


def _spec_from_row(row: dict) -> catalog.ServerSpec:
    """Rebuild a spec from a status row (for icon choice and safety lookup)."""
    spec = catalog.ServerSpec(name=str(row.get("name") or ""),
                              provider=str(row.get("provider") or ""))
    spec.transport = str(row.get("transport") or "stdio")
    spec.url = str(row.get("url") or "")
    spec.permissions = dict(row.get("permissions") or {})
    return spec


# ── the dashboard ─────────────────────────────────────────────────────────────

class McpTab(QWidget):
    """The MCP dashboard, self-contained (own refresh timer, own workers)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pending: dict[str, dict] = {}
        self._filter = "All"
        self._query = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(1200)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        intro = QFrame()
        intro.setStyleSheet(
            f"QFrame{{background:{_rgba(ACCENT, 16)};border:1px solid {_rgba(ACCENT, 60)};"
            f"border-radius:{RADIUS_MD}px;}}")
        iv = QHBoxLayout(intro)
        iv.setContentsMargins(12, 8, 12, 8)
        marker = QLabel("◆")
        marker.setStyleSheet(f"color:{ACCENT_TEXT};font:700 13pt;background:transparent;")
        iv.addWidget(marker)
        text = QLabel("MCP  //  MODEL CONTEXT PROTOCOL")
        text.setStyleSheet(
            f"color:{ACCENT_TEXT};font:800 9pt '{FONT_HEAD.strip(chr(39))}';"
            "background:transparent;letter-spacing:2px;")
        iv.addWidget(text)
        sub = QLabel("Universal tool host & connection bridge for J.A.R.V.I.S.")
        sub.setStyleSheet(f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        iv.addWidget(sub, 1)
        lay.addWidget(intro)

        # ── KPI Telemetry HUD Header ─────────────────────────────────────────
        kpi_frame = QFrame()
        kpi_frame.setStyleSheet(
            f"QFrame{{background:{_rgba(SURFACE_ALT, 160)};border:1px solid {_rgba(BORDER_SOFT, 140)};"
            f"border-radius:{RADIUS_MD}px;}}"
        )
        kpi_box = QHBoxLayout(kpi_frame)
        kpi_box.setContentsMargins(14, 10, 14, 10)
        kpi_box.setSpacing(14)

        def _make_kpi_tile(title: str, default_val: str, val_color: str = ACCENT_TEXT):
            tile = QWidget()
            tile.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            t_lay = QVBoxLayout(tile)
            t_lay.setContentsMargins(0, 0, 0, 0)
            t_lay.setSpacing(2)
            lbl = QLabel(title)
            lbl.setStyleSheet(f"color:{TEXT_DIM};font:700 7pt '{FONT_UI}';letter-spacing:1px;background:transparent;")
            val = QLabel(default_val)
            val.setStyleSheet(f"color:{val_color};font:800 11pt '{FONT_HEAD.strip(chr(39))}';background:transparent;")
            t_lay.addWidget(lbl)
            t_lay.addWidget(val)
            return tile, val

        t1, self._kpi_servers_val = _make_kpi_tile("ACTIVE SERVERS", "0 / 0", GREEN)
        t2, self._kpi_tools_val = _make_kpi_tile("AVAILABLE TOOLS", "0 TOOLS", ACCENT_TEXT)
        t3, self._kpi_resources_val = _make_kpi_tile("RESOURCES & PROMPTS", "0 RES · 0 PRM", TEXT_BRIGHT)
        t4, self._kpi_protocol_val = _make_kpi_tile("CORE PROTOCOL", "MCP 2024-11-05", TEXT_MID)

        kpi_box.addWidget(t1, 1)
        kpi_box.addWidget(t2, 1)
        kpi_box.addWidget(t3, 1)
        kpi_box.addWidget(t4, 1)
        lay.addWidget(kpi_frame)

        # ── Quick Native Local MCP Servers Strip ─────────────────────────────
        quick_frame = QFrame()
        quick_frame.setStyleSheet(
            f"QFrame{{background:{_rgba(SURFACE, 200)};border:1px solid {_rgba(BORDER, 100)};"
            f"border-radius:{RADIUS_MD}px;}}"
        )
        qf_layout = QVBoxLayout(quick_frame)
        qf_layout.setContentsMargins(12, 9, 12, 9)
        qf_layout.setSpacing(6)

        qf_head = QHBoxLayout()
        qf_title = QLabel("QUICK LOCAL MCP SERVERS  //  1-CLICK SETUP & RESTART")
        qf_title.setStyleSheet(f"color:{ACCENT_TEXT};font:700 7.5pt '{FONT_UI}';letter-spacing:1.5px;background:transparent;")
        qf_head.addWidget(qf_title)
        qf_head.addStretch(1)
        qf_desc = QLabel("Pure Python · stdio native · Zero Node/npx dependencies")
        qf_desc.setStyleSheet(f"color:{TEXT_DIM};font:500 7.5pt '{FONT_UI}';background:transparent;")
        qf_head.addWidget(qf_desc)
        qf_layout.addLayout(qf_head)

        btn_strip = QHBoxLayout()
        btn_strip.setSpacing(6)

        quick_servers = [
            ("⚡ System", "system_tools", "System metrics, processes, clipboard, notifications"),
            ("📁 Files", "filesystem", "File read, write, append, directory listing & search"),
            ("🌐 Web", "web_tools", "Webpage scraping, search, HTTP fetch"),
            ("🧠 Vault", "memory_vault", "Obsidian knowledge vault read & search"),
            ("💻 Dev Tools", "dev_tools", "Terminal commands, Git status & diff, Python eval"),
        ]

        for label, key, tip in quick_servers:
            btn = QPushButton(label)
            btn.setToolTip(f"{tip} (1-click activate/restart)")
            btn.setStyleSheet(
                f"QPushButton{{background:{_rgba(SURFACE_ALT, 220)};border:1px solid {_rgba(BORDER_SOFT, 140)};"
                f"color:{TEXT_BRIGHT};font:600 8pt '{FONT_UI}';padding:4px 9px;border-radius:{RADIUS_SM}px;}}"
                f"QPushButton:hover{{background:{_rgba(ACCENT, 40)};border:1px solid {_rgba(ACCENT, 140)};color:{ACCENT_TEXT};}}"
            )
            btn.clicked.connect(lambda _checked=False, k=key: self.install_preset(k))
            btn_strip.addWidget(btn)

        qf_layout.addLayout(btn_strip)
        lay.addWidget(quick_frame)

        # ── toolbar ──────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search MCP servers…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        bar.addWidget(self._search, 1)
        self._filter_box = QComboBox()
        self._filter_box.addItems(["All", "Connected", "Disabled", "Error"])
        self._filter_box.currentTextChanged.connect(self._on_filter)
        bar.addWidget(self._filter_box)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-read every server's real state and capabilities.")
        refresh.clicked.connect(self._refresh_all)
        bar.addWidget(refresh)

        folder_btn = QPushButton("Servers Folder")
        folder_btn.setToolTip("Open mcp_host/servers directory in Explorer")
        folder_btn.clicked.connect(self._open_servers_folder)
        bar.addWidget(folder_btn)

        config_btn = QPushButton("Config File")
        config_btn.setToolTip("Open data/mcp/servers.json in system editor")
        config_btn.clicked.connect(self._open_config_file)
        bar.addWidget(config_btn)

        imp = QPushButton("Import")
        imp.setToolTip("Import an MCP client configuration and review what it "
                       "contains before anything is added.")
        imp.clicked.connect(self._import_config)
        bar.addWidget(imp)
        exp = QPushButton("Export")
        exp.setToolTip("Write this server list to a file. Credentials are never "
                       "included.")
        exp.clicked.connect(self._export_config)
        bar.addWidget(exp)
        add = QPushButton("+  Add MCP Server")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(lambda: self.open_editor())
        bar.addWidget(add)
        lay.addLayout(bar)

        # ── the list ─────────────────────────────────────────────────────────
        self._list_host = QWidget()
        self._list_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(8)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                           "QScrollArea>QWidget>QWidget{background:transparent;}")
        area.viewport().setStyleSheet("background:transparent;")
        area.setWidget(self._list_host)
        lay.addWidget(area, 1)

        self._empty = QLabel("No MCP servers match this view.\n\n"
                             "Press  +  Add MCP Server  to connect one.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(
            f"color:{TEXT_DIM};font:500 9pt '{FONT_UI}';background:transparent;")
        self._empty.hide()
        lay.addWidget(self._empty)
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color:{TEXT_FAINT};font:500 7.5pt '{FONT_UI}';background:transparent;")
        lay.addWidget(self._status)
        self._last_rows_key = None
        self.refresh()

    # ── refresh ───────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Rebuild the cards from the manager's live state (in-memory, cheap)."""
        try:
            data = _manager().status()
        except Exception as exc:
            self._status.setText(f"The MCP manager is unavailable: {exc}")
            return

        total = len(data.get("servers", []))
        connected = sum(1 for r in data.get("servers", [])
                        if r.get("state") == "connected")
        tools_count = data.get("tools", 0)
        res_count = sum(r.get("resource_count", 0) for r in data.get("servers", []))
        prm_count = sum(r.get("prompt_count", 0) for r in data.get("servers", []))

        # Update HUD KPI telemetry
        if hasattr(self, "_kpi_servers_val"):
            self._kpi_servers_val.setText(f"{connected} / {total} ONLINE")
            self._kpi_servers_val.setStyleSheet(
                f"color:{GREEN if connected > 0 else TEXT_MID};font:800 11pt '{FONT_HEAD.strip(chr(39))}';background:transparent;"
            )
            self._kpi_tools_val.setText(f"{tools_count} TOOLS")
            self._kpi_resources_val.setText(f"{res_count} RES  ·  {prm_count} PRM")

        rows = [row for row in data.get("servers", []) if self._matches(row)]

        # Check if rows changed before reconstructing widgets
        current_key = [
            (r.get("name"), r.get("state"), r.get("enabled"), r.get("tool_count"), r.get("detail"))
            for r in rows
        ]
        if self._last_rows_key == current_key:
            return
        self._last_rows_key = current_key

        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for row in rows:
            self._list_layout.addWidget(McpServerCard(row, self))
        self._list_layout.addStretch(1)

        self._status.setText(
            f"{total} server(s) · {connected} connected · "
            f"{tools_count} tool(s) available")
        self._empty.setVisible(not rows and total == 0)

    def _matches(self, row: dict) -> bool:
        if self._query and self._query not in str(row.get("name", "")).lower():
            if self._query not in str(row.get("description", "")).lower():
                return False
        want = self._filter
        if want == "Connected":
            return row.get("state") == "connected"
        if want == "Disabled":
            return not row.get("enabled")
        if want == "Error":
            return row.get("state") == "error"
        return True

    def _on_search(self, text: str) -> None:
        self._query = str(text or "").strip().lower()
        self._last_rows_key = None
        self.refresh()

    def _on_filter(self, text: str) -> None:
        self._filter = str(text or "All")
        self._last_rows_key = None
        self.refresh()

    def _refresh_all(self) -> None:
        self._status.setText("Refreshing every connected server…")

        def _work():
            try:
                _manager().refresh_all()
            except Exception:
                pass
        threading.Thread(target=_work, name="mcp-refresh-all", daemon=True).start()
        self._last_rows_key = None

    def _refresh_list(self) -> None:
        def _work():
            try:
                _manager().refresh_all()
            except Exception:
                pass
        threading.Thread(target=_work, name="mcp-refresh", daemon=True).start()
        self._last_rows_key = None

    # ── actions ───────────────────────────────────────────────────────────────

    def toggle_server(self, name: str, enabled: bool) -> None:
        self._status.setText(f"{'Enabling' if enabled else 'Disabling'} {name}…")
        threading.Thread(target=_manager().set_enabled, args=(name, enabled),
                         name=f"mcp-toggle-{name}", daemon=True).start()
        self._last_rows_key = None

    def restart_server(self, name: str) -> None:
        self._status.setText(f"Restarting {name}…")
        mgr = _manager()
        def _work():
            try:
                spec = mgr.spec_named(name)
                if spec and not spec.enabled:
                    mgr.set_enabled(name, True)
                else:
                    mgr.restart_server(name)
            except Exception:
                pass
        threading.Thread(target=_work, name=f"mcp-restart-{name}", daemon=True).start()
        self._last_rows_key = None
        QTimer.singleShot(900, self.refresh)

    def start_server(self, name: str) -> None:
        self._status.setText(f"Starting {name}…")
        mgr = _manager()
        def _work():
            try:
                mgr.start_server(name)
            except Exception:
                pass
        threading.Thread(target=_work, name=f"mcp-start-{name}", daemon=True).start()
        self._last_rows_key = None
        QTimer.singleShot(900, self.refresh)

    def test_server(self, name: str, callback) -> None:
        """Run live connection & discovery test on server asynchronously."""
        def _work():
            res = {"ok": False, "detail": "Server not found", "tools": 0}
            try:
                mgr = _manager()
                runtime = mgr._runtime(name)
                if runtime is not None:
                    if runtime.session and runtime.session.running:
                        disc = runtime.session.discover(force=True)
                        tools_count = len(runtime.session.tools)
                        res = {"ok": True, "tools": tools_count, "detail": f"{tools_count} tool(s) active"}
                    else:
                        probe_res = catalog.probe(runtime.spec)
                        if not probe_res.get("ok"):
                            res = {"ok": False, "detail": probe_res.get("detail", "Probe failed"), "tools": 0}
                        else:
                            from mcp_host.session import McpSession
                            test_sess = McpSession(runtime.spec)
                            if test_sess.start():
                                disc = test_sess.discover(force=True)
                                tools_count = len(test_sess.tools)
                                test_sess.close("test completed")
                                res = {"ok": True, "tools": tools_count, "detail": f"{tools_count} tool(s) ready"}
                            else:
                                err = test_sess.error or "Failed to start"
                                test_sess.close("test failed")
                                res = {"ok": False, "detail": err, "tools": 0}
            except Exception as exc:
                res = {"ok": False, "detail": str(exc), "tools": 0}
            if callable(callback):
                callback(res)
        threading.Thread(target=_work, name=f"mcp-test-{name}", daemon=True).start()

    def install_preset(self, key: str) -> None:
        """1-click activation, enabling, or instant creation of local MCP servers."""
        mgr = _manager()
        name_map = {
            "filesystem_native": "filesystem",
            "filesystem": "filesystem",
            "system_tools": "system_tools",
            "web_tools": "web_tools",
            "memory_vault": "memory_vault",
            "dev_tools": "dev_tools",
        }
        server_name = name_map.get(key, key)
        spec = mgr.spec_named(server_name)
        if spec is not None:
            if not spec.enabled:
                self._status.setText(f"Enabling and starting {server_name}…")
                mgr.set_enabled(server_name, True)
            else:
                self._status.setText(f"Restarting {server_name}…")
                threading.Thread(target=mgr.restart_server, args=(server_name,),
                                 name=f"mcp-quick-restart-{server_name}", daemon=True).start()
            self._last_rows_key = None
            QTimer.singleShot(800, self.refresh)
            return

        builtins = {s.name: s for s in catalog.builtin_servers()}
        if server_name in builtins:
            spec = builtins[server_name]
            spec.enabled = True
            mgr.add_server(spec)
            self._status.setText(f"Installed native server: {server_name}")
            self._last_rows_key = None
            QTimer.singleShot(800, self.refresh)
            return

        for p in catalog.presets():
            if p.get("key") == key or p.get("name") == server_name:
                cmd = p.get("command", []) + p.get("args", [])
                new_spec = catalog.ServerSpec(
                    name=server_name,
                    command=cmd,
                    description=p.get("description", ""),
                    transport=p.get("transport", "stdio"),
                    provider=p.get("icon", ""),
                    enabled=True,
                )
                mgr.add_server(new_spec)
                self._status.setText(f"Configured and started {server_name}")
                self._last_rows_key = None
                QTimer.singleShot(800, self.refresh)
                return

        self._status.setText(f"No preset found for {key}")

    def _open_servers_folder(self) -> None:
        try:
            folder = Path(__file__).resolve().parent / "mcp_host" / "servers"
            if folder.exists():
                os.startfile(str(folder))
            else:
                QMessageBox.information(self, "MCP Servers", f"Directory not found: {folder}")
        except Exception as exc:
            QMessageBox.warning(self, "MCP Servers", f"Could not open directory: {exc}")

    def _open_config_file(self) -> None:
        try:
            cfg = catalog.config_path()
            if cfg.exists():
                os.startfile(str(cfg))
            else:
                QMessageBox.information(self, "MCP Config", f"Config file not found: {cfg}")
        except Exception as exc:
            QMessageBox.warning(self, "MCP Config", f"Could not open config file: {exc}")

    # ── import / export ───────────────────────────────────────────────────────

    def _import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import MCP configuration", "", "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            QMessageBox.warning(self, "MCP import",
                                f"That file could not be read:\n{exc}")
            return
        entries = _entries_from_config(raw)
        if not entries:
            QMessageBox.information(
                self, "MCP import",
                "No MCP servers were found in that file. It should contain an "
                "'mcpServers' (or 'servers') map.")
            return
        existing = {spec.name for spec in catalog.load_servers()}
        duplicates = [spec.name for spec in entries if spec.name in existing]
        summary = "\n".join(
            f"• {spec.name}  ({spec.transport}"
            + (f", {spec.url}" if spec.is_http else f", {' '.join(spec.command)}")
            + ")" for spec in entries)
        warning = ""
        if duplicates:
            warning = ("\n\nThese already exist and will be replaced:\n"
                       + "\n".join(f"• {name}" for name in duplicates))
        answer = QMessageBox.question(
            self, "MCP import",
            f"{len(entries)} server(s) were found:\n\n{summary}{warning}\n\n"
            "Credential values are not imported — configure them afterwards in "
            "the server's Edit screen.\n\nAdd them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        specs = {spec.name: spec for spec in catalog.load_servers()}
        for spec in entries:
            specs[spec.name] = spec
        result = _manager().save_server_list(list(specs.values()))
        self._status.setText(result.get("detail") or "Import finished.")
        self.refresh()

    def _export_config(self) -> None:
        specs = catalog.load_servers()
        if not specs:
            QMessageBox.information(self, "MCP export", "There are no servers to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export MCP configuration", "mcp-servers.json",
            "JSON files (*.json)")
        if not path:
            return
        payload = {"servers": [spec.as_dict() for spec in specs]}
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except Exception as exc:
            QMessageBox.warning(self, "MCP export",
                                f"That file could not be written:\n{exc}")
            return
        QMessageBox.information(
            self, "MCP export",
            "The server list was exported. Credentials are stored separately and "
            "are never included in an export.")

    # ── dialogs ───────────────────────────────────────────────────────────────

    def open_editor(self, name: str = "", preset: dict | None = None) -> None:
        dialog = McpEditorDialog(name=name, preset=preset, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            spec, secret_values = dialog.result_spec()
            manager = _manager()
            if name:
                result = manager.update_server(spec, old_name=name)
                # Store credentials only after the configuration is accepted.
                secret_store.set_server_secrets(
                    spec.name, **secret_values)
                if not result.get("ok"):
                    QMessageBox.warning(self, "MCP", result.get("detail", ""))
            else:
                result = manager.add_server(spec)
                if result.get("ok"):
                    secret_store.set_server_secrets(spec.name, **secret_values)
                else:
                    QMessageBox.warning(self, "MCP", result.get("detail", ""))
            self._status.setText(str(result.get("detail") or "Saved."))
            self.refresh()

    def open_manage(self, name: str) -> None:
        dialog = McpManageDialog(name=name, panel=self, parent=self)
        dialog.exec()
        self.refresh()


def _entries_from_config(raw: Any) -> list[catalog.ServerSpec]:
    """Read either the ``mcpServers`` or the ``servers`` shape into specs."""
    if not isinstance(raw, dict):
        return []
    block = raw.get("mcpServers")
    if not isinstance(block, dict):
        block = raw.get("servers")
    if not isinstance(block, dict):
        return []
    out: list[catalog.ServerSpec] = []
    for name, entry in block.items():
        if not isinstance(entry, dict):
            continue
        merged = {"name": str(name), **entry}
        merged.setdefault("transport", "http" if entry.get("url") else "stdio")
        spec = catalog._entry_to_spec(merged)
        if spec is not None:
            out.append(spec)
    return out


# ── the Add / Edit screen ─────────────────────────────────────────────────────

class McpEditorDialog(QDialog):
    """Add or edit one MCP server. Only the fields a transport needs are shown."""

    def __init__(self, name: str = "", preset: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self._original = str(name or "")
        self._preset = dict(preset or {})
        self._header_rows: list[tuple[QLineEdit, QLineEdit]] = []
        self._env_rows: list[tuple[QLineEdit, QLineEdit]] = []
        self.setWindowTitle("Add MCP Server" if not self._original else
                            f"Edit — {self._original}")
        # Keep the editor in a compact portrait 4:5 panel. The form itself is
        # scrollable, so longer MCP configurations never push the action bar
        # off-screen. Scale down on very small displays while preserving 4:5.
        self._set_editor_panel_size()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_dialog_css())
        self._build()
        self._load()

    def paintEvent(self, event) -> None:
        """Paint a restrained sci-fi gradient behind the editor without covering child widgets."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        r = self.rect().adjusted(1, 1, -1, -1)
        radius = 18

        # Deep base gradient: darker around the edges, slightly lifted through
        # the centre so the form reads as a focused floating control surface.
        base = QLinearGradient(QPointF(r.left(), r.top()),
                               QPointF(r.right(), r.bottom()))
        base.setColorAt(0.00, QColor(_t.BG_DEEP))
        base.setColorAt(0.42, QColor(SURFACE))
        base.setColorAt(0.72, QColor(SURFACE_ALT))
        base.setColorAt(1.00, QColor(_t.BG_DEEP))
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), radius, radius)
        p.fillPath(path, QBrush(base))

        # Soft accent blooms keep the panel dimensional without competing with
        # the controls. The content remains the single visual focal point.
        for cx, cy, size, colour, alpha in (
            (0.86, 0.10, 0.58, ACCENT, 42),
            (0.12, 0.88, 0.72, ACCENT, 30),
        ):
            center = QPointF(r.left() + r.width() * cx,
                             r.top() + r.height() * cy)
            radius_px = max(r.width(), r.height()) * size
            glow = QRadialGradient(center, radius_px)
            glow.setColorAt(0.0, QColor(colour) if alpha >= 255 else QColor(
                QColor(colour).red(), QColor(colour).green(), QColor(colour).blue(), alpha))
            glow.setColorAt(0.55, QColor(
                QColor(colour).red(), QColor(colour).green(), QColor(colour).blue(), alpha // 2))
            glow.setColorAt(1.0, QColor(
                QColor(colour).red(), QColor(colour).green(), QColor(colour).blue(), 0))
            p.fillPath(path, QBrush(glow))

        # Very subtle diagonal energy lines for depth.
        p.save()
        p.setClipPath(path)
        accent_line = QColor(ACCENT_TEXT)
        accent_line.setAlpha(16)
        p.setPen(QPen(accent_line, 1))
        step = 34
        for offset in range(-r.height(), r.width() + r.height(), step):
            p.drawLine(r.left() + offset, r.bottom(),
                       r.left() + offset + r.height(), r.top())
        p.restore()

        # Crisp outer edge so the 4:5 panel remains visually anchored.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(BORDER_SOFT), 1))
        p.drawPath(path)
        p.end()

    # ── construction ─────────────────────────────────────────────────────────

    def _set_editor_panel_size(self) -> None:
        """Set a portrait 4:5 dialog size, scaling down on short displays."""
        from PyQt6.QtGui import QGuiApplication

        target_w, target_h = 560, 700
        screen = QGuiApplication.screenAt(self.cursor().pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            margin_w, margin_h = 48, 48
            max_w = max(360, avail.width() - margin_w)
            max_h = max(450, avail.height() - margin_h)

            scale = min(1.0, max_w / target_w, max_h / target_h)
            target_w = max(360, int(round(target_w * scale)))
            target_h = int(round(target_w * 5 / 4))

            if target_h > max_h:
                target_h = max(450, int(max_h))
                target_w = int(round(target_h * 4 / 5))

        self.setFixedSize(target_w, target_h)
        self.setSizeGripEnabled(False)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(9)

        # The form scrolls; the title and action row stay visually stable around it.
        scroll = QScrollArea()
        scroll.setObjectName("McpEditorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea>QWidget>QWidget{background:transparent;}"
        )
        scroll.viewport().setStyleSheet("background:transparent;")

        body_host = QWidget()
        body_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        body = QVBoxLayout(body_host)
        body.setContentsMargins(4, 2, 4, 8)
        body.setSpacing(10)
        scroll.setWidget(body_host)

        title = QLabel("ADD MCP SERVER" if not self._original else "EDIT MCP SERVER")
        title.setStyleSheet(
            f"color:{TEXT_BRIGHT};font:800 11pt '{FONT_HEAD.strip(chr(39))}';"
            "letter-spacing:2px;background:transparent;")
        body.addWidget(title)
        hint = QLabel("Any compatible MCP server — local or remote. Choose a "
                      "preset to pre-fill, or configure it yourself.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        body.addWidget(hint)

        body.addWidget(_label("PRESET"))
        self._preset_box = QComboBox()
        for entry in catalog.presets():
            self._preset_box.addItem(str(entry["name"]), entry["key"])
        self._preset_box.currentIndexChanged.connect(self._on_preset)
        body.addWidget(self._preset_box)

        body.addWidget(_label("NAME"))
        self._name = QLineEdit()
        self._name.setPlaceholderText("GitHub MCP")
        body.addWidget(self._name)

        body.addWidget(_label("TRANSPORT"))
        self._transport = QComboBox()
        self._transport.addItem("stdio  ·  local process", "stdio")
        self._transport.addItem("Streamable HTTP  ·  remote endpoint", "http")
        self._transport.currentIndexChanged.connect(self._apply_transport)
        body.addWidget(self._transport)

        # ── stdio fields ─────────────────────────────────────────────────────
        self._stdio = QFrame()
        sv = QVBoxLayout(self._stdio)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(6)
        sv.addWidget(_label("COMMAND"))
        self._command = QLineEdit()
        self._command.setPlaceholderText("npx")
        sv.addWidget(self._command)
        sv.addWidget(_label("ARGUMENTS  ·  one per line"))
        self._args = QPlainTextEdit()
        self._args.setPlaceholderText("-y\n@modelcontextprotocol/server-filesystem\n.")
        self._args.setFixedHeight(72)
        sv.addWidget(self._args)
        sv.addWidget(_label("WORKING DIRECTORY  ·  optional"))
        self._cwd = QLineEdit()
        self._cwd.setPlaceholderText("(defaults to the project directory)")
        sv.addWidget(self._cwd)
        sv.addWidget(_label("ENVIRONMENT VARIABLES"))
        env_head = QHBoxLayout()
        env_head.addStretch(1)
        add_env = QPushButton("+ Add Variable")
        add_env.clicked.connect(lambda: self._add_env_row())
        env_head.addWidget(add_env)
        sv.addLayout(env_head)
        self._env_host = QWidget()
        self._env_layout = QVBoxLayout(self._env_host)
        self._env_layout.setContentsMargins(0, 0, 0, 0)
        self._env_layout.setSpacing(4)
        sv.addWidget(self._env_host)
        body.addWidget(self._stdio)

        # ── http fields ──────────────────────────────────────────────────────
        self._http = QFrame()
        hv = QVBoxLayout(self._http)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(6)
        hv.addWidget(_label("SERVER URL"))
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://connect.composio.dev/mcp")
        hv.addWidget(self._url)
        insecure = QHBoxLayout()
        self._insecure = QCheckBox("Allow plain HTTP (no TLS) for this server")
        insecure.addWidget(self._insecure)
        insecure.addStretch(1)
        hv.addLayout(insecure)
        hv.addWidget(_label("AUTHENTICATION"))
        self._auth = QComboBox()
        self._auth.addItem("None", "none")
        self._auth.addItem("Bearer token", "bearer")
        self._auth.addItem("Custom header", "custom_header")
        self._auth.currentIndexChanged.connect(self._apply_auth)
        hv.addWidget(self._auth)
        self._bearer_host = QWidget()
        bv = QVBoxLayout(self._bearer_host)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.addWidget(_label("BEARER TOKEN"))
        self._bearer = QLineEdit()
        self._bearer.setEchoMode(QLineEdit.EchoMode.Password)
        self._bearer.setPlaceholderText("stored securely; shown masked")
        bv.addWidget(self._bearer)
        hv.addWidget(self._bearer_host)
        self._header_host = QWidget()
        hvv = QVBoxLayout(self._header_host)
        hvv.setContentsMargins(0, 0, 0, 0)
        hvv.setSpacing(4)
        head_row = QHBoxLayout()
        head_row.addWidget(_label("HEADERS"))
        head_row.addStretch(1)
        add_header = QPushButton("+ Add Header")
        add_header.clicked.connect(lambda: self._add_header_row())
        head_row.addWidget(add_header)
        hvv.addLayout(head_row)
        self._header_rows_host = QWidget()
        self._header_layout = QVBoxLayout(self._header_rows_host)
        self._header_layout.setContentsMargins(0, 0, 0, 0)
        self._header_layout.setSpacing(4)
        hvv.addWidget(self._header_rows_host)
        hv.addWidget(self._header_host)
        body.addWidget(self._http)

        # ── permissions ──────────────────────────────────────────────────────
        body.addWidget(_label("PERMISSIONS"))
        perms = QFrame()
        pv = QVBoxLayout(perms)
        pv.setContentsMargins(10, 8, 10, 8)
        pv.setSpacing(4)
        self._allow_discovery = QCheckBox("Allow tool discovery")
        self._allow_read = QCheckBox("Allow read operations")
        self._approve_writes = QCheckBox("Write operations require my approval")
        self._confirm_destructive = QCheckBox("Destructive operations always "
                                              "require confirmation")
        for box in (self._allow_discovery, self._allow_read,
                    self._approve_writes, self._confirm_destructive):
            pv.addWidget(box)
        body.addWidget(perms)

        # ── reconnect ────────────────────────────────────────────────────────
        reconn = QHBoxLayout()
        self._reconnect = QCheckBox("Auto reconnect")
        reconn.addWidget(self._reconnect)
        reconn.addWidget(QLabel("Retry delay"))
        self._delay = QDoubleSpinBox()
        self._delay.setRange(0.5, 300.0)
        self._delay.setSuffix(" s")
        reconn.addWidget(self._delay)
        reconn.addWidget(QLabel("Max attempts"))
        self._attempts = QSpinBox()
        self._attempts.setRange(1, 50)
        reconn.addWidget(self._attempts)
        reconn.addStretch(1)
        body.addLayout(reconn)

        self._enabled = QCheckBox("Enabled")
        body.addWidget(self._enabled)

        # ── add to your client config ────────────────────────────────────────
        body.addWidget(self._client_config_section())

        root.addWidget(scroll, 1)

        # ── actions ──────────────────────────────────────────────────────────
        actions = QHBoxLayout()
        self._test_status = QLabel("")
        self._test_status.setWordWrap(True)
        self._test_status.setStyleSheet(
            f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        actions.addWidget(self._test_status, 1)
        test = QPushButton("Test Connection")
        test.clicked.connect(self._test)
        actions.addWidget(test)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton("Save")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        actions.addWidget(save)
        root.addLayout(actions)

        self._test_timer = QTimer(self)
        self._test_timer.setInterval(300)
        self._test_timer.timeout.connect(self._drain_test)
        self._test_timer.start()

    def _client_config_section(self) -> QWidget:
        box = QFrame()
        box.setStyleSheet(
            f"QFrame{{background:{_rgba(SURFACE_ALT, 120)};"
            f"border:1px solid {_rgba(BORDER_SOFT, 120)};border-radius:{RADIUS_MD}px;}}")
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        self._config_toggle = QPushButton("▾  Add to your client config")
        self._config_toggle.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{ACCENT_TEXT};"
            f"font:700 8.5pt '{FONT_UI}';text-align:left;padding:2px;}}")
        self._config_toggle.clicked.connect(self._toggle_config)
        head.addWidget(self._config_toggle)
        head.addStretch(1)
        head.addWidget(QLabel("Target"))
        self._target = QComboBox()
        for key in config_gen.CLIENT_TARGETS:
            layout = config_gen._TARGET_LAYOUT.get(key, {})
            self._target.addItem(layout.get("label", key.title()), key)
        self._target.currentIndexChanged.connect(self._on_target_changed)
        head.addWidget(self._target)
        v.addLayout(head)
        desc = QLabel("Add the following to your client's MCP configuration.")
        desc.setWordWrap(True)
        self._config_desc = desc
        desc.setStyleSheet(f"color:{TEXT_DIM};font:500 7.5pt '{FONT_UI}';background:transparent;")
        v.addWidget(desc)
        self._config_text = QPlainTextEdit()
        self._config_text.setReadOnly(False)
        self._config_text.setPlaceholderText(
            "Generated configuration — edit or paste here if you need to.")
        self._config_text.setFixedHeight(150)
        self._config_text.textChanged.connect(self._on_config_edited)
        self._config_text.setStyleSheet(
            f"QPlainTextEdit{{background:{_qcolor(_t.BG_DEEP, 200)};color:{TEXT_MID};"
            f"font:500 8pt '{FONT_SMALL}';border:1px solid {_rgba(BORDER, 90)};"
            f"border-radius:{RADIUS_SM}px;padding:6px;}}")
        v.addWidget(self._config_text)
        bottom = QHBoxLayout()
        self._include_secrets = QCheckBox("Include credential values")
        self._include_secrets.setToolTip(
            "Off by default. Turning this on puts the real credential into the "
            "copied text — only do it for a file you keep private.")
        self._include_secrets.stateChanged.connect(
            lambda: self._render_config(force=True))
        bottom.addWidget(self._include_secrets)
        bottom.addStretch(1)
        self._regen_btn = QPushButton("Regenerate")
        self._regen_btn.setToolTip("Rebuild the snippet from the fields above, "
                                   "discarding hand edits.")
        self._regen_btn.clicked.connect(self._regenerate_config)
        bottom.addWidget(self._regen_btn)
        self._save_client_btn = QPushButton("Save to Client Config")
        self._save_client_btn.setObjectName("PrimaryButton")
        self._save_client_btn.setToolTip(
            "Write this server into the selected client's own config file, "
            "keeping every server already written there.")
        self._save_client_btn.clicked.connect(self._save_client_config)
        bottom.addWidget(self._save_client_btn)
        self._copy_btn = QPushButton("Copy Config")
        self._copy_btn.clicked.connect(self._copy_config)
        bottom.addWidget(self._copy_btn)
        v.addLayout(bottom)
        self._config_body = v
        return box

    # ── field helpers ────────────────────────────────────────────────────────

    def _add_header_row(self, name: str = "", value: str = "") -> None:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("x-consumer-api-key")
        value_edit = QLineEdit(value)
        value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        value_edit.setPlaceholderText("value (stored securely)")
        h.addWidget(name_edit, 1)
        h.addWidget(value_edit, 1)
        remove = QPushButton("✕")
        remove.setFixedWidth(30)
        remove.clicked.connect(lambda: self._remove_row(self._header_rows, row))
        h.addWidget(remove)
        self._header_rows.append((name_edit, value_edit))
        self._header_layout.addWidget(row)
        self._render_config()

    def _add_env_row(self, name: str = "", value: str = "") -> None:
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("GITHUB_PERSONAL_ACCESS_TOKEN")
        value_edit = QLineEdit(value)
        value_edit.setEchoMode(QLineEdit.EchoMode.Password)
        value_edit.setPlaceholderText("value (stored securely)")
        h.addWidget(name_edit, 1)
        h.addWidget(value_edit, 1)
        remove = QPushButton("✕")
        remove.setFixedWidth(30)
        remove.clicked.connect(lambda: self._remove_row(self._env_rows, row))
        h.addWidget(remove)
        self._env_rows.append((name_edit, value_edit))
        self._env_layout.addWidget(row)
        self._render_config()

    @staticmethod
    def _remove_row(store: list, row: QWidget) -> None:
        for index, (name_edit, value_edit) in enumerate(list(store)):
            if name_edit.parent() is row or value_edit.parent() is row:
                store.pop(index)
                break
        row.setParent(None)
        row.deleteLater()

    def _apply_transport(self) -> None:
        is_http = self._transport.currentData() == "http"
        self._stdio.setVisible(not is_http)
        self._http.setVisible(is_http)
        self._apply_auth()
        self._render_config()

    def _apply_auth(self) -> None:
        if self._transport.currentData() != "http":
            return
        kind = self._auth.currentData()
        self._bearer_host.setVisible(kind == "bearer")
        self._header_host.setVisible(kind == "custom_header")
        self._render_config()

    def _toggle_config(self) -> None:
        visible = self._config_text.isVisible()
        self._config_text.setVisible(not visible)
        self._copy_btn.setVisible(not visible)
        self._include_secrets.setVisible(not visible)
        self._config_toggle.setText(("▾  " if not visible else "▸  ")
                                    + "Add to your client config")
        self._render_config()

    # ── presets / loading ────────────────────────────────────────────────────

    def _on_preset(self) -> None:
        key = self._preset_box.currentData()
        entry = next((p for p in catalog.presets() if p["key"] == key), None)
        if entry is None:
            return
        self._apply_preset(entry)

    def _apply_preset(self, entry: dict) -> None:
        transport = str(entry.get("transport") or "stdio")
        index = self._transport.findData(transport)
        if index >= 0:
            self._transport.setCurrentIndex(index)
        if not self._name.text().strip():
            self._name.setText(str(entry.get("name") or ""))
        if transport == "http":
            if entry.get("url"):
                self._url.setText(str(entry["url"]))
            auth = str(entry.get("auth") or "none")
            idx = self._auth.findData(auth)
            if idx >= 0:
                self._auth.setCurrentIndex(idx)
            if auth == "custom_header" and not self._header_rows:
                for name in entry.get("header_names") or []:
                    self._add_header_row(str(name), "")
        else:
            command = entry.get("command") or []
            if command:
                self._command.setText(str(command[0]))
            args = entry.get("args") or command[1:] if command else []
            if args:
                self._args.setPlainText("\n".join(str(a) for a in args))
            for name in entry.get("env_names") or []:
                self._add_env_row(str(name), "")
        self._apply_transport()

    def _load(self) -> None:
        spec = None
        self._copied = False
        if self._original:
            spec = next((s for s in catalog.load_servers()
                         if s.name == self._original), None)
        if spec is None and self._preset:
            entry = next((p for p in catalog.presets()
                          if p["key"] == self._preset.get("key")), None)
            if entry:
                self._apply_preset(entry)
                self._load_permissions(catalog.ServerSpec(name=""))
                return
        if spec is None:
            self._load_permissions(catalog.ServerSpec(name=""))
            return
        self._name.setText(spec.name)
        index = self._transport.findData("http" if spec.is_http else "stdio")
        if index >= 0:
            self._transport.setCurrentIndex(index)
        if spec.is_http:
            self._url.setText(spec.url)
            self._insecure.setChecked(bool(spec.allow_insecure_http))
            idx = self._auth.findData(spec.auth)
            if idx >= 0:
                self._auth.setCurrentIndex(idx)
            stored = secret_store.server_secrets(spec.name)
            if stored.get("bearer_token"):
                self._bearer.setPlaceholderText("••••••••  (stored — leave blank "
                                                "to keep)")
            for name in spec.header_name_list():
                self._add_header_row(name, "")
        else:
            command = list(spec.command)
            if command:
                self._command.setText(str(command[0]))
            if len(command) > 1:
                self._args.setPlainText("\n".join(str(a) for a in command[1:]))
            self._cwd.setText(spec.cwd)
            for name in (spec.env or {}):
                self._add_env_row(str(name), "")
        self._load_permissions(spec)
        self._apply_transport()

    def _load_permissions(self, spec: catalog.ServerSpec) -> None:
        perms = spec.permissions or catalog.DEFAULT_PERMISSIONS
        self._allow_discovery.setChecked(bool(perms.get("allow_discovery", True)))
        self._allow_read.setChecked(bool(perms.get("allow_read", True)))
        self._approve_writes.setChecked(bool(perms.get("require_approval_writes", True)))
        self._confirm_destructive.setChecked(
            bool(perms.get("confirm_destructive", True)))
        reconn = spec.reconnect or catalog.DEFAULT_RECONNECT
        self._reconnect.setChecked(bool(reconn.get("enabled", True)))
        self._delay.setValue(float(reconn.get("delay_s", 5.0) or 5.0))
        self._attempts.setValue(int(reconn.get("max_attempts", 5) or 5))
        self._enabled.setChecked(bool(spec.enabled))

    # ── config generation ────────────────────────────────────────────────────

    def _current_spec(self, include_secrets: bool = False) -> catalog.ServerSpec:
        spec = catalog.ServerSpec(name=self._name.text().strip() or "mcp-server")
        spec.transport = str(self._transport.currentData() or "stdio")
        spec.url = self._url.text().strip()
        spec.allow_insecure_http = self._insecure.isChecked()
        spec.auth = str(self._auth.currentData() or "none")
        for name_edit, _value in self._header_rows:
            name = name_edit.text().strip()
            if name:
                spec.header_names.append(name)
        spec.command = ([self._command.text().strip()]
                        + [line.strip() for line in
                           self._args.toPlainText().splitlines() if line.strip()])
        spec.command = [part for part in spec.command if part]
        spec.cwd = self._cwd.text().strip()
        for name_edit, _value in self._env_rows:
            name = name_edit.text().strip()
            if name:
                spec.env[name] = ""
        perms = dict(catalog.DEFAULT_PERMISSIONS)
        perms.update({
            "allow_discovery": self._allow_discovery.isChecked(),
            "allow_read": self._allow_read.isChecked(),
            "require_approval_writes": self._approve_writes.isChecked(),
            "confirm_destructive": self._confirm_destructive.isChecked(),
        })
        spec.permissions = perms
        spec.reconnect = {"enabled": self._reconnect.isChecked(),
                          "delay_s": float(self._delay.value()),
                          "max_attempts": int(self._attempts.value())}
        spec.enabled = self._enabled.isChecked()
        return spec

    def _on_config_edited(self) -> None:
        """A hand edit (or a paste) is the user's intent — stop regenerating."""
        self._config_dirty = True
        self._copied = False
        self._copy_btn.setText("Copy Config")

    def _on_target_changed(self) -> None:
        """Switching client: regenerate, unless the box holds a hand edit.

        A pasted or edited snippet is the user's intent, so switching the
        target must not throw it away — it only re-points where Save writes.
        """
        target = str(self._target.currentData() or "generic")
        if not getattr(self, "_config_dirty", False):
            self._render_config(force=True)
            return
        try:
            found = client_configs.existing(target)
            self._client_config_path = str(found.get("path") or "")
            label = client_configs.target_label(target)
            if self._client_config_path:
                self._config_desc.setText(
                    f"Your text is kept as written; Save writes it to {label} — "
                    f"{self._client_config_path}")
            else:
                self._config_desc.setText(
                    f"Your text is kept as written; Save asks where {label} "
                    f"keeps its configuration.")
        except Exception:
            pass

    def _regenerate_config(self) -> None:
        self._render_config(force=True)

    def _save_client_config(self) -> None:
        """Write this server into the client's own config file, on top."""
        target = str(self._target.currentData() or "generic")
        text = self._config_text.toPlainText()
        ok, problem = config_gen.validate(text)
        if not ok:
            QMessageBox.warning(self, "Save to Client Config", problem)
            return
        try:
            mapping = json.loads(text)
        except Exception as exc:
            QMessageBox.warning(self, "Save to Client Config", str(exc))
            return
        result = client_configs.save(target, mapping)
        if not result.get("ok") and not result.get("path"):
            # No known location for this client: the user picks the file.
            path, _ = QFileDialog.getSaveFileName(
                self, "Save client configuration", "mcp.json",
                "JSON files (*.json)")
            if not path:
                return
            result = client_configs.save(target, mapping, path=path)
        if result.get("ok"):
            self._test_status.setStyleSheet(
                f"color:{GREEN};font:600 8pt '{FONT_UI}';background:transparent;")
            self._test_status.setText(f"✓ {result.get('detail')}")
        else:
            message = str(result.get("detail") or "the save failed")
            self._test_status.setStyleSheet(
                f"color:{RED};font:600 8pt '{FONT_UI}';background:transparent;")
            self._test_status.setText(f"✕ {message}")
            QMessageBox.warning(self, "Save to Client Config", message)

    def _render_config(self, force: bool = False) -> None:
        """Regenerate the snippet unless the user edited or pasted in it."""
        if getattr(self, "_config_dirty", False) and not force:
            return
        spec = self._current_spec()
        target = str(self._target.currentData() or "generic")
        mapping = config_gen.client_config(
            spec, target=target,
            include_secrets=self._include_secrets.isChecked(),
            secret_values=self._secret_values())
        self._client_config_path = ""
        base_text = "Add the following to your client's MCP configuration."
        try:
            found = client_configs.existing(target)
            self._client_config_path = str(found.get("path") or "")
            if found.get("mapping"):
                # The client already has a config: show this server added on
                # top of exactly what is already written there.
                mapping = client_configs.merge(found["mapping"], mapping)
                base_text = (f"Adding on top of the existing configuration: "
                             f"{self._client_config_path}")
            elif found.get("error"):
                base_text = str(found["error"])
                self._client_config_path = str(found.get("path") or "")
        except Exception:
            pass
        try:
            self._config_desc.setText(base_text)
        except Exception:
            pass
        self._config_text.blockSignals(True)
        self._config_text.setPlainText(json.dumps(mapping, indent=2))
        self._config_text.blockSignals(False)
        self._config_dirty = False
        self._copied = False
        self._copy_btn.setText("Copy Config")


    def _secret_values(self) -> dict:
        values: dict[str, str] = {}
        if self._bearer.text().strip():
            values["bearer_token"] = self._bearer.text().strip()
        for name_edit, value_edit in self._header_rows:
            if name_edit.text().strip() and value_edit.text().strip():
                values[f"header:{name_edit.text().strip()}"] = value_edit.text().strip()
        for name_edit, value_edit in self._env_rows:
            if name_edit.text().strip() and value_edit.text().strip():
                values[f"env:{name_edit.text().strip()}"] = value_edit.text().strip()
        return values

    def _copy_config(self) -> None:
        text = self._config_text.toPlainText()
        ok, problem = config_gen.validate(text)
        if not ok:
            QMessageBox.warning(self, "Copy Config",
                                f"The configuration cannot be copied:\n{problem}")
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self._copy_btn.setText("Copied ✓")
        QTimer.singleShot(1600, lambda: self._copy_btn.setText("Copy Config"))

    # ── test / save ──────────────────────────────────────────────────────────

    def _test(self) -> None:
        spec = self._current_spec()
        self._test_status.setText(f"Connecting to {spec.name}…")
        self._test_result = None

        def _work():
            from mcp_host.manager import test_connection
            try:
                self._test_result = test_connection(spec)
            except Exception as exc:
                self._test_result = {"ok": False, "detail": str(exc)}
        threading.Thread(target=_work, name="mcp-editor-test", daemon=True).start()

    def _drain_test(self) -> None:
        result = getattr(self, "_test_result", None)
        if not result:
            return
        self._test_result = None
        if result.get("ok"):
            self._test_status.setStyleSheet(
                f"color:{GREEN};font:600 8pt '{FONT_UI}';background:transparent;")
            self._test_status.setText(
                f"✓ {result.get('detail')}  —  {result.get('tools', 0)} tool(s)"
                + (f", {result.get('resources', 0)} resource(s)"
                   if result.get("resources") else "")
                + (f", {result.get('prompts', 0)} prompt(s)"
                   if result.get("prompts") else ""))
        else:
            self._test_status.setStyleSheet(
                f"color:{RED};font:600 8pt '{FONT_UI}';background:transparent;")
            self._test_status.setText(f"✕ {result.get('detail')}")

    def result_spec(self) -> tuple[catalog.ServerSpec, dict]:
        """The spec and the credential values to store (only what was entered)."""
        spec = self._current_spec()
        values: dict = {}
        if self._bearer.text().strip():
            values["bearer_token"] = self._bearer.text().strip()
        headers = {}
        for name_edit, value_edit in self._header_rows:
            name = name_edit.text().strip()
            value = value_edit.text().strip()
            if name and value:
                headers[name] = value
        if headers:
            values["headers"] = headers
        env = {}
        for name_edit, value_edit in self._env_rows:
            name = name_edit.text().strip()
            value = value_edit.text().strip()
            if name and value:
                env[name] = value
        if env:
            values["env"] = env
        return spec, values

    def _save(self) -> None:
        spec = self._current_spec()
        if not spec.name:
            QMessageBox.warning(self, "MCP", "Give the server a name.")
            return
        if spec.is_http and not spec.url:
            QMessageBox.warning(self, "MCP", "Enter the server URL.")
            return
        if not spec.is_http and not spec.command:
            QMessageBox.warning(self, "MCP", "Enter the command to run.")
            return
        self.accept()


def _label(text: str) -> QLabel:
    label = QLabel(str(text))
    label.setStyleSheet(
        f"color:{TEXT_DIM};font:700 7pt '{FONT_UI}';letter-spacing:1px;"
        "background:transparent;")
    return label


def _dialog_css() -> str:
    return (f"QDialog{{background:{_qcolor(_t.BG_DEEP, 250)};}}"
            f"QLineEdit,QPlainTextEdit,QComboBox,QSpinBox,QDoubleSpinBox{{"
            f"background:{_qcolor(_t.SURFACE_INSET, 220)};color:{TEXT};"
            f"border:1px solid {_rgba(BORDER, 110)};border-radius:{RADIUS_SM}px;"
            f"padding:5px;font:500 9pt '{FONT_UI}';}}"
            f"QCheckBox{{color:{TEXT_MID};font:500 8.5pt '{FONT_UI}';}}"
            f"QLabel{{background:transparent;}}")


def _generate_sample_from_schema(schema: dict) -> dict:
    """Generate sample JSON argument structure from a tool's JSON schema."""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    sample = {}
    for key, pinfo in props.items():
        if not isinstance(pinfo, dict):
            sample[key] = ""
            continue
        ptype = pinfo.get("type", "string")
        if "default" in pinfo:
            sample[key] = pinfo["default"]
        elif "enum" in pinfo and pinfo["enum"]:
            sample[key] = pinfo["enum"][0]
        elif ptype == "string":
            sample[key] = f"<{key}>"
        elif ptype in ("integer", "number"):
            sample[key] = 0
        elif ptype == "boolean":
            sample[key] = True
        elif ptype == "array":
            sample[key] = []
        elif ptype == "object":
            sample[key] = {}
        else:
            sample[key] = ""
    return sample


# ── the Manage screen ─────────────────────────────────────────────────────────

class McpManageDialog(QDialog):
    """One server in depth: overview, tools, resources, prompts, permissions, logs."""

    def __init__(self, name: str, panel: McpTab, parent=None) -> None:
        super().__init__(parent)
        self._name = str(name)
        self._panel = panel
        self._log_seen = 0
        self._tool_schema_map: dict[str, dict] = {}
        self.setWindowTitle(f"MCP — {self._name}")
        self.setMinimumSize(720, 560)
        self.setStyleSheet(_dialog_css())
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(900)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    def _row(self) -> dict:
        try:
            for row in _manager().status().get("servers", []):
                if row.get("name") == self._name:
                    return row
        except Exception:
            pass
        return {"name": self._name}

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        head = QHBoxLayout()
        self._title = QLabel(self._name)
        self._title.setStyleSheet(
            f"color:{TEXT_BRIGHT};font:800 12pt '{FONT_HEAD.strip(chr(39))}';"
            "letter-spacing:1px;background:transparent;")
        head.addWidget(self._title)
        head.addStretch(1)
        self._head_state = QLabel("")
        head.addWidget(self._head_state)
        root.addLayout(head)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._overview_tab(), "Overview")
        self._tabs.addTab(self._tools_tab(), "Tools")
        self._tabs.addTab(self._resources_tab(), "Resources")
        self._tabs.addTab(self._prompts_tab(), "Prompts")
        self._tabs.addTab(self._permissions_tab(), "Permissions")
        self._tabs.addTab(self._logs_tab(), "Logs")
        root.addWidget(self._tabs, 1)

        actions = QHBoxLayout()
        for label, handler, tip in (
            ("Test Connection", self._test, "Connect to this server for real and "
                                            "report what it answers."),
            ("Refresh Capabilities", self._refresh_caps,
             "Re-read the server's tools, resources and prompts."),
            ("Restart", self._restart, "Stop and start the server again."),
            ("Edit Configuration", self._edit, "Change this server's settings."),
        ):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        remove = QPushButton("Remove Server")
        remove.setToolTip("Disconnect and delete this server's J.A.R.V.I.S. "
                          "configuration.")
        remove.clicked.connect(self._remove)
        actions.addWidget(remove)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        root.addLayout(actions)

    def _overview_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self._overview = QLabel("")
        self._overview.setWordWrap(True)
        self._overview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._overview.setStyleSheet(
            f"color:{TEXT};font:500 9pt '{FONT_UI}';background:transparent;")
        v.addWidget(self._overview)
        v.addStretch(1)
        return page

    def _tools_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self._tools_list = QLabel("")
        self._tools_list.setWordWrap(True)
        self._tools_list.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._tools_list.setStyleSheet(
            f"color:{TEXT_MID};font:500 8.5pt '{FONT_SMALL}';background:transparent;")
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet("QScrollArea{background:transparent;border:none;}"
                           "QScrollArea>QWidget>QWidget{background:transparent;}")
        holder = QWidget()
        holder.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        holder_v = QVBoxLayout(holder)
        holder_v.addWidget(self._tools_list)
        holder_v.addStretch(1)
        area.setWidget(holder)
        v.addWidget(area, 1)

        # ── the developer tester ─────────────────────────────────────────────
        tester = QFrame()
        tester.setStyleSheet(
            f"QFrame{{background:{_rgba(SURFACE_ALT, 110)};"
            f"border:1px solid {_rgba(BORDER_SOFT, 110)};border-radius:{RADIUS_MD}px;}}")
        tv = QVBoxLayout(tester)
        tv.setContentsMargins(10, 8, 10, 10)
        tv.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Run tool"))
        self._run_tool = QComboBox()
        self._run_tool.currentTextChanged.connect(self._on_tool_selected)
        row.addWidget(self._run_tool, 1)
        tv.addLayout(row)

        self._tool_desc_lbl = QLabel("")
        self._tool_desc_lbl.setWordWrap(True)
        self._tool_desc_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;padding:2px 0px;"
        )
        tv.addWidget(self._tool_desc_lbl)

        self._run_args = QPlainTextEdit()
        self._run_args.setFixedHeight(75)
        self._run_args.setPlaceholderText('{\n  "arg": "value"\n}')
        tv.addWidget(self._run_args)

        run_row = QHBoxLayout()
        self._run_status = QLabel("")
        self._run_status.setStyleSheet(
            f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        run_row.addWidget(self._run_status, 1)

        fmt_btn = QPushButton("Format JSON")
        fmt_btn.setToolTip("Format and indent argument JSON")
        fmt_btn.clicked.connect(self._format_json_args)
        run_row.addWidget(fmt_btn)

        sample_btn = QPushButton("Sample Args")
        sample_btn.setToolTip("Populate default sample arguments for this tool")
        sample_btn.clicked.connect(self._reset_sample_args)
        run_row.addWidget(sample_btn)

        run = QPushButton("Run Test")
        run.setObjectName("PrimaryButton")
        run.clicked.connect(self._run_test)
        run_row.addWidget(run)
        tv.addLayout(run_row)

        self._run_output = QPlainTextEdit()
        self._run_output.setReadOnly(True)
        self._run_output.setFixedHeight(95)
        self._run_output.setStyleSheet(
            f"QPlainTextEdit{{background:{_qcolor(_t.BG_DEEP, 200)};color:{TEXT_MID};"
            f"font:500 8pt '{FONT_SMALL}';border:1px solid {_rgba(BORDER, 80)};"
            f"border-radius:{RADIUS_SM}px;padding:5px;}}")
        tv.addWidget(self._run_output)
        v.addWidget(tester)
        return page

    def _resources_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self._resources_list = QLabel("")
        self._resources_list.setWordWrap(True)
        self._resources_list.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._resources_list.setStyleSheet(
            f"color:{TEXT_MID};font:500 8.5pt '{FONT_SMALL}';background:transparent;")
        v.addWidget(self._resources_list)
        v.addStretch(1)
        return page

    def _prompts_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self._prompts_list = QLabel("")
        self._prompts_list.setWordWrap(True)
        self._prompts_list.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._prompts_list.setStyleSheet(
            f"color:{TEXT_MID};font:500 8.5pt '{FONT_SMALL}';background:transparent;")
        v.addWidget(self._prompts_list)
        v.addStretch(1)
        return page

    def _permissions_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        info = QLabel("A tool's safety class decides whether it needs approval. "
                      "Destructive tools stay hidden until you enable that exact "
                      "tool below.")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        v.addWidget(info)
        self._perm_rows_host = QWidget()
        self._perm_layout = QVBoxLayout(self._perm_rows_host)
        self._perm_layout.setContentsMargins(0, 0, 0, 0)
        self._perm_layout.setSpacing(4)
        v.addWidget(self._perm_rows_host)
        v.addStretch(1)
        return page

    def _logs_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        self._logs = QPlainTextEdit()
        self._logs.setReadOnly(True)
        self._logs.setStyleSheet(
            f"QPlainTextEdit{{background:{_qcolor(_t.BG_DEEP, 220)};color:{TEXT_MID};"
            f"font:500 8pt '{FONT_SMALL}';border:1px solid {_rgba(BORDER, 80)};"
            f"border-radius:{RADIUS_SM}px;padding:6px;}}")
        v.addWidget(self._logs, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: self._logs.setPlainText(""))
        row.addWidget(clear)
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy_logs)
        row.addWidget(copy)
        v.addLayout(row)
        return page

    # ── refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        row = self._row()
        color = _STATE_COLORS.get(str(row.get("state") or ""), TEXT_MID)
        self._head_state.setText(f"●  {row.get('state_label') or row.get('state')}")
        self._head_state.setStyleSheet(
            f"color:{color};font:700 9pt '{FONT_UI}';background:transparent;")
        self._overview.setText(self._overview_text(row))
        self._refresh_tools(row)
        self._resources_list.setText(self._resource_text(row))
        self._prompts_list.setText(self._prompt_text(row))
        self._refresh_permissions(row)
        self._append_logs(row)
        self._drain_pending()

    def _drain_pending(self) -> None:
        """Show the outcome of a test or a tool run once its thread finished."""
        result = getattr(self, "_test_result", None)
        if result:
            self._test_result = None
            if result.get("ok"):
                self._run_status.setText(
                    f"✓ {result.get('detail')}  —  {result.get('tools', 0)} tool(s)")
            else:
                self._run_status.setText(f"✕ {result.get('detail')}")
            return
        run = getattr(self, "_run_result", None)
        if run is not None:
            self._run_result = None
            if run.ok:
                self._run_status.setText(f"✓ completed in {run.elapsed_s}s")
            else:
                self._run_status.setText(f"✕ failed in {run.elapsed_s}s")
            self._run_output.setPlainText(run.text or run.error or "")

    def _overview_text(self, row: dict) -> str:
        lines = [
            f"Status        {row.get('state_label') or row.get('state')}",
            f"Enabled       {'yes' if row.get('enabled') else 'no'}",
            f"Transport     {str(row.get('transport', 'stdio')).upper()}",
        ]
        if row.get("url"):
            lines.append(f"URL           {row['url']}")
        if row.get("protocol"):
            lines.append(f"Protocol      {row['protocol']} ({row.get('era', '?')})")
        server = row.get("server_info") or {}
        if server.get("name"):
            lines.append(f"Server        {server.get('name')} "
                         f"{server.get('version', '')}".rstrip())
        lines.append(f"Tools         {row.get('tool_count', 0)}")
        lines.append(f"Resources     {row.get('resource_count', 0)}")
        lines.append(f"Prompts       {row.get('prompt_count', 0)}")
        if not row.get("authenticated", True):
            lines.append("Authentication  ⚠ needs attention")
        if row.get("error"):
            lines.append(f"Last error    {row['error']}")
        return "\n".join(lines)

    def _refresh_tools(self, row: dict) -> None:
        spec = _spec_from_row(row)
        lines = []
        for name in row.get("tools") or []:
            safety = spec.safety_of(name, "read")
            exposed = spec.tool_exposed(name, safety)
            label = {"read": "Read", "write": "Write", "destructive": "Destructive"}[safety]
            mark = "ON" if exposed else "OFF"
            lines.append(f"{'✓' if exposed else '✕'}  {name:<34} {label:<12} {mark}")
        self._tools_list.setText("\n".join(lines) or
                                 "This server exposes no tools.")

        self._tool_schema_map = {
            item["name"]: item
            for item in (row.get("tool_details") or [])
            if isinstance(item, dict) and item.get("name")
        }

        current = self._run_tool.currentText()
        names = list(row.get("tools") or [])
        if names != [self._run_tool.itemText(i) for i in range(self._run_tool.count())]:
            self._run_tool.blockSignals(True)
            self._run_tool.clear()
            self._run_tool.addItems(names)
            self._run_tool.blockSignals(False)
            if names:
                self._on_tool_selected(self._run_tool.currentText())
        elif current and current in self._tool_schema_map:
            desc = self._tool_schema_map[current].get("description") or ""
            if not self._tool_desc_lbl.text() and desc:
                self._tool_desc_lbl.setText(desc)

    def _on_tool_selected(self, tool_name: str) -> None:
        tool_name = str(tool_name or "").strip()
        if not tool_name:
            self._tool_desc_lbl.setText("")
            return
        details = self._tool_schema_map.get(tool_name) or {}
        desc = details.get("description") or ""
        self._tool_desc_lbl.setText(desc)
        cur_text = self._run_args.toPlainText().strip()
        if not cur_text or cur_text in ("{}", '{\n  "owner": "octocat"\n}'):
            self._reset_sample_args()

    def _reset_sample_args(self) -> None:
        tool_name = self._run_tool.currentText().strip()
        details = self._tool_schema_map.get(tool_name) or {}
        schema = details.get("input_schema") or {}
        sample = _generate_sample_from_schema(schema)
        self._run_args.setPlainText(json.dumps(sample, indent=2))

    def _format_json_args(self) -> None:
        text = self._run_args.toPlainText().strip()
        if not text:
            return
        try:
            val = json.loads(text)
            self._run_args.setPlainText(json.dumps(val, indent=2))
            self._run_status.setText("JSON formatted ✓")
        except Exception as exc:
            self._run_status.setText(f"Invalid JSON: {exc}")

    def _resource_text(self, row: dict) -> str:
        tools = row.get("resources") or []
        if not tools:
            return ("This server exposes no resources."
                    if row.get("state") == "connected"
                    else "Not connected.")
        return "\n".join(f"•  {str(item)[:200]}" for item in tools)

    def _prompt_text(self, row: dict) -> str:
        prompts = row.get("prompts") or []
        if not prompts:
            return ("This server exposes no prompts."
                    if row.get("state") == "connected"
                    else "Not connected.")
        return "\n".join(f"•  {str(item)[:200]}" for item in prompts)

    def _refresh_permissions(self, row: dict) -> None:
        while self._perm_layout.count():
            item = self._perm_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        spec = _spec_from_row(row)
        for name in row.get("tools") or []:
            safety = spec.safety_of(name, "read")
            box = QCheckBox(f"{name}   ·   {safety}")
            box.setChecked(spec.tool_exposed(name, safety))
            if safety == "destructive":
                box.setToolTip("A destructive tool is hidden until you enable it "
                               "here by name.")
            box.stateChanged.connect(
                lambda _state, tool=name, level=safety, b=box:
                self._set_tool_permission(tool, level, b.isChecked()))
            self._perm_layout.addWidget(box)

    def _append_logs(self, row: dict) -> None:
        lines = row.get("log") or []
        if len(lines) == self._log_seen:
            return
        self._logs.setPlainText("\n".join(lines))
        self._log_seen = len(lines)
        self._logs.verticalScrollBar().setValue(
            self._logs.verticalScrollBar().maximum())

    def _copy_logs(self) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._logs.toPlainText())

    # ── actions ───────────────────────────────────────────────────────────────

    def _set_tool_permission(self, tool: str, safety: str, enabled: bool) -> None:
        runtime = _manager()._runtime(self._name)
        if runtime is None:
            return
        perms = runtime.spec.permissions
        if safety == "destructive":
            allowed = set(perms.get("enabled_tools") or [])
            (allowed.add(tool) if enabled else allowed.discard(tool))
            perms["enabled_tools"] = sorted(allowed)
        else:
            disabled = set(perms.get("disabled_tools") or [])
            (disabled.discard(tool) if enabled else disabled.add(tool))
            perms["disabled_tools"] = sorted(disabled)
        _manager()._persist()
        threading.Thread(target=_manager()._publish, name="mcp-republish",
                         daemon=True).start()

    def _refresh_caps(self) -> None:
        self._run_status.setText("Refreshing capabilities…")
        threading.Thread(target=_manager().refresh_server, args=(self._name,),
                         name="mcp-manage-refresh", daemon=True).start()

    def _restart(self) -> None:
        self._run_status.setText("Restarting…")
        threading.Thread(target=_manager().restart_server, args=(self._name,),
                         name="mcp-manage-restart", daemon=True).start()

    def _test(self) -> None:
        spec = _manager().spec_named(self._name)
        if spec is None:
            return
        self._run_status.setText("Testing the connection…")
        self._test_result = None

        def _work():
            from mcp_host.manager import test_connection
            try:
                self._test_result = test_connection(spec)
            except Exception as exc:
                self._test_result = {"ok": False, "detail": str(exc)}
        threading.Thread(target=_work, name=f"mcp-test-{self._name}",
                         daemon=True).start()

    def _edit(self) -> None:
        self._panel.open_editor(self._name)

    def _remove(self) -> None:
        """Disconnect this server and delete its saved configuration.

        This is the 'Remove Server' button's real handler. Its absence made
        ``self._remove`` raise AttributeError while the dialog was being built,
        so every Manage window crashed on open and the auto-heal sentry kept
        raising repair cards for it.
        """
        name = str(getattr(self, "_name", "") or "").strip()
        if not name:
            return
        answer = QMessageBox.question(
            self, "Remove MCP server",
            f"Remove '{name}'?\n\nThe server is disconnected, its saved "
            "configuration is deleted, and any credentials stored for it are "
            "removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = _manager().remove_server(name)
        except Exception as exc:
            QMessageBox.warning(self, "Remove MCP server",
                                f"The server could not be removed:\n{exc}")
            return
        if not result.get("ok"):
            QMessageBox.warning(
                self, "Remove MCP server",
                str(result.get("detail") or "The server could not be removed."))
            return
        try:
            self._panel._status.setText(
                str(result.get("detail") or f"{name} was removed"))
        except Exception:
            pass
        self.accept()

    def _run_test(self) -> None:
        tool = self._run_tool.currentText().strip()
        if not tool:
            return
        raw = self._run_args.toPlainText().strip() or "{}"
        try:
            arguments = json.loads(raw)
        except Exception as exc:
            self._run_output.setPlainText(f"The arguments are not valid JSON: {exc}")
            return
        self._run_status.setText(f"Running {tool}…")
        self._run_output.setPlainText("")
        self._test_result = None

        def _work():
            # Find the real tool name after the namespace prefix the manager uses
            # internally; the combo already holds the server's own name.
            self._run_result = _manager().call_tool(self._name, tool, arguments)
        threading.Thread(target=_work, name="mcp-run-tool", daemon=True).start()

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)
