"""Tool handlers for the structured local actions.

One entry point per grouped tool (``pc_control``, ``visual_control``,
``browser_action``). Each one:

1. validates the requested action against ``capabilities/registry.py`` (so an
   unknown action or a missing parameter can never reach a controller),
2. takes the matching control lock through the router's existing path
   (desktop for PC/visual work, browser for Playwright), which also runs the
   permission and approval gates,
3. executes through the audited controller,
4. returns one text block for Gemini that always names the controller that ran
   and never presents unverified work as finished.

Nothing here plans or decides: J.A.R.V.I.S. (Gemini) chooses the action, the
router enforces the policy, and the controllers do the work. The model's own
words are only ever read as data — an action name, a selector, a string to type.
"""

from __future__ import annotations

from typing import Callable

from capabilities import registry


def _progress(on_event: Callable[[str], None] | None, text: str) -> None:
    """Forward one REAL step description to the activity monitor."""
    if not on_event or not text:
        return
    try:
        on_event(str(text)[:120])
    except Exception:
        pass


# ── executors ─────────────────────────────────────────────────────────────────

def _pc_runner(action: registry.Action, args: dict,
               on_event: Callable[[str], None] | None):
    from capabilities import desktop

    def runner() -> dict:
        status = desktop.status()
        if not status["available"]:
            return {"ok": False, "controller": "desktop",
                    "reason": "pyautogui_missing",
                    "output": ("Local PC control is unavailable: "
                               + status["detail"] + ". Nothing else was "
                               "substituted.")}
        _progress(on_event, registry.activity_detail(registry.TOOL_PC, args))
        result = desktop.run(action.name, args)
        detail = str(result.get("detail") or "")
        if result.get("ok"):
            _progress(on_event, "Completed")
        elif detail:
            _progress(on_event, f"Failed: {detail[:80]}")
        return {
            "ok": bool(result.get("ok")),
            "controller": str(result.get("controller") or action.controller),
            "output": detail,
            "reason": "" if result.get("ok") else "action_failed",
            "evidence": result.get("evidence") or {},
            "action": action.name,
        }

    return runner


def _browser_runner(action: registry.Action, args: dict,
                    on_event: Callable[[str], None] | None):
    from capabilities import browser_pw

    def runner() -> dict:
        if not browser_pw.installed():
            return {"ok": False, "controller": "playwright",
                    "reason": "playwright_missing",
                    "output": ("Browser automation is unavailable: Playwright is "
                               "not installed (pip install playwright && "
                               "playwright install chromium). No other browser "
                               "engine was substituted.")}
        state = browser_pw.status()
        if not state.get("browsers"):
            return {"ok": False, "controller": "playwright",
                    "reason": "playwright_browser_missing",
                    "output": ("Browser automation is unavailable: no Playwright "
                               "browser is installed. Run 'playwright install "
                               "chromium'.")}
        _progress(on_event, registry.activity_detail(registry.TOOL_BROWSER, args))
        result = browser_pw.run(action.name, args)
        detail = str(result.get("detail") or "")
        if result.get("ok"):
            _progress(on_event, "Completed")
        else:
            _progress(on_event, f"Failed: {detail[:80]}")
        payload = {
            "ok": bool(result.get("ok")),
            "controller": str(result.get("controller") or "playwright"),
            "output": detail,
            "reason": "" if result.get("ok") else "action_failed",
            "evidence": result.get("evidence") or {},
            "action": action.name,
        }
        for key in ("page_url", "page_title", "path", "image"):
            if payload["evidence"].get(key):
                payload[key] = payload["evidence"][key]
        return payload

    return runner


def _power_runner(action: registry.Action, args: dict,
                  on_event: Callable[[str], None] | None):
    """One machine-level action through capabilities/power.py.

    The confirmation gate for the actions that can end the session is declared
    in the registry (``gate="confirm"``) and enforced by the router before this
    runner is ever called — there is no second policy here.
    """
    from capabilities import power

    def runner() -> dict:
        _progress(on_event, registry.activity_detail(registry.TOOL_POWER, args))
        result = power.run(action.name, args)
        detail = str(result.get("detail") or "")
        if result.get("ok"):
            _progress(on_event, "Completed")
        elif detail:
            _progress(on_event, f"Failed: {detail[:80]}")
        return {
            "ok": bool(result.get("ok")),
            "controller": str(result.get("controller") or "power"),
            "output": detail,
            "reason": "" if result.get("ok") else str(
                result.get("reason") or "action_failed"),
            "evidence": result.get("evidence") or {},
            "action": action.name,
        }

    return runner


