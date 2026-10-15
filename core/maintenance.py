"""J.A.R.V.I.S. housekeeping — keep logs, caches and bytecode from growing.

Why this exists
---------------
``docs/cleanup_report.md`` (§7) names the single optimisation gap it left open:
*``data/logs/`` keeps growing — log rotation is not implemented.* An install
that runs for weeks ends up with megabytes of JSONL that slow every directory
listing, and a cache that never expires slowly bloats the install.

This module is the one cross-platform, **stdlib-only** place that answers
"keep this install small and fast". It only ever removes things the project
itself generated and can regenerate:

* ``data/logs``   — rotated ``jarvis-*.jsonl`` older than the retention window
* ``data/cache``  — cached artefacts older than the retention window
* ``__pycache__`` — compiled bytecode, regenerated on the next import

It is deliberately dependency-free and infection-free: every step is guarded,
nothing raises, and no user file (config, memory, documents, models, voices)
is ever in scope. ``run_maintenance()`` returns a structured report and a
one-line human summary so both the setup wizard and the ``diagnostics`` tool
can show exactly what was reclaimed.

Usage::

    from core.maintenance import run_maintenance, format_report
    print(format_report(run_maintenance()))
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

__all__ = [
    "prune_logs", "prune_cache", "prune_pycache", "run_maintenance",
    "format_report", "RETENTION_DAYS", "MAX_LOGS_MB", "MAX_BACKUPS",
]

# Retention defaults. Overridable per call and (for logs) by environment so an
# operator can tune a long-running install without editing code.
RETENTION_DAYS = 14          # logs + cache older than this are removed
MAX_LOGS_MB = 64             # hard ceiling for the whole log directory
MAX_BACKUPS = 20             # config/api_keys.json backups to keep

# Directories never walked for __pycache__ pruning: vendored runtimes, virtual
# environments, the giant model tree and VCS metadata are not ours to touch.
_SKIP_PARTS = {
    ".git", ".venv", "venv", "env", "__pycache__",
    "node_modules", "AgentReach", "UI-TARS", "models",
    "site-packages", ".freebuff",
}


def _root() -> Path:
    """Project root (``core/maintenance.py`` lives one level down)."""
    return Path(__file__).resolve().parents[1]


def _logs_dir() -> Path:
    """The single log location, matching config.settings when importable."""
    try:
        from config.settings import logs_dir as _ld
        return _ld()
    except Exception:
        d = _root() / "data" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _reclaimed(files: list[Path]) -> int:
    """Sum of the byte sizes of *files* (0 for anything unreadable)."""
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


def _mb(n: int) -> float:
    return round(n / (1024 * 1024), 2)


def prune_logs(logs_dir: Path | str | None = None,
               retention_days: int | None = None,
               max_total_mb: int | None = None) -> dict:
    """Rotate the log directory: drop old daily files, cap the total size.

    Always keeps the newest ``jarvis-*.jsonl`` (the file the running process is
    appending to) so rotation can never truncate the live log. Returns
    ``{"removed": n, "bytes": n, "kept": n}``.
    """
    if retention_days is None:
        try:
            retention_days = int(os.environ.get("JARVIS_LOG_RETENTION_DAYS",
                                                RETENTION_DAYS))
        except Exception:
            retention_days = RETENTION_DAYS
    if max_total_mb is None:
        max_total_mb = MAX_LOGS_MB

    out = {"removed": 0, "bytes": 0, "kept": 0}
    try:
        d = Path(logs_dir) if logs_dir else _logs_dir()
        files = [p for p in d.glob("*.jsonl")] + [p for p in d.glob("*.log")]
        # Newest first — index 0 is the live log, never a deletion candidate.
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return out

        cutoff = time.time() - max(0, retention_days) * 86400
        doomed: list[Path] = []
        for p in files[1:]:
            try:
                if p.stat().st_mtime < cutoff:
                    doomed.append(p)
            except OSError:
                continue

        # Size ceiling: if what survives is still over budget, drop oldest-first.
        survivors = [p for p in files if p not in doomed]
        budget = max(0, max_total_mb) * 1024 * 1024
        total = _reclaimed(survivors)
        for p in reversed(survivors[1:]):        # never the newest
            if total <= budget:
                break
            doomed.append(p)
            try:
                total -= p.stat().st_size
            except OSError:
                pass

        for p in doomed:
            try:
                size = p.stat().st_size
                p.unlink()
                out["removed"] += 1
                out["bytes"] += size
            except OSError:
                continue
        out["kept"] = len(files) - out["removed"]
    except Exception:
        pass
    return out


def prune_cache(cache_dir: Path | str | None = None,
                retention_days: int | None = None) -> dict:
    """Remove cached artefacts older than the retention window."""
    if retention_days is None:
        retention_days = RETENTION_DAYS
    out = {"removed": 0, "bytes": 0}
    try:
        try:
            from config.settings import CACHE_DIR as _cd
            d = Path(cache_dir) if cache_dir else Path(_cd)
        except Exception:
            d = Path(cache_dir) if cache_dir else (_root() / "data" / "cache")
        if not d.is_dir():
            return out
        cutoff = time.time() - max(0, retention_days) * 86400
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    size = p.stat().st_size
                    p.unlink()
                    out["removed"] += 1
                    out["bytes"] += size
            except OSError:
                continue
    except Exception:
        pass
    return out


def prune_pycache(root: Path | str | None = None) -> dict:
    """Delete ``__pycache__`` directories, skipping vendored/venv trees.

    Compiled bytecode is regenerated on the next import, so this is always
    safe — but the walk deliberately excludes ``node_modules``, ``.venv``,
    ``AgentReach``, ``UI-TARS``, ``models`` and ``.git``.
    """
    out = {"removed": 0, "bytes": 0}
    try:
        base = Path(root) if root else _root()
        for dirpath, dirnames, _files in os.walk(base, topdown=True):
            keep: list[str] = []
            for name in list(dirnames):
                child = Path(dirpath) / name
                if name == "__pycache__":
                    try:
                        size = _reclaimed(
                            [p for p in child.rglob("*") if p.is_file()])
                        shutil.rmtree(child, ignore_errors=True)
                        out["removed"] += 1
                        out["bytes"] += size
                    except Exception:
                        pass
                    continue                     # deleted — do not descend
                if name in _SKIP_PARTS:
                    continue                     # vendored/venv/VCS — skip
                keep.append(name)
            dirnames[:] = keep
        # Loose .pyc left at the top of the walk root.
        for p in base.glob("*.py[co]"):
            try:
                size = p.stat().st_size
                p.unlink()
                out["bytes"] += size
            except OSError:
                continue
    except Exception:
        pass
    return out


def run_maintenance(prune_bytecode: bool = True,
                    retention_days: int | None = None) -> dict:
    """Run every safe housekeeping step and return one structured report.

    ``prune_bytecode`` is exposed so a caller running the app *from* the tree
    can skip ``__pycache__`` removal; the setup wizard leaves it on.
    """
    logs = prune_logs(retention_days=retention_days)
    cache = prune_cache(retention_days=retention_days)
    pyc = prune_pycache() if prune_bytecode else {"removed": 0, "bytes": 0}
    total = logs["bytes"] + cache["bytes"] + pyc["bytes"]
    report = {
        "logs": logs,
        "cache": cache,
        "pycache": pyc,
        "reclaimed_bytes": total,
        "reclaimed_mb": _mb(total),
    }
    try:
        from observability.logger import log_event
        log_event("INFO", "maintenance", "housekeeping",
                  result={"reclaimed_mb": report["reclaimed_mb"],
                          "logs": logs["removed"],
                          "cache": cache["removed"],
                          "pycache": pyc["removed"]})
    except Exception:
        pass
    return report


def format_report(report: dict) -> str:
    """Human-readable summary — every number is a real measurement."""
    if not report:
        return "Maintenance: nothing to report."
    logs, cache, pyc = report["logs"], report["cache"], report["pycache"]
    lines = ["MAINTENANCE  //  housekeeping complete"]
    lines.append(f"- logs:       {logs['removed']} file(s) rotated, "
                 f"{logs['kept']} kept "
                 f"({_mb(logs['bytes'])} MB)")
    lines.append(f"- cache:      {cache['removed']} file(s) expired "
                 f"({_mb(cache['bytes'])} MB)")
    lines.append(f"- bytecode:   {pyc['removed']} __pycache__ dir(s) removed "
                 f"({_mb(pyc['bytes'])} MB)")
    lines.append(f"TOTAL RECLAIMED: {report['reclaimed_mb']} MB")
    if report["reclaimed_bytes"] == 0:
        lines.append("Nothing needed cleaning — the install is already tidy.")
    return "\n".join(lines)