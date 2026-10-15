"""
MARK XL — Dependency auto-installer.

Called automatically on first launch and after engine reconfiguration.
Installs only the packages that are actually missing, then exits cleanly.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable

# ── Package lists ─────────────────────────────────────────────────────────
# Each entry: (import_name, pip_package_name)

_CORE: list[tuple[str, str]] = [
    ("psutil",             "psutil"),
    ("PIL",                "pillow"),
    ("sounddevice",        "sounddevice"),
    ("numpy",              "numpy"),
    ("requests",           "requests"),
    ("ddgs",               "ddgs"),
    ("mss",                "mss"),
    ("cv2",                "opencv-python"),
    ("soundfile",          "soundfile"),
    ("miniaudio",          "miniaudio"),
    ("send2trash",         "send2trash"),
    ("pptx",               "python-pptx"),
    ("youtube_transcript_api", "youtube-transcript-api"),
]

# Windows-only helpers (toast notifications). comtypes/pycaw used to live here
# for a volume-control path that no longer exists — nothing in the project
# imports either, so a first launch no longer installs them.
_WINDOWS: list[tuple[str, str]] = [
    ("win10toast", "win10toast"),
    # win32com drives the file controller; requirements.txt already pins it for
    # win32, so the installer installing it keeps the two in step.
    ("win32com", "pywin32"),
]

# Local control engines (capabilities/). Their absence is reported honestly by
# the router, so they are installed here too — but they stay optional: the
# project runs without them and simply refuses the actions they serve.
_CONTROL: list[tuple[str, str]] = [
    ("playwright", "playwright"),
    ("pywinauto",   "pywinauto"),
    ("pyautogui",   "pyautogui"),
]

# ── Feature extras (installed on demand, never at first launch) ────────────
# The document architect, the clipboard sentry, the smart-home layer and the
# Instagram link each need their own package. None of them is required to
# *start* J.A.R.V.I.S.: every tool names the missing package and its pip command
# in its own answer instead of failing, so this table exists to be asked, and to
# install one feature when the user asks for it.
#   (feature, import name, pip package, what it enables)
_FEATURE_EXTRAS: list[tuple[str, str, str, str]] = [
    ("documents", "docx",     "python-docx", "Word reports and letters"),
    ("documents", "openpyxl", "openpyxl",    "Excel workbooks"),
    ("documents", "pptx",     "python-pptx", "PowerPoint decks"),
    ("documents", "pypdf",    "pypdf",       "PDF merge and page extraction"),
    ("documents", "reportlab", "reportlab", "PDF writing without a converter"),
    ("clipboard", "pyperclip", "pyperclip",
     "clipboard read/write (tkinter is the fallback and needs nothing)"),
    ("smart_home", "kasa",    "python-kasa", "local Kasa/Tapo plugs and bulbs"),
    ("instagram", "instagrapi", "instagrapi", "the user's own Instagram account"),
    # The connector is deliberately on-demand rather than in requirements.txt:
    # composio is heavy, and a machine without it must still start J.A.R.V.I.S.
    # The feature name is the SAME string as the Settings permission switch and
    # as connectors.tool.FEATURE -- "external_apps" in all three places -- so one
    # name means one thing, and it is NOT "connect", which is already the
    # JarvisOS phone-pairing gateway's flag.
    #
    # This row is what makes the package installable by the command the
    # connector's own error message names. install_all_missing() walks exactly
    # this table, so without a row here "setup_jarvis --install-missing" would
    # silently not install composio and the hint would be a promise the codebase
    # does not keep.
    ("external_apps", "composio", "composio",
     "the connector: Spotify, Gmail, GitHub and every other external app"),
]

#: MCP servers run as child processes over stdio. The vendored Spotify server and
#: any server the user adds need a Node runtime — a real requirement, so it is
#: probed for real rather than assumed.
NODE_MIN_MAJOR = 18


def node_status() -> tuple[bool, str]:
    """(ready, detail) for the Node runtime the MCP host needs. Never raises."""
    try:
        result = subprocess.run(["node", "--version"], capture_output=True,
                                timeout=8)
    except FileNotFoundError:
        return (False, "node is not on PATH")
    except Exception as exc:
        return (False, f"node could not be run ({type(exc).__name__}: {exc})")
    text = (result.stdout or b"").decode(errors="replace").strip()
    if result.returncode != 0 or not text:
        return (False, "`node --version` did not answer")
    try:
        major = int(text.lstrip("v").split(".", 1)[0])
    except Exception:
        return (False, f"unreadable node version {text!r}")
    if major < NODE_MIN_MAJOR:
        return (False, f"node {text} is older than v{NODE_MIN_MAJOR}")
    return (True, f"node {text}")


def feature_status() -> list[dict]:
    """One row per on-demand package, from a real import probe.

    Probes only — it installs nothing. Each row names the feature that needs the
    package and the command that would add it, so a diagnostic line can be
    acted on without a second lookup.
    """
    rows: list[dict] = []
    for feature, module, package, purpose in _FEATURE_EXTRAS:
        ready = _available(module)
        rows.append({"feature": feature, "module": module, "package": package,
                     "purpose": purpose, "ready": ready,
                     "detail": "" if ready else f"missing {package}",
                     "fix": "" if ready else f"pip install {package}"})
    node_ready, node_detail = node_status()
    rows.append({"feature": "mcp_host", "module": "node",
                 "package": f"Node.js >= {NODE_MIN_MAJOR}",
                 "purpose": "MCP servers over stdio (Spotify, and any you add)",
                 "ready": node_ready, "detail": node_detail,
                 "fix": "" if node_ready else
                        f"Install Node.js {NODE_MIN_MAJOR}+ (nodejs.org) and reopen J.A.R.V.I.S."})
    return rows


def missing_features() -> list[dict]:
    """Only the rows that are not ready (what a Settings card would show)."""
    return [row for row in feature_status() if not row.get("ready")]


def install_feature(feature: str, log: Callable | None = None) -> tuple[bool, str]:
    """Install the packages one feature needs — on demand, never at startup.

    Returns ``(ok, sentence)``; the sentence is what the tool or the Settings
    card repeats to the user, so it never claims an install that did not stick.
    """
    name = str(feature or "").strip().lower()
    wanted = [row for row in _FEATURE_EXTRAS if row[0] == name]
    if not wanted:
        known = ", ".join(sorted({row[0] for row in _FEATURE_EXTRAS}))
        return (False, f"'{feature}' has no installable packages. Known features: {known}.")
    missing = [row for row in wanted if not _available(row[1])]
    if not missing:
        return (True, f"{name} is already installed.")
    for _feature, _module, package, _purpose in missing:
        if not _pip(package, log):
            return (False, f"Could not install {package} — see the log for pip's own message.")
    still = [row[2] for row in wanted if not _available(row[1])]
    if still:
        return (False, "Installed, but still not importable: " + ", ".join(still) + ".")
    return (True, f"{name} is ready ({len(missing)} package(s) added).")


# Playwright needs a browser binary as well as the package. One extra command,
# run only when the package had to be installed or no browser is cached.
_PLAYWRIGHT_BROWSERS = [Path.home() / "AppData" / "Local" / "ms-playwright",
                        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"]


def _playwright_browser_ready() -> bool:
    for folder in _PLAYWRIGHT_BROWSERS:
        try:
            if folder.is_dir() and any(folder.glob("chromium*")):
                return True
        except OSError:
            continue
    return False


def _install_playwright_browser(log: Callable | None = None) -> bool:
    """`playwright install chromium` — needed once per machine."""
    if log:
        log("SYS: playwright install chromium …")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
        )
    except Exception as exc:
        if log:
            log(f"ERR: playwright browser install failed — {exc}")
        return False
    if result.returncode != 0 and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: playwright browser install failed — {stderr[:140]}")
    return result.returncode == 0

# STT engine packages
_STT: dict[str, list[tuple[str, str]]] = {
    "whisper": [("faster_whisper", "faster-whisper")],
    "vosk":    [("vosk",           "vosk")],
}

# TTS engine packages
_TTS: dict[str, list[tuple[str, str]]] = {
    "edgetts":    [("edge_tts", "edge-tts")],
    # kokoro>=0.9 dropped AlbertModel/AutoModel from transformers — version pin is critical
    "kokoro":     [("kokoro",   "kokoro>=0.9"), ("soundfile", "soundfile")],
    "elevenlabs": [],   # uses only requests, already in core
}


# ── Helpers ───────────────────────────────────────────────────────────────

def _available(module: str) -> bool:
    """Return True if the module can be imported (no actual import)."""
    return importlib.util.find_spec(module) is not None


def _pip(package: str, log: Callable | None = None) -> bool:
    if log:
        log(f"SYS: pip install {package} …")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", package,
            "--quiet", "--disable-pip-version-check",
        ],
        capture_output=True,
    )
    ok = result.returncode == 0
    if not ok and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: {package} install failed — {stderr[:140]}")
    return ok


# ── Public API ────────────────────────────────────────────────────────────

def install_for_config(config: dict, log: Callable | None = None) -> None:
    """
    Install all missing packages required by *config*.

    Blocking — always call from a background thread.
    Progress is reported via the optional *log* callback (receives a str).
    """
    stt = config.get("stt_engine", "whisper").lower()
    tts = config.get("tts_engine", "edgetts").lower()

    needed: list[tuple[str, str]] = list(_CORE)
    needed += _STT.get(stt, [])
    needed += _TTS.get(tts, [])
    needed += _CONTROL
    if platform.system() == "Windows":
        needed += _WINDOWS

    # Deduplicate (preserve order, key = pip name)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for mod, pkg in needed:
        if pkg not in seen:
            seen.add(pkg)
            unique.append((mod, pkg))

    missing = [(mod, pkg) for mod, pkg in unique if not _available(mod)]

    if not missing:
        if log:
            log("SYS: All dependencies already installed ✓")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Installing {len(missing)} package(s): {pkg_names}")

    for _mod, pkg in missing:
        _pip(pkg, log)

    # The Playwright package is useless without a browser binary.
    if any(pkg.startswith("playwright") for _mod, pkg in missing) \
            or not _playwright_browser_ready():
        _install_playwright_browser(log)

    if log:
        log("SYS: All dependencies ready ✓")


# ── Windows Auto Start (TASK 8) ───────────────────────────────────────────
# The Settings ▸ "Auto-start J.A.R.V.I.S with Windows" toggle only wrote a
# config boolean and never registered anything with the OS, so JARVIS never
# launched at sign-in. These functions implement the real registration using
# the per-user HKCU Run key (no admin rights, single named value = no
# duplicates). The entry point is the SAME console-less launcher the project
# already ships — ``pythonw.exe main.pyw`` from the project root — so no new
# startup mechanism is invented and no visible console window flashes.

_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE = "J.A.R.V.I.S"


def _project_root() -> Path:
    """app/installer.py lives in <root>/app, so the root is one level up."""
    return Path(__file__).resolve().parent.parent


def _console_less_interpreter() -> str:
    """Return pythonw.exe beside the running python.exe (same environment).

    Deriving from ``sys.executable`` keeps the registration pointed at the
    interpreter that is actually running now — a venv or a non-PATH install
    both resolve correctly — instead of a bare ``pythonw.exe`` that relies on
    PATH. Falls back to the current executable if no GUI interpreter is found.
    """
    exe = sys.executable or ""
    name = os.path.basename(exe).lower()
    if name.startswith("python") and name.endswith(".exe") and "pythonw" not in name:
        candidate = os.path.join(os.path.dirname(exe), name.replace("python", "pythonw", 1))
        if os.path.isfile(candidate):
            return candidate
    return exe


def autostart_command() -> str:
    """The ``HKCU\\...\\Run`` value data: quoted absolute paths (spaces-safe)."""
    root = _project_root()
    entry = root / "main.pyw"
    return f'"{_console_less_interpreter()}" "{entry}"'


def enable_autostart() -> tuple[bool, str]:
    """Register JARVIS for Windows sign-in. Idempotent — never duplicates."""
    if platform.system() != "Windows":
        return (False, "Auto Start registration is only supported on Windows.")
    try:
        import winreg
    except Exception as exc:  # pragma: no cover - non-Windows only
        return (False, f"Windows registry unavailable: {exc}")
    entry = _project_root() / "main.pyw"
    if not entry.is_file():
        return (False, f"Startup entry point is missing: {entry}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _AUTOSTART_VALUE, 0, winreg.REG_SZ, autostart_command())
        return (True, "Registered J.A.R.V.I.S for Windows startup.")
    except OSError as exc:
        return (False, f"Startup registration failed: {exc}")


def disable_autostart() -> tuple[bool, str]:
    """Remove JARVIS from Windows sign-in startup (no-op if already absent)."""
    if platform.system() != "Windows":
        return (False, "Auto Start registration is only supported on Windows.")
    try:
        import winreg
    except Exception as exc:  # pragma: no cover - non-Windows only
        return (False, f"Windows registry unavailable: {exc}")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, _AUTOSTART_VALUE)
            except FileNotFoundError:
                pass  # not registered — already OFF
        return (True, "Removed J.A.R.V.I.S from Windows startup.")
    except OSError as exc:
        return (False, f"Startup unregistration failed: {exc}")


def is_autostart_enabled() -> bool:
    """True only when the real OS-level registration exists."""
    if platform.system() != "Windows":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, _AUTOSTART_VALUE)
        return bool(str(value or "").strip())
    except OSError:
        return False
