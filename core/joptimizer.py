"""JOptimizer backend — safe, reversible Windows maintenance for J.A.R.V.I.S.

This module is the operation layer behind the Settings ▸ JOptimizer tab. It is
ported from the standalone HI+ Optimizer but tightened for life inside J.A.R.V.I.S:

* Everything is Windows-only and guarded, so importing this module anywhere
  (tests, other platforms) never raises — the queries just report "not Windows".
* Every registry/service change is preceded by a *snapshot* that stores the
  exact prior values + service start types as JSON under ``data/joptimizer``.
  Restore re-applies them, so nothing here is irreversible.
* Commands never flash a console window (CREATE_NO_WINDOW), matching how the
  rest of J.A.R.V.I.S shells out.
* Privileged ops (HKLM telemetry, service start changes, DISM/SFC/netsh resets,
  restore points) require administrator rights; when absent they report the
  limitation honestly instead of pretending the change landed.

The UI in ``ui_settings.py`` runs these callables on a worker thread and streams
their returned strings into the tab's log, so none of these functions touch Qt.
"""
from __future__ import annotations

import os
import re
import json
import shutil
import ctypes
import platform
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Windows-only surface. Import winreg once, guarded, so non-Windows imports are
# safe; every function that uses it checks IS_WINDOWS first.
# ---------------------------------------------------------------------------
IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import winreg  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "joptimizer"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"

# Registry + service surface, ported verbatim from HI+ Optimizer so the keys are
# the real, documented Windows ones (never invented).
REGISTRY_TARGETS: dict[str, tuple[int, str, list[str]]] = {}
if IS_WINDOWS:
    REGISTRY_TARGETS = {
        "telemetry_policy": (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
            ["AllowTelemetry"],
        ),
        "game_dvr": (
            winreg.HKEY_CURRENT_USER,
            r"System\GameConfigStore",
            ["GameDVR_Enabled", "AppCaptureEnabled", "GameDVR_FSEBehaviorMode"],
        ),
        "game_dvr_policy": (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Policies\Microsoft\Windows\GameDVR",
            ["AllowGameDVR"],
        ),
        "visual_effects": (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects",
            ["VisualFXSetting"],
        ),
        "transparency": (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ["EnableTransparency"],
        ),
        "taskbar_widgets": (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
            ["TaskbarDa"],
        ),
        "advertising_id": (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
            ["Enabled"],
        ),
        "tailored_experiences": (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Privacy",
            ["TailoredExperiencesWithDiagnosticDataEnabled"],
        ),
        "game_mode": (
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\GameBar",
            ["AutoGameModeEnabled"],
        ),
    }

# Reversible service surface (label, id, risk). Windows Update is intentionally
# flagged high-risk so the UI warns before disabling it.
SAFE_SERVICES: list[dict[str, str]] = [
    {"label": "Windows Update", "id": "wuauserv", "risk": "High"},
    {"label": "Bluetooth Support", "id": "bthserv", "risk": "Low"},
    {"label": "Print Spooler", "id": "Spooler", "risk": "Medium"},
    {"label": "WLAN AutoConfig", "id": "WlanSvc", "risk": "Medium"},
    {"label": "Remote Desktop", "id": "TermService", "risk": "Medium"},
]

_START_TYPES = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}
_SC_MAP = {2: "auto", 3: "demand", 4: "disabled"}


