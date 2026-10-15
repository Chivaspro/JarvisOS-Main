"""Traceback analysis: which file, which line, and how dangerous is it to touch?

The engine is only as good as this step. A traceback usually ends in a
third-party frame (``requests``, ``google``, Qt internals) — patching those does
nothing, and the *interesting* frame is the first one that belongs to this
project. So frames are walked innermost-first and the first first-party file
wins.

Every candidate then gets a **risk class**, because "let the assistant rewrite
its own source" must be bounded by something the code can enforce:

* ``low``   — ``actions/``, ``plugins/``, ``evolution/``, ``recovery/``,
              ``documents/``, ``mcp/``, ``connect/``. Leaf capability code,
              hot-reloadable, nothing that runs the app itself.
* ``high``  — ``core/``, ``brain/``, ``memory/``, ``capabilities/``, ``voice/``,
              ``vision/``, ``security/``, ``ui*.py``, ``main.py`` — anything that
              can brick the assistant, plus a small protected list that may never
              be patched automatically at all.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["PROTECTED_NAMES", "parse", "risk_of", "root"]

#: Never patched without an explicit on-screen approval, whatever the policy.
PROTECTED_NAMES: frozenset[str] = frozenset({
    "main.py", "ui.py", "ui_settings.py", "ui_theme.py", "ui_onboarding.py",
    "requirements.txt", "setup.py", "start_jarvis.py", "run_jarvis.bat",
    "patcher.py", "boot_sentry.py", "service.py", "confirm.py",
})

#: Package roots whose files are safe to patch autonomously.
LOW_RISK_ROOTS: frozenset[str] = frozenset({
    "actions", "plugins", "evolution", "recovery", "documents", "mcp",
    "connect", "knowledge",
})

_FILE_RE = re.compile(
    r'File\s+["\']([^"\']+\.py)["\'],\s+line\s+(\d+)(?:,\s+in\s+([^\n\r]+))?',
    re.IGNORECASE,
)
_SKIP_MARKERS = ("site-packages", ".venv", "\\lib\\python", "/lib/python",
                 "node_modules", "pytest", "importlib", "<frozen")


def root() -> Path:
    return Path(__file__).resolve().parent.parent


def risk_of(path: Path) -> tuple[str, str]:
    """``(risk, reason)`` for one candidate file."""
    name = path.name
    if name in PROTECTED_NAMES:
        return "protected", f"{name} is on the protected list"
    try:
        relative = path.resolve().relative_to(root())
    except Exception:
        return "protected", "the file is outside the project"
    parts = relative.parts
    top = parts[0] if parts else ""
    if top in LOW_RISK_ROOTS and len(parts) <= 3:
        return "low", f"leaf capability code ({top}/)"
    if top in ("actions", "plugins", "documents", "mcp", "connect") or \
            top.startswith("plugins"):
        return "low", f"leaf capability code ({top}/)"
    return "high", f"{top or 'project'} code — needs approval"


def _resolve(raw_path: str) -> Path | None:
    """Find the real file on disk for a traceback frame."""
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    for base in (root(), Path.cwd()):
        joined = base / candidate
        if joined.exists():
            return joined.resolve()
    by_name = root() / candidate.name
    if by_name.exists():
        return by_name.resolve()
    for folder in ("actions", "core", "capabilities", "brain", "plugins",
                   "evolution", "memory", "vision", "voice", "recovery"):
        candidate2 = root() / folder / candidate.name
        if candidate2.exists():
            return candidate2.resolve()
    return None


def parse(traceback_text: str) -> dict[str, Any]:
    """Analyse one traceback.

    Returns a dict with ``found`` (bool), ``file``/``line``/``function``,
    ``exception_type``/``exception_message``, ``risk``/``risk_reason`` and the
    raw text. Never raises.
    """
    result: dict[str, Any] = {
        "found": False, "file": None, "line": None, "function": None,
        "exception_type": "", "exception_message": "",
        "risk": "protected", "risk_reason": "no first-party frame found",
        "traceback": str(traceback_text or "")[-8000:], "skipped": [],
    }
    text = str(traceback_text or "")
    if not text.strip():
        return result

    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if lines:
        last = lines[-1]
        if ":" in last:
            kind, _, message = last.partition(":")
            result["exception_type"] = kind.strip()
            result["exception_message"] = message.strip()
        else:
            result["exception_type"] = last

    for raw_path, line_str, func_name in reversed(_FILE_RE.findall(text)):
        low = raw_path.lower()
        if any(marker in low for marker in _SKIP_MARKERS):
            result["skipped"].append(raw_path)
            continue
        resolved = _resolve(raw_path)
        if resolved is None:
            result["skipped"].append(raw_path)
            continue
        risk, reason = risk_of(resolved)
        # Protected files ARE returned as candidates: the repair card needs
        # them so the captured fault (and its proposed patch) can be shown for
        # the owner's explicit approval. `risk == "protected"` still makes
        # _auto_allowed() refuse every automatic application.
        result.update({
            "found": True,
            "file": str(resolved),
            "line": int(line_str),
            "function": (func_name or "").strip() or None,
            "risk": risk,
            "risk_reason": reason,
        })
        break
    return result
