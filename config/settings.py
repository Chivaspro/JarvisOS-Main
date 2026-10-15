"""Centralized configuration for Mark LII (change: modernize-jarvis-architecture).

One read-only view over the existing config sources:

* ``config/api_keys.json``  — user-edited JSON (existing primary store)
* environment variables     — optional overrides (see .env.example)
* built-in defaults         — the value used when nothing else is set

Existing modules keep writing config exactly as before. The one write helper
here, :func:`set_feature`, persists a single feature flag through the shared
guarded writer (app.config_guard) so every reader of an interactive toggle sees
the same value. Existing consumers see identical values, so this is purely
additive.

Feature flags default to OFF — every optional subsystem introduced by the
modernization (LangGraph orchestrator, Mem0, Cua backend, Langfuse, Qdrant,
coding agents) stays dormant until the user opts in.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_KEYS_FILE = CONFIG_DIR / "api_keys.json"
DATA_DIR = BASE_DIR / os.environ.get("JARVIS_DATA_DIR", "data")
LOGS_DIR = DATA_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache"
RUNTIME_DIR = DATA_DIR / "runtime"

# Feature flags and their defaults. Every optional subsystem introduced by the
# modernization is off unless explicitly enabled in config/api_keys.json
# ("features" map) or overridden by an environment variable.
FEATURE_DEFAULTS: dict[str, bool | str] = {
    "langgraph_orchestrator": False,
    "mem0_memory": False,
    "langfuse_tracing": False,
    "qdrant_knowledge": False,
    "coding_agents": True,          # router ships active; individual backends probe availability
    # Control capabilities. Each engine is probed for real before use; a missing
    # engine is reported, never substituted. desktop_control is the permission
    # switch Agent TARS's computer_control is gated on.
    "agent_tars": True,             # PC/browser automation engine (Agent TARS v0.3.0)
    "agent_reach": True,            # internet research channels (Agent Reach)
    "desktop_control": True,        # master switch for everything acting on this PC
    # Local controllers — one switch per engine relationship. Each is enforced
    # per action by capabilities/router.py, so revoking one refuses exactly the
    # actions it covers and never substitutes another controller.
    "playwright_browser": True,     # browser_action  -> Playwright
    "windows_automation": True,     # pc_control applications/windows -> pywinauto
    "uitars_visual": True,          # visual_control  -> UI-TARS prompt on Gemini
    "physical_input": True,         # mouse/keyboard  -> PyAutoGUI
    "capabilities_kill_children_on_close": True,  # stop owned automation workers on exit
    "self_inspection": True,
    "voice_engine_control": True,   # permission: JARVIS may switch its own voice engine (e.g. Gemini native audio)
    "self_diagnostics": True,       # permission: JARVIS may check its own engine/project/settings
    # Chat-panel mode switch: ON = UI TARS mode (voice steering of the PC via
    # Agent TARS), OFF = normal Gemini conversation. Default OFF so a fresh
    # install talks to the AI first and never surprises the user with control.
    "ui_tars_mode": False,
    # ── Deep Windows orchestration + clipboard sentry (Phase 3) ─────────────
    # One switch per relationship, enforced per action by security/permissions.py
    # so a refusal always names the switch that stopped it.
    "process_control": True,        # system_control process actions (freeze/kill/priority)
    "system_settings": True,        # Wi-Fi, volume, brightness, theme, metrics
    "window_control": True,         # minimize/maximize/tile/snap/multi-monitor
    "clipboard_sentry": True,       # writing to and analysing the clipboard
    "document_architect": True,     # writing Word/Excel/PowerPoint/PDF deliverables
    "auto_approve_coding": False,   # when on, coding tasks skip the chatlog approve/decline prompt
    "laya_ai": True,                # local Laya decision AI classifies + routes tasks (falls back safely when the model is absent)
    # Realtime STT: interim (in-progress) speech transcript rendered live into
    # the existing command textbox. OFF stops the live display and the per-chunk
    # correction work; the microphone, the final transcript and every other
    # voice behaviour are untouched.
    "realtime_stt": True,
    # Realtime narration: J.A.R.V.I.S. says what he is doing out loud while he
    # does it ("Sir, I'm opening the page."). Every line comes from a real
    # event and a real engine progress string — OFF is pure silence, and the
    # work itself is completely unaffected either way.
    "narrator": True,
    # Low-latency voice turns: the client detects the end of your sentence and
    # tells the Live session immediately, instead of waiting out the server's
    # ~800 ms silence window. OFF falls back to server-side VAD end-of-speech.
    "realtime_low_latency": True,
    # Streaming TTS: cloud voices (Fish Audio / ElevenLabs) are rendered and
    # played sentence by sentence instead of downloading the whole reply first.
    # OFF restores the previous whole-reply fetch.
    "tts_streaming": True,
    # ── Self-evolution (the Skill Crucible) ─────────────────────────────────
    # J.A.R.V.I.S. may write a brand-new tool for himself when a request has no
    # built-in answer. Every candidate is AST-checked, dependency-resolved and
    # executed in a sandbox child process before a single file is installed.
    "self_evolution": True,
    # May the Crucible pip-install a missing dependency the generated skill
    # needs? OFF turns that stage into a plain check, so a missing package is
    # reported instead of installed.
    "self_evolution_auto_install_deps": True,
    # ── Proactive Auto-Heal ─────────────────────────────────────────────────
    # Background sentry that turns a runtime traceback into a reviewed patch.
    "self_heal": True,
    # auto_low_risk = patch actions/plugins/evolution autonomously, but require an
    # on-screen confirmation for core/, brain/, memory/, capabilities/, voice/,
    # vision/ and every protected file. propose = never write without approval.
    "auto_heal_mode": "auto_low_risk",
    # ── Service links (Phase 3): one switch per external service ────────────
    # Each one gates exactly one relationship, so revoking Spotify cannot leave
    # Instagram automation quietly enabled.
    "spotify": True,                # playback control through the Spotify MCP host
    "instagram": True,              # the user's own Instagram account (instagrapi)
    "smart_home": True,             # local Kasa/Tapo devices on this network
    "connect": True,                # the JarvisOS Connect gateway + paired clients
    "live_vision": True,            # continuous camera understanding in the live session
    # The Composio connector: Spotify, Gmail, GitHub and every other external
    # account, behind one tool. Its own switch on purpose — "connect" above is
    # the phone-pairing gateway, and one flag must not govern two relationships.
    "external_apps": True,          # external apps through the Composio connector
    # ── MCP host ────────────────────────────────────────────────────────────
    # The universal Model Context Protocol server manager (Settings ▸ MCP).
    # OFF leaves every configured server unstarted and the ``mcp`` tool closed;
    # the servers themselves keep their configuration for a later switch-on.
    "mcp_servers": True,
    # ── Obsidian Vault memory ───────────────────────────────────────────────
    # The vault as a long-term memory backend. OFF keeps the vault readable in
    # Settings (so it can still be validated and indexed) but never routes
    # memory through it; the active backend choice is the ``memory_backend``
    # setting, which only takes effect while this flag is on.
    "obsidian_memory": True,
}

# Secret-looking key names, used by the redaction helper at the logging
# boundary (observability requirement: no secrets in logs/traces).
SECRET_FIELD_NAMES = {
    "gemini_api_key", "api_key", "apikey", "token", "access_token",
    "refresh_token", "secret", "secret_key", "password", "passwd", "pwd",
    "authorization", "fish_audio_api_key", "mem0_api_key", "qdrant_api_key",
    # Legacy: a UI-TARS credential existed before this project ran the whole
    # visual layer on the Gemini key. Kept only so an old config file is still
    # redacted at the logging boundary.
    "ui_tars_api_key", "ui_tars_key",
    "langfuse_secret_key", "langfuse_public_key", "private_key",
    # Provider + service credentials added by the capability port. Redacted
    # wherever a config block is logged or traced.
    "openrouter_api_key", "spotify_client_id", "spotify_client_secret",
    "spotify_access_token", "spotify_refresh_token", "weather_api_key",
    "radar_api_key", "instagram_password", "instagram_session",
    "smart_home_password", "smart_home_token", "connect_pairing_secret",
    # The connector's project key. Added the same day the connector was: a key
    # that is read but not listed here is the one that ends up in a log line.
    "composio_api_key",
}


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "data": None}


def _read_api_keys() -> dict:
    """Parsed api_keys.json, cached on (mtime, size); {} when unreadable."""
    try:
        st = API_KEYS_FILE.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _cache_lock:
        if _cache["data"] is not None and _cache["mtime"] == key:
            return _cache["data"]
        try:
            data = json.loads(API_KEYS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    with _cache_lock:
        _cache["mtime"] = key
        _cache["data"] = data
    return data


def get(key: str, default: Any = None) -> Any:
    """Top-level config value: env override → api_keys.json → default."""
    env = os.environ.get(key.upper())
    if env is not None and env != "":
        return env
    return _read_api_keys().get(key, default)


def get_feature(name: str, default: Any = None) -> Any:
    """Feature-flag value with precedence: JARVIS_FEATURE_<NAME> env →
    features map in api_keys.json → FEATURE_DEFAULTS → default."""
    env_key = f"JARVIS_FEATURE_{name.upper()}"
    env = os.environ.get(env_key)
    if env is not None and env != "":
        low = env.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        return env.strip()
    feats = _read_api_keys().get("features")
    if isinstance(feats, dict) and name in feats:
        return feats[name]
    if name in FEATURE_DEFAULTS:
        return FEATURE_DEFAULTS[name]
    return default


def feature_enabled(name: str) -> bool:
    """Boolean view of a feature flag (truthy strings count as enabled)."""
    v = get_feature(name)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "auto")
    return bool(v)


def gemini_api_key() -> str:
    env = os.environ.get("GEMINI_API_KEY", "")
    if env:
        return env
    return str(_read_api_keys().get("gemini_api_key", "") or "")


def api_key(field: str, default: str = "") -> str:
    """One credential, resolved the one way: environment → api_keys.json.

    :func:`get` already uppercases the field for the environment lookup, so
    ``api_key("gemini_api_key")`` reads ``GEMINI_API_KEY`` first and falls back to
    the file — the exact precedence the hub, the capabilities layer and Settings
    already use.

    Every module that needs a provider key must call this instead of parsing
    ``api_keys.json`` itself. A JSON-only reader ignores the environment, so it
    disagrees with Settings about whether a key is configured: that is how a
    configured key was reported as ready in the API HUB while the live session —
    reading the file alone — connected with an empty string and stayed silent.
    """
    return str(get(field, default) or "").strip()


def ui_tars_mode() -> bool:
    """True when the chat panel is switched to UI TARS (PC control) mode."""
    v = get_feature("ui_tars_mode", False)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "tars", "ui_tars")
    return bool(v)


def set_feature(name: str, value: Any) -> bool:
    """Persist one feature flag in api_keys.json (merge-safe, guarded).

    The single writer for the "features" map; every reader goes through
    :func:`get_feature` / :func:`feature_enabled`. Returns True when the value
    is now what was asked for (a no-op write counts). Never raises: a failed
    write returns False and leaves the previous configuration intact.
    """
    try:
        from app import config_guard as _guard
        data = dict(_guard.read(API_KEYS_FILE) or {})
        feats = dict(data.get("features") or {})
        if feats.get(name) == value:
            return True
        feats[name] = value
        data["features"] = feats
        ok, _msg = _guard.guard_write(data, tag=f"feature:{name}", path=API_KEYS_FILE)
        return bool(ok)
    except Exception:
        return False


def set_value(name: str, value: Any) -> bool:
    """Persist one top-level setting in api_keys.json (merge-safe, guarded).

    The single writer for settings that are not feature flags — the active
    memory backend and the Obsidian vault path are the first users. It mirrors
    :func:`set_feature`: read, merge the one key, write through the guarded
    writer, and report success honestly instead of raising into a caller that
    was only trying to remember a preference.
    """
    try:
        from app import config_guard as _guard
        data = dict(_guard.read(API_KEYS_FILE) or {})
        if data.get(name) == value:
            return True
        data[name] = value
        ok, _msg = _guard.guard_write(data, tag=f"setting:{name}",
                                      path=API_KEYS_FILE)
        return bool(ok)
    except Exception:
        return False


def data_dir() -> Path:
    """Runtime data root (logs/cache/runtime), created lazily."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def logs_dir() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


