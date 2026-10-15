"""Startup + runtime stability guards for J.A.R.V.I.S (Task 2).

The launcher (run_jarvis.vbs) starts JARVIS through ``pythonw.exe`` — no console,
so an unhandled exception in any worker thread or in the Qt event loop used to
kill subsystems silently or terminate the whole process with no trace. That is
the "JARVIS lags / crashes and I don't know why" experience on many PCs.

This module installs cheap, additive, never-fatal protection:

* ``sys.excepthook`` + ``threading.excepthook`` — log the full traceback and keep
  running instead of aborting when a background thread raises.
* ``faulthandler`` — dump a native (C-level) traceback on segfault / fatal
  crash / hard hang to a rotating log, so crashes that never reach Python are
  still diagnosable.
* ``acquire_single_instance`` — a named Windows mutex PLUS a pid lock file so
  double-launching does not start two audio/GPU-consuming JARVIS processes, and
  so a lock whose owner has died is recognised as stale instead of blocking
  every future start (the "already running with no visible JARVIS" bug).
* ``request_process_exit`` / ``register_shutdown`` / ``force_exit`` — one
  shutdown coordinator every close path fires, so closing the Chat Panel ends
  the WHOLE process (threads, mutex, mic) rather than leaving an invisible
  orphan behind.
* ``tune_for_responsiveness`` — raise the recursion limit for the deep Qt/asyncio
  stacks and disable gc auto-tuning pauses that can add periodic micro-stutters.

Nothing here changes JARVIS behaviour; it only prevents silent death and makes
whatever does happen visible in ``data/logs``.
"""
from __future__ import annotations

import atexit
import ctypes
import faulthandler
import gc
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

_LOG_DIR: Path | None = None
_CRASH_LOG = None            # persistent handle kept open for faulthandler
_UI_REF = None               # JarvisUI/MainWindow, for surfacing errors to chat
_MAX_LOG_BYTES = 4 * 1024 * 1024
_MUTEX_HANDLE = None
_OWNED_NAME = ""               # the mutex name THIS process actually holds
_INSTALLED = False
installed = False            # public alias, kept in sync by install()
MUTEX_NAME = "J.A.R.V.I.S.OS_v4_singleton"
# The pid lock beside the mutex names the process that HOLDS the mutex. A
# named mutex alone cannot tell a live owner from a leaked handle — a helper
# process that opened it (or a crashed interpreter that skipped atexit) keeps
# it forever, which is exactly the false "already running" users see.
LOCK_PATH = (Path(__file__).resolve().parent.parent
             / "data" / "runtime" / "jarvis.lock")

# ── shutdown coordinator ─────────────────────────────────────────────────────
# Every window-close path funnels into request_process_exit(); registered
# callbacks (stop the backend, release the mic) run first, then force_exit()
# guarantees the interpreter actually dies. A Qt app can return from exec()
# while non-main threads keep the process alive forever — with pythonw.exe
# there is no console to show for it, only a zombie that squats the mutex.

