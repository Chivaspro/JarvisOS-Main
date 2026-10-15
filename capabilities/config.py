"""Environment and settings resolution for the capability layer.

Two engines are wired here:

    agent_tars    -> browser + PC automation (Node runtime, @agent-tars/cli@0.3.0)
    agent_reach   -> internet research channels (own Python 3.11 environment)

Nothing in this module hardcodes a secret: API keys are read through
config.settings, which already applies the precedence "environment variable ->
config/api_keys.json -> default". The same precedence, with a ``JARVIS_*``
override, is used for the Agent Reach environment path and for the Agent TARS
runtime location, so a relocated install never needs a code edit.

Paths are discovered, not assumed: the Agent Reach environment is found by its
installed folder name and the Agent TARS CLI by the project's pinned
``node_modules`` (then the npx cache). A missing engine must read as
"unavailable", never as a crash.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _base_dir() -> Path:
    try:
        from config.settings import get_base_dir
        return Path(get_base_dir())
    except Exception:
        return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()

# Engine id -> (preferred env var, folder names to auto-discover under BASE_DIR)
# Only Python engines appear here; the Node-based Agent TARS runtime is resolved
# separately (see agent_tars_cli).
_ENV_CANDIDATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "agent_reach": ("JARVIS_AGENTREACH_ENV", ("AgentReach", "agent-reach", "agent_reach")),
}

# Worker scripts live inside this package and are executed by the *engine's*
# interpreter, never by J.A.R.V.I.S.'s own.
WORKERS_DIR = Path(__file__).resolve().parent / "workers"

#: Relative path of the pinned Agent TARS CLI entry inside the project.
_TARS_CLI_REL = Path("node_modules") / "@agent-tars" / "cli" / "bin" / "cli.js"


def _read_config(key: str, default: Any = None) -> Any:
    """config.settings.get with a safe fallback (import may not be ready)."""
    try:
        from config.settings import get
        return get(key, default)
    except Exception:
        return default


def raw(key: str, default: Any = None) -> Any:
    """Public single-key config read (env override -> api_keys.json -> default)."""
    return _read_config(key, default)


def _read_feature(name: str, default: Any = None) -> Any:
    try:
        from config.settings import get_feature
        return get_feature(name, default)
    except Exception:
        return default


def enabled(engine: str) -> bool:
    """Per-engine switch: features.agent_tars / agent_reach.

    The key is the bare name: config.settings.get_feature() already looks it up
    inside api_keys.json's "features" map and honours JARVIS_FEATURE_<NAME>.
    """
    return bool(_read_feature(engine, True))


def _venv_of(folder: Path) -> Path:
    """Normalise a candidate to the directory that actually holds the venv."""
    if python_path(folder):
        return folder
    nested = folder / ".venv"
    if python_path(nested):
        return nested
    return folder


def env_root(engine: str) -> Path | None:
    """Locate a virtualenv root for *engine*; None when it is not installed.

    An override may name either the engine folder (``AgentReach``) or the venv
    itself (``AgentReach/.venv``); auto-discovery accepts both shapes too.
    """
    override = str(os.environ.get(_ENV_CANDIDATES[engine][0], "") or "").strip()
    if override:
        cand = _venv_of(Path(override))
        if python_path(cand):
            return cand

    _, folder_names = _ENV_CANDIDATES[engine]
    for name in folder_names:
        for parent in (BASE_DIR, BASE_DIR.parent):
            cand = _venv_of(parent / name)
            if python_path(cand):
                return cand

    # Last resort: a sibling directory holding a .venv with the right marker.
    try:
        for sibling in sorted(BASE_DIR.iterdir()):
            if not sibling.is_dir():
                continue
            squeezed = sibling.name.lower().replace("_", "").replace("-", "")
            if engine not in squeezed and engine.replace("_", "") not in squeezed:
                continue
            cand = _venv_of(sibling)
            if python_path(cand):
                return cand
    except OSError:
        pass
    return None


def python_path(venv: Path | os.PathLike | None) -> str | None:
    """The interpreter inside *venv*, or None when it does not exist."""
    if not venv:
        return None
    venv = Path(venv)
    names = ("Scripts/python.exe", "bin/python", "bin/python3") if os.name == "nt" \
        else ("bin/python", "bin/python3", "Scripts/python.exe")
    for rel in names:
        exe = venv / rel
        if exe.exists():
            return str(exe)
    return None


def interpreter(engine: str) -> str | None:
    """Interpreter for an engine, honouring an explicit ``<ENGINE>_PYTHON``."""
    env_key = f"{engine.upper()}_PYTHON"
    explicit = str(os.environ.get(env_key, "") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    root = env_root(engine)
    return python_path(root) if root else None


def worker(engine: str) -> Path:
    """Path of the in-venv worker script for *engine*."""
    return WORKERS_DIR / f"{engine}_worker.py"


def gemini_api_key() -> str:
    """The existing Google/Gemini key — reused, never duplicated."""
    try:
        from config.settings import gemini_api_key as _k
        return _k() or ""
    except Exception:
        return str(os.environ.get("GEMINI_API_KEY", "") or "")


# UI-TARS has no credential of its own in this project. The visual computer-use
# layer, Agent TARS and J.A.R.V.I.S. itself all run on the one Gemini key above,
# so there is no separate provider key here to configure or to leak.
_GEMINI_ONLY_PROVIDERS = frozenset({"gemini", "google", "google-genai"})


def agent_tars_provider_supported() -> bool:
    """True when TARS is pointed at the one supported model source (Gemini)."""
    return (agent_tars_provider() or "gemini").strip().lower() in _GEMINI_ONLY_PROVIDERS


def agent_tars_credential() -> tuple[str, str]:
    """(env var name, key) for Agent TARS: always the existing Gemini key.

    Returns ("", "") when that key is absent, so callers report the exact gap
    instead of inventing a substitute credential.
    """
    key = gemini_api_key()
    return ("GEMINI_API_KEY", key) if key else ("", "")


def gemini_model() -> str:
    return str(_read_config("ai_model", "") or "gemini-2.5-flash")


# ── local controllers (Playwright · pywinauto · UI-TARS · PyAutoGUI) ──────────
# One Settings switch per engine relationship the user asked for. Each is a
# permission *and* a presence switch: with a switch off the matching actions are
# refused by name, and no other controller is substituted for them.

#: Feature flag → what it turns on.
CONTROLLER_FEATURES: dict[str, str] = {
    "playwright_browser": "Playwright browser automation (browser_action)",
    "windows_automation": "pywinauto native Windows control (applications/windows)",
    "uitars_visual": "UI-TARS visual computer use on the Gemini model (visual_control)",
    "physical_input": "PyAutoGUI physical mouse/keyboard fallback",
    "power_control": "System power and session control (power_control)",
}
#: Master switch for everything that acts on this PC.
MASTER_FEATURE = "desktop_control"


def controller_enabled(name: str) -> bool:
    """One controller's switch, anded with the master PC-control switch."""
    if not enabled(MASTER_FEATURE):
        return False
    return bool(_read_feature(name, True))


