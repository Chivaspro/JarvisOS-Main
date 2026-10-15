"""Permission gate — moved verbatim from main.py (task 3.1/3.2).

Twenty Settings switches used to be saved but never enforced until
main.py::_permission_gate made each tool (and each action of multi-purpose
tools) checked against its own switch before any work happens, with refusals
that name the switch. This module is that gate, unmodified in behavior, so
the security service and future subsystems share one implementation.
"""

from __future__ import annotations

from typing import Callable

_TERMINAL_ACTIONS = {"run", "build", "install", "execute", "terminal"}

#: pc_control actions that drive a real Windows window (pywinauto), as opposed
#: to physical mouse/keyboard input (PyAutoGUI).
_WINDOW_ACTIONS = {"open_application", "close_application", "focus_window"}

#: system_control actions that touch processes rather than system settings.
_PROCESS_ACTIONS = {"list_processes", "kill_process", "suspend_process",
                    "resume_process", "set_process_priority"}

#: clipboard_sentry actions that change the clipboard.
_CLIPBOARD_WRITE_ACTIONS = {"write", "clear"}

#: clipboard_sentry actions that send the clipboard to a model.
_CLIPBOARD_MODEL_ACTIONS = {"analyze", "explain", "translate", "format", "fix"}

_PERMISSION_LABEL = {
    "screen_understanding": "Screen capture and visual understanding",
    "desktop_awareness": "Desktop awareness",
    "desktop_control": "Desktop control",
    "windows_automation": "Windows application automation (pywinauto)",
    "playwright_browser": "Browser automation (Playwright)",
    "uitars_visual": "UI-TARS visual control",
    "physical_input": "Mouse and keyboard control (PyAutoGUI)",
    "power_control": "System power control (lock, sleep, restart, shut down)",
    "terminal_control": "Terminal and command execution",
    "file_control": "File control",
    "camera_access": "Camera access",
    "mic_access": "Microphone access",
    "web_task": "Web task automation",
    "webview": "Web research (Agent Reach)",
    "voice_engine_control": "Voice engine control",
    "self_diagnostics": "Self-diagnostics (engine, project, and settings)",
    "coding_agents": "Coding tasks",
    # Deep Windows orchestration + clipboard sentry (Phase 3).
    "process_control": "Process control (freeze, prioritise, end processes)",
    "system_settings": "System settings (Wi-Fi, volume, brightness, theme)",
    "window_control": "Window control (minimize, tile, multi-monitor)",
    "clipboard_sentry": "Clipboard sentry (read and analyse clipboard text)",
    "document_architect": "Document architect (write Word, Excel, PowerPoint and "
                          "PDF files)",
    "smart_home": "Smart home control (local plugs and bulbs)",
    "instagram": "Instagram (read and act on the user's own account)",
    "live_vision": "Live vision (stream the camera to the model while talking)",
    "connect": "JarvisOS Connect (paired phones and browsers reaching this PC)",
    # Distinct from the line above, and easy to confuse with it: this one is
    # accounts on other people's services, not devices on this network.
    "external_apps": "External apps (Spotify, Gmail, GitHub and the rest, "
                     "through the Composio connector)",
}


def label_for(feature: str) -> str:
    """Human label for a permission switch (falls back to the key)."""
    return _PERMISSION_LABEL.get(feature, feature)


