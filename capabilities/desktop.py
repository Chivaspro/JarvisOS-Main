"""Local PC control: native Windows automation first, physical input second.

Two controllers live here, and nothing else in the project imports either:

* **pywinauto** (``uia`` backend) for applications and native Windows controls —
  launching, closing, focusing, listing windows, and driving a control by name,
  auto-id or control type.
* **pyautogui** for physical mouse and keyboard input, and screen capture.

The controller chain from ``capabilities/registry.py`` decides the order, and
every result names the controller that actually did the work. A window action
falls back to physical input only when the window truly cannot be found — the
fallback is reported, never hidden.

Coordinates are handled explicitly, because two coordinate systems exist on
Windows: the captured monitor's physical pixels (what ``mss`` returns, and what
the visual layer grounds into) and PyAutoGUI's coordinates (virtualised when the
process is not DPI-aware). :func:`screen_info` measures both, and
:func:`to_pointer` converts between them, reporting the scale it applied.

PyAutoGUI's failsafe corner stays **enabled**: yanking the pointer into a corner
aborts the pending input cleanly and is reported as a failed action, never as a
crash. Every function returns a dict and never raises.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

#: Set once, on the first real pyautogui call (never at import time, so
#: importing this module in a headless test run costs nothing).
_PAUSE_S = 0.05

_lock = threading.RLock()
_configured = False
_import_error: dict[str, str] = {}

#: Friendly application names → real executables. Only the names a user actually
#: says are listed; anything else is resolved by the shell or reported missing.
APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "files": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "terminal": "wt.exe",
    "powershell": "powershell.exe",
    "task manager": "taskmgr.exe",
    "taskmgr": "taskmgr.exe",
    "settings": "ms-settings:",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "brave": "brave.exe",
    "opera": "opera.exe",
    "vs code": "code.cmd",
    "vscode": "code.cmd",
    "code": "code.cmd",
    "word": "winword.exe",
    "excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe",
    "vlc": "vlc.exe",
    "spotify": "spotify.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
    "roblox": "robloxplayerbeta.exe",
    "snipping tool": "snippingtool.exe",
    "clock": "ms-clock:",
    "store": "ms-windows-store:",
}


# ── availability ──────────────────────────────────────────────────────────────

def _probe(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def status() -> dict:
    """Which controllers are installed (probed, never assumed)."""
    have = {"pyautogui": _probe("pyautogui"), "pywinauto": _probe("pywinauto"),
            "mss": _probe("mss"), "pillow": _probe("PIL")}
    missing = [name for name, ok in have.items() if not ok]
    detail = ("pyautogui + pywinauto ready" if not missing else
              "missing: " + ", ".join(missing))
    return {**have, "available": have["pyautogui"], "missing": missing,
            "detail": detail}


# ── lazy imports ──────────────────────────────────────────────────────────────

def _pyautogui():
    """The pyautogui module, configured once. Raises RuntimeError when absent."""
    global _configured
    with _lock:
        try:
            import pyautogui
        except Exception as exc:  # pragma: no cover - environment dependent
            _import_error["pyautogui"] = str(exc)
            raise RuntimeError(f"pyautogui is not available: {exc}") from exc
        if not _configured:
            # Failsafe stays ON: the corner aborts pending input instead of
            # leaving the pointer loose. PAUSE keeps input human-paced.
            pyautogui.FAILSAFE = True
            try:
                pyautogui.PAUSE = _PAUSE_S
            except Exception:
                pass
            _configured = True
        return pyautogui


def _desktop(backend: str = "uia"):
    """A pywinauto Desktop. Raises RuntimeError when pywinauto is absent."""
    try:
        from pywinauto import Desktop
    except Exception as exc:
        _import_error["pywinauto"] = str(exc)
        raise RuntimeError(f"pywinauto is not available: {exc}") from exc
    return Desktop(backend=backend)


def _fail(controller: str, detail: str, **evidence) -> dict:
    return {"ok": False, "controller": controller, "detail": detail,
            "evidence": dict(evidence)}


def _done(controller: str, detail: str, **evidence) -> dict:
    return {"ok": True, "controller": controller, "detail": detail,
            "evidence": dict(evidence)}


# ── screen capture and coordinates ────────────────────────────────────────────

def screen_info(capture: bool = True) -> dict:
    """Measure the capture surface and the pointer coordinate system.

    ``scale`` is 1.0 on a DPI-aware process (the normal case: Qt makes the app
    per-monitor DPI aware) and something else when Windows is virtualising
    coordinates for us. The numbers are returned as evidence either way.
    """
    pyat = _pyautogui()
    try:
        vw, vh = pyat.size()
    except Exception as exc:
        raise RuntimeError(f"could not read the screen size: {exc}") from exc

    info = {"pointer_w": int(vw), "pointer_h": int(vh), "monitors": 0,
            "left": 0, "top": 0, "width": int(vw), "height": int(vh),
            "scale_x": 1.0, "scale_y": 1.0, "png": b""}
    if not capture:
        return info

    try:
        import mss
        with mss.mss() as sct:
            monitors = sct.monitors
            info["monitors"] = len(monitors)
            mon = monitors[1] if len(monitors) > 1 else monitors[0]
            shot = sct.grab(mon)
            info.update({"left": int(mon["left"]), "top": int(mon["top"]),
                         "width": int(shot.width), "height": int(shot.height)})
            info["png"] = _to_png(shot)
    except Exception as exc:
        # Capture is optional for input-only actions.
        info["capture_error"] = str(exc)

    if info["width"] and info["pointer_w"]:
        info["scale_x"] = round(info["width"] / float(info["pointer_w"]), 4)
    if info["height"] and info["pointer_h"]:
        info["scale_y"] = round(info["height"] / float(info["pointer_h"]), 4)
    return info


def _to_png(shot) -> bytes:
    """PNG bytes at the monitor's exact pixel size (never resized here)."""
    import mss.tools
    return mss.tools.to_png(shot.rgb, shot.size)