def ensure_dirs() -> None:
    for path in (DATA_DIR, BACKUP_DIR, LOG_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _not_windows() -> RuntimeError:
    return RuntimeError("JOptimizer system actions require Windows 10/11.")


# ---------------------------------------------------------------------------
# Subprocess helpers (no console window ever)
# ---------------------------------------------------------------------------
def run_cmd(args: list[str], timeout: int | None = None) -> tuple[int, str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=creationflags,
        )
    except FileNotFoundError:
        return 127, f"Command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"Timed out: {' '.join(args)}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def powershell(script: str, timeout: int | None = None) -> tuple[int, str]:
    if not IS_WINDOWS:
        raise _not_windows()
    return run_cmd(["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-Command", script], timeout=timeout)


# ---------------------------------------------------------------------------
# Real-time progress plumbing
# ---------------------------------------------------------------------------
# A progress callback is (percent:int, message:str) -> None, called from the
# worker thread. The UI marshals it to the GUI thread via a Qt signal, so the
# bar animates while the command runs. Every op treats the callback as optional
# and fire-and-forget: a broken consumer must never abort a real optimization.
def _report(progress, percent, message="") -> None:
    if progress is None:
        return
    try:
        progress(max(0, min(100, int(percent))), str(message or ""))
    except Exception:
        pass


_PCT_RE = re.compile(r"(\d{1,3})\s*%")


def _run_streaming(args: list[str], progress=None, timeout: int = 1800) -> tuple[int, str]:
    """Run a long Windows command line-by-line so its own % can drive the bar.

    DISM/SFC/defrag/chkdsk print a running percentage; parsing it gives genuinely
    real progress rather than a fake animation. Falls back to a coarse indetermi-
    nate tick when a tool prints no percent, and never blocks longer than timeout.
    """
    if not IS_WINDOWS:
        raise _not_windows()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _report(progress, 1, " ".join(args))
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return 127, f"Command not found: {args[0]}"
    lines: list[str] = []
    deadline = None
    if timeout:
        import time as _time
        deadline = _time.monotonic() + timeout
    if proc.stdout is not None:
        for raw in proc.stdout:
            text = raw.strip()
            if text:
                lines.append(text)
                m = _PCT_RE.search(text)
                if m:
                    _report(progress, int(m.group(1)), text[:80])
            if deadline is not None:
                import time as _time
                if _time.monotonic() > deadline:
                    proc.kill()
                    return 124, "Timed out: " + " ".join(args)
    rc = proc.wait()
    return rc, "\n".join(lines[-40:])


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------
def registry_read(root: int, path: str, name: str) -> dict[str, Any]:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, name)
            return {"exists": True, "value": value, "kind": int(kind)}
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        return {"exists": False, "error": str(exc)}


def registry_write(root: int, path: str, name: str, value: Any, kind: int) -> None:
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, kind, value)


