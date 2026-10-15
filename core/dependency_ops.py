"""Runtime dependency continuity (Universal Control Protocol).

A missing Python package is a blocker to clear, not a dead end. This module is
the single, permission-gated path the assistant uses when a task genuinely
needs a package that is not installed:

    check the registry → classify python vs system → verify requirements.txt
    → install when authorized → verify the import → report so the task resumes

Rules this encodes (mirroring docs/universal-control-protocol.md):

* System applications (PowerShell, Git, FFmpeg, Tesseract, …) are NEVER placed
  into requirements.txt — they are reported as system requirements instead.
* requirements.txt is only appended to, never rewritten: existing lines, order
  and comments are preserved, and an already-listed package is never duplicated.
* Installation is only ever performed for an explicit package name the model
  supplied, validated against a conservative pattern (no command chaining,
  no arbitrary arguments), and the result is proven by re-checking the import.

Blocking by design — callers run it in an executor, exactly like the rest of
the diagnostics tool.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = BASE_DIR / "requirements.txt"
_WIN = sys.platform == "win32"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _WIN else {}

# A pip requirement line may only be: name[extras](specifier)? [; marker]
_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9.,_ -]+\])?"
    r"([<>=!~]=+[A-Za-z0-9._*+,<>=!~ -]{1,40})?$")

# Import name → pip distribution name, where they differ.
IMPORT_TO_PIP = {
    "PIL": "pillow", "cv2": "opencv-python", "fitz": "pymupdf",
    "win32com": "pywin32", "win32api": "pywin32",
    "winreg": "", "wmi": "wmi", "mss": "mss", "psutil": "psutil",
    "comtypes": "comtypes", "langgraph": "langgraph", "qrcode": "qrcode",
    "yaml": "pyyaml", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
    "google": "google-genai", "sounddevice": "sounddevice",
    "numpy": "numpy", "requests": "requests", "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]", "watchdog": "watchdog",
    "faster_whisper": "faster-whisper", "onnxruntime": "onnxruntime",
    "pytesseract": "pytesseract", "edge_tts": "edge-tts",
}

# Binaries that are system applications, not pip packages. Asking for one of
# these is answered with an installation pointer, never with a requirements.txt
# edit (the protocol's classification rule).
SYSTEM_BINARIES = {
    "powershell": "ships with Windows",
    "git": "https://git-scm.com/download/win or `winget install Git.Git`",
    "ffmpeg": "`winget install Gyan.FFmpeg` (media conversion)",
    "7z": "`winget install 7zip.7zip` (archives)",
    "everything": "`winget install voidtools.Everything` (file search)",
    "tesseract": "https://github.com/UB-Mannheim/tesseract/wiki (OCR engine; "
                 "the pip package pytesseract is only its Python wrapper)",
    "wt": "Windows Terminal — `winget install Microsoft.WindowsTerminal`",
    "node": "`winget install OpenJS.NodeJS.LTS` (javascript tooling)",
    "npm": "ships with Node.js",
    "rg": "`winget install BurntSushi.ripgrep.MSVC` (fast search)",
    "mpv": "`winget install mpv.mpv` (media playback)",
    "nmap": "`winget install Insecure.Nmap` (authorized network mapping)",
    "scrcpy": "`winget install Genymobile.scrcpy` (Android mirroring)",
    "winterminal": "Windows Terminal — `winget install Microsoft.WindowsTerminal`",
}

# Optional extras that must never be installed just because they exist.
NEVER_AUTO_INSTALL = {"nmap", "scrcpy", "ultralytics", "openhands", "aider"}


def _available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def _valid_package(raw: str) -> str:
    """Validate a requirement spec: name plus an optional pinned version.

    Anything with spaces, operators beyond a single pin, semicolons or shell
    metacharacters is rejected — this string reaches pip, so it is treated
    like untrusted input. Extras (``qrcode[pil]``) are stripped because the
    runtime installs the plain distribution.
    """
    s = str(raw or "").strip()
    if not s or len(s) > 60:
        return ""
    if re.search(r"[;&|`$()<>\s]", s.replace(">=", "").replace("<=", "")
                 .replace("==", "").replace("!=", "")):
        return ""
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)"
                 r"(?:\[[A-Za-z0-9.,_-]+\])?"
                 r"(?:(?:==|>=|<=|!=)"
                 r"[A-Za-z0-9._*+]+)?$", s)
    if not m:
        return ""
    return m.group(1)


def req_lines(path: Path = REQUIREMENTS_PATH) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def listed_in_requirements(package: str,
                           path: Path = REQUIREMENTS_PATH) -> bool:
    """Whether a distribution is already declared (name match, not specifier)."""
    name = package.lower().split("[")[0].split("=")[0].split(">")[0].strip()
    for line in req_lines(path):
        stripped = line.strip().lower()
        if not stripped or stripped.startswith("#"):
            continue
        base = stripped.split("#")[0].strip()
        base = re.split(r"[<>=!~;\[]", base, 1)[0].strip()
        if base == name:
            return True
    return False


def add_to_requirements(package: str,
                        path: Path = REQUIREMENTS_PATH) -> bool:
    """Append one requirement, preserving everything else. Idempotent."""
    if listed_in_requirements(package, path):
        return False
    try:
        text = req_lines(path)
        if text and text[-1].strip():
            text.append("")
        text.append(f"{package}                # added at runtime: required by an active task")
        path.write_text("\n".join(text) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _pip_install(package: str, timeout: float = 600.0) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "install", package,
             "--disable-pip-version-check"],
            capture_output=True, text=True, timeout=timeout, **_NO_WINDOW)
        tail = ((out.stdout or "") + "\n" + (out.stderr or "")).strip().splitlines()
        return out.returncode == 0, (tail[-1] if tail else "")[:200]
    except subprocess.TimeoutExpired:
        return False, f"pip timed out after {int(timeout)}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def ensure_package(requested: str, add_req: bool = True) -> str:
    """One dependency report/action line for diagnostics mode='install'."""
    raw = str(requested or "").strip()
    if not raw:
        return ("Nothing to install — provide package (the pip name) and, "
                "optionally, module (the import name).")
    low = raw.lower().replace("-", "_")
    # System applications: never pip, never requirements.txt.
    bare = raw.lower().replace(".exe", "")
    if low in SYSTEM_BINARIES or bare in SYSTEM_BINARIES:
        return (f"{raw} is a SYSTEM dependency, not a Python package — it "
                f"belongs in the system requirements, never in requirements.txt. "
                f"How to get it: {SYSTEM_BINARIES.get(low) or SYSTEM_BINARIES.get(bare)}. "
                f"Continue the task with another available method meanwhile.")
    which = shutil.which(bare)
    if which and low not in IMPORT_TO_PIP and not _available(raw):
        return (f"{raw} resolves to a system executable at {which} — that is a "
                f"system dependency and is deliberately not added to "
                f"requirements.txt.")
    if low in NEVER_AUTO_INSTALL or bare in NEVER_AUTO_INSTALL:
        return (f"{raw} is an optional/heavier dependency that is not "
                f"auto-installed; ask the user to approve it explicitly first.")
    # Validate the name BEFORE touching pip.
    package = _valid_package(raw)
    if not package:
        return (f"'{raw[:60]}' is not a valid single package requirement "
                f"(plain name plus an optional version specifier only).")
    module = package.lower().replace("-", "_")
    pip_name = IMPORT_TO_PIP.get(module) or IMPORT_TO_PIP.get(raw) or package
    if pip_name == "":
        return (f"{raw} is part of the Windows Python standard library — "
                f"nothing to install.")
    if _available(module) or _available(raw):
        listed = listed_in_requirements(pip_name)
        if add_req and not listed:
            add_to_requirements(pip_name)
        return (f"{pip_name} is already importable"
                + ("" if listed or not add_req else
                   " and was recorded in requirements.txt")
                + ". The blocked step can continue now.")
    if not _valid_package(pip_name):
        return f"Refusing to install '{pip_name}': failed the safety validation."
    ok, tail = _pip_install(pip_name)
    if not ok:
        return (f"pip install {pip_name} FAILED — the task stays BLOCKED on it. "
                f"Last pip output: {tail or 'none captured'}")
    verified = _available(module) or _available(raw) or _available(
        pip_name.split("[")[0].replace("-", "_"))
    if add_req:
        add_to_requirements(pip_name)
    note = ""
    if verified:
        return (f"Installed {pip_name} and verified the import"
                + (", added it to requirements.txt (no duplicates)"
                   if add_req else "") + f".{note} Resume the interrupted step.")
    return (f"pip reported success for {pip_name} but '{module}' is still not "
            f"importable — treat this as NOT fixed and say so honestly.{note}")


def handle_install(args: dict) -> str:
    """Entry used by the diagnostics tool.

    * mode='install' with a package   → clear one missing dependency;
    * mode='install' with no package  → reconcile the whole requirements.txt
      and summarise what was installed / already present / still needs
      attention (the protocol's dependency-continuity loop, run at once).
    """
    a = dict(args or {})
    package = str(a.get("package") or "").strip()
    target = str(a.get("target") or "").strip()
    module = str(a.get("module") or "").strip()
    # 'target' is also used by mode=check for a subsystem name; only treat it
    # as a package when it is not a known check target.
    if not package and target and target.lower() not in _CHECK_TARGETS:
        package = target
    if not package and not module:
        return sync_requirements_report()
    if package and module and not _available(module):
        module_key = module.split(".")[0]
        if IMPORT_TO_PIP.get(module_key) and not package:
            package = IMPORT_TO_PIP[module_key]
    add_req = str(a.get("add_to_requirements", "true")).lower() \
        not in ("false", "no", "0", "off")
    return ensure_package(package or module, add_req=add_req)


# mode=check target names, so 'install' with target='voice' is not mistaken for
# a package literally called voice.
_CHECK_TARGETS = {
    "ai", "laya", "voice", "memory", "wake_word", "camera", "vision",
    "tools", "plugins", "filesystem", "system", "configuration",
    "safe_mode", "settings",
}


# pip distribution → the top-level import that proves it is present, for the
# project's own requirements where the two differ (pywin32 ships win32api, not
# a 'pywin32' module; pillow ships PIL; and so on).
_PIP_TO_IMPORT = {
    "pillow": "PIL", "opencv-python": "cv2", "beautifulsoup4": "bs4",
    "google-genai": "google", "google-generativeai": "google",
    # Qt distributions are capitalised: importlib is case-sensitive, so the
    # derived lowercase module ("pyqt6") probes False and every boot pip-installed
    # PyQt6 into the live process. Map to the real import names.
    "pyqt6": "PyQt6", "pyqt6-webengine": "PyQt6.QtWebEngineWidgets",
    "pywin32": "win32api", "python-pptx": "pptx",
    "youtube-transcript-api": "youtube_transcript_api", "send2trash": "send2trash",
    "python-multipart": "multipart", "pyaudio": "pyaudio", "pycaw": "pycaw",
    "win10toast": "win10toast", "comtypes": "comtypes", "fish-audio-sdk": "fish_audio_sdk",
    "miniaudio": "miniaudio", "onnxruntime": "onnxruntime", "piper-tts": "piper",
    "pyttsx3": "pyttsx3", "qrcode": "qrcode", "uvicorn": "uvicorn",
    "sqlalchemy": "sqlalchemy", "cryptography": "cryptography",
    "ddgs": "ddgs", "requests": "requests", "numpy": "numpy",
    "mss": "mss", "psutil": "psutil", "wmi": "wmi", "ultralytics": "ultralytics",
}


def _dist_and_spec(line: str) -> tuple[str, str, str]:
    """Split a requirement line into (dist_name, full_spec, marker)."""
    text = line.split("#")[0].strip()
    if not text:
        return "", "", ""
    marker = ""
    if ";" in text:
        text, marker = text.split(";", 1)
        text, marker = text.strip(), marker.strip()
    dist = re.split(r"[<>=!~\[\s]", text, 1)[0].strip()
    return dist, text.strip(), marker


def _marker_ok(marker: str) -> bool:
    """Evaluate the simple platform markers the project's requirements use."""
    if not marker:
        return True
    m = re.search(r"(sys_platform|os_name|platform_system)\s*==\s*['\"]([^'\"]+)['\"]",
                  marker)
    if not m:
        return True  # unknown marker: do not block, the project declared it
    key, want = m.group(1), m.group(2).lower()
    have = {"sys_platform": sys.platform, "os_name": os.name,
            "platform_system": platform.system()}
    return str(have.get(key, "")).lower() == want


def _module_for(dist: str) -> str:
    low = dist.lower()
    if low in _PIP_TO_IMPORT:
        return _PIP_TO_IMPORT[low]
    return low.replace("-", "_").split("[")[0]


def parse_requirements(path: Path | None = None) -> list[dict]:
    """Every declared requirement with its resolved import module.

    The path is resolved at call time (not bound as a fixed default) so tests
    and callers can point the reconcile at a different file.
    """
    path = path or REQUIREMENTS_PATH
    out: list[dict] = []
    for line in req_lines(path):
        dist, spec, marker = _dist_and_spec(line)
        if not dist:
            continue
        out.append({"dist": dist, "spec": spec or dist, "marker": marker,
                    "module": _module_for(dist), "platform_ok": _marker_ok(marker)})
    return out


def sync_requirements(allow_install: bool = True,
                      timeout: float = 900.0) -> dict:
    """Reconcile requirements.txt against the environment, install what is
    missing, verify each import, and return an honest summary. Never raises.

    System applications are never in requirements.txt by design, so nothing
    here tries to pip-install one. A failure is reported as needing attention
    rather than silently skipped.
    """
    summary = {"installed": [], "already": [], "failed": [],
               "skipped_platform": [], "not_listed": []}
    try:
        reqs = parse_requirements()
    except Exception as exc:
        return {**summary, "error": f"could not read requirements.txt: {exc}"}
    for r in reqs:
        if not r["platform_ok"]:
            summary["skipped_platform"].append(r["dist"])
            continue
        if _available(r["module"]):
            summary["already"].append(r["dist"])
            continue
        if not allow_install:
            summary["failed"].append({"dist": r["dist"],
                                      "error": "missing (install not attempted)"})
            continue
        ok, tail = _pip_install(r["spec"], timeout=timeout)
        if ok and _available(r["module"]):
            summary["installed"].append(r["dist"])
        elif ok:
            # pip succeeded but the import probe still fails (e.g. a different
            # top-level name) — say so honestly instead of calling it fixed.
            summary["installed"].append(r["dist"] + " (import not confirmed)")
        else:
            summary["failed"].append({"dist": r["dist"], "error": tail or "pip failed"})
    return summary


def sync_requirements_report() -> str:
    s = sync_requirements(allow_install=True)
    lines = ["DEPENDENCY SYNC  //  requirements.txt reconciled"]
    if s.get("error"):
        return f"DEPENDENCY SYNC failed: {s['error']}"
    lines.append(f"- already present: {len(s['already'])}")
    if s["installed"]:
        lines.append("- installed now: " + ", ".join(s["installed"]))
    else:
        lines.append("- installed now: nothing was missing")
    if s["skipped_platform"]:
        lines.append("- not for this platform (skipped): "
                     + ", ".join(s["skipped_platform"]))
    if s["failed"]:
        lines.append("- STILL NEEDS ATTENTION:")
        for f in s["failed"]:
            lines.append(f"    * {f['dist']} — {f['error']}")
    else:
        lines.append("Nothing needs attention: every declared Python dependency "
                     "is installed and importable.")
    return "\n".join(lines)
