"""Memory backend interface (task 6.1) — long-term memory behind one contract.

The default backend is the existing local store (``BrainMemory``); Mem0 is an
optional backend enabled by ``features.mem0_memory`` + an API key. Backends
are swappable without touching callers: everything goes through
``get_memory_backend()``.

Scope of the interface is deliberately small — only *long-term* facts and
recall. Session context, task state and ephemeral turns never reach a backend
(memory-policy spec; see memory/policy.py for the promotion rules).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

#: The two long-term memory backends a user can choose between. Exactly one is
#: active at a time; the choice lives in ``config/api_keys.json`` under
#: ``memory_backend`` so Settings and the runtime cannot disagree.
BACKEND_ORIGINAL = "original"
BACKEND_OBSIDIAN = "obsidian"
BACKENDS = (BACKEND_ORIGINAL, BACKEND_OBSIDIAN)


class MemoryBackend(Protocol):
    """Backend-agnostic long-term memory contract."""

    name: str

    def write(self, content: str, *, category: str = "",
              project_id: str = "", explicit: bool = False) -> bool:
        """Persist one durable fact. Returns True when stored."""
        ...

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Recall relevant facts (most relevant first)."""
        ...

    def available(self) -> bool:
        """False when the backend cannot serve right now (caller falls back)."""
        ...


class LocalMemoryBackend:
    """The existing sqlite + long_term.json store, unchanged."""

    name = "local"

    def __init__(self, memory=None) -> None:
        self._memory = memory

    def _brain_memory(self):
        if self._memory is None:
            from memory.manager import get_brain_memory
            self._memory = get_brain_memory()
        return self._memory

    def write(self, content: str, *, category: str = "",
              project_id: str = "", explicit: bool = False) -> bool:
        try:
            mid = self._brain_memory().remember(
                content, category=category or None,
                project_id=project_id, explicit=explicit,
            )
            return mid is not None
        except Exception:
            return False

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        try:
            mem = self._brain_memory().retrieve(query, limit=limit)
            return list(mem or [])
        except Exception:
            return []

    def available(self) -> bool:
        try:
            return self._brain_memory().is_enabled()
        except Exception:
            return False


_backend: MemoryBackend | None = None
#: Reentrant on purpose: set_memory_backend() resolves the current backend
#: through get_memory_backend() while holding this lock, and a plain Lock
#: deadlocks the very first switch of a fresh process.
_lock = threading.RLock()

#: Why the last activation attempt failed ('' when the active backend is fine).
#: Kept so Settings can say "Obsidian Vault could not be activated" with the
#: reason instead of silently showing the previous backend as if nothing happened.
_activation_error = ""


def selected_backend() -> str:
    """Which backend the user chose (``original`` when unset or unreadable)."""
    try:
        from config.settings import get
        choice = str(get("memory_backend", BACKEND_ORIGINAL) or "").strip().lower()
    except Exception:
        choice = BACKEND_ORIGINAL
    return choice if choice in BACKENDS else BACKEND_ORIGINAL


def active_backend_name() -> str:
    """What is actually serving memory right now (not what was requested)."""
    backend = _backend if _backend is not None else get_memory_backend()
    return str(getattr(backend, "name", BACKEND_ORIGINAL) or BACKEND_ORIGINAL)


def activation_error() -> str:
    return _activation_error


