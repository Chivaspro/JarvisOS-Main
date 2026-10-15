"""Which files the DGM layer may touch, and which are untouchable.

The protected list is the user's own: Gemini/API configuration and secrets,
authentication, safety and permission checks, startup code, core system files,
the critical PC-control safety code and the sandbox itself. The allowed list is
equally explicit: generated skills, skill implementations, tool adapters and
selected prompts.

Default deny. A path that matches nothing is refused, and ``refusal()`` always
names *why* — "protected core file" is a different answer from "not on the
allowed list", and the user deserves the real one.
"""

from __future__ import annotations

from pathlib import PurePosixPath

__all__ = [
    "ALLOWED_PREFIXES",
    "PROTECTED_FILES",
    "PROTECTED_PREFIXES",
    "is_protected",
    "refusal",
]

#: Never modified, at any depth. Prefixes are matched against a POSIX-style
#: relative path, so they read the same on Windows and Linux.
PROTECTED_PREFIXES: tuple[str, ...] = (
    "config/",              # api_keys.json, settings.py, certs, voice config
    "security/",            # permissions, approvals, rules, the undo stack
    "core/",                # model router, TTS, stability, maintenance
    "app/",                 # providers, the plugin loader, config guard
    "capabilities/",        # PC/browser control and its safety gating
    "memory/",              # both memory stores
    "voice/",
    "vision/",
    "brain/",
    "observability/",
    "recovery/",            # the patch engine that would perform a rollback
    "dashboard/",
    "connect/",
    "mcp_host/",
    "evolution/crucible.py",   # the verifier must not be modifiable by what it verifies
    "evolution/vault.py",
    "evolution/manager.py",
)

#: Single files that are never touched, wherever they live.
PROTECTED_FILES: frozenset[str] = frozenset({
    "main.py", "start_jarvis.py", "setup_jarvis.py", "setup_jarvis.bat",
    "ui.py", "ui_settings.py", "ui_layout.py", "ui_theme.py",
    "requirements.txt", "package.json", "package-lock.json",
})

#: The only places a change may land. ``plugins/`` holds forged skills, which is
#: exactly what the DGM layer exists to improve.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "plugins/",
    "evolution/adapters/",
)


def _norm(relpath: object) -> str:
    """A POSIX-style relative path with no leading './' or drive."""
    text = str(relpath or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def is_protected(relpath: object) -> bool:
    """True when this path must never be modified by the DGM layer."""
    path = _norm(relpath)
    if not path:
        return True
    if path.startswith("config/") or path.startswith("security/"):
        return True
    if path in PROTECTED_FILES or PurePosixPath(path).name in PROTECTED_FILES:
        return True
    if path.endswith((".env", ".env.example")) or "api_keys.json" in path:
        return True
    return path.startswith(PROTECTED_PREFIXES)


def refusal(relpath: object) -> str:
    """Why this path cannot be changed ('' when it can)."""
    path = _norm(relpath)
    if not path:
        return "the path is empty"
    if path.startswith("config/") or "api_keys.json" in path or path.endswith(".env"):
        return "it holds configuration or credentials, which the evolution engine must never rewrite"
    if path.startswith("security/"):
        return "it is part of the safety and permission system"
    if path in PROTECTED_FILES or PurePosixPath(path).name in PROTECTED_FILES:
        return "it is a core system file"
    if is_protected(path):
        return "it sits in a protected subsystem"
    if not path.startswith(ALLOWED_PREFIXES):
        return "it is not on the list of areas the evolution engine may improve"
    return ""
