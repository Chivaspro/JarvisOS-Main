"""Shared data structures for the JARVIS vision subsystem.

Every class here is a plain :mod:`dataclasses` value container with no
dependency on Qt, OpenCV, or any heavyweight library — call modules build
and consume these freely without import cycles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ── Enums ──────────────────────────────────────────────────────────────────

class VisionStage(str, Enum):
    OBJECT   = "object"
    FACE     = "face"
    HAND     = "hand"
    GESTURE  = "gesture"
    MULTIMODAL = "multimodal"


class GestureKind(str, Enum):
    OPEN_PALM     = "open_palm"
    FIST          = "fist"
    POINT         = "point"
    THUMBS_UP     = "thumbs_up"
    THUMBS_DOWN   = "thumbs_down"
    PEACE         = "peace"
    OK            = "ok"
    PINCH         = "pinch"
    STOP          = "stop"
    UNKNOWN       = "unknown"


class Handedness(str, Enum):
    LEFT  = "Left"
    RIGHT = "Right"
    NONE  = "None"


# ── Core results ───────────────────────────────────────────────────────────

@dataclass
class Rect:
    """Normalised bounding box in ``[0..1]`` range (relative to image dimensions)."""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0

    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def center(self) -> Tuple[float, float]:
        return self.x + self.w * 0.5, self.y + self.h * 0.5

    def overlaps(self, other: "Rect", min_iou: float = 0.1) -> bool:
        ox = max(self.x, other.x)
        oy = max(self.y, other.y)
        ox2 = min(self.x + self.w, other.x + other.w)
        oy2 = min(self.y + self.h, other.y + other.h)
        inter = max(0.0, ox2 - ox) * max(0.0, oy2 - oy)
        union = self.area() + other.area() - inter
        return (inter / max(1e-9, union)) >= min_iou if union > 0 else False

    def contains_point(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


@dataclass
class Detection:
    """Single local object-detection output."""
    label: str
    confidence: float
    rect: Rect
    class_id: int = -1
    tracker_id: Optional[int] = None
    source: str = ""        # "mediapipe" | "ultralytics" | "opencv" | "ai"


@dataclass
class HandLand:
    """3-D landmark produced by MediaPipe Hands / Hand Landmarker."""
    x: float = 0.0  # normalised 0..1
    y: float = 0.0
    z: float = 0.0
    index: int = 0


@dataclass
class Hand:
    """Complete hand result."""
    handedness: Handedness = Handedness.NONE
    landmarks: List[HandLand] = field(default_factory=list)
    rect: Rect = field(default_factory=Rect)
    confidence: float = 0.0

    def wrist(self) -> Optional[HandLand]:
        return self.landmarks[0] if self.landmarks else None

    def tip(self, finger: str) -> Optional[HandLand]:
        """Return a named landmark (``"index_finger_tip"``, ``"thumb_tip"``, …)."""
        idx_map = {"thumb_tip": 4, "index_tip": 8, "middle_tip": 12,
                   "ring_tip": 16, "pinky_tip": 20,
                   "index_mcp": 5, "middle_mcp": 9, "ring_mcp": 13, "pinky_mcp": 17}
        i = idx_map.get(finger, -1)
        return self.landmarks[i] if 0 <= i < len(self.landmarks) else None


@dataclass
class Gesture:
    """Classified hand gesture."""
    kind: GestureKind = GestureKind.UNKNOWN
    confidence: float = 0.0
    hand: Handedness = Handedness.NONE
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FaceLand:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass
class FaceResult:
    """Face detection + optional recognition for a single face."""
    rect: Rect = field(default_factory=Rect)
    landmarks: List[FaceLand] = field(default_factory=list)
    identity: Optional[str] = None       # registered name or None
    distance: Optional[float] = None     # embedding distance to best match
    matched: bool = False
    confidence: float = 0.0


@dataclass
class Person:
    """Aggregated human-centric summary extracted from faces/hands."""
    face: Optional[FaceResult] = None
    hands: List[Hand] = field(default_factory=list)
    holding: Optional[str] = None
    position_label: str = ""            # "left" | "center" | "right"


@dataclass
class VisionSnapshot:
    """Immutable snapshot of everything detected in a single frame.

    ``frame_idx`` is a monotonically increasing counter from the camera
    thread so the consumer knows how stale each stage is.
    """
    frame_idx: int = 0
    timestamp: float = field(default_factory=time.monotonic)
    objects: List[Detection] = field(default_factory=list)
    persons: List[Person] = field(default_factory=list)
    hands: List[Hand] = field(default_factory=list)
    gestures: List[Gesture] = field(default_factory=list)
    scene_description: Optional[str] = None   # multimodal text
    stage_times: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None

    def is_fresh(self, max_age: float = 2.0) -> bool:
        return (time.monotonic() - self.timestamp) < max_age

    def objects_by_label(self, label: str) -> List[Detection]:
        low = label.strip().lower()
        return [d for d in self.objects if low in d.label.lower()]

    def count_label(self, label: str) -> int:
        return len(self.objects_by_label(label))

    def faces_unknown(self) -> List[FaceResult]:
        return [p.face for p in self.persons if p.face and not p.face.matched]


@dataclass
class VisionCapabilities:
    """Static feature flags — set once at coordinator startup."""
    has_camera: bool = False
    has_opencv: bool = False
    has_mediapipe: bool = False
    has_ultralytics: bool = False
    has_insightface: bool = False
    has_onnxruntime: bool = False
    has_torch: bool = False
    cuda_available: bool = False
    object_backend: str = "ai"
    face_backend: str = "geometry"
    device: str = "cpu"