def set_memory_backend(choice: str) -> dict:
    """Switch the active backend safely, verifying the new one before dropping
    the old.

    The sequence is: write the choice → build the candidate → verify it is
    available → adopt it. If the candidate cannot be built or is not available,
    the previous backend stays active and the reason is returned, so a broken
    vault path can never leave J.A.R.V.I.S. with no memory at all.
    """
    global _backend, _activation_error
    wanted = str(choice or "").strip().lower()
    if wanted not in BACKENDS:
        return {"ok": False, "active": active_backend_name(),
                "detail": f"'{choice}' is not a memory backend"}
    with _lock:
        previous = _backend if _backend is not None else get_memory_backend()
        if wanted == BACKEND_OBSIDIAN:
            candidate, problem = _build_obsidian()
            if candidate is None:
                _activation_error = problem
                return {"ok": False, "active": getattr(previous, "name", "local"),
                        "detail": problem}
            persisted = True
            try:
                from config.settings import set_value
                persisted = bool(set_value("memory_backend", BACKEND_OBSIDIAN))
            except Exception:
                persisted = False
            _backend = candidate
            _activation_error = ""
            detail = "Obsidian Vault memory is active."
            if not persisted:
                # A switch that silently forgets itself on restart is worse than
                # a refusal: say so, and keep serving the vault for this session.
                detail += (" The choice could not be written to the config file, "
                           "so it will fall back to the original memory when "
                           "J.A.R.V.I.S. restarts.")
            return {"ok": True, "active": candidate.name, "persisted": persisted,
                    "detail": detail}
        # Back to the original store.
        persisted = True
        try:
            from config.settings import set_value
            persisted = bool(set_value("memory_backend", BACKEND_ORIGINAL))
        except Exception:
            persisted = False
        _backend = LocalMemoryBackend()
        _activation_error = ""
        detail = ("The original memory is active." if persisted else
                  "The original memory is active for this session; the choice "
                  "could not be written to the config file.")
        return {"ok": True, "active": _backend.name, "persisted": persisted,
                "detail": detail}


def _build_obsidian() -> tuple[MemoryBackend | None, str]:
    """Construct + verify the Obsidian backend, or explain why it cannot be."""
    try:
        from memory.obsidian_backend import ObsidianVaultBackend
        candidate = ObsidianVaultBackend()
    except Exception as exc:
        return None, f"the Obsidian backend could not be loaded: {exc}"
    if not candidate.available():
        return None, (candidate.last_error
                      or "the Obsidian Vault could not be activated.")
    return candidate, ""


def _mem0_configured() -> bool:
    try:
        from config.settings import get_feature
        if not bool(get_feature("mem0_memory")):
            return False
        import os
        return bool(os.environ.get("MEM0_API_KEY")
                    or _api_key_from_config())
    except Exception:
        return False


def _api_key_from_config() -> str:
    try:
        from config.settings import get
        return str(get("mem0_api_key", "") or "")
    except Exception:
        return ""


def get_memory_backend() -> MemoryBackend:
    """The active backend: the user's choice, then Mem0, then local.

    Order matters. An explicit choice of the Obsidian vault wins; if that vault
    cannot be opened the local store serves instead and the failure is recorded
    in :func:`activation_error` (never a silently empty memory). With no
    explicit choice, Mem0 takes over when enabled+configured, and the local
    SQLite store is the always-available default.
    """
    global _backend, _activation_error
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        if selected_backend() == BACKEND_OBSIDIAN:
            candidate, problem = _build_obsidian()
            if candidate is not None:
                _backend = candidate
                return _backend
            _activation_error = problem
        if _mem0_configured():
            try:
                from memory.mem0_client import Mem0Backend
                candidate = Mem0Backend(
                    api_key=_api_key_from_config() or None,
                    fallback=LocalMemoryBackend(),
                )
                if candidate.available():
                    _backend = candidate
                    return _backend
            except Exception:
                pass  # fall through to local
        _backend = LocalMemoryBackend()
    return _backend


def reset_memory_backend() -> None:
    """Test/teardown hook."""
    global _backend, _activation_error
    with _lock:
        _backend = None
        _activation_error = ""

# ── the write gateway ─────────────────────────────────────────────────────────
#: Every durable memory reaches its store through here. The original store keeps
#: doing exactly what it always did; this is the one place that also feeds the
#: active backend, so the Obsidian vault (or any future backend) receives the
#: same facts without every caller learning a second API.

def active_backend_label() -> str:
    """A human label for the store serving memory right now."""
    return ("Obsidian Vault" if active_backend_name() == BACKEND_OBSIDIAN
            else "Original Memory")


