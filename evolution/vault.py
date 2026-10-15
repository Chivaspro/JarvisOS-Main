"""The vault: where a verified skill becomes an ordinary Jarvis plugin.

A forged tool is not a second-class citizen. It is written as
``plugins/<name>/plugin.py`` with the same ``PLUGIN`` dict and the same
``run(parameters, player=None, session_memory=None)`` entry point a hand-written
plugin has, plus a ``plugin.json`` sidecar carrying the routing metadata the
Crucible collected (triggers, aliases, test cases, version, invocations).

Two guarantees this module exists to keep:

* **Persistence** — the skill lives in the project's ``plugins/`` folder, so it
  is still there after a restart, exactly like any other plugin.
* **Hot-loading** — the file is registered into the live
  ``app.plugin_loader.PluginRegistry`` the moment it is written, so the very
  next ``get_tool_declarations()`` offers it to the model and ``run()``
  dispatches it without restarting the application.

If registration fails for any reason the directory is removed again: a plugin
that cannot be loaded must not be left on disk pretending to exist.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from app import plugin_loader
from evolution import crucible

__all__ = [
    "install",
    "list_forged",
    "plugins_dir",
    "remove",
    "read_sidecar",
    "reverify",
    "sanitize_name",
    "set_active",
]

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DESCRIPTION_LIMIT = 640


def plugins_dir() -> Path:
    """The project's plugin directory (the only place forged skills live)."""
    return Path(__file__).resolve().parent.parent / "plugins"


def sanitize_name(raw: str) -> str:
    """A snake_case, contract-valid plugin name derived from *raw*."""
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw or "")).strip("_").lower()
    value = re.sub(r"_{2,}", "_", value)
    if value and value[0].isdigit():
        value = f"skill_{value}"
    return value[:64]


def _reserved_names() -> set[str]:
    reserved = set(crucible.PROTECTED_NAMES)
    registry = plugin_loader.get_runtime_registry()
    if registry is not None:
        reserved |= set(getattr(registry, "_core_tool_names", set()) or set())
    return reserved


def _header(name: str, description: str, goal: str, manifest: dict) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return (
        f'"""{description}\n\n'
        f"Forged by the J.A.R.V.I.S. self-evolution engine on {stamp}.\n"
        f"Goal: {goal or name}\n"
        f"Verified in the Skill Crucible before installation.\n"
        f'"""\n\n'
        f"from __future__ import annotations\n\n"
    )


def _merge_metadata(manifest: dict, *, name: str, goal: str,
                    triggers: Iterable[str], aliases: Iterable[str],
                    test_cases: list[dict], verification: dict | None = None) -> dict:
    """The sidecar the registry, the UI and the router all read."""
    meta = dict(manifest or {})
    clean_triggers = [str(t).strip() for t in list(triggers or []) if str(t).strip()]
    if goal and goal.strip() and goal.strip() not in clean_triggers:
        clean_triggers.append(goal.strip())
    name_phrase = name.replace("_", " ")
    if name_phrase not in clean_triggers:
        clean_triggers.append(name_phrase)

    clean_aliases = [str(a).strip() for a in list(aliases or []) if str(a).strip()]
    squeezed = name.replace("_", "")
    if squeezed and squeezed not in clean_aliases:
        clean_aliases.append(squeezed)

    meta.update({
        "name": name,
        "description": str(meta.get("description") or "")[:_DESCRIPTION_LIMIT],
        "triggers": list(dict.fromkeys(clean_triggers)),
        "aliases": list(dict.fromkeys(clean_aliases)),
        "parameters": meta.get("parameters") or {"type": "OBJECT", "properties": {}},
        "test_cases": list(test_cases or [{"input": {}}]),
        "goal": goal,
        "created_at": time.time(),
        "version": str(meta.get("version") or "1.0.0"),
        "author": "J.A.R.V.I.S. self-evolution engine",
        "active": True,
        "invocations": int(meta.get("invocations") or 0),
        "last_error": None,
        "origin": "forged",
    })
    if isinstance(verification, dict) and verification:
        # The Crucible verdict, stored verbatim except for the raw telemetry:
        # quality score, hazard notes and per-stage results. Kept in the sidecar
        # so the Settings UI can show it without re-running anything, and so it
        # survives a restart.
        meta["verification"] = {k: v for k, v in verification.items() if k != "telemetry"}
    return meta


