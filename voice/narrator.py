"""Realtime narration — J.A.R.V.I.S. says what he is actually doing, as he does it.

The complaint this answers is conversational, not architectural: while J.A.R.V.I.S.
takes control of the PC he goes silent, and silence reads as "it is stuck". A
narrator fixes that by turning **real** events into **one short spoken line** each:
"Sir, I'm opening the page." / "Sir, I'm clicking that button."

Two rules keep it honest:

* A line is only produced from an event that really happened — the tool that is
  genuinely executing and the progress text the engine genuinely emitted. The
  narrator never invents a step, never reads a script, and never claims success:
  "that's done, sir" is only ever reachable from a completion event, and a
  failure event says so.
* If an event cannot be understood, the narrator says nothing (or, at most, one
  throttled generic line). Silence is honest; a wrong sentence is not.

It is deliberately dumb and dependency-free: pure functions for the phrasing
(so they are unit-testable without a speaker) plus a tiny thread-safe gate that
throttles, de-duplicates and caps how much a single task may say.
"""
from __future__ import annotations

import re
import threading
import time

__all__ = [
    "Narrator",
    "describe_task",
    "describe_progress",
    "describe_end",
    "describe_interrupt",
    "describe_mode",
    "clean_target",
]

# ── phrasing ────────────────────────────────────────────────────────────────
# Ordered: the first pattern that matches wins, so the more specific verbs are
# listed before the generic ones. Each entry is (regex, template) and the
# template's fields come from ``clean_target`` on the matched object group.
_PROGRESS_RULES: list[tuple[str, str]] = [
    # Terminal / failure states — said plainly, never dressed up.
    (r"\b(fail|failed|error|could ?n[o']t|unable|timed ?out|blocked)\b",
     "Sir, that attempt did not go through — trying another way."),
    (r"\b(cancel|cancelled|canceled|abort)\w*\b", "Sir, I have stopped that."),
    (r"\b(complete|completed|done|finished|success)\w*\b",
     "Sir, that is done."),
    # Concrete actions.
    (r"\b(click|clicking|tapped?|tapping|select|selecting)\b",
     "Sir, I'm clicking {target}."),
    (r"\b(typ|enter|fill|input|writ)\w*\b", "Sir, I'm typing that in."),
    (r"\b(press|pressing|hot ?key|shortcut|keystroke)\w*\b",
     "Sir, I'm pressing {target}."),
    (r"\b(scroll)\w*\b", "Sir, I'm scrolling."),
    (r"\b(download|saving|save|export)\w*\b", "Sir, I'm saving that now."),
    (r"\b(upload|attach)\w*\b", "Sir, I'm attaching that now."),
    (r"\b(screenshot|screen ?shot|capture)\w*\b",
     "Sir, I'm taking a look at the screen."),
    (r"\b(read|reading|extract|pars|inspect|analyz|analyse)\w*\b",
     "Sir, I'm reading the page."),
    (r"\b(search|searching|look(?:ing)? up|find(?:ing)?)\b",
     "Sir, I'm searching for that now."),
    (r"\b(navigat|open|opening|visit|go(?:ing)? to|loading)\w*\b",
     "Sir, I'm opening {target}."),
    (r"\b(wait|waiting|loading|render)\w*\b", "Sir, one moment."),
    (r"\b(launch|start|starting|running|execut)\w*\b",
     "Sir, I'm starting {target}."),
]

# Articles are KEPT ("clicking the Stop button" reads naturally, "clicking Stop
# button" does not). Generic nouns are noise on their own: "clicking element 47"
# tells a listener nothing, so a target made only of these is dropped and the
# caller falls back to a vaguer but still TRUE sentence.
_ARTICLES = {"the", "a", "an", "this", "that", "its", "their", "your", "his"}
_GENERIC_NOUNS = {
    "it", "element", "item", "thing", "page", "field", "text", "value",
    "button", "link", "tab", "window", "click", "input", "form", "box",
    "area", "menu", "step", "action", "target", "object", "node",
}

_TASK_LINES = {
    "computer_control": "Sir, taking control of the computer now.",
    "browser_control": "Sir, taking the browser now.",
    "web_research": "Sir, researching that now.",
    "web_search": "Sir, looking that up now.",
    "coding_agent": "Sir, working on the code now.",
}

_GENERIC_LINE = "Sir, still working on it."
_MAX_LINE_CHARS = 100


