"""System power and session control — the machine-level actions.

Every other controller acts *inside* the session: pywinauto drives windows,
PyAutoGUI moves the pointer, Playwright drives a page. This one acts on the
machine itself — lock, sleep, hibernate, restart, shut down, sign out, empty the
Recycle Bin, restart the Windows shell, set system volume or screen brightness.

Because those are the actions a user cannot undo from the chat window, the
policy is declared in ``capabilities/registry.py`` rather than here: each
consequential action carries ``gate="confirm"``, and
``capabilities/router.py`` then parks it behind the project's existing on-screen
confirmation banner (``security/confirm.py``). That token is issued by the
interface after a HUMAN press — the model can ask for it but cannot write it, so
no parameter the model sends can bypass the gate. Actions that are instantly
reversible (lock, cancel a pending shutdown, volume, brightness) are not gated,
because asking before locking the screen would only make the assistant slower
than the Start menu.

Execution rules match the rest of ``capabilities/``: every public function
returns a dict, nothing raises, the platform is probed rather than assumed, and
a failure names what happened. Nothing here takes a shell string from the model:
arguments are validated (a percentage is an int in range) and passed as ``argv``
lists, so there is no command-injection surface.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_WIN = sys.platform == "win32"

#: How long an OS command may take before it is reported as timed out.
COMMAND_TIMEOUT_S = 25.0

#: The sleep request goes through .NET instead of ``rundll32 powrprof.dll``,
#: because the rundll32 form hibernates on a machine with hibernation enabled.
_SLEEP_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.Application]::SetSuspendState('Suspend',$false,$false)"
)

#: Clear-RecycleBin fails on an already-empty bin with a message; that state is
#: a success for the user's request, so the script reports it distinctly.
_EMPTY_BIN_SCRIPT = (
    "$ErrorActionPreference='Stop'; "
    "try { Clear-RecycleBin -Force -Confirm:$false; 'cleared' } "
    "catch { if ($_.Exception.Message -match 'empty') { 'already empty' } else { throw } }"
)

#: The brightness WMI method takes (Timeout, Brightness); the value comes from
#: the validated percentage below, never from the model's text.
_BRIGHTNESS_TEMPLATE = (
    "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods; "
    "if (-not $m) { Write-Error 'no brightness-capable display'; exit 2 }; "
    "$m | Invoke-CimMethod -MethodName WmiSetBrightness -Arguments "
    "@{Timeout=1; Brightness=@@PERCENT@@} | Out-Null; 'set'"
)


def _no_window() -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WIN else 0
    return {"creationflags": flags} if flags else {}


def _fail(reason: str, detail: str, **evidence) -> dict:
    return {"ok": False, "controller": "power", "reason": reason,
            "detail": str(detail), "evidence": dict(evidence)}


def _done(detail: str, **evidence) -> dict:
    return {"ok": True, "controller": "power", "reason": "", "detail": str(detail),
            "evidence": dict(evidence)}


def _unsupported() -> dict:
    return _fail("unsupported_platform",
                 "System power control is a Windows feature; this machine is "
                 f"{sys.platform}, so nothing was done.")


def _run(argv: list[str], timeout: float = COMMAND_TIMEOUT_S) -> tuple[bool, str]:
    """Run one OS command and report whether it succeeded and what it said."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=timeout, **_no_window())
    except FileNotFoundError:
        return False, f"{argv[0]} was not found on this machine"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} did not finish within {timeout:.0f}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = " ".join(str(proc.stdout or proc.stderr or "").split())
    if proc.returncode != 0 and not output:
        output = f"{argv[0]} exited with code {proc.returncode}"
    return proc.returncode == 0, output[:300]


