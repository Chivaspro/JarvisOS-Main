"""Centralized configuration for the JARVIS vision subsystem.

Secrets are never hard-coded here.  All keys are read from
``config/api_keys.json`` (the same store the rest of JARVIS uses) with an
optional environment-variable override so they can be kept out of the
config file when desired.

Priority order (highest first):
    1. Environment variable  (JARVIS_<NAME>)
    2. config/api_keys.json   (``vision`` dict)
    3. Built-in defaults
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from vision.utils import is_package_available


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
DATA_DIR = BASE_DIR / "data"
MODELS_DIR = DATA_DIR / "models" / "vision"
DEFAULT_FACE_DIR = DATA_DIR / "faces"
DEFAULT_OBJECT_MODEL_DIR = MODELS_DIR / "object_detection"


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass
class VisionConfig:
    """Frozen snapshot of every vision setting. Created by :func:`load_vision_config`."""

    # ── Camera ──────────────────────────────────────────────────────────────
    camera_enabled: bool = True            # master switch (privacy)
    camera_index: object = "auto"          # int or "auto"
    camera_width: int = 1024
    camera_height: int = 576
    camera_fps: int = 30
    processing_fps: int = 15               # preview + pipeline heartbeat

    # ── Object detection ────────────────────────────────────────────────────
    object_detection_enabled: bool = True
    object_detection_fps: int = 5
    object_detector_backend: str = "auto"  # auto | ultralytics | mediapipe | opencv | ai | off
    object_model_dir: str = ""             # "" → DEFAULT_OBJECT_MODEL_DIR
    confidence_threshold: float = 0.45
    object_iou_threshold: float = 0.45
    object_imgsz: int = 640
    object_max_detections: int = 30
    object_tracking: bool = True
    object_tracker_backend: str = "bytetrack"  # bytetrack | botsort
    object_person_only: bool = False
    # Three-state person filter: "all" | "only" | "exclude". Supersedes the
    # legacy boolean above; the boolean is kept for backward compatibility and
    # is folded into this mode at load time.
    object_person_mode: str = "all"
    # Explicit class-id allow-list for inference. Empty → every model class.
    object_class_filter: list = field(default_factory=list)

    # ── Face recognition ────────────────────────────────────────────────────
    face_detection_enabled: bool = True
    face_recognition_enabled: bool = True
    face_recognition_fps: int = 3
    face_similarity_threshold: float = 0.58
    face_database_dir: str = ""            # "" → DEFAULT_FACE_DIR
    face_backend: str = "geometry"         # geometry | insightface

    # ── Hand tracking / gestures ────────────────────────────────────────────
    hand_tracking_enabled: bool = True
    hand_tracking_fps: int = 10
    gesture_recognition_enabled: bool = True
    max_hands: int = 2

    # ── Multimodal AI ───────────────────────────────────────────────────────
    multimodal_provider: str = "gemini"    # gemini | openai | anthropic | custom
    vision_model: str = "gemini-3.6-flash"          # provider model id
    image_quality: int = 82                          # JPEG quality 1-95
    image_max_width: int = 1280
    multimodal_timeout: float = 45.0

    # ── Inference device ────────────────────────────────────────────────────
    inference_device: str = "auto"         # auto | cpu | cuda
    cuda_enabled: Optional[bool] = None    # resolved at load; True/False/None

    # ── Behaviour ───────────────────────────────────────────────────────────
    continuous_vision_mode: bool = False   # background processing + overlays
    overlay_enabled: bool = True           # draw detections on preview
    preview_opacity: float = 0.92
    preview_corner_radius: int = 28
    screen_vision_enabled: bool = True
    auto_reconnect: bool = True

    # ── Privacy ─────────────────────────────────────────────────────────────
    local_only: bool = False               # never upload frames to cloud APIs
    log_level: str = "info"                # debug | info | warn | error

    # ── Derived / runtime ───────────────────────────────────────────────────
    as_dict: dict = field(default_factory=dict, repr=False)
    gemini_api_key: str = field(default="", repr=False)

    # ────────────────────────────────────────────────────────────────────────
    @property
    def object_model_path(self) -> Path:
        root = Path(self.object_model_dir) if (self.object_model_dir or "").strip() \
            else DEFAULT_OBJECT_MODEL_DIR
        return root

    @property
    def face_db_path(self) -> Path:
        root = Path(self.face_database_dir) if (self.face_database_dir or "").strip() \
            else DEFAULT_FACE_DIR
        return root

    def effective_camera_index(self) -> int:
        idx = self.camera_index
        try:
            return int(idx)
        except (TypeError, ValueError):
            return -1  # sentinel → auto-detect


DEFAULTS = {
    "camera_enabled": True,
    "camera_index": "auto",
    "camera_width": 1024,
    "camera_height": 576,
    "camera_fps": 30,
    "processing_fps": 15,
    "object_detection_enabled": True,
    "object_detection_fps": 5,
    "object_detector_backend": "auto",
    "object_model_dir": "",
    "confidence_threshold": 0.45,
    "object_iou_threshold": 0.45,
    "object_imgsz": 640,
    "object_max_detections": 30,
    "object_tracking": True,
    "object_tracker_backend": "bytetrack",
    "object_person_only": False,
    "object_person_mode": "all",
    "object_class_filter": [],
    "face_detection_enabled": True,
    "face_recognition_enabled": True,
    "face_recognition_fps": 3,
    "face_similarity_threshold": 0.58,
    "face_database_dir": "",
    "face_backend": "geometry",
    "hand_tracking_enabled": True,
    "hand_tracking_fps": 10,
    "gesture_recognition_enabled": True,
    "max_hands": 2,
    "multimodal_provider": "gemini",
    "vision_model": "gemini-3.6-flash",
    "image_quality": 82,
    "image_max_width": 1280,
    "multimodal_timeout": 45.0,
    "inference_device": "auto",
    "continuous_vision_mode": False,
    "overlay_enabled": True,
    "preview_opacity": 0.92,
    "preview_corner_radius": 28,
    "screen_vision_enabled": True,
    "auto_reconnect": True,
    "local_only": False,
    "log_level": "info",
}


_ALIASES = {
    "camera": "camera_index",
    "resolution_width": "camera_width",
    "resolution_height": "camera_height",
    "detect_fps": "object_detection_fps",
    "vo_detection": "object_detection_enabled",
    "yolo_model": "object_model_dir",
    "detection_backend": "object_detector_backend",
    "yolo_iou": "object_iou_threshold",
    "yolo_tracker": "object_tracker_backend",
    "recog_threshold": "face_similarity_threshold",
    "gestures": "gesture_recognition_enabled",
}


def detect_cuda() -> Optional[bool]:
    """Return True when an NVIDIA CUDA runtime usable by the vision stack exists,
    False when it is clearly absent, None when it is not installed/checkable.

    Cheap and never fatal — used to pick a default inference device, not to
    gate any feature.
    """
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        pass
    try:
        import onnxruntime as ort
        try:
            return "CUDAExecutionProvider" in ort.get_available_providers()
        except Exception:
            return False
    except Exception:
        pass
    try:
        import cv2
        try:
            return bool(cv2.cuda.getCudaEnabledDeviceCount() > 0)
        except Exception:
            return False
    except Exception:
        return None


def resolve_inference_device(pref: str) -> str:
    pref = (pref or "auto").strip().lower()
    if pref in ("cuda", "gpu", "cuda:0"):
        if detect_cuda() is False:
            return "cpu"
        return "cuda"
    if pref in ("cpu", "onnx-cpu"):
        return "cpu"
    # auto
    return "cuda" if detect_cuda() else "cpu"


def sanitize(vision_raw: dict, narrative: dict) -> dict:
    """Merge a raw ``vision`` dict from config with the flat keys that older
    configs may have placed directly under the top level (e.g. ``camera_index``)."""
    cfg = dict(DEFAULTS)
    flat = {}
    for k, v in (narrative or {}).items():
        if k in _ALIASES:
            flat[_ALIASES[k]] = v
            continue
        if k in DEFAULTS:
            flat[k] = v
    if isinstance(vision_raw, dict):
        cfg.update({k: v for k, v in vision_raw.items() if k in DEFAULTS or k in _ALIASES})
    for old, new in _ALIASES.items():
        if old in cfg and new not in cfg:
            cfg[new] = cfg.pop(old)
        if old in vision_raw and new not in vision_raw:
            cfg[new] = vision_raw[old]
    if "camera_index" in flat and "camera_index" not in cfg:
        cfg["camera_index"] = flat["camera_index"]
    cfg.update(flat)
    return cfg


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _as_float(value, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        v = int(value)
        if lo is not None and v < lo:
            v = lo
        if hi is not None and v > hi:
            v = hi
        return v
    except (TypeError, ValueError):
        return default


def _normalise_tracker(value) -> str:
    tracker = str(value or "bytetrack").strip().lower().replace(".yaml", "")
    return tracker if tracker in {"bytetrack", "botsort"} else "bytetrack"


def _normalise_person_mode(value, legacy_only: bool = False) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"only", "exclude"}:
        return mode
    # "all" (or unset) still honours a legacy ``object_person_only=True`` config,
    # which predates the three-state mode. New saves always keep the boolean in
    # sync with the mode, so this cannot override an explicit "all".
    return "only" if legacy_only else "all"


def _as_int_list(value) -> list:
    """Coerce a stored class filter into a clean list of non-negative ints."""
    if value is None:
        return []
    if isinstance(value, (int,)):
        return [int(value)] if value >= 0 else []
    if isinstance(value, (list, tuple, set)):
        out = []
        for v in value:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv >= 0 and iv not in out:
                out.append(iv)
        return out
    return []


def load_vision_config() -> VisionConfig:
    """Build a :class:`VisionConfig` from api_keys.json + environment overrides."""
    config = _read_config()
    raw = config.get("vision")
    raw = raw if isinstance(raw, dict) else {}
    merged = sanitize(raw, config)

    # Prefer a dedicated key, fall back to the main Gemini key.
    gemini_key = (
        _env("JARVIS_GEMINI_API_KEY")
        or _env("GOOGLE_API_KEY")
        or str(raw.get("gemini_api_key") or "")
        or str(config.get("gemini_api_key") or "")
    )

    cuda_flag = detect_cuda()
    device = resolve_inference_device(merged.get("inference_device", "auto"))

    vc = VisionConfig(
        camera_enabled=_as_bool(merged.get("camera_enabled"), True),
        camera_index=merged.get("camera_index", "auto"),
        camera_width=_as_int(merged.get("camera_width"), 1024, 320, 4096),
        camera_height=_as_int(merged.get("camera_height"), 576, 240, 4096),
        camera_fps=_as_int(merged.get("camera_fps"), 30, 5, 60),
        processing_fps=_as_int(merged.get("processing_fps"), 15, 2, 60),
        object_detection_enabled=_as_bool(merged.get("object_detection_enabled"), True),
        object_detection_fps=_as_int(merged.get("object_detection_fps"), 5, 1, 30),
        object_detector_backend=str(merged.get("object_detector_backend", "auto")).strip().lower(),
        object_model_dir=str(merged.get("object_model_dir") or "").strip(),
        confidence_threshold=_as_float(merged.get("confidence_threshold"), 0.45, 0.05, 0.99),
        object_iou_threshold=_as_float(merged.get("object_iou_threshold"), 0.45, 0.05, 0.95),
        object_imgsz=_as_int(merged.get("object_imgsz"), 640, 160, 1536),
        object_max_detections=_as_int(merged.get("object_max_detections"), 30, 1, 100),
        object_tracking=_as_bool(merged.get("object_tracking"), True),
        object_tracker_backend=_normalise_tracker(merged.get("object_tracker_backend")),
        object_person_only=_as_bool(merged.get("object_person_only"), False),
        object_person_mode=_normalise_person_mode(
            merged.get("object_person_mode"),
            _as_bool(merged.get("object_person_only"), False)),
        object_class_filter=_as_int_list(merged.get("object_class_filter")),
        face_detection_enabled=_as_bool(merged.get("face_detection_enabled"), True),
        face_recognition_enabled=_as_bool(merged.get("face_recognition_enabled"), True),
        face_recognition_fps=_as_int(merged.get("face_recognition_fps"), 3, 1, 30),
        face_similarity_threshold=_as_float(
            merged.get("face_similarity_threshold"), 0.58, 0.1, 0.95),
        face_database_dir=str(merged.get("face_database_dir") or "").strip(),
        face_backend=str(merged.get("face_backend", "geometry")).strip().lower(),
        hand_tracking_enabled=_as_bool(merged.get("hand_tracking_enabled"), True),
        hand_tracking_fps=_as_int(merged.get("hand_tracking_fps"), 10, 1, 30),
        gesture_recognition_enabled=_as_bool(merged.get("gesture_recognition_enabled"), True),
        max_hands=_as_int(merged.get("max_hands"), 2, 1, 10),
        multimodal_provider=str(merged.get("multimodal_provider", "gemini")).strip().lower(),
        vision_model=str(merged.get("vision_model") or "gemini-3.6-flash").strip(),
        image_quality=_as_int(merged.get("image_quality"), 82, 10, 95),
        image_max_width=_as_int(merged.get("image_max_width"), 1280, 320, 4096),
        multimodal_timeout=_as_float(merged.get("multimodal_timeout"), 45.0, 5.0, 180.0),
        inference_device=device,
        cuda_enabled=cuda_flag,
        continuous_vision_mode=_as_bool(merged.get("continuous_vision_mode"), False),
        overlay_enabled=_as_bool(merged.get("overlay_enabled"), True),
        preview_opacity=_as_float(merged.get("preview_opacity"), 0.92, 0.1, 1.0),
        preview_corner_radius=_as_int(merged.get("preview_corner_radius"), 28, 8, 80),
        screen_vision_enabled=_as_bool(merged.get("screen_vision_enabled"), True),
        auto_reconnect=_as_bool(merged.get("auto_reconnect"), True),
        local_only=_as_bool(merged.get("local_only"), False),
        log_level=str(merged.get("log_level") or "info").strip().lower(),
        as_dict={k: merged.get(k, v) for k, v in DEFAULTS.items()},
        gemini_api_key=gemini_key,
    )
    # Keep the legacy boolean consistent with the resolved three-state mode so
    # any older reader still sees the right thing.
    vc.object_person_only = (vc.object_person_mode == "only")
    return vc


def save_vision_section(cfg: dict) -> bool:
    """Persist a merged ``vision`` dict (with feature toggles) to api_keys.json.

    Only the documented keys are written — secrets and unrelated keys are
    preserved verbatim.
    """
    try:
        data = _read_config()
        block = {}
        for k in DEFAULTS:
            if k in cfg:
                block[k] = cfg[k]
        if cfg.get("camera_index") is not None:
            block["camera_index"] = cfg["camera_index"]
        data["vision"] = block
        CONFIG_PATH.write_text(json.dumps(data, indent=4, ensure_ascii=False),
                               encoding="utf-8")
        return True
    except Exception:
        return False


def vision_system_status() -> dict:
    """Human/DIAG readable snapshot of what the subsystem can currently use."""
    from vision.utils import is_package_available
    cfg = load_vision_config()
    try:
        import cv2
    except Exception:
        cv2 = None
    try:
        cv2_ver = cv2.__version__ if cv2 is not None else "missing"
    except Exception:
        cv2_ver = "?"
    return {
        "camera": "capable" if is_package_available("cv2") else "missing (opencv-python)",
        "object_detection": _detection_status(cfg),
        "face_recognition": f"geometry (mediapipe={is_package_available('mediapipe')})",
        "hand_tracking": "mediapipe" if is_package_available("mediapipe") else "missing (mediapipe)",
        "cuda": cfg.inference_device,
        "device": cfg.inference_device,
        "multimodal_provider": cfg.multimodal_provider,
        "provider_api_key": "present" if cfg.gemini_api_key else "absent",
        "local_only": cfg.local_only,
        "face_db_path": str(cfg.face_db_path),
        "object_model_dir": str(cfg.object_model_path),
    }


def _detection_status(cfg: VisionConfig) -> str:
    if not cfg.object_detection_enabled:
        return "disabled"
    backend = cfg.object_detector_backend
    if backend == "off":
        return "off"
    if backend == "ai":
        return "multimodal (AI fallback)"
    if backend in ("ultralytics", "mediapipe", "opencv"):
        return f"{backend}"
    # auto
    parts = []
    if is_package_available("ultralytics"):
        parts.append("ultralytics")
    if (cfg.object_model_path / "best.tflite").exists():
        parts.append("mediapipe-tflite")
    if parts:
        return "+".join(parts)
    return "none loaded → multimodal fallback"