def clean_target(raw: str, *, limit: int = 6) -> str:
    """Turn a chunk of engine text into something sayable, or ``""``.

    A URL becomes its bare host ("opening example.com"), paths and ids are
    dropped (they are noise out loud), and a value that is only a generic noun
    ("element", "that") yields nothing so the caller can fall back.
    """
    text = str(raw or "").strip().strip("\"'`.,:;()[]")
    if not text:
        return ""
    # URLs → host only. "Opening https://a.b/c?d=1" must not read the query out
    # loud, and must not read as "opening https colon slash slash".
    text = re.sub(
        r"https?://([^/\s]+)\S*",
        lambda m: m.group(1),
        text, flags=re.IGNORECASE,
    )
    # A bare path or identifier is not worth narrating.
    if re.fullmatch(r"[A-Za-z]:\\\S*|/\S+", text):
        return ""
    if re.fullmatch(r"[\w.-]+\.(exe|py|json|txt|log|dll|bat|ps1)", text, re.I):
        return ""
    # Keep the leading word-run, drop punctuation-only tokens.
    words = [w for w in re.split(r"\s+", text) if w]
    words = [w for w in words if not re.fullmatch(r"[^\w]+", w)]
    if not words:
        return ""
    # Nothing but generic nouns and articles: "element 12", "the button".
    meaningful = [w for w in words
                  if w.strip(".,:;()[]").lower() not in _GENERIC_NOUNS
                  and w.strip(".,:;()[]").lower() not in _ARTICLES
                  and not re.fullmatch(r"[0-9.,\s]+", w)]
    if not meaningful:
        return ""
    out = " ".join(words[:limit]).strip().strip("\"'`.,:;()[]")
    if not out or re.fullmatch(r"[0-9a-fA-F]{8,}", out):
        return ""
    return out


def _target_from(match: re.Match, text: str) -> str:
    """The object of the verb: text after the keyword, else after it entirely."""
    tail = text[match.end():]
    target = clean_target(tail)
    if target:
        return target
    # No object after the verb — try the words before it, which is how
    # "the Stop button was clicked" style events read.
    head = text[: match.start()].strip()
    if not head:
        return ""
    return clean_target(" ".join(head.split(" ")[-4:]))


def describe_progress(detail: str) -> str | None:
    """One narration line for a real progress event, or ``None``.

    ``None`` means "this text is not something worth saying" — the caller stays
    quiet rather than making something up.
    """
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    if not text:
        return None
    low = text.lower()
    for pattern, template in _PROGRESS_RULES:
        m = re.search(pattern, low)
        if not m:
            continue
        if "{target}" in template:
            target = _target_from(m, text)
            if not target:
                # Verb understood, object not: use the verb-only phrasing so the
                # line is still true ("I'm clicking." is true when a click ran).
                line = template.replace(" {target}", " that")
                return line[: _MAX_LINE_CHARS]
            line = template.format(target=target)
        else:
            line = template
        return line[: _MAX_LINE_CHARS]
    return None


def describe_task(tool: str, args: dict | None = None) -> str | None:
    """The opening line for a task that is genuinely starting now."""
    name = str(tool or "").strip()
    line = _TASK_LINES.get(name)
    if line:
        return line
    args = args or {}
    # Unknown automation tools still deserve an honest opening line when they
    # are about to act on the machine.
    if name in ("run_command", "desktop_control"):
        return "Sir, running that on the machine now."
    return None


def describe_end(tool: str, status: str = "") -> str | None:
    """The closing line, derived from the real completion status."""
    st = str(status or "").upper()
    if st in ("FAILED", "VERIFICATION_FAILED", "NOT_DONE", "ERROR", "DENIED"):
        return "Sir, that did not work — I have stopped."
    if st in ("SUCCESS", "REQUIRES_PERMISSION", "OK", "DONE"):
        return "Sir, that is done."
    return None


def describe_interrupt() -> str:
    """The stop the user just pressed, said out loud (they asked for this)."""
    return "Halting now, sir."


def describe_mode(ui_tars: bool) -> str:
    """The chat-panel switch, said out loud so the new mode is unmissable."""
    return ("UI TARS mode, sir — I have control of the computer."
            if ui_tars else "Gemini mode, sir — back to conversation.")


