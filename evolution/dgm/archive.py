"""Every version of every improved capability, with the metrics it earned.

The archive is what makes the DGM layer safe to run: a candidate is promoted only
after the incumbent has been copied here, so "keep the old working version
available for rollback" is a property of the storage rather than a promise in a
docstring.

One pair of files per version, under ``data/evolution/dgm/<name>/``:

* ``<stamp>.py``   — the source exactly as it ran;
* ``<stamp>.json`` — its metrics, verdict and the reason it was archived.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

__all__ = ["latest", "root", "save", "versions"]

_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def root() -> Path:
    """The archive directory (the project's own data dir when it is available)."""
    try:
        from config.settings import data_dir
        base = Path(data_dir())
    except Exception:
        base = Path(__file__).resolve().parents[2] / "data"
    return base / "evolution" / "dgm"


def _safe_name(name: object) -> str:
    clean = str(name or "").strip().lower()
    return clean if _NAME_RE.match(clean) else ""


def save(name: str, source: str, metrics: dict | None = None,
         *, reason: str = "") -> str:
    """Archive one version. Returns the metadata path ('' when it could not)."""
    clean = _safe_name(name)
    if not clean or not str(source or "").strip():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = root() / clean
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{stamp}.py").write_text(str(source), encoding="utf-8")
        meta = {"name": clean, "stamp": stamp, "saved_at": time.time(),
                "reason": str(reason or ""), "metrics": dict(metrics or {})}
        meta_path = folder / f"{stamp}.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return str(meta_path)
    except Exception:
        return ""


def versions(name: str) -> list[dict[str, Any]]:
    """Every archived version of *name*, newest first."""
    clean = _safe_name(name)
    if not clean:
        return []
    folder = root() / clean
    rows: list[dict[str, Any]] = []
    try:
        for meta_path in sorted(folder.glob("*.json"), reverse=True):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(meta, dict):
                meta["source_path"] = str(meta_path.with_suffix(".py"))
                rows.append(meta)
    except Exception:
        return []
    return rows


def latest(name: str) -> tuple[str, dict] | None:
    """The newest archived ``(source, metrics)`` for *name*, or None."""
    for row in versions(name):
        try:
            source = Path(str(row.get("source_path"))).read_text(encoding="utf-8")
        except Exception:
            continue
        return source, dict(row.get("metrics") or {})
    return None