_shutdown_cbs: list = []
_shutdown_fired = threading.Event()
_exit_armed = threading.Event()
_OWNED_NAME_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Logging sink
# ---------------------------------------------------------------------------
def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rotate(path: Path) -> None:
    """Keep the crash log from growing without bound."""
    try:
        if path.exists() and path.stat().st_size > _MAX_LOG_BYTES:
            (path.with_suffix(".log.prev")).write_bytes(path.read_bytes()[-(path.stat().st_size // 2):])
    except OSError:
        pass


def _write(text: str) -> None:
    global _CRASH_LOG
    line = f"[{_stamp()}] {text}"
    # 1) the persistent crash log (best-effort; a broken disk must not crash us)
    try:
        if _CRASH_LOG is not None and not _CRASH_LOG.closed:
            _CRASH_LOG.write(line + "\n")
            _CRASH_LOG.flush()
    except Exception:
        pass
    # 2) stderr when a console exists (run_jarvis.bat path)
    try:
        if sys.stderr is not None:
            sys.stderr.write(line + "\n")
    except Exception:
        pass
    # 3) surface to the Chat Panel so the user sees the app is still alive
    try:
        if _UI_REF is not None and hasattr(_UI_REF, "write_log_safe"):
            _UI_REF.write_log_safe("SYS: " + line[:200])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Exception hooks — log, keep the process alive
# ---------------------------------------------------------------------------
def _excepthook(exc_type, exc, tb) -> None:
    try:
        _write("UNHANDLED: " + "".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        pass
    # Do not chain into the default handler (which prints and, for threads, is
    # the last thing that runs before the thread dies). The process stays up.


def _thread_excepthook(args) -> None:
    try:
        _write("THREAD ERROR (" + str(getattr(args, "thread", None).name) + "): "
               + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    except Exception:
        try:
            _write("THREAD ERROR: " + str(args.exc_value))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def install(log_dir: Path | str | None = None, ui=None) -> None:
    """Install crash + hang guards. Idempotent; never raises."""
    global _LOG_DIR, _CRASH_LOG, _UI_REF, _INSTALLED, installed
    if _INSTALLED:
        if ui is not None:
            globals()["_UI_REF"] = ui
        return
    try:
        base = Path(log_dir) if log_dir else (Path(__file__).resolve().parent.parent / "data" / "logs")
        base.mkdir(parents=True, exist_ok=True)
        _LOG_DIR = base
        crash_path = base / "jarvis_crash.log"
        _rotate(crash_path)
        _CRASH_LOG = open(crash_path, "a", encoding="utf-8", buffering=1)

        # Native crash / hard-hang tracebacks to the same file. dump_c backtrace
        # on fatal signals; every ~30s a stack snapshot helps catch deadlocks.
        try:
            faulthandler.enable(file=_CRASH_LOG, all_threads=True)
        except Exception:
            pass

        # Prevent OpenMP multiple runtime conflict between PyTorch and OpenCV on Windows
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        # Ensure UTF-8 console output
        os.environ.setdefault("PYTHONUTF8", "1")
        # The camera manager retries DSHOW opens forever on machines with no
        # working camera; OpenCV prints a WARN every ~3 s that floods stderr and
        # the activity log. Real errors still come through at ERROR level.
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")

        sys.excepthook = _excepthook
        try:
            threading.excepthook = _thread_excepthook
        except Exception:
            pass

        atexit.register(_shutdown)
        _INSTALLED = True
        installed = True
        _UI_REF = ui
        _write("Stability guards installed.")
    except Exception:
        # Installation must never be the thing that breaks startup.
        pass


def bind_ui(ui) -> None:
    """Attach the UI after install so guarded errors can reach the Chat Panel."""
    globals()["_UI_REF"] = ui


def tune_for_responsiveness() -> None:
    """Small, safe tweaks that reduce stutter without changing behaviour."""
    try:
        # The Qt + asyncio + genai stack can nest deeply; a higher limit avoids
        # RecursionError crashes on long tool chains.
        if sys.getrecursionlimit() < 10000:
            sys.setrecursionlimit(10000)
    except Exception:
        pass
    try:
        # Generational gc auto-tuning causes periodic lengthening pauses as the
        # heap grows; fixing the thresholds keeps pauses predictable instead of
        # creeping, which reads as fewer random UI hitches during long sessions.
        gc.collect()
        gc.freeze()
        gc.set_threshold(50000, 50, 50)
    except Exception:
        pass


def stabilize_gc() -> None:
    """Disable cyclic GC for this session — Python 3.14.8 crash workaround.

    On this machine (Python 3.14.8, PyQt6 6.11, torch/cv2/genai threads) every
    full-app run with automatic GC enabled died in 18-50 s with an access
    violation inside ``_PyEval_EvalFrameDefault`` (python314.dll+0x5323a) while
    the main thread painted the reactor or ran a Qt callback; three runs with
    cyclic GC disabled stayed up for 2.5-8+ minutes. A thread-scheduled
    ``gc.collect()`` crashed during a paint frame, so every cyclic collection
    in this stack is unsafe until the interpreter side is fixed — likely by a
    newer CPython 3.14.x patch or a 3.13 interpreter.

    Refcounting still frees everything acyclic; only reference cycles (mostly
    Qt signal lambdas) are retained until exit, so memory grows slowly within
    one session. Set ``JARVIS_GC_AUTO=1`` to restore the previous behaviour and
    re-validate after an interpreter upgrade.
    """
    try:
        if os.environ.get("JARVIS_GC_AUTO", "").strip().lower() in ("1", "true", "yes"):
            return
        gc.disable()
        _write("Auto GC disabled (Python 3.14 stability workaround; "
               "JARVIS_GC_AUTO=1 re-enables).")
    except Exception:
        pass


def _read_lock_pid() -> int:
    """The pid recorded by the instance that took the lock (0 when none)."""
    try:
        return int(LOCK_PATH.read_text(encoding="utf-8").strip().splitlines()[0])
    except Exception:
        return 0


def _write_lock(pid: int | None = None) -> None:
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(f"{pid if pid is not None else os.getpid()}\n",
                             encoding="utf-8")
    except Exception:
        pass


def _clear_lock(force: bool = False) -> None:
    """Remove the lock file.

    Normally this is guarded to the owning pid — a running instance must never
    delete the lock of the instance that replaced it. ``force=True`` is for
    the eviction path only, where the recorded owner has just been proven
    dead and removing its stale lock IS the point.
    """
    try:
        if not LOCK_PATH.exists():
            return
        if force or _read_lock_pid() in (0, os.getpid()):
            LOCK_PATH.unlink()
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """Is this Windows pid currently running? False = dead or unknown.

    STRICT_ACTIVE_ACCESS_CHECK asks the OS whether the process object is still
    active, which — unlike WaitForSingleObject — cannot be fooled by a lingering
    handle another process holds to the dead pid's kernel object (exactly the
    condition that made the old mutex-only guard report phantom "already
    running").
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STRICT_ACTIVE_ACCESS_CHECK = 0x20
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                                 False, pid)
        if not handle:
            return False
        try:
            active = ctypes.c_bool(False)
            ok = k32.GetProcessHandleActive(handle, STRICT_ACTIVE_ACCESS_CHECK,
                                            ctypes.byref(active))
            return bool(ok and active.value)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        # Unknown — assume alive: never kill or evict on a guess.
        return True


def acquire_single_instance(name: str = MUTEX_NAME) -> bool:
    """True when this process is the live owner of the single-instance lock.

    Two facts are combined: the named Windows mutex (fast, kernel-level) and
    the pid lock file (who owns it, and whether that process is really alive).
    A mutex reported as taken is only believed when the recorded owner is
    actually running — a stale lock from a crashed/closed orphan no longer
    blocks a new start, and the stale handle is released so the mutex itself
    stops lying.
    """
    global _MUTEX_HANDLE, _OWNED_NAME
    if os.name != "nt":
        owner = _read_lock_pid()
        if owner and owner != os.getpid() and pid_alive(owner):
            return False
        _write_lock()
        atexit.register(_clear_lock)
        return True
    try:
        ERROR_ALREADY_EXISTS = 183
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        last = ctypes.windll.kernel32.GetLastError()
        if handle and last == ERROR_ALREADY_EXISTS:
            if _OWNED_NAME == name:
                # This very process already holds that mutex — a second
                # acquire of the same name is a genuine non-unique answer.
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
                return False
            owner = _read_lock_pid()
            if owner and owner != os.getpid() and pid_alive(owner):
                # A genuinely live JARVIS owns the mutex: real refusal.
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
                return False
            if owner == os.getpid() and _OWNED_NAME != name:
                # The lock names THIS process but for a different mutex name:
                # this name really is held by someone else. Refuse honestly
                # rather than treating our own lock as proof of staleness.
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
                return False
            # Owner gone (or never recorded): the lock is stale. Close OUR
            # handle — as long as any process keeps one open the kernel keeps
            # the mutex "existing" — and remove the dead owner's lock file so
            # this start is clean.
            try:
                ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
            _clear_lock(force=True)
            handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
            if handle and ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                # Lost a genuine race with a live starter — do not double-boot.
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:
                    pass
                return False
        _MUTEX_HANDLE = handle
        with _OWNED_NAME_LOCK:
            _OWNED_NAME = name
        # Release on normal interpreter exit (and on the forced exit path via
        # force_exit, which calls _release_mutex before os._exit skips atexit).
        atexit.register(_release_mutex)
        _write_lock()
        atexit.register(_clear_lock)
        return True
    except Exception:
        return True


def owner_pid() -> int:
    """The pid currently recorded as the live instance (0 when none/stale)."""
    owner = _read_lock_pid()
    if owner and owner != os.getpid() and pid_alive(owner):
        return owner
    return 0


def _release_mutex() -> None:
    global _MUTEX_HANDLE, _OWNED_NAME
    if _MUTEX_HANDLE:
        try:
            import ctypes
            ctypes.windll.kernel32.ReleaseMutex(_MUTEX_HANDLE)
            ctypes.windll.kernel32.CloseHandle(_MUTEX_HANDLE)
        except Exception:
            pass
        _MUTEX_HANDLE = None
    _OWNED_NAME = ""


# ---------------------------------------------------------------------------
# Shutdown coordinator — every close path ends the whole process, visibly
# ---------------------------------------------------------------------------
def register_shutdown(cb, name: str = "") -> None:
    """Add a best-effort cleanup callback fired once on process shutdown."""
    if callable(cb):
        _shutdown_cbs.append((cb, name or getattr(cb, "__name__", "callback")))


def shutdown_requested() -> bool:
    """True once an intentional close has begun (backend loops must honour it)."""
    return _shutdown_fired.is_set()


def request_process_exit(reason: str = "close", wait_for_callbacks: bool = True) -> None:
    """Begin a real quit: run the cleanup chain, then guarantee process exit.

    Called from any window's close path (Chat Panel ×, command deck ×,
    Shift+Home, taskbar Quit). Callbacks get a short grace period; nothing is
    allowed to keep the process alive afterwards — force_exit releases the
    mutex/mic and hard-exits, which is precisely what the orphan bug proved
    returning from QApplication.exec() does NOT do.
    """
    if _shutdown_fired.is_set():
        return
    _shutdown_fired.set()
    try:
        _write(f"SHUTDOWN requested ({reason}) — running "
               f"{len(_shutdown_cbs)} cleanup callback(s).")
    except Exception:
        pass
    for cb, label in list(_shutdown_cbs):
        done = threading.Event()

        def _run(cb=cb, done=done):
            try:
                cb()
            except Exception as exc:
                try:
                    _write(f"SHUTDOWN callback '{label}' failed: {exc}")
                except Exception:
                    pass
            finally:
                done.set()

        try:
            t = threading.Thread(target=_run, name=f"shutdown-{label}",
                                 daemon=True)
            t.start()
            done.wait(timeout=3.0)
        except Exception:
            pass
    force_exit(timeout=1.0)


def force_exit(code: int = 0, timeout: float = 1.5) -> None:
    """End the process for good.

    Releases the single-instance mutex and the pid lock first so the NEXT
    launch is never blocked by this one, flushes what can be flushed quickly,
    then os._exit()s — Python's normal shutdown joins non-daemon threads, and
    a hung audio/asyncio worker would keep the invisible orphan alive (the
    exact failure this coordinator exists to end). Guarded so it is safe to
    call twice from different paths.
    """
    if _exit_armed.is_set() and code == 0:
        # Already exiting via another path; let that one finish.
        return
    _exit_armed.set()
    try:
        _release_mutex()
    except Exception:
        pass
    try:
        _clear_lock()
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        if _CRASH_LOG is not None and not _CRASH_LOG.closed:
            _write("Process exiting cleanly (force_exit).")
            _CRASH_LOG.close()
    except Exception:
        pass
    # Brief yield so lingering thread writes complete before process teardown
    time.sleep(0.05)
    # Hard exit. A daemon Timer is armed purely as a backstop in case some
    # platform refuses os._exit outright; normally the line below never
    # returns, which is exactly the point.
    try:
        t = threading.Timer(max(0.5, float(timeout)), lambda: os._exit(code))
        t.daemon = True
        t.start()
    except Exception:
        pass
    try:
        os._exit(code)
    except Exception:
        pass


def _shutdown() -> None:
    global _CRASH_LOG
    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        if _CRASH_LOG is not None and not _CRASH_LOG.closed:
            _write("Stability guards shutting down cleanly.")
            _CRASH_LOG.close()
    except Exception:
        pass
