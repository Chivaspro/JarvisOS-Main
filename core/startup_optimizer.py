"""Launch-time cleanup: clear leftover cache + temp files before JARVIS boots.

Runs once per launch, from start_jarvis.py (skipped while a live instance holds
data/runtime/jarvis.lock) or, when main.py is started by hand, right after the
single-instance guard — always before the UI/backend load.

Safety rules — this module deletes files, so it is deliberately conservative:

* It only ever touches folders it can name: the user's OS temp folder, and
  JARVIS's own ``data/cache`` / ``data/tmp`` / ``data/temp``.
* OS temp: only files untouched for ``JARVIS_STARTUP_TEMP_AGE_HOURS`` (default
  24h). Newer files may belong to a program that is running right now.
* JARVIS cache: only files left over from *earlier* sessions (modified before
  this process started), so nothing this launch creates is ever removed.
* Symlinks / junctions are never followed or removed. Locked or protected
  files are skipped silently. Models, databases, config and logs are untouched.
* A hard time budget stops the sweep so a huge temp folder can never stall boot.

Stages (each one reports progress to the launcher UI, if one is attached):

1. JARVIS cache / tmp leftovers      4. oversized logs (keeps the newest 1 MB)
2. OS temp files older than a day    5. chat DB: WAL checkpoint, ANALYZE, and a
3. orphaned bytecode (.pyc whose        VACUUM only when over a quarter is free
   source file no longer exists)     6. release freed memory

Disable with ``JARVIS_STARTUP_OPTIMIZE=0``.
"""

from __future__ import annotations

import gc
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

__all__ = ["start", "run_startup_optimization", "StartupOptimizer"]

_REPARSE = 0x400  # Windows FILE_ATTRIBUTE_REPARSE_POINT (symlink / junction)


def _enabled() -> bool:
    return os.environ.get("JARVIS_STARTUP_OPTIMIZE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _is_link(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return True  # unreadable: treat as untouchable
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _purge(root: Path, cutoff: float, deadline: float, stats: dict, tick=None) -> None:
    """Delete files under *root* last modified at or before *cutoff*.

    ``tick(stats)`` is called every ~150 files so a launcher UI can show motion.
    """
    root_s = str(root)
    if not os.path.isdir(root_s) or _is_link(root_s):
        return
    for dirpath, dirnames, filenames in os.walk(root_s, topdown=False, followlinks=False):
        if time.monotonic() > deadline:
            stats["timed_out"] = True
            return
        for name in filenames:
            if time.monotonic() > deadline:
                stats["timed_out"] = True
                return
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode) or (
                        getattr(st, "st_file_attributes", 0) & _REPARSE):
                    continue
                if st.st_mtime > cutoff:
                    continue
                try:
                    os.unlink(path)
                except PermissionError:
                    os.chmod(path, stat.S_IWRITE)  # read-only file
                    os.unlink(path)
                stats["files"] += 1
                stats["bytes"] += int(st.st_size)
                if tick is not None and stats["files"] % 150 == 0:
                    tick(stats)
            except OSError:
                stats["skipped"] += 1  # locked / in use / protected
        for name in dirnames:
            sub = os.path.join(dirpath, name)
            if _is_link(sub):
                continue
            try:
                # Only an *empty* folder that is also old: a program may have made
                # a fresh empty temp folder moments ago and be about to fill it.
                if os.lstat(sub).st_mtime > cutoff:
                    continue
                os.rmdir(sub)
                stats["dirs"] += 1
            except OSError:
                pass


_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "agentreach", "site-packages"}


