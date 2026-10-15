"""JARVIS vision subsystem.

A real subsystem — not a demo — consisting of:

    camera            shared OpenCV camera manager (single owner)
    object_detection  pluggable local object detection + AI fallback
    face_recognition  local face detection/recognition + identity store
    hand_tracking     MediaPipe hand landmarks
    gesture           gesture classification from hand landmarks
    multimodal        provider abstraction (Gemini / OpenAI / Anthropic / …)
    frame_processor   rate-limited background pipeline
    manager           VisionCoordinator — the high-level API + command router

The coordinator is the single entrypoint for the rest of JARVIS:

    from vision import get_coordinator
    vc = get_coordinator(ui=self.ui)
    vc.what_am_i_holding()
"""

from __future__ import annotations

from vision.config import (
    DEFAULTS, VisionConfig, load_vision_config, save_vision_section,
    vision_system_status,
)
from vision.manager import (
    VisionCoordinator, get_coordinator, release_coordinator,
)
from vision.camera import (
    CameraManager, available_cameras, auto_detect_camera_index,
    get_camera_manager, release_global_camera,
)
from vision import camera, config, frame_processor, gesture, hand_tracking, manager, multimodal, object_detection, utils

__all__ = [
    "VisionConfig", "load_vision_config", "save_vision_section",
    "DEFAULTS", "vision_system_status",
    "VisionCoordinator", "get_coordinator", "release_coordinator",
    "CameraManager", "get_camera_manager", "release_global_camera",
    "auto_detect_camera_index", "available_cameras",
]

__version__ = "1.0.0"