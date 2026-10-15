"""Guarded bridge between the running app (ui.py / main.py) and the Brain.

Every function here degrades gracefully to a safe no-op when the Brain cannot
be built (missing deps, locked sqlite file, model import failure) — importing
this module can never crash the assistant. Lazy: nothing is loaded until the
first call, and heavy optional packages (sentence-transformers) are skipped via
JARVIS_NO_EMBEDDINGS or an import failure.
"""

from __future__ import annotations

import json
import threading
import time

# NB: `json` is required — feed_wake_audio() read the config with json.loads and
# the missing import was swallowed by its blanket except, so the wake feed
# always reported "disabled" instead of raising.

_brain = None
_main_lock = threading.Lock()
_boot_try: float | None = None


def get_brain():
    """Singleton Brain (thread-safe, lazy). Returns None if it fails once."""
    global _brain, _boot_try
    if _brain is not None:
        return _brain
    with _main_lock:
        if _brain is not None:
            return _brain
        # Don't hammer a failing boot on every call-site; retry once a minute max.
        now = time.time()
        if _boot_try is not None and _boot_try is not None and (now - _boot_try) < 60:
            return None
        _boot_try = now
        try:
            from brain import get_brain as _get_brain

            _brain = _get_brain()
        except Exception as exc:
            print(f"[Brain] ⚠️ brain unavailable: {exc}")
            _brain = None
        return _brain


def current_features() -> dict:
    """Live feature flags from config/api_keys.json (never raises).

    Skill availability is derived from these, so revoking microphone or camera
    access in Settings disables the matching skill immediately instead of the
    model cheerfully offering it."""
    try:
        from pathlib import Path
        cfg = _read_json(Path(__file__).resolve().parents[1]
                         / "config" / "api_keys.json")
        feats = cfg.get("features", {})
        return feats if isinstance(feats, dict) else {}
    except Exception:
        return {}


