"""Registry — everything the assistant and the Settings UI need to see the
forged skills: list them, run one, switch one off, delete one.

This is deliberately a thin facade. The authoritative record is
``plugins/<name>/plugin.json`` (written by ``evolution.vault``), the live
dispatch path is ``app.plugin_loader``, and the enable flag is the same
``plugins_enabled`` map every plugin uses — so a forged skill is managed by
exactly the same machinery as a hand-written one, including the existing
Plugin Manager overlay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app import plugin_loader
from evolution import discovery, forge, manager, vault

__all__ = [
    "delete_skill",
    "list_skills",
    "run_skill",
    "status_report",
    "toggle_skill",
    "verify_skill",
]


def verify_skill(name: str, directory: Path | None = None) -> str:
    """Re-run the Skill Crucible on one forged skill and refresh its record."""
    clean = vault.sanitize_name(name)
    if not clean:
        return "Which skill should I re-verify, sir?"
    result = vault.reverify(clean, directory)
    report = result.get("report") or {}
    message = str(result.get("message") or "")
    if report.get("ok"):
        score = int(((report.get("metrics") or {}).get("quality_score")) or 0)
        hazards = len(report.get("hazards") or [])
        note = f" {hazards} hazard note(s)." if hazards else " No hazard notes."
        return f"'{clean}' passed the Crucible again — quality {score}/100.{note}"
    return message or f"'{clean}' did not pass the Crucible this time, sir."


def list_skills(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every forged skill on disk, each with its live/enabled state."""
    registry = plugin_loader.get_runtime_registry()
    out: list[dict[str, Any]] = []
    for row in vault.list_forged(directory):
        name = row["name"]
        live = bool(registry is not None and registry.has(name))
        try:
            from memory.config_manager import get_plugin_enabled
            enabled = bool(get_plugin_enabled(name))
        except Exception:
            enabled = bool(row.get("active", True))
        row = dict(row)
        row["live"] = live
        row["enabled"] = enabled
        out.append(row)
    return out


def status_report(directory: Path | None = None) -> str:
    """A short spoken summary of the self-evolution subsystem."""
    rows = list_skills(directory)
    if not rows:
        base = "I have forged no new skills yet, sir."
    else:
        names = ", ".join(r["name"] for r in rows[:6])
        more = f" and {len(rows) - 6} more" if len(rows) > 6 else ""
        base = f"{len(rows)} forged skill{'s' if len(rows) != 1 else ''}: {names}{more}."
    providers = [p for p, _cfg in forge.synthesis_providers()]
    if providers:
        base += (" Synthesis brain: " + ", ".join(providers)
                 + " (the first working one writes the skill).")
    else:
        base += (" No AI provider key is configured, so I cannot forge new skills "
                 "until you add one in Settings ▸ API HUB.")
    # A stored value that is not a key is stated, not hidden: it is the reason a
    # forge attempt would otherwise fail on its first provider.
    skipped = forge.skipped_providers()
    if skipped:
        base += (" Not used: " + "; ".join(
            f"{row['provider']} — {row['detail']}" for row in skipped) + ".")
    return base


def toggle_skill(name: str, enabled: bool | None = None,
                 directory: Path | None = None) -> str:
    rows = {r["name"]: r for r in list_skills(directory)}
    row = rows.get(vault.sanitize_name(name))
    if row is None:
        return f"I have no forged skill named '{name}', sir."
    target = (not row["enabled"]) if enabled is None else bool(enabled)
    ok, message = vault.set_active(row["name"], target, directory)
    return message if ok else f"That failed, sir: {message}"


def delete_skill(name: str, directory: Path | None = None) -> str:
    ok, message = vault.remove(name, directory)
    if ok:
        # The vault owns the files; the manager owns the memory of them. Leaving
        # a record behind would make a deleted skill look "already built".
        try:
            manager.forget_skill(vault.sanitize_name(name))
        except Exception:
            pass
    return message if ok else f"That failed, sir: {message}"


