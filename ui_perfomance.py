"""Settings → Performance: real presets over real switches.

Every control here maps to behaviour the application already has — HUD
animations, panel sound effects, the Mind panel's live refresh, the vault
watcher and index cadence, and the Neural Map's node budget, glow work and
simulation tick. A preset applies a fixed, deterministic bundle of those
values, persists through the same guarded config writer as every other
setting, and takes effect without a restart. No preset ever touches memory
storage, vault contents, or the security gates.

The diagnostic scan measures what is actually reachable from this process —
config state, the active memory backend, store sizes, the vault index and a
bounded graph build, plus process CPU/RAM when psutil is installed — and
reports only what it measured.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

import ui_theme as _t
from ui_theme import (
    ACCENT, ACCENT_TEXT, AMBER, GREEN, RED, TEXT_BRIGHT, TEXT_DIM, TEXT_FAINT,
    TEXT_MID, SURFACE, SURFACE_ALT, BORDER_SOFT, RADIUS_MD, FONT_UI,
    FONT_HEAD, rgba as _rgba,
)

__all__ = ["DEFAULTS", "PROFILES", "PROFILE_LABELS", "effective", "resolve",
           "apply_profile", "save_custom", "PerformanceTab",
           "collect_scan", "build_findings", "scan_vault_metrics",
           "graph_metrics"]

DEFAULTS: dict[str, Any] = {
    "profile": "balanced",
    "animations": True,
    "sound_effects": True,
    "audio_reactive": True,
    "world_monitor_refresh": 60,
    "mind_reload_s": 10,
    "vault_watch_s": 6,
    "index_fresh_s": 5,
    "neural_max_nodes": 2500,
    "neural_effects": True,
    "neural_tick_ms": 16,
}

PROFILES: dict[str, dict[str, Any]] = {
    "balanced": {},
    "performance": {
        "animations": False,
        "sound_effects": False,
        "mind_reload_s": 20,
        "vault_watch_s": 12,
        "index_fresh_s": 15,
        "neural_max_nodes": 1200,
        "neural_effects": False,
        "neural_tick_ms": 24,
    },
    "ultra": {
        "animations": False,
        "sound_effects": False,
        "audio_reactive": False,
        "world_monitor_refresh": 300,
        "mind_reload_s": 30,
        "vault_watch_s": 30,
        "index_fresh_s": 60,
        "neural_max_nodes": 400,
        "neural_effects": False,
        "neural_tick_ms": 33,
    },
}

PROFILE_LABELS = {
    "balanced": "Balanced",
    "performance": "Performance",
    "ultra": "Ultra Performance",
    "custom": "Custom",
}

PROFILE_NOTES = {
    "balanced": "Full experience: animations, effects and live refresh at their "
                "default cadence. Memory storage and sync are unchanged.",
    "performance": "Drops HUD animations and panel sounds, slows background "
                   "refresh, and trims the Neural Map's glow work and node "
                   "budget. Memory still persists and syncs.",
    "ultra": "Minimum background work: slowest refresh cadence, smallest map "
             "budget, no animations, sounds or audio-reactive paint. Memory "
             "persistence, data integrity and security are never reduced.",
    "custom": "Your own mix — the profile follows the controls below.",
}

_LIMITS = {
    "world_monitor_refresh": (15, 600),
    "mind_reload_s": (5, 120),
    "vault_watch_s": (2, 120),
    "index_fresh_s": (2, 300),
    "neural_max_nodes": (50, 5000),
    "neural_tick_ms": (16, 66),
}


def _clamp(key: str, value: Any) -> Any:
    lo_hi = _LIMITS.get(key)
    if lo_hi is None:
        return value
    try:
        return max(lo_hi[0], min(lo_hi[1], int(float(value))))
    except Exception:
        return DEFAULTS.get(key)


def _stored() -> dict:
    try:
        from config.settings import get
        raw = get("performance", None)
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def effective() -> dict:
    """The live performance settings: defaults merged with what is stored,
    every bounded value clamped so a hand-edited config cannot break a timer."""
    merged = dict(DEFAULTS)
    merged.update(_stored())
    for key in merged:
        if key in _LIMITS:
            merged[key] = _clamp(key, merged[key])
    if str(merged.get("profile")) not in PROFILE_LABELS:
        merged["profile"] = "balanced"
    return merged


def resolve(profile: str) -> dict:
    """The deterministic settings bundle for one named profile."""
    name = str(profile or "balanced").strip().lower()
    if name not in PROFILES:
        name = "balanced"
    settings = dict(DEFAULTS)
    settings.update(PROFILES[name])
    settings["profile"] = name
    return settings


def _persist(settings: dict) -> bool:
    try:
        from config.settings import set_value, set_feature
        ok = bool(set_value("performance", dict(settings)))
        set_feature("smooth_animations", bool(settings.get("animations", True)))
        set_feature("enable_sfx", bool(settings.get("sound_effects", True)))
        set_feature("audio_reactive", bool(settings.get("audio_reactive", True)))
        return ok
    except Exception:
        return False


def apply_profile(profile: str) -> dict:
    """Apply + persist one named profile; returns the settings now in force."""
    settings = resolve(profile)
    _persist(settings)
    return settings


def save_custom(settings: dict) -> dict:
    """Persist a user-tuned mix as the Custom profile."""
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in dict(settings).items() if k in DEFAULTS})
    for key in merged:
        if key in _LIMITS:
            merged[key] = _clamp(key, merged[key])
    merged["profile"] = "custom"
    _persist(merged)
    return merged


# ── diagnostics ──────────────────────────────────────────────────────────────

def scan_vault_metrics(index=None, path: str | Path | None = None) -> dict:
    """Real vault-index numbers: existence, note count, last scan age, and the
    measured cost of one incremental scan. Never raises."""
    out: dict[str, Any] = {"ok": False, "configured": "", "exists": False,
                           "notes": 0, "folders": 0, "last_scan_age_s": -1.0,
                           "scan_elapsed_s": -1.0, "detail": ""}
    try:
        if index is None:
            from memory.vault_store import VaultIndex, get_index
            if path is not None:
                import tempfile
                db = Path(tempfile.mkdtemp(prefix="jarvis-perf-")) / "scan.db"
                index = VaultIndex(path=str(path), db_path=db)
            else:
                index = get_index()
        out["configured"] = str(index.path)
        out["exists"] = bool(Path(index.path).exists())
        if not out["exists"]:
            out["detail"] = "the vault folder does not exist"
            return out
        started = time.perf_counter()
        result = index.scan()
        out["scan_elapsed_s"] = round(time.perf_counter() - started, 3)
        out["ok"] = bool(result.get("ok"))
        out["detail"] = str(result.get("detail") or "")
        stats = index.stats()
        out["notes"] = int(stats.get("notes") or 0)
        out["folders"] = int(stats.get("folders") or 0)
        last = float(stats.get("last_scan") or 0)
        out["last_scan_age_s"] = round(max(0.0, time.time() - last), 1) if last else -1.0
    except Exception as exc:
        out["detail"] = str(exc)
    return out


def graph_metrics() -> dict:
    """Node/link counts and build cost of one bounded graph build."""
    out: dict[str, Any] = {"ok": False, "nodes": 0, "links": 0,
                           "elapsed_s": -1.0, "source": "", "detail": ""}
    try:
        from ui_neural_map import build_graph
        started = time.perf_counter()
        graph = build_graph()
        out["elapsed_s"] = round(time.perf_counter() - started, 3)
        out["nodes"] = len(graph.get("nodes") or [])
        out["links"] = len(graph.get("links") or [])
        out["source"] = str((graph.get("source") or {}).get("kind") or "")
        out["detail"] = str(graph.get("error") or "")
        out["ok"] = not out["detail"]
    except Exception as exc:
        out["detail"] = str(exc)
    return out


def _process_metrics() -> dict:
    out = {"available": False, "cpu_percent": -1.0, "rss_mb": -1.0}
    try:
        import psutil
        proc = psutil.Process()
        try:
            out["cpu_percent"] = round(float(proc.cpu_percent(interval=0.3)), 1)
        except Exception:
            pass
        out["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 1)
        out["available"] = True
    except Exception:
        pass
    return out


def build_findings(metrics: dict) -> list[dict]:
    """Turn measured metrics into findings. Each one names its evidence; a
    hypothesis is labelled as one. Nothing here is invented."""
    findings: list[dict] = []
    perf = dict(metrics.get("performance_settings") or {})
    vault = dict(metrics.get("vault") or {})
    backend = dict(metrics.get("backend") or {})
    graph = dict(metrics.get("graph") or {})
    process = dict(metrics.get("process") or {})

    if vault.get("configured") and not vault.get("exists"):
        findings.append({
            "issue": "The configured Obsidian vault folder does not exist.",
            "evidence": f"path: {vault.get('configured')}",
            "impact": "Vault memory and the vault Neural Map cannot read or index notes.",
            "confidence": "measured",
            "action": "Open Settings ▸ OBSIDIAN VAULT and point the vault path at a real folder.",
        })
    if vault.get("exists") and not vault.get("ok") and vault.get("detail"):
        findings.append({
            "issue": "The vault index could not be built.",
            "evidence": str(vault.get("detail"))[:200],
            "impact": "Memory search and the Neural Map fall back to the previous state.",
            "confidence": "measured",
            "action": "Run TEST VAULT on the OBSIDIAN VAULT tab to see the failing path.",
        })
    if vault.get("exists") and float(vault.get("scan_elapsed_s") or 0) > 1.5:
        findings.append({
            "issue": "One vault index scan took "
                     f"{vault.get('scan_elapsed_s')}s.",
            "evidence": f"{vault.get('notes')} note(s) indexed",
            "impact": "Anything that scans on the UI thread can feel like a stall.",
            "confidence": "measured",
            "action": "Apply the Performance or Ultra Performance profile to slow "
                      "background refresh and indexing cadence.",
        })
    if vault.get("exists") and int(vault.get("notes") or 0) > int(
            perf.get("neural_max_nodes") or 2500):
        findings.append({
            "issue": "The vault holds more notes than the Neural Map's node budget.",
            "evidence": f"{vault.get('notes')} notes vs budget "
                        f"{perf.get('neural_max_nodes')}",
            "impact": "The map draws the newest slice; omitted notes stay reachable "
                      "through the Mind panel search.",
            "confidence": "measured",
            "action": "Raise the Neural Map node limit in PERFORMANCE if the machine "
                      "has headroom, or keep the current budget for speed.",
        })
    if backend.get("selected") == "obsidian" and backend.get("error"):
        findings.append({
            "issue": "Obsidian Vault is selected but not serving memory.",
            "evidence": str(backend.get("error"))[:200],
            "impact": "The original memory is serving instead; the vault is not receiving writes.",
            "confidence": "measured",
            "action": "Fix the vault path on the OBSIDIAN VAULT tab, then switch the backend again.",
        })
    if graph.get("elapsed_s") is not None and float(graph.get("elapsed_s") or 0) > 2.0:
        findings.append({
            "issue": f"Building the Neural Map took {graph.get('elapsed_s')}s.",
            "evidence": f"{graph.get('nodes')} nodes · {graph.get('links')} links",
            "impact": "Opening or syncing the map delays its first draw.",
            "confidence": "measured",
            "action": "A lower performance profile reduces the node budget and glow work.",
        })
    if process.get("available") and float(process.get("rss_mb") or 0) > 1500:
        findings.append({
            "issue": "The J.A.R.V.I.S. process is using "
                     f"{process.get('rss_mb')} MB of RAM.",
            "evidence": "process memory measured with psutil",
            "impact": "High memory can lead to slowdowns or an OS-level kill.",
            "confidence": "measured",
            "action": "Restart J.A.R.V.I.S.; if it grows back, check the vision/camera "
                      "features that hold large frames.",
        })
    if not findings:
        findings.append({
            "issue": "No performance problems detected in the measured areas.",
            "evidence": "config, memory backend, vault index, graph build"
                        + (", process" if process.get("available") else ""),
            "impact": "—",
            "confidence": "measured",
            "action": "No change needed.",
        })
    return findings


def collect_scan(cancel: threading.Event | None = None,
                 progress: Callable[[int, str], None] | None = None) -> dict:
    """The full scan. Bounded, cancellable, and honest about what it measured."""
    def _report(pct: int, label: str) -> None:
        if progress is not None:
            try:
                progress(int(pct), str(label))
            except Exception:
                pass

    def _cancelled() -> bool:
        return bool(cancel is not None and cancel.is_set())

    metrics: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _report(5, "Reading performance settings…")
    metrics["performance_settings"] = effective()
    if _cancelled():
        return {"cancelled": True, "metrics": metrics, "findings": []}

    _report(15, "Checking the config store…")
    try:
        from config.settings import API_KEYS_FILE
        metrics["config"] = {"present": bool(API_KEYS_FILE.exists()),
                             "path": str(API_KEYS_FILE)}
    except Exception as exc:
        metrics["config"] = {"present": False, "path": "", "error": str(exc)}
    if _cancelled():
        return {"cancelled": True, "metrics": metrics, "findings": []}

    _report(30, "Checking the memory backend…")
    try:
        from memory import backend as memory_backend
        metrics["backend"] = {
            "active": memory_backend.active_backend_name(),
            "selected": memory_backend.selected_backend(),
            "error": memory_backend.activation_error(),
        }
    except Exception as exc:
        metrics["backend"] = {"active": "", "selected": "", "error": str(exc)}
    try:
        from memory.memory_manager import all_entries_for_ui
        metrics["local_rows"] = len(all_entries_for_ui())
    except Exception:
        metrics["local_rows"] = -1
    if _cancelled():
        return {"cancelled": True, "metrics": metrics, "findings": []}

    _report(50, "Scanning the vault index…")
    try:
        from config.settings import get
        configured = str(get("obsidian_vault_path", "") or "").strip()
    except Exception:
        configured = ""
    metrics["vault"] = scan_vault_metrics(path=configured or None)
    if _cancelled():
        return {"cancelled": True, "metrics": metrics, "findings": []}

    _report(75, "Building the Neural Map once…")
    metrics["graph"] = graph_metrics()
    if _cancelled():
        return {"cancelled": True, "metrics": metrics, "findings": []}

    _report(90, "Sampling the process…")
    metrics["process"] = _process_metrics()
    _report(100, "Done.")
    return {"cancelled": False, "metrics": metrics,
            "findings": build_findings(metrics)}


# ── the settings tab ─────────────────────────────────────────────────────────

_CONTROL_DEFS: tuple[tuple[str, str, str, str], ...] = (
    ("animations", "UI animations", "checkbox",
     "Panel fades and slide transitions. Off: panels appear instantly."),
    ("sound_effects", "Panel sound effects", "checkbox",
     "The short UI stings on open/close. Off: silence."),
    ("audio_reactive", "Audio-reactive reactor", "checkbox",
     "The arc reactor pulses with your voice. Off: static paint, less per-frame work."),
    ("neural_effects", "Neural Map glow effects", "checkbox",
     "Per-node glow gradients. Off: flat nodes — the map stays fully interactive."),
    ("mind_reload_s", "Mind panel live refresh", "seconds",
     "How often the Mind panel re-reads memory while it is open."),
    ("vault_watch_s", "Vault watcher cadence", "seconds",
     "How often the background watcher checks the vault for changes made in Obsidian."),
    ("index_fresh_s", "Index freshness window", "seconds",
     "How long the vault index may be used before it re-checks the folder."),
    ("world_monitor_refresh", "World Monitor refresh", "seconds",
     "Headline and telemetry refresh interval."),
    ("neural_max_nodes", "Neural Map node budget", "nodes",
     "Most notes the map draws. Omitted notes stay searchable in the Mind panel."),
    ("neural_tick_ms", "Neural Map tick interval", "ms",
     "Simulation frame time. Higher = less CPU while the layout settles."),
)


class PerformanceTab(QWidget):
    """The Performance section of the Control Center."""

    def __init__(self, window=None, parent=None) -> None:
        super().__init__(parent)
        self._window = window
        self._programmatic = False
        self._settings = effective()
        self._scan_thread: threading.Thread | None = None
        self._scan_cancel: threading.Event | None = None
        self._scan_queue: list[dict] = []
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._build()
        self._load_from(self._settings)
        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._drain_scan)
        self._poll.start()

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        intro = QLabel("PERFORMANCE  //  PRESETS · REAL TOGGLES · DIAGNOSTICS")
        intro.setStyleSheet(
            f"color:{ACCENT_TEXT};font:800 9pt '{FONT_HEAD.strip(chr(39))}';"
            "background:transparent;letter-spacing:2px;padding:2px;")
        lay.addWidget(intro)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        preset_label = QLabel("Profile")
        preset_label.setStyleSheet(f"color:{TEXT_DIM};font:700 8pt '{FONT_UI}';"
                                   "background:transparent;")
        preset_row.addWidget(preset_label)
        self._profile_box = QComboBox()
        for key in ("balanced", "performance", "ultra", "custom"):
            self._profile_box.addItem(PROFILE_LABELS[key], key)
        self._profile_box.setMinimumWidth(220)
        self._profile_box.currentIndexChanged.connect(self._on_profile)
        preset_row.addWidget(self._profile_box)
        preset_row.addStretch(1)
        lay.addLayout(preset_row)

        self._profile_note = QLabel("")
        self._profile_note.setWordWrap(True)
        self._profile_note.setStyleSheet(
            f"color:{TEXT_MID};font:500 8.5pt '{FONT_UI}';background:transparent;")
        lay.addWidget(self._profile_note)

        self._controls: dict[str, Any] = {}
        for key, label, kind, tip in _CONTROL_DEFS:
            row = QHBoxLayout()
            row.setSpacing(8)
            if kind == "checkbox":
                box = QCheckBox(label)
                box.setToolTip(tip)
                box.toggled.connect(lambda _state, k=key: self._on_control(k))
                self._controls[key] = box
                row.addWidget(box)
            else:
                lab = QLabel(label)
                lab.setMinimumWidth(210)
                lab.setToolTip(tip)
                lab.setStyleSheet(f"color:{TEXT_BRIGHT};font:600 8.5pt '{FONT_UI}';"
                                  "background:transparent;")
                spin = QSpinBox()
                lo, hi = _LIMITS[key]
                spin.setRange(int(lo), int(hi))
                spin.setSuffix({"seconds": " s", "nodes": "", "ms": " ms"}[kind])
                spin.setToolTip(tip)
                spin.setMinimumWidth(110)
                spin.valueChanged.connect(lambda _v, k=key: self._on_control(k))
                self._controls[key] = spin
                row.addWidget(lab)
                row.addWidget(spin)
                row.addStretch(1)
            lay.addLayout(row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        restore = QPushButton("RESTORE DEFAULTS")
        restore.setToolTip("Return to the Balanced profile and save it.")
        restore.clicked.connect(lambda: self._apply_and_load("balanced"))
        actions.addWidget(restore)
        self._scan_btn = QPushButton("SCAN PERFORMANCE")
        self._scan_btn.setToolTip(
            "Measure the config store, memory backend, vault index and one graph "
            "build, then report evidence-based findings. Changes nothing.")
        self._scan_btn.clicked.connect(self._start_scan)
        actions.addWidget(self._scan_btn)
        self._scan_cancel_btn = QPushButton("CANCEL")
        self._scan_cancel_btn.setEnabled(False)
        self._scan_cancel_btn.clicked.connect(self._cancel_scan)
        actions.addWidget(self._scan_cancel_btn)
        actions.addStretch(1)
        lay.addLayout(actions)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(6)
        self._progress.setVisible(False)
        lay.addWidget(self._progress)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            f"color:{TEXT_DIM};font:500 8pt '{FONT_UI}';background:transparent;")
        lay.addWidget(self._status)

        self._report = QTextEdit()
        self._report.setReadOnly(True)
        self._report.setPlaceholderText("Run SCAN PERFORMANCE for an evidence-based report.")
        self._report.setMinimumHeight(150)
        self._report.setStyleSheet(
            f"QTextEdit{{background:{_rgba(SURFACE_ALT, 120)};color:{TEXT_BRIGHT};"
            f"border:1px solid {_rgba(BORDER_SOFT, 110)};border-radius:{int(_t.RADIUS_MD)}px;"
            f"font:500 8.5pt '{FONT_UI}';padding:8px;}}")
        lay.addWidget(self._report, 1)

    # ── state ────────────────────────────────────────────────────────────────

    def _load_from(self, settings: dict) -> None:
        self._programmatic = True
        try:
            self._settings = dict(settings)
            index = self._profile_box.findData(str(settings.get("profile", "balanced")))
            self._profile_box.setCurrentIndex(max(0, index))
            self._profile_note.setText(
                PROFILE_NOTES.get(str(settings.get("profile")), PROFILE_NOTES["custom"]))
            for key, widget in self._controls.items():
                value = settings.get(key, DEFAULTS.get(key))
                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                else:
                    widget.setValue(int(value))
        finally:
            self._programmatic = False
        self._sync_feature_checks()

    def _collect_controls(self) -> dict:
        out = dict(self._settings)
        for key, widget in self._controls.items():
            if isinstance(widget, QCheckBox):
                out[key] = bool(widget.isChecked())
            else:
                out[key] = int(widget.value())
        return out

    def _on_profile(self, _index: int) -> None:
        if self._programmatic:
            return
        key = str(self._profile_box.currentData() or "balanced")
        if key == "custom":
            self._profile_note.setText(PROFILE_NOTES["custom"])
            return
        self._apply_and_load(key)

    def _apply_and_load(self, profile: str) -> None:
        settings = apply_profile(profile)
        self._load_from(settings)
        label = PROFILE_LABELS.get(profile, profile).upper()
        if self._persisted(profile):
            self._status.setText(
                f"PROFILE APPLIED — {label} · saved to config/api_keys.json")
        else:
            self._status.setText(
                f"PROFILE APPLIED — {label} · for this session only "
                "(the config file could not be written)")
        self._push_live()

    @staticmethod
    def _persisted(profile: str) -> bool:
        """Read back what is actually stored — a save that silently failed must
        not be reported as saved."""
        try:
            from config.settings import get
            stored = get("performance", {})
            return isinstance(stored, dict) and str(stored.get("profile")) == str(profile)
        except Exception:
            return False

    def _on_control(self, _key: str) -> None:
        if self._programmatic:
            return
        settings = save_custom(self._collect_controls())
        self._settings = settings
        self._programmatic = True
        try:
            index = self._profile_box.findData("custom")
            self._profile_box.setCurrentIndex(max(0, index))
            self._profile_note.setText(PROFILE_NOTES["custom"])
        finally:
            self._programmatic = False
        self._sync_feature_checks()
        if self._persisted("custom"):
            self._status.setText("CUSTOM SETTING SAVED · applied without a restart")
        else:
            self._status.setText(
                "CUSTOM SETTING APPLIED FOR THIS SESSION · the config file "
                "could not be written")
        self._push_live()

    def _push_live(self) -> None:
        """Make the change land in the running application, without a restart.

        The HUD caches its feature flags at startup, so the cached dict is
        updated in place for the three shared flags; the Mind pane's refresh
        timer and the vault watcher's cadence are adjusted on the live owners
        when they exist.
        """
        try:
            from PyQt6.QtWidgets import QApplication
        except Exception:
            return
        anim = bool(self._settings.get("animations", True))
        sfx = bool(self._settings.get("sound_effects", True))
        reactive = bool(self._settings.get("audio_reactive", True))
        interval_ms = int(max(5, int(self._settings.get("mind_reload_s") or 10)) * 1000)
        try:
            widgets = list(QApplication.topLevelWidgets())
        except Exception:
            widgets = []
        for widget in widgets:
            feats = getattr(widget, "_features", None)
            if isinstance(feats, dict):
                feats["smooth_animations"] = anim
                feats["enable_sfx"] = sfx
                feats["audio_reactive"] = reactive
            try:
                for pane in widget.findChildren(QWidget):
                    timer = getattr(pane, "_auto_reload", None)
                    if timer is not None and hasattr(timer, "setInterval"):
                        timer.setInterval(interval_ms)
            except Exception:
                pass
        try:
            from memory import vault_watch
            watcher = getattr(vault_watch, "_WATCHER", None)
            if watcher is not None:
                # The watcher reads its interval every cycle; after the next
                # tick the new cadence governs.
                watcher.interval = max(1.0, float(self._settings.get("vault_watch_s") or 6))
        except Exception:
            pass

    def _sync_feature_checks(self) -> None:
        """Keep the SYSTEM tab's checkboxes for the shared flags in step, so a
        later SAVE CHANGES cannot revert them from a stale widget."""
        checks = getattr(self._window, "_feature_checks", None) or {}
        mapping = {"smooth_animations": "animations",
                   "enable_sfx": "sound_effects",
                   "audio_reactive": "audio_reactive"}
        for flag, key in mapping.items():
            box = checks.get(flag)
            if box is None:
                continue
            try:
                box.blockSignals(True)
                box.setChecked(bool(self._settings.get(key, True)))
            except Exception:
                pass
            finally:
                try:
                    box.blockSignals(False)
                except Exception:
                    pass

    # ── scan ─────────────────────────────────────────────────────────────────

    def _start_scan(self) -> None:
        if self._scan_thread is not None and self._scan_thread.is_alive():
            self._status.setText("A scan is already running — wait for it or press CANCEL.")
            return
        self._scan_cancel = threading.Event()
        self._scan_queue.clear()
        self._progress.setValue(0)
        self._progress.setVisible(True)
        self._scan_btn.setEnabled(False)
        self._scan_cancel_btn.setEnabled(True)
        self._report.setPlainText("Scanning…")
        self._status.setText("SCAN RUNNING — measuring, nothing is changed.")
        cancel = self._scan_cancel
        queue = self._scan_queue

        def _work() -> None:
            try:
                result = collect_scan(
                    cancel=cancel,
                    progress=lambda pct, label: queue.append(
                        {"kind": "progress", "pct": pct, "label": label}))
            except Exception as exc:
                result = {"cancelled": False, "error": str(exc),
                          "metrics": {}, "findings": []}
            queue.append({"kind": "result", "result": result})

        self._scan_thread = threading.Thread(target=_work, name="perf-scan",
                                             daemon=True)
        self._scan_thread.start()

    def _cancel_scan(self) -> None:
        if self._scan_cancel is not None:
            self._scan_cancel.set()
            self._status.setText("Cancelling the scan…")

    def _drain_scan(self) -> None:
        if not self._scan_queue:
            return
        for item in list(self._scan_queue):
            self._scan_queue.remove(item)
            if item.get("kind") == "progress":
                self._progress.setValue(int(item.get("pct") or 0))
                self._status.setText(str(item.get("label") or ""))
            elif item.get("kind") == "result":
                self._finish_scan(dict(item.get("result") or {}))

    def _finish_scan(self, result: dict) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_cancel_btn.setEnabled(False)
        self._progress.setVisible(False)
        if result.get("error"):
            self._report.setPlainText(f"SCAN FAILED — {result['error']}")
            self._status.setText("SCAN FAILED")
            return
        if result.get("cancelled"):
            self._report.setPlainText("Scan cancelled — partial data discarded.")
            self._status.setText("SCAN CANCELLED")
            return
        self._report.setPlainText(self._format_report(result))
        findings = list(result.get("findings") or [])
        problems = [f for f in findings
                    if "no performance problems" not in str(f.get("issue", "")).lower()]
        self._status.setText(
            f"SCAN COMPLETE — {len(problems)} finding(s)" if problems
            else "SCAN COMPLETE — nothing to fix")

    def _format_report(self, result: dict) -> str:
        metrics = dict(result.get("metrics") or {})
        perf = dict(metrics.get("performance_settings") or {})
        backend = dict(metrics.get("backend") or {})
        vault = dict(metrics.get("vault") or {})
        graph = dict(metrics.get("graph") or {})
        process = dict(metrics.get("process") or {})
        lines = [f"PERFORMANCE REPORT — {metrics.get('generated_at', '')}",
                 f"Profile: {PROFILE_LABELS.get(str(perf.get('profile')), '?')}", ""]
        lines.append("MEASURED")
        if backend:
            lines.append(f"- Memory backend: active={backend.get('active') or '—'} · "
                         f"selected={backend.get('selected') or '—'}"
                         + (f" · error: {backend.get('error')}" if backend.get("error") else ""))
        if metrics.get("local_rows") is not None and int(metrics.get("local_rows", -1)) >= 0:
            lines.append(f"- Local memory rows listed by the Mind panel: {metrics.get('local_rows')}")
        if vault:
            state = "ok" if vault.get("ok") else f"not ok ({vault.get('detail') or 'unknown'})"
            lines.append(f"- Vault: {vault.get('configured') or '—'} · exists="
                         f"{'yes' if vault.get('exists') else 'no'} · "
                         f"{vault.get('notes', 0)} notes · scan {state} in "
                         f"{vault.get('scan_elapsed_s', -1)}s")
        if graph:
            lines.append(f"- Neural Map build: {graph.get('nodes', 0)} nodes · "
                         f"{graph.get('links', 0)} links in {graph.get('elapsed_s', -1)}s "
                         f"(source: {graph.get('source') or '—'})")
        if process.get("available"):
            lines.append(f"- Process: CPU {process.get('cpu_percent', -1)}% · "
                         f"RAM {process.get('rss_mb', -1)} MB")
        else:
            lines.append("- Process metrics unavailable (psutil not installed)")
        lines.append("")
        lines.append("FINDINGS")
        for i, finding in enumerate(list(result.get("findings") or []), 1):
            lines.append(f"{i}. {finding.get('issue')}")
            lines.append(f"   evidence: {finding.get('evidence')}")
            lines.append(f"   impact:   {finding.get('impact')}")
            lines.append(f"   confidence: {finding.get('confidence')}")
            lines.append(f"   action:   {finding.get('action')}")
        return "\n".join(lines)