def automation_dir() -> Path:
    """Where automation artifacts live (screenshots, downloads, browser profile)."""
    override = str(_read_config("automation_dir", "") or "").strip()
    if override:
        return Path(override)
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR) / "automation"
    except Exception:
        return BASE_DIR / "data" / "automation"


def downloads_dir() -> Path:
    return automation_dir() / "downloads"


def screenshots_dir() -> Path:
    return automation_dir() / "screenshots"


def browser_profile_dir() -> Path:
    """The JARVIS-owned browser profile: log in once, it persists."""
    override = str(_read_config("browser_profile_dir", "") or "").strip()
    if override:
        return Path(override)
    return automation_dir() / "browser_profile"


def browser_channel() -> str:
    """Browser channel for Playwright: '' = auto (installed Chrome/Edge, else
    the bundled Chromium). 'chromium' forces the bundled build."""
    return str(_read_config("browser_channel", "") or "").strip().lower()


def browser_headless() -> bool:
    """Visible by default — the user should see what J.A.R.V.I.S. is driving."""
    return bool(_read_config("browser_headless", False))


def browser_timeout() -> float:
    try:
        return max(5.0, min(300.0, float(_read_config("browser_timeout", 30))))
    except (TypeError, ValueError):
        return 30.0


# ── Agent TARS specifics ──────────────────────────────────────────────────────
# Agent TARS v0.3.0 is a Node runtime. Its reasoning/vision model is Gemini,
# configured with the SAME key J.A.R.V.I.S. already uses — no second provider and
# no second credential. `prompt_engineering` is the default tool-call engine
# because the native Gemini function-calling path is rejected by the
# generativelanguage API (it receives JSON-Schema `$schema`/`additionalProperties`
# fields it does not accept), which was verified against the installed package.