def _powershell(script: str, timeout: float = COMMAND_TIMEOUT_S) -> tuple[bool, str]:
    """Run one inline PowerShell script (Windows only)."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return False, "PowerShell was not found on this machine"
    return _run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-Command", script], timeout=timeout)


def _percent(args: dict, action: str) -> int | None:
    """One validated 0-100 percentage, or None when the model sent nonsense."""
    raw = args.get("percent", args.get("amount"))
    try:
        value = int(float(str(raw).strip().rstrip("%")))
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None


# ── individual actions ────────────────────────────────────────────────────────

def _h_lock(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _run(["rundll32.exe", "user32.dll,LockWorkStation"])
    if not ok:
        return _fail("lock_failed", f"The workstation could not be locked: {detail}")
    return _done("Locked this PC. Enter your PIN or password to come back in.")


def _h_sleep(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _powershell(_SLEEP_SCRIPT)
    if not ok:
        return _fail("sleep_failed", f"Windows refused the sleep request: {detail}")
    return _done("Put this PC to sleep. Move the mouse or press a key to wake it.")


def _h_hibernate(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _run(["shutdown.exe", "/h"])
    if not ok:
        return _fail("hibernate_failed",
                     f"Windows refused to hibernate (hibernation may be disabled "
                     f"in power settings): {detail}")
    return _done("Hibernating this PC. Nothing was saved from this session.")


def _h_shutdown(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _run(["shutdown.exe", "/s", "/t", "0"])
    if not ok:
        return _fail("shutdown_failed", f"Windows refused to shut down: {detail}")
    return _done("Shutting this PC down now.")


def _h_restart(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _run(["shutdown.exe", "/r", "/t", "0"])
    if not ok:
        return _fail("restart_failed", f"Windows refused to restart: {detail}")
    return _done("Restarting this PC now.")


def _h_sign_out(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _run(["shutdown.exe", "/l"])
    if not ok:
        return _fail("sign_out_failed", f"Windows refused to sign out: {detail}")
    return _done("Signing the current user out. Unsaved work will be lost.")


def _h_cancel_shutdown(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _run(["shutdown.exe", "/a"])
    if not ok:
        return _fail("cancel_failed",
                     f"There was no pending shutdown to cancel: {detail}")
    return _done("Cancelled the pending shutdown or restart.")


def _h_empty_recycle_bin(_args: dict) -> dict:
    if not _WIN:
        return _unsupported()
    ok, detail = _powershell(_EMPTY_BIN_SCRIPT, timeout=90.0)
    if not ok:
        return _fail("recycle_bin_failed",
                     f"The Recycle Bin could not be emptied: {detail}")
    if "already empty" in detail.lower():
        return _done("The Recycle Bin was already empty.")
    return _done("Emptied the Recycle Bin. Everything in it is now permanently gone.")


def _h_restart_explorer(_args: dict) -> dict:
    """Restart the Windows shell so the taskbar and desktop reload."""
    if not _WIN:
        return _unsupported()
    _run(["taskkill.exe", "/f", "/im", "explorer.exe"])
    try:
        subprocess.Popen(["explorer.exe"], close_fds=True, **_no_window())
    except Exception as exc:
        return _fail("explorer_restart_failed",
                     f"Explorer was stopped but could not be started again: {exc}")
    return _done("Restarted Windows Explorer (the taskbar and desktop reload).")


def _h_volume_up(_args: dict) -> dict:
    return _media_key("volume_up")


def _h_volume_down(_args: dict) -> dict:
    return _media_key("volume_down")


def _h_mute(_args: dict) -> dict:
    return _media_key("mute")


def _media_key(action: str) -> dict:
    """System volume and mute live in the media keys — reuse that one path."""
    if not _WIN:
        return _unsupported()
    try:
        from capabilities import media
        result = media.transport(action)
    except Exception as exc:
        return _fail("volume_failed", f"The volume key could not be sent: {exc}")
    if not result.get("ok"):
        return _fail("volume_failed", str(result.get("detail") or "The volume key failed."))
    return _done(str(result.get("detail") or "Volume changed."), action=action)


def _h_set_volume(args: dict) -> dict:
    level = _percent(args, "set_volume")
    if level is None:
        return _fail("bad_percent",
                     "set_volume needs a percentage between 0 and 100.")
    if not _WIN:
        return _unsupported()
    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
        devices = AudioUtilities.GetSpeakers()
        volume = devices.EndpointVolume
        volume.SetMasterVolumeLevelScalar(level / 100.0, None)
        return _done(f"Set the system volume to {level}%.", percent=level)
    except Exception as exc:
        return _fail("set_volume_unavailable",
                     "Setting an exact volume level needs the optional pycaw "
                     f"package, which is not usable here ({type(exc).__name__}). "
                     "I can raise, lower or mute the volume instead.")


def _h_set_brightness(args: dict) -> dict:
    level = _percent(args, "set_brightness")
    if level is None:
        return _fail("bad_percent",
                     "set_brightness needs a percentage between 0 and 100.")
    if not _WIN:
        return _unsupported()
    script = _BRIGHTNESS_TEMPLATE.replace("@@PERCENT@@", str(level))
    ok, detail = _powershell(script)
    if not ok:
        return _fail("brightness_failed",
                     f"The screen brightness could not be set ({detail}). This "
                     "machine may have no built-in display brightness control.")
    return _done(f"Set the screen brightness to {level}%.", percent=level)


_HANDLERS = {
    "lock": _h_lock,
    "sleep": _h_sleep,
    "hibernate": _h_hibernate,
    "shutdown": _h_shutdown,
    "restart": _h_restart,
    "sign_out": _h_sign_out,
    "cancel_shutdown": _h_cancel_shutdown,
    "empty_recycle_bin": _h_empty_recycle_bin,
    "restart_explorer": _h_restart_explorer,
    "volume_up": _h_volume_up,
    "volume_down": _h_volume_down,
    "mute": _h_mute,
    "set_volume": _h_set_volume,
    "set_brightness": _h_set_brightness,
}


def status() -> dict:
    """What this controller can really do here (probed, never assumed)."""
    shell = bool(shutil.which("powershell") or shutil.which("pwsh"))
    available = bool(_WIN)
    detail = ("System power control is ready: lock, sleep, hibernate, restart, "
              "shut down, sign out, cancel a pending shutdown, empty the Recycle "
              "Bin, restart Explorer, and volume/brightness control."
              if available and shell else
              "System power control is ready, but PowerShell was not found, so "
              "sleep, the Recycle Bin and brightness are unavailable."
              if available else
              "System power control is unavailable on this platform "
              f"({sys.platform}).")
    return {"available": available, "platform": sys.platform, "powershell": shell,
            "actions": sorted(_HANDLERS), "detail": detail}


def run(action_name: str, args: dict) -> dict:
    """Execute one power action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        known = ", ".join(sorted(_HANDLERS))
        return _fail("unknown_power_action",
                     f"'{action_name}' is not a system action I support ({known}).")
    try:
        return handler(dict(args or {})) or _fail("power_failed", "No result.")
    except Exception as exc:
        return _fail("power_failed", f"{type(exc).__name__}: {exc}")


__all__ = ["COMMAND_TIMEOUT_S", "run", "status"]