def _py_literal(value: Any, indent: int = 0) -> str:
    """Render a JSON-shaped value as a *Python* literal.

    ``json.dumps`` is the wrong tool for the embedded metadata blocks: it writes
    ``true``/``false``/``null``, which are not Python. The sidecar stays JSON;
    the module source uses this.
    """
    pad = "    " * indent
    inner = "    " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{inner}{k!r}: {_py_literal(v, indent + 1)}" for k, v in value.items()]
        return "{\n" + ",\n".join(items) + f",\n{pad}}}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = [f"{inner}{_py_literal(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + f",\n{pad}]"
    return repr(value)


def _build_source(*, name: str, description: str, goal: str, code: str,
                  parameters: dict, metadata: dict) -> str:
    body = str(code or "").strip("\n")
    # The header already emits ``from __future__ import annotations``; a second
    # copy anywhere after it is a SyntaxError, so a leading one is dropped.
    body = re.sub(r"^from\s+__future__\s+import\s+annotations\s*\n+", "", body)
    plugin_block = (
        "PLUGIN = {\n"
        f"    \"name\": {name!r},\n"
        f"    \"description\": {description!r},\n"
        f"    \"parameters\": {_py_literal(parameters, 1)},\n"
        "}\n\n"
    )
    meta_block = "FEATURE_METADATA = " + _py_literal({k: v for k, v in metadata.items()
                                                      if k != "verification"}) + "\n\n"
    return _header(name, description, goal, metadata) + plugin_block + meta_block + body + "\n"


def install(*, name: str, description: str, code: str, goal: str = "",
            parameters: dict | None = None, triggers: Iterable[str] = (),
            aliases: Iterable[str] = (), test_cases: list[dict] | None = None,
            manifest: dict | None = None, overwrite: bool = True,
            directory: Path | None = None, hot_register: bool = True,
            verification: dict | None = None) -> dict:
    """Write a verified skill into ``plugins/`` and make it live.

    Returns ``{"ok": bool, "name": str, "path": str, "message": str, ...}``.
    Never raises: every failure path returns ``ok=False`` with a sentence the
    assistant can speak.
    """
    clean = sanitize_name(name)
    if not clean or not _NAME_RE.match(clean):
        return {"ok": False, "name": "", "path": "",
                "message": f"'{name}' is not a usable tool name."}
    if clean in _reserved_names():
        return {"ok": False, "name": clean, "path": "",
                "message": f"'{clean}' is reserved by Jarvis itself — pick another name."}

    summary = " ".join(str(description or "").split())[:_DESCRIPTION_LIMIT]
    if not summary:
        summary = f"Forged capability: {goal or clean}"

    meta = _merge_metadata(manifest or {}, name=clean, goal=goal,
                           triggers=triggers, aliases=aliases,
                           test_cases=list(test_cases or [{"input": {}}]),
                           verification=verification)
    meta["description"] = summary
    props = meta.get("parameters") or {"type": "OBJECT", "properties": {}}
    source = _build_source(name=clean, description=summary, goal=goal, code=code,
                           parameters=props, metadata=meta)

    ok, reason = crucible.validate_ast(source)
    if not ok:
        return {"ok": False, "name": clean, "path": "",
                "message": f"The assembled plugin was rejected: {reason}"}

    root = Path(directory) if directory else plugins_dir()
    target = root / clean
    plugin_file = target / "plugin.py"
    if target.exists() and not overwrite:
        return {"ok": False, "name": clean, "path": str(plugin_file),
                "message": f"A skill named '{clean}' already exists."}

    target.mkdir(parents=True, exist_ok=True)
    staging = plugin_file.with_suffix(".py.staging")
    try:
        staging.write_text(source, encoding="utf-8")
        os.replace(staging, plugin_file)
    except Exception as exc:
        try:
            staging.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "name": clean, "path": str(plugin_file),
                "message": f"Could not write the skill file: {exc}"}

    try:
        (target / "plugin.json").write_text(
            json.dumps(meta, indent=4, default=str), encoding="utf-8")
    except Exception:
        # The sidecar is metadata; its absence never blocks the tool itself.
        pass

    if not hot_register:
        return {"ok": True, "name": clean, "path": str(plugin_file),
                "registration": "deferred",
                "message": f"'{clean}' was written to plugins/{clean}/ and loads on next start."}

    registered, message = plugin_loader.register_runtime_plugin(plugin_file, root)
    if not registered:
        # No registry yet (text-only mode / early import) is not a failure of the
        # skill itself — anything else means the plugin is unusable, so it goes.
        registry_running = plugin_loader.get_runtime_registry() is not None
        if registry_running:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "name": clean, "path": str(plugin_file),
                    "message": f"The skill was refused by the plugin loader: {message}"}
        return {"ok": True, "name": clean, "path": str(plugin_file),
                "registration": "next_launch",
                "message": (f"'{clean}' was written and will be available on the next "
                            f"launch ({message}).")}

    return {"ok": True, "name": clean, "path": str(plugin_file),
            "registration": "hot", "metadata": meta,
            "message": f"'{clean}' is forged, verified and live."}


def read_sidecar(name: str, directory: Path | None = None) -> dict:
    """The ``plugin.json`` sidecar for a forged skill ({} when unreadable)."""
    root = Path(directory) if directory else plugins_dir()
    path = root / sanitize_name(name) / "plugin.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_sidecar(name: str, metadata: dict, directory: Path | None = None) -> bool:
    root = Path(directory) if directory else plugins_dir()
    path = root / sanitize_name(name) / "plugin.json"
    try:
        path.write_text(json.dumps(metadata, indent=4, default=str), encoding="utf-8")
        return True
    except Exception:
        return False