def to_pointer(x, y, info: dict | None = None) -> tuple[int, int]:
    """Convert a point in captured-screen pixels to PyAutoGUI coordinates.

    ``x``/``y`` are absolute physical screen pixels (monitor offset included).
    The captured monitor's origin and any DPI scale are applied here so callers
    only ever work in screenshot space.
    """
    data = info or {}
    left = float(data.get("left") or 0.0)
    top = float(data.get("top") or 0.0)
    px = left + float(x)
    py = top + float(y)
    sx = float(data.get("scale_x") or 1.0) or 1.0
    sy = float(data.get("scale_y") or 1.0) or 1.0
    return int(round(px / sx)), int(round(py / sy))


def grab_png() -> tuple[bytes, dict]:
    """One screen frame plus its measurements (the visual layer's input)."""
    info = screen_info(capture=True)
    png = info.pop("png", b"") or b""
    if not png:
        raise RuntimeError(info.get("capture_error")
                           or "the screen could not be captured")
    return png, info


def save_png(png: bytes, path: str | os.PathLike) -> str:
    """Write PNG bytes to *path* (created parents included). Returns the path."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
    except OSError as exc:
        raise RuntimeError(f"could not write {target}: {exc}") from exc
    return str(target)


# ── window helpers (pywinauto) ────────────────────────────────────────────────

def _norm(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def resolve_executable(app: str) -> str:
    """Resolve a spoken application name to something launchable.

    Order: an existing path → the alias table → PATH. Returns "" when nothing
    matches, so the caller can report the gap instead of launching a guess.
    """
    raw = str(app or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.exists():
        return str(candidate)
    alias = APP_ALIASES.get(_norm(raw))
    if alias:
        if alias.endswith(":"):
            return alias                      # a shell URI (ms-settings:, ms-clock:)
        found = shutil.which(alias)
        base = alias[:-4] if alias.lower().endswith(".exe") else alias
        if not found and base != alias:
            found = shutil.which(base)
        if found:
            return found
        return ""                             # named but not installed
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat", ""):
            found = shutil.which(raw + ext) if ext else shutil.which(raw)
            if found:
                return found
    return shutil.which(raw) or ""


def list_windows(visible_only: bool = True) -> list[dict]:
    """Top-level windows with their titles (real UIA enumeration)."""
    desktop = _desktop()
    out: list[dict] = []
    for win in desktop.windows():
        try:
            if visible_only and not win.is_visible():
                continue
            title = win.window_text() or ""
            if not title.strip():
                continue
            out.append({"title": title, "handle": int(getattr(win, "handle", 0) or 0),
                        "class": getattr(win, "class_name", lambda: "")()})
        except Exception:
            continue
    return out


def find_window(app: str = "", title: str = "") -> tuple[object | None, str]:
    """Find one window by application name and/or title fragment.

    Returns ``(spec, "")`` on success, ``(None, reason)`` otherwise. Matching is
    a case-insensitive containment test on the real window titles.
    """
    want_app = _norm(app)
    want_title = _norm(title) or want_app
    if not want_title:
        return None, "no window name was given"
    desktop = _desktop()
    best: tuple[int, object, str] = (0, None, "")
    for win in desktop.windows():
        try:
            if not win.is_visible():
                continue
            text = win.window_text() or ""
            low = _norm(text)
            if not low:
                continue
            if want_title not in low:
                continue
            # Prefer an exact-ish match, and a window whose class hints at the app.
            score = 2 if low == want_title else 1
            if want_app and want_app in low:
                score += 1
            if score > best[0]:
                best = (score, win, text)
        except Exception:
            continue
    if best[1] is None:
        return None, (f"no open window matched '{title or app}'")
    return best[1], best[2]


# ── action handlers ───────────────────────────────────────────────────────────

def _h_open_application(args: dict) -> dict:
    app = str(args.get("app") or "").strip()
    exe = resolve_executable(app)
    if not exe:
        return _fail("pywinauto",
                     f"I could not find an application called '{app}' on this PC, "
                     f"so nothing was launched.", app=app)
    # pywinauto gives us a real handle to the window it starts.
    try:
        from pywinauto import Application
        proc = Application(backend="uia").start(exe, timeout=20)
        try:
            win = proc.top_window()
            title = win.window_text() or ""
            focused = False
            try:
                win.set_focus()
                focused = True
            except Exception:
                pass
            return _done("pywinauto", f"Launched {app} ({exe})",
                         app=app, exe=exe, window=title, focused=focused,
                         pid=int(getattr(proc, "process", 0) or 0))
        except Exception:
            return _done("pywinauto", f"Launched {app} ({exe})", app=app, exe=exe)
    except Exception as exc:
        # Fall back to the Windows shell, which resolves App Paths too.
        try:
            if os.name == "nt":
                os.startfile(exe)  # noqa: S606 - explicit, requested launch
            else:  # pragma: no cover - Windows-only project
                import subprocess
                subprocess.Popen([exe])
            return _done("shell", f"Launched {app} ({exe}) via the Windows shell",
                         app=app, exe=exe, pywinauto_error=str(exc)[:200])
        except Exception as exc2:
            return _fail("pywinauto", f"Could not launch '{app}': {exc2}", app=app)


def _h_close_application(args: dict) -> dict:
    app, title = str(args.get("app") or ""), str(args.get("title") or "")
    win, why = find_window(app, title)
    if win is None:
        return _fail("pywinauto", f"{why}, so nothing was closed.", app=app, title=title)
    name = ""
    try:
        name = win.window_text() or ""
    except Exception:
        pass
    try:
        win.close()
    except Exception as exc:
        return _fail("pywinauto", f"The window '{name}' would not close: {exc}",
                     window=name)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if not win.exists():
                return _done("pywinauto", f"Closed '{name}'", window=name,
                             verified=True)
        except Exception:
            return _done("pywinauto", f"Closed '{name}'", window=name, verified=True)
        time.sleep(0.25)
    return _done("pywinauto",
                 f"Asked '{name}' to close; it was still open when I checked.",
                 window=name, verified=False)


def _h_focus_window(args: dict) -> dict:
    app, title = str(args.get("app") or ""), str(args.get("title") or "")
    win, why = find_window(app, title)
    if win is None:
        return _fail("pywinauto", f"{why}, so nothing was focused.", app=app, title=title)
    try:
        name = win.window_text() or ""
    except Exception:
        name = title or app
    try:
        try:
            win.restore()
        except Exception:
            pass
        win.set_focus()
    except Exception as exc:
        return _fail("pywinauto", f"Could not focus '{name}': {exc}", window=name)
    return _done("pywinauto", f"Focused '{name}'", window=name)


def _point(args: dict) -> tuple[int, int] | None:
    x, y = args.get("x"), args.get("y")
    if x is None or y is None:
        return None
    try:
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def _h_mouse_move(args: dict) -> dict:
    pyat = _pyautogui()
    point = _point(args)
    if point is None:
        return _fail("pyautogui", "mouse_move needs an x and y position.")
    info = screen_info(capture=False)
    tx, ty = to_pointer(point[0], point[1], info)
    pyat.moveTo(tx, ty, duration=0.15)
    return _done("pyautogui", f"Moved the pointer to ({tx}, {ty})",
                 pointer=(tx, ty), scale=(info["scale_x"], info["scale_y"]))


def _h_click(args: dict, clicks: int = 1, button: str = "left") -> dict:
    pyat = _pyautogui()
    info = screen_info(capture=False)
    point = _point(args)
    kwargs: dict = {"clicks": clicks, "button": button, "interval": 0.08}
    evidence: dict = {"clicks": clicks, "button": button}
    if point is not None:
        tx, ty = to_pointer(point[0], point[1], info)
        kwargs.update({"x": tx, "y": ty})
        evidence["pointer"] = (tx, ty)
        evidence["scale"] = (info["scale_x"], info["scale_y"])
    try:
        pyat.click(**kwargs)
    except Exception as exc:
        return _fail("pyautogui", f"The click was refused: {exc}", **evidence)
    where = evidence.get("pointer") or "the current pointer position"
    label = {1: "Clicked", 2: "Double-clicked"}.get(clicks, "Clicked")
    return _done("pyautogui", f"{label} at {where}", **evidence)


def _h_mouse_scroll(args: dict) -> dict:
    pyat = _pyautogui()
    direction = str(args.get("direction") or "").strip().lower()
    amount = int(args.get("amount") or 3)
    if direction in ("down", "up", "left", "right"):
        pass
    else:
        return _fail("pyautogui",
                     "scroll needs a direction of up, down, left or right.",
                     direction=direction)
    info = screen_info(capture=False)
    point = _point(args)
    kwargs: dict = {}
    if point is not None:
        tx, ty = to_pointer(point[0], point[1], info)
        kwargs.update({"x": tx, "y": ty})
        try:
            pyat.moveTo(tx, ty)
        except Exception:
            pass
    step = max(1, abs(amount))
    if direction == "down":
        step = -step
    try:
        if direction in ("up", "down"):
            pyat.scroll(step * 100, **kwargs)
        else:
            horizontal = step if direction == "right" else -step
            pyat.hscroll(horizontal * 100, **kwargs)
    except Exception as exc:
        return _fail("pyautogui", f"The scroll was refused: {exc}", direction=direction)
    return _done("pyautogui", f"Scrolled {direction}", direction=direction,
                 amount=step)


def _h_drag(args: dict) -> dict:
    pyat = _pyautogui()
    to_point = (args.get("to_x"), args.get("to_y"))
    if to_point[0] is None or to_point[1] is None:
        return _fail("pyautogui", "mouse_drag needs to_x and to_y.")
    info = screen_info(capture=False)
    try:
        ex, ey = to_pointer(to_point[0], to_point[1], info)
        start = _point(args)
        if start is not None:
            sx, sy = to_pointer(start[0], start[1], info)
            pyat.moveTo(sx, sy)
            pyat.dragTo(ex, ey, duration=0.4, button="left")
        else:
            pyat.dragTo(ex, ey, duration=0.4, button="left")
    except Exception as exc:
        return _fail("pyautogui", f"The drag was refused: {exc}")
    return _done("pyautogui", f"Dragged to ({ex}, {ey})", pointer=(ex, ey))


def _paste_text(text: str) -> str:
    """Type text fast and unicode-safely via the clipboard when possible."""
    try:
        import pyperclip
        pyperclip.copy(text)
        pyat = _pyautogui()
        pyat.hotkey("ctrl", "v")
        return "clipboard"
    except Exception:
        pass
    pyat = _pyautogui()
    pyat.write(text, interval=0.02)
    return "keyboard"


def _h_keyboard_type(args: dict) -> dict:
    text = str(args.get("text") or "")
    if not text:
        return _fail("pyautogui", "keyboard_type needs text to type.")
    try:
        method = _paste_text(text)
    except Exception as exc:
        return _fail("pyautogui", f"The text could not be typed: {exc}")
    shown = text if len(text) <= 60 else text[:59] + "…"
    return _done("pyautogui", f"Typed: {shown}", chars=len(text), method=method)


def _normalise_key(key: str) -> str:
    low = str(key or "").strip().lower()
    return {"arrowleft": "left", "arrowright": "right", "arrowup": "up",
            "arrowdown": "down", "space": "space", "esc": "esc",
            "escape": "esc", "return": "enter", "enter": "enter"}.get(low, low)


def _h_keyboard_press(args: dict) -> dict:
    pyat = _pyautogui()
    key = _normalise_key(args.get("key"))
    if not key:
        return _fail("pyautogui", "keyboard_press needs a key.")
    repeat = max(1, int(args.get("amount") or 1))
    try:
        for _ in range(repeat):
            pyat.press(key)
    except Exception as exc:
        return _fail("pyautogui", f"The key press was refused: {exc}", key=key)
    return _done("pyautogui", f"Pressed {key}", key=key, repeat=repeat)


def _h_keyboard_hotkey(args: dict) -> dict:
    pyat = _pyautogui()
    raw = args.get("keys")
    if isinstance(raw, (list, tuple)):
        combo = [str(k).strip() for k in raw if str(k).strip()]
    else:
        combo = [part for part in str(raw or "").replace(",", " ").split() if part]
    if not combo:
        return _fail("pyautogui", "keyboard_hotkey needs a combination, e.g. 'ctrl c'.")
    normalised = [("space" if k.lower() == "space" else _normalise_key(k))
                  for k in combo]
    try:
        pyat.hotkey(*normalised)
    except Exception as exc:
        return _fail("pyautogui", f"The hotkey was refused: {exc}",
                     keys=normalised)
    return _done("pyautogui", "Pressed " + "+".join(normalised), keys=normalised)


def _h_screenshot(args: dict) -> dict:
    png, info = grab_png()
    path = str(args.get("path") or "").strip() or default_artifact_path("screen", ".png")
    saved = save_png(png, path)
    return _done("pyautogui",
                 f"Captured the screen to {saved}",
                 path=saved, width=info["width"], height=info["height"],
                 scale=(info["scale_x"], info["scale_y"]),
                 monitors=info["monitors"], image=saved)


#: action name → handler. Every declared pc_control action appears here.
_HANDLERS = {
    "open_application": _h_open_application,
    "close_application": _h_close_application,
    "focus_window": _h_focus_window,
    "mouse_move": _h_mouse_move,
    "mouse_click": lambda a: _h_click(a),
    "mouse_double_click": lambda a: _h_click(a, clicks=2),
    "mouse_right_click": lambda a: _h_click(a, button="right"),
    "mouse_drag": _h_drag,
    "mouse_scroll": _h_mouse_scroll,
    "keyboard_type": _h_keyboard_type,
    "keyboard_press": _h_keyboard_press,
    "keyboard_hotkey": _h_keyboard_hotkey,
    "screen_screenshot": _h_screenshot,
}


def default_artifact_path(stem: str, suffix: str = ".png") -> str:
    """A timestamped path under the shared automation directory."""
    try:
        from capabilities import config
        base = Path(config.automation_dir()) / ("screenshots" if suffix else "")
    except Exception:
        base = Path(__file__).resolve().parent.parent / "data" / "automation"
    name = time.strftime(f"{stem}-%Y%m%d-%H%M%S")
    return str(base / f"{name}{suffix}")


def run(action_name: str, args: dict) -> dict:
    """Execute one pc_control action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip())
    if handler is None:
        return _fail("desktop", f"'{action_name}' is not a local PC action.")
    try:
        return handler(dict(args or {})) or _fail("desktop", "No result.")
    except RuntimeError as exc:
        return _fail("desktop", str(exc))
    except Exception as exc:
        return _fail("desktop", f"{type(exc).__name__}: {exc}")


def run_physical(action_name: str, args: dict) -> dict:
    """Execute a pointer/keyboard action directly (the visual layer's executor).

    Used when the visual layer has already grounded a coordinate: the click is
    still issued through this single audited module, and the result reports
    ``pyautogui`` as the controller.
    """
    if action_name not in _HANDLERS:
        return _fail("pyautogui", f"'{action_name}' is not a physical action.")
    return run(action_name, args)


__all__ = ["APP_ALIASES", "default_artifact_path", "find_window", "grab_png",
           "list_windows", "resolve_executable", "run", "run_physical",
           "save_png", "screen_info", "status", "to_pointer"]
