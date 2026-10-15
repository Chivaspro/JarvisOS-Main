"""Exclusive coding lock (task 5.1) — one coding agent per working tree.

Aider and OpenHands must never edit the same working tree simultaneously.
The lock is a cross-process file lock: ``os.open(..., O_CREAT|O_EXCL)`` is
atomic on Windows and POSIX, the file carries the owner's PID and timestamp,
and a lock older than STALE_SECONDS is considered abandoned (a crashed agent
must not deadlock coding forever) and is broken with a log line.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

STALE_SECONDS = 6 * 3600.0  # a crashed agent's lock is broken after 6 h


class CodingLockBusy(RuntimeError):
    """Raised when the working tree is already locked by a coding agent."""


class CodingLock:
    """Exclusive, cross-process lock for one working tree."""

    def __init__(self, tree: str | os.PathLike) -> None:
        self.tree = Path(tree).resolve()
        self._path = self.tree / ".jarvis-coding.lock"
        self._local = threading.Lock()
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self, owner: str = "coding-agent", timeout_s: float = 0.0) -> None:
        """Take the lock or raise CodingLockBusy. Never blocks indefinitely."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            with self._local:
                if self._held:
                    raise CodingLockBusy(
                        f"A coding agent (this process) is already working in {self.tree}."
                    )
                try:
                    self._try_create(owner)
                    self._held = True
                    return
                except FileExistsError:
                    self._break_if_stale()
                except OSError:
                    # Unwritable tree: proceed without a lock file rather than
                    # refusing all coding in read-only checkouts.
                    self._held = True
                    return
            if time.monotonic() >= deadline:
                holder = self._holder()
                raise CodingLockBusy(
                    f"Another coding agent is already working in {self.tree}"
                    + (f" ({holder})." if holder else ".")
                )
            time.sleep(0.1)

    def release(self) -> None:
        with self._local:
            self._held = False
            try:
                self._path.unlink(missing_ok=True)
            except Exception:
                pass

    def is_locked(self) -> bool:
        if self._held:
            return True
        if not self._path.exists():
            return False
        self._break_if_stale()
        return self._path.exists()

    # ── internals ─────────────────────────────────────────────────────────────

    def _try_create(self, owner: str) -> None:
        payload = json.dumps({
            "owner": owner,
            "pid": os.getpid(),
            "at": time.time(),
        })
        fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)

    def _age(self) -> float:
        try:
            return time.time() - self._path.stat().st_mtime
        except OSError:
            return 0.0

    def _break_if_stale(self) -> None:
        if self._path.exists() and self._age() > STALE_SECONDS:
            try:
                holder = self._holder()
                self._path.unlink(missing_ok=True)
                print(f"[CodingLock] broke stale lock in {self.tree} "
                      f"(held by {holder} for >{STALE_SECONDS / 3600:.0f}h)")
            except Exception:
                pass

    def _holder(self) -> str:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return f"pid {data.get('pid')} {data.get('owner', '')}".strip()
        except Exception:
            return ""


_locks: dict[str, CodingLock] = {}
_locks_guard = threading.Lock()


def get_coding_lock(tree: str | os.PathLike) -> CodingLock:
    """Per-tree lock singleton (thread-safe)."""
    key = str(Path(tree).resolve())
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = CodingLock(tree)
            _locks[key] = lock
    return lock
