"""VisionCoordinator â€” the high-level brain of the vision subsystem.

This is the class JARVIS talks to.  It owns (or lazily builds) every
component, exposes defensive one-shot operations (``what_am_i_holding``,
``who_is_there``, ``describe_scene``, â€¦), routes a spoken/text command via
:meth:`handle_command`, and tints the on-screen overlay payload for the UI.

Design rules
------------
* Every public method returns a **string** (what JARVIS should say) plus an
  optional data payload â€” never raises.  Errors become polite apologies.
* Heavy work runs on worker threads; the asyncio backend simply executes.
* The camera manager + detectors are shared singletons â€” no module ever
  opens the webcam a second time.
* When continuous vision is enabled the pipeline runs in the background and
  one-shot questions reuse its cached snapshot when it is fresh.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from vision.camera import CameraManager, get_camera_manager
from vision.config import VisionConfig, load_vision_config, save_vision_section
from vision.frame_processor import VisionPipeline
from vision.models import (
    Detection, FaceResult, Gesture, GestureKind, Hand, Person, Rect,
    VisionCapabilities, VisionSnapshot,
)
from vision.utils import (
    compress_if_needed, encode_jpeg, is_package_available, logger,
)

# Default line for a coordinate-free "no camera" state.
_NO_CAMERA = "Vision system unavailable because the camera could not be accessed."


class VisionCoordinator:
    """Single-user coordinator. Get it via :func:`get_coordinator`."""

    def __init__(self, config: Optional[VisionConfig] = None,
                 ui=None):
        self._cfg = config or load_vision_config()
        self.ui = ui                       # optional JarvisUI proxy for logging
        self._lock = threading.RLock()
        self._cam: Optional[CameraManager] = None
        self._detector_objs: Dict[str, object] = {}
        self._pipeline: Optional[VisionPipeline] = None
        self._provider = None
        self._provider_checked = False
        self._capabilities = VisionCapabilities()
        self._session_known: Optional[str] = None     # "who's out there" latch
        self._session_known_ts = 0.0
        self._log_sink = self._make_log_sink()

    # â”€â”€ scan progress â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _report(self, stage: str) -> None:
        """Tell the UI a REAL scan step just finished.

        The percentage is looked up from the shared stage table (the single
        source of truth in :mod:`vision.camera`), so no number here is invented
        and each one is emitted only after that phase has actually returned.
        The Vision Panel turns it into its percentage bar; silently ignored
        when no UI proxy is attached.
        """
        from vision.camera import STAGE_PERCENT
        percent = STAGE_PERCENT.get(str(stage))
        if percent is None:
            return
        ui = self.ui
        if ui is None:
            return
        fn = getattr(ui, "vision_scan_progress", None)
        if fn is None:
            return
        try:
            fn(str(stage), int(percent))
        except Exception:
            pass

    # â”€â”€ logging bridge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _make_log_sink(self):
        def _sink(line: str):
            ui = self.ui
            if ui is not None:
                try:
                    ui.write_log(line)
                except Exception:
                    pass
        return _sink

    # â”€â”€ component accessors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @property
    def config(self) -> VisionConfig:
        return self._cfg

    def reload(self) -> None:
        try:
            self._cfg = load_vision_config()
        except Exception as exc:
            logger.warn(f"vision config reload failed: {exc}")
        with self._lock:
            self._detector_objs.clear()
        if self._cam is not None:
            self._cam.reload_config(self._cfg)

    def camera(self) -> Optional[CameraManager]:
        with self._lock:
            if self._cam is None:
                self._cam = get_camera_manager(self._cfg)
            return self._cam

    def _provider_instance(self):
        if not self._provider_checked:
            from vision.multimodal import get_vision_provider
            try:
                self._provider = get_vision_provider(self._cfg)
            except Exception as exc:
                logger.warn(f"vision provider init failed: {exc}")
                self._provider = None
            self._provider_checked = True
        return self._provider

    def _get_detector(self, kind: str):
        with self._lock:
            if kind in self._detector_objs:
                return self._detector_objs[kind]
        obj = None
        try:
            if kind == "object":
                from vision.object_detection import ObjectDetector
                obj = ObjectDetector.create(self._cfg, self._provider_instance)
            elif kind == "face":
                from vision.face_recognition import FaceRecognizer
                obj = FaceRecognizer(self._cfg)
            elif kind == "hand":
                from vision.hand_tracking import HandTracker
                obj = HandTracker(self._cfg)
            elif kind == "gesture":
                from vision.gesture import GestureRecognizer
                obj = GestureRecognizer(self._cfg)
        except Exception as exc:
            logger.warn(f"{kind} detector load failed: {exc}")
            obj = None
        with self._lock:
            self._detector_objs[kind] = obj
        return obj

    # â”€â”€ capabilities / startup log â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def object_runtime_status(self) -> dict:
        """Return the current object-inference snapshot for diagnostics/HUDs."""
        det = self._get_detector("object")
        if det is None:
            return {
                "backend": "unavailable", "model": "none", "task": "detect",
                "device": self._cfg.inference_device or "cpu", "objects": 0,
            }
        try:
            return dict(det.runtime_status())
        except Exception:
            return {
                "backend": getattr(det, "backend", "unavailable"),
                "model": "unknown", "task": "detect",
                "device": self._cfg.inference_device or "cpu", "objects": 0,
            }

    def capabilities(self) -> VisionCapabilities:
        caps = self._capabilities
        caps.has_camera = is_package_available("cv2")
        caps.has_mediapipe = is_package_available("mediapipe")
        caps.has_ultralytics = is_package_available("ultralytics")
        caps.has_insightface = is_package_available("insightface")
        caps.has_onnxruntime = is_package_available("onnxruntime")
        caps.has_torch = is_package_available("torch")
        caps.device = self._cfg.inference_device or "cpu"
        return caps

    def log_capabilities(self) -> str:
        caps = self.capabilities()
        det = self._detector_objs.get("object")
        obj_backend = getattr(det, "backend", "ai") if det is not None else "ai"
        lines = [
            f"Vision device: {caps.device.upper()}" + (
                " (CUDA available)" if caps.cuda_available else ""),
            f"Camera: {'detected' if caps.has_camera else 'missing opencv-python'}",
            f"YOLO: {'loaded' if det is not None and det.ready() else 'unloaded â†’ AI fallback'}",
            f"Face recognition: {'loaded' if self._get_detector('face') is not None else 'unavailable'}",
            f"Hand tracking: {'loaded' if self._get_detector('hand') is not None else 'unavailable'}",
            f"Multimodal provider: {self._cfg.multimodal_provider or 'gemini'} "
                f"({'connected' if (self._provider_instance() or _Unavail()).is_available() else 'unavailable'})",
        ]
        text = " | ".join(lines)
        logger.info(text)
        return text

    # â”€â”€ camera helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def ensure_camera(self) -> bool:
        """Start the shared stream if permitted; returns True when a frame is live."""
        if not self._cfg.camera_enabled:
            logger.warn("camera disabled in config")
            return False
        cam = self.camera()
        if cam is None:
            return False
        return cam.start()

    def stop_camera(self) -> None:
        cam = self.camera()
        if cam is not None:
            # An explicit stop (user request / shutdown) outranks the
            # "another surface still wants frames" deferral in the manager.
            cam.stop(force=True)

    def _get_frame(self, force_capture: bool = True):
        """Latest frame from the shared manager (or a one-shot capture).

        Returns (frame, source) where source is 'live' or 'oneshot'.
        """
        cam = self.camera()
        if cam is not None and cam.running:
            frame = cam.frame()
            if frame is not None:
                return frame, "live"
        if force_capture:
            self.ensure_camera()
            cam = self.camera()
            if cam is None:
                return None, "oneshot"
            latched = cam.frame()
            if latched is not None:
                return latched, "live"
        frame = take_photo_static(self._cfg)
        return frame, "oneshot"

    # â”€â”€ one-shot operations (each returns a spoken string) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def what_am_i_holding(self) -> str:
        """Priority feature: hand + object reasoning, then multimodal if needed."""
        if not self._cfg.camera_enabled:
            return "Camera access is turned off in Settings, so I cannot check your hands."
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            return _NO_CAMERA
        # Simulated mirror view: user's right hand appears on our right.
        from vision.models import Rect as R
        hand: Optional[Hand] = None
        hdet = self._get_detector("hand")
        if hdet is not None and self._cfg.hand_tracking_enabled:
            try:
                hands = hdet.detect(frame)
                hand = max(hands, key=lambda h: h.rect.area()) if hands else None
            except Exception as exc:
                logger.warn(f"holding: hand detect failed: {exc}")

        # best object overlap with the hand
        candidate: Optional[Detection] = None
        odet = self._get_detector("object")
        if odet is not None and self._cfg.object_detection_enabled:
            try:
                objs = odet.detect(frame)
                if hand is not None:
                    hr = hand.rect
                    scored = [(o, _box_distance(hr, o)) for o in objs
                              if _labels_meaningful(o.label)]
                    if scored:
                        scored.sort(key=lambda t: t[1])
                        best_d, best = scored[0]
                        if best_d < 0.42:
                            candidate = best
                if candidate is None and objs:
                    # try the largest object in the centre-ish area
                    objs_sorted = sorted(objs, key=lambda o: (o.rect.area(), o.confidence),
                                         reverse=True)
                    if objs_sorted:
                        centre_candidates = [o for o in objs_sorted[:4]
                                             if 0.3 < o.rect.center()[0] < 0.7]
                        if centre_candidates:
                            candidate = centre_candidates[0]
            except Exception as exc:
                logger.warn(f"holding: object detect failed: {exc}")

        if hand is None and candidate is None:
            return "I can't see a hand holding anything â€” please hold the object in front of the camera."

        if candidate is not None and candidate.confidence >= self._cfg.confidence_threshold:
            name = _label_for_speech(candidate.label)
            loc = _hand_relation(hand.rect, candidate.rect) if hand else "in your hand"
            return (f"Sir, you appear to be holding {name} â€” "
                    f"{loc}, with {int(candidate.confidence * 100)}% confidence.")

        # multimodal reasoning
        return self._multimodal_holding(frame, hand)

    def _multimodal_holding(self, frame, hand) -> str:
        provider = self._provider_instance()
        if provider is None or not provider.is_available():
            return "I can't identify the object with enough confidence, and no vision model is available."
        if self._cfg.local_only:
            return "Privacy mode is on, so I won't send the frame to a vision API. Without it, I can't be sure."
        region = hand.rect if hand is not None else None
        try:
            text = provider.analyze_image(
                frame,
                "Focus on the person's hand in this webcam frame. What are "
                "they holding? Reply with ONE short object name such as 'a "
                "black wireless mouse'. If nothing is being held or you "
                "cannot tell, reply exactly 'nothing identifiable'.",
            )
            if not text or "nothing identifiable" in text.lower():
                return "Sir, I can't identify the object with enough confidence."
            return f"It appears to be {_clean_article(text)}."
        except Exception as exc:
            logger.warn(f"holding multimodal failed: {exc}")
            return "Sir, I can't identify the object with enough confidence."

    def who_is_there(self) -> str:
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            return _NO_CAMERA
        faces: List[FaceResult] = []
        fdet = self._get_detector("face")
        if fdet is not None and self._cfg.face_detection_enabled:
            try:
                if self._cfg.face_recognition_enabled:
                    faces = fdet.recognize(frame) or []
                else:
                    faces = fdet.detect_faces(frame) or []
            except Exception as exc:
                logger.warn(f"who_is_there face error: {exc}")
        if not faces:
            people = self._count_persons_from_objects(frame)
            if people:
                return f"Sir, I can see approximately {_plural(people, 'person')} in the frame."
            return "Sir, no one appears to be in front of the camera right now."

        known, unknown = 0, 0
        names = []
        for f in faces:
            if f.matched and f.identity:
                known += 1
                names.append(f.identity)
            else:
                unknown += 1
        if known and unknown == 0:
            return (f"Sir, I recognize {_comma_list(names)} in front of me."
                    if len(names) > 1 else f"Sir, I recognize {names[0]}.")
        if known:
            return (f"Sir, I recognize {_comma_list(names)} "
                    f"plus {_plural(unknown, 'unfamiliar person')}.")
        return "Sir, I don't recognize that person. Would you like me to register them?"

    def _count_persons_from_objects(self, frame) -> int:
        odet = self._get_detector("object")
        if odet is None:
            return 0
        try:
            objs = odet.detect(frame)
            return len([o for o in objs if "person" in o.label.lower()])
        except Exception:
            return 0

    def describe_scene(self, question: str = "") -> str:
        # Each _report() below fires only after that phase has really returned,
        # so the Vision Panel's percentage tracks actual scan work.
        self._report("capturing")
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            self._report("failed")
            return _NO_CAMERA
        self._report("captured")
        provider = self._provider_instance()
        # Try to include local detection for speed first.
        local = ""
        odet = self._get_detector("object")
        if odet is not None and odet.ready():
            try:
                objs = odet.detect(frame) or []
                labels = [_label_for_speech(o.label) for o in objs][:8]
                if labels:
                    local = "Locally I can see " + ", ".join(dict.fromkeys(labels)) + ". "
            except Exception:
                pass
        self._report("detecting")
        if provider is None or not provider.is_available():
            if local:
                self._report("finalizing")
                return local
            self._report("failed")
            return "Scene description is unavailable: no vision model is connected."

        # Specific question â†’ prefer the semantic answer path (region-aware),
        # best matching main.py's `vision_look` handler which passes a question.
        _q = (question or "").strip()
        if _q:
            if self._cfg.local_only:
                self._report("failed")
                return "Privacy mode is on, so I cannot send this frame to a vision API."
            self._report("interpreting")
            try:
                text = provider.answer_visual_question(frame, _q)
                if text and text.strip():
                    self._report("finalizing")
                    return " ".join(s for s in (local, text) if s).strip()
            except Exception as exc:
                logger.warn(f"describe_scene question failed: {exc}")
            # Fall through to a generic description if the Q/A route errored.

        if self._cfg.local_only:
            self._report("failed")
            return "Privacy mode is on, so I cannot send this frame to a vision API."
        self._report("interpreting")
        try:
            text = provider.describe_scene(frame)
            self._report("finalizing")
            return " ".join(s for s in (local, text) if s).strip()
        except Exception as exc:
            logger.warn(f"describe_scene failed: {exc}")
            self._report("failed")
            return "I'm unable to describe the scene right now â€” the vision model failed."


    def detect_objects_now(self) -> str:
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            return _NO_CAMERA
        odet = self._get_detector("object")
        if odet is None:
            return "Object detection is unavailable."
        try:
            objs = odet.detect(frame) or []
        except Exception as exc:
            logger.warn(f"object detect error: {exc}")
            return "Object detection failed."
        if not objs:
            return "I don't see any recognizable objects."
        # Publish the scan to the conversation ledger BEFORE phrasing the
        # sentence: this is what makes "the left one" / "the other one"
        # resolvable on the next turn, with the real position attached.
        try:
            from core.brain_bridge import note_objects
            note_objects([
                {"label": _label_for_speech(o.label).removeprefix("a ").removeprefix("an "),
                 "position": ("centre" if _position_text(o.rect.center()[0])
                              in ("side", "in the middle")
                              else _position_text(o.rect.center()[0]))}
                for o in objs[:8]
            ])
        except Exception:
            pass
        # Group identical labels, count and position them.
        from collections import Counter
        counts = Counter(_label_for_speech(o.label) for o in objs)
        items = []
        for label, n in counts.items():
            first = next((o for o in objs if _label_for_speech(o.label) == label), None)
            pos = _position_text(first.rect.center()[0]) if first else ""
            items.append(f"{_count_word(n)} {label}{pos}")
        return "I can see " + ", ".join(items) + "."

    def how_many_people(self) -> str:
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            return _NO_CAMERA
        faces = []
        fdet = self._get_detector("face")
        if fdet is not None and self._cfg.face_detection_enabled:
            try:
                faces = fdet.detect_faces(frame) or []
            except Exception as exc:
                logger.warn(f"count faces error: {exc}")
        n = len(faces)
        if n == 0:
            n = self._count_persons_from_objects(frame)
        if n <= 0:
            return "I don't see anyone in the camera right now."
        return f"There is 1 person" if n == 1 else f"There are {n} people"

    def what_gesture(self) -> str:
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            return _NO_CAMERA
        hdet = self._get_detector("hand")
        gdet = self._get_detector("gesture")
        if hdet is None or gdet is None:
            return "Hand tracking is unavailable."
        try:
            hands = hdet.detect(frame) or []
            gestures = [gdet.recognize(h) for h in hands]
        except Exception as exc:
            logger.warn(f"gesture error: {exc}")
            return "Gesture recognition failed."
        meaningful = [g for g in gestures if g.kind != GestureKind.UNKNOWN and g.confidence >= 0.6]
        if not meaningful:
            return "I don't recognize any clear hand gesture â€” try an open palm, a fist, a peace sign, or a thumbs up."
        h = meaningful[-1]
        desc = h.hand.value + (" hand" if h.hand else "") + " " + _gesture_name(h.kind)
        return f"Sir, you are showing {desc}."

    def read_screen(self, question: str = "What is on the screen? Be concise.") -> str:
        """Optional screen vision â€” reuses the existing mss screenshot path."""
        if not self._cfg.screen_vision_enabled:
            return "Screen analysis is disabled in Settings."
        try:
            from actions.screen_processor import _capture_screen
            img_b, mime = _capture_screen()
        except Exception as exc:
            logger.warn(f"screen capture failed: {exc}")
            return "I could not capture the screen."
        provider = self._provider_instance()
        if provider is None or not provider.is_available():
            return "Screen analysis is unavailable â€” no vision model is connected."
        if self._cfg.local_only:
            return "Privacy mode is on, so I will not upload your screen."
        try:
            import numpy as np
            frame = decode_to_bgr(img_b)
            text = provider.answer_visual_question(
                frame, str(question) or "What is on the screen? Be concise.")
            return text or "I could not read anything useful from the screen."
        except Exception as exc:
            logger.warn(f"screen question failed: {exc}")
            return "I'm having trouble reading the screen right now."

    # â”€â”€ command router â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def handle_command(self, text: str) -> Optional[str]:
        """Route a natural-language query to the right vision operation.

        Returns None when the message is not vision-related (the caller
        should hand it to the normal AI pipeline instead).
        """
        low = (" " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " ")
        low = re.sub(r"\s{2,}", " ", low)
        patterns = []

        def has(*phrases: str) -> bool:
            for p in phrases:
                if " " + p + " " in low or low.strip().endswith(p.strip()):
                    return True
            return False

        if has("what am i holding", "what's in my hand", "what is in my hand",
               "what do i have in my hand", "what's in my hands", "holding", "am i holding"):
            return self.what_am_i_holding()
        if has("who is", "who are", "who's", "in front of me", "know me",
               "who am i looking at", "recognize", "face"):
            return self.who_is_there()
        if has("what gesture", "hand gesture", "gesture am i making",
               "what sign", "hand sign", "what am i doing with my hand"):
            return self.what_gesture()
        if has("how many people", "how many persons", "count people", "anyone there",
               "is anyone behind", "is there anyone", "any person", "people in front"):
            return self.how_many_people()
        if has("what do you see", "describe", "what's around", "what is around",
               "surroundings", "look at this", "look at it", "what's in front",
               "what is in front", "what's on my desk", "what is on my desk",
               "tell me what you see", "camera"):
            return self.describe_scene()
        if has("what objects", "objects do you see", "object detection",
               "detect objects", "list objects", "show me objects",
               "what can you see on the table", "what is on the table"):
            return self.detect_objects_now()
        if has("on my screen", "read the screen", "my screen", "screen for",
               "look at my screen", "why is this error", "what application is open",
               "read the error", "explain what i'm looking at"):
            return self.read_screen()
        if has("what color", "what colour", "what is this object used for",
               "what's this", "what is this", "identify that object",
               "identify this", "where is the phone", "where's my phone",
               "is there a laptop", "laptop nearby"):
            return self._semantic_question(text)
        return None

    def _semantic_question(self, question: str) -> str:
        frame, source = self._get_frame(force_capture=True)
        if frame is None:
            return _NO_CAMERA
        odet = self._get_detector("object")
        # Local fast path for "where is the phone / is there a laptop".
        if odet is not None and odet.ready():
            low = question.lower()
            if any(w in low for w in ("where", "is there", "nearby")):
                try:
                    objs = odet.detect(frame) or []
                    names = [o for o in objs if _labels_meaningful(o.label)]
                    target = _extract_target(question)
                    if target:
                        for o in names:
                            if target in o.label.lower():
                                return (f"Sir, the {_label_for_speech(o.label)} is "
                                        f"on your {_position_text(o.rect.center()[0])}.")
                        return f"I don't think there's a {target} in view."
                except Exception:
                    pass
        provider = self._provider_instance()
        if provider is None or not provider.is_available():
            return "I need a vision model to answer that â€” none is connected."
        if self._cfg.local_only:
            return "Privacy mode prevents me from sending this frame to a vision API."
        try:
            text = provider.answer_visual_question(frame, question)
            return text or "I don't have a confident answer for that."
        except Exception as exc:
            logger.warn(f"semantic question failed: {exc}")
            return "I couldn't answer that â€” the vision model failed."

    # â”€â”€ continuous pipeline â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def start_continuous(self, snapshot_cb=None) -> bool:
        """Enable background processing (needs camera + enabled vision)."""
        if not self._cfg.continuous_vision_mode:
            return False
        if not self.ensure_camera():
            return False
        with self._lock:
            if self._pipeline is not None and self._pipeline.running:
                return True
            self._pipeline = VisionPipeline(
                self._cfg, self.camera(), self._detector_objs, snapshot_cb)
            self._pipeline.start()
        return True

    def stop_continuous(self) -> None:
        with self._lock:
            if self._pipeline is not None:
                self._pipeline.stop()
                self._pipeline = None

    def latest_snapshot(self) -> VisionSnapshot:
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline.latest_snapshot()
        return VisionSnapshot()

    def release(self) -> None:
        """Shut down everything and release the camera (app exit)."""
        try:
            self.stop_continuous()
        except Exception:
            pass
        try:
            self.stop_camera()
        except Exception:
            pass

    def save_overlay(self, snapshot: VisionSnapshot) -> dict:
        """Convert a snapshot into the small dict the UI overlay consumes."""
        objects = [{"label": o.label, "conf": round(o.confidence, 2),
                    "rect": [o.rect.x, o.rect.y, o.rect.w, o.rect.h],
                    "tracker": o.tracker_id}
                   for o in snapshot.objects][:12]
        persons = [{"name": (p.face.identity if (p.face and p.face.matched) else None),
                    "rect": [p.face.rect.x, p.face.rect.y, p.face.rect.w, p.face.rect.h]
                    if p.face else None} for p in snapshot.persons][:6]
        gestures = [{"hand": g.hand.value, "gesture": g.kind.value,
                     "conf": round(g.confidence, 2)}
                    for g in snapshot.gestures if g.kind != GestureKind.UNKNOWN][:4]
        return {"objects": objects, "persons": persons, "gestures": gestures}


# â”€â”€ module-level singleton â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_COORDINATOR: Optional[VisionCoordinator] = None
_COORD_LOCK = threading.Lock()


def get_coordinator(config: Optional[VisionConfig] = None, ui=None) -> VisionCoordinator:
    global _COORDINATOR
    with _COORD_LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = VisionCoordinator(config, ui=ui)
        else:
            _COORDINATOR.reload()
        if ui is not None:
            _COORDINATOR.ui = ui
        return _COORDINATOR


def release_coordinator() -> None:
    global _COORDINATOR
    with _COORD_LOCK:
        if _COORDINATOR is not None:
            try:
                _COORDINATOR.release()
            except Exception:
                pass
            _COORDINATOR = None


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def take_photo_static(cfg: VisionConfig):
    """One-shot BGR frame independent of the streaming manager."""
    try:
        cam = get_camera_manager(cfg)
        return cam.take_photo()
    except Exception as exc:
        logger.warn(f"one-shot capture failed: {exc}")
        return None


def decode_to_bgr(jpg_bytes: bytes):
    from vision.utils import buffer_to_bgr
    return buffer_to_bgr(jpg_bytes)


def _box_distance(hand_rect: Rect, det: Detection) -> float:
    """Approximate centre distance (normalised units)."""
    hc = hand_rect.center()
    dc = det.rect.center()
    return ((hc[0] - dc[0]) ** 2 + (hc[1] - dc[1]) ** 2) ** 0.5


def _labels_meaningful(label: str) -> bool:
    label = (label or "").strip().lower()
    skip = {"person"}  # person is handled by face stage
    return bool(label) and label not in skip and not label.startswith("class_")


def _label_for_speech(label: str) -> str:
    label = (label or "").strip()
    if not label:
        return "an object"
    label = label.lower()
    vowels = "aeiou"
    if label[:1] in vowels:
        return f"an {label}"
    return f"a {label}"


def _clean_article(text: str) -> str:
    return re.sub(r"^(it|that)\s+(appears?\s+to\s+be|looks?\s+like|is|seems?\s+to\s+be)\s+"
                  r"((a|an|the)\s+)?", "", (text or "").strip(), flags=re.IGNORECASE).strip()


def _hand_relation(hr: Rect, dr: Rect) -> str:
    hc = hr.center()
    if hr.contains_point(*dr.center()):
        return "held in your hand"
    if dr.center()[0] < hc[0]:
        return "just beside your hand, on the left"
    return "just beside your hand, on the right"


def _position_text(cx: float) -> str:
    if cx < 0.3:
        return "left"
    if cx > 0.7:
        return "right"
    if 0.3 <= cx < 0.45 or 0.55 < cx <= 0.7:
        return "side"
    return "in the middle"


def _extract_target(question: str) -> Optional[str]:
    low = (question or "").lower()
    for kw in ("is there a ", "where's my ", "where is my ", "where's the ",
               "where is the ", "is there ", "a laptop", "the phone"):
        if kw in low:
            leftover = low.replace("where's my ", "").replace("where is my ", "")
            leftover = leftover.replace("where is the ", "").replace("where's the ", "")
            leftover = leftover.replace("is there a ", "").replace("is there ", "")
            leftover = leftover.split("?")[0].strip()
            # strip leading filler words
            leftover = leftover.replace("laptop", "laptop").strip()
            candidates = ["laptop", "phone", "smartphone", "bottle", "cup", "book",
                          "mouse", "keyboard", "monitor", "remote", "wallet", "glasses"]
            for c in candidates:
                if c in leftover:
                    return c
    return None


def _count_word(n: int) -> str:
    if n == 1:
        return "a"
    return f"{n}"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _comma_list(names) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _gesture_name(kind: GestureKind) -> str:
    names = {
        GestureKind.OPEN_PALM: "an open palm",
        GestureKind.FIST: "a closed fist",
        GestureKind.POINT: "a pointing finger",
        GestureKind.THUMBS_UP: "a thumbs up",
        GestureKind.THUMBS_DOWN: "a thumbs down",
        GestureKind.PEACE: "a peace sign",
        GestureKind.OK: "an OK sign",
        GestureKind.PINCH: "a pinch",
        GestureKind.STOP: "a stop hand",
        GestureKind.UNKNOWN: "an unknown gesture",
    }
    return names.get(kind, "an unknown gesture")


class _Unavail:
    def is_available(self) -> bool:
        return False


__all__ = [
    "VisionCoordinator", "get_coordinator", "release_coordinator",
    "take_photo_static",
]