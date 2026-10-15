"""Agent Reach adapter — the web research / internet access layer (task §4).

Agent Reach is a channel router: it owns the health of 16 research channels and
points each one at a real upstream tool (yt-dlp, gh CLI, mcporter/Exa, OpenCLI,
feedparser, Jina Reader...). This adapter therefore does two things, both by
invoking the installed package in its own environment:

* ``doctor``   — which channels are live and through which backend;
* ``research`` — actually fetch findings for a query or read one URL.

It is never a mouse/keyboard controller (task §4) and it never falls back to a
different engine when it is unavailable (task §18).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

_ENGINE = "agent_reach"
_DOMAIN = "research"

_lock = threading.Lock()
_doctor_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_DOCTOR_TTL = 120.0


def _log(message: str) -> None:
    """Internal diagnostics only — never routed into the visible ChatLog (§19)."""
    try:
        from observability.logger import log_event
        log_event("INFO", "capabilities", "agent_reach", detail=message[:400])
        return
    except Exception:
        pass
    print(f"[AgentReach] {message}")


def available(refresh: bool = False) -> bool:
    from capabilities import config
    if not config.enabled(_ENGINE):
        return False
    return bool(config.interpreter(_ENGINE))


def doctor(timeout_s: float = 90.0, force: bool = False) -> dict:
    """Agent Reach's own health report, cached briefly (probing touches net)."""
    with _lock:
        age = time.time() - _doctor_cache["at"]
        if not force and _doctor_cache["payload"] and age < _DOCTOR_TTL:
            return _doctor_cache["payload"]

    from capabilities import config, proc
    result = proc.run_worker(_ENGINE, {"action": "doctor"},
                             timeout_s=timeout_s, label="agent_reach:doctor")
    data = result.get("data") or {}
    payload = {
        "ok": bool(result.get("ok")),
        "version": result.get("engine_version") or "",
        "channels": data.get("channels") or {},
        "active": data.get("active") or [],
        "summary": result.get("summary") or "",
        "error": result.get("error") or "",
        "elapsed_s": result.get("elapsed_s", 0),
        "python": proc.python_version(_ENGINE),
        "env": str(config.env_root(_ENGINE) or ""),
    }
    with _lock:
        _doctor_cache["at"] = time.time()
        _doctor_cache["payload"] = payload
    return payload


def status() -> dict:
    """Probe result for diagnostics and skill_query (never raises)."""
    from capabilities import config
    base = {"name": _ENGINE, "kind": "subprocess",
            "env": str(config.env_root(_ENGINE) or ""),
            "interpreter": config.interpreter(_ENGINE) or "",
            "enabled": config.enabled(_ENGINE)}
    if not base["interpreter"]:
        return {**base, "available": False,
                "missing": ["AgentReach virtual environment not found"],
                "detail": "no Python environment for Agent Reach"}
    if not base["enabled"]:
        return {**base, "available": False, "missing": ["switched off in Settings"],
                "detail": "disabled in Settings"}
    doc = doctor()
    active = doc.get("active") or []
    missing = _remediations(doc.get("channels") or {})
    return {**base,
            "available": bool(doc.get("ok")),
            "version": doc.get("version", ""),
            "python": doc.get("python", ""),
            "active_channels": active,
            "missing": missing,
            "detail": (f"{len(active)} live channel(s): {', '.join(active[:8])}"
                       if active else "no channel reports an active backend"),
            "error": doc.get("error", "")}


def _remediations(channels: dict) -> list:
    """Readable 'how to enable it' lines straight from Agent Reach's own advice."""
    out = []
    for name, info in (channels or {}).items():
        if not isinstance(info, dict) or info.get("active_backend"):
            continue
        msg = re.sub(r"\s+", " ", str(info.get("message") or ""))
        m = re.search(r"(运行|安装|install|pipx|npm|uvx|apt|brew)[^。]{0,120}", msg)
        out.append(f"{name}: {(m.group(0) if m else msg)[:110] or 'needs setup'}")
    return out[:8]


