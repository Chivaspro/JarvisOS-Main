"""Identity & long-term profile — who J.A.R.V.I.S. is, and who he is speaking to.

One small JSON file (``memory/identity.json``, already gitignored by
``memory/*.json``) holding the facts the assistant should never have to ask
again: the owner's name, role, location, interests and about text, the
assistant's own name and title, the behaviour mode, whether proactivity is
wanted, whether this is a shared computer, and the standing **custom
instructions** the user typed once and expects to keep.

``prompt_block()`` renders the profile as a bounded block the system prompt
carries on every session, so identity survives restarts, model changes and
reconnects — the same guarantee Brahma's identity service gave, implemented on
Jarvis's existing config/memory rails. The two legacy keys the UI already reads
(``assistant_name`` / ``user_name``) are mirrored into ``api_keys.json`` so the
greeting, avatar and log prefix keep working unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = [
    "FIELDS",
    "get_field",
    "is_setup_complete",
    "load",
    "mark_setup_complete",
    "prompt_block",
    "save",
    "set_field",
    "summary",
]

_MAX_TEXT = 1200
_MAX_INTERESTS = 24

#: key -> (label, kind) for the onboarding form and the settings card.
FIELDS: dict[str, tuple[str, str]] = {
    "assistant_name": ("Assistant name", "text"),
    "assistant_title": ("Assistant title", "text"),
    "owner_name": ("Your name", "text"),
    "owner_role": ("Your role", "text"),
    "owner_location": ("Your location", "text"),
    "owner_interests": ("Interests", "list"),
    "owner_about": ("About you", "text"),
    "behavior_mode": ("Behaviour mode", "text"),
    "custom_instructions": ("Standing instructions", "text"),
    "proactive": ("Speaks up unprompted", "bool"),
    "shared_computer": ("Shared computer", "bool"),
}

#: The title is empty by default: the written system prompt already establishes
#: who J.A.R.V.I.S. is, so the profile block only ever carries *personalised*
#: facts, and a blank profile injects nothing at all.
_DEFAULTS: dict[str, Any] = {
    "assistant_name": "JARVIS",
    "assistant_title": "",
    "owner_name": "",
    "owner_role": "",
    "owner_location": "",
    "owner_interests": [],
    "owner_about": "",
    "behavior_mode": "professional",
    "custom_instructions": "",
    "proactive": True,
    "shared_computer": False,
    "setup_complete": False,
}


def _path() -> Path:
    return Path(__file__).resolve().parent / "identity.json"


def _legacy_names() -> tuple[str, str]:
    """``(assistant_name, user_name)`` from the legacy config, or ("", "")."""
    try:
        from memory.config_manager import get_assistant_name, get_user_name
        return (str(get_assistant_name() or ""), str(get_user_name() or ""))
    except Exception:
        return ("", "")


def load() -> dict[str, Any]:
    """The whole profile, defaults filled in. Never raises."""
    data = dict(_DEFAULTS)
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in _DEFAULTS:
                if key in raw:
                    data[key] = raw[key]
            data["updated_at"] = raw.get("updated_at")
    except Exception:
        pass
    # The live config wins for the two legacy keys when identity.json has never
    # been written, so an existing install keeps the name it already shows.
    if not _path().exists():
        assistant, owner = _legacy_names()
        data["assistant_name"] = assistant or data["assistant_name"]
        data["owner_name"] = owner or data["owner_name"]
    return data


def save(profile: dict[str, Any]) -> bool:
    """Persist a full profile (unknown keys are dropped)."""
    data = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in (profile or {}):
            data[key] = profile[key]
    data["owner_interests"] = [str(i).strip() for i in
                               (data.get("owner_interests") or [])
                               if str(i).strip()][:_MAX_INTERESTS]
    for key in ("owner_about", "custom_instructions", "behavior_mode",
                "assistant_title", "owner_role", "owner_location",
                "assistant_name", "owner_name"):
        value = str(data.get(key) or "").strip()
        data[key] = value[:_MAX_TEXT] if key in ("owner_about", "custom_instructions") else value[:200]
    data["proactive"] = bool(data.get("proactive", True))
    data["shared_computer"] = bool(data.get("shared_computer", False))
    data["setup_complete"] = bool(data.get("setup_complete", False))
    data["updated_at"] = time.time()
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        return False
    try:
        from memory.config_manager import save_assistant_config
        save_assistant_config(str(data.get("assistant_name") or "JARVIS"),
                              str(data.get("owner_name") or ""))
    except Exception:
        pass
    return True


def get_field(key: str, default: Any = None) -> Any:
    return load().get(key, _DEFAULTS.get(key, default))


def set_field(key: str, value: Any) -> bool:
    """Update one field ("owner_interests" accepts a list or a comma string)."""
    profile = load()
    if key == "owner_interests" and isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    profile[key] = value
    return save(profile)


def is_setup_complete() -> bool:
    profile = load()
    return bool(profile.get("setup_complete")) and bool(
        str(profile.get("owner_name") or "").strip()
        or str(profile.get("custom_instructions") or "").strip())


def mark_setup_complete(complete: bool = True) -> bool:
    profile = load()
    profile["setup_complete"] = bool(complete)
    return save(profile)


def summary() -> str:
    """A short spoken/UI summary of the profile."""
    profile = load()
    bits = [f"Assistant: {profile.get('assistant_name')}"]
    if profile.get("owner_name"):
        bits.append(f"Owner: {profile['owner_name']}")
    if profile.get("owner_role"):
        bits.append(f"Role: {profile['owner_role']}")
    if profile.get("owner_location"):
        bits.append(f"Location: {profile['owner_location']}")
    if profile.get("owner_interests"):
        bits.append("Interests: " + ", ".join(profile["owner_interests"][:6]))
    if profile.get("custom_instructions"):
        bits.append("Standing instructions: set")
    bits.append(f"Behaviour: {profile.get('behavior_mode')}")
    return " · ".join(bits)


def prompt_block() -> str:
    """The identity block appended to the system prompt ("" when empty).

    Only personalised facts are emitted: a profile nobody has touched adds
    nothing to the prompt, and every line here is something the user told
    J.A.R.V.I.S. rather than a restatement of the written prompt.
    """
    profile = load()
    lines: list[str] = []
    assistant = str(profile.get("assistant_name") or "JARVIS")
    title = str(profile.get("assistant_title") or "").strip()
    if title or assistant != _DEFAULTS["assistant_name"]:
        lines.append(f"Your name is {assistant}"
                     + (f", the user's {title}" if title else "") + ".")
    if profile.get("owner_name"):
        lines.append(f"You are speaking with {profile['owner_name']}"
                     + (f", {profile['owner_role']}" if profile.get("owner_role") else "")
                     + ".")
    if profile.get("owner_location"):
        lines.append(f"They are based in {profile['owner_location']}.")
    if profile.get("owner_interests"):
        lines.append("Their interests: " + ", ".join(profile["owner_interests"][:10]) + ".")
    if profile.get("owner_about"):
        lines.append("About them: " + str(profile["owner_about"]).strip())
    if profile.get("shared_computer"):
        lines.append("This computer is shared: never repeat private details unprompted.")
    if not profile.get("proactive", True):
        lines.append("Stay silent unless you are asked something.")
    instructions = str(profile.get("custom_instructions") or "").strip()
    if instructions:
        lines.append("Standing instructions from the user (always follow): "
                     + instructions)
    if len(lines) <= 1 and not instructions:
        return ""
    return "USER PROFILE\n" + "\n".join(f"- {line}" for line in lines)
