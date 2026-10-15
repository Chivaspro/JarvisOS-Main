"""Face detection + recognition, fully local by default.

Approach
--------
* **Detection** uses MediaPipe FaceMesh / Face Detection (bundled models, no
  downloads, Apache-2.0).
* **Recognition** defaults to a *geometric fingerprint*: the 468 face-mesh
  landmarks are normalised and encoded into a compact descriptor, so identity
  comparison never leaves the machine and never touches a cloud API.  This is
  a lightweight, local-only solution (good enough for a personal assistant's
  "who is that?"), not a forensic matcher.
* Optional backend ``insightface`` is honoured when the user installs it
  (`pip install insightface onnxruntime`) and sets ``face_backend =
  "insightface"`` — sensible when heavier accuracy is wanted.

Storage
-------
Registered identities live under ``config/vision.face_database_dir`` (default
``data/faces``): a ``db.npz`` with the descriptor matrix plus a
``records.json`` with names + thumbnails.  Only a normalised numeric
descriptor and a thumbnail are stored — no raw biometric payloads.

Recognition avoids hammering itself: within a session a matched identity is
remembered for a few seconds before being re-evaluated.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from vision.config import VisionConfig
from vision.models import FaceLand, FaceResult, Rect
from vision.utils import encode_jpeg, is_package_available, logger

try:
    import cv2
    _CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _CV2 = False

try:
    import mediapipe as mp
    _MP = True
except Exception:  # pragma: no cover
    mp = None
    _MP = False


# Landmarks used for the geometric fingerprint: eyes, nose, mouth, jaw edges.
_FP_POINTS = [
    33, 133, 159, 145, 263, 362, 386, 374,      # eyes
    1, 4, 98, 327, 168, 6,                       # nose / glabella
    61, 291, 0, 17, 37, 267,                     # mouth + corners
    234, 454, 132, 361,                          # lower face / jaw top cheeks
    93, 187, 301,                                   # mid face
    205, 425, 152,                                 # philtrum + chin
]


class FaceDatabase:
    """Minimal local identity store (descriptors + metadata)."""

    def __init__(self, directory: Path):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._embeddings: Optional[np.ndarray] = None   # (N, D)
        self._names: List[str] = []
        self._meta: List[Dict] = []
        self._reload()

    def _db_file(self) -> Path:
        return self._dir / "db.npz"

    def _meta_file(self) -> Path:
        return self._dir / "records.json"

    def _reload(self) -> None:
        try:
            if self._db_file().exists():
                data = np.load(self._db_file(), allow_pickle=False)
                self._embeddings = data["embeddings"].astype(np.float32)
                self._names = [str(x) for x in data["names"]]
        except Exception as exc:
            logger.warn(f"face db reload failed: {exc}")
            self._embeddings = None
            self._names = []
        self._meta = []
        try:
            if self._meta_file().exists():
                self._meta = json.loads(self._meta_file().read_text(encoding="utf-8"))
        except Exception:
            self._meta = []

    def names(self) -> List[str]:
        with self._lock:
            return list(self._names)

    def count(self) -> int:
        with self._lock:
            return len(self._names)

    def add(self, name: str, embedding: np.ndarray, thumbnail: bytes,
            extra: Optional[dict] = None) -> bool:
        if embedding is None or embedding.size == 0:
            return False
        norm = np.asarray(embedding, dtype=np.float32).reshape(1, -1)
        with self._lock:
            existing = self._names
            if name in existing:
                idx = existing.index(name)
                if self._embeddings is None:
                    self._embeddings = norm
                else:
                    if idx == 0 and len(existing) == 1:
                        self._embeddings = norm
                    elif 0 <= idx < len(self._embeddings):
                        self._embeddings[idx] = norm[0]
                self._names = list(self._names)
            else:
                if self._embeddings is None:
                    self._embeddings = norm
                else:
                    self._embeddings = np.vstack([self._embeddings, norm[0]])
                self._names.append(name)
            self._flush_locked()
            meta_entry = {"name": name, "ts": time.time(),
                          "thumb": _b64(thumbnail)}
            self._meta = [m for m in self._meta if m.get("name") != name]
            self._meta.append(meta_entry)
            try:
                self._meta_file().write_text(json.dumps(self._meta, indent=2),
                                             encoding="utf-8")
            except Exception:
                pass
            return True

    def _flush_locked(self) -> None:
        try:
            if self._embeddings is None:
                return
            np.savez(self._db_file(),
                     embeddings=self._embeddings.astype(np.float32),
                     names=np.asarray(self._names, dtype=object))
        except Exception as exc:
            logger.error(f"face db save failed: {exc}")

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self._names:
                return False
            idx = self._names.index(name)
            self._names.pop(idx)
            if self._embeddings is not None and len(self._embeddings) > 0:
                self._embeddings = np.delete(self._embeddings, idx, axis=0)
            if len(self._names) == 0:
                self._embeddings = None
            self._flush_locked()
            self._meta = [m for m in self._meta if m.get("name") != name]
            try:
                self._meta_file().write_text(json.dumps(self._meta, indent=2),
                                             encoding="utf-8")
            except Exception:
                pass
            return True

    def match(self, embedding: np.ndarray,
              threshold: float) -> Tuple[Optional[str], Optional[float]]:
        """Return (name|None, distance). Distance = cosine distance."""
        if embedding is None or embedding.size == 0:
            return None, None
        with self._lock:
            if self._embeddings is None or len(self._embeddings) == 0:
                return None, None
            vec = np.asarray(embedding, dtype=np.float32).reshape(1, -1)
            denom = np.linalg.norm(vec) * np.linalg.norm(self._embeddings, axis=1)
            denom = np.maximum(denom, 1e-9)
            sims = (self._embeddings @ vec.T).ravel() / denom
            sims = np.clip(sims, -1.0, 1.0)
            distances = 1.0 - sims
            best_i = int(np.argmin(distances))
            best_d = float(distances[best_i])
            if best_d <= threshold:
                return self._names[best_i], best_d
            return None, best_d

    def thumbnails(self) -> List[dict]:
        with self._lock:
            return list(self._meta)


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


class FaceRecognizer:
    """Detect + recognise faces in BGR frames."""

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._db = FaceDatabase(cfg.face_db_path)
        self._mesh = None
        self._face_det = None
        self._lock = threading.Lock()
        self._backend = cfg.face_backend or "geometry"

        self._known_cache: Dict[int, Tuple[str, float]] = {}   # box key → name
        self._cache_ts: Dict[int, float] = {}
        self._cache_dur = 2.5                                   # seconds

        self._insight = None
        self._insight_detector = None
        self._load()

    # ── model loading ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not _MP:
            logger.warn("mediapipe missing — face recognition limited to "
                        "OpenCV Haar detection (no identity matching)")
            return
        try:
            self._face_det = mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=0.4)
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=True, max_num_faces=8,
                refine_landmarks=False, min_detection_confidence=0.4)
        except Exception as exc:
            logger.warn(f"mediapipe face models failed: {exc}")
            self._face_det = None
            self._mesh = None

        if self._backend == "insightface" and is_package_available("insightface"):
            try:
                import insightface
                self._insight = insightface.app.FaceAnalysis(name="buffalo_l")
                self._insight.prepare(ctx_id=0 if self._cfg.inference_device == "cuda" else -1)
                self._insight_detector = self._insight
                logger.info("face recognition backend: insightface")
                return
            except Exception as exc:
                logger.warn(f"insightface failed: {exc}; using geometry backend")
        self._backend = "geometry"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def db(self) -> FaceDatabase:
        return self._db

    def reload(self, cfg: VisionConfig) -> None:
        self._cfg = cfg
        self._db = FaceDatabase(cfg.face_db_path)

    # ── public API ──────────────────────────────────────────────────────────

    def detect_faces(self, frame_bgr) -> List[FaceResult]:
        """Detect faces (no identity lookup). Robust: never raises."""
        try:
            if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
                return []
            if not _MP:
                return self._detect_haar(frame_bgr)
            results = []
            if self._face_det is not None:
                rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                det = self._face_det.process(rgb)
                for i, fd in enumerate(det.detections or []):
                    bbox = fd.location_data.relative_bounding_box
                    results.append(FaceResult(
                        rect=Rect(x=max(0.0, bbox.xmin), y=max(0.0, bbox.ymin),
                                  w=min(1.0, bbox.width), h=min(1.0, bbox.height)),
                        confidence=float(getattr(fd, "score", [0])[0]) if hasattr(fd, "score") else 0.5,
                        distance=0.5, matched=False,
                    ))
            if not results:
                results = self._detect_haar(frame_bgr)
            return results
        except Exception as exc:
            logger.debug(f"face detect error: {exc}")
            return []

    def _detect_haar(self, frame_bgr) -> List[FaceResult]:
        if not _CV2:
            return []
        try:
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            faces = cascade.detectMultiScale(gray, 1.15, 5, minSize=(48, 48))
            h, w = frame_bgr.shape[:2]
            return [FaceResult(rect=Rect(x=fx / w, y=fy / h, w=fw / w, h=fh / h),
                               confidence=0.5, matched=False)
                    for (fx, fy, fw, fh) in faces]
        except Exception:
            return []

    def recognize(self, frame_bgr) -> List[FaceResult]:
        """Detect + resolve identities. Cached per-box to avoid spam."""
        try:
            # Fast path: also derive identities from geometry fingerprints.
            faces = self.detect_faces(frame_bgr)
            if not faces:
                return []
            if not self._cfg.face_recognition_enabled:
                return faces
            now = time.monotonic()
            for face in faces:
                key = (int(face.rect.x * 100), int(face.rect.y * 100),
                        int(face.rect.w * 100), int(face.rect.h * 100))
                cached = self._known_cache.get(key)
                if cached is not None and now - self._cache_ts.get(key, 0) < self._cache_dur:
                    face.identity, face.distance = cached
                    face.matched = face.identity is not None
                    continue
                identity, distance = self._resolve_identity(frame_bgr, face)
                face.identity, face.distance = identity, distance
                face.matched = identity is not None
                if len(self._known_cache) > 24:
                    self._known_cache = {}
                self._known_cache[key] = (identity, distance)
                self._cache_ts[key] = now
                if identity is not None:
                    logger.debug(f"face recognition: matched {identity} ({distance:.2f})")
            return faces
        except Exception as exc:
            logger.warn(f"face recognition error: {exc}")
            return []

    def _resolve_identity(self, frame_bgr, face: FaceResult):
        if self._backend == "insightface" and self._insight_detector is not None:
            try:
                faces = self._insight_detector.get(frame_bgr)
                if faces:
                    f = faces[0]
                    emb = np.asarray(f.embedding, dtype=np.float32).reshape(1, -1)
                    threshold = self._cfg.face_similarity_threshold * 0.45
                    name, dist = self._db.match(emb, threshold=threshold)
                    return name, dist
            except Exception as exc:
                logger.debug(f"insightface resolve error: {exc}")
                return None, None
        # geometry backend
        emb = self._embed(frame_bgr, face)
        if emb is None:
            return None, None
        name, dist = self._db.match(emb, threshold=self._cfg.face_similarity_threshold)
        return name, dist

    def _landmarks_for(self, frame_bgr, face: FaceResult) -> Optional[List[FaceLand]]:
        if self._mesh is None:
            return None
        try:
            h, w = frame_bgr.shape[:2]
            x0 = max(0, int(face.rect.x * w)); y0 = max(0, int(face.rect.y * h))
            x1 = min(w, int((face.rect.x + face.rect.w) * w))
            y1 = min(h, int((face.rect.y + face.rect.h) * h))
            if x1 - x0 < 24 or y1 - y0 < 24:
                return None
            crop = frame_bgr[y0:y1, x0:x1]
            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            res = self._mesh.process(rgb)
            if not res.multi_face_landmarks:
                return None
            lm = res.multi_face_landmarks[0]
            return [FaceLand(x=l.x, y=l.y, z=l.z) for l in lm.landmark]
        except Exception:
            return None

    def _embed(self, frame_bgr, face: FaceResult) -> Optional[np.ndarray]:
        """Geometric fingerprint from face-mesh landmarks.

        Encodes the normalised landmark positions of a stable subset plus
        their pairwise distances → a robust scale/pose-robust descriptor.
        """
        lm = self._landmarks_for(frame_bgr, face)
        if lm is None or len(lm) < 50:
            return None
        pts = np.array([[p.x, p.y, p.z] for p in lm], dtype=np.float32)
        # Normalise translation via centroid, scale via inter-eye distance.
        left = pts[33]; right = pts[263]
        eye_dist = float(np.linalg.norm(right[:2] - left[:2]))
        if eye_dist < 1e-6:
            return None
        centered = (pts[:, :2] - pts[168][:2]) / eye_dist
        idx = [i for i in _FP_POINTS if i < len(centered)]
        sel = centered[idx]
        # Pairwise distances of the salient points give a strong descriptor.
        from itertools import combinations
        pairs = list(combinations(range(len(idx)), 2))[::6]
        dists = np.array([np.linalg.norm(sel[a] - sel[b]) for a, b in pairs],
                         dtype=np.float32)
        flat = np.concatenate([sel.reshape(-1), dists]).astype(np.float32)
        n = np.linalg.norm(flat)
        if n < 1e-9:
            return None
        return (flat / n).reshape(1, -1)

    def register(self, frame_bgr, name: str) -> bool:
        """Register the largest detected face under `name`."""
        faces = self.detect_faces(frame_bgr)
        if not faces:
            return False
        face = max(faces, key=lambda f: f.rect.area())
        emb = self._embed(frame_bgr, face)
        thumb = b""
        try:
            h, w = frame_bgr.shape[:2]
            x0 = max(0, int(face.rect.x * w)); y0 = max(0, int(face.rect.y * h))
            x1 = min(w, int((face.rect.x + face.rect.w) * w))
            y1 = min(h, int((face.rect.y + face.rect.h) * h))
            crop = frame_bgr[y0:y1, x0:x1]
            if crop.size > 0:
                thumb = encode_jpeg(crop, quality=70, max_width=160)
        except Exception:
            pass
        if emb is None:
            return False
        return self._db.add(name, emb, thumb)

    def who_is_there(self, frame_bgr) -> List[Tuple[Optional[str], float, Rect, float]]:
        """Handy summary: [(name|None, distance, rect, confidence)]. Never raises."""
        try:
            out = []
            for f in self.recognize(frame_bgr):
                out.append((f.identity, f.distance, f.rect, f.confidence))
            return out
        except Exception as exc:
            logger.warn(f"who_is_there failed: {exc}")
            return []


__all__ = ["FaceRecognizer", "FaceDatabase"]