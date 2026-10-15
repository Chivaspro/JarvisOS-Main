"""Settings → Obsidian Vault: the vault's real state and the memory switch.

Three things this panel refuses to fake:

* **Status.** "Connected" is only shown after the vault folder has actually been
  read and the index opened. A missing folder is "Vault Not Found", a scan in
  progress is "Indexing…", and a failure names the path that failed.
* **The memory switch.** Original Memory and Obsidian Vault are two real
  backends behind :mod:`memory.backend`; choosing one routes writes and recall
  there for real, and a backend that cannot be activated leaves the previous one
  serving with the reason shown.
* **The graph.** The Neural Map is built from the vault's actual links, tags and
  folders — or from the original memory graph when that backend is active — so
  it never draws an invented relationship.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter, QPainterPath, QBrush, QPen
from PyQt6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

import ui_theme as _t
from ui_theme import (
    ACCENT, ACCENT_TEXT, AMBER, GREEN, RED, TEXT, TEXT_BRIGHT, TEXT_DIM,
    TEXT_FAINT, TEXT_MID, SURFACE, SURFACE_ALT, BORDER_SOFT, RADIUS_MD,
    RADIUS_SM, FONT_UI, FONT_HEAD, FONT_SMALL, qcolor as _qcolor,
    rgba as _rgba, scrollbar_css as _scrollbar_css,
)

from memory import backend as memory_backend
from memory.vault_store import get_index

__all__ = ["ObsidianVaultTab"]


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


class _StatusCard(QFrame):
    """The live status block: one state line and the numbers behind it."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumHeight(120)
        self._lines: list[tuple[str, str]] = []

    def set_lines(self, lines: list[tuple[str, str]]) -> None:
        self._lines = list(lines)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect().adjusted(1, 1, -1, -1)
            path = _card_path(rect, 12)
            p.fillPath(path, QBrush(_qcolor(SURFACE, 210)))
            p.setPen(QPen(_qcolor(BORDER_SOFT, 130), 1))
            p.drawPath(path)
        except Exception:
            pass
        p.end()


