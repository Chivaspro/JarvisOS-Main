"""JARVIS skill registry — one declarative map of everything JARVIS can do.

Why this exists
---------------
The abilities already live in the project (actions/, vision/, memory/, dev_agent,
...). What was missing was a single place that
says, for each capability: *what it is for, when it should be picked, which
tools implement it, what configuration it needs, and what it usually hands off
to*. That is all this module is.

Design rules
------------
* Metadata + routing + prompt text only — no work happens here, and no existing
  tool/action module is reimplemented or replaced.
* Zero heavy imports: safe to import from the Qt UI thread, the asyncio live
  loop, or a background worker.
* One skill = one small declaration. Adding a capability later means appending a
  ``Skill`` entry, not touching the other skills.
* Everything degrades: an unknown skill key, a missing config file or a failed
  route never raises to the caller.

Routing is deliberately deterministic (word-boundary matching over declared
triggers, weighted by priority) so the same request always selects the same
skills, and so it costs microseconds with no model round trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "Skill",
    "SKILLS",
    "get_skill",
    "route",
    "enabled_keys",
    "disabled_keys",
    "prompt_block",
    "chains_for",
    "describe",
    "report",
    "handle_query",
]


@dataclass(frozen=True)
class Skill:
    """Declarative description of one capability of the assistant."""

    key: str                       # stable id, e.g. "engineering"
    name: str                      # human name for speech/diagnostics
    purpose: str                   # one line: what it is for
    use_when: tuple[str, ...] = ()  # natural-language "pick me when ..." hints
    triggers: tuple[str, ...] = ()  # routing phrases (lower-case, word matched)
    tools: tuple[str, ...] = ()     # existing tool names that implement it
    requires: tuple[str, ...] = ()  # feature flags that must be enabled
    needs: tuple[str, ...] = ()     # external requirements (api key, device...)
    priority: int = 50              # tie-break weight, higher = picked sooner
    chains: tuple[str, ...] = ()    # skills this one usually hands off to
    reports: str = "Speaks the verified outcome; never claims unverified work."
    # compare=False keeps the dict out of __eq__/__hash__, so a Skill stays
    # hashable and can be used in sets without a surprise TypeError.
    metadata: Mapping[str, str] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        # A bare string is iterable, so a declaration missing its trailing comma
        # would quietly become ('h','i','n','t'). Normalise every phrase field.
        for name in ("use_when", "triggers", "tools", "requires", "needs",
                     "chains"):
            value = getattr(self, name)
            if isinstance(value, str):
                object.__setattr__(self, name, (value,))


# ── the registry ──────────────────────────────────────────────────────────────
# `tools` names are the real tool names declared in main.py; `requires` names are
# real keys from config/api_keys.json → features (checked live, so revoking a
# permission disables the skill immediately).

SKILLS: tuple[Skill, ...] = (
    Skill(
        key="engineering",
        name="Engineering",
        purpose="Read, change and extend the user's own code and projects.",
        use_when=("the user asks to change, build, extend or repair a project",
                  "a request implies editing files in a repository"),
        triggers=("code", "codebase", "project", "script", "function", "class",
                  "bug", "compile", "refactor", "repository", "repo", "module",
                  "implement", "add a feature", "develop", "program"),
        tools=("code_helper", "dev_agent", "file_controller", "file_processor"),
        priority=80,
        chains=("debugging", "ui_engineering", "files", "monitoring"),
        metadata={"rule": "inspect before editing; reuse before rebuilding"},
    ),
    Skill(
        key="debugging",
        name="Debugging",
        purpose="Find the actual cause of a failure from evidence, then fix it.",
        use_when=("something is broken, throws, crashes or behaves wrongly",
                  "the user shows an error, a log or a stack trace"),
        triggers=("error", "errors", "traceback", "exception", "crash", "broken",
                  "not working", "doesn't work", "does not work", "fails",
                  "failing", "stack trace", "log", "debug", "why is it"),
        tools=("code_helper", "dev_agent", "system_status", "recall_memory"),
        priority=85,
        chains=("engineering", "files", "monitoring"),
        metadata={"rule": "read the real error first; never claim a fix that was not verified"},
    ),
    Skill(
        key="ui_engineering",
        name="UI Engineering",
        purpose="Change and repair the assistant's own interface without redesigning it.",
        use_when=("a widget, layout, animation or panel is wrong or needs adjusting",
                  "the user asks for a small visual or interactive change"),
        triggers=("ui", "interface", "widget", "layout", "panel", "button",
                  "animation", "pyqt", "qt", "css", "stylesheet", "window",
                  "theme", "hud", "logo", "webcam view"),
        tools=("code_helper", "dev_agent", "jarvis_window"),
        priority=60,
        chains=("engineering", "debugging"),
        metadata={"rule": "preserve the existing design; fix the cause, never mask it"},
    ),
    Skill(
        key="system_engineering",
        name="System Engineering",
        purpose="Inspect and adjust this machine — resources, processes, services, environment.",
        use_when=("the user asks about their computer's health, performance or configuration",
                  "a task needs real measured system data"),
        triggers=("cpu", "ram", "memory usage", "gpu", "temperature", "battery",
                  "slow", "performance", "process", "processes", "task manager",
                  "disk", "storage", "uptime", "system", "network"),
        tools=("system_status", "performance_scan", "manage_monitor"),
        priority=70,
        chains=("monitoring", "debugging"),
        metadata={"rule": "report only measured values; never invent statistics"},
    ),
    Skill(
        key="diagnostics",
        name="Diagnostics",
        purpose=("Run JARVIS's own health checks, inventory the installed tools, "
                 "validate or repair the configuration, and answer 'why is this broken?'"),
        use_when=("the user asks what is wrong, asks for diagnostics, or asks about "
                  "installed tools and settings integrity",
                  "something failed and the cause is not obvious"),
        triggers=("diagnostics", "diagnostic", "health check", "health", "self test",
                  "what tools do i have", "installed tools", "why is jarvis broken",
                  "why is this broken", "fix jarvis", "fix my settings", "fix settings",
                  "check settings", "what changed", "restore settings",
                  "last good configuration", "safe mode", "run checks",
                  "model roles", "which model", "what model", "model router"),
        tools=("diagnostics",),
        priority=86,
        chains=("debugging", "system_engineering", "verification"),
        metadata={"rule": "report only checks that actually ran; never claim a pass "
                          "for something that was not verified"},
    ),
    Skill(
        key="planning",
        name="Task Planning",
        purpose="Break a multi-step job into ordered steps and track them to completion.",
        use_when=("a request needs three or more dependent actions",
                  "the user describes a workflow rather than a single action"),
        triggers=("plan", "workflow", "steps", "step by step", "first then",
                  "and then", "after that", "multi step", "handle it", "take care of it",
                  "do the whole", "everything", "one by one"),
        tools=("task_plan",),
        priority=90,
        chains=("research", "files", "verification"),
        metadata={"rule": "understand -> plan -> act -> check -> finish -> report"},
    ),
    Skill(
        key="task_memory",
        name="Task Memory",
        purpose="Remember an in-flight job so work survives interruptions and restarts.",
        use_when=("a task is interrupted, must be resumed, or the user says continue",
                  "several different jobs are in flight at once"),
        triggers=("continue", "resume", "keep going", "where were we", "carry on",
                  "pick up where", "what was i doing", "unfinished", "pending",
                  "still running", "left off"),
        tools=("task_plan", "recall_memory"),
        priority=88,
        chains=("planning",),
        metadata={"store": "brain memory tasks table"},
    ),
    Skill(
        key="research",
        name="Research",
        purpose="Answer questions that need external, current or verified information.",
        use_when=("the answer depends on facts outside memory",
                  "the user asks for comparisons, prices, documentation, news"),
        triggers=("search", "look up", "google", "research", "find out",
                  "latest", "news", "current", "price", "prices", "cost",
                  "compare", "comparison", "which is better", "reviews",
                  "documentation", "docs", "how much", "who won", "weather"),
        tools=("web_search", "youtube_video", "weather_report"),
        requires=("webview",),
        needs=("internet",),
        priority=75,
        chains=("verification", "memory", "conversation"),
        metadata={"rule": "prefer official and trustworthy sources; never fabricate them"},
    ),
    Skill(
        key="desktop_control",
        name="Desktop Control",
        purpose=("Operate this PC through Agent TARS — launching and running "
                 "applications, commands and files on the desktop."),
        use_when=("the user wants something done in a desktop application, not a website",
                  "a request refers to clicking, opening, switching or typing into a window"),
        triggers=("open notepad", "open vscode", "open vs code", "open visual studio",
                  "open task manager", "open control panel", "open excel", "open word",
                  "open vlc", "open spotify", "open file explorer", "open calculator",
                  "launch", "start the app", "close the app", "close this window",
                  "switch window", "alt tab", "minimise", "maximise", "click",
                  "double click", "right click", "scroll", "drag", "press",
                  "hotkey", "keyboard shortcut", "type into", "type this into",
                  "on my desktop", "the settings button", "look at my screen and click",
                  "desktop application", "the application on my screen",
                  "focus the window", "screenshot my screen", "my screen",
                  "press enter", "key combination"),
        tools=("computer_control", "pc_control"),
        requires=("desktop_control",),
        needs=("agent_tars_runtime", "pyautogui"),
        priority=84,
        chains=("verification", "system_engineering", "files"),
        metadata={"engine": ("Agent TARS v0.3.0 for whole tasks; pc_control "
                            "(pywinauto then PyAutoGUI) for single actions"),
                  "rule": "state one intended outcome per call and let Agent TARS plan "
                          "the steps; for one precise action use pc_control and report "
                          "the controller it names"},
    ),
    Skill(
        key="visual_control",
        name="Visual Control",
        purpose=("Act on what is visible on screen through UI-TARS visual grounding "
                 "on the Gemini model — for targets that cannot be addressed by "
                 "name or selector."),
        use_when=("the target is identified by sight rather than by name",
                  "the user is pointing at something or names an unlabelled icon"),
        triggers=("pointing at", "point at", "what i am pointing at",
                  "the icon on my desktop", "that icon", "click that icon",
                  "the button i am pointing at", "find the download button on the screen",
                  "on the screen", "visually", "by sight", "the unlabelled",
                  "the thing on my desktop", "this button here"),
        tools=("visual_control",),
        requires=("uitars_visual",),
        needs=("pyautogui",),
        priority=80,
        chains=("desktop_control", "browser_automation", "verification"),
        metadata={"engine": "UI-TARS action format on the Gemini vision model "
                            "(no separate UI-TARS credential)",
                  "rule": "only for genuinely visual targets; name the element and "
                          "report the position that was grounded"},
    ),
    Skill(
        key="browser_automation",
        name="Browser Automation",
        purpose=("Complete website workflows through Agent TARS — navigating, forms, "
                 "buttons on a page, logins, downloads and reading what a site shows."),
        use_when=("the user wants something achieved on a website",
                  "a request names a site and an action, e.g. 'open Gmail and find my latest mail'"),
        triggers=("fill the form", "fill out", "fill this form", "website form",
                  "form on this page", "open chrome", "open the website", "open firefox",
                  "go to", "navigate to", "click the button on", "on this website",
                  "on the webpage", "log in", "login", "download from", "upload to",
                  "submit this form", "search this site", "open gmail", "whats app",
                  "webpage", "in the browser", "the website",
                  "new tab", "switch tab", "close the tab", "fill the field",
                  "read the page", "what does this page say"),
        tools=("browser_control", "browser_action"),
        requires=("web_task",),
        needs=("agent_tars_runtime", "internet"),
        priority=82,
        chains=("verification", "desktop_control", "conversation"),
        metadata={"engine": ("Agent TARS v0.3.0 for whole web workflows; "
                            "browser_action (Playwright) for one page element at a time"),
                  "rule": "one controller owns the browser at a time: the router holds "
                          "the browser lock, picks Playwright when the page can "
                          "describe the element and Agent TARS for multi-step work"},
    ),
    Skill(
        key="internet_research",
        name="Internet Research",
        purpose=("Gather information from real online platforms through Agent Reach and "
                 "return sourced findings for Gemini to analyse."),
        use_when=("the user asks to research something across platforms or sources",
                  "a request names GitHub, YouTube, Reddit, Twitter/X, RSS or a page to read"),
        triggers=("research", "github", "youtube", "reddit", "twitter", "hacker news",
                  "v2ex", "rss", "find out online", "what people say", "search the web for",
                  "look up this repo", "podcast", "forums", "see comments", "reviews online"),
        tools=("web_research",),
        requires=("webview",),
        needs=("agent_reach_environment", "internet"),
        priority=79,
        chains=("research", "verification", "browser_automation", "desktop_control"),
        metadata={"engine": "Agent Reach (agent_reach, own Python environment)",
                  "rule": "retrieve only; Agent Reach never posts, comments or controls "
                          "anything, and every finding names its channel and backend"},
    ),
    Skill(
        key="vision",
        name="Vision",
        purpose="Understand images, screenshots, the webcam and anything visually shown.",
        use_when=("the user shows or asks about something visible",
                  "a request refers to 'this', 'the left one', 'the bigger one'"),
        triggers=("look at", "look here", "see this", "what do you see", "screenshot",
                  "screen", "this image", "this picture", "this photo", "read this",
                  "what is on my screen", "holding", "gesture", "faces",
                  "who is there", "object", "objects", "camera", "webcam", "video"),
        tools=("screen_process", "vision_look", "detect_objects", "recognize_faces",
               "what_am_i_holding", "detect_gesture", "close_camera"),
        requires=("camera_access",),
        priority=78,
        chains=("comparison", "research", "files"),
        metadata={"rule": "one vision capture per request; never claim to see what was not processed"},
    ),
    Skill(
        key="comparison",
        name="Comparison",
        purpose="Compare two or more things the user is looking at or has provided.",
        use_when=("the user asks which of several options is better and why",),
        triggers=("which one", "which is better", "better one", "compare these",
                  "compare them", "these two", "the two", "cheaper", "worth it",
                  "should i buy", "which should i choose", "difference between"),
        tools=("web_search", "screen_process", "file_processor"),
        priority=76,
        chains=("research", "vision", "conversation"),
        metadata={"rule": "explain the reasoning; state plainly what could not be determined"},
    ),
    Skill(
        key="memory",
        name="Memory",
        purpose="Store and recall durable facts, preferences and decisions.",
        use_when=("the user teaches a preference or asks what is remembered",
                  "a past decision or preference matters for the current request"),
        triggers=("remember", "memorize", "note that", "keep in mind", "forget",
                  "what do you remember", "do you remember", "recall", "i prefer",
                  "from now on", "always", "never do"),
        tools=("recall_memory", "brain_remember", "brain_forget", "brain_summary",
               "save_memory"),
        priority=72,
        chains=("conversation",),
        metadata={"rule": "never expose ids, tables or embeddings; never invent a memory"},
    ),
    Skill(
        key="files",
        name="File Management",
        purpose="Find, read, create, move, convert and organise files and folders.",
        use_when=("the request names a file, folder, path or document",
                  "output must be saved somewhere"),
        triggers=("file", "files", "folder", "directory", "path", "rename",
                  "copy", "move", "delete", "organize", "organise", "list my",
                  "open the file", "save it", "convert", "compress", "zip",
                  "pdf", "docx", "spreadsheet", "csv", "download", "upload"),
        tools=("file_controller", "file_processor"),
        requires=("file_control",),
        priority=65,
        chains=("documents", "verification"),
        metadata={"rule": "never overwrite or delete important files without an explicit instruction"},
    ),
    Skill(
        key="documents",
        name="Documents",
        purpose="Understand and work with documents, spreadsheets and study material.",
        use_when=("the user provides or points at a document, worksheet or assignment",
                  "the task is homework, revision or report writing"),
        triggers=("homework", "assignment", "worksheet", "study", "explain this",
                  "summarize this", "summarise this", "find the answer", "solve",
                  "essay", "report", "notes", "lecture", "exam", "questions",
                  "math", "equation"),
        tools=("file_processor", "file_controller", "web_search"),
        requires=("file_control",),
        priority=62,
        chains=("research", "teaching", "files"),
        metadata={"rule": "answer from the actual material; never invent its content"},
    ),
    Skill(
        key="teaching",
        name="Teaching",
        purpose="Explain, guide and help the user learn at the right depth.",
        use_when=("the user asks how something works, or wants to understand rather than just do",),
        triggers=("explain", "how does", "how do", "why does", "teach me",
                  "walk me through", "what is", "help me understand", "simpler",
                  "in plain english", "beginner"),
        tools=("web_search", "file_processor", "recall_memory"),
        priority=45,
        chains=("research", "conversation"),
        metadata={"rule": "patient and clear; adjust depth to the user's level"},
    ),
    Skill(
        key="automation",
        name="Automation",
        purpose="Run repeated work on a schedule or in the background.",
        use_when=("the user wants something to happen later, repeatedly, or without being watched",),
        triggers=("remind", "reminder", "in ten minutes", "tomorrow", "schedule",
                  "every day", "every hour", "repeat", "recurring", "monitor",
                  "watch for", "alert me", "notify me", "keep an eye"),
        tools=("reminder", "manage_monitor"),
        priority=64,
        chains=("monitoring", "planning"),
        metadata={"rule": "state the exact time/condition that was actually scheduled"},
    ),
    Skill(
        key="monitoring",
        name="System Monitoring",
        purpose=("Watch a value or event and report meaningful changes only; "
                 "diagnose lag and performance bottlenecks when the machine "
                 "feels slow."),
        use_when=("the user asks for ongoing status, or a task is long-running",
                  "the user says the computer is slow, lagging or stuttering",),
        triggers=("monitor", "status of", "is it done", "still downloading",
                  "progress", "how is it going", "check on", "alerts", "temperatures",
                  "slow", "lagging", "lag", "stuttering", "performance",
                  "performance scan", "bottleneck", "why is my computer slow",
                  "why is my pc slow", "what is slowing", "fix the lag",
                  "computer is lagging", "running slow"),
        tools=("system_status", "manage_monitor", "performance_scan"),
        priority=42,
        chains=("automation", "system_engineering", "diagnostics"),
        metadata={"rule": ("be useful, never noisy; for lag, report the measured "
                           "bottleneck and suggest — never kill processes or "
                           "change settings unasked")},
    ),
    Skill(
        key="voice",
        name="Voice",
        purpose="Speak and listen naturally through the live audio session.",
        use_when=("any reply is delivered to the user", "the user speaks a command"),
        triggers=("voice", "speak", "say", "louder", "quieter", "slower", "faster",
                  "mute", "unmute", "stop talking", "be quiet", "wake", "jarvis"),
        tools=(),
        requires=("mic_access",),
        priority=40,
        chains=("conversation",),
        metadata={"rule": "short replies stay short; speaking state must match real playback"},
    ),
    Skill(
        key="verification",
        name="Verification",
        purpose="Check that an action actually produced the claimed result.",
        use_when=("an action changed something outside the conversation",
                  "before reporting success for an important task"),
        triggers=("did it work", "is it done", "check if", "verify", "confirm that",
                  "make sure", "double check", "confirm it"),
        tools=("file_controller", "system_status", "recall_memory"),
        priority=58,
        chains=("files",),
        metadata={"rule": "evidence before claims — no fabricated success"},
    ),
    Skill(
        key="conversation",
        name="Conversation",
        purpose="Understand intent, hold context and reply in the JARVIS voice.",
        use_when=("the request is social, ambiguous or conversational",
                  "context or a follow-up reference must be resolved"),
        triggers=("hello", "hi ", "thanks", "thank you", "how are you", "good morning",
                  "good night", "what time", "who are you", "your name",
                  "do you think", "what about", "how about"),
        tools=(),
        priority=30,
        chains=("memory", "humor"),
        metadata={"rule": "ask one concise question only when information is genuinely missing"},
    ),
    Skill(
        key="humor",
        name="Humour",
        purpose="Subtle, intelligent, original wit when the moment suits it.",
        use_when=("the user is relaxed or joking and nothing is broken or urgent",),
        triggers=("joke", "funny", "make me laugh", "cheer me up", "roast me",
                  "say something funny"),
        tools=(),
        priority=25,
        chains=("conversation",),
        metadata={"rule": "original lines only; never copy film dialogue; off when the user is frustrated"},
    ),
    Skill(
        key="mcp",
        name="MCP Servers",
        purpose="Use tools from connected MCP servers, and report or control them.",
        use_when=("the user asks what MCP servers or tools are available",
                  "the user wants to connect, test, enable or disable an MCP server"),
        triggers=("mcp", "mcp server", "mcp servers", "connector", "connectors",
                  "composio", "github mcp", "connect a server", "available tools",
                  "what tools", "server status", "disconnect a server"),
        tools=("mcp",),
        requires=("mcp_servers",),
        priority=70,
        chains=("engineering", "monitoring"),
        metadata={"rule": "report the real connection state; never claim a server is "
                          "connected when it is not"},
    ),
)


# ── lookups ───────────────────────────────────────────────────────────────────

_BY_KEY: dict[str, Skill] = {s.key: s for s in SKILLS}
_WORD_RE: dict[str, re.Pattern] = {}


def get_skill(key: str) -> Skill | None:
    """Skill by key (or by tool name), case-insensitive."""
    k = str(key or "").strip().lower()
    if not k:
        return None
    if k in _BY_KEY:
        return _BY_KEY[k]
    for s in SKILLS:
        if k == s.name.lower() or k in {t.lower() for t in s.tools}:
            return s
    return None


def _pattern(phrase: str) -> re.Pattern:
    """Word-boundary matcher for a trigger phrase, cached."""
    pat = _WORD_RE.get(phrase)
    if pat is None:
        pat = re.compile(r"(?<!\w)" + re.escape(phrase.strip()) + r"(?!\w)")
        _WORD_RE[phrase] = pat
    return pat


def _feature_enabled(features: Mapping[str, object], key: str) -> bool:
    """A feature gate is satisfied when it is missing (default on) or truthy."""
    if not features:
        return True
    if key not in features:
        return True
    return bool(features.get(key))


# ── runtime dependency truth ─────────────────────────────────────────────────
# A skill is only as good as the packages behind it. Settings flags say what
# the user ALLOWED; this says what is actually INSTALLED. Probed with
# find_spec only — importing the real module would cost the whole point.

_TOOL_DEPS: dict[str, str] = {
    "screen_process": "mss",
    "system_status": "psutil",
    # Local controllers: one package each, so a missing engine is visible in the
    # skill report instead of failing at the moment of use.
    "pc_control": "pyautogui",
    "browser_action": "playwright",
}

_deps_cache: tuple[float, tuple[str, ...]] | None = None


def missing_deps() -> tuple[str, ...]:
    """Distinct Python packages a declared skill needs but cannot import.

    Cached for five minutes; any probe failure degrades to "nothing missing"
    because a broken lookup must never disable a skill on paper.
    """
    global _deps_cache
    import time as _time
    now = _time.time()
    if _deps_cache and (now - _deps_cache[0]) < 300:
        return _deps_cache[1]
    import importlib.util
    missing: list[str] = []
    for pkg in sorted(set(_TOOL_DEPS.values())):
        try:
            if importlib.util.find_spec(pkg) is None and pkg not in missing:
                missing.append(pkg)
        except Exception:
            continue
    _deps_cache = (now, tuple(missing))
    return _deps_cache[1]


def enabled_keys(features: Mapping[str, object] | None = None) -> list[str]:
    """Keys of every skill available with the current configuration."""
    feats = features or {}
    out: list[str] = []
    for s in SKILLS:
        if all(_feature_enabled(feats, req) for req in s.requires):
            out.append(s.key)
    return out


def disabled_keys(features: Mapping[str, object] | None = None) -> list[str]:
    feats = features or {}
    return [s.key for s in SKILLS
            if not all(_feature_enabled(feats, req) for req in s.requires)]


# ── routing ───────────────────────────────────────────────────────────────────

def route(text: str, features: Mapping[str, object] | None = None,
          limit: int = 4, include_baseline: bool = True) -> list[Skill]:
    """Deterministically pick the skills a request needs.

    Returns the best-matching enabled skills, strongest first. ``conversation``
    (and ``memory`` when the request sounds personal) is always appended while
    ``include_baseline`` is True, so the caller never ends up with an empty plan.
    """
    low = str(text or "").lower()
    if not low.strip():
        return [s for s in (_BY_KEY.get("conversation"),) if s]

    hits: list[tuple[float, Skill]] = []
    for s in SKILLS:
        if not all(_feature_enabled(features or {}, req) for req in s.requires):
            continue
        score = 0.0
        for phrase in s.triggers:
            if phrase and _pattern(phrase).search(low):
                # Longer phrases are far more specific than single words.
                score += 1.0 + min(2.0, len(phrase) / 12.0)
        if score <= 0.0:
            continue
        hits.append((score + s.priority / 100.0, s))

    hits.sort(key=lambda pair: pair[0], reverse=True)
    picked = [s for _score, s in hits[:max(1, int(limit))]]

    if include_baseline:
        for key in ("conversation", "memory"):
            s = _BY_KEY.get(key)
            if s is None or s in picked:
                continue
            if key == "memory" and not re.search(r"\b(prefer|remember|always|never)\b", low):
                continue
            if all(_feature_enabled(features or {}, req) for req in s.requires):
                picked.append(s)
    return picked


def chains_for(keys: Iterable[str], features: Mapping[str, object] | None = None,
               limit: int = 6) -> list[str]:
    """Skill keys commonly chained onto the given skills, without duplicates."""
    out: list[str] = []
    for key in keys:
        s = get_skill(key)
        if s is None:
            continue
        for nxt in s.chains:
            if nxt in out:
                continue
            n = _BY_KEY.get(nxt)
            if n is None:
                continue
            if all(_feature_enabled(features or {}, req) for req in n.requires):
                out.append(nxt)
            if len(out) >= limit:
                return out
    return out


# ── prompt text ───────────────────────────────────────────────────────────────

def prompt_block(features: Mapping[str, object] | None = None,
                 verbose: bool = False) -> str:
    """The skill map injected into the live system instruction.

    Deliberately compact: this text rides along on every session connect, and
    the user's first priority is speed, so the default form is a grouped map
    (~250 tokens) instead of one paragraph per skill. ``verbose=True`` gives the
    full per-skill detail for diagnostics and the ``skill_query`` tool.
    """
    feats = features or {}
    available = [s for s in SKILLS
                 if all(_feature_enabled(feats, req) for req in s.requires)]
    if not available:
        return ""

    if verbose:
        lines = ["[SKILL MAP]"]
        for s in sorted(available, key=lambda x: (-x.priority, x.name)):
            tools = ", ".join(s.tools) if s.tools else "voice/chat only"
            lines.append(f"- {s.name}: {s.purpose} Tools: {tools}.")
        return "\n".join(lines + _gating_lines(feats))

    groups: dict[str, list[Skill]] = {
        "Core": [], "Research": [], "Vision": [], "Work": [], "Meta": [],
    }
    _group_of = {
        "engineering": "Core", "debugging": "Core", "ui_engineering": "Core",
        "system_engineering": "Core", "planning": "Core", "task_memory": "Core",
        "research": "Research",
        "vision": "Vision", "comparison": "Vision",
        "files": "Work", "documents": "Work",
        "automation": "Work", "monitoring": "Work", "verification": "Work",
        "memory": "Meta", "voice": "Meta", "conversation": "Meta",
        "humor": "Meta", "teaching": "Meta",
    }
    for s in available:
        groups.setdefault(_group_of.get(s.key, "Meta"), []).append(s)

    def _brief(s: Skill) -> str:
        tools = ", ".join(s.tools[:2])
        hint = s.use_when[0] if s.use_when else s.purpose
        return f"{s.name} ({hint}; {tools})" if tools else f"{s.name} ({hint})"

    lines = [
        "[SKILL MAP]",
        "Your own capabilities. Pick them yourself — the user never names a "
        "skill — and chain them freely to finish one request.",
    ]
    for label in ("Core", "Research", "Vision", "Work", "Meta"):
        items = groups.get(label) or []
        if not items:
            continue
        items.sort(key=lambda x: -x.priority)
        lines.append(f"{label}: " + "; ".join(_brief(s) for s in items) + ".")
    lines.extend(_gating_lines(feats))
    lines.append("For a job needing 3+ dependent actions, open a plan with "
                 "task_plan, work it step by step, and finish it only after "
                 "verifying the result.")
    return "\n".join(lines)


def _gating_lines(features: Mapping[str, object]) -> list[str]:
    """One line naming skills the user has switched off, so JARVIS says so."""
    missing = [s.name for s in SKILLS
               if not all(_feature_enabled(features, req) for req in s.requires)]
    lines = []
    if missing:
        lines.append("Disabled in settings right now: " + ", ".join(missing)
                     + ". Say so plainly instead of pretending you can.")
    try:
        deps = missing_deps()
    except Exception:
        deps = ()
    if deps:
        lines.append("Missing Python packages right now: " + ", ".join(deps)
                     + ". The tools behind them cannot run — do not claim they "
                     "work; clear the blocker with diagnostics mode='install' "
                     "when the task needs them.")
    return lines


# ── question answering / diagnostics ──────────────────────────────────────────

def describe(query: str = "", features: Mapping[str, object] | None = None,
             list_all: bool = False) -> str:
    """Human-readable answer about capabilities (backs the skill_query tool)."""
    feats = features or {}
    try:
        if list_all or not str(query or "").strip():
            lines = ["I can work across these areas:"]
            for s in sorted(SKILLS, key=lambda x: (-x.priority, x.name)):
                if not all(_feature_enabled(feats, req) for req in s.requires):
                    continue
                lines.append(f"- {s.name} — {s.purpose}")
            blocked = [s.name for s in SKILLS
                       if not all(_feature_enabled(feats, req) for req in s.requires)]
            if blocked:
                lines.append("Unavailable while disabled in settings: " + ", ".join(blocked) + ".")
            return "\n".join(lines)

        picked = route(query, features=feats, limit=4)
        if not picked:
            return ("No specific skill matches that directly — I can still handle "
                    "it as a normal conversation, or search the web if you want "
                    "current information.")
        out = ["For that I would use:"]
        for s in picked:
            tools = ", ".join(s.tools) if s.tools else "voice and reasoning only"
            out.append(f"- {s.name}: {s.purpose}")
            if s.use_when:
                out.append(f"    when: {s.use_when[0]}")
            out.append(f"    tools: {tools}")
            if s.needs:
                out.append(f"    needs: {', '.join(s.needs)}")
        chained = chains_for([s.key for s in picked], features=feats)
        if chained:
            names = [get_skill(k).name for k in chained if get_skill(k)]
            out.append("Often chained with: " + ", ".join(names) + ".")
        return "\n".join(out)
    except Exception as exc:  # never raise into a tool call
        return f"Skill lookup failed: {exc}"


def report(features: Mapping[str, object] | None = None) -> str:
    """Full registry dump for diagnostics."""
    feats = features or {}
    lines = [f"Skill registry: {len(SKILLS)} skills defined."]
    for s in SKILLS:
        ok = all(_feature_enabled(feats, req) for req in s.requires)
        state = "enabled" if ok else "disabled"
        lines.append(
            f"[{state}] {s.name} ({s.key}) priority={s.priority}\n"
            f"    purpose: {s.purpose}\n"
            f"    tools:   {', '.join(s.tools) or '-'}\n"
            f"    needs:   {', '.join(s.requires + s.needs) or '-'}\n"
            f"    chains:  {', '.join(s.chains) or '-'}"
        )
    return "\n".join(lines)


def handle_query(args: Mapping[str, object] | None = None) -> str:
    """Tool entry point for skill_query (config-aware, never raises)."""
    a = dict(args or {})
    query = str(a.get("query") or a.get("topic") or "").strip()
    list_all = bool(a.get("list_all"))
    try:
        feats = _live_features()
    except Exception:
        feats = {}
    return describe(query, features=feats, list_all=list_all)


def _live_features() -> dict:
    """Read the feature flags from config/api_keys.json (cached by config.py)."""
    try:
        import json
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "config" / "api_keys.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        feats = data.get("features", {})
        return feats if isinstance(feats, dict) else {}
    except Exception:
        return {}
