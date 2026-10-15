"""J.A.R.V.I.S launch UI — progress model + two renderers.

* ``ConsoleUI``  — used by run_jarvis.bat: ANSI banner, gradient progress bar,
                   percentage, "decoding" status text and a scrolling log feed.
* ``SplashUI``   — used by run_jarvis.vbs: a borderless, fully transparent,
                   text-only overlay (no window background, no buttons) that shows
                   what is being optimized, a percentage and a hacking-style log.

Both draw from the same thread-safe ``Progress`` model that start_jarvis.py and
core/startup_optimizer.py report real work into. The percentage is never
invented: it is the weighted sum of the tracks' reported fractions, and it only
ever moves forward.

STDLIB ONLY — this runs inside start_jarvis.py, which must work on a bare
interpreter. ``tkinter`` is imported lazily and its absence just means "no
overlay"; every public method degrades to a no-op instead of raising.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

__all__ = ["Progress", "ConsoleUI", "SplashUI", "decode", "bar_text"]

# ── palette (R, G, B) ────────────────────────────────────────────────────────
CYAN = (0, 229, 255)
DEEP = (0, 70, 130)
WHITE = (200, 250, 255)
GREEN = (57, 255, 182)
AMBER = (255, 196, 64)
RED = (255, 84, 96)
FAINT = (70, 120, 140)

_GLYPHS = "01#$%&@?<>/\\|[]{}=+*ABCDEF0123456789"


# ── shared model ─────────────────────────────────────────────────────────────

class Progress:
    """Weighted multi-track progress. Thread-safe; percent is monotonic."""

    LOG_KEEP = 8

    def __init__(self, tracks: dict[str, float]):
        total = float(sum(tracks.values())) or 1.0
        self._weights = {k: float(v) / total for k, v in tracks.items()}
        self._frac = {k: 0.0 for k in tracks}
        self._lock = threading.Lock()
        self._label = "INITIALISING"
        self._detail = ""
        self._label_at = time.monotonic()
        self._shown = 0.0
        self._log: list[str] = []
        self.started = time.monotonic()

    def update(self, track: str, fraction: float, label: str | None = None,
               detail: str = "") -> None:
        with self._lock:
            if track in self._frac:
                self._frac[track] = max(self._frac[track], max(0.0, min(1.0, float(fraction))))
            if label and label != self._label:
                self._label = str(label)
                self._label_at = time.monotonic()
                self._push(self._label, detail)
            if detail or label:
                self._detail = str(detail or "")

    def note(self, text: str) -> None:
        """A free-form line for the log feed (does not move the bar)."""
        text = " ".join(str(text or "").split())
        if text:
            with self._lock:
                self._push(text, "")

    def _push(self, label: str, detail: str) -> None:
        addr = "0x%04X" % random.randrange(0x1000, 0xFFFF)
        line = f"[{addr}] {label}"
        if detail:
            line += f"  ::  {detail}"
        self._log.append(line)
        del self._log[:-self.LOG_KEEP]

    @property
    def percent(self) -> float:
        with self._lock:
            raw = sum(self._weights[k] * self._frac[k] for k in self._weights) * 100.0
            self._shown = max(self._shown, min(100.0, raw))
            return self._shown

    def finish(self) -> None:
        with self._lock:
            for k in self._frac:
                self._frac[k] = 1.0

    def snapshot(self) -> dict:
        pct = self.percent
        with self._lock:
            return {"percent": pct, "label": self._label, "detail": self._detail,
                    "label_age": time.monotonic() - self._label_at,
                    "log": list(self._log), "elapsed": time.monotonic() - self.started}


# ── text helpers shared by both renderers ────────────────────────────────────

def decode(text: str, age: float, duration: float = 0.55) -> str:
    """'Hacking' reveal: unrevealed characters shimmer as random glyphs."""
    text = str(text)
    if age >= duration:
        return text
    shown = int(len(text) * max(0.0, age) / duration)
    out = []
    for i, ch in enumerate(text):
        out.append(ch if (i < shown or ch == " ") else random.choice(_GLYPHS))
    return "".join(out)


def bar_text(percent: float, width: int = 40, fill: str = "█", empty: str = "░") -> str:
    n = int(round(width * max(0.0, min(100.0, percent)) / 100.0))
    return fill * n + empty * (width - n)


def _lerp(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _fg(rgb) -> str:
    return "\x1b[38;2;%d;%d;%dm" % rgb


_RESET = "\x1b[0m"


def _enable_vt() -> bool:
    """Turn on ANSI/VT processing for the Windows console. True when usable."""
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


# ── console renderer (run_jarvis.bat) ────────────────────────────────────────

_BANNER = (
    "     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗",
    "     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝",
    "     ██║███████║██████╔╝██║   ██║██║███████╗",
    "██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║",
    "╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║",
    " ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝",
)
_SPIN = "◢◣◤◥"


class ConsoleUI:
    """Animated launch screen for a real console window."""

    def __init__(self, prog: Progress, out=None):
        self.prog = prog
        self.out = out or sys.stdout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drawn = 0
        self._final = ""
        self._ended = False
        try:
            tty = bool(self.out.isatty())
        except Exception:
            tty = False
        self.color = tty and _enable_vt()
        self._last_plain = ""

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self.out is None:
            return
        if self.color:
            self._write("\x1b[?25l")                       # hide cursor
            self._write("\n")
            for i, row in enumerate(_BANNER):
                t = i / max(1, len(_BANNER) - 1)
                self._write(" " + _fg(_lerp(CYAN, DEEP, t * 0.85)) + row + _RESET + "\n")
            self._write(" " + _fg(FAINT) + "  S Y S T E M   O P T I M I Z E R   //   "
                        "L A U N C H   S E Q U E N C E" + _RESET + "\n\n")
        else:
            self._write("J.A.R.V.I.S :: launch sequence\n")
        self._thread = threading.Thread(target=self._loop, name="launch-ui", daemon=True)
        self._thread.start()

    def finish(self, message: str = "J.A.R.V.I.S ONLINE") -> None:
        """Final 100% frame, then release the console to the running app."""
        if self._ended:
            return
        self._ended = True
        self._final = message
        self.prog.finish()
        self._halt()
        if self.color:
            self._frame()
            self._write("\x1b[?25h")
        else:
            self._write(f"[100%] {message}\n")

    def abort(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._halt()
        if self.color:
            self._write("\x1b[?25h\n")

    def _halt(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    # -- drawing ---------------------------------------------------------------
    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.color:
                    self._frame()
                else:
                    self._plain()
            except Exception:
                pass
            self._stop.wait(0.06)

    def _plain(self) -> None:
        s = self.prog.snapshot()
        line = f"[{s['percent']:3.0f}%] {s['label']}"
        if line != self._last_plain:
            self._last_plain = line
            self._write(line + "\n")

    def _cols(self) -> int:
        try:
            import shutil

            return max(60, shutil.get_terminal_size((100, 30)).columns - 2)
        except Exception:
            return 98

    def _frame(self) -> None:
        s = self.prog.snapshot()
        cols = self._cols()
        pct = s["percent"]
        tick = int(time.monotonic() * 12)
        lines: list[str] = []

        spin = "●" if self._ended else _SPIN[tick % 4]
        title = f" {spin} " + ("COMPLETE" if self._ended else "WORKING")
        lines.append(_fg(CYAN) + title + _RESET + _fg(FAINT)
                     + f"   T+{s['elapsed']:05.1f}s" + _RESET)

        label = self._final if self._ended else s["label"]
        txt = (label.upper() if self._ended else decode(label.upper(), s["label_age"]))[: cols - 6]
        cursor = "▌" if (tick // 4) % 2 == 0 and not self._ended else " "
        lines.append(" " + _fg(GREEN) + "❯ " + _fg(WHITE) + txt + _fg(GREEN) + cursor + _RESET)

        width = max(20, min(46, cols - 18))
        filled = int(round(width * pct / 100.0))
        cells = []
        for i in range(width):
            if i < filled:
                head = (i == filled - 1) and pct < 100
                cells.append(_fg(WHITE if head else _lerp(DEEP, CYAN, i / max(1, width - 1)))
                             + "█")
            else:
                cells.append(_fg((18, 52, 70)) + "░")
        lines.append(" " + _fg(FAINT) + "[" + "".join(cells) + _fg(FAINT) + "] "
                     + _RESET + _fg(WHITE) + f"\x1b[1m{pct:5.1f}%" + _RESET)

        detail = (s["detail"] or "")[: cols - 6]
        lines.append(" " + _fg(FAINT) + "  " + detail + _RESET)
        lines.append("")

        feed = s["log"][-self.prog.LOG_KEEP:]
        feed = [""] * (self.prog.LOG_KEEP - len(feed)) + feed
        for i, raw in enumerate(feed):
            t = (i + 1) / len(feed)                      # newest line is brightest
            shade = _lerp((22, 60, 76), GREEN if i == len(feed) - 1 else CYAN, t)
            lines.append(" " + _fg(shade) + raw[: cols - 4] + _RESET)

        out = []
        if self._drawn:
            out.append("\x1b[%dA" % self._drawn)
        for ln in lines:
            out.append("\x1b[2K" + ln + "\n")
        self._drawn = len(lines)
        self._write("".join(out))


# ── transparent text-only overlay (run_jarvis.vbs) ───────────────────────────

class SplashUI:
    """Borderless, transparent window that draws only text.

    ``run(worker)`` owns the main thread (Tk requires it), runs *worker* on a
    thread and returns its result once the worker is finished and the overlay
    has faded away.
    """

    KEY = "#010203"          # colour key made fully transparent by Windows
    FONT = "Consolas"

    def __init__(self, prog: Progress):
        self.prog = prog
        self._closing_at: float | None = None
        self._abort = False
        self._active = False

    @staticmethod
    def available() -> bool:
        try:
            import tkinter  # noqa: F401
            return True
        except Exception:
            return False

    # -- called from the worker thread -----------------------------------------
    def finish(self, message: str = "J.A.R.V.I.S ONLINE") -> None:
        self.prog.update("launch", 1.0, message)
        self.prog.finish()
        if self._closing_at is None:
            self._closing_at = time.monotonic() + 0.9      # hold 100%, then fade

    def abort(self) -> None:
        self._abort = True

    # -- main thread -------------------------------------------------------------
    def run(self, worker):
        box = {"rc": 1, "exc": None}

        def target():
            try:
                box["rc"] = worker()
            except BaseException as exc:  # re-raised on the main thread
                box["exc"] = exc
            finally:
                if self._closing_at is None:
                    self._closing_at = time.monotonic()    # worker ended early

        root = None
        try:
            import tkinter as tk
            root = tk.Tk()
            self._build(root, tk)
        except Exception:
            try:
                if root is not None:
                    root.destroy()
            except Exception:
                pass
            return worker()          # no display / no tkinter: run without UI

        thread = threading.Thread(target=target, name="launch-worker", daemon=False)
        self._active = True
        thread.start()
        try:
            root.mainloop()
        except Exception:
            pass
        finally:
            self._active = False
            try:
                root.destroy()
            except Exception:
                pass
        thread.join()
        if box["exc"] is not None:
            raise box["exc"]
        return box["rc"]

    def _build(self, root, tk) -> None:
        self.root = root
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        self.k = max(1.0, dpi / 96.0)
        root.title("J.A.R.V.I.S")
        root.overrideredirect(True)
        root.configure(bg=self.KEY)
        for attr, val in (("-topmost", True), ("-transparentcolor", self.KEY), ("-alpha", 1.0)):
            try:
                root.attributes(attr, val)
            except Exception:
                pass
        self.w, self.h = int(780 * self.k), int(330 * self.k)
        x = (root.winfo_screenwidth() - self.w) // 2
        y = (root.winfo_screenheight() - self.h) // 2
        root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=self.KEY,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._hex = ""
        self._frame_no = 0
        self._tick()

    @staticmethod
    def _hexcol(rgb) -> str:
        return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)

    def _text(self, x, y, text, rgb, size, bold=False, anchor="nw", glow=True):
        font = (self.FONT, size, "bold" if bold else "normal")
        c = self.canvas
        if glow:
            g = self._hexcol(_lerp((0, 0, 0), rgb, 0.38))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                c.create_text(x + dx, y + dy, text=text, fill=g, font=font, anchor=anchor)
        c.create_text(x, y, text=text, fill=self._hexcol(rgb), font=font, anchor=anchor)

    def _tick(self) -> None:
        try:
            now = time.monotonic()
            if self._abort:
                self.root.quit()
                return
            alpha = 1.0
            if self._closing_at is not None and now >= self._closing_at:
                alpha = 1.0 - (now - self._closing_at) / 0.5
                if alpha <= 0.0:
                    self.root.quit()
                    return
                try:
                    self.root.attributes("-alpha", alpha)
                except Exception:
                    pass
            self._draw()
            self._frame_no += 1
            self.root.after(45, self._tick)
        except Exception:
            try:
                self.root.quit()
            except Exception:
                pass

    def _draw(self) -> None:
        k = self.k
        s = self.prog.snapshot()
        done = self._closing_at is not None
        c = self.canvas
        c.delete("all")
        x = int(18 * k)
        tick = self._frame_no

        self._text(x, int(10 * k), "J.A.R.V.I.S", CYAN, 20, bold=True)
        self._text(x + int(200 * k), int(17 * k), "//  SYSTEM OPTIMIZER", FAINT, 11, glow=False)
        self._text(self.w - x, int(17 * k), time.strftime("%H:%M:%S"), FAINT, 11,
                   anchor="ne", glow=False)

        label = "J.A.R.V.I.S ONLINE" if done else s["label"]
        txt = label.upper() if done else decode(label.upper(), s["label_age"])
        cursor = "_" if (tick // 8) % 2 == 0 and not done else " "
        self._text(x, int(58 * k), "> " + txt + cursor, GREEN if not done else WHITE, 13, bold=True)

        pct = s["percent"]
        self._text(x, int(96 * k), "[" + bar_text(pct, 38) + "]", CYAN, 15, bold=True)
        self._text(self.w - x, int(90 * k), f"{pct:5.1f}%", WHITE, 24, bold=True, anchor="ne")

        self._text(x, int(136 * k), (s["detail"] or "")[:84], FAINT, 10, glow=False)

        if tick % 3 == 0 or not self._hex:
            self._hex = " ".join("%04X" % random.randrange(0x10000) for _ in range(14))
        self._text(x, int(160 * k), self._hex, (24, 66, 84), 9, glow=False)

        feed = s["log"][-6:]
        for i, line in enumerate(feed):
            t = (i + 1) / max(1, len(feed))
            shade = _lerp((26, 78, 96), GREEN if i == len(feed) - 1 else CYAN, t)
            self._text(x, int((188 + i * 22) * k), line[:92], shade, 10, glow=False)
