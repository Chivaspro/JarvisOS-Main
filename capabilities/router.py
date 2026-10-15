"""The unified capability router.

Gemini stays the brain and sees high-level capabilities:

    computer_control   ->  Agent TARS   (Windows apps: launch/run; browser when needed)
    browser_control    ->  Agent TARS   (real-browser web workflows)
    web_research       ->  Agent Reach  (internet research channels)

Both automation capabilities are served by Agent TARS v0.3.0 — the single PC /
browser executor — while Agent Reach keeps its own research channels.

This module decides which backend serves a request, holds the ownership lock
while it works, runs the existing approval policy for consequential actions,
reports what actually happened rather than what was attempted, and never
substitutes one engine for another. ``on_event`` (optional) forwards the engine's
REAL progress to the existing activity monitor; nothing here fabricates progress.

Mixed tasks are composed by Gemini across several tool calls (research -> reason
-> act), which is why each handler returns an outcome the model can read and
chain from, instead of trying to plan inside this layer.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from capabilities import locks

#: Consequential verbs: an action that changes something outside the machine.
_SUBMIT_VERBS = (
    "send", "submit", "publish", "post", "reply", "buy", "purchase", "order",
    "pay", "delete", "remove", "uninstall", "install", "overwrite", "empty",
    "unsubscribe", "apply for", "book", "reserve", "share", "comment",
)
#: Anything that touches the machine itself rather than a page.
_SYSTEM_VERBS = ("delete", "format", "registry", "uninstall", "install",
                 "shutdown", "restart", "disable", "settings", "permission")

_domain_locks = threading.Lock()

ENGINE_OF = {"computer_control": "agent_tars",
             "browser_control": "agent_tars",
             "web_research": "agent_reach",
             "pc_control": "desktop",
             "visual_control": "uitars",
             "browser_action": "playwright",
             "power_control": "power",
             "system_control": "windows",
             "window_control": "win32",
             "clipboard_sentry": "clipboard",
             "documents": "documents",
             "smart_home": "kasa",
             "instagram": "instagrapi",
             "globe": "globe"}
#: Every registry tool must appear here: ``_locked_run`` reads the domain, so a
#: missing entry would surface as a crash rather than as a refusal. The lock is
#: what guarantees one controller owns the target at a time.
LOCK_OF = {"computer_control": "desktop",
           "browser_control": "browser",
           "web_research": "research",
           "pc_control": "desktop",
           "visual_control": "desktop",
           "browser_action": "browser",
           "power_control": "desktop",
           "system_control": "desktop",
           "window_control": "desktop",
           # Reading or writing the clipboard does not drive the desktop, and
           # writing a document only touches the disk: they take their own
           # domains so a long document build never blocks PC control.
           "clipboard_sentry": "clipboard",
           "documents": "documents",
           # Devices and accounts are their own domains: switching a lamp off must
           # not wait behind a window being tiled, nor block one.
           "smart_home": "smart_home",
           "instagram": "instagram",
           "globe": "desktop"}
LABEL_OF = {"computer_control": "Agent TARS",
            "browser_control": "Agent TARS",
            "web_research": "Agent Reach",
            "pc_control": "Local PC control",
            "visual_control": "UI-TARS visual control",
            "browser_action": "Playwright",
            "power_control": "System power control",
            "system_control": "System control",
            "window_control": "Window control",
            "clipboard_sentry": "Clipboard sentry",
            "documents": "Document architect",
            "smart_home": "Smart home",
            "instagram": "Instagram",
            "globe": "HoloGlobe"}

#: How long a local action waits for the domain lock before refusing. Agent TARS
#: may legitimately be mid-task; a short wait is friendlier than an instant
#: "busy", while still guaranteeing one controller owns the target.
LOCAL_LOCK_WAIT = {"pc_control": 10.0, "visual_control": 15.0,
                   "browser_action": 20.0, "power_control": 5.0,
                   "system_control": 5.0, "window_control": 5.0,
                   "clipboard_sentry": 5.0, "documents": 30.0,
                   "smart_home": 20.0, "instagram": 45.0,
                   # A geocoding lookup can be slow; the globe never holds the
                   # desktop lock for long because it only shares it on write.
                   "globe": 8.0}

#: Reasons where the engine could not act at all — nothing may be presented as
#: done and the request must not be handed to a different engine.
_BLOCKED_REASONS = {
    "unavailable", "disabled", "no_gemini_key", "environment_missing",
    "node_missing", "cli_missing", "worker_missing", "refused",
    "awaiting_confirmation", "lock_busy", "no_input", "server_unavailable",
    # Local controllers: the engine could not act at all, so the request must
    # not be presented as done nor handed to a different controller.
    "pyautogui_missing", "playwright_missing", "playwright_browser_missing",
    "uitars_sdk_missing", "capture_failed", "no_position",
    "provider_unsupported",
    # Power control: the OS refused, or the request itself could not be
    # understood, so nothing happened and nothing may be reported as done.
    "unsupported_platform", "set_volume_unavailable", "unknown_power_action",
    "bad_percent",
}


@dataclass
class CapabilityResult:
    """One capability execution, in the shape every caller and the ledger expects."""

    ok: bool
    capability: str
    backend: str
    output: str
    reason: str = ""
    data: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    approval: str = ""
    elapsed_s: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "capability": self.capability,
                "backend": self.backend, "output": self.output,
                "reason": self.reason, "data": self.data,
                "verification": self.verification, "approval": self.approval,
                "elapsed_s": self.elapsed_s}


# ── internal diagnostics (never surfaced into ChatLog) ────────────────────────

def _log(level: str, capability: str, message: str, **extra) -> None:
    try:
        from observability.logger import log_event
        log_event(level, "capabilities", "capability_router",
                  detail=message[:400], capability=capability, **extra)
        return
    except Exception:
        pass
    try:
        print(f"[Capability:{capability}] {message}")
    except Exception:
        pass


# ── consequential-action detection ───────────────────────────────────────────

def consequential(task: str) -> str:
    """The first consequential verb found in the request ('' when none)."""
    text = str(task or "").lower()
    for verb in _SUBMIT_VERBS:
        if re.search(rf"\b{re.escape(verb)}\b", text):
            return verb
    return ""


def _approval_gate(capability: str, task: str, params: dict,
                   enabled: Callable[[str], bool] | None) -> dict:
    """Run the assistant's existing approval policy over one control action.

    Returns {'proceed': bool, 'message': str, 'state': str}. No second permission
    system and no second UI: security.approvals + security.confirm only.
    """
    verb = consequential(task)
    if not verb:
        return {"proceed": True, "message": "", "state": ""}

    action = f"{capability}_{verb}"
    try:
        from security.approvals import evaluate
        decision = evaluate(action, dict(params or {}), enabled=enabled)
    except Exception as exc:
        _log("WARNING", capability, f"approval service unavailable: {exc}")
        return {"proceed": False,
                "message": ("The approval service is unavailable, so I have not "
                            "performed that consequential action."),
                "state": "service_unavailable"}

    if decision.get("allowed"):
        return {"proceed": True, "message": "", "state": ""}
    if decision.get("refusal"):
        return {"proceed": False, "message": decision["refusal"], "state": "refused"}
    return {"proceed": False, "message": "", "state": "needs_token",
            "decision": decision}


def _park_behind_token(capability: str, task: str, run: Callable[[], str]) -> str:
    """Send an irreversible action through the existing on-screen confirmation."""
    try:
        from security import confirm
        return confirm.request(
            key=f"capability:{capability}",
            title=f"{LABEL_OF.get(capability, capability)}: {task[:70]}",
            detail=("This will change something outside this machine. Confirm on "
                    "the HUD to let it run, or cancel."),
            run=run)
    except Exception as exc:
        _log("WARNING", capability, f"confirmation gate failed: {exc}")
        return ("I could not put the confirmation on screen, so I have not "
                "done that consequential action.")


# ── the shared execution path ─────────────────────────────────────────────────

def _execute(capability: str, task: str, runner: Callable[[], dict],
             enabled: Callable[[str], bool] | None = None,
             params: dict | None = None,
             wait_for_lock_s: float = 0.0) -> CapabilityResult:
    """Lock -> approve -> run -> verify -> unlock, with one honest result object."""
    from capabilities import config
    backend = ENGINE_OF[capability]
    started = time.monotonic()
    params = dict(params or {})

    if not config.enabled(backend):
        return CapabilityResult(
            False, capability, backend,
            f"{LABEL_OF[capability]} is switched off in Settings, so I did not use it. "
            f"No other engine was substituted.",
            reason="disabled")

    gate = _approval_gate(capability, task, params, enabled)
    if not gate["proceed"]:
        if gate["state"] == "needs_token":
            def _deferred() -> str:
                res = _locked_run(capability, backend, runner, wait_for_lock_s, started)
                return res.output
            message = _park_behind_token(capability, task, _deferred)
            return CapabilityResult(False, capability, backend, message,
                                    reason="awaiting_confirmation", approval="pending_ui_token")
        return CapabilityResult(False, capability, backend, gate["message"],
                                reason="refused", approval="refused")

    result = _locked_run(capability, backend, runner, wait_for_lock_s, started)
    return result


def _locked_run(capability: str, backend: str, runner: Callable[[], dict],
                wait_for_lock_s: float, started: float) -> CapabilityResult:
    """Acquire the domain lock, run, and always release it."""
    from capabilities import proc
    domain = LOCK_OF[capability]
    lock = locks.get_lock(domain)
    try:
        lock.acquire(owner=f"{capability}:{backend}", timeout_s=wait_for_lock_s)
    except locks.ControlBusy as busy:
        _log("WARNING", capability, f"lock busy: {busy}")
        return CapabilityResult(False, capability, backend, str(busy), reason="lock_busy")

    proc.register_shutdown_hook()
    _log("INFO", capability, f"{backend} task started")
    try:
        raw = runner() or {}
    except Exception as exc:
        _log("ERROR", capability, f"{backend} raised {type(exc).__name__}: {exc}")
        return CapabilityResult(False, capability, backend,
                                f"{LABEL_OF[capability]} failed unexpectedly: {exc}",
                                reason="crashed",
                                elapsed_s=round(time.monotonic() - started, 2))
    finally:
        try:
            lock.release()
        except Exception:
            pass

    ok = bool(raw.get("ok"))
    verification = raw.get("verification") or {
        "verified": ok,
        "how": ("the engine returned a result" if ok else "the engine reported no result"),
    }
    _log("INFO" if ok else "WARNING", capability,
         f"{backend} finished ok={ok} elapsed={time.monotonic() - started:.1f}s "
         f"verified={verification.get('verified')} reason={raw.get('reason', '')}")
    return CapabilityResult(
        ok, capability, backend,
        str(raw.get("output") or f"{LABEL_OF[capability]} returned nothing."),
        reason=str(raw.get("reason") or raw.get("error_code") or ""),
        data={k: v for k, v in raw.items() if k not in ("output", "ok", "reason")},
        verification=verification,
        elapsed_s=round(time.monotonic() - started, 2),
    )


# ── public capability entry points ────────────────────────────────────────────

def route_computer_control(task: str, execute: bool = True,
                           enabled: Callable[[str], bool] | None = None,
                           on_event: Callable[[str], None] | None = None,
                           url: str = "") -> CapabilityResult:
    """Native Windows work. Agent TARS only — never a substitute engine."""
    from capabilities import agent_tars

    def runner() -> dict:
        return agent_tars.run_task(task, url=url, mode="computer",
                                   on_event=on_event)

    return _execute("computer_control", task, runner, enabled,
                    params={"task": task[:400]})


def route_browser_control(task: str, url: str = "",
                          max_steps: int = 0,
                          enabled: Callable[[str], bool] | None = None,
                          on_event: Callable[[str], None] | None = None) -> CapabilityResult:
    """Web workflow work. Agent TARS only."""
    from capabilities import agent_tars

    def runner() -> dict:
        return agent_tars.run_task(task, url=url, mode="browser",
                                   max_steps=max_steps, on_event=on_event)

    return _execute("browser_control", task or url, runner, enabled,
                    params={"task": (task or url)[:400], "url": url})


def route_web_research(query: str = "", platform: str = "", url: str = "",
                       max_results: int = 0,
                       enabled: Callable[[str], bool] | None = None) -> CapabilityResult:
    """Internet retrieval. Agent Reach only."""
    from capabilities import agent_reach

    def runner() -> dict:
        return agent_reach.research(query=query, platform=platform, url=url,
                                    max_results=max_results)

    return _execute("web_research", query or url, runner, enabled,
                    params={"query": (query or url)[:400], "platform": platform})


# ── tool-facing handlers (return the string Gemini reads back) ────────────────

# ── the local controllers (pc_control / visual_control / browser_action) ──────

def route_local(tool: str, action, args: dict, runner: Callable[[], dict],
                enabled: Callable[[str], bool] | None = None,
                wait_for_lock_s: float | None = None) -> CapabilityResult:
    """One structured local action through the same policy path as the engines.

    Order: permission switch (the exact feature named by the action) → approval
    policy (the request's own words decide the risk tier) → domain lock → run →
    unlock. The result carries the controller that actually ran.
    """
    backend = ENGINE_OF.get(tool, tool)
    started = time.monotonic()
    params = dict(args or {})

    if enabled is not None:
        try:
            from security.permissions import permission_refusal
            refusal = permission_refusal(enabled, tool, params)
        except Exception as exc:
            _log("WARNING", tool, f"permission gate unavailable: {exc}")
            refusal = None
        if refusal:
            return CapabilityResult(False, tool, backend, refusal,
                                    reason="disabled")

    scan = _scan_text(action, params)
    # A registry-declared "confirm" gate is absolute: the action can end or
    # destroy the user's session, so it goes through the human-issued token
    # whatever the approval mode is configured to do. The model cannot write
    # that token — only the HUD can produce it.
    if getattr(action, "gate", "") == "confirm":
        def _confirmed() -> str:
            res = _locked_run(tool, backend, runner, wait_for_lock_s or 0.0, started)
            return res.output
        message = _park_behind_token(tool, scan, _confirmed)
        return CapabilityResult(False, tool, backend, message,
                                reason="awaiting_confirmation",
                                approval="pending_ui_token")

    gate = _approval_gate(tool, scan, params, enabled)
    if not gate["proceed"]:
        if gate["state"] == "needs_token":
            def _deferred() -> str:
                res = _locked_run(tool, backend, runner, wait_for_lock_s or 0.0,
                                  started)
                return res.output
            message = _park_behind_token(tool, scan, _deferred)
            return CapabilityResult(False, tool, backend, message,
                                    reason="awaiting_confirmation",
                                    approval="pending_ui_token")
        return CapabilityResult(False, tool, backend, gate["message"],
                                reason="refused", approval="refused")

    wait = LOCAL_LOCK_WAIT.get(tool, 0.0) if wait_for_lock_s is None \
        else float(wait_for_lock_s)
    return _locked_run(tool, backend, runner, wait, started)


def _scan_text(action, params: dict) -> str:
    """The request in its own words, for the consequential-verb scan.

    Everything the user asked for is included — the action name, the element or
    application named, the text being typed, the URL — so a click on a "Submit"
    control is judged the same way a natural-language instruction would be.
    """
    parts = [str(getattr(action, "name", action) or "")]
    for key in ("target", "selector", "instruction", "text", "url", "app",
                "title", "keys", "key", "tab", "path"):
        value = params.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    return " | ".join(parts)[:400]


def finalize_local(result: CapabilityResult) -> str:
    """One text block for Gemini, plus the evidence the action produced.

    Evidence arrives in one of two shapes: a runner that returns an
    ``evidence`` dict, or the flat keys a runner returns directly (which is what
    the shared ``_locked_run`` path produces). Both are read, so a controller's
    real measurements always reach the model.
    """
    text = _finalize(result)
    data = dict(result.data or {})
    evidence = data.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        evidence = {k: v for k, v in data.items() if k != "controller"}
    line = _evidence_line(evidence)
    if line:
        text += "\n" + line
    return text


def _evidence_line(evidence: dict) -> str:
    """Compact, human-readable evidence (scalars only, never a blob)."""
    if not isinstance(evidence, dict):
        return ""
    bits: list[str] = []
    for key in ("action", "strategy", "matches", "page_url", "page_title",
                "pointer", "scale", "path", "file", "width", "height",
                "model", "point", "thought", "keys", "key", "method",
                "chars", "option", "direction", "amount", "window",
                "verified", "tabs", "click", "type"):
        value = evidence.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        text = " ".join(str(value).split())
        # A truncated file path would be useless to the caller (the image panel
        # opens the path this line reports), so path-like values keep their
        # length and just the rest is kept short.
        limit = 400 if key in ("path", "image", "page_url", "page_title") else 80
        bits.append(f"{key}={text[:limit]}")
    return ("evidence: " + "; ".join(bits)) if bits else ""


def handle_computer_control(args: dict, enabled=None,
                            on_event: Callable[[str], None] | None = None) -> str:
    args = dict(args or {})
    task = str(args.get("task") or args.get("instruction") or "").strip()
    if not task:
        return "computer_control needs a task describing what to do on the desktop."
    result = route_computer_control(task, enabled=enabled, on_event=on_event,
                                    url=str(args.get("url") or "").strip())
    return _finalize(result)


def handle_browser_control(args: dict, enabled=None,
                           on_event: Callable[[str], None] | None = None) -> str:
    args = dict(args or {})
    task = str(args.get("task") or args.get("instruction") or "").strip()
    url = str(args.get("url") or "").strip()
    if not task and not url:
        return "browser_control needs a task, a URL to open, or both."
    try:
        steps = int(args.get("max_steps") or 0)
    except (TypeError, ValueError):
        steps = 0
    result = route_browser_control(task, url=url, max_steps=steps, enabled=enabled,
                                   on_event=on_event)
    return _finalize(result)


def handle_web_research(args: dict, enabled=None) -> str:
    args = dict(args or {})
    query = str(args.get("query") or args.get("topic") or "").strip()
    url = str(args.get("url") or "").strip()
    platform = str(args.get("platform") or "").strip()
    if not query and not url:
        return "web_research needs a query or a URL."
    try:
        n = int(args.get("max_results") or 0)
    except (TypeError, ValueError):
        n = 0
    result = route_web_research(query=query, platform=platform, url=url,
                                max_results=n, enabled=enabled)
    return _finalize(result)


def _finalize(result: CapabilityResult) -> str:
    """One text block for Gemini, always stating the real backend and outcome."""
    head = result.output or f"{LABEL_OF.get(result.capability, 'The engine')} returned nothing."
    controller = str((result.data or {}).get("controller") or "")
    tail = [f"\n[capability={result.capability} backend={result.backend} "
            + (f"controller={controller} " if controller else "")
            + f"status={'DONE' if result.ok else 'NOT_DONE'}"
            + (f" reason={result.reason}" if result.reason else "")
            + f" elapsed={result.elapsed_s}s]"]
    verify = result.verification or {}
    if verify:
        tail.append(f"verification: {verify.get('how', '')}; "
                    f"verified={verify.get('verified')}")
    if not result.ok:
        if result.reason in _BLOCKED_REASONS:
            # The engine could not act at all: nothing may be presented as done,
            # and the request must not be handed to a different control engine.
            tail.append("Do not claim the task was done and do not retry it through a "
                        "different control engine. Tell the user what is missing.")
        else:
            tail.append("The engine ran but did not confirm the outcome. Say what it "
                        "actually did and what remains unverified; never present it as "
                        "finished. Retry only if the action is safe to repeat.")
    return head + "".join(tail)


# ── router status ─────────────────────────────────────────────────────────────

def backends_status(refresh: bool = False) -> dict[str, dict]:
    """Live availability of every engine (adapters cache their own probes)."""
    from capabilities import agent_reach, agent_tars
    out = {}
    for key, module in (("agent_tars", agent_tars), ("agent_reach", agent_reach)):
        try:
            out[key] = module.status()
        except Exception as exc:
            out[key] = {"name": key, "available": False,
                        "detail": f"probe failed: {type(exc).__name__}: {exc}",
                        "missing": [str(exc)[:120]]}
    out.update(local_status())
    return out


def local_status() -> dict[str, dict]:
    """Live state of the three local controllers (real probes, no guessing)."""
    from capabilities import config
    out: dict[str, dict] = {}
    probes = (
        ("desktop", "desktop"),
        ("playwright", "browser_pw"),
        ("uitars_visual", "uitars"),
        ("power_control", "power"),
    )
    for key, module_name in probes:
        try:
            import importlib
            module = importlib.import_module(f"capabilities.{module_name}")
            st = dict(module.status())
        except Exception as exc:
            st = {"available": False,
                  "detail": f"probe failed: {type(exc).__name__}: {exc}"}
        st.setdefault("name", key)
        st.setdefault("usable", st.get("available"))
        st["enabled"] = config.controller_enabled(_CONTROLLER_SWITCH.get(key, key))
        out[key] = st
    return out


#: Local controller probe name → the Settings switch that gates it.
_CONTROLLER_SWITCH = {"desktop": "physical_input", "playwright": "playwright_browser",
                      "uitars_visual": "uitars_visual",
                      "power_control": "power_control"}


def report() -> str:
    """Human/Gemini-readable capability sheet for skill_query and diagnostics."""
    st = backends_status()
    lines = ["CONTROL CAPABILITIES"]
    tars = st["agent_tars"]
    if tars.get("version") and not tars.get("usable"):
        lines.append(f"- computer_control / browser_control: Agent TARS "
                     f"{tars['version']} installed, automation OFF — "
                     f"{'; '.join(tars.get('missing') or ['not ready'])}")
    else:
        lines.append(f"- computer_control / browser_control: Agent TARS "
                     f"{'ready' if tars.get('usable') else 'unavailable'}"
                     + (f" ({tars.get('version')})" if tars.get("version") else "")
                     + f", model {tars.get('provider', '?')}/{tars.get('model', '?')}, "
                       f"browser {tars.get('browser_control', '?')}")
    ar = st["agent_reach"]
    active = ar.get("active_channels") or []
    lines.append(f"- web_research: Agent Reach "
                 f"{'ready' if ar.get('available') else 'unavailable'}"
                 + (f" {ar.get('version')}" if ar.get("version") else "")
                 + (f", live channels: {', '.join(active)}" if active else ""))
    # ASCII only: this text is printed by console tools whose encoding is not
    # guaranteed to be UTF-8.
    for key, headline in (("desktop", "pc_control (pywinauto then pyautogui)"),
                          ("playwright", "browser_action (Playwright)"),
                          ("uitars_visual", "visual_control (UI-TARS on Gemini)")):
        info = st.get(key) or {}
        state = ("ready" if info.get("usable") else
                 "switched off in Settings" if info.get("enabled") is False else
                 "unavailable")
        lines.append(f"- {headline}: {state}"
                     + (f" - {info.get('detail')}" if info.get("detail") else ""))
    locks_now = {k: v for k, v in locks.status().items() if v.get("locked")}
    lines.append("- ownership: " + (", ".join(
        f"{k} held by {v.get('holder')}" for k, v in locks_now.items())
        or "no control engine is active"))
    return "\n".join(lines)


def shutdown(reason: str = "shutdown") -> None:
    """Release every lock and stop every engine worker this process owns."""
    from capabilities import proc
    try:
        from capabilities import tars_server
        tars_server.stop(reason)
    except Exception:
        pass
    try:
        from capabilities import browser_pw
        browser_pw.close(reason)
    except Exception:
        pass
    freed = locks.release_all(reason)
    killed = proc.terminate_all(reason)
    _log("INFO", "router", f"shutdown: released {freed} lock(s), "
                          f"terminated {killed} owned worker(s)")
