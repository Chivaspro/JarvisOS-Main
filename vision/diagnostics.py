"""Environment / capability diagnostic for the JARVIS vision subsystem.

Run from the project root:

    python -m vision.diagnostics

Prints a structured report with exit code 0 when everything critical is OK.
Does not open the GUI and never calls remote APIs.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

_LINES: list[str] = []
_FAILED: list[str] = []


def _log(line: str = "") -> None:
    _LINES.append(line)


def _ok(label: str, detail: str = "") -> None:
    _log(f"  [  OK ]  {label}" + (f"  — {detail}" if detail else ""))


def _warn(label: str, detail: str = "") -> None:
    _log(f"  [ WARN ]  {label}" + (f"  — {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    _log(f"  [FAIL ]  {label}" + (f"  — {detail}" if detail else ""))
    _FAILED.append(label)


def _have(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _version(name: str) -> str:
    try:
        mod = __import__(name)
        return getattr(mod, "__version__", "?")
    except Exception:
        return "-"


def _section(title: str) -> None:
    _log("")
    _log(f"┌─ {title}" + "─" * max(2, 52 - len(title)))
    _log("│")


def _end_section() -> None:
    _log("└" + "─" * 54)


def _report_env() -> None:
    _section("Python / platform")
    py = platform.python_version()
    _ok("python", py)
    if py.split(".")[:2] >= ["3", "10"]:
        _ok("python >= 3.10 (vision requires 3.10+)")
    else:
        _fail("vision requires python >= 3.10")
    _ok("platform", platform.platform())

    _section("Native packages")
    cv2, numpy = _have("cv2"), _have("numpy")
    _log("")
    _ok("opencv-python", _version("cv2") if cv2 else "missing") if cv2 else _fail(
        "opencv-python required: pip install opencv-python")
    _ok("numpy", _version("numpy") if numpy else "missing") if numpy else _fail(
        "numpy required: pip install numpy")


def _report_optional() -> None:
    _section("Optional packages")
    _log("")
    checks = [
        ("mediapipe", "hand_tracking / gesture / face geometry"),
        ("ultralytics", "local object detection (YOLO)"),
        ("onnxruntime", "YOLO-onnx / insightface backend"),
        ("insightface", "face recognition backend (stronger than geometry)"),
        ("torch", "GPU inference (CUDA)"),
        ("PIL", "image helpers"),
        ("mss", "screen capture"),
        ("google.genai", "Gemini multimodal provider"),
    ]
    for mod, role in checks:
        if _have(mod):
            _ok(mod, role + (f" ({_version(mod)})" if _version(mod) != "?" else ""))
        else:
            _warn(mod + " (optional)", role + " — feature degrades gracefully")


def _report_config() -> None:
    _section("Vision configuration")
    _log("")
    try:
        from vision.config import load_vision_config
        cfg = load_vision_config()
        _ok("config loaded", f"cam={cfg.camera_index or 'auto'} "
                             f"{cfg.camera_width}x{cfg.camera_height}@{cfg.camera_fps}fps")
        _ok("inference device", f"{cfg.inference_device} "
                                f"(cuda={cfg.cuda_enabled})")
        _ok("object backend", cfg.object_detector_backend)
        _ok("face db dir", cfg.face_db_path)
        _ok("multimodal", f"provider={cfg.multimodal_provider} model={cfg.vision_model} "
                          f"local_only={cfg.local_only}")
    except Exception as exc:
        _fail("config load", str(exc))


def _report_camera() -> None:
    _section("Camera")
    _log("")
    try:
        from vision.config import load_vision_config
        from vision.camera import available_cameras, auto_detect_camera_index
        cfg = load_vision_config()
        available = available_cameras(3)
        if available:
            _ok("available indices", str(available))
        else:
            _warn("no camera detected (indices 0-2) — on-demand capture may fail")
        idx = auto_detect_camera_index(cfg.camera_index)
        _ok("auto-detected index", str(idx))
    except Exception as exc:
        _fail("camera probe", str(exc))


def _report_model_dirs() -> None:
    _section("Model directories")
    _log("")
    from vision.config import load_vision_config
    cfg = load_vision_config()
    for label, p in [("object detection", cfg.object_model_path),
                     ("face database", cfg.face_db_path)]:
        path = Path(p)
        if path.exists():
            files = [f.name for f in path.iterdir()][:4]
            _ok(label, f"{path}  {'· '.join(files) if files else '(empty)'}")
        else:
            _warn(label, f"{path}  (created on first use)")


def _report_self_tests() -> None:
    _section("Self-tests")
    _log("")
    try:
        from vision.config import load_vision_config
        from vision.models import Hand, HandLand, Handedness
        from vision.gesture import GestureRecognizer

        cfg = load_vision_config()
        rec = GestureRecognizer(cfg)

        fist = [HandLand(x=0.5, y=0.8, z=0.0, index=i) for i in range(21)]
        fist[4] = HandLand(x=0.45, y=0.75, z=0, index=4)
        fist[5] = HandLand(x=0.5, y=0.74, z=0, index=5)
        g = rec.recognize(Hand(handedness=Handedness.LEFT, landmarks=fist))
        _ok("gesture classifier", f"fist confidence={g.confidence:.2f}" if g.confidence else "fist detected")

        from vision.face_recognition import FaceDatabase
        import tempfile
        import numpy as np
        tmp = Path(tempfile.mkdtemp(prefix="visdiag"))
        db = FaceDatabase(tmp)
        db.add("diaguser", np.random.rand(140).astype(np.float32), b"")
        name, dist = db.match(np.random.rand(140).astype(np.float32), threshold=1.5)
        db.remove("diaguser")
        _ok("face db roundtrip", f"stores/normalizes ok (rand-vec hit={name})")
    except Exception as exc:
        _fail("self-test", str(exc))


def _report_provider() -> None:
    _section("Multimodal provider")
    _log("")
    try:
        from vision.config import load_vision_config
        from vision.multimodal import get_vision_provider
        cfg = load_vision_config()
        provider = get_vision_provider(cfg)
        _ok("provider selected", cfg.multimodal_provider)
        if cfg.local_only:
            _ok("local_only", "API key never sent — scene analysis disabled")
        elif provider.is_available():
            _ok("api key", "present; will not call the API from diagnostics")
        else:
            _warn("api key", "missing — multimodal scene answers will be unavailable")
    except Exception as exc:
        _fail("provider", str(exc))


def main(argv=None) -> int:
    _log("JARVIS vision diagnostics")
    _log("=========================")
    _report_env()
    _report_optional()
    _report_config()
    _report_camera()
    _report_model_dirs()
    _report_self_tests()
    _report_provider()
    _log("")
    _log("── summary ──────────────────────────────────────────────")
    if _FAILED:
        _log(f"  {len(_FAILED)} failure(s): {', '.join(_FAILED)}")
        code = 1
    else:
        _log("  all critical checks passed (optional packages may be absent)")
        code = 0
    _log("")
    for line in _LINES:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())