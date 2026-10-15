"""Laya AI — the local decision service (Settings section: "Laya AI").

Laya is a 322M-parameter non-autoregressive "System 1" model that runs on CPU
and answers ONE question about each user request: which kind of task is this?
The eight routed categories (SEARCH, CODE, REVIEW, VIEW, FIND, GENERATE,
ANALYZE, AUTOMATE) plus CHAT mirror the dashboard's activity rows one-for-one,
so a classification is directly displayable and directly consumable by the AI
task router (``app/model_router.py`` maps a category to the role/downstream
system that should serve it).

Design rules (change: laya-ai-gemini):

* Laya is an ADVISORY local decision layer — it classifies and routes; it
  never generates answers and never replaces Gemini. Every call site keeps
  working when Laya is disabled, missing, or broken.
* Nothing here may raise into the caller. Failures are logged through the
  project's observability logger and surfaced as an accurate status string.
* Model availability is REAL detection on disk (the same file manifest the
  laya library validates: rl_agent_config.json, model.safetensors,
  tokenizer/*, encoder/*) — never a hard-coded "installed" flag.
* Persistence lives in the one config store the whole app already uses
  (config/api_keys.json), written through the same guarded path the Settings
  window uses (core.config_guard.save_with_history).

Config shape (config/api_keys.json):

    "features": { "laya_ai": true }            # live enable switch
    "laya": {
        "enabled": true,                        # restored on boot when saved as default
        "save_as_default": false,               # "Save as Default" toggle
        "model_dir": "models/laya-multilingual" # last detected directory
    }Latency rules (change: laya-decision-speed) — measured on this machine with the
real checkpoint and real forward passes, never guessed:

    threads   cold load   one decision (avg / worst of 5, same inputs)
    2         21.2 s      1.14 s / 1.59 s   ← the old min(2, logical // 2) cap
    3         21.8 s      0.89 s / 1.06 s   ← what this module now uses
    4         22.2 s      0.98 s / 1.36 s   ← no better than 3
    same words twice (cache hit)            0.000 s

(Cold load varied 18-36 s between runs with no relationship to the thread
count — it is disk/CPU contention on this machine — so the budget is chosen on
decision latency, which it does affect.)

So the decision path is engineered around those numbers: nothing in the user's
turn ever waits for a load, the model keeps ONE logical CPU free for the UI,
audio and the Gemini session instead of being hard-capped at two threads
(which made decisions ~28% slower and the worst case ~50% slower), identical
requests are answered from a small TTL cache, only one inference runs at a
time, and every stage is timed for real (last_timing()/timing_report()).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Task vocabulary — identical to the dashboard activity rows and the criteria
# list the model was probed with (the probe script has been retired; the
# criteria are what routed correctly against the installed checkpoint).
CATEGORIES = (
    "SEARCH", "CODE", "REVIEW", "VIEW", "FIND",
    "GENERATE", "ANALYZE", "AUTOMATE", "CHAT",
)

# Candidate locations for the model, relative to the project root. The first
# one whose file manifest validates wins.
MODEL_CANDIDATES = (
    ("models", "laya-multilingual"),
    ("laya-multilingual",),
)

# The laya library itself refuses to load without exactly these pieces
# (laya.agent validates this same manifest) — so this IS the definition of a
# valid installed model; nothing is invented here.
REQUIRED_FILES = ("rl_agent_config.json", "model.safetensors")
REQUIRED_DIRS = ("tokenizer", "encoder")

# The typed question asked of the model every turn. "choice" + criteria keeps
# the output space to the categories the router understands. The criteria are
# DESCRIPTIVE (option → meaning): measured against the installed checkpoint,
# bare upper-case labels mis-route badly ("Open Chrome" → CODE at 0.97) while
# the descriptions hit SEARCH/CODE/AUTOMATE/CHAT/… correctly on every probe
# phrase. The keys stay exactly the routed CATEGORIES vocabulary.
_QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "Which action should J.A.R.V.I.S. take?",
        "criteria": {
            "SEARCH":   "look up current information, web search, news",
            "CODE":     "write, edit, fix or run code; modify a program or file",
            "REVIEW":   "review or explain existing code for quality",
            "VIEW":     "show or open something on screen: website, video, camera view",
            "FIND":     "locate or search for a file, app or item on this computer",
            "GENERATE": "create a new image, picture or media",
            "ANALYZE":  "analyze or summarize a document, data or screenshot",
            "AUTOMATE": "carry out a requested multi-step task for the user",
            "CHAT":     "general conversation, questions about the assistant itself, no action needed",
        },
    }
}

# Which downstream system serves each category (advisory routing table — the
# tool layer and the model router consume it; Gemini stays the fallback for
# everything, see Settings → AI).
CATEGORY_TARGETS = {
    "SEARCH":   "web search (actions.web_search)",
    "CODE":     "coding model / coding agent (actions.code_helper, actions.dev_agent)",
    "REVIEW":   "AI analysis (reasoning role)",
    "VIEW":     "vision / screen capture (actions.screen_process)",
    "FIND":     "search / file lookup",
    "GENERATE": "generation model / tool (image role)",
    "ANALYZE":  "file / vision analysis (actions.file_processor)",
    "AUTOMATE": "general task handling (Gemini fallback)",
    "CHAT":     "Gemini (general conversation)",
}

# Display identity of the decision model (matches the checkpoint README:
# laya-multilingual, mmBERT-base encoder, 322M params, CPU).
DECISION_AI_LABEL = "Laya 322M CPU"

STATE_DISABLED = "disabled"
STATE_NOT_FOUND = "model_not_found"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_ERROR = "error"

_lock = threading.RLock()
_agent = None                       # loaded laya Agent (heavy; created once)
_state = "uninitialized"            # one of STATE_* (or pre-init)
_state_detail = ""                  # human-readable error when relevant
_load_started = 0.0
_last_result: dict | None = None    # last real classification (activity + router)
_config_cache: tuple[float, dict] | None = None   # (mtime, parsed config)

_CONFIG_CACHE_S = 1.0               # re-read api_keys.json at most 1×/second

# ── decision-speed machinery ─────────────────────────────────────────────────
# Only ONE inference may run at a time: two stacked 2-8 s forward passes on a
# 2-core machine make every other thread (UI, mic callback, audio playback)
# wait, which is exactly what "JARVIS takes too long to decide" feels like.
# A second request arriving while the first is still running is therefore
# skipped (honestly reported as "busy") instead of queueing behind it.
_classify_gate = threading.Lock()

# Identical requests are answered from here instead of paying for another
# forward pass. Bounded and time-limited so a stale reading of the same words
# can never outlive its usefulness.
_CACHE_TTL_S = 600.0
_CACHE_MAX = 48
_decision_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_cache_lock = threading.Lock()

# Disabling Laya must free the ~1.3 GB of weights it holds — but a user testing
# the toggle (OFF → ON → OFF) must not pay 78-164 s of reloading every time.
# The loaded agent is therefore parked briefly and released for good when the
# grace window expires untouched.
_HOLD_AFTER_DISABLE_S = 300.0
_parked_agent = None
_park_timer: threading.Timer | None = None

# Real timings (never fabricated): the last decision's stage-by-stage numbers,
# a short history, and running stats. Surfaced through status()/timing_report()
# and the observability metrics, so "Laya is slow/fast" is always a measurement.
_timing_lock = threading.Lock()
_last_timing: dict | None = None
_last_skip: dict | None = None      # honest "why was Laya not consulted" reason
_timing_events: deque = deque(maxlen=12)
_timing_stats = {"decisions": 0, "inferences": 0, "cache_hits": 0, "skips": 0,
                 "total_ms": 0.0, "inference_ms": 0.0, "max_ms": 0.0,
                 "load_ms": 0.0, "warm_ms": 0.0, "threads": None}


def _note_timing(*, event: str, total_ms: float = 0.0, inference_ms: float = 0.0,
                 cache_hit: bool = False, skipped: str = "", **fields) -> dict:
    """Record one real decision-path measurement. Never raises: a diagnostics
    failure must not cost the caller their classification."""
    global _last_timing
    try:
        now = time.time()
        with _timing_lock:
            if cache_hit:
                _timing_stats["cache_hits"] += 1
            elif skipped:
                _timing_stats["skips"] += 1
            else:
                _timing_stats["decisions"] += 1
                _timing_stats["total_ms"] += total_ms
                _timing_stats["inference_ms"] += inference_ms
                _timing_stats["max_ms"] = max(_timing_stats["max_ms"], total_ms)
            _last_timing = {
                "event": event, "ts": now,
                "total_ms": round(total_ms, 1),
                "inference_ms": round(inference_ms, 1),
                "cache_hit": bool(cache_hit), "skipped": skipped or "",
            }
            _last_timing.update(
                {k: v for k, v in fields.items() if v is not None})
            _timing_events.append(dict(_last_timing))
        # Same numbers into the project's metric counters (diagnostics reads them).
        if not skipped:
            from observability.metrics import record as _record
            _record("laya", total_ms, ok=bool(fields.get("category") or cache_hit))
        return _last_timing
    except Exception:
        return {}


def last_timing() -> dict | None:
    """The most recent real decision-path measurement (or None)."""
    with _timing_lock:
        return dict(_last_timing) if _last_timing else None


def timing_stats() -> dict:
    """Running averages over real decisions this process has made."""
    with _timing_lock:
        s = dict(_timing_stats)
        n = int(s["inferences"]) or int(s["decisions"]) or 0
        dec = int(s["decisions"]) or 1
        s["avg_total_ms"] = round(s["total_ms"] / dec, 1) if dec else 0.0
        s["avg_inference_ms"] = (round(s["inference_ms"] / n, 1)
                                 if s["inferences"] else
                                 round(s["total_ms"] / dec, 1) if dec else 0.0)
        s["decisions"] = int(s["decisions"])
        s["events"] = list(_timing_events)
    return s


def timing_report() -> str:
    """Plain-text timing summary for the diagnostics tool — measured only."""
    s = timing_stats()
    if not s["decisions"] and not s["cache_hits"] and not s["skips"]:
        return ("LAYA TIMING — no decision has run yet this session "
                "(nothing measured, nothing claimed).")
    last = last_timing() or {}
    lines = [
        "LAYA TIMING  //  measured this session",
        f"- decisions: {s['decisions']} · avg total {s['avg_total_ms']:.0f} ms · "
        f"max {s['max_ms']:.0f} ms",
        f"- model load: {s['load_ms'] / 1000.0:.1f}s · warm-up: "
        f"{s['warm_ms'] / 1000.0:.1f}s · torch threads: {s.get('threads') or '—'}",
        f"- cache hits: {s['cache_hits']} · skipped (busy/not ready): {s['skips']}",
    ]
    if last:
        lines.append(
            f"- last: {last.get('event')} total={last.get('total_ms')}ms "
            f"inference={last.get('inference_ms')}ms "
            f"cache_hit={last.get('cache_hit')} "
            f"category={last.get('category') or '—'}")
    return "\n".join(lines)


def _cache_key(text: str) -> str:
    """Normalised request text (case/punctuation/spacing-insensitive)."""
    keep = "".join(ch.lower() if ch.isalnum() else " " for ch in str(text or ""))
    return " ".join(keep.split())[:220]


def _cache_get(key: str) -> dict | None:
    if not key:
        return None
    now = time.time()
    with _cache_lock:
        found = _decision_cache.get(key)
        if not found:
            return None
        ts, outcome = found
        if now - ts > _CACHE_TTL_S:
            _decision_cache.pop(key, None)
            return None
        _decision_cache.move_to_end(key)
        return dict(outcome)


def _cache_put(key: str, outcome: dict) -> None:
    if not key:
        return
    with _cache_lock:
        _decision_cache[key] = (time.time(), dict(outcome))
        _decision_cache.move_to_end(key)
        while len(_decision_cache) > _CACHE_MAX:
            _decision_cache.popitem(last=False)


#: Hard ceiling on the decision model's torch threads (see _cpu_thread_budget).
_MAX_DECISION_THREADS = 4


def _cpu_thread_budget() -> int:
    """How many CPU threads torch may use for this local decision model.

    Measured on this machine (4 logical CPUs, the real checkpoint and the real
    ``_QUESTIONS``, five identical inputs per run):

        threads   one decision (avg / worst)
        2         1.14 s / 1.59 s   ← the old min(2, logical // 2) cap
        3         0.89 s / 1.06 s   ← used below
        4         0.98 s / 1.36 s   ← no better than 3, no core left over

    The old rule capped the model at two threads, which on a 4-CPU box made
    every decision ~28% slower (and the worst case ~50% slower) — the "Laya is
    extremely slow" symptom, and it left a three-quarter-idle machine. The
    budget is now sized the same way voice/tts.py sizes Kokoro's: keep ONE
    logical CPU for the Qt UI, the microphone callback and the Gemini session,
    hand the model the rest, and never exceed ``_MAX_DECISION_THREADS``.
    ``JARVIS_LAYA_THREADS`` overrides it for tuning.
    """
    override = str(os.environ.get("JARVIS_LAYA_THREADS", "") or "").strip()
    if override:
        try:
            n = int(override)
            if n >= 1:
                return n
        except (TypeError, ValueError):
            pass
    try:
        logical = os.cpu_count() or 4
    except Exception:
        logical = 4
    if logical <= 2:
        # A genuinely tiny machine: a single thread, so the interpreter that
        # hosts the UI and the microphone still gets scheduled.
        return 1
    return max(2, min(_MAX_DECISION_THREADS, logical - 1))


def _cap_torch_threads() -> int | None:
    """Apply the CPU thread budget to torch (before the model is loaded)."""
    n = _cpu_thread_budget()
    try:
        import torch
    except Exception:
        return None
    try:
        # Inter-op parallelism buys nothing for one 180-token row and must be
        # set before any parallel work has started — failure is expected later
        # in the process lifetime and is harmless.
        try:
            torch.set_num_interop_threads(1)
        except Exception:
            pass
        torch.set_num_threads(n)
    except Exception:
        return None
    return n


# ── configuration ─────────────────────────────────────────────────────────────

def _read_config() -> dict:
    """Live config file (features + laya section), cheaply cached on mtime."""
    global _config_cache
    try:
        st = CONFIG_PATH.stat()
        mtime = st.st_mtime
    except OSError:
        return {}
    if _config_cache is not None and _config_cache[0] == mtime:
        return _config_cache[1]
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _config_cache = (mtime, data)
    return data


def config_enabled() -> bool:
    """Is Laya supposed to participate right now?

    Precedence mirrors the voice "Save as Default" pattern: when a default was
    saved, ``laya.enabled`` restores on every boot; otherwise the live
    ``features.laya_ai`` switch governs (default ON — the feature is opt-out,
    and it still falls back safely when the model is not on disk)."""
    cfg = _read_config()
    feats = cfg.get("features") if isinstance(cfg.get("features"), dict) else {}
    enabled = bool(feats.get("laya_ai", True))
    laya_cfg = cfg.get("laya") if isinstance(cfg.get("laya"), dict) else {}
    if laya_cfg.get("save_as_default"):
        enabled = bool(laya_cfg.get("enabled", enabled))
    # Same env-override convention as config/settings.get_feature
    # (JARVIS_FEATURE_LAYA_AI=0/off forces the feature off for this process —
    # also the documented escape hatch when the local model must be skipped).
    env = os.environ.get("JARVIS_FEATURE_LAYA_AI", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        enabled = True
    elif env in ("0", "false", "no", "off"):
        enabled = False
    return enabled


def save_as_default_enabled() -> bool:
    cfg = _read_config()
    laya_cfg = cfg.get("laya") if isinstance(cfg.get("laya"), dict) else {}
    return bool(laya_cfg.get("save_as_default"))


def _write_config(updates: dict) -> tuple[bool, str]:
    """Merge *updates* into api_keys.json through the same guarded write path
    the Settings window uses (backup → atomic write → verify → rollback).

    The project's config_guard owns one absolute CONFIG_PATH; we only route
    through it when it points at the very file this module writes (always true
    in a real install). Tests — and any relocated config — fall back to an
    equivalent atomic, verify-then-rollback write so no caller can leave a
    half-written file behind."""
    try:
        current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}
    merged = dict(current)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            grown = dict(merged[key])
            grown.update(value)
            merged[key] = grown
        else:
            merged[key] = value
    global _config_cache
    _config_cache = None
    try:
        from core import config_guard as _guard
        use_guard = Path(_guard.CONFIG_PATH).resolve() == CONFIG_PATH.resolve()
    except Exception:
        _guard, use_guard = None, False
    if _guard is not None and use_guard:
        ok, msg, _bak = _guard.save_with_history(merged, tag="laya")
        return ok, msg
    # Guard unavailable or writing elsewhere: equivalent atomic write that
    # verifies the read-back and restores the previous bytes when it differs.
    previous_bytes = CONFIG_PATH.read_bytes() if CONFIG_PATH.exists() else None
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, indent=4), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        verify = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if verify != merged:
            if previous_bytes is not None:
                CONFIG_PATH.write_bytes(previous_bytes)
            return False, "saved file did not read back identically — restored"
        return True, "saved and verified"
    except Exception as exc:
        if previous_bytes is not None:
            try:
                CONFIG_PATH.write_bytes(previous_bytes)
            except Exception:
                pass
        return False, str(exc)


def _release_parked_agent() -> None:
    """Give the RAM back once Laya has stayed off for the whole grace window.

    The checkpoint is 322M parameters (~1.3 GB resident); an option the user
    switched off must not keep charging for it indefinitely."""
    global _parked_agent, _park_timer
    with _lock:
        if _parked_agent is None:
            return
        _parked_agent = None
        _park_timer = None
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    _log("INFO", "parked decision model released (disabled long enough to "
                 "return its memory; re-enabling reloads it in the background)")


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """The Settings "Enable Laya AI" switch: persist AND change runtime now.

    Disabling stops Laya from being called immediately and releases the model
    (parked briefly so a quick re-enable does not pay a 78-164 s reload);
    enabling restores a parked agent instantly, otherwise it (re)kicks a
    background initialization. Either way the caller never waits."""
    updates = {"features": {"laya_ai": bool(enabled)}}
    if save_as_default_enabled():
        updates["laya"] = {"enabled": bool(enabled)}
    ok, msg = _write_config(updates)
    global _agent, _state, _state_detail, _parked_agent, _park_timer
    with _lock:
        if not enabled:
            # Park, don't drop: the weights stay reachable for _HOLD_AFTER_DISABLE_S
            # while Laya is genuinely OUT of the pipeline (state DISABLED).
            if _agent is not None:
                _parked_agent = _agent
                _agent = None
                if _park_timer is not None:
                    try:
                        _park_timer.cancel()
                    except Exception:
                        pass
                _park_timer = threading.Timer(
                    _HOLD_AFTER_DISABLE_S, _release_parked_agent)
                _park_timer.daemon = True
                try:
                    _park_timer.start()
                except Exception:
                    pass
            _state = STATE_DISABLED
            _state_detail = ""
        elif ok:
            # Restore a parked model instead of reloading 322M parameters.
            if _parked_agent is not None:
                if _park_timer is not None:
                    try:
                        _park_timer.cancel()
                    except Exception:
                        pass
                    _park_timer = None
                _agent = _parked_agent
                _parked_agent = None
                _state = STATE_READY
                _state_detail = ""
                _log("INFO", "decision model re-enabled from the parked copy "
                             "(no reload, no restart)")
            else:
                _state = "uninitialized"
                _state_detail = ""
    if enabled and ok and get_state() != STATE_READY:
        initialize(background=True)
    return ok, msg


def set_save_as_default(on: bool) -> tuple[bool, str]:
    """The Settings "Save as Default" switch: persist the CURRENT enabled state
    as the default every next launch restores."""
    laya_cfg = {"save_as_default": bool(on)}
    if on:
        laya_cfg["enabled"] = bool(config_enabled())
    return _write_config({"laya": laya_cfg})


# ── model detection (real, on-disk) ───────────────────────────────────────────

def detect() -> dict:
    """Locate laya-multilingual and validate its file manifest.

    Returns {"detected": bool, "path": str, "relative": str, "missing": [...]}
    — pure filesystem inspection, never imports torch/laya, safe to call from
    the UI thread on every tab switch."""
    for cand in MODEL_CANDIDATES:
        p = BASE_DIR.joinpath(*cand)
        if not p.is_dir():
            continue
        missing = [f for f in REQUIRED_FILES if not (p / f).is_file()]
        missing += [d + "/*" for d in REQUIRED_DIRS if not (p / d).is_dir()]
        return {
            "detected": not missing,
            "path": str(p),
            "relative": "/".join(cand),
            "missing": missing,
        }
    return {"detected": False, "path": "", "relative": "",
            "missing": ["laya-multilingual/"]}


_MODEL_INFO_TTL_S = 30.0
_model_info_cache: tuple[str, float, dict] | None = None


def model_info() -> dict:
    """Model facts read from the installed checkpoint (never invented).

    Memoised per directory: the Settings page polls status() every 2 s on the
    UI thread, and re-reading/parsing the checkpoint config each time is work
    nobody needs."""
    global _model_info_cache
    d = detect()
    out = {"label": DECISION_AI_LABEL, "encoder": "", "params": "322M",
           "device": "cpu", "directory": d["path"], "detected": d["detected"]}
    if not d["path"]:
        return out
    now = time.monotonic()
    if (_model_info_cache is not None
            and _model_info_cache[0] == d["path"]
            and now - _model_info_cache[1] < _MODEL_INFO_TTL_S):
        return dict(_model_info_cache[2])
    try:
        cfg = json.loads(
            (Path(d["path"]) / "rl_agent_config.json").read_text(encoding="utf-8"))
        out["encoder"] = str(cfg.get("encoder", "") or "")
        out["max_len"] = cfg.get("max_len")
    except Exception:
        pass
    _model_info_cache = (d["path"], now, dict(out))
    return out


# ── lifecycle ─────────────────────────────────────────────────────────────────

def _log(level: str, message: str) -> None:
    """Log through the project's observability logger; additionally echo INFO
    lifecycle lines to the console in the same [JARVIS] style main.py uses
    (log_event already echoes WARN+ itself, and the JSONL always receives the
    full record — startup verification needs to SEE the ready transition)."""
    try:
        from observability.logger import log_event
        log_event(level, "laya", message)
    except Exception:
        pass
    if str(level).upper() == "INFO":
        print(f"[JARVIS] Laya: {message}")


def _load_blocking(detection: dict | None = None) -> None:
    """Import laya + load the checkpoint on THIS thread. Guarded end to end;
    the outcome is visible via get_state()/status().

    Callers on a user-facing path must go through initialize(background=True):
    a load measures 78-164 s in practice, so nothing that a turn waits for may
    call it directly. The lock is deliberately NOT held across the load — only
    the tiny state transitions take it, so Settings/diagnostics never stall.

    *detection* is the already-resolved detect() result. initialize() resolves it
    on the calling thread and hands it over, so a background load can never
    decide to use a project root that has changed underneath it."""
    global _agent, _state, _state_detail, _load_started
    with _lock:
        if _state == STATE_READY and _agent is not None:
            return
        if _state == STATE_LOADING:
            return
        if not config_enabled():
            _state = STATE_DISABLED
            return
        _state = STATE_LOADING
        _state_detail = ""
        _load_started = time.monotonic()
    d = detection if isinstance(detection, dict) else detect()
    if not d.get("detected"):
        with _lock:
            _state = STATE_NOT_FOUND
            _state_detail = ("model files missing"
                             if d.get("path") else "laya-multilingual/ not found")
            if d.get("missing"):
                _state_detail += ": " + ", ".join(d["missing"])
        _log("WARNING", f"model unavailable ({_state_detail}) — "
                        "falling back to existing AI routing")
        return
    # The model runtime package must exist before the torch import below: a
    # missing 'laya' used to cost a full torch import (~10 s, GBs of native
    # memory) on a background thread and then fail anyway (the startup log's
    # "No module named 'laya'"). Probe first; unknown result keeps old path.
    try:
        import importlib.util as _ilu
        _laya_installed = _ilu.find_spec("laya") is not None
    except Exception:
        _laya_installed = True
    if not _laya_installed:
        with _lock:
            _state = STATE_ERROR
            _state_detail = "ModuleNotFoundError: No module named 'laya'"
            _agent = None
        _log("ERROR", "model load failed — falling back to existing AI "
                      "routing (ModuleNotFoundError: No module named 'laya')")
        return
    try:
        # Cap torch's CPU budget BEFORE the heavy import: the load itself is
        # parallel, and an uncapped torch saturates every core of the machine.
        threads = _cap_torch_threads()
        with _timing_lock:
            _timing_stats["threads"] = threads
        # Measured 78-164 s on a 2-core CPU. Without this line the service was
        # silently unavailable for the first two minutes of every session.
        _log("INFO", f"loading decision model on CPU ({d['relative']}, "
                     f"torch threads={threads}) — requests route normally until "
                     f"it is ready")
        import laya
        agent = laya.load(d["path"], device="cpu")
    except Exception as exc:
        with _lock:
            _state = STATE_ERROR
            _state_detail = f"{type(exc).__name__}: {exc}"
            _agent = None
        _log("ERROR", f"model load failed — falling back to existing AI "
                      f"routing ({_state_detail})")
        return
    with _lock:
        _agent = agent
    load_ms = (time.monotonic() - _load_started) * 1000.0
    with _timing_lock:
        _timing_stats["load_ms"] = load_ms
    # The model is not actually usable until the first forward pass has paid
    # its cold cost (measured 23-40 s here, versus 1.6-4 s afterwards), so the
    # state stays LOADING through the warm-up: status(), Settings and the
    # ACTIVITY line then tell the truth instead of claiming "ready" for a
    # decision layer that cannot answer yet.
    _warm_up(agent)
    with _lock:
        _state = STATE_READY
        _state_detail = ""
    _log("INFO", f"decision model ready on CPU ({d['relative']}, "
                 f"{load_ms / 1000.0:.1f}s load, torch threads="
                 f"{_timing_stats.get('threads')})")


_WARM_PROBE = "Open Chrome"


def _warm_up(agent) -> None:
    """Pay the first-inference cost (torch lazy init, allocator, tokenizer
    warm) here instead of on the user's first request.

    Measured: the first predict() after a load costs several times what the
    same request costs afterwards, and a warm-up that OVERLAPS a user request
    measured 23.8 s for that request on a 2-core box (both forward passes
    fighting for the same two cores). So the warm-up holds the same single
    inference gate: either it runs while nothing else is deciding, or a real
    request is already in flight and the warm-up stands aside.

    The probe answer is discarded — it is never published as a classification
    and never cached, so no real request can inherit it."""
    try:
        for _ in range(3):
            if _classify_gate.acquire(blocking=False):
                break
            time.sleep(5.0)
        else:
            _log("INFO", "decision warm-up postponed (the user is already "
                         "asking things — no point competing for the CPU)")
            return
        try:
            s = time.monotonic()
            agent.predict(_WARM_PROBE, _QUESTIONS)
            warm_ms = (time.monotonic() - s) * 1000.0
        finally:
            _classify_gate.release()
        with _timing_lock:
            _timing_stats["warm_ms"] = warm_ms
        _log("INFO", f"decision model warmed up ({warm_ms / 1000.0:.1f}s "
                     "first-inference cost paid)")
    except Exception as exc:
        _log("WARNING", f"decision model warm-up failed (first decision will "
                        f"be slower): {type(exc).__name__}: {exc}")


def initialize(background: bool = True) -> str:
    """Startup / post-save hook — converges the runtime with the config.
    Never blocks and never fails the app: disabled → drop the model and skip;
    enabled + model present → load on a daemon thread.

    Returns the resulting state so a caller that just triggered a load can
    report the truth about THIS turn without racing the worker thread."""
    global _agent, _state, _state_detail, _parked_agent, _park_timer
    if not config_enabled():
        with _lock:
            _agent = None
            _state = STATE_DISABLED
            _state_detail = ""
        return STATE_DISABLED
    with _lock:
        # A parked copy beats a 78-164 s reload whenever one is available.
        if _agent is None and _parked_agent is not None:
            if _park_timer is not None:
                try:
                    _park_timer.cancel()
                except Exception:
                    pass
                _park_timer = None
            _agent = _parked_agent
            _parked_agent = None
            _state = STATE_READY
            _state_detail = ""
    current = get_state()
    if current in (STATE_READY, STATE_LOADING):
        return current
    # Resolved HERE, on the caller's view of the project root, then handed to
    # the worker — see _load_blocking().
    detection = detect()
    if background:
        # The state itself is set by the worker (LOADING → READY/NOT_FOUND);
        # this only tells the caller what is about to happen for THIS turn.
        threading.Thread(target=_safe_init, args=(detection,),
                         name="laya-init", daemon=True).start()
        return STATE_LOADING if detection.get("detected") else STATE_NOT_FOUND
    _safe_init(detection)
    return get_state()


def _safe_init(detection: dict | None = None) -> None:
    try:
        _load_blocking(detection)
    except Exception as exc:  # belt and braces: init must never kill a thread usefully
        _log("ERROR", f"initialization crashed: {exc}")


def get_state() -> str:
    with _lock:
        return _state if _state != "uninitialized" else (
            "detecting" if config_enabled() else STATE_DISABLED)


def enabled() -> bool:
    return config_enabled()


# ── classification (the actual decision) ──────────────────────────────────────

def _extract_category(result) -> tuple[str | None, float]:
    """Pull (category, confidence) out of a laya predict() payload. Tolerant of
    both the wrapped {'answers': {...}} form and a flat dict."""
    try:
        if isinstance(result, dict):
            ans = result.get("answers")
            payload = ans.get("action") if isinstance(ans, dict) else None
            if payload is None:
                payload = result.get("action")
            if isinstance(payload, str):
                choice = payload
                conf = 0.0
            elif isinstance(payload, dict):
                choice = str(payload.get("choice", "") or "")
                try:
                    conf = float(payload.get("confidence")
                                 or payload.get("answer_confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
            else:
                return None, 0.0
            choice = choice.strip().upper()
            if choice in CATEGORIES:
                return choice, conf
        return None, 0.0
    except Exception:
        return None, 0.0


def last_skip() -> dict | None:
    """Why the most recent request did NOT get a Laya decision (real reason)."""
    with _lock:
        return dict(_last_skip) if _last_skip else None


_SKIP_EXPLANATION = {
    "disabled":        "disabled in Settings",
    "too short":       "too short to decide on",
    "model loading":   "decision model still loading on CPU",
    "model unavailable": "decision model unavailable — existing routing in charge",
    "busy":            "another decision is still running",
    "no model":        "decision model not initialized",
    "inference failed": "decision failed — existing routing handled it",
    "abstained":       "no confident category — general AI handled this",
}


def _skip(reason: str, received_at: float | None = None) -> None:
    """Record an honest 'Laya was not consulted, because …' (never a fake answer)."""
    global _last_skip
    with _lock:
        _last_skip = {"reason": reason, "ts": time.time(),
                      "detail": _SKIP_EXPLANATION.get(reason, reason)}
    _note_timing(event="routing skipped", skipped=reason,
                 total_ms=((time.monotonic() - received_at) * 1000.0
                           if received_at else 0.0))


def classify_task(text: str, *, record: bool = True,
                  received_at: float | None = None) -> dict | None:
    """Classify a user request into one of the routed categories.

    Returns {"category", "confidence", "request", "target", "ts"} or None when
    Laya is disabled/unavailable/busy or the model abstained. NEVER raises — a
    bad inference degrades to None and the existing routing stays in charge.

    *received_at* is the monotonic timestamp of "request received" (main.py
    passes it); every stage is measured against it so the reported routing
    latency is the real one the user waited through.

    Nothing here ever waits for the model: a cold decision layer reports
    "model loading" and the turn proceeds on the existing AI path."""
    global _last_result
    t_recv = (float(received_at) if received_at
              else time.monotonic())
    key = _cache_key(text)
    if not key:
        _skip("too short", t_recv)
        return None
    # Greetings and one-word interjections are CHAT by shape — asking a CPU
    # model costs seconds and buys nothing on a two-character message.
    if len(key) < 4:
        _skip("too short", t_recv)
        return None
    if not config_enabled():
        _skip("disabled", t_recv)
        return None
    # The same words asked twice inside the TTL are answered from the cache:
    # one 180-token forward pass costs 1.6-8 s of this machine's CPU, and a
    # repeated question cannot deserve a second one.
    hit = _cache_get(key)
    if hit is not None:
        hit = dict(hit)
        hit["cached"] = True
        if record:
            with _lock:
                _last_result = hit
        _note_timing(event="decision cached", cache_hit=True,
                     total_ms=(time.monotonic() - t_recv) * 1000.0,
                     category=hit.get("category"),
                     confidence=hit.get("confidence"))
        return hit
    state = get_state()
    if state in ("uninitialized", "detecting"):
        # Background only — the 78-164 s load must never land in a turn.
        state = initialize(background=True)
    if state == STATE_LOADING:
        _skip("model loading", t_recv)
        return None
    if state != STATE_READY:
        _skip("model unavailable" if state in (STATE_NOT_FOUND, STATE_ERROR)
              else "no model", t_recv)
        return None
    with _lock:
        agent = _agent
    if agent is None:
        _skip("no model", t_recv)
        return None
    if not _classify_gate.acquire(blocking=False):
        # One inference at a time: stacking forward passes is what makes the
        # whole interface wait. The turn continues without a hint.
        _skip("busy", t_recv)
        return None
    try:
        t_start = time.monotonic()
        try:
            result = agent.predict(str(text).strip(), _QUESTIONS)
        finally:
            infer_ms = (time.monotonic() - t_start) * 1000.0
        category, confidence = _extract_category(result)
        tokens = None
        try:
            tokens = int((result.get("usage") or {}).get("input_tokens"))
        except Exception:
            tokens = None
    except Exception as exc:
        _log("ERROR", f"inference failed for {str(text)[:60]!r}: {exc}")
        _note_timing(event="inference failed", inference_ms=infer_ms,
                     total_ms=(time.monotonic() - t_recv) * 1000.0, error=exc)
        _classify_gate.release()
        _skip("inference failed", t_recv)
        return None
    _classify_gate.release()
    if category is None:
        with _timing_lock:
            _timing_stats["inferences"] += 1
        _note_timing(event="decision abstained", inference_ms=infer_ms,
                     total_ms=(time.monotonic() - t_recv) * 1000.0,
                     tokens=tokens)
        _skip("abstained", t_recv)
        return None
    outcome = {
        "category": category,
        "confidence": round(confidence, 4),
        "request": str(text).strip()[:300],
        "target": CATEGORY_TARGETS.get(category, "Gemini"),
        "ts": time.time(),
    }
    with _timing_lock:
        _timing_stats["inferences"] += 1
    _cache_put(key, outcome)
    _note_timing(event="decision", total_ms=(time.monotonic() - t_recv) * 1000.0,
                 inference_ms=infer_ms, category=category,
                 confidence=outcome["confidence"], tokens=tokens,
                 routing_ms=infer_ms)
    if record:
        with _lock:
            _last_result = outcome
    return outcome


def last_classification() -> dict | None:
    with _lock:
        return dict(_last_result) if _last_result else None


def clear_last_classification() -> None:
    global _last_result
    with _lock:
        _last_result = None


# ── AI-stack pipeline status (Laya + Gemini work TOGETHER) ──────────────────
#
# The single honest answer to "which AIs are active, and doing what?". The
# pipeline is USER → J.A.R.V.I.S. core → Laya (local decision / task routing)
# → AI task router → Gemini (general reasoning / response) → tools/agents →
# result. Laya never replaces Gemini and Gemini never replaces Laya, so a
# status view that reports only the chat provider (Gemini) and stays silent
# about the decision layer is what made "Is Laya enabled?" answer wrong —
# these two functions exist so every surface (diagnostics tool, session
# context, Settings) reads the SAME runtime state.

GENERAL_AI_ROLES = "general AI — conversation, reasoning, response generation, analysis, planning"
DECISION_AI_ROLE = "local decision AI — intent detection, task classification, routing decisions"

# Provider → config key that proves it is usable (same keys Settings writes).
_PROVIDER_KEYS = {
    "gemini": "gemini_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "groq": "groq_api_key",
    "custom": "custom_ai_api_key",
}


def general_ai_status() -> dict:
    """The MAIN AI currently configured for reasoning/responses.

    Read from the same config keys the Settings → AI page writes
    (ai_provider / ai_model / *_api_key) — never assumed, never hard-coded."""
    cfg = _read_config()
    provider = str(cfg.get("ai_provider") or "gemini").strip().lower() or "gemini"
    model = str(cfg.get("ai_model") or "").strip() or "(provider default)"
    key_name = _PROVIDER_KEYS.get(provider, "gemini_api_key")
    has_key = bool(str(cfg.get(key_name) or "").strip())
    name = provider.capitalize()
    if provider == "custom":
        name = str(cfg.get("custom_provider_name") or "").strip() or "Custom"
    return {
        "name": name,
        "provider": provider,
        "model": model,
        "role": GENERAL_AI_ROLES,
        "enabled": True,               # the chat pipeline always has a provider
        "ready": has_key,
        "detail": ("configured, key present" if has_key
                   else f"no {key_name} configured"),
    }


def pipeline_status() -> dict:
    """Both halves of the AI stack, as one runtime snapshot (§9 of the spec):
    Laya = local decision AI, Gemini (or configured provider) = general AI."""
    st = status()
    return {
        "decision_ai": {
            "name": "Laya",
            "label": DECISION_AI_LABEL,
            "role": DECISION_AI_ROLE,
            "enabled": st["enabled"],
            "detected": st["detected"],
            "ready": st["ready"],
            "state": st["state"],
            "detail": st["detail_line"],
            "model_dir": st["model_dir"],
        },
        "general_ai": general_ai_status(),
        "pipeline": "USER → J.A.R.V.I.S. core → Laya (decision) → AI task "
                    "router → general AI (reasoning/response) → tools/agents → result",
    }


def status_brief() -> str:
    """Two truthful lines for prompts/diagnostics — the exact shape the
    'Is Laya enabled?' answer must be built from — plus the MEASURED decision
    latency once at least one real decision has run (no numbers are invented:
    with nothing measured, the timing line simply says so)."""
    p = pipeline_status()
    d, g = p["decision_ai"], p["general_ai"]
    if not d["enabled"]:
        laya_line = "Laya AI: DISABLED (switched off in Settings → Laya AI)"
    elif d["ready"]:
        laya_line = "Laya AI: ENABLED / READY (local decision AI — active in the pipeline)"
    elif d["state"] == STATE_LOADING:
        laya_line = "Laya AI: ENABLED / LOADING (model starting on CPU)"
    else:
        # uninitialized / detecting: a file on disk is NOT a running model.
        laya_line = (f"Laya AI: ENABLED but NOT ACTIVE (state={d['state']}) — "
                     "existing routing is in charge as the fallback")
    gen_line = (f"{g['provider'].capitalize()}: "
                f"{'ENABLED / READY' if g['ready'] else 'ENABLED / NO API KEY'} "
                f"(general AI — reasoning, response, planning; model {g['model']})")
    return laya_line + "\n" + gen_line + "\n" + timing_line()


def timing_line() -> str:
    """One measured sentence about decision speed ('—' when nothing ran)."""
    try:
        s = timing_stats()
        if not s["decisions"] and not s["cache_hits"]:
            return ("LAYA TIMING: nothing decided yet this session "
                    "(no latency claimed either way)")
        last = last_timing() or {}
        bits = [f"avg {s['avg_total_ms']:.0f} ms",
                f"max {s['max_ms']:.0f} ms",
                f"{s['decisions']} decided"]
        if s["cache_hits"]:
            bits.append(f"{s['cache_hits']} from cache")
        if s["skips"]:
            bits.append(f"{s['skips']} skipped")
        if last.get("inference_ms") is not None:
            bits.append(f"last inference {last['inference_ms']:.0f} ms")
        if last.get("total_ms") is not None:
            bits.append(f"last routing {last['total_ms']:.0f} ms")
        if s.get("threads"):
            bits.append(f"torch threads {s['threads']}")
        if s.get("load_ms"):
            bits.append(f"model load {s['load_ms'] / 1000.0:.1f}s")
        return "LAYA TIMING: " + " · ".join(bits)
    except Exception:
        return "LAYA TIMING: unavailable"


# ── AI task router hookup ─────────────────────────────────────────────────────

def route_for_category(category: str) -> str:
    """The downstream system a classification points at (advisory)."""
    return CATEGORY_TARGETS.get(str(category or "").strip().upper(),
                                "Gemini (general conversation)")


def shutdown() -> None:
    """Drop the loaded model, the parked copy and the decision cache
    (process teardown / tests). Measured timings survive: they are history."""
    global _agent, _state, _parked_agent, _park_timer, _last_skip, _config_cache
    with _lock:
        _agent = None
        _parked_agent = None
        if _park_timer is not None:
            try:
                _park_timer.cancel()
            except Exception:
                pass
            _park_timer = None
        _state = "uninitialized"
        _last_skip = None
    _config_cache = None
    with _cache_lock:
        _decision_cache.clear()


# ── status for Settings + diagnostics ─────────────────────────────────────────

def status() -> dict:
    """Everything the "Laya AI" Settings section and the diagnostics tool
    need to tell the truth about the current state — including the REAL,
    measured decision latency (never an estimate, never a rounded claim)."""
    is_enabled = config_enabled()
    state = get_state()
    d = detect()
    if not is_enabled:
        line, detail = "Disabled", "Laya is not part of the pipeline"
    elif state == STATE_READY:
        line, detail = "Enabled", "Ready"
    elif state == STATE_LOADING:
        line, detail = "Enabled", "Loading model…"
    elif state == STATE_NOT_FOUND:
        line, detail = "Enabled", "Model not found — fallback active"
    elif state == STATE_ERROR:
        line, detail = "Enabled", f"Load failed — fallback active ({_state_detail})"
    else:
        # uninitialized / detecting: a file on disk is NOT a running model.
        # "Ready" is reserved for STATE_READY only (Settings spec §11).
        line = "Enabled"
        detail = ("Model detected — not initialized yet"
                  if d["detected"] else "Model not found")
    with _lock:
        parked = _parked_agent is not None
    last = last_timing() or {}
    return {
        "enabled": is_enabled,
        "save_as_default": save_as_default_enabled(),
        "detected": d["detected"],
        "model_dir": d["relative"] or d["path"] or "—",
        "model_path": d["path"],
        "missing": d["missing"],
        "state": state,
        "state_detail": _state_detail,
        "ready": state == STATE_READY,
        "status_line": line,
        "detail_line": detail,
        "decision_ai": DECISION_AI_LABEL,
        "model_info": model_info(),
        "last_classification": last_classification(),
        # Measured runtime truth about the decision path (items 4/26): how long
        # the last real decision took, whether it came from the cache, and why
        # a request was NOT decided at all.
        "last_timing": last or None,
        "timing": timing_stats(),
        "last_skip": last_skip(),
        "cached_model": parked,
        "torch_threads": (timing_stats().get("threads")),
        "cache_entries": _cache_size(),
    }


def _cache_size() -> int:
    with _cache_lock:
        return len(_decision_cache)

