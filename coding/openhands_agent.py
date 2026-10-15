"""OpenHands backend (task 5.3) — autonomous multi-file coding tasks.

OpenHands officially runs through Docker Desktop (WSL2) on Windows, so
availability is probed via its runtime (Docker + the OpenHands image), never
by importing the package. The probe verdict is cached; absence yields a clear
setup message and the router falls back to Aider. This backend is optional by
design: JARVIS works fully without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

IMAGE = os.environ.get("OPENHANDS_IMAGE", "docker.all-hands.dev/all-hands-ai/openhands")
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

_probe_lock = threading.Lock()
_probe_cache: dict[str, bool] = {}


def _docker_present() -> bool:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=25, **_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def _image_present() -> bool:
    try:
        r = subprocess.run(["docker", "images", "-q", IMAGE],
                           capture_output=True, text=True, timeout=25, **_NO_WINDOW)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def available(refresh: bool = False) -> bool:
    """Docker + OpenHands image reachable? Cached per session."""
    global _probe_cache
    with _probe_lock:
        if "ok" in _probe_cache and not refresh:
            return _probe_cache["ok"]
        ok = _docker_present() and _image_present()
        _probe_cache["ok"] = ok
        return ok


def setup_hint() -> str:
    """What the user must install for OpenHands to become available."""
    if not _docker_present():
        return ("OpenHands needs Docker Desktop (with WSL2) installed and "
                "running. Start Docker Desktop, then retry.")
    if not _image_present():
        return (f"OpenHands image is missing. Run: docker pull {IMAGE}")
    return "OpenHands is configured; the last run failed — see the captured output."


def execute(
    instruction: str,
    tree: str | os.PathLike = ".",
    timeout_s: float = 1800.0,
    mount_tree: bool = True,
) -> dict:
    """Run one autonomous task in the OpenHands container. Never raises into
    the router; failures return ok=False with evidence."""
    if not available():
        return {"ok": False, "backend": "openhands",
                "output": f"OpenHands is not available. {setup_hint()}"}
    tree_path = str(tree)
    host_root = os.environ.get("OPENHANDS_WORKSPACE", tree_path)
    workspace = tree_path if mount_tree else host_root
    cmd = [
        "docker", "run", "--rm",
        "-e", "SANDBOX_USER_ID=0",
        "-v", f"{workspace}:/opt/workspace:Z",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        IMAGE,
        "--workspace-dir", "/opt/workspace",
        "--task", instruction,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=max(60.0, float(timeout_s)), **_NO_WINDOW,
        )
        output = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return {"ok": r.returncode == 0, "backend": "openhands",
                "output": output[-6000:] or f"exited with code {r.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "backend": "openhands",
                "output": f"OpenHands timed out after {int(timeout_s)} s."}
    except Exception as exc:
        return {"ok": False, "backend": "openhands",
                "output": f"OpenHands failed to run: {exc}"}


def status_detail() -> dict:
    return {
        "name": "openhands",
        "available": available(),
        "docker": _docker_present(),
        "image": _image_present(),
        "kind": "container",
    }
