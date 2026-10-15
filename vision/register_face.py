"""Face registration CLI for JARVIS vision.

Usage:
    python -m vision.register_face --name "Alex"                 # live still from camera
    python -m vision.register_face --name "Alex" --image photo.jpg
    python -m vision.register_face --list
    python -m vision.register_face --remove "Alex"

Reads the same config as the app (config/api_keys.json → `vision` section).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load(frame_source, name: str) -> None:
    from vision.config import load_vision_config
    from vision.face_recognition import FaceDatabase, FaceRecognizer
    from vision import camera as cam_mod

    cfg = load_vision_config()
    fr = FaceRecognizer(cfg)

    frame = None
    if frame_source:
        import cv2
        frame = cv2.imread(str(frame_source))
        if frame is None:
            print(f"[REGISTER]  could not read image: {frame_source}")
            sys.exit(2)
    else:
        mgr = cam_mod.get_camera_manager(cfg)
        frame = mgr.take_photo()
        if frame is None:
            print("[REGISTER]  camera returned no frame — is the sensor in use?")
            sys.exit(2)

    ok = fr.register(frame, name)
    if not ok:
        print(f"[REGISTER]  FAILED: no face detectable in the capture.")
        sys.exit(1)
    db = FaceDatabase(Path(cfg.face_db_path))
    print(f"[REGISTER]  '{name}' registered OK. Known identities: {db.names()}")


def _list_identities() -> None:
    from vision.config import load_vision_config
    from vision.face_recognition import FaceDatabase
    cfg = load_vision_config()
    db = FaceDatabase(Path(cfg.face_db_path))
    names = db.names()
    print(f"[FACES]  {len(names)} registered identity(ies):")
    for n in names:
        print(f"    - {n}")


def _remove_identity(name: str) -> None:
    from vision.config import load_vision_config
    from vision.face_recognition import FaceDatabase
    cfg = load_vision_config()
    db = FaceDatabase(Path(cfg.face_db_path))
    if db.remove(name):
        print(f"[FACES]  removed '{name}'")
    else:
        print(f"[FACES]  '{name}' not found in database")
        sys.exit(1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vision.register_face",
                                 description="Register faces into the JARVIS face DB.")
    ap.add_argument("--name", help="identity to (re)register")
    ap.add_argument("--image", help="photo path (BMP/JPG/PNG) instead of live camera")
    ap.add_argument("--list", action="store_true", help="print known identities")
    ap.add_argument("--remove", help="delete an identity")
    args = ap.parse_args(argv)

    if args.list:
        _list_identities()
        return 0
    if args.remove:
        _remove_identity(args.remove)
        return 0
    if not args.name:
        ap.print_help()
        return 2
    _load(args.image, args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())