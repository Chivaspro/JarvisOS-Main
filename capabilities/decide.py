"""Which controller serves this request? One place that decides.

Every control tool J.A.R.V.I.S. owns is good at one thing, and the model has to
pick between them for a spoken request. Left to itself it guesses — that is how
"play music" ended up in an internal web page on a machine with Spotify
installed, and how "open YouTube" can end up in a raw browser when the HUD has a
perfectly good panel for it.

This module is the decision, made once and testable:

* :func:`classify` reads a request and says what KIND of work it is;
* :func:`plan` turns that into an ordered list of concrete controllers to try,
  grounded in what is really installed, really running and really switched on;
* :func:`report` renders a plan as text for the model.

It executes **nothing** and imports no controller at module level: it asks the
existing probes (``capabilities/media.py``, ``capabilities/router.py``) for the
facts, and answers with reasons. The model stays the one that acts, and every
handler that follows a plan is following an order it can also explain.

Facts are injected for tests (``plan(..., facts={...})``), which is what makes
the policy measurable without a Spotify install, a Playwright browser, or a UI.
"""

from __future__ import annotations

#: The kinds of work a request can be. ``chat`` means no controller at all.
INTENTS = ("media", "app", "web", "screen", "research", "chat")

# ── classification vocabulary ─────────────────────────────────────────────────

_MEDIA_WORDS = (
    "music", "song", "songs", "track", "tracklist", "playlist", "album", "artist",
    "spotify", "vlc", "itunes", "media player", "pause", "resume", "unpause",
    "skip", "next song", "previous song", "volume", "mute", "unmute", "audio",
)
_WEB_WORDS = (
    "website", "web page", "webpage", "url", "browser", "in chrome", "in edge",
    "in firefox", "http://", "https://", "www.", ".com", ".org", ".net", ".io",
    "google for", "search the web", "search online", "in the browser",
)
_SCREEN_WORDS = (
    "click", "double click", "double-click", "right click", "right-click", "tap ",
    "type into", "type in", "press the", "press enter", "hotkey", "the button",
    "this button", "that icon", "on my screen", "on the screen", "drag ", "scroll",
    "screenshot", "screen capture", "what is on my screen",
)
_RESEARCH_WORDS = (
    "research", "look up", "look into", "find information", "find out about",
    "gather information", "compare prices", "find sources", "investigate",
)
_APP_WORDS = (
    "open ", "launch ", "start the ", "close ", "quit ", "focus ", "bring up ",
    "switch to ",
)


def _tokens(request: str) -> str:
    return " ".join(str(request or "").lower().split())


def _score(text: str, words: tuple[str, ...], weight: int = 2) -> int:
    return sum(weight for word in words if word in text)


def classify(request: str) -> tuple[str, str]:
    """The intent behind one request, with the wording that decided it.

    Deliberately conservative: anything that does not clearly name work returns
    ``chat``, because inventing automation for a conversational sentence is the
    expensive mistake.
    """
    text = _tokens(request)
    if not text:
        return ("chat", "there is no request to route")
    if text.startswith("play ") or text.startswith("play:") or text == "play":
        return ("media", "'play …' is a request to start something playing")
    scores = {
        "media": _score(text, _MEDIA_WORDS),
        "web": _score(text, _WEB_WORDS),
        "screen": _score(text, _SCREEN_WORDS, 1),
        "research": _score(text, _RESEARCH_WORDS, 2),
        "app": _score(text, _APP_WORDS, 1),
    }
    best = max(scores.values())
    if best <= 0:
        return ("chat", "nothing in the wording asks for the computer to be driven")
    # A named application is the strongest signal there is: "open Spotify" is an
    # app request even though it also contains a media word.
    if scores["app"] and _named_application(text):
        return ("app", "the request names an application to open or close")
    # Ties resolve towards the most specific controller, never towards a guess
    # that would type or click for a request that only mentioned a topic.
    for intent in ("screen", "media", "app", "web", "research"):
        if scores[intent] == best:
            return (intent, f"the wording matches the {intent} vocabulary")
    return ("chat", "no controller vocabulary matched")