def list_forged(directory: Path | None = None) -> list[dict]:
    """Every forged skill on disk, newest first (invalid sidecars tolerated)."""
    root = Path(directory) if directory else plugins_dir()
    if not root.exists():
        return []
    found: list[dict] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if not (child / "plugin.py").exists():
            continue
        meta = read_sidecar(child.name, root)
        if str(meta.get("origin") or "") != "forged" and not meta:
            continue
        verification = meta.get("verification") if isinstance(meta.get("verification"), dict) else {}
        found.append({
            "name": str(meta.get("name") or child.name),
            "description": str(meta.get("description") or ""),
            "version": str(meta.get("version") or "1.0.0"),
            "created_at": float(meta.get("created_at") or 0.0),
            "invocations": int(meta.get("invocations") or 0),
            "last_error": meta.get("last_error"),
            "triggers": list(meta.get("triggers") or []),
            "aliases": list(meta.get("aliases") or []),
            "goal": str(meta.get("goal") or ""),
            "active": bool(meta.get("active", True)),
            "path": str(child),
            "origin": str(meta.get("origin") or "forged"),
            # The Crucible verdict (empty for a legacy sidecar written before it).
            "quality_score": int((verification.get("metrics") or {}).get("quality_score") or 0),
            "verified_at": float(verification.get("verified_at") or 0.0),
            "hazards": list(verification.get("hazards") or []),
        })
    found.sort(key=lambda item: item["created_at"], reverse=True)
    return found


def reverify(name: str, directory: Path | None = None, *,
             allow_install: bool = False) -> dict:
    """Re-run the Crucible on an installed forged skill and refresh its sidecar.

    Used by the registry's ``verify`` action and the Settings ▸ EVOLUTION button:
    it re-proves a skill that was installed earlier (after the interpreter or its
    dependencies changed) and writes the fresh verdict back. Returns
    ``{"ok": bool, "message": str, "report": dict}`` and never raises.
    """
    clean = sanitize_name(name)
    root = Path(directory) if directory else plugins_dir()
    folder = root / clean
    try:
        source = (folder / "plugin.py").read_text(encoding="utf-8")
    except Exception:
        return {"ok": False, "message": f"I have no installed skill named '{clean}'.",
                "report": {}}
    meta = read_sidecar(clean, root)
    cases = [c for c in (meta.get("test_cases") or []) if isinstance(c, dict)]
    cases = cases or [{"input": {}}]
    report = crucible.verify(source, cases, allow_install=bool(allow_install))
    if report.get("ok"):
        stages = report.get("stages") or {}
        meta["verification"] = {
            "ok": True,
            "attempts": 1,
            "hazards": list(report.get("hazards") or []),
            "metrics": report.get("metrics") or {},
            "contract": bool((stages.get("contract") or {}).get("ok", True)),
            "dependencies": list(report.get("dependencies") or []),
            "verified_at": report.get("verified_at"),
        }
        write_sidecar(clean, meta, root)
    message = str(report.get("message") or "Verification produced no verdict.")
    return {"ok": bool(report.get("ok")), "message": message, "report": report}


def set_active(name: str, enabled: bool, directory: Path | None = None) -> tuple[bool, str]:
    """Enable/disable a forged skill (persisted; takes effect immediately)."""
    clean = sanitize_name(name)
    meta = read_sidecar(clean, directory)
    if not meta:
        return False, f"No forged skill named '{clean}' was found."
    meta["active"] = bool(enabled)
    if not write_sidecar(clean, meta, directory):
        return False, f"Could not update '{clean}'."
    try:
        from memory.config_manager import save_plugin_enabled
        save_plugin_enabled(clean, bool(enabled))
    except Exception as exc:
        return False, f"The skill file was updated but the switch failed: {exc}"
    return True, f"'{clean}' is now {'enabled' if enabled else 'disabled'}."


def remove(name: str, directory: Path | None = None) -> tuple[bool, str]:
    """Delete a forged skill's directory (and its enable-state entry)."""
    clean = sanitize_name(name)
    root = Path(directory) if directory else plugins_dir()
    target = root / clean
    if not target.exists():
        return False, f"No skill named '{clean}' exists."
    try:
        shutil.rmtree(target)
    except Exception as exc:
        return False, f"Could not delete '{clean}': {exc}"
    try:
        from memory.config_manager import save_plugin_enabled
        save_plugin_enabled(clean, True)
    except Exception:
        pass
    registry = plugin_loader.get_runtime_registry()
    if registry is not None:
        registry._plugins.pop(clean, None)
        registry._all_records = [r for r in registry._all_records if r.name != clean]
    return True, f"'{clean}' was removed."
