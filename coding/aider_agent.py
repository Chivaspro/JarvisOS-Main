"""Aider backend (task 5.2) — focused, precise code edits.

Aider installs natively on Windows via pip and runs as a subprocess with a
non-interactive prompt; ``--yes-always`` keeps it unattended, and the working
tree is isolated (worktree) or explicitly approved by the caller (the router
enforces both). Availability is probed once and cached: ``aider --version``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

_avail_lock = threading.Lock()
_avail_cache: bool | None = None

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _binary() -> str:
    return os.environ.get("AIDER_BIN", "aider")


def available(refresh: bool = False) -> bool:
    """Is the aider executable on PATH? Cached; --version proves it runs."""
    global _avail_cache
    with _avail_lock:
        if _avail_cache is not None and not refresh:
            return _avail_cache
        ok = False
        try:
            r = subprocess.run(
                [_binary(), "--version"],
                capture_output=True, text=True, timeout=20,
                **_NO_WINDOW,
            )
            ok = r.returncode == 0
        except Exception:
            ok = False
        _avail_cache = ok
        return ok


def execute(
    instruction: str,
    files: list[str] | None = None,
    tree: str | os.PathLike = ".",
    timeout_s: float = 900.0,
) -> dict:
    """Run one focused edit. Returns {ok, backend, output, files}. Never raises
    into the router: failures come back as ok=False with the captured output."""
    if not available():
        return {"ok": False, "backend": "aider",
                "output": "Aider is not installed (pip install aider-chat).",
                "files": list(files or [])}
    cmd = [_binary(), "--yes-always", "--no-auto-commits", "--message", instruction]
    for f in (files or [])[:20]:
        cmd.append(str(f))
    try:
        r = subprocess.run(
            cmd, cwd=str(tree), capture_output=True, text=True,
            timeout=max(30.0, float(timeout_s)), **_NO_WINDOW,
        )
        output = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        ok = r.returncode == 0
        if not ok and not output.strip():
            output = f"aider exited with code {r.returncode}"
        return {"ok": ok, "backend": "aider", "output": output[-4000:],
                "files": list(files or [])}
    except subprocess.TimeoutExpired:
        return {"ok": False, "backend": "aider",
                "output": f"aider timed out after {int(timeout_s)} s.",
                "files": list(files or [])}
    except Exception as exc:
        return {"ok": False, "backend": "aider",
                "output": f"aider failed to run: {exc}",
                "files": list(files or [])}


def status_detail() -> dict:
    return {"name": "aider", "available": available(), "kind": "subprocess"}