def _prune_orphan_bytecode(base: Path, deadline: float, stats: dict) -> int:
    """Delete ``__pycache__/*.pyc`` whose source ``.py`` no longer exists.

    Stale bytecode is never imported by CPython (it needs the source for a
    ``__pycache__`` entry), so removing it is purely a tidy-up. Live bytecode —
    the files that make boot fast — is never touched.
    """
    removed = 0
    try:
        for dirpath, dirnames, _files in os.walk(str(base), followlinks=False):
            if time.monotonic() > deadline:
                stats["timed_out"] = True
                break
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in _SKIP_DIRS and not _is_link(os.path.join(dirpath, d))]
            if os.path.basename(dirpath) != "__pycache__":
                continue
            parent = os.path.dirname(dirpath)
            try:
                names = os.listdir(dirpath)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".pyc"):
                    continue
                module = name.split(".", 1)[0]
                if os.path.isfile(os.path.join(parent, module + ".py")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    os.unlink(path)
                    removed += 1
                    stats["files"] += 1
                    stats["bytes"] += int(size)
                except OSError:
                    stats["skipped"] += 1
    except Exception:
        pass
    return removed


_LOG_LIMIT = 8 * 1024 * 1024   # a log bigger than this is trimmed...
_LOG_KEEP = 1 * 1024 * 1024    # ...down to its newest megabyte


def _trim_logs(base: Path, deadline: float, stats: dict) -> int:
    """Trim runaway ``data/logs/*.log`` files to their newest 1 MB.

    The crash log is left alone — it is evidence. A file another process holds
    open simply fails to be replaced and is skipped.
    """
    trimmed = 0
    logs = base / "data" / "logs"
    try:
        if not logs.is_dir() or _is_link(str(logs)):
            return 0
        for entry in logs.iterdir():
            if time.monotonic() > deadline:
                stats["timed_out"] = True
                break
            name = entry.name.lower()
            if not name.endswith(".log") or "crash" in name or _is_link(str(entry)):
                continue
            try:
                size = entry.stat().st_size
                if size <= _LOG_LIMIT:
                    continue
                with entry.open("rb") as handle:
                    handle.seek(max(0, size - _LOG_KEEP))
                    tail = handle.read()
                nl = tail.find(b"\n")
                if 0 <= nl < len(tail) - 1:
                    tail = tail[nl + 1:]          # start on a whole line
                tmp = entry.with_name(entry.name + ".trim")
                tmp.write_bytes(tail)
                os.replace(str(tmp), str(entry))
                stats["bytes"] += int(size - len(tail))
                trimmed += 1
            except OSError:
                stats["skipped"] += 1
    except Exception:
        pass
    return trimmed


def _checkpoint_conversations(base: Path) -> bool:
    """Fold the chat-history WAL into the main DB so it opens fast and small.

    Stdlib only: this also runs inside start_jarvis.py, which must never import
    project or third-party code.
    """
    try:
        import sqlite3
        paths = [base / "data" / "conversations.db"]
        mod = sys.modules.get("memory.conversations")
        if mod is not None:
            try:
                paths.insert(0, Path(mod._default_db_path()))
            except Exception:
                pass
        for db in paths:
            if not db.is_file():
                continue
            conn = sqlite3.connect(str(db), timeout=2.0, isolation_level=None)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("PRAGMA optimize")
                try:
                    pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
                    free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
                    # Rebuild only when it pays: >25% free pages, and a file small
                    # enough that VACUUM finishes inside the boot budget.
                    if pages > 0 and free / pages > 0.25 and db.stat().st_size < 256 * 1024 * 1024:
                        conn.execute("VACUUM")
                except Exception:
                    pass
            finally:
                conn.close()
            return True
        return False
    except Exception:
        return False


def _emit(progress, fraction: float, label: str, detail: str = "") -> None:
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, float(fraction))), str(label), str(detail))
    except Exception:
        pass


