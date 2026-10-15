"""Real-time local object detection with pluggable backends.

Backends
--------
``ultralytics`` (YOLO)   — best accuracy; requires ``pip install ultralytics``
                           and a ``.pt`` / ``.onnx`` model.
``mediapipe``            — EfficientDet TFLite task file in the model dir
                           (Apache-2.0); downloads the default model if absent
                           and a network is reachable.
``opencv``               — YOLOv4/.onnx via ``cv2.dnn``; needs weights files.
``ai``                   — no local model; detection is delegated to the
                           multimodal provider (slow, but always available).
``off``                  — detection disabled.

Backend selection is ``auto`` by default: the first backend for which a
model file is actually present wins.  When nothing local is installed the
detector transparently routes to the multimodal provider so JARVIS can
still answer "what do you see?" — via the exact same public API.

Every method is defensive: a broken model returns an empty list / a
friendly error message and never propagates an exception to the caller.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import List, Optional

from vision.config import VisionConfig, base_dir
from vision.models import Detection, Rect
from vision.utils import frame_to_png_b64, is_package_available, logger

try:
    import numpy as np
    _NP = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    _NP = False

try:
    import cv2
    _CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _CV2 = False


# Default MediaPipe ObjectDetector (EfficientDet-Lite0, 4.6 MB, Apache-2.0).
_DEFAULT_TFLITE_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "object_detector/efficientdet_lite0/float32/latest/efficientdet_lite0.tflite"
)
_DEFAULT_TFLITE_NAME = "efficientdet_lite0.tflite"


class ObjectDetectionError(RuntimeError):
    pass


class ObjectDetector:
    """Detector facade. Create via :meth:`create` and call ``detect()``."""

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._backend = ""
        self._model = None        # mediapipe task / ultralytics model / None
        self._names = []          # class-name list (mediapipe/opencv)
        self._ai_provider = None  # injected by the coordinator for 'ai' backend
        self._ai_provider_getter = None
        self._device = cfg.inference_device

        self._tracker = None
        self._tracker_inited = False
        self._prev_boxes: list = []

        # Live-inference telemetry. These values are updated only by the
        # detector worker and exposed as a read-only snapshot for the HUD.
        self._model_name = ""
        self._task = "detect"
        self._last_inference_ms = 0.0
        self._inference_fps = 0.0
        self._last_object_count = 0
        self._native_tracking = False

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def create(cls, cfg: VisionConfig,
               ai_provider_getter=None) -> "ObjectDetector":
        detector = cls(cfg)
        detector._ai_provider_getter = ai_provider_getter
        detector.ensure_backend()
        return detector

    def ensure_backend(self) -> str:
        """Pick and load the best available backend. Returns backend name."""
        cfg = self._cfg
        requested = (cfg.object_detector_backend or "auto").strip().lower()
        if requested == "off":
            self._backend = ""
            return ""
        if requested == "ai":
            self._backend = "ai"
            return "ai"

        candidates = []
        if requested == "auto":
            candidates = ["ultralytics", "mediapipe", "opencv", "ai"]
        elif requested == "ultralytics":
            candidates = ["ultralytics", "ai"]
        elif requested == "mediapipe":
            candidates = ["mediapipe", "ai"]
        elif requested == "opencv":
            candidates = ["opencv", "ai"]
        else:
            candidates = ["ai"]

        for name in candidates:
            try:
                if name == "ultralytics" and self._try_load_ultralytics():
                    self._backend = "ultralytics"
                    return self._backend
                if name == "mediapipe" and self._try_load_mediapipe():
                    self._backend = "mediapipe"
                    return self._backend
                if name == "opencv" and self._try_load_opencv():
                    self._backend = "opencv"
                    return self._backend
            except Exception as exc:
                logger.warn(f"backend {name} failed to load: {exc} continue")

        if requested != "off":
            self._backend = "ai"
        return self._backend

    @property
    def backend(self) -> str:
        return self._backend

    def ready(self) -> bool:
        return self._backend in ("ultralytics", "mediapipe", "opencv")

    # ── model loading helpers ───────────────────────────────────────────────

    def _model_path(self, *names: str) -> Path:
        model_dir = self._cfg.object_model_path
        for name in names:
            p = model_dir / name
            if p.exists():
                return p
        # A pretrained weight bundled with the app (e.g. yolo11n.pt in the repo
        # root) must be found before we ever consider a network bootstrap —
        # local-only mode blocks downloads, and shipping the file only to have
        # the detector ignore it would silently disable YOLO CORE.
        try:
            root = base_dir()
            for name in names:
                p = root / name
                if p.exists():
                    return p
        except Exception:
            pass
        return Path(model_dir) / (names[0] if names else "")

    def _try_load_ultralytics(self) -> bool:
        if not is_package_available("ultralytics"):
            return False
        model_file = self._model_path(
            "yolo11n.pt", "yolo26n.pt", "yolov8n.pt", "yolov8n.onnx", "best.pt")
        from ultralytics import YOLO

        # A lightweight pretrained model is allowed to bootstrap itself on the
        # first run. Local-only mode deliberately blocks this network path.
        if not model_file.exists():
            if self._cfg.local_only:
                logger.info("local-only mode: YOLO model download skipped")
                return False
            logger.info("ultralytics installed but no local model found; "
                        "bootstrapping yolo11n.pt")
            try:
                self._model = YOLO("yolo11n.pt")
                self._model_name = "yolo11n.pt"
            except Exception as exc:
                logger.warn(f"YOLO pretrained bootstrap failed: {exc}")
                self._model = None
                return False
        else:
            self._model = YOLO(str(model_file))
            self._model_name = model_file.name

        names = getattr(self._model, "names", {}) or {}
        if isinstance(names, dict):
            self._names = [str(names[k]) for k in sorted(names)]
        else:
            self._names = [str(x) for x in list(names)]
        self._task = str(getattr(self._model, "task", "detect") or "detect")
        logger.info("object detection backend: ultralytics "
                    f"(model={self._model_name}, task={self._task}, device={self._device})")
        return True

    def _try_load_mediapipe(self) -> bool:
        if not is_package_available("mediapipe"):
            return False
        model_file = self._model_path(_DEFAULT_TFLITE_NAME, "efficientdet.tflite",
                                      "best.tflite")
        if not model_file.exists():
            logger.info("mediapipe installed but no object model present")
            downloaded = self._download_default_tflite(model_file)
            if not downloaded:
                return False
        try:
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision as mp_vision
            base = mp_python.BaseOptions
            opts = mp_vision.ObjectDetectorOptions(
                base_options=base(model_asset_path=str(model_file)),
                running_mode=mp_vision.RunningMode.IMAGE,
                score_threshold=max(0.05, min(0.9, self._cfg.confidence_threshold)),
                max_results=40,
            )
            self._model = mp_vision.ObjectDetector.create_from_options(opts)
            self._names = ([""] * 91)  # COCO 91 classes; labels come from results
            logger.info("object detection backend: mediapipe-tflite")
            return True
        except Exception as exc:
            logger.warn(f"mediapipe object model failed to load: {exc}")
            self._model = None
            return False

    def _download_default_tflite(self, out: Path) -> bool:
        try:
            # Imported here rather than at module scope: `urllib.request` drags
            # in http.client + ssl + email (measured ~42 ms), and it is only
            # needed when the bundled detection model is actually missing.
            import urllib.request
            out.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"downloading efficientdet_lite0 → {out}")
            with urllib.request.urlopen(_DEFAULT_TFLITE_URL, timeout=60) as r, \
                    open(out, "wb") as f:
                shutil.copyfileobj(r, f)
            logger.info("model downloaded")
            return True
        except Exception as exc:
            logger.warn(f"model download failed: {exc}")
            return False

    def _try_load_opencv(self) -> bool:
        if not _CV2:
            return False
        model_dir = self._cfg.object_model_path
        weights = self._model_path("yolov4-tiny.weights", "yolov4.weights",
                                   "yolov3.weights")
        cfg_file = self._model_path("yolov4-tiny.cfg", "yolov4.cfg", "yolov3.cfg")
        if not weights.exists() or not cfg_file.exists():
            logger.info("opencv backend selected but weights/cfg missing")
            return False
        net = cv2.dnn.readNet(str(weights), str(cfg_file))
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        if self._device == "cuda":
            try:
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CUDA)
            except Exception:
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self._model = net
        try:
            self._names = [x.strip() for x in
                           (model_dir / "coco.names").read_text().splitlines()]
        except Exception:
            self._names = []
        logger.info("object detection backend: opencv-dnn")
        return True

    # ── detection ───────────────────────────────────────────────────────────

    def set_runtime_options(self, *, tracking=None, confidence=None,
                            iou=None, person_only=None, person_mode=None,
                            imgsz=None, max_det=None, device=None,
                            classes=None) -> None:
        """Update safe live-inference controls without rebuilding the model.

        Every argument is optional; only the ones that are not ``None`` are
        applied. ``device`` is resolved against the real hardware so the HUD can
        never claim CUDA when CUDA is not actually in use.
        """
        try:
            if tracking is not None:
                self._cfg.object_tracking = bool(tracking)
                if not self._cfg.object_tracking:
                    self._prev_boxes = []
            if confidence is not None:
                self._cfg.confidence_threshold = max(0.05, min(0.99, float(confidence)))
            if iou is not None:
                self._cfg.object_iou_threshold = max(0.05, min(0.95, float(iou)))
            if imgsz is not None:
                self._cfg.object_imgsz = int(max(160, min(1536, int(imgsz))))
            if max_det is not None:
                self._cfg.object_max_detections = int(max(1, min(300, int(max_det))))
            if person_mode is not None:
                mode = str(person_mode).strip().lower()
                self._cfg.object_person_mode = mode if mode in {"all", "only", "exclude"} else "all"
                self._cfg.object_person_only = (self._cfg.object_person_mode == "only")
            elif person_only is not None:
                # Legacy boolean callers map onto the three-state mode.
                self._cfg.object_person_mode = "only" if bool(person_only) else "all"
                self._cfg.object_person_only = bool(person_only)
            if classes is not None:
                clean = []
                for c in (classes or []):
                    try:
                        ic = int(c)
                    except (TypeError, ValueError):
                        continue
                    if ic >= 0 and ic not in clean:
                        clean.append(ic)
                self._cfg.object_class_filter = clean
            if device is not None:
                from vision.config import resolve_inference_device
                resolved = resolve_inference_device(str(device))
                self._device = resolved
                self._cfg.inference_device = resolved
        except Exception as exc:
            logger.warn(f"live detector option update failed: {exc}")

    # ── class / person helpers ────────────────────────────────────────────

    def _person_class_ids(self) -> List[int]:
        """Class ids whose *name* means a person — never a hard-coded index 0."""
        ids = []
        for i, n in enumerate(self._names or []):
            low = str(n).strip().lower()
            if low in ("person", "people", "pedestrian") or "person" in low:
                ids.append(i)
        return ids

    def _active_class_ids(self):
        """Resolve the class-id allow-list from CLASS FILTER + PERSON FILTER.

        Returns ``None`` when no filtering applies (pass nothing to the model),
        or a list of ids. An empty list means the filters exclude everything.
        """
        names = self._names or []
        if not names:
            return None
        all_ids = list(range(len(names)))
        allowed = None
        cf = []
        for c in (self._cfg.object_class_filter or []):
            try:
                ic = int(c)
            except (TypeError, ValueError):
                continue
            if 0 <= ic < len(names):
                cf.append(ic)
        if cf:
            allowed = set(cf)
        mode = str(self._cfg.object_person_mode or "all").strip().lower()
        person_ids = set(self._person_class_ids())
        if mode == "only":
            allowed = person_ids if allowed is None else (allowed & person_ids)
        elif mode == "exclude":
            base = set(all_ids) if allowed is None else allowed
            allowed = base - person_ids
        if allowed is None:
            return None
        return sorted(allowed)

    def device_display(self) -> str:
        dev = str(self._device or "cpu").strip().lower()
        if dev.startswith("cuda"):
            return "CUDA:0" if ":" not in dev else dev.upper()
        return "CPU"

    def _filter_dets_by_label(self, dets: List[Detection]) -> List[Detection]:
        """Label-based PERSON FILTER + CLASS FILTER for non-Ultralytics backends.

        Ultralytics filters natively via the ``classes`` argument; MediaPipe and
        OpenCV-DNN do not, so the same intent is applied here by label. Class-id
        filtering is only enforced when the backend published real class names.
        """
        mode = str(self._cfg.object_person_mode or "all").strip().lower()
        names = [str(n) for n in (self._names or [])]
        have_names = any(n.strip() for n in names)
        allowed_ids = self._active_class_ids() if have_names else None
        out: List[Detection] = []
        for d in dets:
            low = str(d.label).strip().lower()
            is_person = low in ("person", "people", "pedestrian") or "person" in low
            if mode == "only" and not is_person:
                continue
            if mode == "exclude" and is_person:
                continue
            if allowed_ids is not None:
                lid = None
                try:
                    lid = names.index(str(d.label))
                except ValueError:
                    lid = d.class_id if 0 <= int(d.class_id) < len(names) else None
                if lid is not None and lid not in allowed_ids:
                    continue
            out.append(d)
        return out


    @property
    def class_names(self) -> List[str]:
        return list(self._names)

    def runtime_status(self) -> dict:
        """Return the current detector state for the live Vision HUD."""
        return {
            "backend": self._backend or "off",
            "model": self._model_name or "none",
            "task": self._task or "detect",
            "device": self._device or "cpu",
            "device_display": self.device_display(),
            "tracker": (
                str(self._cfg.object_tracker_backend or "bytetrack").replace(".yaml", "").lower()
                if self._cfg.object_tracking and self._backend == "ultralytics"
                else ("local-iou" if self._cfg.object_tracking and self.ready() else "off")
            ),
            "inference_ms": float(self._last_inference_ms),
            "inference_fps": float(self._inference_fps),
            "objects": int(self._last_object_count),
            "confidence": float(self._cfg.confidence_threshold),
            "iou": float(self._cfg.object_iou_threshold),
            "imgsz": int(self._cfg.object_imgsz),
            "max_det": int(self._cfg.object_max_detections),
            "person_mode": str(self._cfg.object_person_mode or "all"),
            "person_only": bool(self._cfg.object_person_only),
            "class_filter": [int(c) for c in (self._cfg.object_class_filter or [])],
            "class_names": list(self._names or []),
            "class_count": len(self._names or []),
            "native_tracking": bool(self._native_tracking),
        }

    def detect(self, frame_bgr) -> List[Detection]:
        """Detect objects in a BGR frame. Robust: never raises."""
        try:
            if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
                return []
            if not self.ready():
                return self._detect_via_ai(frame_bgr)
            if self._backend == "ultralytics":
                return self._detect_ultralytics(frame_bgr)
            if self._backend == "mediapipe":
                return self._detect_mediapipe(frame_bgr)
            if self._backend == "opencv":
                return self._detect_opencv(frame_bgr)
            return self._detect_via_ai(frame_bgr)
        except ObjectDetectionError:
            raise
        except Exception as exc:
            logger.error(f"object detection failed: {exc}")
            return []

    def _detect_ultralytics(self, frame_bgr) -> List[Detection]:
        start = time.perf_counter()
        self._native_tracking = False
        # Resolve CLASS FILTER + PERSON FILTER into a real class-id allow-list
        # handed to the model (native, efficient). An empty list means the
        # current filters exclude every class, so nothing can be detected.
        allowed_ids = self._active_class_ids()
        if allowed_ids is not None and not allowed_ids:
            self._last_inference_ms = 0.0
            self._last_object_count = 0
            return []
        try:
            kwargs = {
                "conf": self._cfg.confidence_threshold,
                "iou": self._cfg.object_iou_threshold,
                "imgsz": self._cfg.object_imgsz,
                "max_det": self._cfg.object_max_detections,
                "verbose": False,
                "device": self._device,
            }
            if allowed_ids is not None:
                kwargs["classes"] = allowed_ids
            if self._cfg.object_tracking:
                tracker_name = str(self._cfg.object_tracker_backend or "bytetrack").strip().lower().replace(".yaml", "")
                if tracker_name not in {"bytetrack", "botsort"}:
                    tracker_name = "bytetrack"
                tracker_file = f"{tracker_name}.yaml"
                try:
                    results = self._model.track(frame_bgr, persist=True, tracker=tracker_file, **kwargs)
                    self._native_tracking = True
                except Exception as track_exc:
                    logger.warn(f"YOLO native tracking failed, using detection only: {track_exc}")
                    results = self._model.predict(frame_bgr, **kwargs)
            else:
                results = self._model.predict(frame_bgr, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._last_inference_ms = elapsed_ms
            inst_fps = 1000.0 / elapsed_ms if elapsed_ms > 0.0 else 0.0
            self._inference_fps = (inst_fps if self._inference_fps <= 0.0
                                   else (self._inference_fps * 0.75 + inst_fps * 0.25))

        if not results:
            self._last_object_count = 0
            return []
        boxes = getattr(results[0], "boxes", None)
        if boxes is None:
            self._last_object_count = 0
            return []
        height, width = frame_bgr.shape[:2]
        dets: List[Detection] = []
        for box in boxes:
            try:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                conf = float(box.conf[0])
                cls = int(box.cls[0])
                label = str(self._names[cls]) if 0 <= cls < len(self._names) else f"class_{cls}"
                tracker_id = -1
                ids = getattr(box, "id", None)
                if ids is not None:
                    try:
                        tracker_id = int(ids[0])
                    except Exception:
                        tracker_id = -1
                detection = Detection(
                    label=label, confidence=conf,
                    rect=Rect(x=max(0.0, x1 / width), y=max(0.0, y1 / height),
                              w=min(1.0, max(0.0, (x2 - x1) / width)),
                              h=min(1.0, max(0.0, (y2 - y1) / height))),
                    class_id=cls, source="ultralytics",
                )
                detection.tracker_id = tracker_id
                dets.append(detection)
            except Exception:
                continue
        self._last_object_count = len(dets)
        # Native tracking normally owns persistent IDs. Some model/back-end
        # combinations still return id=None for the first frames; keep the HUD
        # useful by falling back to local IoU association in that case.
        has_native_ids = any(getattr(d, "tracker_id", -1) >= 0 for d in dets)
        return dets if self._native_tracking and has_native_ids else self._associate_tracks(dets)

    def _detect_mediapipe(self, frame_bgr) -> List[Detection]:
        import cv2
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        import numpy as np
        mp_image = None
        try:
            from mediapipe.tasks.python import vision as mp_vision
            mp_image = mp_vision.Image(image_format=mp_vision.ImageFormat.SRGB,
                                       data=rgb)
        except Exception:
            mp_image = None
        result = self._model.detect(mp_image)
        dets: List[Detection] = []
        for det in (result.detections or []):
            try:
                bb = det.bounding_box
                w, h = frame_bgr.shape[1], frame_bgr.shape[0]
                x, y = float(bb.origin_x) / w, float(bb.origin_y) / h
                bw, bh = float(bb.width) / w, float(bb.height) / h
                cats = det.categories or []
                label = cats[0].category_name if cats else "object"
                conf = float(cats[0].score) if cats else 0.0
                dets.append(Detection(label=label, confidence=conf,
                                      rect=Rect(x=x, y=y, w=bw, h=bh),
                                      class_id=cats[0].index if cats else -1,
                                      source="mediapipe"))
            except Exception:
                continue
        dets = self._filter_dets_by_label(dets)
        self._last_object_count = len(dets)
        return self._associate_tracks(dets)

    def _detect_opencv(self, frame_bgr) -> List[Detection]:
        h, w = frame_bgr.shape[:2]
        blob = cv2.dnn.blobFromImage(frame_bgr, 1 / 255.0, (416, 416),
                                     swapRB=True, crop=False)
        self._model.setInput(blob)
        outs = self._model.forward(self._model.getUnconnectedOutLayersNames())
        dets: List[Detection] = []
        conf_thresh = self._cfg.confidence_threshold
        for out in outs:
            for row in out:
                scores = row[5:]
                cls = int(np.argmax(scores))
                conf = float(scores[cls])
                if conf < conf_thresh:
                    continue
                cx, cy, bw, bh = [float(v) for v in row[:4]]
                x1 = (cx - bw / 2) / w
                y1 = (cy - bh / 2) / h
                label = self._names[cls] if 0 <= cls < len(self._names) else f"class_{cls}"
                dets.append(Detection(label=label, confidence=conf,
                                      rect=Rect(x=max(0.0, x1), y=max(0.0, y1),
                                                w=min(1.0, max(0.0, bw / w)),
                                                h=min(1.0, max(0.0, bh / h))),
                                      class_id=cls, source="opencv"))
        dets = self._filter_dets_by_label(dets)
        self._last_object_count = len(dets)
        return self._associate_tracks(dets)

    def _detect_via_ai(self, frame_bgr) -> List[Detection]:
        """Multimodal fallback: ask the vision model for rough bounding boxes.

        This path has no strict real-time guarantees and is throttled by the
        coordinator — the output is expressed as approximate object names +
        locations so JARVIS can still count / describe without YOLO.
        """
        getter = self._ai_provider_getter
        if getter is None:
            return []
        provider = getter()
        if provider is None or not provider.is_available():
            return []
        question = (
            "List the visible objects as compact lines, one per object, "
            "formatted as: Name | approximate-left-tenth | top-tenth. "
            "Use only the tenths 1-9 for position. Be brief."
        )
        try:
            text = provider.analyze_image(frame_bgr, question)
        except Exception as exc:
            logger.warn(f"AI detection failed: {exc}")
            return []
        return _parse_ai_detections(text)

    # ── tracking (cheap centroid IoU association) ───────────────────────────

    def _associate_tracks(self, dets: List[Detection]) -> List[Detection]:
        if not self._cfg.object_tracking or not dets:
            self._prev_boxes = []
            return dets
        from vision.utils import iou
        new_dets = []
        used_prev: set = set()
        valid_ids = []
        for d in dets:
            try:
                tid = int(d.tracker_id)
            except (TypeError, ValueError):
                tid = -1
            if tid >= 0:
                valid_ids.append(tid)
            d.tracker_id = tid
        next_id = (max(valid_ids or [0]) + 1 if dets else 0)
        for d in dets:
            best_iou, best_i = -1.0, -1
            for i, (pbox, pid) in enumerate(self._prev_boxes):
                if i in used_prev:
                    continue
                if pbox.label != d.label:
                    continue
                v = iou((pbox.rect.x, pbox.rect.y, pbox.rect.w, pbox.rect.h),
                        (d.rect.x, d.rect.y, d.rect.w, d.rect.h))
                if v > best_iou:
                    best_iou, best_i = v, i
            if best_iou >= 0.35:
                d.tracker_id = self._prev_boxes[best_i][1]
                used_prev.add(best_i)
            else:
                d.tracker_id = next_id
                next_id += 1
            new_dets.append(d)
        self._prev_boxes = [(d, d.tracker_id) for d in new_dets]
        return new_dets

    # ── health ──────────────────────────────────────────────────────────────

    def describe(self) -> str:
        if not self.ready():
            return f"backend={self._backend} (AI fallback)"
        return f"backend={self._backend} device={self._device}"


# ── parser for the AI fallback text ────────────────────────────────────────

def _parse_ai_detections(text: str) -> List[Detection]:
    """Parse 'Name | left-tenth | top-tenth' lines the multimodal model emits."""
    dets: List[Detection] = []
    if not text:
        return dets
    for raw_line in (text or "").splitlines():
        line = raw_line.strip("- ").strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        try:
            fx, fy = int(parts[1]), int(parts[2])
            fx = max(1, min(9, fx)); fy = max(1, min(9, fy))
        except Exception:
            continue
        dets.append(Detection(
            label=parts[0][:32],
            confidence=0.5,
            rect=Rect(x=(fx - 0.5) / 10.0, y=(fy - 0.5) / 10.0,
                      w=0.10, h=0.10),
            source="ai",
        ))
    return dets


__all__ = ["ObjectDetector", "ObjectDetectionError"]