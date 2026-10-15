"""The Evolution Manager — one router for every kind of self-improvement.

Two jobs, deliberately kept apart:

* **The capability does not exist** → the Brahma-style **Skill Forge**
  (``evolution.forge``): generate with a model, verify in the Crucible, repair
  while attempts remain, install as an ordinary plugin, hot-register it.
* **The capability exists but keeps failing** → the **DGM adapter**
  (``evolution.dgm``): propose a candidate improvement, test it against the
  incumbent, keep it only when it is genuinely better, and always keep the
  previous version recoverable.

The manager is the only place that decides which of the two runs, so J.A.R.V.I.S.
never has two self-improvement systems answering the same request.

It also remembers what it has already built. :func:`remember_skill` records the
name, goal, triggers and provider of every successful forge, and
:func:`find_skill` answers "have I already made this?" — so asking for the same
thing twice does not burn a second synthesis on it. The record is mirrored into
the project's own long-term memory, so the assistant can also *say* it remembers.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "ROUTES",
    "find_skill",
    "forget_skill",
    "handle",
    "note_invocation",
    "remember_skill",
    "route",
    "skill_memory",
    "status",
]

#: The two self-improvement routes, plus "none".
ROUTES = ("forge", "improve", "none")

_lock = threading.RLock()

#: Words that carry no meaning when comparing one goal with another.
_STOPWORDS = frozenset({
    "a", "an", "the", "to", "for", "of", "and", "or", "in", "on", "at", "by",
    "with", "from", "is", "are", "be", "that", "this", "it", "which", "where",
    "when", "how", "my", "me", "you", "your", "jarvis", "please", "can", "could",
    "would", "skill", "tool", "new", "create", "make", "build", "add", "learn",
    "capability", "function", "feature", "plugin",
})


def _data_dir() -> Path:
    """The project's data directory (its own helper, with a safe fallback)."""
    try:
        from config.settings import data_dir
        return Path(data_dir())
    except Exception:
        return Path(__file__).resolve().parent.parent / "data"


def _record_path() -> Path:
    return _data_dir() / "evolution" / "forged_skills.json"


def _tokens(text: object) -> set[str]:
    """The meaningful lowercase words in *text*."""
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if w not in _STOPWORDS and len(w) > 2}


def _load_records() -> list[dict]:
    try:
        raw = json.loads(_record_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("skills")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _save_records(rows: list[dict]) -> bool:
    """One writer, always verified — a failed write leaves the old file intact."""
    path = _record_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"skills": rows}, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


# ── skill memory ──────────────────────────────────────────────────────────────

def remember_skill(name: str, goal: str = "", description: str = "",
                   triggers: Any = None, provider: str = "",
                   path: str = "", test_cases: Any = None) -> dict:
    """Record a successfully forged skill so it is never forged twice.

    The record is the manager's own JSON (it must survive a registry that failed
    to load) and is mirrored, best-effort, into the project's long-term memory so
    the assistant can also talk about what it built.
    """
    clean = str(name or "").strip()
    if not clean:
        return {}
    entry = {
        "name": clean,
        "goal": str(goal or "").strip(),
        "description": str(description or "").strip()[:640],
        "triggers": [str(t).strip() for t in list(triggers or []) if str(t).strip()],
        "provider": str(provider or "").strip(),
        "path": str(path or "").strip(),
        "test_cases": list(test_cases or []),
        "forged_at": time.time(),
        "invocations": 0,
    }
    with _lock:
        rows = [r for r in _load_records() if r.get("name") != clean]
        rows.append(entry)
        _save_records(rows)
    # Mirror into the existing long-term store (never fatal).
    try:
        from memory.memory_manager import remember
        remember(f"forged skill {clean}",
                 (entry["description"] or entry["goal"]
                  or f"A skill I built for: {clean}").strip(),
                 "projects")
    except Exception:
        pass
    return entry


def note_invocation(name: str) -> None:
    """Count one real call of a forged skill (feeds the "already built" answer)."""
    clean = str(name or "").strip()
    if not clean:
        return
    with _lock:
        rows = _load_records()
        for row in rows:
            if row.get("name") == clean:
                row["invocations"] = int(row.get("invocations") or 0) + 1
                row["last_used"] = time.time()
                _save_records(rows)
                return


def forget_skill(name: str) -> bool:
    """Drop one record (used when a skill is deleted from the vault)."""
    clean = str(name or "").strip()
    with _lock:
        rows = _load_records()
        kept = [r for r in rows if r.get("name") != clean]
        if len(kept) == len(rows):
            return False
        return _save_records(kept)


def find_skill(goal: str, threshold: float = 0.6) -> dict | None:
    """The skill already built for *goal*, or None.

    Matching is token overlap against the recorded goal, description, triggers and
    name — deliberately generous, because the cost of a false positive (the
    assistant says "I already have that" and runs it) is far lower than a false
    negative: a second synthesis call, and two skills doing one job.
    """
    wanted = _tokens(goal)
    if not wanted:
        return None
    best: tuple[float, dict] | None = None
    for row in _load_records():
        candidate = _tokens(" ".join([
            str(row.get("goal") or ""), str(row.get("description") or ""),
            " ".join(str(t) for t in (row.get("triggers") or [])),
            str(row.get("name") or "").replace("_", " "),
        ]))
        if not candidate:
            continue
        overlap = len(wanted & candidate) / float(len(wanted))
        if overlap >= threshold and (best is None or overlap > best[0]):
            best = (overlap, row)
    return dict(best[1], match=round(best[0], 2)) if best else None