class Narrator:
    """Thread-safe gate in front of a ``speak`` callable.

    Responsibilities, all of which exist to keep narration pleasant instead of
    unbearable: never repeat the same line twice in a row, never speak faster
    than ``min_gap_s``, cap the lines a single task may spend, and drop lines
    outright when narration is off or when the speaker says J.A.R.V.I.S. is
    already talking (he must not talk over himself).
    """

    def __init__(self, speak=None, enabled: bool = True, min_gap_s: float = 2.2,
                 max_lines_per_task: int = 12):
        self._speak = speak
        self._enabled = bool(enabled)
        self._min_gap = max(0.0, float(min_gap_s))
        self._max_lines = max(1, int(max_lines_per_task))
        self._lock = threading.RLock()
        self._last_line = ""
        self._last_at = 0.0
        self._task_lines = 0
        self._suppress = None      # callable → True when speech must not start

    # ── configuration ───────────────────────────────────────────────────────
    def set_speaker(self, speak) -> None:
        with self._lock:
            self._speak = speak

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)

    def set_suppress(self, predicate) -> None:
        """Register "do not start speaking right now" (already speaking, muted)."""
        with self._lock:
            self._suppress = predicate

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    # ── task lifecycle ──────────────────────────────────────────────────────
    def begin_task(self) -> None:
        """Reset the per-task budget. Called once when real work starts."""
        with self._lock:
            self._task_lines = 0

    def end_task(self, tool: str = "", status: str = "") -> str | None:
        line = describe_end(tool, status)
        # The closing line ignores the per-task cap (it is the one line that
        # tells the user the wait is over) but still respects the gap.
        return self.say(line, count=False, force_gap=True)

    # ── event intake ────────────────────────────────────────────────────────
    def task(self, tool: str, args: dict | None = None) -> str | None:
        self.begin_task()
        return self.say(describe_task(tool, args))

    def progress(self, tool: str, detail: str) -> str | None:
        line = describe_progress(detail)
        if line is None and detail:
            # Unparsed but real activity: at most one generic line, and only
            # after the normal gap, so an unreadable stream stays quiet.
            line = _GENERIC_LINE
        return self.say(line)

    def interrupt(self) -> str | None:
        return self.say(describe_interrupt(), forced=True)

    def mode(self, ui_tars: bool) -> str | None:
        return self.say(describe_mode(ui_tars), forced=True)

    # ── the gate ────────────────────────────────────────────────────────────
    def say(self, line: str | None, *, count: bool = True,
            forced: bool = False, force_gap: bool = False) -> str | None:
        """Speak ``line`` if it passes the gate; return what was spoken.

        ``forced`` bypasses the gap (a user action such as stop / mode switch
        must be acknowledged immediately); the de-duplication and the task
        budget still apply so a stuck loop cannot chatter.
        """
        text = str(line or "").strip()
        if not text:
            return None
        with self._lock:
            if not self._enabled:
                return None
            # Repeating the previous line is never useful — including for a
            # forced line (pressing stop twice must not say "halting" twice).
            if text == self._last_line:
                return None
            now = time.monotonic()
            if not forced or force_gap:
                if (now - self._last_at) < self._min_gap:
                    return None
            if count and self._task_lines >= self._max_lines:
                return None
            speaker = self._speak
            suppress = self._suppress
            # Reserve the slot before releasing the lock so two threads racing
            # on the same event cannot both speak it.
            self._last_line = text
            self._last_at = now
            if count:
                self._task_lines += 1
        if speaker is None:
            return None
        # A forced line is the acknowledgement of something the user just did
        # (stop, mode switch) — it must not be swallowed by the very state it is
        # announcing, so suppression only applies to automatic narration.
        if suppress is not None and not forced:
            try:
                if suppress():
                    # Not spoken after all — give the slot back so the line can
                    # be said once the other speech finishes.
                    with self._lock:
                        self._task_lines = max(0, self._task_lines - 1)
                        self._last_line = ""
                    return None
            except Exception:
                pass
        try:
            # Contract: speaker(text, forced) — ``forced`` lets the speaker
            # bypass anything that silences new speech (the interruption flag),
            # which is exactly what a "halting now, sir" line needs. Speakers
            # that only take the text still work.
            speaker(text, bool(forced))
        except TypeError:
            try:
                speaker(text)
            except Exception:
                return None
        except Exception:
            return None
        return text

    # ── introspection (UI / diagnostics) ────────────────────────────────────
    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "lines_this_task": self._task_lines,
                "max_lines": self._max_lines,
                "min_gap_s": self._min_gap,
                "last_line": self._last_line,
            }
