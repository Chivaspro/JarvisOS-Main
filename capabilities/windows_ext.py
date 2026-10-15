"""Window arrangement across one or many monitors.

Windows handles are found through ``capabilities/desktop.py`` (the same pywinauto
enumeration every other window action uses), and the geometry work is done by
the Win32 calls directly — ``ShowWindow`` for minimize/maximize/restore and
``MoveWindow`` for tiling — because pywinauto's own ``maximize`` does not behave
identically across Win32/UWP windows.

Multi-monitor support is real, not cosmetic: ``monitors()`` enumerates the
physical displays with their work areas (taskbar excluded) via
``EnumDisplayMonitors``, and ``move_to_monitor`` uses ``MonitorFromWindow`` to
work out which display a window is on now and where the requested one starts.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

__all__ = ["monitors", "run", "status"]

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOACTIVATE = 0x0010

_MONITOR_DEFAULTTONEAREST = 2


def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "win32", "detail": detail, "evidence": evidence}


def _fail(detail: str, reason: str = "action_failed", **evidence: Any) -> dict:
    return {"ok": False, "controller": "win32", "detail": detail,
            "reason": reason, "evidence": evidence}


def status() -> dict:
    if os.name != "nt":
        return {"available": False, "detail": "window arrangement is Windows-only"}
    return {"available": True, "detail": f"{len(monitors())} display(s) detected"}


# ── monitors ──────────────────────────────────────────────────────────────────

def monitors() -> list[dict]:
    """Physical displays with their work areas (taskbar excluded)."""
    if os.name != "nt":
        return []
    results: list[dict] = []
    try:
        user32 = ctypes.windll.user32
        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(wintypes.RECT), ctypes.c_double)

        def _callback(hmonitor, _hdc, _rect, _data):
            info = wintypes.RECT()
            try:
                MONITORINFO = type("MONITORINFO", (ctypes.Structure,), {
                    "_fields_": [("cbSize", wintypes.DWORD),
                                 ("rcMonitor", wintypes.RECT),
                                 ("rcWork", wintypes.RECT),
                                 ("dwFlags", wintypes.DWORD)]})
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
                    results.append({
                        "index": len(results),
                        "x": int(mi.rcWork.left), "y": int(mi.rcWork.top),
                        "width": int(mi.rcWork.right - mi.rcWork.left),
                        "height": int(mi.rcWork.bottom - mi.rcWork.top),
                        "primary": bool(mi.dwFlags & 1),
                    })
            except Exception:
                pass
            return 1

        user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_callback), 0)
    except Exception:
        return []
    if not results:
        # Fall back to the virtual screen so a single-display machine still works.
        try:
            user32 = ctypes.windll.user32
            results = [{"index": 0, "x": 0, "y": 0,
                        "width": int(user32.GetSystemMetrics(0)),
                        "height": int(user32.GetSystemMetrics(1)),
                        "primary": True}]
        except Exception:
            pass
    return results


def _work_area(index: int | None = None) -> dict:
    areas = monitors()
    if not areas:
        return {"index": 0, "x": 0, "y": 0, "width": 1920, "height": 1080,
                "primary": True}
    if index is None:
        for area in areas:
            if area.get("primary"):
                return area
        return areas[0]
    for area in areas:
        if int(area["index"]) == int(index):
            return area
    return areas[0]


# ── window target ─────────────────────────────────────────────────────────────

def _target(args: dict):
    """``(hwnd, title, error)`` for the window an action should act on."""
    from capabilities import desktop
    win, title = desktop.find_window(str(args.get("app") or ""),
                                     str(args.get("title") or ""))
    if win is None:
        return 0, "", title or "no window matched"
    try:
        hwnd = int(getattr(win, "handle", 0) or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return 0, "", "that window has no usable handle"
    return hwnd, str(title or ""), ""


def _show(hwnd: int, command: int) -> bool:
    try:
        ctypes.windll.user32.ShowWindow(int(hwnd), int(command))
        return True
    except Exception:
        return False


def _move(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    try:
        return bool(ctypes.windll.user32.MoveWindow(int(hwnd), int(x), int(y),
                                                    int(width), int(height), True))
    except Exception:
        return False


# ── handlers ──────────────────────────────────────────────────────────────────

def _simple(command: int, verb: str):
    def _handler(args: dict) -> dict:
        hwnd, title, error = _target(args)
        if not hwnd:
            return _fail(error, "not_found")
        if not _show(hwnd, command):
            return _fail(f"Windows refused to {verb} '{title}'.")
        return _ok(f"{verb.capitalize()}d '{title}'.", window=title)
    return _handler


_h_minimize = _simple(SW_MINIMIZE, "minimize")
_h_maximize = _simple(SW_MAXIMIZE, "maximize")
_h_restore = _simple(SW_RESTORE, "restore")


def _tile(args: dict, horizontal: bool) -> dict:
    hwnd, title, error = _target(args)
    if not hwnd:
        return _fail(error, "not_found")
    monitor_index = args.get("monitor")
    area = _work_area(int(monitor_index) if monitor_index not in (None, "") else None)
    half = int(area["width"] // 2)
    _show(hwnd, SW_RESTORE)
    if horizontal:
        x, y, w, h = area["x"], area["y"], half, int(area["height"])
        side = "left"
    else:
        x, y, w, h = area["x"] + half, area["y"], int(area["width"]) - half, int(area["height"])
        side = "right"
    if not _move(hwnd, x, y, w, h):
        return _fail(f"Windows refused to move '{title}'.")
    return _ok(f"Tiled '{title}' to the {side} half of display "
               f"{area['index'] + 1}.", window=title, rect=[x, y, w, h])


def _h_tile_left(args: dict) -> dict:
    return _tile(args, True)


def _h_tile_right(args: dict) -> dict:
    return _tile(args, False)


def _h_snap_grid(args: dict) -> dict:
    """Snap into a quadrant, or a half when only a side is named."""
    hwnd, title, error = _target(args)
    if not hwnd:
        return _fail(error, "not_found")
    position = str(args.get("position") or args.get("corner") or "top-left").lower()
    position = position.replace("_", "-").replace(" ", "-")
    aliases = {
        "left": "left-half", "right": "right-half",
        "top": "top-half", "bottom": "bottom-half",
        "tl": "top-left", "tr": "top-right", "bl": "bottom-left", "br": "bottom-right",
    }
    position = aliases.get(position, position)
    area = _work_area(int(args["monitor"]) if args.get("monitor") not in (None, "") else None)
    x, y = int(area["x"]), int(area["y"])
    half_w, half_h = int(area["width"] // 2), int(area["height"] // 2)
    rects = {
        "top-left": (x, y, half_w, half_h),
        "top-right": (x + half_w, y, half_w, half_h),
        "bottom-left": (x, y + half_h, half_w, half_h),
        "bottom-right": (x + half_w, y + half_h, half_w, half_h),
        "left-half": (x, y, half_w, int(area["height"])),
        "right-half": (x + half_w, y, half_w, int(area["height"])),
        "top-half": (x, y, int(area["width"]), half_h),
        "bottom-half": (x, y + half_h, int(area["width"]), half_h),
        "full": (x, y, int(area["width"]), int(area["height"])),
    }
    rect = rects.get(position)
    if rect is None:
        return _fail(f"'{position}' is not a snap position. Use one of: "
                     + ", ".join(sorted(rects)) + ".")
    _show(hwnd, SW_RESTORE)
    if not _move(hwnd, *rect):
        return _fail(f"Windows refused to snap '{title}'.")
    return _ok(f"Snapped '{title}' to {position} on display {area['index'] + 1}.",
               window=title, rect=list(rect))


def _h_move_to_monitor(args: dict) -> dict:
    hwnd, title, error = _target(args)
    if not hwnd:
        return _fail(error, "not_found")
    areas = monitors()
    if len(areas) < 2:
        return _fail("Only one display is connected, so there is nowhere to move "
                     "that window.", "single_display")
    target_index = int(args.get("monitor") or 0) - 1
    if str(args.get("monitor") or "").strip().lower() in ("next", "other"):
        current = _current_monitor_index(hwnd, areas)
        target_index = (current + 1) % len(areas)
    if not 0 <= target_index < len(areas):
        return _fail(f"Display {target_index + 1} does not exist — "
                     f"{len(areas)} are connected.")
    area = areas[target_index]
    _show(hwnd, SW_RESTORE)
    if not _move(hwnd, area["x"] + 40, area["y"] + 40,
                 max(400, area["width"] - 80), max(300, area["height"] - 80)):
        return _fail(f"Windows refused to move '{title}'.")
    return _ok(f"Moved '{title}' to display {target_index + 1}.",
               window=title, monitor=target_index + 1)


def _current_monitor_index(hwnd: int, areas: list[dict]) -> int:
    try:
        mon = ctypes.windll.user32.MonitorFromWindow(int(hwnd), _MONITOR_DEFAULTTONEAREST)
        rect = wintypes.RECT()
        MONITORINFO = type("MONITORINFO", (ctypes.Structure,), {
            "_fields_": [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                         ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]})
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            for area in areas:
                if (int(area["x"]) == int(info.rcWork.left)
                        and int(area["y"]) == int(info.rcWork.top)):
                    return int(area["index"])
    except Exception:
        pass
    return 0


def _h_arrange_all(args: dict) -> dict:
    """Tile every visible window in a grid across the chosen display."""
    from capabilities import desktop
    area = _work_area(int(args["monitor"]) - 1 if args.get("monitor") else None)
    try:
        windows = [w for w in desktop.list_windows(visible_only=True)
                   if int(w.get("handle") or 0)]
    except Exception as exc:
        return _fail(f"Could not enumerate the windows: {exc}")
    if not windows:
        return _fail("I found no visible windows to arrange.", "not_found")
    count = len(windows)
    columns = 2 if count > 1 else 1
    rows = (count + columns - 1) // columns
    cell_w = int(area["width"] // columns)
    cell_h = int(area["height"] // rows)
    moved: list[str] = []
    for index, window in enumerate(windows):
        row, column = divmod(index, columns)
        hwnd = int(window["handle"])
        _show(hwnd, SW_RESTORE)
        if _move(hwnd, area["x"] + column * cell_w, area["y"] + row * cell_h,
                 cell_w, cell_h):
            moved.append(str(window.get("title") or hwnd))
    if not moved:
        return _fail("Windows refused every move.")
    return _ok(f"Arranged {len(moved)} window(s) in a {columns}×{rows} grid on "
               f"display {area['index'] + 1}: " + "; ".join(moved[:8]),
               windows=moved)


def _h_list_monitors(args: dict) -> dict:
    areas = monitors()
    if not areas:
        return _fail("I could not read the display configuration.", "unavailable")
    described = "; ".join(
        f"display {area['index'] + 1}: {area['width']}×{area['height']} at "
        f"({area['x']}, {area['y']})" + (" [primary]" if area.get("primary") else "")
        for area in areas)
    return _ok(f"{len(areas)} display(s) — {described}", monitors=areas)


_HANDLERS = {
    "minimize": _h_minimize,
    "maximize": _h_maximize,
    "restore": _h_restore,
    "tile_left": _h_tile_left,
    "tile_right": _h_tile_right,
    "snap_grid": _h_snap_grid,
    "move_to_monitor": _h_move_to_monitor,
    "arrange_all": _h_arrange_all,
    "list_monitors": _h_list_monitors,
}


def run(action_name: str, args: dict) -> dict:
    """Execute one window action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not a window action.")
    try:
        return handler(dict(args or {})) or _fail("No result.")
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")


__all__ = ["monitors", "run", "status"]
