"""Watching the vault, so memory stays current without a full rescan.

Polling, not a filesystem-events library: a scan compares ``(mtime, size)`` per
note and only reads what actually changed, so a tick over a large vault is a
handful of ``stat`` calls. That is cheap enough to run on a background thread
without adding a dependency, and it works identically on Windows, macOS and
Linux — where the native event APIs and their edge cases differ.

When a change is seen the index is brought up to date and every listener is told
what happened, which is what feeds the "Obsidian Vault — note changed, updating
memory index…" activity line. A scan that finds nothing changed is silent: an
idle vault must not produce a stream of notices.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from memory.vault_store import VaultIndex, get_index

__all__ = ["VaultWatcher", "get_watcher", "reset_watcher"]

#: How often the vault is checked. Fast enough that a note saved in Obsidian
#: appears in memory within a breath, slow enough to be invisible. The live
#: default follows the Performance profile.
INTERVAL_S = 6.0


def _configured_interval(default: float) -> float:
    try:
        from ui_perfomance import effective
        return max(1.0, float(effective().get("vault_watch_s") or default))
    except Exception:
        return max(1.0, float(default))


class VaultWatcher:
    """Watches one vault and keeps its index current."""

    def __init__(self, index: VaultIndex | None = None,
                 interval: float | None = None) -> None:
        self.index = index or get_index()
        if interval is None:
            interval = _configured_interval(INTERVAL_S)
        self.interval = max(1.0, float(interval))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listeners: list[Callable[[dict], None]] = []
        self.last_result: dict = {}

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        if callable(callback) and callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vault-watch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self) -> dict:
        """One scan; notify listeners only when something really changed."""
        result = self.index.scan()
        self.last_result = result
        if not result.get("ok"):
            return result
        changed = (result.get("added", 0) or result.get("updated", 0)
                   or result.get("removed", 0))
        if not changed:
            return result
        for callback in list(self._listeners):
            try:
                callback(result)
            except Exception:
                pass
        return result


_WATCHER: VaultWatcher | None = None
_LOCK = threading.Lock()


def get_watcher(index: VaultIndex | None = None) -> VaultWatcher:
    global _WATCHER
    with _LOCK:
        if _WATCHER is None:
            _WATCHER = VaultWatcher(index=index)
        return _WATCHER


def reset_watcher(watcher: VaultWatcher | None = None) -> None:
    global _WATCHER
    with _LOCK:
        if _WATCHER is not None:
            _WATCHER.stop()
        _WATCHER = watcher