def store_in_backend(content: str, *, category: str = "", explicit: bool = False,
                     source: str = "", confidence: float = 0.0,
                     project_id: str = "", local_stored: bool = True) -> dict:
    """Record one durable fact in the ACTIVE backend, and count the growth.

    The original store is written by the caller exactly as before; this function
    exists so a non-local backend receives the same fact. It never raises, and it
    only records growth when a store genuinely accepted the fact, so "stronger
    every time he remembers" stays honest on both backends.
    """
    text = " ".join(str(content or "").split()).strip()
    if not text:
        return {"stored": False, "backend": active_backend_name(), "detail": "empty"}
    name = active_backend_name()
    # The local store answers to both names: "local" is the backend's own name
    # and "original" is the setting the user chose. Treating only one of them as
    # local would double-write every fact into the SQLite store.
    if name in (BACKEND_ORIGINAL, "local"):
        if not local_stored:
            return {"stored": False, "backend": name,
                    "detail": "the original memory declined this fact"}
        try:
            from memory import growth
            stats = growth.record(backend=name, category=category)
        except Exception:
            stats = {}
        return {"stored": True, "backend": name,
                "detail": "stored in the original memory", "growth": stats}
    try:
        be = get_memory_backend()
    except Exception as exc:
        return {"stored": False, "backend": name, "detail": f"no backend: {exc}"}
    try:
        try:
            ok = bool(be.write(text, category=category, project_id=project_id,
                               explicit=explicit, source=source,
                               confidence=confidence))
        except TypeError:
            # A backend built to the minimal contract takes only the four
            # required keyword arguments.
            ok = bool(be.write(text, category=category, project_id=project_id,
                               explicit=explicit))
    except Exception as exc:
        return {"stored": False, "backend": name,
                "detail": f"{name} refused the write: {exc}"}
    if not ok:
        return {"stored": False, "backend": name,
                "detail": getattr(be, "last_error", "")
                          or "the backend declined this fact"}
    try:
        from memory import growth
        stats = growth.record(backend=name, category=category)
    except Exception:
        stats = {}
    return {"stored": True, "backend": name, "detail": "", "growth": stats}


#: Recall is half of memory: using a stored fact again strengthens the core.
#: Throttled so prompt assembly cannot inflate the meter with one lookup per
#: turn — at most one reinforcement per 15 seconds, whatever calls this.
_LAST_RECALL_REINFORCE = 0.0


def recall_backend(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Recall through the active backend (never raises)."""
    try:
        hits = list(get_memory_backend().search(str(query or ""), limit=limit) or [])
    except Exception:
        return []
    if hits:
        global _LAST_RECALL_REINFORCE
        now = time.time()
        if now - _LAST_RECALL_REINFORCE >= 15.0:
            _LAST_RECALL_REINFORCE = now
            try:
                from memory import growth
                growth.reinforce(amount=1)
            except Exception:
                pass
    return hits


def forget_in_backend(query: str, hard: bool = False) -> int:
    """Remove matching facts from the active backend when it supports it."""
    try:
        be = get_memory_backend()
        fn = getattr(be, "forget", None)
        if callable(fn):
            return int(fn(str(query or ""), hard=hard) or 0)
    except Exception:
        pass
    return 0


def test_roundtrip() -> dict:
    """Write a probe fact through the active backend, then recall it.

    Settings ▸ MEMORY's Test Memory button reports the store that actually
    served the write — never the one that was merely selected.
    """
    import time as _time
    probe = f"JARVIS memory self-test {int(_time.time())}"
    result = store_in_backend(probe, category="CONTEXT", explicit=True,
                              source="self-test", confidence=0.9)
    if not result.get("stored"):
        return {"ok": False, "backend": active_backend_label(),
                "detail": result.get("detail") or "the write failed"}
    hits = recall_backend(probe, limit=3)
    if not hits:
        return {"ok": False, "backend": active_backend_label(),
                "detail": "the fact was written but could not be recalled"}
    return {"ok": True, "backend": active_backend_label(),
            "detail": f"stored and recalled from {active_backend_label()}",
            "hits": len(hits)}
