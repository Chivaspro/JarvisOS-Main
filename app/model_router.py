"""Model router — one place that knows which model does what (master spec §6/§7).

Every Gemini call site used to hard-code a model string ("gemini-flash-latest",
"gemini-flash-lite-latest", …). That meant no way to promote a new model, no
fallback when a model rate-limits, and no difference between a planner that
needs to think and a summariser that needs to be quick.

Roles (master spec §6): DEFAULT, FAST, REASONING, CODING, VISION, EMBEDDING,
FALLBACK — plus IMAGE for image generation. Each role resolves to a concrete
model id; Settings → AI → MODEL ROLES can override any of them (config key
``model_roles``). An override that names a model the SDK can't see is treated
like any other failure: it is benched and the fallback role takes over.

Failure handling (§6/§58) — never retry the same thing blind:

    classify_error(exc) → rate_limit | auth | timeout | context_overflow |
                          quota | provider | unknown
    note_result(model, ok, err) → benched for a cooldown on failure,
                                   cleared on success
    generate(client, …) → attempt 1: role model
                          attempt 2+: another healthy model (fallback family)
                          auth errors raise immediately — another model
                          cannot fix a bad key.

The router never talks to the network itself; it only picks ids. Call sites
keep their own client construction (`genai.Client(...)` or the live session),
so a router bug can never take the voice session down.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# The default map. "fallback" is the safe substitute tried when the role model
# is benched; it must be a cheap, high-availability model.
DEFAULTS: dict[str, str] = {
    "default":   "gemini-flash-latest",
    "fast":      "gemini-flash-lite-latest",
    "reasoning": "gemini-flash-latest",
    "coding":    "gemini-flash-latest",
    "vision":    "gemini-flash-latest",
    "embedding": "gemini-embedding-001",
    "image":     "gemini-2.5-flash-image",
    "fallback":  "gemini-flash-lite-latest",
}
ROLES = tuple(DEFAULTS)

ROLE_HINTS: dict[str, str] = {
    "default":   "Everyday questions and tool summaries. Blank = gemini-flash-latest.",
    "fast":      "High-frequency light work: search digests, captions, session summaries.",
    "reasoning": "Planners that decompose multi-step work (dev agent planner).",
    "coding":    "Code writing, patches and code review (dev agent writer, code helper).",
    "vision":    "Understanding screenshots and images (desktop awareness, file vision).",
    "embedding": "Embeddings for memory similarity (used by future retrieval work).",
    "image":     "Image generation (generate_image tool).",
    "fallback":  "Tried automatically when the role model fails or is rate-limited.",
}

# Role aliases call sites already use in their own vocabulary.
_ALIASES = {
    "planner": "reasoning", "plan": "reasoning",
    "writer": "coding", "code": "coding",
    "summary": "fast", "summarize": "fast", "summariser": "fast",
    "picture": "image", "draw": "image",
    "embed": "embedding",
    "normal": "default", "main": "default",
}

# ── Laya task classification → routing (change: laya-ai-gemini) ─────────────
# The local decision AI (core/laya_decision.py) labels every incoming request
# with one of the routed categories before a downstream system is chosen.
# The router consumes that label ADVISORILY: it picks which model role serves
# the work (and which tool family the activity monitor names), while Gemini
# stays the general-purpose provider and the safe fallback for anything Laya
# cannot decide (disabled, model missing, inference failure, CHAT).
TASK_CATEGORY_ROLES: dict[str, str] = {
    "SEARCH":   "fast",
    "CODE":     "coding",
    "REVIEW":   "reasoning",
    "VIEW":     "vision",
    "FIND":     "fast",
    "GENERATE": "image",
    "ANALYZE":  "reasoning",
    "AUTOMATE": "default",
    "CHAT":     "default",
}

# Human-readable downstream systems per category (tool/agent layer view).
TASK_CATEGORY_TARGETS: dict[str, str] = {
    "SEARCH":   "web_search",
    "CODE":     "coding_agent",
    "REVIEW":   "ai_review",
    "VIEW":     "browser_vision",
    "FIND":     "lookup",
    "GENERATE": "generation",
    "ANALYZE":  "file_vision_analysis",
    "AUTOMATE": "pc_automation",
    "CHAT":     "gemini_general",
}


def role_for_category(category: str | None) -> str:
    """Model role a Laya category routes to; unknown/None → default."""
    return TASK_CATEGORY_ROLES.get(
        str(category or "").strip().upper(), "default")


def target_for_category(category: str | None) -> str:
    """Downstream system name a Laya category routes to (advisory)."""
    return TASK_CATEGORY_TARGETS.get(
        str(category or "").strip().upper(), "gemini_general")


def classify_request(text: str, *, received_at: float | None = None) -> dict | None:
    """Ask the local decision AI about *text* and resolve its answer into a
    concrete model. Returns None when Laya is off/unavailable — callers then
    keep their existing routing. This is the ONLY routing path that consults
    Laya; nothing here can take the voice session down.

    *received_at* (monotonic) is forwarded to the decision layer so the routing
    latency it records starts when the request arrived, not when the model was
    called."""
    try:
        from core import laya_decision
        outcome = laya_decision.classify_task(text, received_at=received_at)
    except Exception:
        return None
    if not outcome:
        return None
    role = role_for_category(outcome.get("category"))
    outcome["role"] = role
    outcome["target"] = target_for_category(outcome.get("category"))
    outcome["model"] = healthy_model_for(role)
    return outcome

_COOLDOWN_S = 120.0        # how long a failing model is benched
_MAX_EVENTS = 24

_lock = threading.Lock()
_benched: dict[str, float] = {}            # model → monotonic time until benched
_events: deque = deque(maxlen=_MAX_EVENTS) # newest-last failure/success records


def _normalise(role: str | None) -> str:
    r = str(role or "default").strip().lower().replace("-", "_").replace(" ", "_")
    r = _ALIASES.get(r, r)
    return r if r in DEFAULTS else "default"


def _load_cfg() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve(role: str = "default") -> str:
    """Concrete model id for a role: config override wins, then the default."""
    r = _normalise(role)
    override = (_load_cfg().get("model_roles") or {}).get(r)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return DEFAULTS[r]


def is_benched(model: str) -> bool:
    with _lock:
        until = _benched.get(model, 0.0)
    return until > time.monotonic()


def healthy_model_for(role: str = "default", exclude: tuple = ()) -> str:
    """Role model, unless it is benched/excluded — then the fallback role model."""
    primary = resolve(role)
    if not is_benched(primary) and primary not in exclude:
        return primary
    fb = resolve("fallback")
    if fb != primary and not is_benched(fb) and fb not in exclude:
        return fb
    # Everything sensible is benched: return the least-bad primary anyway.
    # A cooling-down model is better than no answer at all.
    return primary


def classify_error(exc: BaseException | str) -> str:
    """Bucket an exception so retries can change strategy (§58)."""
    text = str(exc)
    low = text.lower()
    if "429" in text or "resource_exhausted" in low or "rate" in low and "limit" in low:
        return "rate_limit"
    if "quota" in low and "exceed" in low:
        return "quota"
    if ("401" in text or "403" in text or "api key" in low or "api_key" in low
            or "unauthenticated" in low or "permission" in low or "credential" in low):
        return "auth"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    if ("context length" in low or "input tokens" in low or "maximum" in low
            and "token" in low) or "too large" in low:
        return "context_overflow"
    if ("500" in text or "503" in text or "unavailable" in low or "internal" in low
            or "deadline" in low or "connection" in low or "network" in low):
        return "provider"
    return "unknown"


def note_result(model: str, ok: bool, error: str = "") -> None:
    """Record a call outcome. Failures bench the model for a cooldown window."""
    now = time.monotonic()
    with _lock:
        if ok:
            _benched.pop(model, None)
        else:
            _benched[model] = now + _COOLDOWN_S
        _events.append({
            "model": model, "ok": bool(ok),
            "error": str(error)[:160] if error else "",
            "t": time.strftime("%H:%M:%S"),
        })


def generate(client, *, contents, role: str = "default", config=None,
             max_attempts: int = 3):
    """Resilient generate_content: switches model between attempts.

    `client` is a google-genai Client. Returns the raw response object on
    success. Raises RuntimeError after exhausting attempts (auth failures and
    context overflow of every candidate raise earlier — changing models cannot
    fix those).
    """
    tried: list[str] = []
    last_exc: BaseException | None = None
    last_kind = "unknown"
    for attempt in range(max(1, max_attempts)):
        model = healthy_model_for(role, exclude=tuple(tried))
        if model in tried:
            break
        try:
            kwargs: dict = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            resp = client.models.generate_content(**kwargs)
            note_result(model, True)
            return resp
        except Exception as exc:  # noqa: BLE001 — classified below
            last_exc = exc
            last_kind = classify_error(exc)
            note_result(model, False, f"{last_kind}: {exc}")
            tried.append(model)
            if last_kind == "auth":
                raise RuntimeError(
                    f"Gemini rejected the API key ({exc}). Fix the key in "
                    "Settings → AI — switching models will not help."
                ) from exc
            if last_kind == "context_overflow":
                # Try the fallback once; if that is also over, give up —
                # shrinking context is the caller's job (§5).
                continue
            time.sleep(min(2 ** attempt, 4))  # brief, capped backoff
    raise RuntimeError(
        f"All models for role '{_normalise(role)}' failed ({last_kind}): {last_exc}"
    ) from last_exc


def report() -> dict:
    """What the diagnostics tool shows: roles, overrides, health, recent events."""
    cfg = _load_cfg().get("model_roles") or {}
    with _lock:
        now = time.monotonic()
        cooldowns = {
            m: round(until - now)
            for m, until in _benched.items() if until > now
        }
        events = list(_events)
    return {
        "roles": {r: resolve(r) for r in ROLES},
        "overridden": sorted(k for k, v in cfg.items()
                             if isinstance(v, str) and v.strip()),
        "benched": cooldowns,
        "recent": events[-8:],
    }


def describe_report(rep: dict) -> str:
    """One readable paragraph from report() — used by the diagnostics tool."""
    lines = []
    for role in ROLES:
        mark = "*" if role in rep.get("overridden", []) else ""
        lines.append(f"{role}={rep['roles'][role]}{mark}")
    text = "Model roles: " + ", ".join(lines) + " (* = overridden in Settings)."
    if rep.get("benched"):
        text += " Cooling down: " + ", ".join(
            f"{m} ({s}s left)" for m, s in rep["benched"].items()
        ) + "."
    return text
