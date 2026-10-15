"""The Auto-Heal sentry: catch a real traceback, decide, patch, tell the user.

It is deliberately a *layer on top of* ``core/stability.py`` instead of a
replacement: stability already installs the exception hooks that keep the app
alive and log to ``data/logs/jarvis_crash.log``. This module wraps whatever hook
is installed, so both behaviours survive — the crash is still logged, and it is
now also analysed and (per policy) repaired.

Policy comes from ``features.auto_heal_mode``:

* ``auto_low_risk`` (default) — a leaf-capability file (``actions/``,
  ``plugins/``, ``evolution/`` …) is patched automatically, because a bad patch
  there cannot stop the assistant from starting; anything else is proposed and
  waits for the on-screen confirmation gate.
* ``propose`` — nothing is ever written without approval.
* ``auto`` — patch any non-protected file (still backed up, still rollback-able).
* ``off`` / ``self_heal`` false — detect and report only.

Every event is published through one callback (``set_event_handler``), which
main.py wires to the HUD, so the repair telemetry card shows real state: the
exception, the file, the explanation, the patch id, the outcome.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from recovery import analyzer, patcher

__all__ = [
    "configure",
    "handle_now",
    "install",
    "last_error",
    "record",
    "set_event_handler",
    "status",
]

_lock = threading.Lock()
_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=32)
_worker: threading.Thread | None = None
_event_handler: Callable[[str, dict], None] | None = None
_log: Callable[[str], None] | None = None
_last_error: str = ""
_installed = False
_session_patches = 0
_last_outcome: dict[str, Any] = {}

#: Never patch the same line twice in a row, and never more than this many
#: automatic patches in one session — a repair loop is worse than the bug.
_recent: dict[str, float] = {}
_REPEAT_WINDOW_S = 300.0
_MAX_AUTO_PER_SESSION = 5


# ── plumbing ──────────────────────────────────────────────────────────────────

def configure(*, ui=None, on_event: Callable[[str, dict], None] | None = None,
              log: Callable[[str], None] | None = None) -> None:
    """Bind the HUD + log sinks (called once from main.py)."""
    global _event_handler, _log
    if on_event is not None:
        _event_handler = on_event
    if log is not None:
        _log = log
    elif ui is not None and hasattr(ui, "write_log"):
        _log = ui.write_log


def set_event_handler(callback: Callable[[str, dict], None] | None) -> None:
    global _event_handler
    _event_handler = callback


def _emit(kind: str, payload: dict[str, Any]) -> None:
    handler = _event_handler
    if handler is None:
        return
    try:
        handler(kind, payload)
    except Exception:
        pass


def _say(text: str) -> None:
    if _log is None:
        return
    try:
        _log(str(text)[:400])
    except Exception:
        pass


def _mode() -> str:
    get_feature = None
    try:
        from config.settings import get_feature
    except Exception:
        get_feature = None
    try:
        if get_feature is not None and not bool(get_feature("self_heal", True)):
            return "off"
        mode = str(get_feature("auto_heal_mode", "auto_low_risk") if get_feature
                   else "auto_low_risk").strip().lower()
    except Exception:
        mode = "auto_low_risk"
    return mode if mode in ("auto_low_risk", "propose", "auto", "off") else "auto_low_risk"


def last_error() -> str:
    """The most recent captured traceback ("" when none)."""
    return _last_error


def status() -> dict[str, Any]:
    rows = patcher.history(limit=5)
    return {
        "mode": _mode(),
        "captured": bool(_last_error),
        "last_error": _last_error[-2000:],
        "session_patches": _session_patches,
        "history": rows,
        "last_outcome": dict(_last_outcome),
    }


# ── detection ─────────────────────────────────────────────────────────────────

def install(ui=None) -> None:
    """Chain the heal sentry onto the process-wide exception hooks. Idempotent."""
    global _installed, _worker
    if _installed:
        if ui is not None and _log is None and hasattr(ui, "write_log"):
            configure(ui=ui)
        return
    _installed = True
    try:
        previous_excepthook = sys.excepthook

        def _hook(exc_type, exc, tb, _previous=previous_excepthook):
            try:
                _previous(exc_type, exc, tb)      # stability keeps its log
            except Exception:
                pass
            try:
                import traceback as _tb
                record("".join(_tb.format_exception(exc_type, exc, tb)),
                       source="unhandled")
            except Exception:
                pass

        sys.excepthook = _hook
    except Exception:
        pass

    try:
        previous_thread_hook = threading.excepthook

        def _thread_hook(args, _previous=previous_thread_hook):
            try:
                _previous(args)
            except Exception:
                pass
            try:
                import traceback as _tb
                record("".join(_tb.format_exception(args.exc_type, args.exc_value,
                                                    args.exc_traceback)),
                       source=f"thread:{getattr(args.thread, 'name', '?')}")
            except Exception:
                pass

        threading.excepthook = _thread_hook
    except Exception:
        pass

    _worker = threading.Thread(target=_loop, name="auto-heal", daemon=True)
    _worker.start()
    if ui is not None:
        configure(ui=ui)


def record(traceback_text: str, *, source: str = "runtime",
           request: str = "") -> dict[str, Any]:
    """Remember one real traceback and queue it for analysis (never blocks)."""
    global _last_error
    text = str(traceback_text or "").strip()
    if not text:
        return {"ok": False, "message": "no traceback"}
    with _lock:
        _last_error = text[-8000:]
    job = {"text": text[-8000:], "source": source, "request": request,
           "queued_at": time.time()}
    try:
        _queue.put_nowait(job)
    except Exception:
        # Queue full: drop the oldest instead of growing without bound.
        try:
            _queue.get_nowait()
            _queue.put_nowait(job)
        except Exception:
            pass
    return {"ok": True, "message": "captured"}


def _loop() -> None:
    while True:
        try:
            job = _queue.get()
        except Exception:
            time.sleep(0.5)
            continue
        try:
            handle_now(job.get("text", ""), source=job.get("source", "runtime"),
                       request=job.get("request", ""))
        except Exception:
            continue


# ── the decision + repair ─────────────────────────────────────────────────────

def _auto_allowed(risk: str, mode: str) -> bool:
    if mode == "off" or mode == "propose":
        return False
    if risk == "protected":
        return False
    if mode == "auto":
        return True
    return risk == "low"


def handle_now(traceback_text: str, *, source: str = "manual", request: str = "",
               force: bool = False) -> dict[str, Any]:
    """Analyse one traceback and act according to the current policy."""
    global _session_patches, _last_outcome

    parsed = analyzer.parse(traceback_text)
    if not parsed.get("found"):
        outcome = {"ok": False, "stage": "analyze", "parsed": parsed,
                   "message": ("I read that traceback but found no first-party file "
                               "of mine to patch, sir.")}
        _last_outcome = outcome
        _emit("repair_analyzed", outcome)
        return outcome

    key = f"{parsed['file']}:{parsed['line']}"
    now = time.time()
    if not force and (now - float(_recent.get(key, 0.0))) < _REPEAT_WINDOW_S:
        outcome = {"ok": False, "stage": "dedup", "parsed": parsed,
                   "message": (f"I already looked at {Path(parsed['file']).name} line "
                               f"{parsed['line']} a moment ago, sir — I will not patch "
                               f"it again yet.")}
        _last_outcome = outcome
        return outcome

    mode = _mode()
    _emit("repair_detected", {
        "parsed": parsed, "source": source, "request": request, "mode": mode,
        "traceback": parsed.get("traceback", ""),
    })

    proposal = patcher.propose(traceback_text, parsed=parsed, context_notes=request)
    if not proposal.get("ok"):
        outcome = {"ok": False, "stage": "propose", "parsed": parsed,
                   "message": proposal.get("message", "no patch")}
        _last_outcome = outcome
        _emit("repair_failed", outcome)
        return outcome

    _recent[key] = now
    allowed = _auto_allowed(str(proposal.get("risk") or "high"), mode)
    if allowed and _session_patches >= _MAX_AUTO_PER_SESSION:
        allowed = False
        proposal["explanation"] = (str(proposal.get("explanation") or "")
                                   + " (auto-patch budget for this session is used up)")

    _emit("repair_proposed", {
        "parsed": parsed, "proposal": {k: v for k, v in proposal.items()
                                       if k != "staged"},
        "auto": allowed, "mode": mode,
    })

    if not allowed:
        outcome = {
            "ok": True, "stage": "proposed", "auto": False, "mode": mode,
            "parsed": parsed, "proposal": proposal,
            "message": (f"I found the fault in {Path(parsed['file']).name} line "
                        f"{parsed['line']} and prepared a patch: "
                        f"{proposal.get('explanation')} Say the word and I will apply "
                        f"it."),
        }
        _last_outcome = outcome
        return outcome

    applied = patcher.apply_patch(proposal)
    if applied.get("ok"):
        _session_patches += 1
    outcome = {
        "ok": bool(applied.get("ok")),
        "stage": "applied" if applied.get("ok") else "apply_failed",
        "auto": True, "mode": mode, "parsed": parsed,
        "proposal": {k: v for k, v in proposal.items() if k != "staged"},
        "patch_id": applied.get("patch_id", ""),
        "message": applied.get("message", ""),
    }
    _last_outcome = outcome
    _emit("repair_applied" if applied.get("ok") else "repair_failed", outcome)
    _say(("SYS: auto-heal — " if applied.get("ok") else "SYS: auto-heal failed — ")
         + str(outcome.get("message") or "")[:200])
    return outcome


def approve_last() -> dict[str, Any]:
    """Apply the most recent proposal that is still waiting for approval."""
    proposal = (_last_outcome or {}).get("proposal")
    if not proposal or not proposal.get("ok"):
        return {"ok": False, "message": "There is no pending repair proposal, sir."}
    global _session_patches
    applied = patcher.apply_patch(proposal)
    if applied.get("ok"):
        _session_patches += 1
    _last_outcome["stage"] = "applied" if applied.get("ok") else "apply_failed"
    _last_outcome["patch_id"] = applied.get("patch_id", "")
    _emit("repair_applied" if applied.get("ok") else "repair_failed", _last_outcome)
    return applied
