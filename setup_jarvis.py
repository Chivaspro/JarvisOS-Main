#!/usr/bin/env python3
"""J.A.R.V.I.S. Setup & Control Center — one file to go from a bare machine to
a running assistant.

What it does
------------
    [1] Environment check     Python, pip, Node, git, FFmpeg — with versions
    [2] Install dependencies  requirements.txt, aimed at the interpreter JARVIS
                              will actually run (delegates to start_jarvis.py,
                              so the "wrong Python" bug can never come back)
    [3] Node runtime          npm ci for the Agent TARS browser/PC executor
    [4] Playwright browser    the one binary `playwright` needs per machine
    [5] Configure API keys    paste Gemini + the rest; written to .env and
                              config/api_keys.json (existing values preserved).
                              Paste-friendly, and every value is verified live
                              the moment it is typed. Type a one-word command at
                              the prompt: '-'=clear, 'test'=verify,
                              'open'=authorize in a browser, 'help'=where to get
                              it, 'env'=import from .env.
    [6] Test connections      check every stored credential against its provider
                              with one cheap read-only request (stdlib urllib)
    [X] Fix Gemini            repair the missing google-genai package into the
                              interpreter JARVIS runs with, recover the stored
                              key, and verify it with a live probe (the same
                              repair runs automatically before [L] Launch)
    [7] Install ALL missing   requirements.txt + every optional feature package
                              + the Node runtime + the Playwright browser
    [8] Optimize              rotate logs, expire cache, drop regenerable
                              bytecode — via core/maintenance.py
    [S] Compress / Shrink     reclaim disk space: clean build artifacts, pytest cache,
                              bytecode, trim logs, and compact SQLite databases
    [I] Inspect & fix crash   diagnose what caused run_jarvis to crash, repair stale
                              locks, verify modules, and optimize startup
    [9] Diagnose & repair     report providers and engines, then offer to
                              install everything that is missing
    [O] Authorize providers   open each provider's sign-up / key page in a browser
    [C] Convert to .exe       build a Jarvis OS executable that carries its
                              saved data (config, data, memory, assets) — the
                              project folder itself is never moved or deleted
    [R] Reset data            back up (config/backups/reset_*), then clear the
                              keys, memory, conversations, caches, logs and
                              temp files for a genuine fresh start
    [L] Launch J.A.R.V.I.S.   hands over to start_jarvis.py

The Node runtime install walks a repair ladder (npm ci -> npm install ->
legacy-peer-deps -> forced ci) and clears a partial node_modules and a corrupt
npm cache between attempts, so the "npm reported an error" dead end now ends
with a working Agent TARS runtime or an honest manual fix.

The wizard renders in the J.A.R.V.I.S. HUD colours (ui_theme.py's "hud" theme):
arc-reactor cyan, Stark repulsor-gold, the same three status colours; 24-bit on
a real terminal, plain text when piped or ``NO_COLOR`` is set.

Every answered menu item clears the screen first, so the previous output never
piles up on the next one (set ``NO_CLEAR=1`` to keep the scrollback).

Why it is STANDALONE (stdlib only)
----------------------------------
Like ``start_jarvis.py``, this must run on a fresh interpreter that has *no*
third-party packages, because installing them is its job. So it imports only
the standard library and shells out to pip / npm through the project's own
proven helpers. ``tests/unit/test_setup_wizard.py`` locks that invariant in.

Non-interactive use
-------------------
    python setup_jarvis.py --check          # report only
    python setup_jarvis.py --install        # Python dependencies
    python setup_jarvis.py --node           # Node dependencies (Agent TARS)
    python setup_jarvis.py --browser        # Playwright chromium
    python setup_jarvis.py --keys --yes     # write keys from .env, no prompts
    python setup_jarvis.py --test          # test stored keys live
    python setup_jarvis.py --fix-gemini    # repair Gemini (google-genai + key) and verify
    python setup_jarvis.py --install-missing  # install everything still absent
    python setup_jarvis.py --optimize       # housekeeping
    python setup_jarvis.py --shrink         # reduce folder size (builds, cache, DBs)
    python setup_jarvis.py --inspect-crash  # diagnose run_jarvis crashes & auto-fix
    python setup_jarvis.py --diagnose       # report providers and engines
    python setup_jarvis.py --all --yes      # everything, no prompts
    python setup_jarvis.py --launch         # start J.A.R.V.I.S.

Run it with the interpreter you intend to use::

    python setup_jarvis.py
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# Windows consoles default to a legacy codepage where box-drawing characters
# print as mojibake. Same guard the rest of the project uses — never fatal.
for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
START = ROOT / "start_jarvis.py"
ENTRY = ROOT / "main.py"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_FILE = ROOT / "config" / "api_keys.json"
PACKAGE_JSON = ROOT / "package.json"
VENV = ROOT / ".venv"  # local virtual environment — delete to wipe all packages

_IS_WIN = os.name == "nt"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WIN else {}

MIN_PYTHON = (3, 11)
NODE_MIN_MAJOR = 18

# ── ANSI colours — the J.A.R.V.I.S. HUD palette ──────────────────────────────
# The wizard speaks the same colour language as the app itself (ui_theme.py's
# "hud" theme): an arc-reactor ice-cyan working colour, a single Stark
# repulsor-gold signature accent, and the same three status colours. 24-bit
# codes render on a real terminal and degrade to plain text when NO_COLOR is
# set or output is piped, so a logged run stays clean.
_COLOR = sys.stdout.isatty()
if os.environ.get("NO_COLOR"):
    _COLOR = False

HUD_ACCENT = "#00d4ff"   # arc-reactor cyan — the working colour
HUD_BRIGHT = "#8ceaff"   # lifted cyan — headings
HUD_MID = "#a6e9ff"      # secondary text
HUD_FAINT = "#547f92"    # hint / disabled — the small grey text
HUD_GOLD = "#f4b24a"     # Stark repulsor-gold — the live/active accent
HUD_GREEN = "#5df0b6"    # ready
HUD_AMBER = "#ffc773"    # attention
HUD_RED = "#ff6f8b"      # failed


def _hex_rgb(color: str) -> tuple[int, int, int]:
    s = str(color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except Exception:
        return 0, 0, 0


def _paint(color: str, text: str, heavy: bool = False) -> str:
    """Wrap *text* in a HUD colour. Plain text when colour is switched off."""
    if not _COLOR:
        return text
    r, g, b = _hex_rgb(color)
    lead = "\033[1m" if heavy else ""
    return f"{lead}\033[38;2;{r};{g};{b}m{text}\033[0m"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def cyan(t: str) -> str:   return _paint(HUD_ACCENT, t)
def accent(t: str) -> str: return _paint(HUD_ACCENT, t)
def bright(t: str) -> str: return _paint(HUD_BRIGHT, t, heavy=True)
def gold(t: str) -> str:   return _paint(HUD_GOLD, t)
def mid(t: str) -> str:    return _paint(HUD_MID, t)
def green(t: str) -> str:  return _paint(HUD_GREEN, t)
def yellow(t: str) -> str: return _paint(HUD_AMBER, t)
def red(t: str) -> str:    return _paint(HUD_RED, t)
def faint(t: str) -> str:  return _paint(HUD_FAINT, t)
def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _paint(HUD_FAINT, t)



# ── launcher look (same palette / glyphs as launch_ui.py's ConsoleUI) ────────
L_CYAN = (0, 229, 255)
L_DEEP = (0, 70, 130)
L_WHITE = (200, 250, 255)
L_GREEN = (57, 255, 182)
L_FAINT = (70, 120, 140)
L_TRACK = (18, 52, 70)
_GLYPHS = "01#$%&@?<>/\\|[]{}=+*ABCDEF0123456789"
_SPIN = "◢◣◤◥"
_BANNER = (
    "     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗",
    "     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝",
    "     ██║███████║██████╔╝██║   ██║██║███████╗",
    "██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║",
    "╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║",
    " ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝",
)


def _lerp(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _fg(rgb) -> str:
    return "\x1b[38;2;%d;%d;%dm" % tuple(rgb) if _COLOR else ""


def _rst() -> str:
    return "\x1b[0m" if _COLOR else ""


def _enable_vt() -> None:
    """Switch on ANSI processing for the Windows console (the launcher does too)."""
    if not (_IS_WIN and _COLOR):
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_enable_vt()


def decode(text: str, age: float, duration: float = 0.45) -> str:
    """'Hacking' reveal: unrevealed characters shimmer as random glyphs."""
    import random
    text = str(text)
    if age >= duration or not _COLOR:
        return text
    shown = int(len(text) * max(0.0, age) / duration)
    return "".join(ch if (i < shown or ch == " ") else random.choice(_GLYPHS)
                   for i, ch in enumerate(text))


# ── screen & style helpers ───────────────────────────────────────────────────

def _vlen(text: str) -> int:
    """Visible width of *text* — ANSI colour codes are not letters."""
    width = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\033" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and text[j] != "m":
                j += 1
            i = j + 1
            continue
        width += 1
        i += 1
    return width


_BOX_W = 76


def clear_screen() -> None:
    """Wipe the console so the next answer replaces the last.

    Only touches a real terminal, so a piped or logged run keeps every line.
    Set ``NO_CLEAR=1`` in the environment to keep the scrollback interactively.
    """
    if os.environ.get("NO_CLEAR"):
        return
    if not (sys.stdout and sys.stdout.isatty()):
        return
    try:
        os.system("cls" if _IS_WIN else "clear")
    except Exception:
        try:
            print("\033[2J\033[H", end="")
        except Exception:
            pass


def pause(message: str = "  Press Enter to continue…") -> None:
    """Block until the user acknowledges — never hangs a piped run."""
    if not (sys.stdin and sys.stdin.isatty()):
        return
    try:
        input(dim(message))
    except (EOFError, KeyboardInterrupt):
        print()


_PROGRESS_RE = re.compile(r"(?<!\d)(\d{1,3}(?:\.\d+)?)%")


def _progress_bar(percent: float, width: int = 28) -> str:
    percent = max(0.0, min(100.0, float(percent)))
    filled = int(round(width * percent / 100.0))
    return "█" * filled + "░" * (width - filled)


def _terminal_width(default: int = 96) -> int:
    try:
        return max(60, int(shutil.get_terminal_size((default, 20)).columns) - 1)
    except Exception:
        return default


_PROG_STATE = {"label": None, "since": 0.0}


def show_progress(label: str, percent: float, detail: str = "") -> None:
    """One live line in the launcher's style: spinner, decoding label, gradient bar."""
    pct = max(0.0, min(100.0, float(percent)))
    width = _terminal_width()
    now = time.monotonic()
    if _PROG_STATE["label"] != label:
        _PROG_STATE["label"], _PROG_STATE["since"] = label, now
    age = now - _PROG_STATE["since"]

    detail_plain = re.sub(r"\x1b\[[0-9;]*m", "", str(detail or ""))
    detail_plain = " ".join(detail_plain.replace("\r", " ").replace("\n", " ").split())
    bar_w = 24
    prefix = f"  ◆ {label[:26]:<26} [{_progress_bar(pct, bar_w)}] {pct:5.1f}%"
    detail_plain = detail_plain[:max(0, width - len(prefix) - 2)]

    if _COLOR and sys.stdout.isatty():
        spin = _SPIN[int(now * 12) % 4]
        filled = int(round(bar_w * pct / 100.0))
        cells = []
        for i in range(bar_w):
            if i < filled:
                head = (i == filled - 1) and pct < 100
                cells.append(_fg(L_WHITE if head else _lerp(L_DEEP, L_CYAN, i / max(1, bar_w - 1))) + "█")
            else:
                cells.append(_fg(L_TRACK) + "░")
        line = (" " + _fg(L_CYAN) + spin + " " +
                _fg(L_WHITE) + f"{decode(label[:26].upper(), age):<26}" +
                " " + _fg(L_FAINT) + "[" + "".join(cells) + _fg(L_FAINT) + "] " + _rst() +
                _fg(L_WHITE) + f"\x1b[1m{pct:5.1f}%" + _rst() +
                (("  " + _fg(L_FAINT) + detail_plain + _rst()) if detail_plain else ""))
        sys.stdout.write("\r\x1b[2K" + line)
    else:
        plain = prefix + (f"  {detail_plain}" if detail_plain else "")
        sys.stdout.write("\r" + plain.ljust(width)[:width])
    sys.stdout.flush()


def finish_progress(success: bool = True) -> None:
    """Remove the live progress bar before printing the final result."""
    if sys.stdout.isatty():
        sys.stdout.write("\r\033[2K\r")
    else:
        sys.stdout.write("\r" + (" " * _terminal_width()) + "\r")
    sys.stdout.flush()


