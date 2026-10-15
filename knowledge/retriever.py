"""Knowledge retrieval (task 7.1) — local search by default, Qdrant optional.

* Default (no vector store configured): deterministic local search over the
  repository/documents tree — token scoring, zero dependencies, identical to
  the pre-change behavior of "search my files" tools.
* Optional (``features.qdrant_knowledge=true`` + Qdrant reachable): semantic
  retrieval with source citations back to the original files.

Backend failure → local fallback + one degradation notice; the user's request
never fails because of the knowledge layer.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

_DOC_EXTENSIONS = {
    ".md", ".txt", ".rst", ".py", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".html", ".css", ".js", ".ts", ".pdf", ".docx",
}
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              "data", "config", "openspec"}

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
        log_event("WARNING", "knowledge", "vector store unreachable; local search active",
                  error=reason)
    except Exception:
        pass


def _qdrant_enabled() -> bool:
    try:
        from config.settings import get_feature
        return bool(get_feature("qdrant_knowledge"))
    except Exception:
        return False


# ── local (default) backend ───────────────────────────────────────────────────

def _iter_docs(root: Path, max_files: int = 2000):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in _DOC_EXTENSIONS:
                yield Path(dirpath) / name
                count += 1
                if count >= max_files:
                    return


def _score_tokens(query_tokens: list[str], text: str) -> int:
    low = text.lower()
    return sum(1 for t in query_tokens if t and t in low)


def local_search(query: str, root: str | Path = "", limit: int = 8) -> list[dict[str, Any]]:
    """Deterministic local search: scan documents, score token overlap,
    return the best passages with their source paths. Never raises."""
    base = Path(root) if root else _default_root()
    tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2][:12]
    if not tokens:
        return []
    hits: list[dict[str, Any]] = []
    try:
        for path in _iter_docs(base):
            try:
                if path.suffix.lower() in (".pdf", ".docx"):
                    continue  # binary docs need the vector backend; skip honestly
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            score = _score_tokens(tokens, text)
            if score <= 0:
                continue
            passage = _best_window(text, tokens)
            hits.append({"source": str(path), "score": score, "passage": passage})
            if len(hits) > limit * 6:
                break
    except Exception:
        return []
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def _best_window(text: str, tokens: list[str], width: int = 240) -> str:
    low = text.lower()
    best_pos, best_score = 0, -1
    step = max(1, len(text) // 200)
    for pos in range(0, max(1, len(text) - width), step):
        score = _score_tokens(tokens, low[pos:pos + width])
        if score > best_score:
            best_score, best_pos = score, pos
    snippet = " ".join(text[best_pos:best_pos + width].split())
    return snippet[:width]


def _default_root() -> Path:
    try:
        from config.settings import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parents[1]


# ── optional Qdrant backend ───────────────────────────────────────────────────

def qdrant_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Semantic retrieval from Qdrant; falls back to local on any failure."""
    try:
        from knowledge.qdrant_store import search as q_search
        return q_search(query, limit=limit)
    except Exception as exc:
        _log_degradation_once(str(exc))
        return local_search(query, limit=limit)


# ── public entry points ───────────────────────────────────────────────────────

def knowledge_search(query: str, root: str | Path = "", limit: int = 8) -> dict[str, Any]:
    """Search the knowledge corpus. Returns {backend, results: [{source,
    passage, score}]}. Local by default; Qdrant only when enabled."""
    if _qdrant_enabled():
        results = qdrant_search(query, limit=limit)
        backend = "qdrant"
        if not results:
            results = local_search(query, root, limit=limit)
            backend = "qdrant→local"
    else:
        results = local_search(query, root, limit=limit)
        backend = "local"
    return {"backend": backend, "query": query, "results": results}


def knowledge_status() -> dict[str, Any]:
    try:
        from config.settings import get_feature
        enabled = bool(get_feature("qdrant_knowledge"))
    except Exception:
        enabled = False
    return {"qdrant_enabled": enabled, "default_backend": "local"}


def knowledge_check() -> "Check":
    """Diagnostics Check for the knowledge layer."""
    try:
        from core.diagnostics import Check, ONLINE, DISABLED
        st = knowledge_status()
        if st["qdrant_enabled"]:
            return Check("knowledge", ONLINE, "backend=qdrant (local fallback)", "")
        return Check("knowledge", ONLINE, "backend=local file search", "")
    except Exception as exc:
        from core.diagnostics import Check, ERROR
        return Check("knowledge", ERROR, f"{type(exc).__name__}: {exc}", "")