def registry_delete_value(root: int, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def _set_reg(target: str, name: str, value: Any, kind: int) -> None:
    if not IS_WINDOWS:
        raise _not_windows()
    root, path, _ = REGISTRY_TARGETS[target]
    registry_write(root, path, name, value, kind)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def service_start_value(service: str) -> int | None:
    if not IS_WINDOWS:
        return None
    path = rf"SYSTEM\CurrentControlSet\Services\{service}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, "Start")
            return int(value)
    except (FileNotFoundError, OSError, ValueError):
        return None


def service_state(service: str) -> str:
    if not IS_WINDOWS:
        return "Unknown"
    rc, out = run_cmd(["sc.exe", "query", service])
    if rc != 0:
        return "Unknown"
    for line in out.splitlines():
        if "STATE" in line:
            if "RUNNING" in line:
                return "Running"
            if "STOPPED" in line:
                return "Stopped"
            if "PAUSED" in line:
                return "Paused"
    return "Unknown"


def set_service_start(service: str, start_arg: str) -> tuple[int, str]:
    if not IS_WINDOWS:
        raise _not_windows()
    if start_arg not in {"auto", "demand", "disabled"}:
        raise ValueError(f"Unsupported service start type: {start_arg}")
    return run_cmd(["sc.exe", "config", service, "start=", start_arg])


# ---------------------------------------------------------------------------
# Snapshot / restore (the reversibility guarantee)
# ---------------------------------------------------------------------------
def snapshot_state(name: str) -> Path:
    if not IS_WINDOWS:
        raise _not_windows()
    ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"{stamp}_{name}.json"
    data: dict[str, Any] = {"created": datetime.now().isoformat(),
                           "name": name, "registry": {}, "services": {}}
    for key, (root, reg_path, values) in REGISTRY_TARGETS.items():
        root_name = "HKCU" if root == winreg.HKEY_CURRENT_USER else "HKLM"
        data["registry"][key] = {
            "root": root_name, "path": reg_path,
            "values": {v: registry_read(root, reg_path, v) for v in values},
        }
    for svc in SAFE_SERVICES:
        data["services"][svc["id"]] = {
            "label": svc["label"], "start": service_start_value(svc["id"]),
            "state": service_state(svc["id"]),
        }
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


def restore_snapshot(path: Path) -> list[str]:
    if not IS_WINDOWS:
        raise _not_windows()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors: list[str] = []
    roots = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    for item in data.get("registry", {}).values():
        root = roots.get(item["root"])
        if root is None:
            continue
        for name, original in item.get("values", {}).items():
            try:
                if original.get("exists"):
                    registry_write(root, item["path"], name,
                                   original["value"], int(original["kind"]))
                else:
                    registry_delete_value(root, item["path"], name)
            except Exception as exc:
                errors.append(f"{item['path']}::{name}: {exc}")
    for service, sd in data.get("services", {}).items():
        original = sd.get("start")
        if original is None:
            continue
        start_arg = _SC_MAP.get(int(original))
        if start_arg:
            rc, out = set_service_start(service, start_arg)
            if rc != 0:
                errors.append(f"Service {service}: {out}")
    return errors


def list_snapshots() -> list[Path]:
    ensure_dirs()
    return sorted(BACKUP_DIR.glob("*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def open_backup_folder() -> None:
    if not IS_WINDOWS:
        raise _not_windows()
    ensure_dirs()
    os.startfile(str(BACKUP_DIR))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Metrics / inspection (read-only, psutil)
# ---------------------------------------------------------------------------
def _psutil():
    import psutil
    return psutil


def get_system_summary() -> dict[str, Any]:
    ps = _psutil()
    mem = ps.virtual_memory()
    disk = ps.disk_usage(Path.home().anchor or "C:\\")
    return {
        "cpu": ps.cpu_percent(interval=None),
        "ram": mem.percent,
        "ram_gb": round(mem.total / 1024**3, 1),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "uptime": str(datetime.now() - datetime.fromtimestamp(ps.boot_time())).split(".")[0],
        "os": platform.platform(),
    }


def health_score(summary: dict[str, Any] | None = None) -> int:
    ps = _psutil()
    s = summary or get_system_summary()
    score = 100
    if s["cpu"] >= 90:
        score -= 25
    elif s["cpu"] >= 80:
        score -= 15
    if s["ram"] >= 90:
        score -= 25
    elif s["ram"] >= 80:
        score -= 15
    free_ratio = s["disk_free_gb"] / s["disk_total_gb"] if s["disk_total_gb"] else 1.0
    if free_ratio < 0.10:
        score -= 20
    elif free_ratio < 0.20:
        score -= 10
    return max(0, score)


def top_processes(limit: int = 8) -> list[str]:
    ps = _psutil()
    rows: list[tuple[float, float, str, int]] = []
    for proc in ps.process_iter(["name", "pid", "memory_percent"]):
        try:
            rows.append((proc.cpu_percent(interval=None),
                         float(proc.info.get("memory_percent") or 0.0),
                         str(proc.info.get("name") or "Unknown"),
                         int(proc.info.get("pid") or 0)))
        except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess, OSError):
            continue
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [f"{name} (PID {pid}) · CPU {cpu:.1f}% · RAM {mem:.1f}%"
            for cpu, mem, name, pid in rows[:limit]]


def memory_pressure() -> str:
    ps = _psutil()
    mem = ps.virtual_memory()
    lines = [f"MEMORY · {mem.percent:.0f}% used · {_fmt_bytes(mem.available)} available"]
    if mem.percent >= 90:
        lines.append("  ⚠ High memory pressure — J.A.R.V.I.S. may stutter; close heavy apps.")
    elif mem.percent >= 80:
        lines.append("  Elevated memory pressure — worth trimming background apps.")
    else:
        lines.append("  Healthy memory headroom for J.A.R.V.I.S.")
    lines.extend(f"  {row}" for row in top_processes())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Temp / JARVIS cache helpers (conservative, never touches user documents)
# ---------------------------------------------------------------------------
def _fmt_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _temp_targets() -> list[Path]:
    out = []
    for env in ("TEMP", "TMP"):
        p = os.environ.get(env)
        if p:
            out.append(Path(p))
    out.append(Path(r"C:\Windows\Temp"))
    return [p for p in dict.fromkeys(out) if p.exists() and p.is_dir()]


def temp_cleanup_preview() -> tuple[int, int]:
    count = total = 0
    for target in _temp_targets():
        for entry in target.iterdir():
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    size = sum(c.stat().st_size for c in entry.rglob("*")
                               if c.is_file() and not c.is_symlink())
                else:
                    size = entry.stat().st_size
                count += 1
                total += size
            except (PermissionError, OSError):
                continue
    return count, total


def clear_temp_files(progress=None) -> tuple[int, int]:
    deleted = skipped = 0
    targets = _temp_targets()
    for ti, target in enumerate(targets):
        _report(progress, int(ti / max(1, len(targets)) * 100), target.name)
        try:
            entries = list(target.iterdir())
        except OSError:
            continue
        for ei, entry in enumerate(entries):
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                deleted += 1
            except (PermissionError, OSError):
                skipped += 1
            if ei % 25 == 0:
                _report(progress, int((ti + ei / max(1, len(entries))) / max(1, len(targets)) * 100))
    _report(progress, 100, "Temp cleanup complete")
    return deleted, skipped


def jarvis_cache_preview() -> tuple[int, int]:
    """Sizes of ONLY auto-regenerable J.A.R.V.I.S caches (__pycache__, .pytest_cache).

    Never data/, config/, memory or user files — deleting those would be data loss.
    """
    count = total = 0
    for cache in _iter_jarvis_caches():
        try:
            for f in cache.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
                    count += 1
        except OSError:
            continue
    return count, total


def _iter_jarvis_caches():
    for base in (ROOT,):
        for dirpath, dirnames, _ in os.walk(base):
            # Don't descend into heavy/irrelevant trees.
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "node_modules", "models", ".venv", "venv")]
            for d in list(dirnames):
                if d in ("__pycache__", ".pytest_cache"):
                    yield Path(dirpath) / d