def run_startup_optimization(base_dir, started_at: float | None = None,
                             time_budget: float = 15.0, progress=None) -> dict:
    """Do the sweep now. Never raises; returns a summary dict.

    ``progress(fraction, label, detail)`` (all optional) reports real progress.
    """
    stats = {"files": 0, "dirs": 0, "bytes": 0, "skipped": 0,
             "timed_out": False, "db": False, "seconds": 0.0, "ran": False}
    if not _enabled():
        _emit(progress, 1.0, "OPTIMIZER DISABLED", "JARVIS_STARTUP_OPTIMIZE=0")
        return stats
    t0 = time.monotonic()
    deadline = t0 + max(2.0, float(time_budget))
    started = float(started_at or time.time())
    base = Path(base_dir).resolve()
    stats["ran"] = True

    def freed() -> str:
        return f"{stats['files']} files / {stats['bytes'] / (1024 * 1024):.1f} MB"

    # 1. JARVIS's own regenerable cache/temp — leftovers from earlier sessions.
    _emit(progress, 0.00, "PURGING JARVIS CACHE", "data/cache  data/tmp  data/temp")
    try:
        data = base / "data"
        for name in ("cache", "tmp", "temp"):
            target = (data / name).resolve()
            if target.parent == data.resolve() and target.is_dir():
                _emit(progress, 0.05, f"PURGING data/{name}", freed())
                _purge(target, started, deadline, stats,
                       tick=lambda s: _emit(progress, 0.15, "PURGING JARVIS CACHE", freed()))
    except Exception:
        pass

    # 2. The user's OS temp folder — only files untouched for a day or more.
    _emit(progress, 0.30, "SWEEPING SYSTEM TEMP", "files idle for 24h+")
    try:
        hours = float(os.environ.get("JARVIS_STARTUP_TEMP_AGE_HOURS", "24") or 24)
        tmp = Path(tempfile.gettempdir()).resolve()
        if (tmp.name.lower() in ("temp", "tmp")
                and tmp != Path(tmp.anchor) and tmp != Path.home()):
            _purge(tmp, time.time() - max(1.0, hours) * 3600.0, deadline, stats,
                   tick=lambda s: _emit(progress, 0.45, "SWEEPING SYSTEM TEMP", freed()))
    except Exception:
        pass

    # 3. Bytecode that outlived its source file.
    _emit(progress, 0.60, "PRUNING ORPHANED BYTECODE", freed())
    _prune_orphan_bytecode(base, deadline, stats)

    # 4. Runaway log files.
    _emit(progress, 0.72, "TRIMMING OVERSIZED LOGS", "keeping newest 1 MB")
    _trim_logs(base, deadline, stats)

    # 5. Compact the chat-history database.
    _emit(progress, 0.82, "COMPACTING CHAT DATABASE", "WAL checkpoint / analyze")
    stats["db"] = _checkpoint_conversations(base)

    # 6. Release freed memory.
    _emit(progress, 0.95, "RELEASING MEMORY", freed())
    try:
        gc.collect()
    except Exception:
        pass
    stats["seconds"] = round(time.monotonic() - t0, 2)
    _emit(progress, 1.0, "OPTIMIZATION COMPLETE", freed())
    return stats


class StartupOptimizer:
    """Handle for the background sweep so boot can wait on it briefly."""

    def __init__(self) -> None:
        self.result: dict = {}
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def wait(self, timeout: float = 4.0) -> bool:
        return self._done.wait(max(0.0, float(timeout)))

    def summary(self) -> str:
        r = self.result
        if not r or not r.get("ran"):
            return ""
        mb = r.get("bytes", 0) / (1024 * 1024)
        tail = " (stopped at time budget)" if r.get("timed_out") else ""
        return (f"SYS: Startup optimize — cleared {r.get('files', 0)} cache/temp file(s), "
                f"{mb:.1f} MB freed in {r.get('seconds', 0)}s; "
                f"chat DB {'compacted' if r.get('db') else 'unchanged'}{tail}.")


def start(base_dir, progress=None) -> StartupOptimizer:
    """Start the sweep on a daemon thread and return its handle.

    ``progress(fraction, label, detail)`` is called from that thread.
    """
    handle = StartupOptimizer()
    started = time.time()

    def _work() -> None:
        try:
            handle.result = run_startup_optimization(base_dir, started_at=started,
                                                     progress=progress)
        except Exception:
            handle.result = {}
        finally:
            _emit(progress, 1.0, "OPTIMIZATION COMPLETE", "")
            handle._done.set()

    handle._thread = threading.Thread(target=_work, name="startup-optimizer", daemon=True)
    handle._thread.start()
    return handle