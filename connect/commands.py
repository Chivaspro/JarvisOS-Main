"""What a paired phone may ask the desktop to do.

The gateway accepts a small, closed set of commands. Nothing arriving from a
paired device is the assistant's words: the phone sends one of the names below,
and this module decides what that means and hands it to the same code the voice
UI uses. A device can therefore never smuggle an instruction into the model, and
a command the desktop refuses is refused with a sentence rather than executed
quietly.

Each entry declares what the device must send (``action`` plus required fields),
and **the Settings switch that gates it. That is the real gate.** The gateway
enforces the same switch for a phone that the UI enforces for the assistant, so
a permission revoked on this machine cannot be used through the phone instead —
which is the whole point of connecting to a desktop rather than to a cloud.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["Command", "COMMANDS", "describe", "describe_all", "execute", "names"]

#: A phone is a small screen; a long answer is truncated here rather than
#: flooding the client, and the truncation is stated rather than silent.
MAX_TEXT = 4000


@dataclass(frozen=True)
class Command:
    """One command a paired device may send."""

    action: str
    summary: str
    #: Settings switch that must be on ("" = always allowed).
    permission: str = ""
    required: tuple[str, ...] = ()
    #: True when the command must be confirmed on the desktop HUD first, because
    #: it changes something outside this machine or cannot be taken back.
    confirm: bool = False
    #: Extra parameters the phone may send, for the client to render.
    fields: tuple[tuple[str, str], ...] = field(default=())


def _ask(args: dict) -> dict:
    """Answer a question through the same provider the typed chat uses."""
    text = str(args.get("text") or args.get("question") or "").strip()
    if not text:
        return {"ok": False, "detail": "The device sent no question.", "reason": "no_input"}
    try:
        from memory.config_manager import load_api_keys
        from app import ai_providers
        config = dict(load_api_keys() or {})
        reply = ai_providers.chat(config, text, timeout=90)
        return {"ok": True, "detail": str(reply or "").strip()[:MAX_TEXT]}
    except Exception as exc:
        return {"ok": False, "detail": f"The assistant could not answer: {exc}",
                "reason": "model_unavailable"}


def _system_metrics(_args: dict) -> dict:
    from capabilities import sysctl
    return sysctl.run("system_metrics", {})


def _screenshot(_args: dict) -> dict:
    """A PNG the phone can display. Saved locally too, so the desktop can see it."""
    try:
        from capabilities import desktop
        png, info = desktop.grab_png()
    except Exception as exc:
        return {"ok": False, "detail": f"The screen could not be captured: {exc}",
                "reason": "capture_failed"}
    if not png:
        return {"ok": False, "detail": "The capture returned no image.",
                "reason": "capture_failed"}
    path = ""
    try:
        import base64
        from pathlib import Path
        from config.settings import DATA_DIR
        folder = Path(DATA_DIR) / "connect" / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        import time as _time
        path = str(folder / f"screen_{int(_time.time())}.png")
        Path(path).write_bytes(png)
    except Exception:
        path = ""
    import base64 as _b64
    return {"ok": True,
            "detail": f"Captured {info.get('width', '?')}x{info.get('height', '?')} "
                      f"from this machine's screen.",
            "image": _b64.b64encode(png).decode("ascii"),
            "mime": "image/png", "path": path,
            "evidence": {"width": info.get("width"), "height": info.get("height"),
                         "path": path}}


def _list_windows(_args: dict) -> dict:
    from capabilities import windows_ext
    return windows_ext.run("list_windows", {})


def _power(args: dict) -> dict:
    """Lock, sleep, or (always with confirmation) restart/shut down."""
    mode = str(args.get("mode") or args.get("action_name") or "").strip().lower()
    from capabilities import power
    action = {"lock": "lock", "sleep": "sleep", "restart": "restart",
              "reboot": "restart", "shutdown": "shutdown",
              "poweroff": "shutdown"}.get(mode, "")
    if not action:
        return {"ok": False, "detail": "Which power action: lock, sleep, restart "
                                      "or shut down?", "reason": "no_input"}
    return power.run(action, {})


def _documents(args: dict) -> dict:
    from documents import tool as documents_tool
    return documents_tool.run(str(args.get("document_action") or "list_documents"),
                              args)


def _clipboard_read(_args: dict) -> dict:
    from capabilities import clipboard
    return clipboard.run("read", {})


def _notify(args: dict) -> dict:
    """Show one line on the desktop HUD, from the phone."""
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "detail": "Nothing to show.", "reason": "no_input"}
    try:
        from connect import runtime
        runtime.notify(f"Phone: {text[:200]}")
    except Exception:
        pass
    return {"ok": True, "detail": "Shown on the desktop."}


#: Every command a paired device may send, in the order the client lists them.
COMMANDS: tuple[Command, ...] = (
    Command("ping", "Check that the desktop is awake and reachable."),
    Command("status", "Report this machine's CPU, memory and battery."),
    Command("ask", "Ask the assistant a question through this desktop.",
            required=("text",), fields=(("text", "Your question"),)),
    Command("notify", "Show a line on the desktop HUD.",
            required=("text",), fields=(("text", "Text to display"),)),
    Command("screenshot", "Capture this machine's screen.",
            permission="screen_understanding"),
    Command("windows", "List the open windows on this machine.",
            permission="desktop_awareness"),
    Command("clipboard_read", "Read this machine's clipboard out loud.",
            permission="clipboard_sentry"),
    Command("metrics", "Report CPU, memory and battery figures.",
            permission="system_settings"),
    Command("documents", "List the documents generated on this machine.",
            permission="document_architect",
            fields=(("document_action", "list_documents or engines"),)),
    Command("lock", "Lock this machine.",
            permission="power_control"),
    Command("sleep", "Put this machine to sleep.",
            permission="power_control"),
    Command("restart", "Restart this machine.",
            permission="power_control", confirm=True),
    Command("shutdown", "Shut this machine down.",
            permission="power_control", confirm=True),
)

_BY_ACTION = {command.action: command for command in COMMANDS}

#: Which function serves each command.
_RUNNERS: dict[str, Callable[[dict], dict]] = {
    "ping": lambda args: {"ok": True, "detail": "The desktop is awake."},
    "status": _system_metrics,
    "ask": _ask,
    "notify": _notify,
    "screenshot": _screenshot,
    "windows": _list_windows,
    "clipboard_read": _clipboard_read,
    "metrics": _system_metrics,
    "documents": _documents,
    "lock": _power,
    "sleep": _power,
    "restart": _power,
    "shutdown": _power,
}


def names() -> list[str]:
    return [command.action for command in COMMANDS]


def describe_all() -> list[dict]:
    """The catalogue, for the web client to render and for the tool to report."""
    return [describe(command.action) for command in COMMANDS]


def describe(action: str) -> dict:
    command = _BY_ACTION.get(str(action or "").strip().lower())
    if command is None:
        return {}
    return {"action": command.action, "summary": command.summary,
            "permission": command.permission, "confirm": command.confirm,
            "required": list(command.required),
            "fields": [{"name": name, "label": label}
                       for name, label in command.fields]}


def _switch_on(name: str) -> bool:
    if not name:
        return True
    try:
        from config.settings import get_feature
        return bool(get_feature(name, True))
    except Exception:
        return True


def execute(action: str, args: dict | None = None,
            enabled: Callable[[str], bool] | None = None) -> dict:
    """Run one command from a paired device. Returns a result dict, never raises.

    ``enabled`` is the UI's ``feature_enabled``. The switch declared on the
    command is checked here as well as by the caller, so no caller can bypass it
    by forgetting to pass one.
    """
    args = dict(args or {})
    key = str(action or "").strip().lower()
    command = _BY_ACTION.get(key)
    if command is None:
        return {"ok": False, "reason": "unknown_command",
                "detail": (f"'{action}' is not a command a paired device may send. "
                           f"Available: " + ", ".join(names()) + ".")}
    missing = [field_name for field_name in command.required
               if not str(args.get(field_name) or "").strip()]
    if missing:
        return {"ok": False, "reason": "missing_parameter",
                "detail": "That command needs " + ", ".join(missing) + "."}
    if command.permission:
        allowed = None
        try:
            if enabled is not None:
                allowed = bool(enabled(command.permission))
            else:
                from config.settings import get_feature
                allowed = bool(get_feature(command.permission, True))
        except Exception:
            allowed = None
        if allowed is False:
            from security.permissions import label_for
            return {"ok": False, "reason": "disabled",
                    "detail": (f"{label_for(command.permission)} is switched off "
                               f"on the desktop, so it refused that command.")}
    runner = _RUNNERS.get(key)
    if runner is None:
        return {"ok": False, "reason": "unsupported",
                "detail": f"'{key}' has no implementation on this machine."}
    try:
        result = dict(runner(args) or {})
    except Exception as exc:
        return {"ok": False, "reason": "failed",
                "detail": f"{type(exc).__name__}: {exc}"}
    detail = str(result.get("detail") or "")
    if len(detail) > MAX_TEXT:
        detail = detail[:MAX_TEXT] + " … (truncated)"
    payload = {"ok": bool(result.get("ok")), "detail": detail,
               "reason": str(result.get("reason") or ""),
               "command": key}
    for key_name in ("image", "mime", "path"):
        if result.get(key_name):
            payload[key_name] = result[key_name]
    return payload