def _module_runner(tool: str, action: registry.Action, args: dict,
                   on_event: Callable[[str], None] | None,
                   resolve: Callable[[str], object]):
    """Shared runner for the modules that implement one action each.

    ``resolve`` maps an action name to the module that owns it (a module
    exposing ``run(action, args) -> dict``), so process/settings/window/clipboard
    actions all report the controller that actually ran them.
    """

    def runner() -> dict:
        module = resolve(action.name)
        if module is None:
            return {"ok": False, "controller": "local",
                    "reason": "unknown_action",
                    "output": (f"'{action.name}' is not an action of {tool}. "
                               "Nothing was run.")}
        _progress(on_event, registry.activity_detail(tool, args))
        result = module.run(action.name, dict(args))  # type: ignore[attr-defined]
        detail = str(result.get("detail") or "")
        if result.get("ok"):
            _progress(on_event, "Completed")
        elif detail:
            _progress(on_event, f"Failed: {detail[:80]}")
        payload = {
            "ok": bool(result.get("ok")),
            "controller": str(result.get("controller") or action.controller),
            "output": detail,
            "reason": "" if result.get("ok") else str(result.get("reason")
                                                       or "action_failed"),
            "evidence": result.get("evidence") or {},
            "action": action.name,
        }
        # Whatever else the module measured travels with the result instead of
        # being dropped here: a surface that has to re-run the work to draw it
        # (the HoloGlobe re-resolving a city) would be producing a second answer
        # that can silently disagree with the one the model was handed.
        for key, value in result.items():
            if key not in ("ok", "controller", "detail", "reason", "evidence"):
                payload.setdefault(key, value)
        return payload

    return runner


def _system_runner(action: registry.Action, args: dict,
                   on_event: Callable[[str], None] | None):
    """``system_control`` — processes (psutil) and settings (netsh/registry)."""

    def resolve(action_name: str):
        from capabilities import processes, sysctl
        if action_name in processes._HANDLERS:
            return processes
        if action_name in sysctl._HANDLERS:
            return sysctl
        return None

    return _module_runner(registry.TOOL_SYSTEM, action, args, on_event, resolve)


def _window_runner(action: registry.Action, args: dict,
                   on_event: Callable[[str], None] | None):
    """``window_control`` — Win32 arrangement across every display."""

    def resolve(_action_name: str):
        from capabilities import windows_ext
        return windows_ext

    return _module_runner(registry.TOOL_WINDOW, action, args, on_event, resolve)


def _clipboard_runner(action: registry.Action, args: dict,
                      on_event: Callable[[str], None] | None):
    """``clipboard_sentry`` — clipboard text, treated strictly as data."""

    def resolve(_action_name: str):
        from capabilities import clipboard
        return clipboard

    return _module_runner(registry.TOOL_CLIPBOARD, action, args, on_event, resolve)


def _documents_runner(action: registry.Action, args: dict,
                      on_event: Callable[[str], None] | None):
    """``documents`` — Word, Excel, PowerPoint and PDF deliverables."""

    def resolve(_action_name: str):
        from documents import tool as documents_tool
        return documents_tool

    return _module_runner(registry.TOOL_DOCUMENTS, action, args, on_event, resolve)


def _smart_home_runner(action: registry.Action, args: dict,
                       on_event: Callable[[str], None] | None):
    """``smart_home`` — local Kasa/Tapo devices, never through a cloud."""

    def resolve(_action_name: str):
        from capabilities import smart_home
        return smart_home

    return _module_runner(registry.TOOL_SMART_HOME, action, args, on_event, resolve)


def _instagram_runner(action: registry.Action, args: dict,
                      on_event: Callable[[str], None] | None):
    """``instagram`` — the user's own account; the public actions gate on confirm."""

    def resolve(_action_name: str):
        from capabilities import instagram
        return instagram

    return _module_runner(registry.TOOL_INSTAGRAM, action, args, on_event, resolve)


def _globe_runner(action: registry.Action, args: dict,
                  on_event: Callable[[str], None] | None):
    """``globe`` — the geospatial resolver that feeds the HoloGlobe pane."""

    def resolve(_action_name: str):
        from capabilities import geospatial
        return geospatial

    return _module_runner(registry.TOOL_GLOBE, action, args, on_event, resolve)