#: Default model for TARS's own agent loop (matches app.model_router's "default"
#: and "reasoning" roles, which both resolve to this moving alias).
_TARS_DEFAULT_MODEL = "gemini-flash-latest"
#: Tried when the default model is overloaded (503) or rate-limited (429) — the
#: "fast"/"fallback" role model.
_TARS_FALLBACK_MODEL = "gemini-flash-lite-latest"


def agent_tars_node() -> str:
    """The Node.js executable, discovered (never a hardcoded path)."""
    override = str(_read_config("agent_tars_node", "") or "").strip()
    if override and Path(override).exists():
        return override
    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        return found
    for guess in (os.environ.get("PROGRAMFILES"), os.environ.get("ProgramFiles"),
                  os.environ.get("LOCALAPPDATA")):
        if guess:
            cand = Path(guess) / "nodejs" / "node.exe"
            if cand.exists():
                return str(cand)
    return ""


def agent_tars_cli() -> str:
    """Path to the pinned Agent TARS CLI entry (``.../bin/cli.js``), or "".

    Order: explicit ``agent_tars_cli``/``JARVIS_AGENT_TARS_CLI`` override, then
    the project's pinned ``node_modules``, then any npx cache copy — so a machine
    that only ever ran ``npx @agent-tars/cli@latest`` still resolves, while a
    pinned install stays authoritative.
    """
    override = str(_read_config("agent_tars_cli", "")
                   or os.environ.get("JARVIS_AGENT_TARS_CLI", "") or "").strip()
    if override and Path(override).exists():
        return str(Path(override))

    pinned = BASE_DIR / _TARS_CLI_REL
    if pinned.exists():
        return str(pinned)

    local = os.environ.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA".lower())
    roots = [Path(local) / "npm-cache" / "_npx"] if local else []
    for root in roots:
        try:
            hits = sorted(root.glob("*/node_modules/@agent-tars/cli/bin/cli.js"))
        except OSError:
            hits = []
        if hits:
            return str(hits[-1])
    return ""


def agent_tars_mode() -> str:
    """'server' (default, real events + cancel) or 'oneshot' (single CLI call)."""
    mode = str(_read_config("agent_tars_mode", "server") or "server").strip().lower()
    return mode if mode in ("server", "oneshot") else "server"


def agent_tars_provider() -> str:
    """TARS's model provider. Gemini is native to v0.3.0; no second provider."""
    return str(_read_config("agent_tars_provider", "") or "gemini").strip().lower()


def agent_tars_model() -> str:
    return str(_read_config("agent_tars_model", "") or _TARS_DEFAULT_MODEL).strip()


def agent_tars_fallback_model() -> str:
    return str(_read_config("agent_tars_fallback_model", "")
               or _TARS_FALLBACK_MODEL).strip()


def agent_tars_tool_call_engine() -> str:
    """TARS tool-call engine. ``prompt_engineering`` is required for Gemini."""
    value = str(_read_config("agent_tars_tool_call_engine", "")
                or "prompt_engineering").strip().lower()
    return value if value in ("native", "prompt_engineering", "structured_outputs") \
        else "prompt_engineering"


def agent_tars_port() -> int:
    """Fixed server port, or 0 to let the adapter pick a free one."""
    try:
        return max(0, int(_read_config("agent_tars_port", 0) or 0))
    except (TypeError, ValueError):
        return 0


def agent_tars_timeout() -> float:
    try:
        return max(30.0, float(_read_config("agent_tars_timeout", 300)))
    except (TypeError, ValueError):
        return 300.0


def agent_tars_health_timeout() -> float:
    """How long to wait for the server to answer /api/v1/health after start."""
    try:
        return max(10.0, float(_read_config("agent_tars_health_timeout", 60)))
    except (TypeError, ValueError):
        return 60.0