def permission_refusal(
    enabled: Callable[[str], bool],
    name: str,
    args: dict | None,
) -> str | None:
    """Refusal sentence when a revoked Settings permission covers this tool.

    `enabled` is ui.feature_enabled (name -> bool). Returns None when the call
    may proceed. Never raises: a broken feature lookup must not block tools.
    """
    def off(feat: str) -> bool:
        try:
            return not bool(enabled(feat))
        except Exception:
            return False

    def refusal(feat: str) -> str:
        return (f"{_PERMISSION_LABEL.get(feat, feat)} is switched off in Settings, "
                f"so I have not done that.")

    act = str((args or {}).get("action", "") or "").strip().lower()

    if name == "system_status":
        return refusal("desktop_awareness") if off("desktop_awareness") else None

    if name in ("file_controller", "file_processor"):
        return refusal("file_control") if off("file_control") else None

    if name in ("code_helper", "dev_agent"):
        # Only the actions that actually execute something are gated; writing
        # and explaining code is not terminal access.
        if act in _TERMINAL_ACTIONS and off("terminal_control"):
            return refusal("terminal_control")
        return None

    if name == "screen_process":
        return refusal("screen_understanding") if off("screen_understanding") else None

    if name in ("vision_look", "recognize_faces", "what_am_i_holding",
                "detect_objects", "detect_gesture"):
        return refusal("camera_access") if off("camera_access") else None

    if name == "voice_settings":
        # Reporting the current engine (mode=status) is read-only; only an
        # actual switch of the voice engine/provider needs the permission.
        mode = str((args or {}).get("mode", "") or act or "").strip().lower()
        if mode in ("set", "switch", "change") and off("voice_engine_control"):
            return refusal("voice_engine_control")
        return None

    if name == "diagnostics":
        if off("self_diagnostics"):
            return refusal("self_diagnostics")
        # mode='install' actually runs pip — that is command execution, so it
        # additionally needs the terminal permission (Universal Control
        # Protocol: dependency install happens "when authorized and safe").
        mode = str((args or {}).get("mode", "") or "").strip().lower()
        if mode == "install" and off("terminal_control"):
            return refusal("terminal_control")
        return None

    # ── control capabilities ──────────────────────────────────────────────────
    # Each of the three engines has its own switch, so revoking desktop control
    # cannot silently leave browser automation (or research) acting instead.
    if name == "computer_control":
        return refusal("desktop_control") if off("desktop_control") else None

    if name == "browser_control":
        return refusal("web_task") if off("web_task") else None

    if name == "web_research":
        return refusal("webview") if off("webview") else None

    # ── local controllers ─────────────────────────────────────────────────────
    # One switch per engine relationship, plus the master desktop switch. The
    # refusal always names the exact switch that stopped the action, so the user
    # never has to guess which one to re-enable.
    if name == "pc_control":
        if off("desktop_control"):
            return refusal("desktop_control")
        if act in _WINDOW_ACTIONS and off("windows_automation"):
            return refusal("windows_automation")
        if act == "screen_screenshot" and off("screen_understanding"):
            return refusal("screen_understanding")
        if act and act not in _WINDOW_ACTIONS and act != "screen_screenshot":
            if off("physical_input"):
                return refusal("physical_input")
        return None

    if name == "visual_control":
        if off("desktop_control"):
            return refusal("desktop_control")
        return refusal("uitars_visual") if off("uitars_visual") else None

    if name == "browser_action":
        return (refusal("playwright_browser") if off("playwright_browser")
                else None)

    # Power control has its own switch AND honours the master desktop switch: a
    # user who turned desktop control off has not asked J.A.R.V.I.S. to be able
    # to shut the machine down.
    if name == "power_control":
        if off("desktop_control"):
            return refusal("desktop_control")
        return refusal("power_control") if off("power_control") else None

    # ── deep Windows orchestration ────────────────────────────────────────────
    # Two switches inside one tool: process work and settings work are separate
    # permissions, so revoking one cannot leave the other quietly enabled.
    if name == "system_control":
        if off("desktop_control"):
            return refusal("desktop_control")
        if act in _PROCESS_ACTIONS:
            return refusal("process_control") if off("process_control") else None
        return refusal("system_settings") if off("system_settings") else None

    if name == "window_control":
        if off("desktop_control"):
            return refusal("desktop_control")
        return refusal("window_control") if off("window_control") else None

    # The clipboard is read-only until the user writes to it, so reading does not
    # need the switch; anything that puts text ON the clipboard, or sends it to a
    # model for analysis, does.
    if name == "clipboard_sentry":
        if act in _CLIPBOARD_WRITE_ACTIONS or act in _CLIPBOARD_MODEL_ACTIONS:
            return refusal("clipboard_sentry") if off("clipboard_sentry") else None
        return None

    # Every document action writes a file, so one switch covers the tool. The
    # engine probe is not writing anything but is still the same deliverable
    # feature — refusing it would make 'can you write Word documents?' answer
    # with silence instead of a listing.
    if name == "documents":
        if act == "engines":
            return None
        return refusal("document_architect") if off("document_architect") else None

    # The globe only draws what the geospatial resolver found, and drawing is a
    # surface on this machine — so it follows desktop control rather than being a
    # capability of its own. The read-only listings answer "what can you place?"
    # without needing anything enabled, exactly like the document engines probe.
    if name == "globe":
        if act in ("list", "status"):
            return None
        return refusal("desktop_control") if off("desktop_control") else None

    # ── household and accounts ────────────────────────────────────────────────
    # Neither is desktop control: a user who revoked control of this PC has not
    # asked to lose the lamp, and the two services are separate permissions for
    # the same reason (one can be revoked without touching the other).
    if name == "smart_home":
        return refusal("smart_home") if off("smart_home") else None

    if name == "instagram":
        if act == "status":
            return None
        return refusal("instagram") if off("instagram") else None

    if name == "live_vision":
        return refusal("live_vision") if off("live_vision") else None

    return None
