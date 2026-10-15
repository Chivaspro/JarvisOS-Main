"""System settings on this PC: Wi-Fi, volume, brightness, theme and live metrics.

Everything here degrades honestly. Windows will not let a normal process toggle a
network adapter without elevation, so ``wifi_on``/``wifi_off`` attempt the real
call and then *report the refusal* instead of pretending the radio changed.
Volume, mute and brightness are delegated to ``capabilities/power.py`` so there
is exactly one implementation of each (and one place the pycaw fallback lives).
Theme switching is a per-user registry write, so it needs no elevation at all.
"""

from __future__ import annotations

import subprocess
from typing import Any

__all__ = ["run", "status"]

_WIFI_NAMES = ("Wi-Fi", "WiFi", "WLAN", "Wireless Network Connection")

_CREATE_NO_WINDOW = 0x08000000


def _ok(detail: str, controller: str = "windows", **evidence: Any) -> dict:
    return {"ok": True, "controller": controller, "detail": detail, "evidence": evidence}


def _fail(detail: str, reason: str = "action_failed", **evidence: Any) -> dict:
    return {"ok": False, "controller": "windows", "detail": detail,
            "reason": reason, "evidence": evidence}


def _run_command(argv: list[str], timeout: float = 12.0) -> tuple[int, str, str]:
    """Run a Windows CLI tool without flashing a console window."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW if hasattr(subprocess,
                                                                       "CREATE_NO_WINDOW")
                              else 0)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def status() -> dict:
    ok = True
    detail = "netsh available"
    code, _out, err = _run_command(["netsh", "wlan", "show", "interfaces"])
    if code != 0:
        ok = False
        detail = (err or "the wlan interface could not be queried").strip()[:200]
    return {"available": ok, "detail": detail}


# ── Wi-Fi ─────────────────────────────────────────────────────────────────────

def _wifi_interface() -> str:
    code, out, _err = _run_command(["netsh", "wlan", "show", "interfaces"])
    if code == 0:
        for line in out.splitlines():
            if ":" in line and line.strip().lower().startswith(("name", "ssid")):
                key, _, value = line.partition(":")
                if key.strip().lower() == "name" and value.strip():
                    return value.strip()
    return _WIFI_NAMES[0]


def _h_wifi_status(args: dict) -> dict:
    code, out, err = _run_command(["netsh", "wlan", "show", "interfaces"])
    if code != 0:
        return _fail((err or "the wireless interface could not be queried").strip()[:200],
                     "unavailable")
    state = "unknown"
    ssid = ""
    for line in out.splitlines():
        low = line.strip().lower()
        if low.startswith("state") and ":" in line:
            state = line.split(":", 1)[1].strip()
        if low.startswith("ssid") and "bssid" not in low and ":" in line:
            ssid = line.split(":", 1)[1].strip()
    if "connected" in state.lower():
        return _ok(f"Wi-Fi is connected" + (f" to {ssid}." if ssid else "."), ssid=ssid)
    if not out.strip():
        return _fail("This machine reports no wireless interface.", "no_wireless")
    return _ok(f"Wi-Fi is {state or 'not connected'}.")


def _set_wifi(enabled: bool) -> dict:
    interface = _wifi_interface()
    state = "enabled" if enabled else "disabled"
    code, out, err = _run_command(
        ["netsh", "interface", "set", "interface", interface, f"admin={state}"])
    if code != 0:
        detail = (err or out or "").strip()[:200]
        # Windows words this differently depending on the build: "sufficient
        # privileges", "requires elevation", "access is denied", or nothing at
        # all when netsh was denied silently. All of them mean the same thing.
        low = detail.lower()
        if (not detail or "elevat" in low or "administrator" in low
                or "privilege" in low or "access is denied" in low):
            return _fail(
                f"Turning Wi-Fi {state} needs administrator rights, which I do not "
                f"have — nothing was changed. Use the Windows network flyout, or "
                f"grant elevation and ask again.", "needs_elevation")
        return _fail(f"Windows refused that: {detail}", "refused")
    return _ok(f"Wi-Fi {state}.", interface=interface)


def _h_wifi_on(args: dict) -> dict:
    return _set_wifi(True)


def _h_wifi_off(args: dict) -> dict:
    return _set_wifi(False)


# ── volume / brightness (one implementation, in power.py) ─────────────────────

def _delegate(action: str, args: dict) -> dict:
    from capabilities import power
    result = power.run(action, args)
    detail = str(result.get("detail") or "")
    return {"ok": bool(result.get("ok")),
            "controller": str(result.get("controller") or "power"),
            "detail": detail,
            "reason": "" if result.get("ok") else str(result.get("reason") or "action_failed"),
            "evidence": result.get("evidence") or {}}


def _h_set_volume(args: dict) -> dict:
    return _delegate("set_volume", args)


def _h_mute(args: dict) -> dict:
    return _delegate("mute", args)


def _h_set_brightness(args: dict) -> dict:
    return _delegate("set_brightness", args)


# ── theme ─────────────────────────────────────────────────────────────────────

def _set_theme(dark: bool) -> dict:
    try:
        import winreg  # type: ignore
    except Exception as exc:
        return _fail(f"Theme switching needs Windows: {exc}", "unsupported")
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 0 if dark else 1)
    except Exception as exc:
        return _fail(f"Could not write the theme setting: {exc}")
    return _ok(f"Switched the system and app theme to "
               f"{'dark' if dark else 'light'}.")


def _h_set_theme(args: dict) -> dict:
    value = str(args.get("theme") or args.get("mode") or "").strip().lower()
    if value in ("dark", "night"):
        return _set_theme(True)
    if value in ("light", "day"):
        return _set_theme(False)
    if value in ("toggle", "switch", ""):
        current = _current_theme_is_dark()
        return _set_theme(not current)
    return _fail(f"'{value}' is not a theme. Use dark, light or toggle.")


def _current_theme_is_dark() -> bool:
    try:
        import winreg  # type: ignore
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return True


# ── live metrics ──────────────────────────────────────────────────────────────

def _h_system_metrics(args: dict) -> dict:
    try:
        import psutil  # type: ignore
    except Exception as exc:
        return _fail(f"Reading metrics needs psutil: {exc}", "unavailable")
    try:
        cpu = float(psutil.cpu_percent(interval=0.4))
        memory = psutil.virtual_memory()
        battery_text = ""
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                charging = "charging" if battery.power_plugged else "on battery"
                battery_text = f", battery {int(battery.percent)}% ({charging})"
        except Exception:
            battery_text = ""
        detail = (f"CPU {cpu:.0f}%, memory {memory.percent:.0f}% used "
                  f"({memory.available // (1024 * 1024)} MB free){battery_text}.")
        return _ok(detail, cpu=cpu, memory=memory.percent)
    except Exception as exc:
        return _fail(f"Could not read the metrics: {exc}")


_HANDLERS = {
    "wifi_status": _h_wifi_status,
    "wifi_on": _h_wifi_on,
    "wifi_off": _h_wifi_off,
    "set_volume": _h_set_volume,
    "mute": _h_mute,
    "set_brightness": _h_set_brightness,
    "set_theme": _h_set_theme,
    "system_metrics": _h_system_metrics,
}


def run(action_name: str, args: dict) -> dict:
    """Execute one system-settings action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not a system-settings action.")
    try:
        return handler(dict(args or {})) or _fail("No result.")
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")


__all__ = ["run", "status"]