def _visual_runner(action: registry.Action, args: dict,
                   on_event: Callable[[str], None] | None):
    from capabilities import desktop, uitars

    instruction = _instruction(action, args)

    def runner() -> dict:
        state = uitars.status()
        if not state.get("sdk"):
            return {"ok": False, "controller": "uitars",
                    "reason": "uitars_sdk_missing",
                    "output": ("Visual control is unavailable: " + state["detail"]
                               + ". Nothing else was substituted.")}
        if not state.get("available"):
            return {"ok": False, "controller": "uitars",
                    "reason": "no_gemini_key",
                    "output": ("Visual control is unavailable: " + state["detail"]
                               + ". UI-TARS uses the Gemini key — there is no "
                                 "separate UI-TARS credential.")}
        _progress(on_event, f"Using UI-TARS visual control: {instruction[:70]}")
        try:
            png, info = desktop.grab_png()
        except Exception as exc:
            return {"ok": False, "controller": "uitars",
                    "reason": "capture_failed",
                    "output": f"The screen could not be captured: {exc}"}

        if action.name == "visual_find":
            step = uitars.next_action(instruction, png=png, info=info)
            if not step.get("ok"):
                return _visual_failure(step)
            point = step.get("point")
            if not point:
                return {"ok": False, "controller": "uitars",
                        "reason": "no_position",
                        "output": ("I could not find a usable position for "
                                   f"'{instruction}' on the screen. Nothing was "
                                   "clicked."),
                        "evidence": step.get("evidence") or {}}
            return {"ok": True, "controller": "uitars",
                    "output": (f"Found it at ({point[0]}, {point[1]}) on the "
                               f"screen — found with UI-TARS visual grounding on "
                               f"Gemini/{step.get('model')}. Nothing was clicked."),
                    "evidence": {**(step.get("evidence") or {}),
                                 "thought": step.get("thought", ""),
                                 "action": step.get("action_type", "")},
                    "point": list(point)}

        # Every other visual action: ask for the next GUI action, execute it.
        step = uitars.next_action(instruction, png=png, info=info)
        if not step.get("ok"):
            return _visual_failure(step)
        action_type = str(step.get("action_type") or "")
        if action.name == "visual_type" and action_type == "click":
            # A single model reply can only do one thing. Clicking the field is
            # the right first step; typing follows immediately as our own
            # deterministic second step, reported as such.
            click = uitars.execute(step, info)
            if not click.get("ok"):
                return {**click, "reason": "click_failed"}
            typed = desktop.run_physical("keyboard_type",
                                          {"text": str(args.get("text") or "")})
            return {
                "ok": bool(typed.get("ok")),
                "controller": "uitars",
                "output": (f"Clicked the element UI-TARS found at {step.get('point')}"
                           f" and typed the requested text ({typed.get('detail')})."),
                "reason": "" if typed.get("ok") else "type_failed",
                "evidence": {**(step.get("evidence") or {}),
                             "click": click.get("evidence") or {},
                             "type": typed.get("evidence") or {},
                             "thought": step.get("thought", "")},
            }
        result = uitars.execute(step, info)
        ok = bool(result.get("ok"))
        evidence = {**(step.get("evidence") or {}),
                    "thought": step.get("thought", ""),
                    "executed": result.get("evidence") or {}}
        if ok:
            _progress(on_event, "Completed")
        return {
            "ok": ok,
            "controller": str(result.get("controller") or "uitars"),
            "output": str(result.get("detail") or ""),
            "reason": "" if ok else "action_failed",
            "evidence": evidence,
            "action": action_type or action.name,
        }

    return runner


def _visual_failure(step: dict) -> dict:
    return {"ok": False,
            "controller": "uitars",
            "reason": str((step.get("evidence") or {}).get("reason") or "failed"),
            "output": str(step.get("detail") or "The visual layer did not answer."),
            "evidence": step.get("evidence") or {}}


def _instruction(action: registry.Action, args: dict) -> str:
    """What the model asked for, in the words it used — never a synthesis."""
    given = str(args.get("instruction") or args.get("target") or "").strip()
    if given:
        return given
    text = str(args.get("text") or "").strip()
    direction = str(args.get("direction") or "").strip()
    key = str(args.get("key") or "").strip()
    if action.name == "visual_type" and text:
        return f"the text field where this belongs: {text[:60]}"
    if action.name == "visual_scroll":
        return f"scroll {direction or 'down'} the current view"
    if action.name == "visual_keypress":
        return f"press the {key or 'requested'} key in the current view"
    return action.summary


# ── tool entry points ─────────────────────────────────────────────────────────

def _handle(tool: str, args: dict, enabled=None,
            on_event: Callable[[str], None] | None = None,
            on_result: Callable[..., None] | None = None) -> str:
    """Validate against the registry, then run through the router.

    The registry is authoritative: an action it does not declare cannot reach a
    controller, and a tool without a runner (a declaration bug) is refused with
    a sentence instead of raising a KeyError in the middle of a voice turn.

    ``on_result`` (optional) receives ``(action_name, raw_data, ok)`` before the
    result is flattened into a sentence. Surfaces such as the HoloGlobe need the
    data itself; a surface that parsed the sentence back would be guessing at
    what the controller actually reported.
    """
    from capabilities import router

    args = dict(args or {})
    action, error = registry.validate(tool, args)
    if action is None:
        return error
    if tool not in _RUNNERS:
        return (f"{tool} has no local runner in this build, so nothing was run.")
    runner = _RUNNERS[tool](action, args, on_event)
    result = router.route_local(tool, action, args, runner, enabled=enabled)
    if on_result is not None:
        try:
            on_result(action.name, dict(result.data or {}), bool(result.ok))
        except Exception:
            pass
    return router.finalize_local(result)


