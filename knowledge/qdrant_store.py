"""Optional Qdrant adapter (knowledge-retrieval spec).

Only imported when ``features.qdrant_knowledge=true``. Semantic search over
an ingested document collection with source citations. The qdrant-client
package is an optional dependency — an import failure here propagates to the
retriever, which falls back to local search and logs one notice.
"""

from __future__ import annotations

import os
from typing import Any

_COLLECTION = os.environ.get("QDRANT_COLLECTION", "jarvis_knowledge")


def _qdrant_setting(env_name: str, default: str = "") -> str:
    """Env first (deployment override), then config/api_keys.json (API tab)."""
    val = os.environ.get(env_name)
    if val:
        return str(val)
    try:
        from config.settings import get
        return str(get(env_name.lower(), "") or "")
    except Exception:
        return ""


def _client():
    from qdrant_client import QdrantClient  # optional dependency
    url = _qdrant_setting("QDRANT_URL") or "http://localhost:6333"
    api_key = _qdrant_setting("QDRANT_API_KEY") or None
    return QdrantClient(url=url, api_key=api_key, timeout=5)


def search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Semantic search; raises on unavailability (retriever falls back)."""
    client = _client()
    try:
        from qdrant_client.models import Filter
    except Exception:
        Filter = None  # older clients
    # Embedding provider: reuse the memory layer's local embeddings when
    # present (no extra network dependency), else dense-vector is unavailable
    # and the caller falls back to local search.
    try:
        from memory.embeddings import embed
        vector = embed(query)
        if vector is None:
            raise RuntimeError("no embedding provider for the query vector")
    except Exception:
        raise
    hits = client.search(
        collection_name=_COLLECTION, query_vector=list(vector), limit=limit,
    )
    out: list[dict[str, Any]] = []
    for h in hits:
        payload = h.payload or {}
        out.append({
            "source": payload.get("source", ""),
            "passage": payload.get("text", ""),
            "score": float(h.score),
        })
    return out


def ingest(sources: list[str], root: str | os.PathLike = "") -> int:
    """Ingest text documents into the collection. Returns chunks stored.
    Intended for explicit user-triggered indexing, not background work."""
    from knowledge.retriever import _iter_docs, _default_root
    from qdrant_client.models import PointStruct
    client = _client()
    try:
        from memory.embeddings import embed
    except Exception:
        return 0
    base = root or _default_root()
    points, pid = [], 0
    for src in sources or [str(p) for p in _iter_docs(_root(base))]:
        path = Path(src)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for chunk in _chunks(text):
            vec = embed(chunk)
            if vec is None:
                continue
            points.append(PointStruct(
                id=pid, vector=list(vec),
                payload={"source": str(path), "text": chunk[:1200]},
            ))
            pid += 1
    if points:
        client.upsert(collection_name=_COLLECTION, points=points)
    return len(points)


def _root(base):
    from pathlib import Path
    return Path(base)


def _chunks(text: str, size: int = 900):
    words = text.split()
    for i in range(0, len(words), size // 2):
        yield " ".join(words[i:i + size // 2])
        if i > 4000:  # bounded ingest per file
            break