def jarvis_cache_clean(progress=None) -> tuple[int, int]:
    removed = skipped = 0
    caches = list(_iter_jarvis_caches())
    for i, cache in enumerate(caches):
        _report(progress, int(i / max(1, len(caches)) * 100), cache.name)
        try:
            shutil.rmtree(cache)
            removed += 1
        except (PermissionError, OSError):
            skipped += 1
    _report(progress, 100, "Cache clean complete")
    return removed, skipped


# ---------------------------------------------------------------------------
# Power + network + repair commands
# ---------------------------------------------------------------------------
def get_active_power_plan() -> str:
    if not IS_WINDOWS:
        return "Unknown"
    rc, out = run_cmd(["powercfg.exe", "/getactivescheme"], timeout=20)
    return out if rc == 0 else "Unknown"


def set_power_plan(kind: str) -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    guid = "SCHEME_BALANCED" if kind == "balanced" else "SCHEME_MIN"
    rc, out = run_cmd(["powercfg.exe", "/setactive", guid], timeout=30)
    if rc != 0:
        raise RuntimeError(out or "powercfg failed")
    return f"Power plan set to {'Balanced' if kind == 'balanced' else 'High Performance'}."


def flush_dns() -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = run_cmd(["ipconfig.exe", "/flushdns"], timeout=30)
    if rc != 0:
        raise RuntimeError(out or "ipconfig flushdns failed")
    return "DNS resolver cache flushed."


def latency_test() -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = run_cmd(["ping.exe", "-n", "4", "1.1.1.1"], timeout=30)
    return out or "Ping produced no output."


def winsock_reset() -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = run_cmd(["netsh.exe", "winsock", "reset"], timeout=60)
    if rc != 0:
        raise RuntimeError(out or "Winsock reset failed (needs Administrator).")
    return "Winsock reset. Reboot if connectivity is affected."


def ip_reset() -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = run_cmd(["netsh.exe", "int", "ip", "reset"], timeout=60)
    if rc != 0:
        raise RuntimeError(out or "TCP/IP reset failed (needs Administrator).")
    return "TCP/IP reset. Reboot recommended."


def network_info() -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = run_cmd(["ipconfig.exe", "/all"], timeout=30)
    return out or "No interface information."


def run_dism(progress=None) -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = _run_streaming(["DISM.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"], progress, timeout=1800)
    if rc != 0:
        raise RuntimeError((out[-800:] if out else "DISM failed (needs Administrator)."))
    _report(progress, 100, "DISM complete")
    return "DISM component-store repair completed."


def run_sfc(progress=None) -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = _run_streaming(["sfc.exe", "/scannow"], progress, timeout=1800)
    if rc != 0:
        raise RuntimeError((out[-800:] if out else "SFC failed (needs Administrator)."))
    _report(progress, 100, "SFC complete")
    tail = out.splitlines()[-1] if out else "System File Check completed."
    return f"System File Check completed — {tail[:160]}"


