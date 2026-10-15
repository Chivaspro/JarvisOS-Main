"""J.A.R.V.I.S launcher — the one place that decides WHICH Python runs the app.

Why this file is a resolver and not a one-line launcher
-------------------------------------------------------
A Windows box commonly has several Pythons: the Microsoft Store build, a
python.org build behind ``py``, and a ``python``/``pythonw`` on PATH that may
belong to either. They are *separate* environments: installing
``requirements.txt`` into one leaves the others empty.

That is exactly how J.A.R.V.I.S failed to start. ``py -3`` resolved to a
Python 3.11 with nothing but pip installed, every launcher preferred ``py -3``
because ``where py`` succeeded, and the app died on the very first third-party
import (``ui.py`` line 19) with::

    ModuleNotFoundError: No module named 'psutil'

while a fully provisioned interpreter sat on PATH, unused.

So this module does what a launcher should: it *proves* an interpreter can
import what the app needs before handing over the process, and only if no
interpreter on the machine qualifies does it install the dependencies.

STDLIB ONLY — deliberately. This code has to run on the bare interpreter that
caused the failure (pip and nothing else), so it may never import a third-party
package. The one project module it imports, ``launch_ui`` (inside ``main()``,
with a fallback when the import fails), is itself stdlib-only and is checked by
``tests/unit/test_startup_bootstrap.py``.

Usage::

    python start_jarvis.py            # resolve, install if needed, launch
    python start_jarvis.py --check    # report candidates and chosen one, exit
    python start_jarvis.py --install  # install into the interpreter that will run

    --splash      transparent text-only launch overlay (what run_jarvis.vbs uses)
    --console-ui  animated console launch screen (what run_jarvis.bat uses; it is
                  also chosen automatically when stdout is a real console)
    --no-ui       never draw a launch UI

Before the app starts, a launch optimization pass runs (cache/temp sweep,
orphaned bytecode, oversized logs, chat-DB compaction — see
core/startup_optimizer.py) and its real progress drives the launch UI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Windows consoles default to a legacy codepage (cp1252, cp1254, cp932 …),
# where an em-dash or ellipsis in a launcher message prints as mojibake. The
# same guard main.py uses: reconfigure with a replacement fallback, never fatal.
for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
ENTRY = BASE / "main.py"
REQUIREMENTS = BASE / "requirements.txt"
LOG_DIR = BASE / "data" / "logs"
INSTALL_LOG = LOG_DIR / "dependency_install.log"
ENTRY_ERROR_LOG = BASE / "jarvis_startup_error.log"

# Set on the child process this module launches, so a mistaken re-entry can
# never resolve-and-install recursively.
ENV_BOOTSTRAPPED = "JARVIS_BOOTSTRAPPED"
# Escape hatch for the test harness: never runs pip.
ENV_SKIP_INSTALL = "JARVIS_SKIP_DEP_INSTALL"

# What ``import ui`` / ``import main`` genuinely need at import time. Anything
# else (vision, cloud TTS, memory backends, browser automation …) is optional,
# installed on demand and degrades gracefully, so it must never block a start.
HARD_IMPORTS: tuple[str, ...] = (
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "numpy",
)

# Probed only to *report* which optional engines are missing — a candidate that
# has every hard import still wins with soft ones missing.
SOFT_IMPORTS: tuple[str, ...] = (
    "psutil",
    "cv2",
    "requests",
    "PIL",
    "sounddevice",
    "mss",
    "send2trash",
)

PROBE_TIMEOUT = 30.0          # cold PyQt6/numpy import on a slow disk
INSTALL_TIMEOUT = 3600.0      # a first full install can genuinely take minutes

_WIN = sys.platform == "win32"

# Child process that reports what an interpreter can import. Kept as a source
# string so nothing has to exist on disk for a probe to run.
_PROBE_CODE = (
    "import importlib.util, json, sys\n"
    "missing = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        if importlib.util.find_spec(name) is None:\n"
    "            missing.append(name)\n"
    "    except Exception:\n"
    "        missing.append(name)\n"
    "print(json.dumps({\n"
    "    'missing': missing,\n"
    "    'version': sys.version.split()[0],\n"
    "    'executable': sys.executable,\n"
    "}))\n"
)


# ── Console / dialog plumbing ─────────────────────────────────────────────
# Launched from run_jarvis.vbs this runs under pythonw.exe, where sys.stdout
# is None: printing there raises AttributeError, so every write is guarded.

def _has_console() -> bool:
    """True when this process can actually show console output to the user."""
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        try:
            if stream is not None and stream.isatty():
                return True
        except Exception:
            continue
    return False


def _echo(text: str = "") -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        stream.write(str(text) + "\n")
        stream.flush()
    except Exception:
        pass


def dialog(text: str, title: str = "J.A.R.V.I.S", flags: int = 0x10) -> None:
    """Native Windows message box; silently skipped everywhere else.

    ``flags`` is the MessageBoxW uType (0x10 error, 0x40 information).
    """
    if not _WIN:
        _echo(f"{title}: {text}")
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), int(flags))
    except Exception:
        _echo(f"{title}: {text}")


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WIN else 0


# ── Candidate interpreters ────────────────────────────────────────────────

def _venv_python() -> Path | None:
    """The project's own virtualenv, when it exists (README's recommended setup)."""
    for relative in (
        Path(".venv") / "Scripts" / "python.exe",
        Path(".venv") / "bin" / "python",
        Path("venv") / "Scripts" / "python.exe",
        Path("venv") / "bin" / "python",
    ):
        candidate = BASE / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _py_launcher_interpreters() -> list[Path]:
    """Interpreters registered with the Windows ``py`` launcher (``py -0p``).

    This is how a python.org install that is *not* first on PATH is still
    discovered: ``py -0p`` lists every registered build with its full path.
    """
    if not _WIN:
        return []
    try:
        proc = subprocess.run(
            ["py", "-0p"],
            capture_output=True,
            text=True,
            timeout=15.0,
            creationflags=_no_window_flags(),
        )
    except Exception:
        return []
    found: list[Path] = []
    for line in (proc.stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        token = stripped.split()[-1]
        # Each line ends with the interpreter path; skip anything else (the
        # "-V:3.11" version column never contains a python executable name).
        if not token.lower().endswith(".exe") or "python" not in Path(token).name.lower():
            continue
        candidate = Path(token)
        try:
            if candidate.is_file():
                found.append(candidate)
        except OSError:
            continue
    return found


def _unique_existing(paths) -> list[Path]:
    """De-duplicate interpreter paths, dropping anything that is not a file."""
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        if path is None:
            continue
        try:
            candidate = Path(path)
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved).casefold() if _WIN else str(resolved)
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def candidate_interpreters() -> list[Path]:
    """Every interpreter worth testing, in order of preference.

    Order matters. The project's own virtualenv wins outright; then the
    ``python``/``pythonw`` that PATH resolves (usually the environment a user
    actually installed into); then every build the ``py`` launcher knows about;
    and finally the interpreter running this file, so a bootstrap launched by
    an already-correct interpreter still has somewhere to land.
    """
    if _WIN:
        path_names = ("python.exe", "pythonw.exe")
    else:
        path_names = ("python3", "python")

    ordered: list[Path | None] = [_venv_python()]
    for name in path_names:
        found = shutil.which(name)
        if found:
            ordered.append(Path(found))
    ordered.extend(_py_launcher_interpreters())
    ordered.append(Path(sys.executable) if sys.executable else None)
    return _unique_existing(ordered)


# ── Probing ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    """One interpreter and what a real subprocess proved about it."""

    exe: Path
    version: str = "?"
    missing: tuple[str, ...] = ()
    error: str | None = None

    @property
    def usable(self) -> bool:
        """The interpreter ran our probe at all (so it can also run pip)."""
        return self.error is None

    @property
    def complete(self) -> bool:
        """Every hard import resolved — this interpreter can run JARVIS."""
        return self.error is None and not self.missing


def probe_interpreter(exe, modules=HARD_IMPORTS, timeout: float = PROBE_TIMEOUT) -> Candidate:
    """Ask *exe* (in its own process) which of *modules* it can import.

    Runs in a fresh interpreter rather than in this one: ``find_spec`` in the
    current process would answer for the WRONG environment, which is the very
    confusion this module exists to remove. A probe that cannot run at all
    (no such file, crash, timeout) is returned with ``error`` set.
    """
    exe = Path(exe)
    wanted = [str(m) for m in modules]
    try:
        proc = subprocess.run(
            [str(exe), "-c", _PROBE_CODE, *wanted],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_no_window_flags(),
        )
    except Exception as exc:
        return Candidate(exe=exe, missing=tuple(wanted), error=f"{type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return Candidate(exe=exe, missing=tuple(wanted),
                         error=detail[-1] if detail else f"exit code {proc.returncode}")
    payload = None
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if not isinstance(payload, dict):
        return Candidate(exe=exe, missing=tuple(wanted), error="unreadable probe output")
    missing = tuple(str(m) for m in payload.get("missing", []))
    return Candidate(exe=exe, version=str(payload.get("version", "?")), missing=missing)


def resolve(interpreter=None, log=None, on_progress=None) -> list[Candidate]:
    """Probe every candidate and return the results in preference order.

    ``on_progress(fraction, label, detail)`` lets a launch UI follow along.
    """
    results: list[Candidate] = []
    exes = candidate_interpreters()
    for index, exe in enumerate(exes):
        if on_progress is not None:
            try:
                on_progress(index / max(1, len(exes)), "PROBING PYTHON RUNTIME",
                            f"{exe.name}  ({exe.parent})")
            except Exception:
                pass
        result = probe_interpreter(exe, HARD_IMPORTS)
        results.append(result)
        if log is not None:
            if result.error:
                log(f"  [skip] {exe} — {result.error}")
            elif result.complete:
                log(f"  [ ok ] {exe} (Python {result.version})")
            else:
                log(f"  [miss] {exe} (Python {result.version}) missing: "
                    f"{', '.join(result.missing)}")
    if interpreter is not None:
        forced = probe_interpreter(interpreter, HARD_IMPORTS)
        results.insert(0, forced)
    return results


def first_complete(results) -> Candidate | None:
    """The first interpreter that can run JARVIS as-is."""
    for result in results:
        if result.complete:
            return result
    return None


def first_usable(results) -> Candidate | None:
    """The first interpreter that at least *ran* — the sane install target."""
    for result in results:
        if result.usable:
            return result
    return None


def install_target(interpreter=None, results=None) -> Path:
    """Which interpreter a ``pip install`` should be pointed at.

    Prefer an interpreter that already works but lacks packages (it is the one
    the launchers will pick, so filling it in fixes the problem for good); fall
    back to the running interpreter rather than failing outright.
    """
    if interpreter is not None:
        return Path(interpreter)
    usable = first_usable(results or [])
    if usable is not None:
        return usable.exe
    return Path(sys.executable)


# ── Installing ────────────────────────────────────────────────────────────

def install_requirements(exe, log=None) -> bool:
    """``pip install -r requirements.txt`` with *exe*; output goes to a log."""
    if os.environ.get(ENV_SKIP_INSTALL, "").strip().lower() in ("1", "true", "yes"):
        if log:
            log(f"  (skipped: {ENV_SKIP_INSTALL} is set)")
        return False
    if not REQUIREMENTS.is_file():
        if log:
            log(f"  requirements.txt is missing: {REQUIREMENTS}")
        return False
    cmd = [str(exe), "-m", "pip", "install", "-r", str(REQUIREMENTS),
           "--disable-pip-version-check"]
    if log:
        log(f"  {' '.join(cmd)}")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with INSTALL_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {cmd[0]} -m pip install -r requirements.txt ===\n")
            handle.flush()
            proc = subprocess.run(
                cmd,
                cwd=str(BASE),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=INSTALL_TIMEOUT,
                creationflags=_no_window_flags(),
            )
    except Exception as exc:
        if log:
            log(f"  install failed to run: {type(exc).__name__}: {exc}")
        return False
    if proc.returncode != 0 and log:
        log(f"  pip exited with code {proc.returncode} — see {INSTALL_LOG}")
    return proc.returncode == 0


# ── Launching ─────────────────────────────────────────────────────────────

def console_less(exe) -> Path:
    """The GUI (no-console) twin of *exe*, when it exists.

    Launching ``python.exe`` from a process that has no console would allocate
    a fresh black window for JARVIS; ``pythonw.exe`` beside it never does.
    """
    exe = Path(exe)
    name = exe.name.lower()
    if not _WIN or not name.endswith(".exe") or "pythonw" in name:
        return exe
    if not name.startswith("python"):
        return exe
    sibling = exe.with_name(name.replace("python", "pythonw", 1))
    try:
        return sibling if sibling.is_file() else exe
    except OSError:
        return exe


def launch(exe, on_started=None) -> int:
    """Start ``main.py`` in its own process with *exe* and wait for it.

    ``on_started()`` runs the moment the process exists (the launch UI closes
    there, rather than lingering until the app exits).
    """
    exe = Path(exe)
    console = _has_console()
    target = exe if console else console_less(exe)
    env = dict(os.environ)
    env[ENV_BOOTSTRAPPED] = "1"
    kwargs: dict = {"cwd": str(BASE), "env": env}
    if not console:
        flags = _no_window_flags()
        if flags:
            kwargs["creationflags"] = flags
    try:
        proc = subprocess.Popen([str(target), str(ENTRY)], **kwargs)
    except Exception as exc:
        dialog(f"J.A.R.V.I.S could not be launched.\n\n"
               f"Interpreter: {target}\nEntry point: {ENTRY}\n\n"
               f"{type(exc).__name__}: {exc}", "J.A.R.V.I.S — Startup Error")
        return 1
    if on_started is not None:
        try:
            on_started()
        except Exception:
            pass
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:
            pass
        return 130


# ── Launch optimization (cache + temp) ────────────────────────────────────
# Runs FIRST on every normal launch, concurrently with interpreter probing so it
# adds almost no waiting. Stdlib only: core/startup_optimizer.py is loaded by
# file path (not as the ``core`` package, which may import heavy modules).

ENV_OPT_DONE = "JARVIS_STARTUP_OPT_DONE"
ENV_OPT_SUMMARY = "JARVIS_STARTUP_OPT_SUMMARY"
LOCK_PATH = BASE / "data" / "runtime" / "jarvis.lock"


def _live_instance() -> bool:
    """True when a running JARVIS owns the lock file. Unsure => True (never clean)."""
    try:
        pid = int(LOCK_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
    except Exception:
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if not _WIN:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87      # 87 = no such process
        try:
            code = ctypes.c_ulong(0)
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return (not ok) or code.value == 259      # 259 = STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return True


def _start_optimizer(progress=None):
    """Begin the cache/temp sweep on a background thread; None when skipped."""
    module = BASE / "core" / "startup_optimizer.py"
    try:
        if not module.is_file() or _live_instance():
            return None
        import importlib.util

        spec = importlib.util.spec_from_file_location("jarvis_startup_optimizer", str(module))
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        try:
            return loaded.start(BASE, progress=progress)
        except TypeError:            # an older optimizer without progress support
            return loaded.start(BASE)
    except Exception:
        return None


def _finish_optimizer(handle, log) -> None:
    """Give the sweep a few seconds, then hand its result to main.py via env."""
    if handle is None:
        return
    try:
        handle.wait(25.0)   # its own time budget is 15s; never cut a sweep short
        os.environ[ENV_OPT_DONE] = "1"   # main.py will not sweep a second time
        line = handle.summary()
        if line:
            os.environ[ENV_OPT_SUMMARY] = line
            log(line)
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────

def _install_log_hint() -> str:
    return f"Install log:\n{INSTALL_LOG}"


def _report(results, chosen: Candidate | None, log) -> None:
    if chosen is None:
        log("No interpreter with the J.A.R.V.I.S dependencies was found.")
        return
    log("")
    log(f"Selected interpreter: {chosen.exe}  (Python {chosen.version})")
    soft = probe_interpreter(chosen.exe, SOFT_IMPORTS)
    if soft.missing:
        log(f"Optional engines not installed here: {', '.join(soft.missing)}")
        log("(these are installed on demand and do not block startup)")


# ── Launch UI selection ───────────────────────────────────────────────────

# Weighted tracks behind the percentage. "optimize" and "resolve" genuinely run
# at the same time, so the bar tracks both rather than pretending they are serial.
_TRACKS = {"optimize": 0.50, "resolve": 0.28, "deps": 0.07, "launch": 0.15}


def _ui_mode(args, check_only: bool, install_only: bool) -> str:
    """'splash' | 'console' | 'none'."""
    if check_only or install_only or "--no-ui" in args:
        return "none"
    if os.environ.get("JARVIS_LAUNCH_UI", "1").strip().lower() in ("0", "false", "no", "off"):
        return "none"
    if "--splash" in args:
        return "splash"
    if "--console-ui" in args:
        return "console"
    return "console" if _has_console() else "none"


def _pipeline(args, prog, ui) -> int:
    """The whole launch sequence. *prog*/*ui* are None when no launch UI is shown."""
    check_only = "--check" in args
    install_only = "--install" in args

    def emit(track, fraction, label=None, detail=""):
        if prog is not None:
            prog.update(track, fraction, label, detail)

    def fail(message: str, title: str = "J.A.R.V.I.S — Startup Error") -> None:
        if ui is not None:
            ui.abort()               # never leave a topmost overlay over the dialog
        dialog(message, title)

    log = prog.note if prog is not None else _echo
    log(f"J.A.R.V.I.S launcher — project root: {BASE}")

    optimizer = None
    if not (check_only or install_only):
        emit("optimize", 0.0, "INITIALISING OPTIMIZER", "scanning cache and temp")
        optimizer = _start_optimizer(
            progress=(lambda f, label, detail="": emit("optimize", f, label, detail))
            if prog is not None else None)
        if optimizer is None:
            emit("optimize", 1.0, "OPTIMIZER SKIPPED", "another instance is running")
    else:
        emit("optimize", 1.0)

    log("Resolving a Python interpreter with the project dependencies…")
    results = resolve(
        log=log,
        on_progress=(lambda f, label, detail="": emit("resolve", f, label, detail))
        if prog is not None else None)
    chosen = first_complete(results)
    emit("resolve", 1.0, "PYTHON RUNTIME LOCATED" if chosen else "NO COMPLETE RUNTIME FOUND",
         str(chosen.exe) if chosen else "dependencies will be installed")

    if check_only:
        _report(results, chosen, log)
        return 0 if chosen is not None else 1

    # --install exists to make the interpreter JARVIS will use *complete*, not
    # merely able to start: a missing optional engine such as psutil is exactly
    # what the installer is for. (The launch path stays fast and probes no more
    # than it must.)
    need_install = chosen is None
    soft_missing: tuple[str, ...] = ()
    if install_only and chosen is not None:
        soft_missing = tuple(probe_interpreter(chosen.exe, SOFT_IMPORTS).missing)
        need_install = bool(soft_missing)

    if install_only and not need_install:
        log(f"All project dependencies are present for: {chosen.exe}  "
            f"(Python {chosen.version})")
        return 0

    if need_install:
        target = chosen.exe if chosen is not None else install_target(results=results)
        if install_only:
            log(f"Installing project dependencies into: {target}")
            if soft_missing:
                log(f"  missing optional engines: {', '.join(soft_missing)}")
        else:
            log("No interpreter can import the J.A.R.V.I.S dependencies yet.")
            log(f"Installing them into: {target}")
            emit("deps", 0.15, "INSTALLING DEPENDENCIES",
                 "first launch — this can take a few minutes")
            if prog is None and not _has_console():
                # pythonw has no console to show progress in — say so up front
                # rather than appearing to do nothing for several minutes.
                dialog("J.A.R.V.I.S is installing its Python dependencies.\n\n"
                       "This happens once and can take a few minutes.\n"
                       f"Progress is written to:\n{INSTALL_LOG}",
                       "J.A.R.V.I.S — First launch", 0x40)
        install_requirements(target, log=log)
        after = probe_interpreter(target, HARD_IMPORTS)
        if not after.complete:
            missing_packages = ", ".join(after.missing) or "unknown"
            message = (
                "J.A.R.V.I.S could not start: its Python dependencies are missing.\n\n"
                f"Interpreter: {target}\n"
                f"Still missing: {missing_packages}\n\n"
                "Install them manually with:\n"
                f'"{target}" -m pip install -r "{REQUIREMENTS}"\n\n'
                + _install_log_hint()
            )
            if install_only:
                log(message)
            else:
                fail(message)
            return 1
        chosen = after
        log("Dependencies installed successfully.")
    emit("deps", 1.0, "DEPENDENCIES VERIFIED", "")

    if install_only:
        log(f"Dependencies are ready for: {chosen.exe}  (Python {chosen.version})")
        return 0

    _report(results, chosen, log)
    emit("launch", 0.1, "WAITING FOR OPTIMIZER", "finishing cleanup before boot")
    _finish_optimizer(optimizer, log)
    emit("optimize", 1.0)
    emit("launch", 0.5, "HANDING OVER TO MAIN CORE", f"Python {chosen.version}")

    def started() -> None:
        emit("launch", 1.0, "J.A.R.V.I.S ONLINE", "")
        if ui is not None:
            ui.finish()

    return launch(chosen.exe, on_started=started if ui is not None else None)


def main(argv=None) -> int:
    args = list(sys.argv[1:]) if argv is None else [str(a) for a in argv]
    check_only = "--check" in args
    install_only = "--install" in args

    # Re-entrancy guard: this process was started BY the bootstrap, so the
    # interpreter is already known-good. Run the app directly.
    if os.environ.get(ENV_BOOTSTRAPPED) == "1":
        return launch(sys.executable)

    if not ENTRY.is_file():
        dialog(f"J.A.R.V.I.S is incomplete: main.py was not found.\n\n{ENTRY}",
               "J.A.R.V.I.S — Startup Error")
        return 1

    mode = _ui_mode(args, check_only, install_only)
    launch_ui = None
    if mode != "none":
        try:
            if str(BASE) not in sys.path:
                sys.path.insert(0, str(BASE))
            import launch_ui  # type: ignore[no-redef]
        except Exception:
            launch_ui = None
            mode = "none"

    if mode == "splash":
        if not launch_ui.SplashUI.available():
            return _pipeline(args, None, None)
        prog = launch_ui.Progress(_TRACKS)
        ui = launch_ui.SplashUI(prog)
        return ui.run(lambda: _pipeline(args, prog, ui))

    if mode == "console":
        prog = launch_ui.Progress(_TRACKS)
        ui = launch_ui.ConsoleUI(prog)
        ui.start()
        try:
            return _pipeline(args, prog, ui)
        finally:
            ui.abort()               # no-op when finish() already ran

    return _pipeline(args, None, None)


if __name__ == "__main__":
    raise SystemExit(main())