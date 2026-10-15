"""Memory growth — why J.A.R.V.I.S. is stronger after every memory.

The user's ask: "every time he remembers something he gets smarter and
stronger". That has to mean something real, so it is one honest counter:

* every durable fact that is stored is a **write** (the strength of the core),
* every fact that is recalled or re-affirmed is a **reinforcement** (worth
  half — repeating a thing is weaker evidence than stating it),
* level and strength are deterministic functions of those two numbers, so the
  same total always shows the same level and nothing is invented.

The record lives beside the other runtime data (``data/memory/growth.json``)
and is backend-agnostic: the same growth is counted whether the fact went into
the original store or the Obsidian vault, so switching backends never resets
how far J.A.R.V.I.S. has come. Nothing here raises: growth must never fail a
memory.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

__all__ = ["record", "reinforce", "snapshot", "reset", "growth_path"]

_LOCK = threading.RLock()

#: 400 points fills the strength meter: a real, reachable target rather than an
#: ever-retreating one.
_FULL_STRENGTH = 400.0


def _data_dir() -> Path:
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent / "data"


def growth_path() -> Path:
    """Where the growth record lives (folder created on demand)."""
    target = _data_dir() / "memory"
    target.mkdir(parents=True, exist_ok=True)
    return target / "growth.json"


_DEFAULT: dict = {
    "writes": 0,
    "reinforcements": 0,
    "backends": {},      # backend name -> writes counted there
    "categories": {},    # category -> writes
    "started": 0.0,
    "updated": 0.0,
}


def _read() -> dict:
    try:
        raw = json.loads(growth_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return dict(_DEFAULT)
        out = dict(_DEFAULT)
        out.update(raw)
        for key in ("backends", "categories"):
            if not isinstance(out.get(key), dict):
                out[key] = {}
        return out
    except Exception:
        return dict(_DEFAULT)


def _write(data: dict) -> None:
    """Atomic write: a half-written growth file must never be readable."""
    try:
        path = growth_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _points(data: dict) -> int:
    return int(data.get("writes") or 0) + int(int(data.get("reinforcements") or 0) * 0.5)


def _level(points: int) -> int:
    """Deterministic level: the first write makes level 2, then sqrt steps."""
    return 1 + int(max(0, points) ** 0.5)


def _strength(points: int) -> float:
    """0.0–1.0 indicator of how much the core has learned."""
    return round(min(1.0, max(0.0, points) / _FULL_STRENGTH), 4)


def _bump(*, kind: str, backend: str = "", category: str = "", amount: int = 1) -> dict:
    with _LOCK:
        data = _read()
        now = time.time()
        if not data.get("started"):
            data["started"] = now
        data["updated"] = now
        if kind == "reinforce":
            data["reinforcements"] = int(data.get("reinforcements") or 0) + max(1, int(amount))
        else:
            data["writes"] = int(data.get("writes") or 0) + max(1, int(amount))
            name = str(backend or "local").strip() or "local"
            backends = dict(data.get("backends") or {})
            backends[name] = int(backends.get(name) or 0) + 1
            data["backends"] = backends
            cat = str(category or "").strip().upper()
            if cat:
                cats = dict(data.get("categories") or {})
                cats[cat] = int(cats.get(cat) or 0) + 1
                data["categories"] = cats
        _write(data)
        return _snapshot_of(data)


def record(*, backend: str = "local", category: str = "") -> dict:
    """Count one durable fact that was really stored."""
    return _bump(kind="write", backend=backend, category=category)


def reinforce(*, backend: str = "", amount: int = 1) -> dict:
    """Count a fact that was recalled or re-affirmed."""
    return _bump(kind="reinforce", backend=backend, amount=amount)


def _snapshot_of(data: dict) -> dict:
    points = _points(data)
    return {
        "writes": int(data.get("writes") or 0),
        "reinforcements": int(data.get("reinforcements") or 0),
        "points": points,
        "level": _level(points),
        "strength": _strength(points),
        "backends": dict(data.get("backends") or {}),
        "categories": dict(data.get("categories") or {}),
        "updated": float(data.get("updated") or 0.0),
    }


def snapshot() -> dict:
    """The current growth state (never raises)."""
    with _LOCK:
        return _snapshot_of(_read())


def reset() -> None:
    """Test/teardown hook, and the Reset-data path."""
    with _LOCK:
        _write(dict(_DEFAULT))