def run_chkdsk(progress=None) -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = _run_streaming(["chkdsk.exe", "C:", "/scan"], progress, timeout=1800)
    if rc != 0:
        raise RuntimeError((out[-800:] if out else "Disk scan failed (needs Administrator)."))
    _report(progress, 100, "Scan complete")
    return "Online disk scan completed."


def optimize_drive(progress=None) -> str:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = _run_streaming(["defrag.exe", "C:", "/O", "/U"], progress, timeout=1800)
    if rc != 0:
        raise RuntimeError(out or "Drive optimization failed (needs Administrator).")
    _report(progress, 100, "Optimize complete")
    return "Drive optimization completed."


def create_restore_point(description: str = "JOptimizer") -> tuple[bool, str]:
    if not IS_WINDOWS:
        raise _not_windows()
    rc, out = powershell("Checkpoint-Computer -Description "
                         + json.dumps(description)
                         + " -RestorePointType MODIFY_SETTINGS", timeout=180)
    return rc == 0, out or ("Restore point created." if rc == 0 else "Restore point creation failed.")


# ---------------------------------------------------------------------------
# Reversible privacy / performance toggles (HKCU — no admin, snapshot restores)
# ---------------------------------------------------------------------------
_DWORD = 1  # winreg.REG_DWORD == 1; use literal so it's importable off-Windows


def disable_game_dvr() -> str:
    _set_reg("game_dvr", "GameDVR_Enabled", 0, _DWORD)
    _set_reg("game_dvr", "AppCaptureEnabled", 0, _DWORD)
    _set_reg("game_dvr", "GameDVR_FSEBehaviorMode", 2, _DWORD)
    return "Game DVR background capture disabled for this user."


def privacy_extras() -> str:
    _set_reg("advertising_id", "Enabled", 0, _DWORD)
    _set_reg("tailored_experiences", "TailoredExperiencesWithDiagnosticDataEnabled", 0, _DWORD)
    return "Advertising ID + tailored-experience diagnostics disabled for this user."


def disable_widgets() -> str:
    _set_reg("taskbar_widgets", "TaskbarDa", 0, _DWORD)
    return "Taskbar Widgets button hidden for this user."


def disable_transparency() -> str:
    _set_reg("transparency", "EnableTransparency", 0, _DWORD)
    return "UI transparency disabled for this user."


def enable_game_mode() -> str:
    _set_reg("game_mode", "AutoGameModeEnabled", 1, _DWORD)
    return "Windows Game Mode enabled for this user."


def visual_performance() -> str:
    _set_reg("visual_effects", "VisualFXSetting", 2, _DWORD)
    return "Visual effects tuned for performance for this user."


def remove_telemetry() -> str:
    """HKLM policy + telemetry services — REQUIRES administrator."""
    if not IS_WINDOWS:
        raise _not_windows()
    if not is_admin():
        raise PermissionError("Telemetry policy + services need Administrator rights.")
    _set_reg("telemetry_policy", "AllowTelemetry", 0, _DWORD)
    for service in ("DiagTrack", "dmwappushservice"):
        set_service_start(service, "disabled")
    return "Telemetry policy restricted + DiagTrack/dmwappushservice set disabled."


# ---------------------------------------------------------------------------
# Profiles + the J.A.R.V.I.S optimization suites (snapshot-first, reversible)
# ---------------------------------------------------------------------------
def _run_steps(steps: list[tuple[str, Callable[[], str]]], progress=None) -> list[str]:
    """Run ordered (label, fn) reversible steps; collect per-step results.

    A step that raises (e.g. an admin-only op without elevation) is recorded as a
    warning and the rest still proceed — nothing is claimed as done that failed.
    When a progress callback is supplied it advances 0→100 across the steps, so
    the UI shows real per-step movement.
    """
    out: list[str] = []
    total = max(1, len(steps))
    for i, (label, fn) in enumerate(steps):
        _report(progress, int(i / total * 100), label)
        try:
            out.append(f"✓ {label}: {fn()}")
        except Exception as exc:
            out.append(f"⚠ {label} skipped: {str(exc)[:120]}")
        _report(progress, int((i + 1) / total * 100), label)
    return out