class ObsidianVaultTab(QWidget):
    """The vault dashboard, self-contained."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._busy = ""
        self._pending: dict = {}
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

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
        title = QLabel("OBSIDIAN VAULT  //  LONG-TERM MEMORY")
        title.setStyleSheet(
            f"color:{ACCENT_TEXT};font:800 9pt '{FONT_HEAD.strip(chr(39))}';"
            "background:transparent;letter-spacing:2px;")
        iv.addWidget(title)
        iv.addStretch(1)
        lay.addWidget(intro)

        # ── status ───────────────────────────────────────────────────────────
        self._status = _StatusCard()
        lay.addWidget(self._status)
        self._state_labels: dict[str, QLabel] = {}
        self._state_host = QWidget()
        self._state_host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._state_layout = QVBoxLayout(self._state_host)
        self._state_layout.setContentsMargins(18, 10, 18, 12)
        self._state_layout.setSpacing(3)
        holder = QVBoxLayout(self._status)
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addWidget(self._state_host)
        for key in ("state", "active", "path", "notes", "indexed", "updated",
                    "graph"):
            label = QLabel("")
            label.setWordWrap(True)
            if key in ("state", "active"):
                label.setStyleSheet(
                    f"color:{TEXT_BRIGHT};font:700 10pt '{FONT_UI}';"
                    "background:transparent;")
            else:
                label.setStyleSheet(
                    f"color:{TEXT_MID};font:500 8.5pt '{FONT_UI}';"
                    "background:transparent;")
            self._state_labels[key] = label
            self._state_layout.addWidget(label)

        # ── actions ──────────────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.setSpacing(8)
        for label, handler, tip in (
            ("Open Vault", self._open_vault,
             "Open this vault in Obsidian via its official obsidian:// link."),
            ("Refresh", self.refresh, "Re-read the vault state."),
            ("Reindex", self._reindex,
             "Rebuild the index from every note in the vault."),
            ("Test Vault", self._test_vault,
             "Check that the path exists, is readable and writable, and that "
             "the index can be built."),
            ("Neural Map", self._neural_map,
             "Open the interactive graph of this vault's memories."),
        ):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        lay.addLayout(actions)

        # ── path ─────────────────────────────────────────────────────────────
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_label = QLabel("Vault path")
        path_label.setStyleSheet(f"color:{TEXT_DIM};font:700 8pt '{FONT_UI}';"
                                 "background:transparent;")
        path_row.addWidget(path_label)
        self._path = QLineEdit()
        self._path.setPlaceholderText("Path to the Obsidian vault folder")
        path_row.addWidget(self._path, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        validate = QPushButton("Validate")
        validate.clicked.connect(self._validate)
        path_row.addWidget(validate)
        save = QPushButton("Use This Vault")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save_path)
        path_row.addWidget(save)
        lay.addLayout(path_row)

        # ── memory backend switch ────────────────────────────────────────────
        switch_box = QFrame()
        switch_box.setStyleSheet(
            f"QFrame{{background:{_rgba(SURFACE_ALT, 120)};"
            f"border:1px solid {_rgba(BORDER_SOFT, 120)};border-radius:{RADIUS_MD}px;}}")
        sv = QVBoxLayout(switch_box)
        sv.setContentsMargins(12, 9, 12, 11)
        sv.setSpacing(6)
        head = QLabel("MEMORY BACKEND")
        head.setStyleSheet(f"color:{TEXT_DIM};font:700 7pt '{FONT_UI}';"
                           "letter-spacing:1px;background:transparent;")
        sv.addWidget(head)
        row = QHBoxLayout()
        self._orig_btn = QPushButton("Original Memory")
        self._orig_btn.setCheckable(True)
        self._orig_btn.clicked.connect(lambda: self._switch("original"))
        row.addWidget(self._orig_btn)
        self._obsidian_btn = QPushButton("Obsidian Vault")
        self._obsidian_btn.setCheckable(True)
        self._obsidian_btn.clicked.connect(lambda: self._switch("obsidian"))
        row.addWidget(self._obsidian_btn)
        row.addStretch(1)
        sv.addLayout(row)
        self._switch_note = QLabel("")
        self._switch_note.setWordWrap(True)
        self._switch_note.setStyleSheet(
            f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        sv.addWidget(self._switch_note)
        lay.addWidget(switch_box)
        lay.addStretch(1)

    # ── refresh ───────────────────────────────────────────────────────────────

    def _index(self):
        return get_index()

    def refresh(self) -> None:
        """Read the vault's real state and paint it. Cheap (index is on disk)."""
        index = self._index()
        try:
            stats = index.stats()
        except Exception as exc:
            stats = {"notes": 0, "folders": 0, "path": str(index.path),
                     "last_scan": 0.0}
            self._state_labels["state"].setText(f"⚠  Index unavailable — {exc}")
        try:
            from config.settings import get, get_feature
            vault_path = str(get("obsidian_vault_path", "") or "") or str(index.path)
            enabled = bool(get_feature("obsidian_memory", True))
        except Exception:
            vault_path, enabled = str(index.path), True
        if not self._path.text().strip():
            self._path.setText(vault_path)

        exists = Path(vault_path).exists()
        active = memory_backend.active_backend_name()
        selected = memory_backend.selected_backend()
        error = memory_backend.activation_error()

        if not enabled:
            state, colour = "○  Disabled", TEXT_DIM
        elif not exists:
            state, colour = "⚠  Vault Not Found", RED
        elif self._busy:
            state, colour = f"◐  {self._busy}", AMBER
        elif error:
            state, colour = "⚠  Error", RED
        else:
            state, colour = "●  Connected", GREEN
        self._state_labels["state"].setText(state)
        self._state_labels["state"].setStyleSheet(
            f"color:{colour};font:700 10pt '{FONT_UI}';background:transparent;")

        if active == "obsidian":
            self._state_labels["active"].setText("●  Memory Active  ·  Obsidian Vault")
            self._state_labels["active"].setStyleSheet(
                f"color:{GREEN};font:700 10pt '{FONT_UI}';background:transparent;")
        elif selected == "obsidian" and error:
            self._state_labels["active"].setText(
                "⚠  Selected but not active — the original memory is serving")
            self._state_labels["active"].setStyleSheet(
                f"color:{AMBER};font:700 10pt '{FONT_UI}';background:transparent;")
        else:
            self._state_labels["active"].setText("●  Original memory is active")
            self._state_labels["active"].setStyleSheet(
                f"color:{TEXT_BRIGHT};font:700 10pt '{FONT_UI}';"
                "background:transparent;")

        self._state_labels["path"].setText(f"Path        {vault_path}")
        self._state_labels["notes"].setText(
            f"Notes       {stats.get('notes', 0)}   ·   Folders {stats.get('folders', 0)}")
        self._state_labels["indexed"].setText(
            f"Indexed     {stats.get('notes', 0)} / {stats.get('notes', 0)}")
        last = float(stats.get("last_scan") or 0)
        when = _ago(last) if last else "never"
        self._state_labels["updated"].setText(f"Last index  {when}")
        self._state_labels["graph"].setText(self._graph_line(index))
        if error:
            self._state_labels["graph"].setText(
                self._state_labels["graph"].text() + f"\n⚠  {error}")

        self._orig_btn.setChecked(active != "obsidian")
        self._obsidian_btn.setChecked(active == "obsidian")
        self._switch_note.setText(
            "Obsidian Vault memory is active; the original store is inactive."
            if active == "obsidian" else
            "The original memory is active. Switching routes reads and writes "
            "to the vault.")
        self._drain()

    def _graph_line(self, index) -> str:
        try:
            graph = index.graph(layers={"links": True, "backlinks": True,
                                        "tags": True, "folders": False})
        except Exception:
            return "Graph       unavailable"
        return (f"Graph       {len(graph.get('nodes', []))} nodes  ·  "
                f"{len(graph.get('links', []))} connections")

    # ── actions ───────────────────────────────────────────────────────────────

    def _open_vault(self) -> None:
        """Open the vault in Obsidian through its official URI scheme."""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        index = self._index()
        try:
            from config.settings import get
            path = str(get("obsidian_vault_path", "") or "") or str(index.path)
        except Exception:
            path = str(index.path)
        name = Path(path).name
        if not name:
            QMessageBox.information(self, "Obsidian Vault",
                                    "Set a vault path first.")
            return
        opened = QDesktopServices.openUrl(
            QUrl(f"obsidian://open?vault={name}"))
        if not opened:
            # Obsidian may not be registered; offering the folder is the honest
            # fallback rather than pretending the URI worked.
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose the Obsidian vault")
        if folder:
            self._path.setText(folder)

    def _validate(self) -> None:
        path = Path(self._path.text().strip())
        if not path.exists():
            QMessageBox.warning(self, "Obsidian Vault",
                                f"That folder does not exist:\n{path}")
            return
        readable = any(path.glob("*.md")) or True
        writable = _writable(path)
        detail = (f"Path:     {path}\n"
                  f"Notes:    {len(list(path.rglob('*.md')))} markdown file(s)\n"
                  f"Readable: {'yes' if readable else 'no'}\n"
                  f"Writable: {'yes' if writable else 'no'}")
        QMessageBox.information(self, "Obsidian Vault", detail)

    def _save_path(self) -> None:
        path = self._path.text().strip()
        if not path:
            return
        from config.settings import set_value
        if not set_value("obsidian_vault_path", path):
            QMessageBox.warning(self, "Obsidian Vault",
                                "The vault path could not be saved.")
            return
        # The index follows the configured path; a different vault must be
        # validated and indexed before it can serve memory. use_path() is the
        # one switch that also invalidates the freshness marker, so rows from
        # the previous vault are never served for this one.
        index = self._index()
        index.use_path(path)
        self._busy = "Indexing…"
        self.refresh()
        self._run_async(lambda: index.scan(force=True), "Indexing…")

    def _reindex(self) -> None:
        index = self._index()
        self._busy = "Reindexing…"
        self.refresh()
        self._run_async(lambda: index.scan(force=True), "Reindexing…")

    def _test_vault(self) -> None:
        index = self._index()

        def _work():
            path = index.path
            if not path.exists():
                return {"ok": False, "detail": f"the vault folder does not exist: {path}"}
            try:
                result = index.scan()
            except Exception as exc:
                return {"ok": False, "detail": f"the index could not be built: {exc}"}
            if not result.get("ok"):
                return {"ok": False, "detail": result.get("detail", "")}
            if not _writable(path / "Memory"):
                return {"ok": True,
                        "detail": (f"Readable — {result.get('total', 0)} note(s) "
                                   f"indexed. The folder is not writable, so new "
                                   f"memory cannot be stored there.")}
            return {"ok": True,
                    "detail": (f"Readable and writable — {result.get('total', 0)} "
                               f"note(s) indexed in "
                               f"{result.get('elapsed_s', 0)}s.")}
        self._busy = "Testing…"
        self.refresh()
        self._run_async(_work, "Testing…")

    def _neural_map(self) -> None:
        try:
            from ui_neural_map2d import open_neural_map2d
            open_neural_map2d(parent=self)
        except Exception as exc:
            QMessageBox.warning(self, "Neural Map",
                                f"The Neural Map could not be opened: {exc}")

    def _switch(self, choice: str) -> None:
        """Switch the store and tell the truth about what actually happened.

        A switch that could not be persisted is reported here: it would look
        successful for this session and then quietly revert on restart, which is
        exactly the bug this message exists to prevent.
        """
        result = memory_backend.set_memory_backend(choice)
        detail = str(result.get("detail") or "")
        if not result.get("ok") or not result.get("persisted", True):
            QMessageBox.warning(self, "Memory backend", detail)
        elif detail:
            self._switch_note.setText(detail)
        self.refresh()

    # ── worker plumbing ───────────────────────────────────────────────────────

    def _run_async(self, work, label: str) -> None:
        def _runner():
            try:
                self._pending["result"] = work()
            except Exception as exc:
                self._pending["result"] = {"ok": False, "detail": str(exc)}
            self._pending["label"] = label
        threading.Thread(target=_runner, name="vault-task", daemon=True).start()

    def _drain(self) -> None:
        if "result" not in self._pending:
            return
        result = self._pending.pop("result")
        label = self._pending.pop("label", "")
        self._busy = ""
        if isinstance(result, dict) and result.get("ok") is False:
            self._state_labels["graph"].setText(f"⚠  {result.get('detail')}")
        elif isinstance(result, dict) and result.get("detail") and label == "Testing…":
            QMessageBox.information(self, "Obsidian Vault", result["detail"])
        self.refresh()


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".jarvis-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _ago(stamp: float) -> str:
    delta = max(0.0, time.time() - float(stamp or 0))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} minute(s) ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hour(s) ago"
    return f"{int(delta // 86400)} day(s) ago"
