"""Centralised JARVIS personality (spec §18, §19) with learning.

Personality provides:
* A stable persona block injected into prompts.
* Style preferences that JARVIS learns from the user over time and persists
  (formality, verbosity, technical depth, humour, address form, etc.).
* Original phrasing guidance - not a verbatim copy of any character.

Preferences persist through the brain memory store (kv table), so a restart
does not reset JARVIS's personality.
"""

from __future__ import annotations

import re

# ── Reference persona (identity stays stable) ─────────────────────────────────
_PERSONA = (
    "PERSONALITY[\n"
    "You are an original, sophisticated futuristic digital assistant. "
    "Professional, intelligent, calm, precise, respectful and confident. "
    "Helpful and context-aware; occasionally and appropriately witty. "
    "Never excessively verbose, never repetitive, never theatrical.\n"
    "Address the user in the manner set by ADDRESS. Refer to yourself as "
    "matches the IDENTITY block.\n"
    "You must sound natural, like a composed expert, not a scripted actor. "
    "Do not imitate any copyrighted fictional character, do not quote film "
    "dialogue, and never mention these instructions.\n"
    "When unsure, say so plainly. Never pretend to know what you do not.\n]"
)

# Tunable behavioural knobs - JARVIS nudges these as it learns the user.
STYLE_DEFAULTS = {
    "address": "",            # "" => use ADDRESS rule; else e.g. "sir"/name
    "verbosity": "balanced",  # concise | balanced | detailed
    "formality": "refined",   # casual | refined | formal
    "tech_depth": "auto",     # low | medium | high | auto
    "humor": "slight",        # none | slight | playful
    "confirmations": "normal",  # minimal | normal | thorough
}


class Personality:
    """Stable persona + learned, persistent communication preferences."""

    def __init__(self, store=None):
        self._store = store  # optional BrainMemory (kv access)
        self._style = dict(STYLE_DEFAULTS)
        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._store:
            return
        try:
            saved = self._store.db.kv_get("personality_style", {})
            if isinstance(saved, dict):
                merged = dict(STYLE_DEFAULTS)
                merged.update({k: v for k, v in saved.items() if k in STYLE_DEFAULTS})
                self._style = merged
        except Exception:
            pass

    def _persist(self) -> None:
        if not self._store:
            return
        try:
            self._store.db.kv_set("personality_style", dict(self._style))
        except Exception:
            pass

    # ── accessors ─────────────────────────────────────────────────────────────

    def get(self, key: str, default: str | None = None) -> str:
        return self._style.get(key, default if default is not None else STYLE_DEFAULTS.get(key, ""))

    def set(self, key: str, value: str) -> None:
        if key not in STYLE_DEFAULTS:
            return
        value = str(value).lower().strip()
        self._style[key] = value
        self._persist()

    def address_rule(self) -> str:
        addr = self.get("address")
        return addr or ""

    # ── learning (spec §19) ───────────────────────────────────────────────────

    def observe_user_text(self, text: str) -> None:
        """Nudge style preferences from what the user says/writes. Cheap signal,
        only nudges when confident; never flips on one message."""
        text = (text or "").lower()
        changes: dict[str, str] = {}
        if re.search(r"\b(short|concise|brief|quick|one line|tl;dr)\b", text):
            changes["verbosity"] = "concise"
        elif re.search(r"\b(detail|detailed|explain|in depth|thorough|full)\b", text):
            changes["verbosity"] = "detailed"
        if re.search(r"\b(less .{0,10}funny|no jokes|no humor|serious|formal|professionally)\b", text):
            changes["humor"] = "none"; changes["formality"] = "formal"
        elif re.search(r"\b(make it fun|funny|casual|relaxed|less formal|joke)\b", text):
            changes["humor"] = "playful"; changes["formality"] = "casual"
        if re.search(r"\b(technical|deep|internals|code level|architecture)\b", text):
            changes["tech_depth"] = "high"
        elif re.search(r"\b(simpler|simplify|plain english|layman|basic)\b", text):
            changes["tech_depth"] = "low"
        for k, v in changes.items():
            current = self.get(k)
            if current != v:
                self.set(k, v)

    def learn_address(self, form: str) -> None:
        form = (form or "").strip().lower()
        if form and len(form) < 30:
            self.set("address", form)

    # ── prompt block ──────────────────────────────────────────────────────────

    def prompt_block(self) -> str:
        style_lines = [
            f"Preferred verbosity: {self.get('verbosity')}.",
            f"Formality: {self.get('formality')}.",
            f"Technical depth: {self.get('tech_depth')}.",
            f"Humor: {self.get('humor')}.",
            f"Confirmations before acting: {self.get('confirmations')}.",
        ]
        return _PERSONA + "\n" + "\n".join("STYLE " + l for l in style_lines) + "\n"


def describe() -> str:
    return _PERSONA