def redact_secrets(value: Any, _key_hint: str = "") -> Any:
    """Return *value* with secret-shaped content replaced.

    Rules:
    * dict: keys in SECRET_FIELD_NAMES (case-insensitive) become "[REDACTED]";
      other values are redacted recursively.
    * str that *is* the secret for a secret-named key: "[REDACTED]".
    * everything else passes through unchanged.

    Used by the structured logger so credentials never reach log files.
    """
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if isinstance(k, str) and k.strip().lower() in SECRET_FIELD_NAMES:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_secrets(v, _key_hint)
        return out
    if (
        isinstance(value, str)
        and _key_hint
        and _key_hint.strip().lower() in SECRET_FIELD_NAMES
    ):
        return "[REDACTED]"
    return value


def env_summary() -> dict:
    """Safe configuration summary for diagnostics (secrets redacted)."""
    data = _read_api_keys()
    return {
        "config_file": str(API_KEYS_FILE),
        "config_present": API_KEYS_FILE.exists(),
        "assistant_name": data.get("assistant_name", "JARVIS"),
        "gemini_key_present": bool(gemini_api_key()),
        "features": {k: get_feature(k) for k in FEATURE_DEFAULTS},
        "log_level": os.environ.get("JARVIS_LOG_LEVEL", "INFO"),
        "data_dir": str(DATA_DIR),
    }