_RUNNERS = {
    registry.TOOL_PC: _pc_runner,
    registry.TOOL_BROWSER: _browser_runner,
    registry.TOOL_VISUAL: _visual_runner,
    registry.TOOL_POWER: _power_runner,
    registry.TOOL_SYSTEM: _system_runner,
    registry.TOOL_WINDOW: _window_runner,
    registry.TOOL_CLIPBOARD: _clipboard_runner,
    registry.TOOL_DOCUMENTS: _documents_runner,
    registry.TOOL_SMART_HOME: _smart_home_runner,
    registry.TOOL_INSTAGRAM: _instagram_runner,
    registry.TOOL_GLOBE: _globe_runner,
}


def handle_pc_control(args: dict, enabled=None, on_event=None) -> str:
    """``pc_control`` — mouse, keyboard, screen, applications and windows."""
    return _handle(registry.TOOL_PC, args, enabled, on_event)


def handle_visual_control(args: dict, enabled=None, on_event=None) -> str:
    """``visual_control`` — UI-TARS visual grounding on the Gemini model."""
    return _handle(registry.TOOL_VISUAL, args, enabled, on_event)


def handle_browser_action(args: dict, enabled=None, on_event=None) -> str:
    """``browser_action`` — Playwright element actions."""
    return _handle(registry.TOOL_BROWSER, args, enabled, on_event)


def handle_power_control(args: dict, enabled=None, on_event=None) -> str:
    """``power_control`` — system power, session and volume actions.

    Actions that can end the session are declared with ``gate="confirm"`` in the
    registry and are therefore parked behind the on-screen confirmation, even
    when the approval mode would otherwise let them run.
    """
    return _handle(registry.TOOL_POWER, args, enabled, on_event)


def handle_system_control(args: dict, enabled=None, on_event=None) -> str:
    """``system_control`` — processes plus Wi-Fi/volume/brightness/theme/metrics.

    ``kill_process`` is declared with ``gate="confirm"``: freezing a process is
    reversible and happens immediately, ending one always asks first.
    """
    return _handle(registry.TOOL_SYSTEM, args, enabled, on_event)


def handle_window_control(args: dict, enabled=None, on_event=None) -> str:
    """``window_control`` — minimize, maximize, tile, snap and multi-monitor moves."""
    return _handle(registry.TOOL_WINDOW, args, enabled, on_event)


def handle_clipboard_sentry(args: dict, enabled=None, on_event=None) -> str:
    """``clipboard_sentry`` — read/write plus analysis of clipboard text as data."""
    return _handle(registry.TOOL_CLIPBOARD, args, enabled, on_event)


def handle_documents(args: dict, enabled=None, on_event=None) -> str:
    """``documents`` — write real Word/Excel/PowerPoint/PDF deliverables.

    The result always names the file that was written; when an optional package
    is missing the answer names it and its pip command instead of producing an
    empty file.
    """
    return _handle(registry.TOOL_DOCUMENTS, args, enabled, on_event)


def handle_smart_home(args: dict, enabled=None, on_event=None) -> str:
    """``smart_home`` — local smart plugs and bulbs over this network."""
    return _handle(registry.TOOL_SMART_HOME, args, enabled, on_event)


def handle_instagram(args: dict, enabled=None, on_event=None) -> str:
    """``instagram`` — the user's own account; publishing always confirms."""
    return _handle(registry.TOOL_INSTAGRAM, args, enabled, on_event)


def handle_globe(args: dict, enabled=None, on_event=None, on_result=None) -> str:
    """``globe`` — resolve places, then hand the evidence to the HoloGlobe.

    The pane is a surface, not a controller: the places are resolved once, in
    the same registry/approval path as every other action, and ``on_result``
    receives that exact evidence so the HUD can only ever draw what the model
    was told — never the result of a second lookup that could disagree.
    """
    return _handle(registry.TOOL_GLOBE, args, enabled, on_event,
                   on_result=on_result)


__all__ = ["handle_browser_action", "handle_clipboard_sentry", "handle_documents",
           "handle_globe", "handle_instagram", "handle_pc_control",
           "handle_power_control", "handle_smart_home", "handle_system_control",
           "handle_visual_control", "handle_window_control"]