def research(query: str = "", platform: str = "", url: str = "",
             max_results: int = 0, timeout_s: float = 0) -> dict:
    """Run one real Agent Reach research/read task. Never raises (§18)."""
    from capabilities import config
    from capabilities import proc

    if not available():
        return {"ok": False, "backend": _ENGINE,
                "output": ("Web research is unavailable: Agent Reach's environment "
                           "is not installed or is switched off in Settings. "
                           "No other research engine was used."),
                "reason": "unavailable"}

    q = str(query or "").strip()
    u = str(url or "").strip()
    if not q and not u:
        return {"ok": False, "backend": _ENGINE,
                "output": "Agent Reach needs a query or a URL.",
                "reason": "no_input"}

    payload = {
        "action": "read" if (u and not q) else "research",
        "query": q,
        "url": u,
        "platform": str(platform or "").strip(),
        "max_results": int(max_results or config.agent_reach_max_results()),
        "timeout": max(20.0, float(config.agent_reach_timeout())),
        "channels_allow": config.agent_reach_channels(),
    }
    _log(f"research start: {payload['action']} {q[:60] or u[:60]}")
    started = time.monotonic()
    result = proc.run_worker(_ENGINE, payload,
                             timeout_s=float(timeout_s or (config.agent_reach_timeout() + 45)),
                             label=f"agent_reach:{payload['action']}")
    data = result.get("data") or {}
    ok = bool(result.get("ok"))
    _log(f"research end ok={ok} elapsed={time.monotonic() - started:.1f}s "
         f"channels={data.get('channels_used')}")
    return {
        "ok": ok,
        "backend": _ENGINE,
        "output": _render(q or u, result, data),
        "reason": "" if ok else (result.get("error_code") or "no_results"),
        "findings": data.get("findings") or [],
        "channels_used": data.get("channels_used") or [],
        "attempts": data.get("attempts") or [],
        "elapsed_s": result.get("elapsed_s", round(time.monotonic() - started, 2)),
        "verification": {
            "verified": ok,
            "how": ("agent_reach returned real records from a named backend"
                    if ok else "no channel returned any record"),
            "channels": data.get("channels_used") or [],
        },
    }


def _render(query: str, result: dict, data: dict) -> str:
    """Flat text for Gemini to reason over — the structured list stays in 'findings'."""
    if result.get("action") == "read" and data.get("text"):
        return (f"SOURCE {data.get('url')} (via {data.get('backend')})\n"
                f"{str(data['text'])[:6000]}")
    findings = data.get("findings") or []
    if not findings:
        attempts = data.get("attempts") or []
        blocked = "; ".join(f"{a.get('channel')}: {a.get('detail') or 'unavailable'}"
                            for a in attempts if not a.get("ok"))
        head = result.get("summary") or f"Agent Reach returned nothing for '{query}'."
        return f"{head}" + (f"\nUnreachable channels -> {blocked}" if blocked else "")
    lines = [result.get("summary") or f"Agent Reach results for '{query}':", ""]
    for i, f in enumerate(findings[:25], 1):
        title = f.get("title") or f.get("url") or f"Result {i}"
        url = f.get("url") or ""
        snip = f.get("snippet") or ""
        src = f.get("source") or ""
        backend = f.get("backend") or ""
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if snip:
            lines.append(f"   {snip[:300]}")
        if src or backend:
            lines.append(f"   [channel {src} · backend {backend}]")
    lines.append("")
    lines.append("Answer only from these records; say which channel each fact came from.")
    return "\n".join(lines)


def health_check() -> "Check":
    """Diagnostics Check for the research backend.

    The state comes from what Agent Reach's own doctor actually reported on this
    machine, not from the folder merely existing.
    """
    from core.diagnostics import Check, ONLINE, DEGRADED, OFFLINE, ERROR
    st = status()
    if not st.get("interpreter"):
        return Check("agent_reach", OFFLINE,
                     "Agent Reach environment not found next to the project",
                     "Set JARVIS_AGENTREACH_ENV to the folder containing .venv.")
    if not st.get("enabled"):
        return Check("agent_reach", OFFLINE, "switched off in Settings",
                     "Enable Agent Reach in Settings.")
    if st.get("error") and not st.get("version"):
        return Check("agent_reach", ERROR, st["error"],
                     "Run the Agent Reach installer in its own environment.")
    active = st.get("active_channels") or []
    state = ONLINE if (st.get("available") and active) else DEGRADED
    return Check("agent_reach", state,
                 st.get("detail") or "doctor reported nothing usable",
                 "" if active else "agent-reach install --system  (enables channels)",
                 {"version": st.get("version", ""),
                  "python": st.get("python", ""),
                  "channels": active[:12],
                  "setup_hints": (st.get("missing") or [])[:4]})
