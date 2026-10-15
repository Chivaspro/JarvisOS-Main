"""Control-session ownership locks (prevent tool conflicts).

Only one control engine may own a target interaction at a time:

* ``desktop``  — the real desktop is being driven: Agent TARS launching
  applications and running commands, the local ``pc_control`` actions
  (pywinauto/PyAutoGUI), or a UI-TARS visual action. A browser workflow must not
  grab focus (or move the pointer) at the same time.
* ``browser``  — a browser session is owned: Agent TARS web workflows or the
  Playwright ``browser_action`` layer. A desktop task must not click on that
  browser window behind its back.
* ``research`` — one Agent Reach research job at a time, so channel CLIs and
  their rate limits are never driven concurrently.

Like coding/coding_lock.py the lock is both an in-process guard and a
cross-process file lock, because the engines are separate processes and a
crashed worker must not deadlock control forever.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

STALE_SECONDS = 30 * 60.0   # a crashed worker's lock is broken after 30 min
WAIT_HINT_SECONDS = 15.0    # how long a caller blocks before being told "busy"


class ControlBusy(RuntimeError):
    """Raised when another control engine already owns the target."""


def _runtime_dir() -> Path:
    try:
        from config.settings import RUNTIME_DIR
        base = Path(RUNTIME_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent / "data" / "runtime"
    base = base / "capability_locks"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


class ControlLock:
    """Exclusive lock for one control domain."""

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._path = _runtime_dir() / f"{domain}.lock"
        self._local = threading.Lock()
        self._held = False
        self._owner = ""

    # ── public ────────────────────────────────────────────────────────────────

    def acquire(self, owner: str, timeout_s: float = 0.0) -> "ControlLock":
        """Take the lock, waiting up to *timeout_s*. Raises ControlBusy."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            with self._local:
                if self._held:
                    raise ControlBusy(
                        f"{self.domain} control is already active in this J.A.R.V.I.S. "
                        f"session ({self._owner})."
                    )
                try:
                    self._create(owner)
                    self._held, self._owner = True, owner
                    return self
                except FileExistsError:
                    self._break_if_stale()
                except OSError:
                    # Unwritable runtime dir: keep the in-process guarantee and
                    # let the task run rather than refusing all control.
                    self._held, self._owner = True, owner
                    return self
            if time.monotonic() >= deadline:
                holder = self.holder()
                raise ControlBusy(
                    f"Another control engine is holding {self.domain}"
                    + (f" ({holder})." if holder else ".")
                    + " Wait for it to finish, or say 'stop' to cancel it."
                )
            time.sleep(0.1)

    def release(self) -> None:
        with self._local:
            self._held = False
            self._owner = ""
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

    def holder(self) -> str:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return f"pid {data.get('pid')} {data.get('owner', '')}".strip()
        except Exception:
            return ""

    def __enter__(self) -> "ControlLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ── internals ─────────────────────────────────────────────────────────────

    def _create(self, owner: str) -> None:
        payload = json.dumps({"owner": owner, "pid": os.getpid(),
                              "at": time.time(), "domain": self.domain})
        fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)

    def _break_if_stale(self) -> None:
        try:
            age = time.time() - self._path.stat().st_mtime
        except OSError:
            return
        if age > STALE_SECONDS:
            holder = self.holder()
            try:
                self._path.unlink(missing_ok=True)
                print(f"[CapabilityLock] broke stale {self.domain} lock "
                      f"(held by {holder} for >{STALE_SECONDS / 60:.0f} min)")
            except Exception:
                pass


_registry: dict[str, "ControlLock"] = {}
_registry_guard = threading.Lock()


def get_lock(domain: str) -> ControlLock:
    """Per-domain lock singleton (thread-safe)."""
    key = str(domain or "").strip().lower() or "default"
    with _registry_guard:
        lock = _registry.get(key)
        if lock is None:
            lock = ControlLock(key)
            _registry[key] = lock
    return lock


def desktop_lock() -> ControlLock:
    return get_lock("desktop")


def browser_lock() -> ControlLock:
    return get_lock("browser")


def research_lock() -> ControlLock:
    return get_lock("research")


def release_all(reason: str = "shutdown") -> int:
    """Release every lock this process holds. Returns how many were held."""
    with _registry_guard:
        locks = list(_registry.values())
    freed = 0
    for lock in locks:
        if lock._held:
            try:
                lock.release()
                freed += 1
            except Exception:
                pass
    if freed:
        print(f"[CapabilityLock] released {freed} control lock(s) — {reason}")
    return freed


def status() -> dict[str, dict]:
    """Live lock state for diagnostics and for the router's busy messages."""
    with _registry_guard:
        domains = list(_registry)
    out: dict[str, dict] = {}
    for key in dict.fromkeys([*domains, "desktop", "browser", "research"]):
        lock = get_lock(key)
        out[key] = {"locked": lock.is_locked(), "holder": lock.holder()}
    return out
