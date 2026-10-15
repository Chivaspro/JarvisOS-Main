"""JARVIS Learning Engine (spec §9-§16, §40, §56).

Controlled continuous learning - no self-modifying binaries:

* Corrections: user corrects JARVIS -> recorded, contradictions superseded.
* Success/failure: tool and provider outcomes accumulate into stats.
* Workflows: repeated successful sequences become reusable workflows.
* Experiences: reusable knowledge gets stored and linked.
* Adaptive tool selection: prefer the most reliable tool for an intent once
  enough evidence exists (never after a single event - spec §13).

All state lives in the brain memory database, so learning survives restarts.
"""

from __future__ import annotations

import re
import time
from typing import Any

_CORRECTION_HINTS = (
    "no", "not that", "wrong", "incorrect", "i meant", "not the", "that's not",
    "instead", "don't", "should be", "actually", "you got it wrong", "not what i",
)


class LearningEngine:
    """High-level learning operations over a BrainMemory store."""

    def __init__(self, memory):
        self._memory = memory

    @property
    def memory(self):
        return self._memory

    # ── corrections (spec §10) ────────────────────────────────────────────────

    def is_correction(self, user_text: str) -> bool:
        low = (user_text or "").lower()
        return any(h in low for h in _CORRECTION_HINTS) and len(low) > 6

    def handle_correction(self, user_text: str) -> str | None:
        """When the user says something that *is* the corrected fact (e.g. 'it's
        not X, it's Y'), extract Y and store it as a superseding memory."""
        try:
            low = (user_text or "").strip()
            if not low:
                return None
            # Patterns: "not X, it's Y" / "no, it's Y" / "it's Y, not X" / "instead use Y"
            m = re.search(r"(?:it'?s|it is|use|should be)\s+(.+?)(?:\s+not\s+|\s+instead|\s*,|$)", low)
            corrected = None
            if m:
                corrected = m.group(1).strip(" .,")
            if not corrected:
                m2 = re.search(r"not\s+([^,]{2,40}?)[,.]+\s+it's\s+(.+)", low)
                if m2:
                    corrected = m2.group(2).strip()
            if corrected and len(corrected) > 2:
                return self._memory.learn_correction(original=low[:120], corrected=corrected)
        except Exception:
            return None
        return None

    # ── success / failure (spec §11, §12) ─────────────────────────────────────

    def record_success(self, request: str, intent: str, tool: str, ms: float, steps=None) -> None:
        self._memory.record_task_outcome(request, intent, tool, True, ms, steps=steps)

    def record_failure(self, request: str, intent: str, tool: str, ms: float, error: str, fallback: str | None = None) -> None:
        self._memory.record_task_outcome(request, intent, tool, False, ms, error=error)
        if fallback:
            # The fallback that worked becomes an experience too.
            self._memory.record_experience(
                f"{request[:80]} failed with {tool} ({error[:80][:60]}); "
                f"fallback {fallback} worked.",
                kind="workflow", outcome="success",
                context=intent or request[:40],
            )

    def record_provider(self, provider: str, ok: bool, ms: float) -> None:
        try:
            self._memory.db.record_provider_result(provider, ok, ms)
        except Exception:
            pass

    # ── task → workflow promotion (spec §11) ──────────────────────────────────

    def promote_workflow(self, intent: str, steps: list[str], description: str = "") -> None:
        """Called when the same sequence succeeds repeatedly - creates/reinforces
        a reusable workflow used by the Brain's routing."""
        key = intent or description or "generic"
        key = key.strip().lower()[:80]
        if not key:
            return
        try:
            self._memory.db.upsert_workflow({
                "intent_key": key, "steps": steps, "success_count": 1,
                "fail_count": 0, "description": description,
            })
        except Exception:
            pass

    def best_workflow_for(self, intent: str) -> dict | None:
        try:
            return self._memory.db.best_workflow(intent)
        except Exception:
            return None

    # ── feedback verbatim (spec §15, §43) ────────────────────────────────────

    def handle_feedback(self, user_text: str) -> dict:
        """Handles explicit learning commands. Returns {handled: bool, reply: str}."""
        low = (user_text or "").strip().lower()
        reply = ""
        handled = False

        if low.startswith(("remember ", "remember this", "learn this", "don't forget ")):
            content = user_text
            for p in ("Remember this: ", "Remember this: ", "remember to ", "remember that ",
                      "learn this: ", "Don't forget that ", "don't forget to ", "Don't forget: ",
                      "remember this: ", "remember ", "learn this ", "remember that "):
                if low.startswith(p.lower()):
                    content = user_text[len(p):].strip(" :.")
                    break
            if content:
                self._memory.remember(content, explicit=True, confidence=0.9)

                # And into the selected store (Obsidian Vault when active).
                try:
                    from memory.backend import store_in_backend
                    store_in_backend(content, explicit=True,
                                     source="feedback", confidence=0.9)
                except Exception:
                    pass
                reply = f"Understood. I've remembered: {content}"
                handled = True

        elif low.startswith(("forget ", "forget that", "don't remember that", "forget this")):
            content = user_text
            for p in ("Forget that ", "Forget this ", "Forget ", "Don't remember that ",
                      "Forget that: ", "forget "):
                if low.startswith(p.lower()):
                    content = user_text[len(p):].strip(" :.")
                    break
            if content and self._memory.forget_content(content):
                reply = f"Forgotten: {content}"
                handled = True
            elif content:
                reply = "I couldn't find anything stored matching that exact text."
                handled = True

        elif low.startswith(("that's wrong", "that's incorrect", "you got it wrong", "not correct", "wrong")):
            self._memory.record_experience(
                f"User corrected me: {user_text[:120]}", kind="correction",
                outcome="success",
            )
            reply = "Understood - I've noted the correction and will not repeat the mistake."
            handled = True

        elif low.startswith(("that's correct", "that's right", "correct", "that worked", "nice, that worked",
                             "good job", "perfect, that worked")):
            self._memory.record_experience(
                f"User confirmed a correct response: {user_text[:120]}",
                kind="feedback", outcome="success",
            )
            reply = "Good - I'll keep that approach."
            handled = True

        elif low.startswith(("what have you learned", "what did you learn")):
            reply = self._memory.learned_report()
            handled = True

        elif low.startswith(("what do you remember about me", "what do you know about me",
                             "show me what you know about me")):
            rows = self._memory.retrieve("", limit=20)
            if not rows:
                reply = "I don't have any stored memories about you yet."
            else:
                reply = "From my memory, I know:\n" + "\n".join(
                    f"- {m['content']}" for m in rows[:15])
            handled = True

        elif low.startswith(("what do you remember about this project",
                             "what do you know about my project")):
            rows = self._memory.db.list_memories(category="PROJECTS", limit=15)
            if not rows:
                reply = "No project memories stored yet."
            else:
                reply = "Project memories:\n" + "\n".join(
                    f"- {m['content']}" for m in rows)
            handled = True

        elif low.startswith(("use this method next time", "use this method from now on",
                             "do it this way next time")):
            self._memory.record_experience(
                f"User preference for future method: {user_text[:120]}",
                kind="preference", outcome="success",
            )
            reply = "Noted - I'll use that method going forward."
            handled = True

        elif low.startswith("remember this"):
            pass  # handled above

        return {"handled": handled, "reply": reply}

    # ── reports ───────────────────────────────────────────────────────────────

    def report(self) -> str:
        return self._memory.learned_report()