def run_skill(name: str, parameters: dict | None = None, player=None,
              directory: Path | None = None) -> str:
    """Run a forged skill right now, even before the Live session reconnects."""
    clean = vault.sanitize_name(name)
    registry = plugin_loader.get_runtime_registry()
    if registry is None:
        return (f"The plugin registry is not running, sir, so '{clean}' cannot be "
                f"executed yet.")
    if not registry.has(clean):
        return f"I have no forged skill named '{clean}', sir."
    out = registry.run(clean, dict(parameters or {}), player=player,
                       session_memory=None)
    # Remember that it was actually used: the count is what lets the manager say
    # "I already built that, and I have run it N times" instead of re-forging.
    try:
        manager.note_invocation(clean)
    except Exception:
        pass
    return out


def handler(args: dict | None = None, player=None, progress=None) -> str:
    """The ``skill_forge`` tool entry point (see main.py's dispatcher).

    ``progress(phase, detail)`` is the HUD hook: every stage the work really
    reaches is reported as it happens, so the interface names the real stage
    instead of guessing at one.
    """
    args = dict(args or {})
    action = str(args.get("action") or "forge").strip().lower()
    goal = str(args.get("goal") or args.get("request") or "").strip()
    name = str(args.get("name") or "").strip() or None

    if action in ("status", "list", "report"):
        rows = list_skills()
        if not rows:
            return status_report()
        lines = [status_report(), ""]
        for row in rows:
            state = "live" if row["live"] else ("disabled" if not row["enabled"] else "on disk")
            goal_text = f" — goal: {row['goal']}" if row.get("goal") else ""
            score = int(row.get("quality_score") or 0)
            score_text = f" · quality {score}/100" if score else ""
            lines.append(f"- {row['name']} [{state}] v{row['version']} · "
                         f"{row['invocations']} call(s){score_text}{goal_text}")
        return "\n".join(lines)

    if action in ("verify", "recheck", "reverify"):
        target = str(args.get("skill") or name or goal or "").strip()
        if not target:
            target = next((r["name"] for r in list_skills()), "")
        if not target:
            return "There is no forged skill to re-verify yet, sir."
        return verify_skill(target)

    if action in ("forge", "create", "learn", "build"):
        # The Evolution Manager owns this decision: it checks what has already
        # been built before spending a synthesis call, and remembers the result
        # afterwards so the same skill is never forged twice.
        return manager.forge_now(goal, name=name, player=player,
                                 force=bool(args.get("force")), progress=progress)

    if action in ("improve", "evolve", "dgm"):
        # The other half of the manager: the capability exists but keeps failing,
        # so a candidate improvement is tested against it and kept only when it
        # measurably wins. Rollback uses the archive this leaves behind.
        target = str(args.get("skill") or name or goal or "").strip()
        if not target:
            return "Which forged skill should I improve, sir?"
        return manager.improve_now(target, player=player, progress=progress)

    if action in ("run", "execute", "test"):
        target = str(args.get("skill") or name or "").strip()
        if not target:
            target = next((r["name"] for r in list_skills()), "")
        if not target:
            return "There is no forged skill to run yet, sir."
        params = args.get("parameters")
        if not isinstance(params, dict):
            params = {k: v for k, v in args.items()
                      if k not in ("action", "skill", "name", "goal", "request")}
        return run_skill(target, params, player=player)

    if action in ("enable", "disable"):
        target = str(args.get("skill") or name or goal or "").strip()
        if not target:
            return "Which skill should I change, sir?"
        return toggle_skill(target, enabled=(action == "enable"))

    if action in ("delete", "remove", "forget"):
        target = str(args.get("skill") or name or goal or "").strip()
        if not target:
            return "Which skill should I remove, sir?"
        return delete_skill(target)

    return (f"'{action}' is not a skill-forge action, sir. Use one of: forge, "
            f"improve, status, verify, run, enable, disable, delete.")