def skill_memory() -> list[dict]:
    """Every remembered forge, newest first (status output and the UI)."""
    rows = _load_records()
    return sorted(rows, key=lambda r: float(r.get("forged_at") or 0), reverse=True)


# ── the route ─────────────────────────────────────────────────────────────────

#: How many failures of one capability make it a DGM candidate.
FAILURE_THRESHOLD = 3


def route(request: str = "", *, target: str = "", failures: int = 0,
          missing: bool = False) -> str:
    """Which route serves this request: ``forge``, ``improve`` or ``none``.

    Both pieces of evidence are passed in rather than guessed here, so the
    decision is testable without a model, a UI or a failing tool:

    * ``missing`` — no tool exists for the request (the same signal
      ``evolution.discovery.is_capability_gap`` produces);
    * ``failures`` — how many times ``target`` has failed, i.e. something exists
      but does not work well enough.

    Order matters. An improvement outranks a forge: if the thing exists and keeps
    failing, rebuilding it from scratch would throw away the working parts. A
    remembered skill outranks a forge too — the answer there is to *run* it, not
    to build it a second time.
    """
    if target and int(failures or 0) >= FAILURE_THRESHOLD:
        return "improve"
    if request and find_skill(request):
        return "none"
    try:
        from evolution import discovery
        explicit = bool(discovery.analyze_command(request))
    except Exception:
        explicit = False
    if missing or explicit:
        return "forge"
    return "none"


def forge_now(goal: str, *, name: str | None = None, player=None,
              force: bool = False, progress=None) -> str:
    """Forge through the manager: consult memory first, remember the result."""
    goal = str(goal or "").strip()
    if not goal:
        return ("Tell me what the new skill should do, sir — for example "
                "“learn how to track the price of a stock and draw a chart”.")
    if not force:
        existing = find_skill(goal)
        if existing:
            return (f"I already built '{existing['name']}' for that, sir — "
                    f"{(existing.get('description') or existing.get('goal') or '').strip()}. "
                    f"Say “run {existing['name']}” to use it, or ask again with "
                    f"force to rebuild it.")
    from evolution import discovery, forge
    parsed = discovery.analyze_command(goal)
    if parsed:
        goal = parsed["goal"]
        name = name or parsed.get("suggested_name")
    result = forge.forge(goal, name=name, progress=progress)
    if result.get("ok"):
        remember_skill(str(result.get("name") or name or ""), goal=goal,
                       description=str(result.get("description") or ""),
                       provider=str(result.get("provider") or ""),
                       path=str(result.get("path") or ""))
    return str(result.get("message") or "The forge finished without a verdict.")


def improve_now(skill: str, *, player=None, progress=None) -> str:
    """Improve an existing capability through the DGM adapter, never rebuilding it."""
    clean = str(skill or "").strip()
    if not clean:
        return "Which skill should I improve, sir?"
    try:
        from evolution.dgm import adapter
    except Exception as exc:
        return (f"The improvement engine is unavailable, sir: "
                f"{type(exc).__name__}: {exc}")
    return str(adapter.improve_skill(clean, player=player, progress=progress))


def status() -> str:
    """One paragraph: what has been forged, and which brain would forge next."""
    rows = skill_memory()
    if not rows:
        base = "I have not forged anything yet, sir."
    else:
        newest = rows[0]
        base = (f"I remember {len(rows)} forged skill(s), most recently "
                f"'{newest['name']}' for “{str(newest.get('goal') or '')[:60]}”.")
    try:
        from evolution import forge
        providers = [p for p, _cfg in forge.synthesis_providers()]
        base += (f" Synthesis brain: {', '.join(providers)}."
                 if providers else
                 " No AI provider key is configured, so I cannot forge yet.")
    except Exception:
        pass
    try:
        from evolution.dgm import adapter
        base += " " + str(adapter.status())
    except Exception:
        pass
    return base


def handle(args: dict | None = None, player=None) -> str:
    """The ``evolution`` tool entry point: status, route, forge or improve."""
    args = dict(args or {})
    action = str(args.get("action") or "status").strip().lower()
    request = str(args.get("goal") or args.get("request") or "").strip()
    name = str(args.get("name") or "").strip() or None

    if action in ("status", "list", "report"):
        return status()

    if action in ("route", "decide"):
        chosen = route(request, target=str(args.get("target") or ""),
                       failures=int(args.get("failures") or 0),
                       missing=bool(args.get("missing")))
        return f"Route for '{(request or 'this request')[:80]}': {chosen}."

    if action in ("forge", "create", "learn", "build"):
        return forge_now(request, name=name, player=player,
                         force=bool(args.get("force")))

    if action in ("improve", "evolve", "dgm"):
        return improve_now(str(args.get("skill") or name or ""), player=player)

    return (f"'{action}' is not an evolution action, sir. Use one of: status, "
            f"route, forge, improve.")