def _named_application(text: str) -> bool:
    """True when a known application name appears in the request."""
    try:
        from capabilities import desktop
        aliases = desktop.APP_ALIASES
    except Exception:
        return False
    return any(_norm(alias) and _norm(alias) in text for alias in aliases)


def _norm(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


# ── live facts ────────────────────────────────────────────────────────────────

#: Settings switches this layer reads. Each gates the matching candidate.
_SWITCHES = ("native_music_apps", "webview_engine", "webview_music",
             "desktop_control", "windows_automation", "physical_input",
             "playwright_browser", "uitars_visual", "power_control", "web_task",
             "webview")


def _switch(name: str, default: bool = True) -> bool:
    try:
        from config.settings import get_feature
        return bool(get_feature(name, default))
    except Exception:
        return bool(default)


def facts() -> dict:
    """The real state of every controller this layer routes between.

    All four probes are the controllers' own (they cache and never raise), so
    this is cheap enough to call before answering one question.
    """
    out: dict = {"switches": {name: _switch(name) for name in _SWITCHES}}
    try:
        from capabilities import media
        out["media"] = media.status()
    except Exception as exc:
        out["media"] = {"available": False, "detail": f"probe failed: {exc}"}
    try:
        from capabilities import router
        out["controllers"] = router.local_status()
    except Exception as exc:
        out["controllers"] = {"_error": f"probe failed: {exc}"}
    try:
        from capabilities import agent_tars
        out["agent_tars"] = agent_tars.status()
    except Exception as exc:
        out["agent_tars"] = {"usable": False, "detail": f"probe failed: {exc}"}
    try:
        from capabilities import agent_reach
        out["agent_reach"] = agent_reach.status()
    except Exception as exc:
        out["agent_reach"] = {"available": False, "detail": f"probe failed: {exc}"}
    return out


#: Live facts for :func:`plan` — named so a caller-supplied ``facts`` argument
#: can never shadow the probe itself.
_live_facts = facts


def _usable(f: dict, name: str) -> bool:
    info = (f.get("controllers") or {}).get(name) or {}
    return bool(info.get("usable"))


def _controller_detail(f: dict, name: str) -> str:
    info = (f.get("controllers") or {}).get(name) or {}
    return str(info.get("detail") or "")


def _candidate(controller: str, tool: str, reason: str, ready: bool,
               args: dict | None = None) -> dict:
    return {"controller": controller, "tool": tool, "reason": reason,
            "ready": bool(ready), "args": dict(args or {})}


# ── the chains ────────────────────────────────────────────────────────────────

def _media_chain(f: dict, request: str) -> list[dict]:
    switches = f.get("switches") or {}
    media = f.get("media") or {}
    query = _play_query(request)
    chain: list[dict] = []
    installed = media.get("installed") or {}
    if switches.get("native_music_apps", True) and media.get("available"):
        label = ", ".join(str(k).title() for k in installed) or "a media application"
        chain.append(_candidate(
            "media", "play_music",
            f"an installed player was found ({label}) and native music apps are on",
            True, {"query": query}))
    elif not switches.get("native_music_apps", True):
        chain.append(_candidate(
            "media", "play_music",
            "native music apps are switched off in Settings, so the installed "
            "player is not used", False, {"query": query}))
    else:
        chain.append(_candidate(
            "media", "play_music",
            "no native music player (Spotify, VLC) is installed on this machine",
            False, {"query": query}))
    if switches.get("webview_engine", True) and switches.get("webview_music", True):
        chain.append(_candidate(
            "webview", "play_music",
            "the internal WebView player is enabled in Settings", True,
            {"query": query}))
    else:
        chain.append(_candidate(
            "webview", "play_music",
            "the internal WebView player is switched off in Settings", False,
            {"query": query}))
    chain.append(_candidate(
        "youtube", "youtube_video",
        "the YouTube action can play a song without any local player", True,
        {"query": query}))
    return chain


def _app_chain(f: dict, request: str) -> list[dict]:
    switches = f.get("switches") or {}
    target = _app_target(request)
    chain: list[dict] = []
    if not switches.get("desktop_control", True):
        ready, reason = False, "desktop control is switched off in Settings"
    elif not switches.get("windows_automation", True):
        ready, reason = False, "Windows application automation is switched off in Settings"
    elif not _usable(f, "desktop"):
        ready, reason = False, (_controller_detail(f, "desktop")
                                or "the desktop controller is not available")
    else:
        ready, reason = True, "pywinauto can launch and focus the application"
    chain.append(_candidate("desktop", "pc_control", reason, ready,
                            {"action": "open_application", "app": target}))
    tars = f.get("agent_tars") or {}
    chain.append(_candidate(
        "agent_tars", "computer_control",
        ("Agent TARS can run the whole desktop job"
         if tars.get("usable") else
         f"Agent TARS is not ready ({tars.get('detail') or 'unavailable'})"),
        bool(tars.get("usable")), {"task": request}))
    return chain


def _web_chain(f: dict, request: str) -> list[dict]:
    switches = f.get("switches") or {}
    url = _first_url(request)
    chain = [
        _candidate("playwright", "browser_action",
                   ("Playwright is installed and can drive the page element by "
                    "element" if _usable(f, "playwright")
                    else _controller_detail(f, "playwright")
                    or "Playwright is not available"),
                   _usable(f, "playwright") and switches.get("playwright_browser", True),
                   {"action": "browser_open", "url": url}),
    ]
    tars = f.get("agent_tars") or {}
    chain.append(_candidate(
        "agent_tars", "browser_control",
        ("Agent TARS can run the whole browser workflow" if tars.get("usable")
         else f"Agent TARS is not ready ({tars.get('detail') or 'unavailable'})"),
        bool(tars.get("usable")) and switches.get("web_task", True),
        {"task": request, "url": url}))
    webview = switches.get("webview", True)
    chain.append(_candidate(
        "webview", "jarvis_window",
        ("the internal HUD panel can show the page" if webview
         else "the internal WebView is switched off in Settings"),
        webview, {"window": "webview", "action": "open", "query": url}))
    return chain


def _screen_chain(f: dict, request: str) -> list[dict]:
    switches = f.get("switches") or {}
    visual = (_usable(f, "uitars_visual") and switches.get("uitars_visual", True)
              and switches.get("desktop_control", True))
    physical = (_usable(f, "desktop") and switches.get("physical_input", True)
                and switches.get("desktop_control", True))
    return [
        _candidate("uitars", "visual_control",
                   ("UI-TARS can find the target on screen and act on it"
                    if visual else
                    _controller_detail(f, "uitars_visual")
                    or "visual control is switched off in Settings"),
                   visual, {"instruction": request}),
        _candidate("pyautogui", "pc_control",
                   ("PyAutoGUI can type or click at a known position" if physical
                    else "physical input is switched off in Settings"),
                   physical, {"instruction": request}),
    ]


def _research_chain(f: dict, request: str) -> list[dict]:
    reach = f.get("agent_reach") or {}
    chain = [
        _candidate("agent_reach", "web_research",
                   ("Agent Reach can research this across its live channels"
                    if reach.get("available")
                    else f"Agent Reach is not available ({reach.get('detail') or 'unavailable'})"),
                   bool(reach.get("available")), {"query": request}),
    ]
    chain.extend(_web_chain(f, request))
    return chain


_CHAINS = {"media": _media_chain, "app": _app_chain, "web": _web_chain,
           "screen": _screen_chain, "research": _research_chain}


# ── request parsing helpers ───────────────────────────────────────────────────

def _play_query(request: str) -> str:
    text = " ".join(str(request or "").split()).strip()
    low = text.lower()
    for prefix in ("play music", "play some music", "play a song", "play song",
                   "play me some", "play me", "play"):
        if low.startswith(prefix):
            return text[len(prefix):].strip(" :-–—,") or ""
    return ""


def _app_target(request: str) -> str:
    """The application named in the request, as the controller expects it."""
    text = _tokens(request)
    try:
        from capabilities import desktop
        aliases = desktop.APP_ALIASES
    except Exception:
        return ""
    for alias in sorted(aliases, key=len, reverse=True):
        if _norm(alias) and _norm(alias) in text:
            return _norm(alias)
    return ""


def _first_url(request: str) -> str:
    for token in str(request or "").split():
        if token.startswith(("http://", "https://")):
            return token.strip(".,;)\"'")
    return ""


# ── the public entry points ───────────────────────────────────────────────────

def plan(request: str = "", *, intent: str = "", facts: dict | None = None) -> dict:
    """Recommend a controller for one request, with reasons and live evidence.

    ``intent`` overrides classification when the caller already knows what the
    request is. Facts are probed when not supplied (tests inject them).
    """
    state = facts if facts is not None else _live_facts()
    wanted = str(intent or "").strip().lower()
    if wanted in INTENTS:
        kind, why = wanted, "the caller named the intent"
    else:
        kind, why = classify(request)

    if kind == "chat":
        return {"intent": "chat", "controller": "", "tool": "", "args": {},
                "reason": why, "candidates": [], "ready": False,
                "facts": _fact_summary(state)}

    chain = _CHAINS[kind](state, request)
    chosen = next((c for c in chain if c["ready"]), None)
    return {
        "intent": kind,
        "reason": why,
        "controller": (chosen or chain[0])["controller"],
        "tool": (chosen or chain[0])["tool"],
        "args": dict((chosen or chain[0])["args"]),
        "ready": chosen is not None,
        "candidates": chain,
        "facts": _fact_summary(state),
    }


def _fact_summary(state: dict) -> dict:
    """The few facts that decided the route, kept for the model to see."""
    media = state.get("media") or {}
    controllers = state.get("controllers") or {}
    return {
        "media_installed": sorted((media.get("installed") or {}).keys()),
        "media_preferred": str(media.get("preferred") or ""),
        "desktop": bool(_usable(state, "desktop")),
        "playwright": bool(_usable(state, "playwright")),
        "uitars": bool(_usable(state, "uitars_visual")),
        "power": bool(_usable(state, "power_control")),
        "agent_tars": bool((state.get("agent_tars") or {}).get("usable")),
        "agent_reach": bool((state.get("agent_reach") or {}).get("available")),
        "switches_off": sorted(k for k, v in (state.get("switches") or {}).items()
                               if not v),
    }


def report(request: str = "", *, intent: str = "") -> str:
    """One text block for Gemini: the route, the reasons, the fallbacks."""
    result = plan(request, intent=intent)
    if result["intent"] == "chat":
        return (f"No controller is needed for this: {result['reason']}. Answer it "
                "as a conversation.")
    lines = [f"CONTROL PLAN — intent: {result['intent']}"]
    if result["ready"]:
        lines.append(f"use: {result['controller']} via {result['tool']}")
    else:
        lines.append("use: nothing is ready — tell the user what is missing "
                     "instead of substituting another engine")
    for index, candidate in enumerate(result["candidates"]):
        state = "ready" if candidate["ready"] else "NOT ready"
        mark = "→" if index == 0 else " "
        lines.append(f"  {mark} {index + 1}. {candidate['controller']} "
                     f"({candidate['tool']}) [{state}] — {candidate['reason']}")
    facts_now = result["facts"]
    lines.append("facts: " + ", ".join(f"{k}={v}" for k, v in facts_now.items()))
    if result["ready"]:
        lines.append("Call that tool with the arguments shown; read its result "
                     "before claiming anything was done.")
    return "\n".join(lines)


__all__ = ["INTENTS", "classify", "facts", "plan", "report"]
