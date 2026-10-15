"""Optional semantic embeddings for brain memory.

Kept intentionally dependency-light:
* If sentence-transformers is installed, real embeddings are produced.
* Otherwise a deterministic character-ngram hash vector is used, which is
  cheap, offline and sufficient for near-duplicate detection.
The storage format is a compact bytes blob in the memories.embedding column.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Optional

_DIM = 96
_encoder = None
_loaded = False


def _load_encoder():
    """Lazily try to load sentence-transformers once. Never raises."""
    global _encoder, _loaded
    if _loaded:
        return _encoder
    _loaded = True
    try:
        if os.environ.get("JARVIS_NO_EMBEDDINGS"):
            return None
        from sentence_transformers import SentenceTransformer  # type: ignore

        _encoder = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception:
        _encoder = None
    return _encoder


def _hash_vector(text: str) -> bytes:
    vec = [0.0] * _DIM
    norm = " ".join((text or "").lower().split())
    for i in range(len(norm) - 1):
        chunk = norm[i:i + 3] or norm[i:i + 2] or norm[i]
        idx = int(hashlib.md5(chunk.encode("utf-8", "ignore")).hexdigest()[:8], 16) % _DIM
        vec[idx] += 1.0
    mag = math.sqrt(sum(v * v for v in vec)) or 1.0
    vals = [round(v / mag, 6) for v in vec]
    return b"h" + b"".join(
        int(round((v + 1.0) * 500)).to_bytes(2, "big") for v in vals[:64]
    )


def embed(text: str) -> bytes:
    enc = _load_encoder()
    if enc is not None:
        try:
            v = enc.encode(text[:512])  # keep embedding cheap for huge strings
            vals = v.tolist()[:64]
            mag = math.sqrt(sum(x * x for x in vals)) or 1.0
            vals = [x / mag for x in vals]
            return b"s" + b"".join(
                int(round((min(1.0, max(-1.0, x)) + 1.0) * 500)).to_bytes(2, "big")
                for x in vals
            )
        except Exception:
            pass
    return _hash_vector(text)


def cosine_similarity(a: bytes, b: bytes) -> float:
    """Approximate cosine from the packed int16 representation. Returns 0 for
    mismatched/unknown formats instead of raising."""
    if not a or not b or a[0] != b[0] or len(a) != len(b):
        return 0.0
    av = [int.from_bytes(a[i:i + 2], "big") - 500 for i in range(1, len(a), 2)]
    bv = [int.from_bytes(b[i:i + 2], "big") - 500 for i in range(1, len(b), 2)]
    dot = sum(x * y for x, y in zip(av, bv))
    na = math.sqrt(sum(x * x for x in av)) or 1.0
    nb = math.sqrt(sum(y * y for y in bv)) or 1.0
    return max(-1.0, min(1.0, dot / (na * nb)))