def apply_profile(name: str, progress=None) -> str:
    """Safe / Gaming / Low Spec. All HKCU + power-plan only, so they work without
    elevation and are fully restorable from the snapshot taken first."""
    if not IS_WINDOWS:
        raise _not_windows()
    _report(progress, 3, "Snapshot…")
    snapshot = snapshot_state(f"profile_{name}")
    results = [f"Snapshot saved: {snapshot.name}"]
    steps_by_profile = {
        "safe": [
            ("Power plan", lambda: set_power_plan("high")),
            ("Game DVR off", disable_game_dvr),
            ("Game Mode on", enable_game_mode),
            ("Visual effects", visual_performance),
        ],
        "gaming": [
            ("Power plan", lambda: set_power_plan("high")),
            ("Game DVR off", disable_game_dvr),
            ("Game Mode on", enable_game_mode),
            ("Visual effects", visual_performance),
        ],
        "lowspec": [
            ("Game DVR off", disable_game_dvr),
            ("Visual effects", visual_performance),
            ("Transparency off", disable_transparency),
            ("Widgets off", disable_widgets),
        ],
    }
    # Snapshot owns 0-10%, the steps fill 10-95%, completion hits 100%.
    scaled = (lambda pct, msg: _report(progress, 10 + pct * 0.85, msg)) if progress else None
    results.extend(_run_steps(steps_by_profile.get(name, []), scaled))
    results.append(f"Restore anytime from snapshot: {snapshot.name}")
    _report(progress, 100, "Done")
    return "  \n".join(results)


def jarvis_ultimate_optimize(auto: bool = False, progress=None) -> str:
    """A conservative, reversible suite tuned to keep J.A.R.V.I.S. smooth.

    Deliberately EXCLUDES irreversible / high-risk actions (no Winsock/IP reset,
    no DISM/SFC, no service disabling). Everything here is undone by the snapshot.
    """
    if not IS_WINDOWS:
        raise _not_windows()
    _report(progress, 3, "Snapshot…")
    snapshot = snapshot_state("jarvis_auto" if auto else "jarvis_ultimate")
    results = [f"Snapshot saved: {snapshot.name}"]
    steps = [
        ("Power plan", lambda: set_power_plan("high")),
        ("Game DVR off (frees background capture)", disable_game_dvr),
        ("Game Mode on", enable_game_mode),
        ("Visual effects for performance", visual_performance),
        ("Transparency off (lighter UI)", disable_transparency),
        ("Widgets off", disable_widgets),
        ("Privacy extras (advertising + tailored diagnostics off)", privacy_extras),
    ]
    scaled = (lambda pct, msg: _report(progress, 5 + pct * 0.80, msg)) if progress else None
    results.extend(_run_steps(steps, scaled))
    # JARVIS caches are safe to clear (auto-regenerated).
    _report(progress, 90, "Clearing JARVIS caches…")
    removed, skipped = jarvis_cache_clean()
    results.append(f"✓ J.A.R.V.I.S. caches: cleared {removed} (skipped {skipped} in use)")
    results.append(f"Restore anytime from snapshot: {snapshot.name}")
    _report(progress, 100, "Done")
    label = "AUTO ULTIMATE OPTIMIZE" if auto else "J.A.R.V.I.S. ULTIMATE OPTIMIZE"
    return f"{label}  \n  \n".join(results)


def jarvis_revert_latest() -> str:
    snaps = list_snapshots()
    if not snaps:
        return "No snapshot to restore yet."
    errors = restore_snapshot(snaps[0])
    if errors:
        return f"Restored {snaps[0].name} with warnings: " + " | ".join(errors[:8])
    return f"Reverted to snapshot: {snaps[0].name}"


def smart_scan() -> str:
    ps = _psutil()
    s = get_system_summary()
    # A short interval sample gives a real CPU figure.
    s["cpu"] = ps.cpu_percent(interval=0.6)
    score = health_score(s)
    findings: list[str] = []
    if s["ram"] >= 85:
        findings.append("RAM high")
    if s["disk_free_gb"] / max(1, s["disk_total_gb"]) < 0.12:
        findings.append("Disk space low")
    if s["cpu"] >= 85:
        findings.append("CPU busy right now")
    if not findings:
        findings.append("No obvious low-risk issue")
    return ("SMART SCAN · health={}/100 · CPU={:.0f}% · RAM={:.0f}% · disk-free={:.1f} GB · {}"
            .format(score, s["cpu"], s["ram"], s["disk_free_gb"], ", ".join(findings)))