def _run_with_progress(
    cmd: list[str], *, label: str, timeout: float | None = None,
    cwd: Path | None = None, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a command while showing real percentages when the child emits them."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        process = subprocess.Popen(
            cmd, cwd=str(cwd or ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=False, bufsize=0, env=merged_env,
            **_NO_WINDOW,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))

    q: queue.Queue[str] = queue.Queue()
    chunks: list[str] = []

    def reader() -> None:
        try:
            assert process.stdout is not None
            while True:
                data = process.stdout.read(1024)
                if not data:
                    break
                q.put(data.decode("utf-8", "replace"))
        except Exception as exc:
            q.put(f"[reader error] {exc}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    pct = 0.0
    detail = "Starting…"
    started = time.monotonic()

    while process.poll() is None or not q.empty():
        try:
            chunk = q.get(timeout=0.12)
        except queue.Empty:
            show_progress(label, pct, detail)
            if timeout and time.monotonic() - started > timeout:
                process.kill()
                break
            continue
        chunks.append(chunk)
        vals = _PROGRESS_RE.findall(chunk)
        if vals:
            try:
                pct = max(pct, float(vals[-1]))
            except ValueError:
                pass
        clean = " ".join(x.strip() for x in re.split(r"[\r\n]+", chunk) if x.strip())
        if clean:
            detail = clean
        show_progress(label, pct, detail)
        if timeout and time.monotonic() - started > timeout:
            process.kill()
            break

    thread.join(timeout=1.0)
    rc = process.poll()
    if rc is None:
        rc = 1
    output = "".join(chunks)
    if rc == 0:
        pct = 100.0
        show_progress(label, pct, "Finished")
    finish_progress(rc == 0)
    return subprocess.CompletedProcess(cmd, rc, output, "")


def _read_menu_key() -> str:
    """Read a single key: ↑/↓ or W/S, Enter, or the existing shortcuts."""
    if _IS_WIN:
        import msvcrt
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {"H": "up", "P": "down"}.get(ch2, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch.lower() == "w":
            return "up"
        if ch.lower() == "s":
            return "down"
        return ch.lower()
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                seq = sys.stdin.read(2)
                return {"[A": "up", "[B": "down"}.get(seq, "")
            if ch in ("\r", "\n"):
                return "enter"
            if ch.lower() == "w":
                return "up"
            if ch.lower() == "s":
                return "down"
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return ask("  select > ").strip().lower()


def rule() -> None:
    print(faint("  " + "─" * _BOX_W))


def section(title: str) -> None:
    """Launcher-style stage header:  ❯ TITLE  with a faint rule beneath."""
    print()
    if _COLOR:
        print(" " + _fg(L_GREEN) + "❯ " + _fg(L_WHITE) + "\x1b[1m" + title.upper() + _rst())
        print(" " + _fg(L_FAINT) + "  " + "─" * max(10, min(_BOX_W, len(title) + 6)) + _rst())
    else:
        print("> " + title.upper())


def banner() -> None:
    """The launch screen's gradient ASCII logo + spaced-out subtitle."""
    print()
    if not _COLOR:
        print("J.A.R.V.I.S :: setup / control center")
        print(f"  {ROOT}")
        print()
        return
    for i, row in enumerate(_BANNER):
        t = i / max(1, len(_BANNER) - 1)
        print(" " + _fg(_lerp(L_CYAN, L_DEEP, t * 0.85)) + row + _rst())
    print(" " + _fg(L_FAINT) + "  S Y S T E M   S E T U P   //   C O N T R O L   C E N T E R" + _rst())
    print(" " + _fg(L_FAINT) + f"  {ROOT}" + _rst())
    print()


def ok(msg: str) -> None:   print("  " + green("[ ok ] ") + msg)
def warn(msg: str) -> None: print("  " + yellow("[warn] ") + msg)
def bad(msg: str) -> None:  print("  " + red("[ X  ] ") + msg)
def info(msg: str) -> None: print("  " + accent("[ .. ] ") + msg)


def _run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None,
         capture: bool = True) -> subprocess.CompletedProcess | None:
    """Run a command with a suppressed console window. Never raises."""
    try:
        return subprocess.run(
            cmd, cwd=str(cwd or ROOT),
            capture_output=True if capture else False,
            text=True, timeout=timeout, **_NO_WINDOW,
        )
    except Exception:
        return None


def _version_of(cmd: str, *args: str) -> str:
    r = _run([cmd, *args], timeout=10)
    if not r or r.returncode != 0:
        return ""
    text = (r.stdout or r.stderr or "").strip().splitlines()
    return text[0].strip() if text else ""


def ask(prompt: str, default: str = "") -> str:
    """input() that degrades to the default when stdin is not interactive."""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        value = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value if value != "" else default


def _confirm(prompt: str, default: bool = False) -> bool:
    """Yes/no question. A non-interactive run takes *default* and never blocks."""
    if not (sys.stdin and sys.stdin.isatty()):
        return default
    hint = " [Y/n] " if default else " [y/N] "
    answer = ask("  " + prompt + hint, "y" if default else "n").strip().lower()
    return answer in ("y", "yes")


# ── 1. environment check ─────────────────────────────────────────────────────

def check_python() -> bool:
    v = sys.version_info
    good = (v.major, v.minor) >= MIN_PYTHON
    label = f"Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})"
    (ok if good else bad)(label + ("" if good else
         f"  — need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"))
    if not good:
        warn(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required. "
             "Use menu option [p] or run: winget install Python.Python.3.13")
    return good


def check_pip() -> bool:
    r = _run([sys.executable, "-m", "pip", "--version"], timeout=30)
    good = bool(r and r.returncode == 0)
    (ok if good else bad)("pip " + (r.stdout.strip().split()[1] if good else "not available"))
    return good


def install_python_runtime() -> bool:
    """Install the latest Python 3 via winget (Windows only).

    After winget finishes, the user must reopen the terminal so the new
    ``python`` lands on PATH — the running process cannot update its own PATH.
    Returns ``True`` if winget reported success.
    """
    section("Install Python runtime")
    if not _IS_WIN:
        bad("Automatic Python install is only supported on Windows via winget.")
        info("Visit https://python.org/downloads and install Python 3.13+, then re-run.")
        return False
    if not shutil.which("winget"):
        bad("winget is not available on this machine.")
        info("Install Python 3.13+ manually from https://python.org/downloads")
        return False
    info("Installing latest Python 3.13 via winget — this may take a few minutes…")
    r = _run_with_progress(
        [
            "winget", "install", "--id", "Python.Python.3.13",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        label="Installing Python 3.13", timeout=1800,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("Python 3.13 installed successfully.")
        warn("IMPORTANT: Close and reopen this terminal window so the new "
             "python.exe lands on PATH, then run setup_jarvis.py again.")
    else:
        bad("winget could not install Python automatically.")
        info("Install manually from https://python.org/downloads  "
             "(tick 'Add Python to PATH' in the installer).")
    return good


def check_node() -> tuple[bool, str]:
    exe = shutil.which("node")
    if not exe:
        bad("Node.js not found — needed only for the Agent TARS automation engine")
        return False, "node is not on PATH"
    ver = _version_of("node", "--version")
    try:
        major = int(ver.lstrip("v").split(".", 1)[0])
    except Exception:
        major = 0
    good = major >= NODE_MIN_MAJOR
    (ok if good else warn)(f"Node.js {ver or '?'}" +
        ("" if good else f"  — Agent TARS wants v{NODE_MIN_MAJOR}+"))
    return good, ver


def check_tool(name: str, args: tuple[str, ...], why: str, required: bool = False) -> bool:
    exe = shutil.which(name)
    if not exe:
        (bad if required else warn)(f"{name} not found — {why}")
        return False
    ver = _version_of(name, *args)
    ok(f"{name} {ver or ''}".strip() + dim(f"  ({why})"))
    return True


def _pad(text: str, width: int) -> str:
    """Pad to a visible width (ANSI codes do not count)."""
    return text + " " * max(0, width - _vlen(text))


def _env_status(value: bool) -> tuple[str, str]:
    return (green("READY"), "ok") if value else (yellow("MISSING"), "warn")


_ENV_TAG = {"ok": ("ONLINE", L_GREEN), "warn": ("MISSING", (255, 196, 64)),
            "bad": ("FAILED", (255, 84, 96)), "opt": ("OPTIONAL", L_FAINT)}


def _env_dot(state: str) -> str:
    if not _COLOR:
        return {"ok": "+", "warn": "!", "bad": "x", "opt": "-"}.get(state, "-")
    return _fg(_ENV_TAG.get(state, _ENV_TAG["opt"])[1]) + "●" + _rst()


def _env_card_row(label: str, value: str, state: str) -> str:
    """│ ●  PYTHON        3.12.1                      ONLINE │"""
    inner = _BOX_W - 2
    tag, rgb = _ENV_TAG.get(state, _ENV_TAG["opt"])
    tag_txt = (_fg(rgb) + "\x1b[1m" + tag + _rst()) if _COLOR else tag
    label_txt = (_fg(L_WHITE) + label.upper() + _rst()) if _COLOR else label.upper()
    value_txt = (_fg(L_FAINT) + value + _rst()) if _COLOR else value
    left = f" {_env_dot(state)}  {_pad(label_txt, 12)}{_pad(value_txt, 26)}"
    body = _pad(left, inner - 10) + _pad(tag_txt, 9)
    edge = _fg(L_CYAN) + "│" + _rst() if _COLOR else "|"
    return "  " + edge + _pad(body, inner) + edge


def _gauge(pct: float, width: int = 30) -> str:
    """Launcher-style gradient bar."""
    filled = int(round(width * max(0.0, min(100.0, pct)) / 100.0))
    if not _COLOR:
        return "[" + "#" * filled + "." * (width - filled) + "]"
    cells = []
    for i in range(width):
        if i < filled:
            head = i == filled - 1 and pct < 100
            cells.append(_fg(L_WHITE if head else _lerp(L_DEEP, L_CYAN, i / max(1, width - 1))) + "█")
        else:
            cells.append(_fg(L_TRACK) + "░")
    return _fg(L_FAINT) + "[" + "".join(cells) + _fg(L_FAINT) + "]" + _rst()


def _panel_top(title: str, right: str = "") -> str:
    inner = _BOX_W - 2
    t = (_fg(L_GREEN) + "\x1b[1m" + title + _rst()) if _COLOR else title
    r = (_fg(L_FAINT) + right + _rst()) if (_COLOR and right) else right
    fill = max(1, inner - 4 - _vlen(t) - (_vlen(r) + 2 if right else 0))
    edge = lambda s: (_fg(L_CYAN) + s + _rst()) if _COLOR else s
    return ("  " + edge("╭─ ") + t + edge(" " + "─" * fill) +
            ((" " + r + " ") if right else "") + edge("╮" if not right else "─╮"))


def _panel_bottom() -> str:
    s = "╰" + "─" * (_BOX_W - 2) + "╯"
    return "  " + ((_fg(L_CYAN) + s + _rst()) if _COLOR else s)


def report_env() -> dict:
    """Render a small status card instead of a large diagnostic dump."""
    py = sys.version_info
    python_ok = (py.major, py.minor) >= MIN_PYTHON

    pip_ok = bool(_version_of(sys.executable, "-m", "pip", "--version"))

    node_exe = shutil.which("node")
    node_version = _version_of("node", "--version") if node_exe else ""
    try:
        node_major = int(node_version.lstrip("v").split(".", 1)[0])
    except Exception:
        node_major = 0
    node_ok = bool(node_exe and node_major >= NODE_MIN_MAJOR)

    git_ok = bool(shutil.which("git"))
    ffmpeg_ok = bool(shutil.which("ffmpeg"))
    tars_ok = _node_modules_ready()
    venv_ok = Path(_venv_python()).is_file()

    requirements_ok = REQUIREMENTS.is_file()
    env_ok = ENV_FILE.is_file()
    config_ok = CONFIG_FILE.is_file()

    res = {
        "python": python_ok,
        "pip": pip_ok,
        "node": node_ok,
        "git": git_ok,
        "ffmpeg": ffmpeg_ok,
        "node_modules": tars_ok,
        "venv": venv_ok,
        "requirements": requirements_ok,
        "env_file": env_ok,
        "config": config_ok,
    }

    py_value = (
        f"{py.major}.{py.minor}.{py.micro}"
        if python_ok else f"{py.major}.{py.minor}  / need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    )
    node_value = node_version if node_ok else "Not installed"
    pip_value = "Installed" if pip_ok else "Not installed"
    venv_value = f".venv/ ({_venv_python()})" if venv_ok else "Not created (run [2])"

    checks = [
        ("Python", py_value, "ok" if python_ok else "bad"),
        ("pip", pip_value, "ok" if pip_ok else "bad"),
        (".venv", venv_value, "ok" if venv_ok else "warn"),
        ("Node.js", node_value, "ok" if node_ok else "warn"),
        ("Git", "Installed" if git_ok else "Not installed",
         "ok" if git_ok else "warn"),
        ("FFmpeg", "Installed" if ffmpeg_ok else "Optional",
         "ok" if ffmpeg_ok else "warn"),
        ("Agent TARS", "Ready" if tars_ok else "Not installed",
         "ok" if tars_ok else "warn"),
    ]

    # Three-state rows: FFmpeg is genuinely optional, so it is not alarming.
    checks[5] = ("FFmpeg", "Installed" if ffmpeg_ok else "Not installed",
                 "ok" if ffmpeg_ok else "opt")
    scored = [c for c in checks if c[2] != "opt"]
    readiness = 100.0 * sum(1 for c in scored if c[2] == "ok") / max(1, len(scored))

    print(_panel_top("ENVIRONMENT", f"{sum(1 for c in checks if c[2] == 'ok')}/{len(checks)} ONLINE"))
    for label, value, state in checks:
        print(_env_card_row(label, value, state))
    edge = _fg(L_CYAN) + "│" + _rst() if _COLOR else "|"
    gauge = " " + (_fg(L_FAINT) + "READINESS " + _rst() if _COLOR else "READINESS ") + _gauge(readiness, 28) + \
        " " + ((_fg(L_WHITE) + f"\x1b[1m{readiness:3.0f}%" + _rst()) if _COLOR else f"{readiness:3.0f}%")
    print("  " + edge + _pad(gauge, _BOX_W - 2) + edge)
    print(_panel_bottom())

    # Keep these files known to the caller without expanding the main card.
    if not requirements_ok:
        print("  " + faint("requirements.txt  ·  missing"))
    if not venv_ok:
        print("  " + faint(".venv/  ·  not created — run option [2] to create and populate it"))
    if not env_ok or not config_ok:
        print("  " + faint("configuration  ·  not initialized"))

    print()
    return res


# ── 2. Python dependencies — isolated local virtual environment (.venv) ────────

def _venv_python() -> str:
    """Absolute path to the venv's Python executable.

    On Windows the layout is ``.venv/Scripts/python.exe``; on POSIX it is
    ``.venv/bin/python``.  The caller can use this path even before the venv
    exists — it simply won't be a real file yet.
    """
    if _IS_WIN:
        return str(VENV / "Scripts" / "python.exe")
    return str(VENV / "bin" / "python")


def ensure_venv() -> bool:
    """Create the project-local virtual environment if it does not exist yet.

    Uses the same interpreter that is running this script so the venv's Python
    version always matches.  Returns ``True`` when the venv is (or becomes)
    ready, ``False`` on failure.
    """
    py = _venv_python()
    if Path(py).is_file():
        ok(f"Virtual environment already exists  ({VENV.name}/)")
        return True
    info(f"Creating virtual environment at  {VENV.name}/  …")
    r = _run([sys.executable, "-m", "venv", str(VENV)], timeout=120)
    if r and r.returncode == 0 and Path(py).is_file():
        ok(f"Virtual environment created  ({VENV.name}/)")
        return True
    bad(f"Could not create virtual environment — "
        f"check that the `venv` stdlib module is available.")
    return False


def install_python_deps() -> bool:
    """Install ``requirements.txt`` into the project-local ``.venv``.

    All three steps run against the venv's own Python/pip so nothing leaks
    into the system or user interpreter.  To start over, delete ``.venv/``.
    """
    section("Python dependencies  (isolated virtual environment)")

    if not REQUIREMENTS.is_file():
        bad(f"requirements.txt is missing — cannot install ({REQUIREMENTS})")
        return False

    # Step 1 — make sure the venv exists.
    if not ensure_venv():
        return False

    vpy = _venv_python()
    info(f"Installing packages from requirements.txt into  {VENV.name}/  …")

    # Step 2 — upgrade pip inside the venv first (silent, best-effort).
    _run([vpy, "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
         timeout=120)

    # Step 3 — install the project requirements.
    r = _run_with_progress(
        [
            vpy, "-m", "pip", "install",
            "-r", str(REQUIREMENTS),
            "--disable-pip-version-check",
        ],
        label="Downloading Python packages", timeout=3600,
        env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok(f"Python dependencies installed into  {VENV.name}/")
        info(f"To remove all packages, delete the  {VENV.name}/  folder.")
    else:
        bad("Dependency install reported a problem — "
            "re-run [2] or check the output above.")
    return good


# ── 3. Node runtime (Agent TARS) ─────────────────────────────────────────────

NPM_TIMEOUT = 2400


def _npm_exe() -> str:
    return shutil.which("npm") or "npm"


def _node_modules_ready() -> bool:
    """A real, non-empty ``node_modules`` is what 'installed' actually means."""
    nm = ROOT / "node_modules"
    try:
        return nm.is_dir() and any(nm.iterdir())
    except OSError:
        return False


def _npm_tail(result, limit: int = 14) -> None:
    """Print the last meaningful npm lines so a failure is readable."""
    text = ""
    if result is not None:
        text = (result.stdout or "") + "\n" + (result.stderr or "")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        print("      " + faint(line))


def _npm_run(args: list[str], timeout: float = NPM_TIMEOUT):
    return _run_with_progress(
        [_npm_exe(), *args], label="Downloading Node packages",
        timeout=timeout, env={
            "NPM_CONFIG_PROGRESS": "true",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_AUDIT": "false",
        },
    )


def install_node_runtime() -> bool:
    """Install Node.js itself (winget on Windows). Best effort, never fatal."""
    section("Node.js runtime")
    if shutil.which("winget"):
        info("Installing Node.js LTS with winget — this may take a minute…")
        r = _run(["winget", "install", "--id", "OpenJS.NodeJS.LTS", "-e",
                  "--accept-package-agreements", "--accept-source-agreements"],
                 timeout=1800, capture=True)
        good = bool(r and r.returncode == 0)
        (ok if good else warn)(
            "Node.js installed — reopen this terminal if `node` is still missing."
            if good else "winget could not install Node.js.")
        return good
    warn("winget is not available — install Node.js 18+ from https://nodejs.org")
    return False


def _repair_npm(has_lock: bool) -> None:
    """Clear the two things that actually break an npm install, then retry."""
    # 1. A partial node_modules left by an interrupted download.
    nm = ROOT / "node_modules"
    if nm.is_dir():
        info("Removing the partial node_modules before retrying…")
        shutil.rmtree(nm, ignore_errors=True)
    # 2. A corrupt npm cache.
    info("Clearing the npm cache (a corrupt cache is the usual cause)…")
    r = _npm_run(["cache", "clean", "--force"], timeout=300)
    (ok if (r and r.returncode == 0) else warn)(
        "npm cache cleared." if (r and r.returncode == 0)
        else "Could not clear the npm cache.")
    if has_lock:
        info("Note: `npm ci` needs package.json and package-lock.json in step; "
             "if they disagree the next attempt falls back to `npm install`.")


def install_node_deps() -> bool:
    """Install the Agent TARS Node runtime, repairing the usual failures.

    The old version ran a single command and gave up on any error. This one
    walks a real ladder — pinned ``npm ci``, then ``npm install``, then a
    legacy-peer-deps install, then a forced ``npm ci`` — and clears the two
    things that actually break npm (a partial node_modules and a corrupt cache)
    between attempts, so a transient failure ends with a working runtime.
    """
    section("Node runtime  (Agent TARS — browser & PC automation)")
    node_ok, _ = check_node()
    if not node_ok:
        warn(f"Node.js {NODE_MIN_MAJOR}+ is required for the Agent TARS engine.")
        if _confirm("Install Node.js now with winget?", default=False):
            install_node_runtime()
            node_ok, _ = check_node()
        if not node_ok:
            warn("Skipping — install Node.js 18+ first "
                 "(https://nodejs.org, or `winget install OpenJS.NodeJS.LTS`)")
            return False
    if not PACKAGE_JSON.is_file():
        bad(f"package.json is missing ({PACKAGE_JSON})")
        return False
    if not shutil.which("npm"):
        bad("npm was not found on PATH (it ships with Node.js) — reinstall Node.js.")
        return False

    has_lock = (ROOT / "package-lock.json").is_file()

    # The ladder: each rung is only tried after the previous one failed.
    ladder: list[tuple[str, list[str]]] = []
    if has_lock:
        ladder.append(("npm ci", ["ci", "--no-audit", "--no-fund"]))
    ladder.append(("npm install", ["install", "--no-audit", "--no-fund"]))
    ladder.append(("npm install --legacy-peer-deps",
                   ["install", "--no-audit", "--no-fund", "--legacy-peer-deps"]))
    if has_lock:
        ladder.append(("npm ci --force", ["ci", "--force", "--no-audit", "--no-fund"]))

    for index, (label, args) in enumerate(ladder, start=1):
        info(f"[{index}/{len(ladder)}] {label} — downloading the pinned Agent TARS CLI…")
        result = _npm_run(args)
        if result and result.returncode == 0 and _node_modules_ready():
            ok("Node runtime installed — Agent TARS is ready.")
            return True
        warn(f"{label} did not finish cleanly.")
        _npm_tail(result)
        if index < len(ladder):
            _repair_npm(has_lock)

    bad("The Node runtime could not be installed automatically.")
    info("Manual fix: open a terminal in this folder and run `npm install`.")
    return False


# ── 4. Playwright browser binary ─────────────────────────────────────────────

def install_playwright_browser() -> bool:
    section("Playwright browser")
    # Prefer the venv interpreter so playwright runs inside the isolated env.
    vpy = _venv_python()
    py = vpy if Path(vpy).is_file() else sys.executable
    r = _run([py, "-m", "playwright", "--version"], timeout=30)
    if not r or r.returncode != 0:
        warn("The `playwright` package is not installed yet — choose option [2] first")
        return False
    info("Downloading Chromium…")
    r = _run_with_progress(
        [py, "-m", "playwright", "install", "chromium"],
        label="Downloading Chromium", timeout=1800,
        env={"PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": "120000"},
    )
    good = bool(r and r.returncode == 0)
    (ok if good else bad)("Chromium ready for browser automation" if good else
                          "Playwright install failed")
    return good


# ── 5. API keys → .env + config/api_keys.json ────────────────────────────────
# (json key, prompt, secret?, where to get it)
CORE_KEYS: list[tuple[str, str, bool, str]] = [
    ("gemini_api_key",    "Gemini API key   (REQUIRED: AI + Live voice)", True,
     "https://aistudio.google.com/app/apikey"),
    ("openai_api_key",    "OpenAI key       (optional: text)", True,
     "https://platform.openai.com/api-keys"),
    ("anthropic_api_key", "Anthropic Claude (optional: text)", True,
     "https://console.anthropic.com/settings/keys"),
    ("groq_api_key",      "Groq key         (optional: fast text)", True,
     "https://console.groq.com/keys"),
    ("openrouter_api_key", "OpenRouter key  (optional: many models)", True,
     "https://openrouter.ai/keys"),
]
VOICE_KEYS: list[tuple[str, str, bool, str]] = [
    ("fish_audio_api_key", "Fish Audio key (optional: cloud voice)", True,
     "https://fish.audio/go-api/"),
    ("elevenlabs_api_key", "ElevenLabs key (optional: cloud voice)", True,
     "https://elevenlabs.io/app/settings/api-keys"),
]
EXTRA_KEYS: list[tuple[str, str, bool, str]] = [
    ("mem0_api_key",        "Mem0 key        (optional: cloud memory)", True,
     "https://app.mem0.ai/dashboard/api-keys"),
    ("qdrant_api_key",      "Qdrant key      (optional: vector knowledge)", True,
     "https://cloud.qdrant.io/"),
    ("qdrant_url",          "Qdrant URL      (default http://localhost:6333)", False, ""),
    ("weather_api_key",     "Weather key     (optional)", True, ""),
    ("spotify_client_id",   "Spotify client id     (optional, for MCP host)", False, ""),
    ("spotify_client_secret", "Spotify client secret (optional)", True, ""),
    ("instagram_username",  "Instagram username    (optional, your account)", False, ""),
    ("instagram_password",  "Instagram password    (optional)", True, ""),
    ("smart_home_username", "Kasa/Tapo username    (optional, local network)", False, ""),
    ("smart_home_password", "Kasa/Tapo password    (optional)", True, ""),
]


def _read_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data: dict) -> bool:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
        return True
    except Exception as exc:
        bad(f"Could not write {CONFIG_FILE}: {exc}")
        return False


def _mask(value: str) -> str:
    v = str(value or "")
    if not v:
        return dim("(not set)")
    if len(v) <= 10:
        return dim("*" * len(v))
    return dim(f"{v[:6]}…{v[-4:]}")


def _ensure_env() -> None:
    """Create .env from .env.example on first run (never overwrites)."""
    if ENV_FILE.exists():
        return
    try:
        if ENV_EXAMPLE.is_file():
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            ok("Created .env from .env.example")
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass


def _read_env() -> dict:
    out: dict[str, str] = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip().upper()] = v.split("#", 1)[0].strip()
    except Exception:
        pass
    return out


def _sync_gemini_env(gemini: str, env_values: dict) -> None:
    """Reflect the Gemini key into .env so shell launches pick it up too."""
    if not gemini or env_values.get("GEMINI_API_KEY") == gemini:
        return
    try:
        text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.is_file() else ""
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("GEMINI_API_KEY="):
                lines[i] = f"GEMINI_API_KEY={gemini}"
                break
        else:
            lines.append(f"GEMINI_API_KEY={gemini}")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _prompt_key(label: str) -> str | None:
    """Read one value with plain ``input()``.

    ``getpass.getpass`` was used before, but on Windows it reads the console
    key-by-key through ``msvcrt`` and silently drops a pasted key — the exact
    "I cannot paste my API key" failure. ``input()`` takes a paste intact, so
    the value lands whole. Returns ``None`` when there is nothing to read.
    """
    try:
        return input(f"    {label} : ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _key_urls() -> dict[str, str]:
    """field -> sign-up / key page, from the one key table."""
    return {k: url for k, _label, _secret, url
            in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS if url}


def _key_label(field: str) -> str:
    for key, label, _secret, _url in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS:
        if key == field:
            return label.split("(")[0].strip()
    return field


def _open_url(url: str) -> bool:
    """Open a URL in the default browser (stdlib ``webbrowser``). Never raises."""
    if not url:
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _help_for(field: str, url: str = "") -> None:
    """Where to get one credential — printed in the HUD's small grey."""
    target = url or _key_urls().get(field, "")
    print("      " + faint(f"{field}  —  {_key_label(field)}"))
    if target:
        print("      " + faint(f"open: {target}"))
    else:
        print("      " + faint("no sign-up link on record — see the provider's docs"))


#: One-word commands the key prompt accepts, so the hint and the parser agree.
KEY_COMMANDS = {
    "clear": "-", "delete": "-", "remove": "-",
    "test": "test", "verify": "test", "check": "test",
    "open": "open", "auth": "open", "authorize": "open", "get": "open", "link": "open",
    "help": "help", "?": "help", "info": "help",
    "env": "env", "import": "env",
    "skip": "keep",
}


def authorize_keys() -> bool:
    """Open each missing provider's page so the user can authorize an account.

    Nothing is written and no key is invented: this only opens the browser to
    the page that issues the credential, one provider at a time and only when
    asked, so a fresh machine can be authorised end to end.
    """
    section("Authorize providers")
    cfg = _read_config()
    opened = 0
    for key, label, _secret, url in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS:
        if not url:
            continue
        name = label.split("(")[0].strip()
        if str(cfg.get(key, "") or "").strip():
            print("  " + green("✔ ") + f"{name} " + faint("— already authorized"))
            continue
        print("  " + accent("→ ") + f"{name} " + faint(url))
        if _confirm(f"Open the {name} page in your browser now?", default=False):
            if _open_url(url):
                ok(f"Opened {url}")
                opened += 1
            else:
                warn(f"Could not open a browser — visit {url}")
    if opened:
        info("Authorize on the page, copy the key, then use [5] Configure API keys.")
    else:
        info("Nothing opened. Add a key with [5] Configure API keys.")
    return True


def configure_keys(groups: str = "core", non_interactive: bool = False) -> bool:
    """Collect API keys and persist them, preserving everything else.

    ``groups``: ``core`` (Gemini + text providers), ``voice``, ``extra`` or ``all``.
    Non-interactive mode seeds every key from the process environment / ``.env``
    so a scripted install can pass ``GEMINI_API_KEY=…`` and be done.
    """
    section("API keys")
    if groups == "all":
        spec = CORE_KEYS + VOICE_KEYS + EXTRA_KEYS
    elif groups == "voice":
        spec = VOICE_KEYS
    elif groups == "extra":
        spec = EXTRA_KEYS
    else:
        spec = CORE_KEYS

    cfg = _read_config()
    _ensure_env()
    env_values = _read_env()

    if non_interactive:
        changed = 0
        for key, _label, _secret, _url in spec:
            val = env_values.get(key.upper(), "") or os.environ.get(key.upper(), "")
            if val:
                cfg[key] = val
                changed += 1
        if changed:
            ok(f"Imported {changed} key(s) from .env / environment")
        else:
            warn("No keys found in .env or the environment (set GEMINI_API_KEY=… "
                 "or run without --yes to type them)")
        _write_config(cfg)
        _sync_gemini_env(str(cfg.get("gemini_api_key", "") or ""), env_values)
        return True

    print(faint("    Paste a value · Enter keeps the current one."))
    print(faint("    Commands: '-'=clear · test=verify · open=authorize "
                "· help=where to get it · env=import from .env"))
    touched: list[str] = []
    for key, label, _secret, url in spec:
        current = str(cfg.get(key, "") or "")
        print(f"\n  {bright(label)}")
        print(f"    current: {_mask(current)}")
        print("   " + faint("(Enter=keep · '-'=clear · 'test'=verify · "
                            "'open'=authorize · 'help'=info)"))
        typed = _prompt_key("New API KEY")
        if typed is None:
            continue
        action = KEY_COMMANDS.get(typed.strip().lower())
        if action == "keep" or (action is None and typed.strip() == ""):
            continue                       # keep the current value
        if action == "-":
            cfg[key] = ""
            info(f"cleared {key}")
            continue
        if action == "test":
            _report_probe(label, *probe_key(key, current, cfg))
            continue
        if action == "open":
            target = url or _key_urls().get(key, "")
            if target and _open_url(target):
                ok(f"Opened {target} — authorize there, then paste the key here.")
            else:
                warn("No page could be opened — use this link:")
                _help_for(key, url)
            continue
        if action == "help":
            _help_for(key, url)
            continue
        if action == "env":
            value = env_values.get(key.upper(), "") or os.environ.get(key.upper(), "")
            if value:
                cfg[key] = value.strip()
                touched.append(key)
                ok(f"imported {key} from .env / environment")
                _report_probe(label, *probe_key(key, cfg[key], cfg))
            else:
                warn(f"No {key.upper()} found in .env or the environment.")
            continue
        cfg[key] = typed.strip()
        touched.append(key)
        ok(f"set {key}  {_mask(cfg[key])}")
        # Verify the instant it is typed, so a truncated paste or a typo is
        # caught here instead of at the first conversation.
        _report_probe(label, *probe_key(key, cfg[key], cfg))

    if _write_config(cfg):
        ok(f"Saved to {CONFIG_FILE.relative_to(ROOT)}")
    _sync_gemini_env(str(cfg.get("gemini_api_key", "") or ""), env_values)
    if touched and _confirm("Run a live connection test on the keys you just set?",
                            default=True):
        test_keys("all")
    return True


# ── live connection tests (stdlib urllib only — no third-party import) ───────

def _http_status(url: str, headers: dict | None = None,
                 timeout: float = 12.0) -> tuple[int, bytes, str]:
    """One GET with the standard library. Returns ``(status, body, error)``.

    ``status`` is 0 when no HTTP reply arrived at all, so a caller can tell
    "the network is down" apart from "the provider answered 401".
    """
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), response.read(8192), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(8192)
        except Exception:
            body = b""
        return int(getattr(exc, "code", 0) or 0), body, ""
    except Exception as exc:
        return 0, b"", f"{type(exc).__name__}: {exc}"


def probe_key(field: str, value: str, config: dict | None = None) -> tuple[str, str]:
    """Test one credential against its real provider. Returns ``(state, detail)``.

    ``state`` is ``ready`` (the provider accepted it), ``failed``, ``optional``
    (nothing stored), ``present`` (stored, but the provider exposes no safe
    read-only call) or ``skipped`` (no cheap live endpoint exists). Only the
    standard library is used, so this still works before pip has run once.
    """
    field = str(field or "").strip().lower()
    value = str(value or "").strip()
    if not value:
        return "optional", "no value stored"

    def check(label: str, url: str, headers: dict | None,
              count_models: bool = False) -> tuple[str, str]:
        status, body, err = _http_status(url, headers)
        if status == 0:
            return "failed", f"{label} could not be reached ({err})"
        if status in (401, 403):
            return "failed", f"{label} rejected the key (HTTP {status})"
        if status != 200:
            return "failed", f"{label} answered HTTP {status}"
        if count_models:
            try:
                listed = len((json.loads(body.decode("utf-8", "replace")) or {})
                             .get("data") or [])
            except Exception:
                listed = 0
            return "ready", f"{label} accepted the key ({listed} model(s) listed)"
        return "ready", f"{label} accepted the key"

    if field == "gemini_api_key":
        import urllib.parse
        url = ("https://generativelanguage.googleapis.com/v1beta/models?key="
               + urllib.parse.quote(value, safe=""))
        status, _body, err = _http_status(url)
        if status == 0:
            return "failed", f"Google could not be reached ({err})"
        if status == 200:
            return "ready", "Google accepted the key"
        if status in (400, 401, 403):
            return "failed", f"Google rejected the key (HTTP {status})"
        return "failed", f"Google answered HTTP {status}"

    if field == "openai_api_key":
        return check("OpenAI", "https://api.openai.com/v1/models",
                     {"Authorization": f"Bearer {value}"}, count_models=True)
    if field == "groq_api_key":
        return check("Groq", "https://api.groq.com/openai/v1/models",
                     {"Authorization": f"Bearer {value}"}, count_models=True)
    if field == "openrouter_api_key":
        return check("OpenRouter", "https://openrouter.ai/api/v1/key",
                     {"Authorization": f"Bearer {value}"})
    if field == "anthropic_api_key":
        return check("Anthropic", "https://api.anthropic.com/v1/models",
                     {"x-api-key": value, "anthropic-version": "2023-06-01"},
                     count_models=True)
    if field == "fish_audio_api_key":
        return check("Fish Audio", "https://api.fish.audio/model?page_size=1",
                     {"Authorization": f"Bearer {value}"})
    if field == "elevenlabs_api_key":
        return check("ElevenLabs", "https://api.elevenlabs.io/v1/user",
                     {"xi-api-key": value})
    if field in ("qdrant_api_key", "qdrant_url"):
        cfg = config or _read_config()
        url = str(cfg.get("qdrant_url") or value or "http://localhost:6333")
        if "://" not in url:
            url = "http://" + url
        cluster_key = str(cfg.get("qdrant_api_key") or "")
        headers = {"api-key": cluster_key} if cluster_key else {}
        return check("Qdrant", url.rstrip("/") + "/collections", headers)
    if field == "mem0_api_key":
        return "present", ("Mem0 has no read-only call to test a key against — "
                           "the first memory write is the real test")
    return "skipped", "no cheap live endpoint for this credential"


def _report_probe(label: str, state: str, detail: str) -> None:
    """One credential's verdict, in the wizard's own colour language."""
    tag = label.split("(")[0].strip() or "key"
    if state == "ready":
        ok(f"{tag} — {detail}")
    elif state in ("optional", "present", "skipped"):
        print("  " + dim(f"• {tag} — {detail}"))
    else:
        bad(f"{tag} — {detail}")


def _group_spec(groups: str) -> list[tuple[str, str, bool, str]]:
    if groups == "all":
        return CORE_KEYS + VOICE_KEYS + EXTRA_KEYS
    if groups == "voice":
        return VOICE_KEYS
    if groups == "extra":
        return EXTRA_KEYS
    return CORE_KEYS


def test_keys(groups: str = "all") -> bool:
    """Check every stored credential against its provider.

    Returns True when nothing failed. Every verdict comes from a real request,
    so "working" means the provider answered 200 — not merely that a value was
    typed in.
    """
    section("Connection test")
    cfg = _read_config()
    working = failed = quiet = 0
    for key, label, _secret, _url in _group_spec(groups):
        state, detail = probe_key(key, str(cfg.get(key, "") or ""), cfg)
        tag = label.split("(")[0].strip() or key
        if state == "ready":
            ok(f"{tag} — {detail}")
            working += 1
        elif state in ("optional", "present", "skipped"):
            print("  " + dim(f"• {tag} — {detail}"))
            quiet += 1
        else:
            bad(f"{tag} — {detail}")
            failed += 1
    print()
    (ok if failed == 0 else warn)(
        f"{working} working · {failed} failed · {quiet} empty or not testable")
    if failed:
        info("Fix a failing key with [5] Configure API keys, then test again.")
    return failed == 0


# ── automatic Gemini repair ──────────────────────────────────────────────────
# The Settings ▸ API tab reports "Google Gemini — unavailable" for two very
# different reasons: the `google-genai` package is missing from the interpreter
# JARVIS actually runs with (the TEST detail says exactly that), or the stored
# key is not accepted. Both are repaired from here without opening the app: the
# package is installed into the same interpreter start_jarvis.py picks, the key
# is recovered from config/.env, and the repair is proven with a live probe.

GEMINI_IMPORT = "google.genai"
GEMINI_PACKAGE = "google-genai"


def _gemini_target_python() -> str:
    """The interpreter JARVIS will actually run with.

    The launcher prefers the project's own ``.venv`` outright, so the repair
    must land there too — installing into any other interpreter is the exact
    "wrong Python" problem ``start_jarvis.py`` exists to prevent.
    """
    vpy = _venv_python()
    if Path(vpy).is_file():
        return vpy
    return _get_target_python()


def _interpreter_has_module(exe: str, module: str, timeout: float = 30.0) -> bool:
    """True when *exe* can import *module* — asked in its own process.

    ``find_spec`` in this process would answer for the interpreter running the
    wizard, not the one JARVIS uses; the two are often different.
    """
    code = ("import importlib.util, sys; "
            "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)")
    r = _run([exe, "-c", code, module], timeout=timeout)
    return bool(r and r.returncode == 0)


def _gemini_key() -> str:
    """The stored Gemini key, importing it from .env / the environment if needed."""
    cfg = _read_config()
    key = str(cfg.get("gemini_api_key", "") or "").strip()
    if key:
        return key
    env_values = _read_env()
    key = (env_values.get("GEMINI_API_KEY", "")
           or str(os.environ.get("GEMINI_API_KEY", "") or "")).strip()
    if key:
        cfg["gemini_api_key"] = key
        _write_config(cfg)
        _sync_gemini_env(key, env_values)
        ok("Imported the Gemini key from .env / the environment")
    return key


def gemini_available() -> bool:
    """Offline readiness: the SDK imports in the target interpreter + a key exists."""
    return (_interpreter_has_module(_gemini_target_python(), GEMINI_IMPORT)
            and bool(_gemini_key()))


def fix_gemini(*, verify: bool = True, announce: bool = True) -> tuple[bool, str]:
    """Repair the Gemini setup in place: SDK package, stored key, live probe.

    Returns ``(ready, detail)``. ``ready`` is True only when the interpreter
    JARVIS runs with can import google.genai AND a key is stored AND (with
    *verify*) the provider accepted that key just now.
    """
    section("Gemini repair")
    exe = _gemini_target_python()
    info(f"Target interpreter: {exe}")

    module_ok = _interpreter_has_module(exe, GEMINI_IMPORT)
    if not module_ok:
        info(f"{GEMINI_PACKAGE} is missing — installing it into the interpreter "
             f"JARVIS uses…")
        r = _run_with_progress(
            [exe, "-m", "pip", "install", GEMINI_PACKAGE,
             "--disable-pip-version-check"],
            label=f"Installing {GEMINI_PACKAGE}", timeout=900,
            env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        module_ok = (bool(r and r.returncode == 0)
                     and _interpreter_has_module(exe, GEMINI_IMPORT))
    if module_ok:
        ok(f"{GEMINI_PACKAGE} is importable — the Settings TEST can use the SDK")
    else:
        bad(f"{GEMINI_PACKAGE} could not be installed — see the pip output above")
        info(f"Manual fix: \"{exe}\" -m pip install {GEMINI_PACKAGE}")

    key = _gemini_key()
    if not key:
        warn("No Gemini API key is stored — the Settings TEST reports "
             "'missing key' until one is added")
        info("Add it with [5] API keys, then run [x] again.")
        return False, "no gemini_api_key stored"

    if not verify:
        ready = module_ok
        (ok if ready else warn)(
            "Gemini engine ready (offline check — run [6] Test connections "
            "for a live probe)" if ready
            else "Gemini is still incomplete — see the notes above")
        return ready, "package present and key stored (not verified live)"

    state, detail = probe_key("gemini_api_key", key, _read_config())
    _report_probe("Gemini", state, detail)
    ready = module_ok and state == "ready"
    if ready and announce:
        ok("Gemini is ready — the Settings ▸ API TEST will report it online.")
    return ready, detail


def auto_fix_gemini_before_launch() -> None:
    """Repair a broken Gemini, then let the launch continue regardless.

    "Run JARVIS even when Gemini is unavailable" is the point: the assistant
    degrades to its other engines, so this never blocks the launch — it first
    removes the causes the Settings ▸ API test reports as "unavailable" (the
    missing google-genai package, a key present in only one of .env / config)
    and then hands over. Only the offline check runs here; the live probe stays
    in [x] Fix Gemini / [6] Test connections so a launch is never held up by
    the network.
    """
    if gemini_available():
        return
    warn("Gemini is not fully available — repairing automatically before launch…")
    fix_gemini(verify=False)


# ── missing-dependency install (the repair half of Diagnose) ─────────────────

def _feature_rows() -> list[dict]:
    """The installer's on-demand probe, or [] before dependencies exist."""
    try:
        sys.path.insert(0, str(ROOT))
        from app.installer import feature_status
        return feature_status()
    except Exception:
        return []


def _install_feature(feature: str) -> tuple[bool, str]:
    try:
        sys.path.insert(0, str(ROOT))
        from app.installer import install_feature
        return install_feature(feature, log=lambda m: print("      " + dim(str(m))))
    except Exception as exc:
        return False, f"{feature}: could not install ({type(exc).__name__}: {exc})"


def _count_configured_keys() -> int:
    cfg = _read_config()
    fields = {key for key, _l, _s, _u in CORE_KEYS + VOICE_KEYS + EXTRA_KEYS}
    return sum(1 for field in fields if str(cfg.get(field, "") or "").strip())


def install_all_missing() -> bool:
    """Install everything still absent with a simple overall progress indicator."""
    section("Install all missing")

    show_progress("Overall setup", 0, "Preparing downloads")
    python_ok = install_python_deps()
    show_progress("Overall setup", 30, "Python dependencies finished")

    info("Step 2/4 — Gemini engine repair (google-genai package + key)")
    gemini_ok, _gemini_detail = fix_gemini()
    show_progress("Overall setup", 45, "Gemini repair finished")

    info("Step 3/4 — optional feature packages")
    rows = _feature_rows()
    if not rows:
        warn("Feature probe unavailable — skipping the optional packages.")
    pending = sorted({r["feature"] for r in rows if not r.get("ready") and r.get("module") != "node"})
    if rows and not pending:
        ok("Every optional feature package is already installed.")
    for index, feature in enumerate(pending, start=1):
        good, sentence = _install_feature(feature)
        (ok if good else warn)(sentence)
        show_progress("Overall setup", 45 + (15 * index / max(1, len(pending))), feature)
    show_progress("Overall setup", 60, "Optional packages finished")

    info("Step 4/4 — Node runtime (Agent TARS) and Playwright browser")
    node_ok = install_node_deps()
    show_progress("Overall setup", 80, "Node runtime finished")
    browser_ok = install_playwright_browser()
    show_progress("Overall setup", 100, "All install steps finished")
    print()

    rule()
    if not gemini_ok:
        warn("Gemini still needs attention — run [x] Fix Gemini or [5] API keys.")
    good = python_ok and node_ok and browser_ok
    (ok if good else warn)("Install pass finished — run [9] Diagnose & repair to confirm." if good
                           else "Install pass finished with one or more problems.")
    return good

# ── 8. Optimize (housekeeping via core/maintenance.py) ───────────────────────

def optimize() -> dict:
    section("Optimize  (logs · cache · bytecode)")
    try:
        sys.path.insert(0, str(ROOT))
        from core.maintenance import run_maintenance, format_report
        report = run_maintenance()
        for line in format_report(report).splitlines():
            print("  " + line)
        return report
    except Exception as exc:
        bad(f"Maintenance could not run: {type(exc).__name__}: {exc}")
        return {}


# ── L. Launch ────────────────────────────────────────────────────────────────

def launch() -> int:
    section("Launch")
    if not START.is_file():
        bad("start_jarvis.py is missing — cannot launch")
        return 1
    auto_fix_gemini_before_launch()
    info("Starting J.A.R.V.I.S…")
    try:
        if _IS_WIN:
            subprocess.Popen([sys.executable, str(START)], cwd=str(ROOT))
            ok("J.A.R.V.I.S. is starting in a separate process")
            return 0
        return subprocess.call([sys.executable, str(START)], cwd=str(ROOT))
    except Exception as exc:
        bad(f"Launch failed: {exc}")
        return 1


# ── 9. Diagnose & repair ─────────────────────────────────────────────────────

def diagnose(auto_fix: bool = False) -> dict:
    """Report providers and engines, then offer to install what is missing.

    With ``auto_fix`` (or a "y" answer to the prompt) it runs
    :func:`install_all_missing`, so the report is the start of a repair rather
    than a dead end. Returns a small summary dict.
    """
    section("Diagnostics")
    check_python()
    check_pip()
    check_node()

    rows = _feature_rows()
    missing = [r for r in rows if not r.get("ready")]
    ready = len(rows) - len(missing)
    if rows:
        ok(f"optional feature packages ready: {ready}/{len(rows)}")
    else:
        warn("Feature probe unavailable until the Python dependencies are installed.")

    rule()
    if missing:
        warn(f"{len(missing)} item(s) still missing:")
        for row in missing:
            warn(f"  {row['feature']}: {row['detail']}  ->  {row['fix']}")
    else:
        ok("Everything the project probes for is installed.")

    configured = _count_configured_keys()
    info(f"API keys stored: {configured}  (choose [6] Test connections to verify)")

    if missing and (auto_fix or _confirm(
            f"Install all {len(missing)} missing item(s) now?", default=False)):
        install_all_missing()
    return {"ready": ready, "missing": len(missing)}


# ── packaging: a .exe that carries its saved data ────────────────────────────

#: Folders that hold J.A.R.V.I.S.'s own saved state. They are bundled into the
#: build so the executable starts with its configuration, memory and assets
#: instead of blank. The project folder itself is never moved or deleted.
DATA_FOLDERS = ("config", "data", "memory", "assets")

#: Where a build is written. dist/ holds only build output, so packaging can
#: never overwrite something the user cares about.
DIST_DIR = "dist"
WORK_DIR = "build/pyinstaller"
SPEC_DIR = "build"

#: Files a reset empties, after they have been backed up.
RESET_BACKUP_FILES = ("config/api_keys.json", "memory/long_term.json",
                      "data/brain_memory.db", "data/conversations.db",
                      "data/memory/growth.json", "data/mcp/servers.json")

#: Directories a reset clears completely: caches, logs and build leftovers.
RESET_CLEAR_DIRS = ("data/cache", "data/logs", WORK_DIR)


def _get_target_python() -> str:
    """Return the interpreter best suited to run JARVIS and packaging tools."""
    # 1. Probe sys.executable first
    try:
        r = _run([sys.executable, "-c", "import PyQt6"], timeout=10)
        if r and r.returncode == 0:
            return sys.executable
    except Exception:
        pass
    # 2. Check if start_jarvis.py resolved an interpreter
    try:
        if START.is_file():
            r = _run([sys.executable, str(START), "--check"], timeout=15)
            if r and r.returncode == 0:
                for line in (r.stdout or "").splitlines():
                    if "Chosen" in line and "(" in line and ")" in line:
                        cand = line.split("(", 1)[1].split(")", 1)[0].strip()
                        if cand and Path(cand).is_file():
                            return cand
    except Exception:
        pass
    # 3. Check 'python' on PATH
    py_cmd = shutil.which("python")
    if py_cmd:
        try:
            r = _run([py_cmd, "-c", "import PyQt6"], timeout=10)
            if r and r.returncode == 0:
                return py_cmd
        except Exception:
            pass
    return sys.executable


def build_pyinstaller_args(name: str, *, mode: str = "onedir",
                           windowed: bool = True, icon: str = "",
                           data_folders: tuple = DATA_FOLDERS,
                           entry: str = "main.py", distpath: str = DIST_DIR,
                           workpath: str = WORK_DIR,
                           specpath: str = SPEC_DIR,
                           python_exe: str | None = None) -> list:
    """The exact PyInstaller command for one build (pure, so tests can read it)."""
    py = python_exe or sys.executable
    args = [py, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--name", str(name), "--distpath", str(distpath),
            "--workpath", str(workpath), "--specpath", str(specpath),
            "--onefile" if str(mode) == "onefile" else "--onedir"]
    if windowed:
        args.append("--windowed")
    if icon:
        args += ["--icon", str(icon)]
    args += ["--paths", str(ROOT)]
    for h in ("core", "core.stability", "core.startup_optimizer",
              "core.maintenance", "observability", "capabilities",
              "PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets",
              "sqlite3", "json"):
        args += ["--hidden-import", h]
    for folder in data_folders:
        path = ROOT / folder
        if path.exists():
            args += ["--add-data", f"{path}{os.pathsep}{folder}"]
    args.append(str(ROOT / entry))
    return args


def _compact_sqlite_db(path: Path) -> int:
    """Run VACUUM and PRAGMA optimize on an SQLite database. Return bytes saved."""
    if not path.is_file():
        return 0
    size_before = path.stat().st_size
    if size_before < 4096:
        return 0
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("VACUUM;")
        conn.execute("PRAGMA optimize;")
        conn.close()
        size_after = path.stat().st_size
        return max(0, size_before - size_after)
    except Exception:
        return 0


def shrink_workspace(aggressive: bool = False) -> dict:
    """Reduce repository folder size: clean builds, caches, bytecode, oversized logs,
    and compact SQLite databases."""
    section("Compress / Shrink  ·  reduce folder size & reclaim disk space")
    bytes_freed = 0
    cleaned_items: list[str] = []

    # 1. Build and cache directories
    junk_dirs = [
        ROOT / "build",
        ROOT / ".pytest_cache",
        ROOT / ".pytest_tmp",
        ROOT / ".freebuff",
        ROOT / ".kilo",
        ROOT / "data" / "cache",
    ]
    if aggressive:
        junk_dirs.append(ROOT / "dist")

    for jd in junk_dirs:
        if jd.is_dir():
            try:
                dir_size = sum(f.stat().st_size for f in jd.rglob("*") if f.is_file())
                shutil.rmtree(jd, ignore_errors=True)
                bytes_freed += dir_size
                cleaned_items.append(f"Directory: {jd.name} ({round(dir_size / (1024*1024), 2)} MB)")
            except Exception:
                pass
    (ROOT / "data" / "cache").mkdir(parents=True, exist_ok=True)

    # 2. Python bytecode __pycache__ and *.pyc (excluding .venv, venv, node_modules)
    pyc_count = 0
    pyc_bytes = 0
    skip_dirs = {".venv", "venv", "env", "node_modules", ".git"}
    for root_dir, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if os.path.basename(root_dir) == "__pycache__":
            for f in files:
                fp = Path(root_dir) / f
                try:
                    sz = fp.stat().st_size
                    pyc_bytes += sz
                    pyc_count += 1
                except Exception:
                    pass
            try:
                shutil.rmtree(root_dir, ignore_errors=True)
            except Exception:
                pass
        else:
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    fp = Path(root_dir) / f
                    try:
                        sz = fp.stat().st_size
                        pyc_bytes += sz
                        pyc_count += 1
                        fp.unlink(missing_ok=True)
                    except Exception:
                        pass
    if pyc_count > 0:
        bytes_freed += pyc_bytes
        cleaned_items.append(f"Bytecode: {pyc_count} .pyc files ({round(pyc_bytes / (1024*1024), 2)} MB)")

    # 3. Compact SQLite databases
    db_freed = 0
    data_dir = ROOT / "data"
    if data_dir.is_dir():
        for db_file in data_dir.glob("*.db"):
            saved = _compact_sqlite_db(db_file)
            if saved > 0:
                db_freed += saved
                cleaned_items.append(f"Compacted {db_file.name} (saved {round(saved / 1024, 1)} KB)")
    bytes_freed += db_freed

    # 4. Truncate oversized crash log and clean old logs
    crash_log = ROOT / "data" / "logs" / "jarvis_crash.log"
    if crash_log.is_file():
        try:
            sz = crash_log.stat().st_size
            if sz > 100 * 1024:
                lines = crash_log.read_text(encoding="utf-8", errors="replace").splitlines()
                kept_lines = lines[-200:] if len(lines) > 200 else lines
                crash_log.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
                new_sz = crash_log.stat().st_size
                freed_log = max(0, sz - new_sz)
                bytes_freed += freed_log
                cleaned_items.append(f"Trimmed jarvis_crash.log (saved {round(freed_log / 1024, 1)} KB)")
        except Exception:
            pass

    log_dir = ROOT / "data" / "logs"
    if log_dir.is_dir():
        for old_log in log_dir.glob("*.log.prev"):
            try:
                sz = old_log.stat().st_size
                old_log.unlink(missing_ok=True)
                bytes_freed += sz
                cleaned_items.append(f"Removed {old_log.name}")
            except Exception:
                pass

    # 5. Git garbage collection
    if (ROOT / ".git").is_dir() and shutil.which("git"):
        r = _run(["git", "gc", "--prune=now", "--quiet"], timeout=60)
        if r and r.returncode == 0:
            cleaned_items.append("Git repository garbage collected & packed")

    # 6. Report
    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    if cleaned_items:
        for item in cleaned_items:
            ok(f"Cleaned: {item}")
        good(f"Shrink complete — freed ~{mb_freed} MB of space.")
    else:
        ok("Workspace is already compact — no reclaimable files found.")

    return {
        "bytes_freed": bytes_freed,
        "mb_freed": mb_freed,
        "cleaned_items": cleaned_items,
    }


def inspect_crashes(auto_fix: bool = False) -> dict:
    """Inspect run_jarvis crash causes, diagnose root causes, and apply fixes."""
    section("Crash Inspection & Diagnostic  ·  run_jarvis stability check")
    issues: list[dict] = []
    fixes: list[str] = []

    # 1. Check data/logs/jarvis_crash.log
    crash_log = ROOT / "data" / "logs" / "jarvis_crash.log"
    crash_count = 0
    recent_errors: list[str] = []
    if crash_log.is_file():
        try:
            content = crash_log.read_text(encoding="utf-8", errors="replace")
            crash_count = content.count("Windows fatal exception") + content.count("Traceback (most recent call last)")
            if crash_count > 0:
                lines = content.strip().splitlines()
                recent_errors = [line.strip() for line in lines[-20:] if line.strip()]
        except Exception:
            pass

    if crash_count > 0:
        issues.append({
            "source": "jarvis_crash.log",
            "count": crash_count,
            "details": recent_errors[-3:] if recent_errors else ["Crash dumps detected in log"],
        })
        warn(f"Found {crash_count} crash event(s) recorded in {crash_log.name}")
        for err_line in recent_errors[-5:]:
            print("    " + faint(err_line))
    else:
        ok(f"{crash_log.name}: No active crash dumps found.")

    # 2. Check jarvis_startup_error.log
    startup_err = ROOT / "jarvis_startup_error.log"
    if startup_err.is_file() and startup_err.stat().st_size > 0:
        err_snippet = startup_err.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        issues.append({
            "source": "jarvis_startup_error.log",
            "count": len(err_snippet),
            "details": err_snippet[-3:],
        })
        warn(f"Startup errors detected in {startup_err.name}:")
        for line in err_snippet[-3:]:
            print("    " + red(line))
    else:
        ok("Startup error log: Clean (no startup errors recorded).")

    # 3. Check for stale runtime lock
    lock_file = ROOT / "data" / "runtime" / "jarvis.lock"
    stale_lock = False
    if lock_file.is_file():
        try:
            pid_str = lock_file.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                running = False
                if _IS_WIN:
                    r = _run(["tasklist", "/FI", f"PID eq {pid}"], timeout=5)
                    running = bool(r and str(pid) in (r.stdout or ""))
                else:
                    try:
                        os.kill(pid, 0)
                        running = True
                    except OSError:
                        running = False
                if not running:
                    stale_lock = True
                    warn(f"Stale lock file found: PID {pid} is no longer running.")
                    issues.append({"source": "lock", "detail": f"Stale lock for PID {pid}"})
                else:
                    info(f"JARVIS is currently running with PID {pid}.")
        except Exception:
            pass

    # 4. Check critical modules
    opt_module = ROOT / "core" / "startup_optimizer.py"
    if not opt_module.is_file():
        warn("Missing core/startup_optimizer.py — causes ImportError on launch.")
        issues.append({"source": "module", "detail": "Missing core/startup_optimizer.py"})
    else:
        ok("Startup optimizer: core/startup_optimizer.py verified.")

    # 5. Check Python environment & PyQt6
    target_py = _get_target_python()
    r = _run([target_py, "-c", "import PyQt6; import core.stability"], timeout=10)
    if not r or r.returncode != 0:
        warn(f"Target Python ({target_py}) failed to import PyQt6 or core.stability.")
        issues.append({"source": "import", "detail": "Failed importing PyQt6 or core.stability"})
    else:
        ok(f"Target Python ({Path(target_py).name}): PyQt6 and core modules import cleanly.")

    # 6. Check OpenMP duplicate lib guard
    kmp = os.environ.get("KMP_DUPLICATE_LIB_OK")
    if kmp != "TRUE":
        info("KMP_DUPLICATE_LIB_OK is guarded at runtime in core/stability.py.")

    # Auto-fix handling
    if issues and (auto_fix or _confirm("Apply automatic repairs for detected issues now?", default=True)):
        if stale_lock and lock_file.is_file():
            try:
                lock_file.unlink(missing_ok=True)
                fixes.append("Removed stale runtime lock file")
                ok("Removed stale jarvis.lock.")
            except Exception as e:
                warn(f"Could not remove lock file: {e}")

        if not opt_module.is_file() and (ROOT / "startup_optimizer.py").is_file():
            try:
                shutil.copy2(ROOT / "startup_optimizer.py", opt_module)
                fixes.append("Restored core/startup_optimizer.py from root")
                ok("Restored core/startup_optimizer.py.")
            except Exception as e:
                warn(f"Could not restore core/startup_optimizer.py: {e}")

        if crash_log.is_file() and crash_count > 0:
            try:
                archive = crash_log.with_name(f"jarvis_crash_archived_{int(time.time())}.log")
                shutil.copy2(crash_log, archive)
                crash_log.write_text("", encoding="utf-8")
                fixes.append(f"Archived crash log to {archive.name} and cleared active log")
                ok(f"Archived previous crash log ({crash_count} entries).")
            except Exception as e:
                warn(f"Could not archive crash log: {e}")

        if startup_err.is_file() and startup_err.stat().st_size > 0:
            try:
                startup_err.unlink(missing_ok=True)
                fixes.append("Cleared jarvis_startup_error.log")
                ok("Cleared jarvis_startup_error.log.")
            except Exception:
                pass

        good(f"Repairs applied: {len(fixes)} fix(es) completed.")
    elif not issues:
        good("System is clean and fully optimized — no crash blockers detected.")

    return {
        "crashes_found": crash_count,
        "stale_lock": stale_lock,
        "issues": issues,
        "fixes_applied": fixes,
        "healthy": len(issues) == 0 or len(fixes) >= len(issues),
    }


def _terminate_running_jarvis() -> bool:
    """Ensure no running JARVIS processes hold locks on databases or logs."""
    lock_file = ROOT / "data" / "runtime" / "jarvis.lock"
    terminated = False
    if lock_file.is_file():
        try:
            pid_str = lock_file.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                if _IS_WIN:
                    _run(["taskkill", "/F", "/PID", str(pid)], timeout=5)
                else:
                    try:
                        os.kill(pid, 9)
                    except Exception:
                        pass
                terminated = True
        except Exception:
            pass
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass
    return terminated


def reset_plan(root: Path | None = None) -> dict:
    """What a reset would back up and clear — computed without touching anything."""
    base = Path(root or ROOT)
    vault = base / "Jarvis Obsidian Vault" / "Memory"
    pycaches: list[str] = []
    skip_dirs = {".venv", "venv", "env", "node_modules", ".git"}
    for r_dir, dirs, _files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        if os.path.basename(r_dir) == "__pycache__":
            pycaches.append(r_dir)

    return {
        "backup_files": [str(base / p) for p in RESET_BACKUP_FILES],
        "clear_dirs": [str(base / p) for p in RESET_CLEAR_DIRS],
        "pycache": pycaches,
        "vault_memory": str(vault) if vault.is_dir() else "",
    }


def _pyinstaller_ready(python_exe: str | None = None) -> bool:
    py = python_exe or _get_target_python()
    r = _run([py, "-m", "PyInstaller", "--version"], timeout=60)
    return bool(r and r.returncode == 0)


def build_executable(name: str = "", *, mode: str = "",
                     windowed: bool | None = None,
                     onefile: bool = False) -> bool:
    """Build the executable, keeping the project folder exactly as it is."""
    section("Convert to .exe  ·  a Jarvis OS executable that keeps its data")
    info("The project folder is never moved or deleted — output goes to dist/.")
    if not ENTRY.is_file():
        bad(f"{ENTRY.name} is missing — cannot build.")
        return False

    if not name:
        name = ask("  Name of the app [Jarvis OS] > ", "Jarvis OS").strip()
        name = name or "Jarvis OS"
    if not mode:
        mode = "onefile" if (onefile or _confirm(
            "  Single .exe file? (slower first start, one file)",
            False)) else "onedir"
    if windowed is None:
        windowed = not _confirm("  Show a console window while it runs "
                                "(debugging only)?", False)

    icon = ""
    default_icon = ROOT / "assets" / "jarvis.ico"
    if default_icon.is_file():
        if _confirm(f"  Use {default_icon.name} as the icon?", True):
            icon = str(default_icon)
    else:
        icon = ask("  Icon file (blank for none) > ", "").strip()

    target_py = _get_target_python()
    if not _pyinstaller_ready(target_py):
        warn(f"PyInstaller is not installed for {target_py}.")
        if not _confirm("  Install PyInstaller now with pip?", True):
            bad("A build needs PyInstaller — nothing was changed.")
            return False
        r = _run([target_py, "-m", "pip", "install", "pyinstaller"],
                 timeout=900)
        if not r or r.returncode != 0:
            bad("PyInstaller could not be installed — see the pip output above.")
            return False
        good("PyInstaller installed")

    wp = ROOT / WORK_DIR
    if wp.exists():
        try:
            shutil.rmtree(wp, ignore_errors=True)
        except Exception:
            pass

    args = build_pyinstaller_args(name, mode=mode, windowed=windowed, icon=icon, python_exe=target_py)
    info("Running: " + " ".join(args[1:]))
    r = _run_with_progress(args, label=f"Building {name}", timeout=3600)
    if not r or r.returncode != 0:
        bad("The build reported a problem — your project folder is untouched.")
        return False
    exe = (ROOT / DIST_DIR / f"{name}.exe" if str(mode) == "onefile"
           else ROOT / DIST_DIR / name / f"{name}.exe")
    if not exe.is_file():
        candidates = list((ROOT / DIST_DIR).glob(f"*{name}*"))
        if not candidates:
            bad("Build completed but the output executable was not found.")
            return False
        good(f"Built: {candidates[0]}")
    else:
        size_mb = round(exe.stat().st_size / (1024 * 1024), 1)
        good(f"Built: {exe} ({size_mb} MB)")
    print(dim("  The executable carries config/, data/, memory/ and assets/ with it."))
    return True


def reset_data(*, yes: bool = False) -> bool:
    """Back up, then return J.A.R.V.I.S. to a fresh start."""
    section("Reset data  ·  fresh start (everything is backed up first)")
    planned = reset_plan()
    print("  Backed up:")
    for path in planned["backup_files"]:
        if Path(path).exists():
            print("    " + faint(Path(path).name))
    if ENV_FILE.is_file():
        print("    " + faint(".env (stored credentials)"))
    if planned["vault_memory"]:
        print("    " + faint("vault Memory/ notes (archived, never deleted)"))
    print("  Cleared:")
    print("    " + faint("API keys (.env & config) · memory · conversations · caches · logs · temp"))
    print("  Kept:")
    print("    " + faint("the project source and your vault notes"))
    print()
    if not yes:
        if ask("  Type RESET to confirm > ", "").strip().upper() != "RESET":
            info("Nothing was changed.")
            return False

    # 0) Stop any running instance holding file locks
    _terminate_running_jarvis()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = ROOT / "config" / "backups" / f"reset_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    backed = 0
    for path in planned["backup_files"]:
        source = Path(path)
        if not source.exists():
            continue
        try:
            shutil.copy2(source, dest / source.name)
            backed += 1
        except Exception as exc:
            warn(f"{source.name} could not be backed up: {exc}")
        for ext in ("-wal", "-shm"):
            sidecar = Path(str(source) + ext)
            if sidecar.is_file():
                try:
                    shutil.copy2(sidecar, dest / sidecar.name)
                except Exception:
                    pass

    if ENV_FILE.is_file():
        try:
            shutil.copy2(ENV_FILE, dest / ".env")
            backed += 1
        except Exception:
            pass

    if planned["vault_memory"]:
        try:
            shutil.copytree(planned["vault_memory"], dest / "vault-Memory",
                            dirs_exist_ok=True)
        except Exception as exc:
            warn(f"the vault memory notes could not be archived: {exc}")

    # 1) Fresh API keys in config/api_keys.json
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({}, indent=4), encoding="utf-8")
    except Exception as exc:
        bad(f"the configuration could not be reset: {exc}")
        return False

    # 2) Fresh .env file — reset to template or blank
    try:
        if ENV_EXAMPLE.is_file():
            shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        else:
            ENV_FILE.write_text("", encoding="utf-8")
    except Exception as exc:
        warn(f".env could not be reset: {exc}")

    # 3) Memory stores & databases (everything backed up + sidecars)
    for path in planned["backup_files"]:
        target = Path(path)
        if target.name == "api_keys.json":
            continue
        try:
            target.unlink(missing_ok=True)
        except Exception:
            warn(f"{target.name} could not be removed")
        for ext in ("-wal", "-shm"):
            sidecar = Path(str(target) + ext)
            try:
                sidecar.unlink(missing_ok=True)
            except Exception:
                pass

    data_dir = ROOT / "data"
    if data_dir.is_dir():
        for db in data_dir.glob("*.db*"):
            try:
                db.unlink(missing_ok=True)
            except Exception:
                pass

    # 4) Caches, logs, build leftovers, bytecode
    for path in planned["clear_dirs"]:
        p = Path(path)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
        if p.name == "cache":
            p.mkdir(parents=True, exist_ok=True)
        elif p.name == "logs":
            p.mkdir(parents=True, exist_ok=True)
            try:
                (p / ".gitkeep").write_text("", encoding="utf-8")
            except Exception:
                pass

    for path in planned["pycache"]:
        try:
            shutil.rmtree(Path(path), ignore_errors=True)
        except Exception:
            pass

    skip_dirs = {".venv", "venv", "env", "node_modules", ".git"}
    for root_dir, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                try:
                    (Path(root_dir) / f).unlink(missing_ok=True)
                except Exception:
                    pass

    # 5) J.A.R.V.I.S.-owned temp files in workspace and system temp
    for temp_pattern in ("temp_*.py", "temp_*.json", "*.tmp"):
        for f in ROOT.glob(temp_pattern):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    temp_root = Path(os.environ.get("TEMP") or "/tmp")
    for pattern in ("jarvis*", "JARVIS*"):
        for target in temp_root.glob(pattern):
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            except Exception:
                pass

    lock_file = ROOT / "data" / "runtime" / "jarvis.lock"
    try:
        lock_file.unlink(missing_ok=True)
    except Exception:
        pass

    good(f"Reset complete — {backed} file(s) backed up to {dest}")
    print(dim("  Start J.A.R.V.I.S. and it will ask for a fresh API key."))
    return True


# ── extra menu actions ───────────────────────────────────────────────────────

def wipe_venv() -> bool:
    """Delete the local .venv folder so the next [2] starts completely clean."""
    section("Wipe virtual environment (.venv/)")
    if not VENV.is_dir():
        warn(f"{VENV.name}/ does not exist — nothing to delete.")
        return True
    if not _confirm(
        f"Delete  {VENV}/  and all installed packages?",
        default=False,
    ):
        info("Cancelled — .venv/ was not touched.")
        return False
    info(f"Removing  {VENV.name}/  …")
    try:
        shutil.rmtree(VENV)
        ok(f"{VENV.name}/ deleted.  Run [2] to reinstall into a fresh environment.")
        return True
    except Exception as exc:
        bad(f"Could not delete {VENV.name}/: {exc}")
        return False


def update_python_deps() -> bool:
    """Upgrade every package in the venv to the latest compatible version."""
    section("Update Python packages  (.venv/ — upgrade to latest)")
    if not REQUIREMENTS.is_file():
        bad(f"requirements.txt missing — cannot upgrade ({REQUIREMENTS})")
        return False
    if not ensure_venv():
        return False
    vpy = _venv_python()
    info("Upgrading pip inside the venv…")
    _run([vpy, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], timeout=120)
    info("Upgrading all packages from requirements.txt to latest versions…")
    r = _run_with_progress(
        [
            vpy, "-m", "pip", "install",
            "--upgrade",
            "-r", str(REQUIREMENTS),
            "--disable-pip-version-check",
        ],
        label="Upgrading packages", timeout=3600,
        env={"PIP_PROGRESS_BAR": "on", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    good = bool(r and r.returncode == 0)
    (ok if good else bad)(
        "All packages upgraded successfully." if good
        else "Upgrade finished with errors — check the output above."
    )
    return good


def install_ffmpeg() -> bool:
    """Install FFmpeg via winget (Windows only)."""
    section("Install FFmpeg")
    if shutil.which("ffmpeg"):
        ok("FFmpeg is already installed and on PATH.")
        return True
    if not _IS_WIN:
        bad("Automatic FFmpeg install is only supported on Windows via winget.")
        info("Install FFmpeg from https://ffmpeg.org/download.html")
        return False
    if not shutil.which("winget"):
        bad("winget is not available — install FFmpeg from https://ffmpeg.org/download.html")
        return False
    info("Installing FFmpeg via winget…")
    r = _run_with_progress(
        [
            "winget", "install", "--id", "Gyan.FFmpeg",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        label="Installing FFmpeg", timeout=1200,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("FFmpeg installed. Reopen this terminal so it lands on PATH.")
    else:
        bad("winget could not install FFmpeg.")
        info("Download from https://ffmpeg.org/download.html and add to PATH manually.")
    return good


def install_git() -> bool:
    """Install Git via winget (Windows only)."""
    section("Install Git")
    if shutil.which("git"):
        ok("Git is already installed and on PATH.")
        return True
    if not _IS_WIN:
        bad("Automatic Git install is only supported on Windows via winget.")
        info("Install Git from https://git-scm.com/downloads")
        return False
    if not shutil.which("winget"):
        bad("winget is not available — install Git from https://git-scm.com/downloads")
        return False
    info("Installing Git for Windows via winget…")
    r = _run_with_progress(
        [
            "winget", "install", "--id", "Git.Git",
            "-e",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ],
        label="Installing Git", timeout=1200,
    )
    good = bool(r and r.returncode == 0)
    if good:
        ok("Git installed. Reopen this terminal for it to appear on PATH.")
    else:
        bad("winget could not install Git.")
        info("Download from https://git-scm.com/downloads and run the installer.")
    return good


def open_project_folder() -> None:
    """Open the J.A.R.V.I.S. project root in the system file explorer."""
    section("Open project folder")
    info(f"Opening  {ROOT}  in the file explorer…")
    try:
        if _IS_WIN:
            subprocess.Popen(["explorer", str(ROOT)], **_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ROOT)])
        else:
            subprocess.Popen(["xdg-open", str(ROOT)])
        ok("File explorer opened.")
    except Exception as exc:
        bad(f"Could not open folder: {exc}")


def view_logs() -> None:
    """Print the tail of the most recent log files to the console."""
    section("View logs  (last 40 lines each)")
    log_sources = [
        ROOT / "data" / "logs" / "dependency_install.log",
        ROOT / "setup_error.log",
        ROOT / "jarvis_startup_error.log",
        ROOT / "data" / "logs" / "jarvis.log",
    ]
    found_any = False
    for log_path in log_sources:
        if not log_path.is_file() or log_path.stat().st_size == 0:
            continue
        found_any = True
        print()
        print("  " + gold(f"── {log_path.name} ") + faint(str(log_path)))
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-40:]
            for line in tail:
                print("    " + faint(line))
        except Exception as exc:
            bad(f"  Could not read {log_path.name}: {exc}")
    if not found_any:
        info("No log files found yet — run J.A.R.V.I.S. first.")


# ── the interactive menu ─────────────────────────────────────────────────────

#: (key, title, description) for every menu row — one source for the menu, so
#: the list can never drift out of step with the handler below.
MENU_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("SYSTEM", [
        ("1", "Environment check",   "Python · pip · Node · git · FFmpeg · .venv"),
        ("i", "Inspect & fix crash", "debug run_jarvis crashes & fix"),
        ("9", "Diagnose & repair",   "report, then fix what is missing"),
        ("t", "View logs",           "tail dependency_install, setup_error, jarvis logs"),
        ("d", "Open project folder", "open the JARVIS OS folder in Explorer"),
    ]),
    ("INSTALL", [
        ("p", "Install Python",      "auto-install Python 3.13 via winget"),
        ("2", "Python packages",     "requirements.txt → isolated .venv/ (delete to clean)"),
        ("u", "Update packages",     "pip upgrade all .venv packages to latest"),
        ("v", "Wipe .venv",          "delete .venv/ for a clean reinstall"),
        ("3", "Node runtime",        "Agent TARS browser + PC automation"),
        ("4", "Playwright browser",  "chromium binary for browser_action"),
        ("f", "Install FFmpeg",      "auto-install FFmpeg via winget"),
        ("g", "Install Git",         "auto-install Git via winget"),
        ("7", "Install ALL missing", "every optional feature package"),
    ]),
    ("ACCESS", [
        ("5", "API keys",            "Gemini + others -> .env / api_keys.json"),
        ("6", "Test connections",    "verify each key with its provider"),
        ("x", "Fix Gemini",          "repair google-genai install & verify key"),
        ("o", "Authorize providers", "open sign-up / key pages in browser"),
    ]),
    ("MAINTAIN", [
        ("8", "Optimize",            "rotate logs, expire cache, drop bytecode"),
        ("s", "Compress / Shrink",   "reduce folder size · clean cache & builds"),
        ("a", "Essentials",          "keys -> packages -> node -> optimize"),
    ]),
    ("PACKAGE", [
        ("c", "Convert to .exe",     "build Jarvis OS with its saved data bundled"),
        ("r", "Reset data",          "fresh start — keys, memory, cache (backed up)"),
    ]),
    ("LAUNCH", [
        ("l", "Launch J.A.R.V.I.S.", "hand over to start_jarvis.py"),
        ("0", "Exit",                ""),
    ]),
]

MENU_ROWS: list[tuple[str, str, str]] = [row for _g, rows in MENU_GROUPS for row in rows]
MENU_EXTRAS: list[tuple[str, str, str]] = []


def _menu_line(key: str, title: str, desc: str, selected: bool = False) -> str:
    """Selected row: green ▌ bar, bright title. Others: faint, quiet."""
    inner = _BOX_W - 2
    edge = (_fg(L_CYAN) + "│" + _rst()) if _COLOR else "|"
    if not _COLOR:
        body = f"{'>' if selected else ' '} [{key}] {title:<22} {desc}"
    elif selected:
        body = (_fg(L_GREEN) + "▌" + _rst() + " " + _fg(L_GREEN) + f"\x1b[1m[{key}]" + _rst() + "  " +
                _fg(L_WHITE) + f"\x1b[1m{title:<22}" + _rst() + " " + _fg(L_GREEN) + desc + _rst())
    else:
        body = (" " + " " + _fg(L_CYAN) + f"[{key}]" + _rst() + "  " +
                _fg(_lerp(L_WHITE, L_FAINT, 0.35)) + f"{title:<22}" + _rst() + " " +
                _fg(L_FAINT) + desc + _rst())
    body = " " + body
    # clip the description if the row would overflow the frame
    while _vlen(body) > inner - 1 and desc:
        desc = desc[:-2]
        return _menu_line(key, title, desc.rstrip() + "…", selected) if not desc.endswith("…") else body
    return "  " + edge + _pad(body, inner) + edge


def _group_header(name: str) -> str:
    inner = _BOX_W - 2
    edge = (_fg(L_CYAN) + "│" + _rst()) if _COLOR else "|"
    label = (_fg(L_FAINT) + "◢ " + _rst() + _fg(L_CYAN) + name + _rst() +
             " " + _fg(L_TRACK) + "─" * max(2, inner - len(name) - 6) + _rst()) if _COLOR \
        else f"-- {name} " + "-" * max(2, inner - len(name) - 6)
    return "  " + edge + _pad(" " + label, inner) + edge


def render_menu(selected: int = 0) -> str:
    inner = _BOX_W - 2
    lines = [_panel_top("ACTIONS", "SELECT")]
    index = 0
    for gi, (group, rows) in enumerate(MENU_GROUPS):
        lines.append(_group_header(group))
        for key, title, desc in rows:
            lines.append(_menu_line(key, title, desc, index == selected))
            index += 1
    lines += [
        _panel_bottom(),
        "",
        (" " + _fg(L_FAINT) + "  ↑ ↓ / W S  move    Enter  run    shortcut keys  [ ]    Q  exit" + _rst())
        if _COLOR else "  up/down or W/S move, Enter run, Q exit",
    ]
    return "\n".join(lines)


def menu() -> int:
    if not (sys.stdin and sys.stdin.isatty()):
        report_env()
        warn("No interactive terminal detected — run with a flag instead, e.g.")
        print(dim("    python setup_jarvis.py --check"))
        print(dim("    python setup_jarvis.py --all --yes     # install everything"))
        print(dim("    python setup_jarvis.py --launch"))
        return 0

    rows = MENU_ROWS + MENU_EXTRAS
    selected = 0
    primed = False

    while True:
        clear_screen()
        banner()
        if not primed:
            report_env()
            primed = True

        print(render_menu(selected))
        choice = _read_menu_key()

        if choice == "up":
            selected = (selected - 1) % len(rows)
            continue
        if choice == "down":
            selected = (selected + 1) % len(rows)
            continue
        if choice in ("q", "0"):
            print(_fg(L_FAINT) + "\n  ● SETUP COMPLETE  —  bye, Sir." + _rst())
            return 0
        if choice == "enter":
            choice = rows[selected][0]

        clear_screen()
        banner()
        handled = True
        if choice == "1":
            report_env()
        elif choice == "2":
            install_python_deps()
        elif choice == "3":
            install_node_deps()
        elif choice == "4":
            install_playwright_browser()
        elif choice == "5":
            g = ask("  group [core / voice / extra / all] (core) > ", "core").strip().lower() or "core"
            configure_keys(g)
        elif choice == "6":
            test_keys("all")
        elif choice == "x":
            fixed, _detail = fix_gemini()
            if not fixed and _confirm("Start J.A.R.V.I.S. anyway?", default=True):
                return launch()
        elif choice == "7":
            install_all_missing()
        elif choice == "8":
            optimize()
        elif choice == "9":
            diagnose()
        elif choice == "p":
            install_python_runtime()
        elif choice == "u":
            update_python_deps()
        elif choice == "v":
            wipe_venv()
        elif choice == "f":
            install_ffmpeg()
        elif choice == "g":
            install_git()
        elif choice == "t":
            view_logs()
        elif choice == "d":
            open_project_folder()
        elif choice == "s":
            shrink_workspace()
        elif choice == "i":
            inspect_crashes()
        elif choice == "c":
            build_executable()
        elif choice == "r":
            reset_data()
        elif choice == "o":
            authorize_keys()
        elif choice == "l":
            return launch()
        elif choice == "a":
            section("Essentials  ·  keys → dependencies → Gemini → node → optimize")
            configure_keys("core")
            install_python_deps()
            fix_gemini()
            install_node_deps()
            optimize()
        else:
            handled = False
            warn("Unknown choice. Use ↑/↓, W/S, or the number/letter shortcuts.")

        if handled:
            pause("  Press Enter to return to the menu…")


# ── CLI ──────────────────────────────────────────────────────────────────────

USAGE = """\
python setup_jarvis.py                 interactive Setup & Control Center
python setup_jarvis.py --check         environment report only
python setup_jarvis.py --install       install / repair the Python dependencies
python setup_jarvis.py --node          install the Node runtime (Agent TARS)
python setup_jarvis.py --browser       install the Playwright chromium binary
python setup_jarvis.py --keys          configure API keys (.env + api_keys.json)
python setup_jarvis.py --authorize     open the provider sign-up / key pages
python setup_jarvis.py --test          test every stored key against its provider
python setup_jarvis.py --fix-gemini    repair Gemini: install google-genai into the
                                       interpreter JARVIS uses, check the key, verify live
python setup_jarvis.py --fix-gemini --launch
                                       repair Gemini, then start J.A.R.V.I.S.
python setup_jarvis.py --install-missing  install every dependency still absent
python setup_jarvis.py --optimize      housekeeping (logs, cache, bytecode)
python setup_jarvis.py --shrink        remove build dirs, clean caches, vacuum DBs
python setup_jarvis.py --inspect-crash inspect & auto-fix run_jarvis crash causes
python setup_jarvis.py --diagnose      report providers and engines
python setup_jarvis.py --diagnose --yes  the same, auto-installing what is missing
python setup_jarvis.py --all --yes     keys + dependencies + node, non-interactive
python setup_jarvis.py --launch        start J.A.R.V.I.S.
python setup_jarvis.py --build-exe     build a Jarvis OS .exe with its saved data
python setup_jarvis.py --build-exe --name "Jarvis OS" --onefile
python setup_jarvis.py --reset --yes   back up, then reset to a fresh start
"""


def build_parser() -> argparse.ArgumentParser:
    """The flag form, for scripts and CI where a menu cannot be answered."""
    parser = argparse.ArgumentParser(
        prog="setup_jarvis.py",
        description="J.A.R.V.I.S. — Setup & Control Center",
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true",
                        help="report the environment and exit")
    parser.add_argument("--install", action="store_true",
                        help="install / repair the Python dependencies")
    parser.add_argument("--node", action="store_true",
                        help="install the Node runtime (Agent TARS)")
    parser.add_argument("--browser", action="store_true",
                        help="install the Playwright chromium binary")
    parser.add_argument("--keys", action="store_true",
                        help="configure API keys")
    parser.add_argument("--group", default="core",
                        choices=("core", "voice", "extra", "all"),
                        help="which key group --keys collects (default: core)")
    parser.add_argument("--test", action="store_true",
                        help="test every stored API key against its provider")
    parser.add_argument("--fix-gemini", dest="fix_gemini", action="store_true",
                        help="repair Gemini: install google-genai into the "
                             "interpreter JARVIS uses, recover/check the stored "
                             "key, and verify it with a live probe")
    parser.add_argument("--authorize", action="store_true",
                        help="open each provider's sign-up / key page in a browser")
    parser.add_argument("--install-missing", "--fix", dest="install_missing",
                        action="store_true",
                        help="install every dependency still absent "
                             "(requirements + optional features + node)")
    parser.add_argument("--optimize", action="store_true",
                        help="run housekeeping (logs, cache, bytecode)")
    parser.add_argument("--diagnose", action="store_true",
                        help="report providers and engines")
    parser.add_argument("--all", action="store_true",
                        help="keys, dependencies and node, in order")
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive: never prompt, take defaults")
    parser.add_argument("--launch", action="store_true",
                        help="start J.A.R.V.I.S.")
    parser.add_argument("--build-exe", dest="build_exe", action="store_true",
                        help="build a Jarvis OS executable that bundles its "
                             "saved data (config/, data/, memory/, assets/)")
    parser.add_argument("--name", default="Jarvis OS",
                        help="executable name for --build-exe (default: Jarvis OS)")
    parser.add_argument("--onefile", action="store_true",
                        help="--build-exe: one single .exe instead of a folder")
    parser.add_argument("--console", action="store_true",
                        help="--build-exe: keep a console window (debugging only)")
    parser.add_argument("--reset", action="store_true",
                        help="back up, then reset keys, memory and cache to a "
                             "fresh start")
    parser.add_argument("--shrink", action="store_true",
                        help="reduce folder size: clean build dirs, caches, "
                             "bytecode, and vacuum SQLite databases")
    parser.add_argument("--inspect-crash", dest="inspect_crash",
                        action="store_true",
                        help="inspect run_jarvis crash causes and auto-fix them")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. No flag at all drops into the interactive menu.

    Order matters: keys first (so a later step that needs one has it), then the
    Python dependencies, then the Node runtime — the same order the menu's
    ESSENTIALS shortcut uses, and the same order a bare machine needs.
    """
    args = build_parser().parse_args(argv)
    banner()

    did_something = False

    if args.check:
        report_env()
        did_something = True

    if args.all:
        print(bold("\n  ESSENTIALS: keys → dependencies → Gemini → node → optimize"))
        configure_keys("all", non_interactive=bool(args.yes))
        install_python_deps()
        fix_gemini()
        install_node_deps()
        optimize()
        did_something = True

    if args.keys:
        configure_keys(args.group, non_interactive=bool(args.yes))
        did_something = True

    if args.install:
        install_python_deps()
        did_something = True

    if args.node:
        install_node_deps()
        did_something = True

    if args.browser:
        install_playwright_browser()
        did_something = True

    if args.test:
        test_keys(args.group)
        did_something = True

    if args.fix_gemini:
        fix_gemini()
        did_something = True

    if args.authorize:
        authorize_keys()
        did_something = True

    if args.install_missing:
        install_all_missing()
        did_something = True

    if args.optimize:
        optimize()
        did_something = True

    if args.diagnose:
        diagnose(auto_fix=bool(args.yes))
        did_something = True

    if args.build_exe:
        build_executable(args.name,
                         mode="onefile" if args.onefile else "onedir",
                         windowed=not args.console)
        did_something = True

    if args.reset:
        reset_data(yes=bool(args.yes))
        did_something = True

    if args.shrink:
        shrink_workspace()
        did_something = True

    if args.inspect_crash:
        inspect_crashes()
        did_something = True

    if args.launch:
        return launch()

    if not did_something:
        return menu()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())