def agent_tars_retries() -> int:
    """Extra attempts after a transient provider error (503/429)."""
    try:
        return max(0, min(3, int(_read_config("agent_tars_retries", 1))))
    except (TypeError, ValueError):
        return 1


def agent_tars_browser_control() -> str:
    """Browser control strategy: hybrid (default), dom, or visual-grounding."""
    value = str(_read_config("agent_tars_browser_control", "")
                or "hybrid").strip().lower()
    return value if value in ("hybrid", "dom", "visual-grounding") else "hybrid"


def agent_tars_cdp_endpoint() -> str:
    """CDP endpoint of an already-running Chrome (e.g. a signed-in profile)."""
    return str(_read_config("agent_tars_cdp_endpoint", "") or "").strip()


def agent_tars_workspace() -> Path:
    """TARS working directory (its own sandbox), defaulting to a project subdir."""
    override = str(_read_config("agent_tars_workspace", "") or "").strip()
    if override:
        return Path(override)
    try:
        from config.settings import DATA_DIR
        return Path(DATA_DIR) / "agent_tars_workspace"
    except Exception:
        return BASE_DIR / "data" / "agent_tars_workspace"


def agent_tars_planner() -> bool:
    """Enable TARS's planner for multi-step tasks."""
    return bool(_read_config("agent_tars_planner", True))


def agent_tars_search_provider() -> str:
    """TARS web-search provider: browser_search (default), tavily, bing_search."""
    value = str(_read_config("agent_tars_search_provider", "")
                or "browser_search").strip().lower()
    return value if value in ("browser_search", "tavily", "bing_search") \
        else "browser_search"


# ── Agent Reach specifics ─────────────────────────────────────────────────────

def agent_reach_channels() -> str:
    """Comma-separated allow-list; empty means "whatever doctor reports live"."""
    return str(_read_config("agent_reach_channels", "") or "").strip()


def agent_reach_timeout() -> float:
    try:
        return max(20.0, float(_read_config("agent_reach_timeout", 180)))
    except (TypeError, ValueError):
        return 180.0


def agent_reach_max_results() -> int:
    try:
        return max(1, min(30, int(_read_config("agent_reach_max_results", 8))))
    except (TypeError, ValueError):
        return 8


# ── shared process policy ─────────────────────────────────────────────────────

def kill_on_close() -> bool:
    """Terminate J.A.R.V.I.S.-owned engine workers on shutdown (default on)."""
    return bool(_read_feature("capabilities_kill_children_on_close", True))


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def summary() -> dict:
    """Secret-free configuration summary for diagnostics and logs."""
    return {
        "base_dir": str(BASE_DIR),
        "agent_tars": {
            "enabled": enabled("agent_tars"),
            "node": agent_tars_node(),
            "cli": agent_tars_cli(),
            "mode": agent_tars_mode(),
            "provider": agent_tars_provider(),
            "model": agent_tars_model(),
            "fallback_model": agent_tars_fallback_model(),
            "tool_call_engine": agent_tars_tool_call_engine(),
            "browser_control": agent_tars_browser_control(),
            "cdp_endpoint": agent_tars_cdp_endpoint(),
            "workspace": str(agent_tars_workspace()),
            "planner": agent_tars_planner(),
            "search_provider": agent_tars_search_provider(),
            "gemini_key_present": bool(gemini_api_key()),
            "provider_supported": agent_tars_provider_supported(),
            "credential": "gemini_api_key",
        },
        "agent_reach": {
            "enabled": enabled("agent_reach"),
            "env": str(env_root("agent_reach") or ""),
            "interpreter": interpreter("agent_reach") or "",
            "channels": agent_reach_channels(),
        },
        "local_controllers": {
            "master_switch": enabled(MASTER_FEATURE),
            **{name: controller_enabled(name) for name in CONTROLLER_FEATURES},
            "automation_dir": str(automation_dir()),
            "browser_profile": str(browser_profile_dir()),
            "browser_channel": browser_channel() or "auto",
            "browser_headless": browser_headless(),
            "browser_timeout": browser_timeout(),
            "vision_model": "model_router role 'vision' (Gemini)",
        },
    }