def _read_json(path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_skill_block_cache: tuple[float, str] | None = None


def skill_block() -> str:
    """The skill map injected into the system instruction.

    Cached for a minute: it only changes when the user toggles permissions, and
    a session rebuild should not re-parse the config for every connect."""
    global _skill_block_cache
    now = time.time()
    if _skill_block_cache is not None and (now - _skill_block_cache[0]) < 60.0:
        return _skill_block_cache[1]
    text = ""
    try:
        from brain import skills as _skills
        text = _skills.prompt_block(features=current_features())
    except Exception:
        text = ""
    _skill_block_cache = (now, text)
    return text


def task_block() -> str:
    """Live multi-step task state (empty string when nothing is in flight)."""
    try:
        from brain.tasks import get_task_tracker
        return get_task_tracker().context_block()
    except Exception:
        return ""


# ── orchestrator step execution (brain/graph.py contract) ──────────────────────
# The LangGraph/pure-Python Orchestrator needs one thing the deterministic path
# already has but it did not: a way to run a single step through the REAL tool
# layer and to ask whether the step's outcome was verified. The running app
# (JarvisLive) registers those two callables at startup; before then — or in a
# headless/test process — a step honestly reports itself unverified rather than
# fake a pass. graph.py never imports the UI, so this indirection is what lets
# the orchestrator share the app's actual dispatch without a circular import.

_step_runner = None
_step_verifier = None


def register_orchestrator_hooks(runner, verifier=None) -> None:
    """Wire the live tool dispatcher into the orchestrator (idempotent).

    ``runner(step, ctx) -> observation dict`` must return
    ``{ok, verified, result, error}``; it must NEVER raise into the caller.
    """
    global _step_runner, _step_verifier
    _step_runner = runner if callable(runner) else None
    _step_verifier = verifier if callable(verifier) else None


def run_orchestrator_step(step, ctx: dict | None = None) -> dict:
    """Execute one orchestrator step through the live dispatcher (never raises).

    Returns an observation dict. When no runner is registered the observation
    is an explicit non-success, so the orchestrator's verifier refuses to call
    it a pass (spec: an unrun step is never a completed step).
    """
    if _step_runner is None:
        return {"ok": False, "verified": False,
                "error": "no live tool runner is registered (headless/orphan step)"}
    try:
        obs = _step_runner(step, ctx or {})
    except Exception as exc:
        return {"ok": False, "verified": False,
                "error": f"step runner raised {type(exc).__name__}: {exc}"}
    if not isinstance(obs, dict):
        obs = {"ok": bool(obs), "result": obs}
    obs.setdefault("verified", bool(obs.get("ok")))
    return obs


def verify_orchestrator_step(step, ctx: dict | None = None,
                             observation: dict | None = None) -> bool:
    """Ask the app's verifier whether a step truly achieved its intent."""
    if _step_verifier is None:
        # No dedicated verifier: trust only an explicit, evidence-carrying
        # observation the runner already marked verified.
        return bool((observation or {}).get("verified"))
    try:
        return bool(_step_verifier(step, ctx or {}, observation or {}))
    except Exception:
        return False


def handle_skill_query(args: dict | None = None) -> str:
    """Backend for the skill_query tool (what can you do / which skill is this)."""
    try:
        from brain import skills as _skills
        return _skills.handle_query(args)
    except Exception as exc:
        return f"Skill lookup unavailable: {exc}"


def handle_task_command(args: dict | None = None) -> str:
    """Backend for the task_plan tool (multi-step task memory)."""
    try:
        from brain.tasks import handle_task_command as _handle
        return _handle(args)
    except Exception as exc:
        return f"Task memory unavailable: {exc}"


def augment_system_prompt(parts: list, request: str = "") -> list:
    """Append the Brain's context block (personality + honesty hint), the live
    skill map and any in-flight task state to a list of prompt parts.

    ``request`` is the user's current text whenever the caller has one: recall
    is assembled against it, so the prompt carries what THAT question needs.
    Returns the same list; every step is individually guarded so a broken
    subsystem degrades to a slightly smaller prompt, never to a failed connect."""
    try:
        b = get_brain()
        if b is not None:
            block = b.build_context_block(request)
            if block:
                parts.append(block)
    except Exception:
        pass
    try:
        sblock = skill_block()
        if sblock:
            parts.append(sblock)
    except Exception:
        pass
    try:
        tblock = task_block()
        if tblock:
            parts.append(tblock)
    except Exception:
        pass
    # Persistent-conversation block: real archive excerpts ("what were we
    # working on" survives restarts) plus the hard forgotten-topics guard so
    # an explicitly deleted subject never rides back in on old context.
    try:
        from memory.service import get_memory_service
        svc = get_memory_service()
        if svc is not None:
            pblock = svc.build_startup_context()
            if pblock:
                parts.append(pblock)
            # The operation rules stand alone: they shape memory-command
            # replies even before any archive exists.
            parts.append(svc.command_directive())
    except Exception:
        pass
    return parts


def memory_system_prompt(request: str = "") -> str:
    """Persona + everything JARVIS must know, for one plain provider call.

    The typed path talks straight to an HTTP provider; without this it would
    answer with no personality, no memory and no archive. Never raises — an
    empty string simply means the provider keeps its own default."""
    try:
        blocks = [str(p) for p in augment_system_prompt([], request=request) if p]
        return "\n\n".join(blocks)
    except Exception:
        return ""


def observe_utterance(text: str) -> None:
    """Learn from a user's finished utterance (personality + activity).

    Also routes the text through the skill registry and remembers the result on
    the Brain, so the selected skills are deterministic, inspectable and shared
    with the skill_query tool instead of living only inside the model. The
    utterance is appended to the conversation ledger too, which is what makes
    "this / that / the other one" resolvable on the next turn.
    """
    if not text:
        return
    try:
        b = get_brain()
        if b is None:
            return
        try:
            b.observe(text)
        except Exception:
            pass
        try:
            b.note_turn("user", text)
        except Exception:
            pass
        try:
            b.route_skills(text, features=current_features(), limit=4)
        except Exception:
            pass
        # Archive the finished user turn to the persistent conversation store,
        # so "what were we working on" survives a restart. Best-effort.
        try:
            from memory.service import get_memory_service
            svc = get_memory_service()
            if svc is not None:
                svc.persist_turn("user", text)
        except Exception:
            pass
        # Feedback commands ("remember...", "forget...") handled in the backend
        # by main.py's command interceptor, not only by the model's tool choice.
        # A deferral ("...but let's do it tomorrow") is not a command at all —
        # but it is exactly the sentence that creates tomorrow's open thread, so
        # it is recorded here, where both the typed and the spoken paths pass.
        try:
            from memory.service import get_memory_service, is_deferral
            if is_deferral(text):
                svc = get_memory_service()
                if svc is not None:
                    svc.remember_deferred(text)
        except Exception:
            pass
        # Durable statements reach memory in EVERY mode — original store and
        # Obsidian Vault alike. The promotion policy is the only gate, and it
        # is conservative by design, so chatter cannot leak in. An explicit
        # "remember ..." command is already stored by its own handler, so it is
        # skipped here to avoid counting the same fact twice.
        try:
            from memory.service import detect_memory_command
            explicit_command = detect_memory_command(text) is not None
        except Exception:
            explicit_command = False
        if not explicit_command:
            try:
                from memory import policy as _memory_policy
                outcome = _memory_policy.promote(text) or {}
                # The active store received it. Mirror it into the original
                # memory as well, so switching backends never loses what the
                # conversation taught: the Memory tab and the vault agree.
                if outcome.get("stored"):
                    from memory.backend import (BACKEND_ORIGINAL, LocalMemoryBackend,
                                                active_backend_name)
                    if active_backend_name() not in (BACKEND_ORIGINAL, "local"):
                        LocalMemoryBackend().write(text, explicit=False)
            except Exception:
                pass
    except Exception:
        pass


def record_reply(text: str) -> None:
    """Append JARVIS's own finished reply to the conversation ledger.

    The reply matters: it names the file/website/object that was just handled,
    and it is what "the same thing again" refers back to.
    """
    if not text:
        return
    try:
        b = get_brain()
        if b is None:
            return
        b.note_turn("assistant", text)
    except Exception:
        pass
    try:
        from memory.service import get_memory_service
        svc = get_memory_service()
        if svc is not None:
            svc.persist_turn("assistant", text)
    except Exception:
        pass


def resolve_reference(text: str) -> dict:
    """Resolve deictic references against the conversation ledger.

    Returns `{"references": [...], "notes": [...], "summary": str}`; empty
    fields mean nothing concrete was recognised (the caller must not guess).
    """
    try:
        b = get_brain()
        if b is None:
            return {"references": [], "notes": [], "summary": ""}
        return b.resolve(text)
    except Exception:
        return {"references": [], "notes": [], "summary": ""}


def note_entity(kind: str, label: str, **kw) -> None:
    """Record a file/url/object/app the assistant is now working with."""
    try:
        b = get_brain()
        if b is not None:
            b.note_entity(kind, label, **kw)
    except Exception:
        pass


def note_objects(objects) -> None:
    """Record a fresh detection scan (labels + left/right position)."""
    try:
        b = get_brain()
        if b is not None:
            b.note_objects(objects)
    except Exception:
        pass


def plan_request(text: str) -> dict:
    """Deterministic skill plan for a request (advisory, offline)."""
    try:
        b = get_brain()
        if b is not None:
            return b.plan(text, features=current_features())
    except Exception:
        pass
    return {"skills": [], "chain": [], "steps": []}


def learn_tool_outcome(name: str, ok: bool, detail: str = "") -> None:
    """Feed tool success/failure into the learning engine (preferred tools)."""
    try:
        b = get_brain()
        if b is None:
            return
        method = str(getattr(b, "_last_routed_method", "")) or name
        request = detail or name
        ms = 0.0
        if ok:
            b.learning.record_success(request=request, intent=f"tool_{name}",
                                      tool=method, ms=ms, steps=None)
        else:
            b.learning.record_failure(request=request, intent=f"tool_{name}",
                                      tool=method, ms=ms, error=request, fallback=None)
    except Exception:
        pass


def _wake_config() -> dict:
    """Live wake configuration, read from the file Settings writes.

    Settings stores the master switch at ``features.wake_word`` and the phrases
    at ``wake_words``. The old lookup only checked a top-level ``wake_enabled``
    key that Settings never wrote, so the detector was never built: the "Wake
    word" switch, the extra wake words and the sensitivity all did nothing.
    """
    from pathlib import Path
    cfg = _read_json(Path(__file__).resolve().parents[1] / "config" / "api_keys.json")
    feats = cfg.get("features") if isinstance(cfg.get("features"), dict) else {}
    return {
        "wake_words": cfg.get("wake_words") or [],
        "wake_sensitivity": cfg.get("wake_sensitivity", 0.5),
        # Only a real keyword model counts as a wake word. The energy-based
        # speech fallback is opt-in, because it cannot tell "Jarvis" from any
        # loud sentence and would report a wake on ordinary speech.
        "wake_fallback": str(cfg.get("wake_fallback", "off") or "off"),
        "features": feats,
    }


def wake_enabled() -> bool:
    """Is the local wake detector allowed to run right now?

    Honours the Settings switches: the wake word must be on, the detector is a
    background listener by definition, and push-to-talk turns hands-free
    listening off by design.
    """
    try:
        cfg = _wake_config()
        feats = cfg.get("features") or {}
        if "wake_word" not in feats:
            # Legacy config with no Settings-written flag.
            return bool(_read_json(Path(__file__).resolve().parents[1]
                                   / "config" / "api_keys.json").get("wake_enabled", False))
        if not bool(feats.get("wake_word")):
            return False
        if not bool(feats.get("background_listening", True)):
            return False
        if bool(feats.get("push_to_talk", False)) and not bool(feats.get("hands_free", True)):
            return False
        return True
    except Exception:
        return False


def feed_wake_audio(samples, *, force: bool | None = None) -> bool:
    """Non-invasive wake-word feed. Takes optional pre-computed PCM bytes; when
    `force` is True the detector pumps even if app-level mute states its owner
    normally gates on. Returns True when a wake word fired (barge-in request).
    No-op (False) when wake is disabled/unavailable."""
    try:
        if force is None and not wake_enabled():
            return False
        from core.wake import get_wake_manager

        mgr = get_wake_manager()
        try:
            # Install the live reader BEFORE ensure() builds the detector, so
            # the detector is created with the configured words/sensitivity.
            mgr.set_config_provider(_wake_config)
        except Exception:
            pass
        det = mgr.ensure()
        if det is None:
            return False
        if not det.active():
            mgr.start()
        return bool(det.feed(samples))
    except Exception:
        return False


def reconfigure_wake(*, words: list[str] | None = None,
                     sensitivity: float | None = None) -> bool:
    """Apply new wake words/sensitivity from Settings without a restart.
    Safe to call any time: no-op when wake is disabled or the detector is
    still being built. Returns True when a live detector was updated."""
    try:
        from core.wake import get_wake_manager
        mgr = get_wake_manager()
        try:
            mgr.set_config_provider(_wake_config)
        except Exception:
            pass
        mgr.reconfigure(words=words, sensitivity=sensitivity)
        return mgr._detector is not None
    except Exception:
        return False


def set_wake_barge_in(fn) -> None:
    """Hook an interrupt function onto the shared wake detector so a detected
    wake word stops JARVIS mid-speech (spec §23)."""
    try:
        from core.wake import get_wake_manager

        mgr = get_wake_manager()
        det = mgr.ensure()
        if det is not None:
            det.set_interrupt(fn)
    except Exception:
        pass


__all__ = [
    "get_brain",
    "wake_enabled",
    "augment_system_prompt",
    "observe_utterance",
    "record_reply",
    "resolve_reference",
    "note_entity",
    "note_objects",
    "plan_request",
    "learn_tool_outcome",
    "feed_wake_audio",
    "set_wake_barge_in",
    "reconfigure_wake",
    "current_features",
    "skill_block",
    "task_block",
    "handle_skill_query",
    "handle_task_command",
]