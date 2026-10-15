"""Mem0 adapter (task 6.3) — optional long-term memory backend.

Enabled only when ``features.mem0_memory=true`` and a Mem0 API key exists;
otherwise the local store serves everything and this module is never
imported. On any Mem0 failure the backend falls back to the local store for
that operation and logs the degradation **once per session** (not per call —
a red line per call would spam the log during an outage).

Session-scoped context stays local by design: Mem0 receives only promoted
long-term facts (memory-policy spec).
"""

from __future__ import annotations

import threading
from typing import Any

_degrade_lock = threading.Lock()
_degraded_logged = False


def _log_degradation_once(reason: str) -> None:
    global _degraded_logged
    with _degrade_lock:
        if _degraded_logged:
            return
        _degraded_logged = True
    try:
        from observability.logger import log_event
        log_event("WARNING", "memory", "mem0 unavailable; using local store",
                  error=reason)
    except Exception:
        try:
            print(f"[Memory] Mem0 unavailable, using local store ({reason})")
        except Exception:
            pass


def reset_degradation_flag() -> None:
    """Test hook."""
    global _degraded_logged
    with _degrade_lock:
        _degraded_logged = False


class Mem0Backend:
    """Mem0 long-term memory with per-operation local fallback."""

    name = "mem0"

    def __init__(self, api_key: str | None = None,
                 fallback: Any = None, user_id: str = "default") -> None:
        import os
        self._api_key = api_key or os.environ.get("MEM0_API_KEY", "")
        self._user_id = os.environ.get("MEM0_USER_ID", user_id)
        self._fallback = fallback
        self._client = None

    def _mem0(self):
        if self._client is not None:
            return self._client
        try:
            from mem0 import MemoryClient  # lazy: only when Mem0 is enabled
            self._client = MemoryClient(api_key=self._api_key)
        except Exception as exc:
            _log_degradation_once(f"client init failed: {exc}")
            self._client = False
        return self._client or None

    def available(self) -> bool:
        return self._mem0() is not None

    def write(self, content: str, *, category: str = "",
              project_id: str = "", explicit: bool = False) -> bool:
        client = self._mem0()
        if client is None:
            return self._fallback_write(content, category=category,
                                        project_id=project_id, explicit=explicit)
        try:
            metadata = {"category": category} if category else {}
            if project_id:
                metadata["project_id"] = project_id
            client.add([{"role": "user", "content": content}],
                       user_id=self._user_id, metadata=metadata or None)
            return True
        except Exception as exc:
            _log_degradation_once(f"write failed: {exc}")
            return self._fallback_write(content, category=category,
                                        project_id=project_id, explicit=explicit)

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        client = self._mem0()
        if client is None:
            return self._fallback_search(query, limit)
        try:
            hits = client.search(query, user_id=self._user_id, limit=limit) or []
            out: list[dict[str, Any]] = []
            for h in hits:
                if isinstance(h, str):
                    h = {"content": h}
                out.append({
                    "content": h.get("memory") or h.get("content", ""),
                    "category": (h.get("metadata") or {}).get("category", ""),
                    "score": h.get("score", 0.0),
                    "backend": "mem0",
                })
            return out
        except Exception as exc:
            _log_degradation_once(f"search failed: {exc}")
            return self._fallback_search(query, limit)

    # ── local fallback passthrough ────────────────────────────────────────────

    def _fallback_write(self, content: str, **kw) -> bool:
        try:
            return bool(self._fallback.write(content, **kw))
        except Exception:
            return False

    def _fallback_search(self, query: str, limit: int) -> list[dict]:
        try:
            return list(self._fallback.search(query, limit=limit))
        except Exception:
            return []
