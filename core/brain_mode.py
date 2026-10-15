"""Which brain mode does the user want? One pure function, one answer.

J.A.R.V.I.S. has two ways to work, and the chat panel exposes them as a switch:

* **GEMINI** — ordinary conversation. Answer, explain, discuss; act on the
  computer only when explicitly asked.
* **UI TARS** — PC control. Spoken requests are instructions to act.

Until now the switch existed only as a widget: the model had no way to change it,
so "switch to UI TARS" was something it could only *talk* about — and a model
that cannot flip the switch is exactly why the mode felt decorative. This module
is the decision behind the new ``brain_mode`` tool.

It is deliberately pure and tiny: text in, a target mode out. No config writes,
no UI, no session work — the caller owns all of that, which is what makes this
testable without a running J.A.R.V.I.S.

``resolve`` returns ``(target, note)``:

* ``target is True``  → switch to UI TARS
* ``target is False`` → switch to GEMINI
* ``target is None``  → nothing to change (already there, a status question, or
  wording this module does not recognise) — ``note`` still explains the answer so
  the model can say something accurate either way.
"""

from __future__ import annotations

#: Canonical mode names, used by the tool vocabulary and the settings flag.
MODE_GEMINI = "gemini"
MODE_UI_TARS = "ui_tars"
MODES = (MODE_GEMINI, MODE_UI_TARS)

#: Leaving UI TARS is phrased as a *stop* far more often than as a mode name, so
#: these are matched before the bare "ui tars" phrases below ("stop pc control"
#: must not read as a request to enter PC control).
_LEAVE_PHRASES = (
    "stop pc control", "stop ui tars", "exit pc control", "exit ui tars",
    "quit ui tars", "leave pc control", "leave ui tars", "disable pc control",
    "disable ui tars", "turn off ui tars", "turn off pc control",
    "stop controlling", "stop controlling my pc", "stop controlling the pc",
    "back to gemini", "return to gemini", "back to chat", "go back to gemini",
    "switch back", "go back",
)

_UI_TARS_PHRASES = (
    "ui tars", "uitars", "ui-tars", "tars mode", "pc control", "pc mode",
    "computer control", "control mode", "act mode", "action mode",
    "take control", "control my pc", "control the pc", "control my computer",
    "control the computer", "use the computer", "act on my pc", "act on the pc",
)

_GEMINI_PHRASES = (
    "gemini mode", "gemini", "chat mode", "conversation mode", "normal mode",
    "talk mode", "voice mode", "assistant mode", "plain mode",
)

_TOGGLE_WORDS = ("toggle", "switch", "flip", "change")

_STATUS_WORDS = ("status", "state", "which mode", "what mode", "current mode",
                 "are we in", "mode?")


def _normalise(request) -> str:
    """Lowercase, collapse whitespace, drop surrounding punctuation/quotes."""
    text = " ".join(str(request or "").lower().replace("_", " ").split())
    return text.strip(" \t\r\n.,!?;:'\"`")


def resolve(request, current: bool) -> tuple[bool | None, str]:
    """Resolve a mode request against the mode that is active now.

    ``current`` is the live mode (True = UI TARS); ``target`` is what it should
    become, or None when nothing was asked for.
    """
    text = _normalise(request)
    now = "UI TARS (act on the PC)" if current else "GEMINI (conversation)"

    if not text:
        return (None, f"Brain mode is {now}.")

    # A stop/leave phrase wins: it names UI TARS only to switch it off.
    if any(phrase in text for phrase in _LEAVE_PHRASES):
        return (False, "Leaving PC control mode: acting stops and J.A.R.V.I.S. "
                       "goes back to conversation.")

    if any(phrase in text for phrase in _UI_TARS_PHRASES):
        if current:
            return (None, "Brain mode is already UI TARS (act on the PC).")
        return (True, "Switching to UI TARS mode: requests are now treated as "
                      "instructions to act on this computer.")

    if any(phrase in text for phrase in _GEMINI_PHRASES):
        if not current:
            return (None, "Brain mode is already GEMINI (conversation).")
        return (False, "Switching to GEMINI mode: back to ordinary conversation.")

    if any(word in text for word in _TOGGLE_WORDS):
        target = not current
        which = "UI TARS (act on the PC)" if target else "GEMINI (conversation)"
        return (target, f"Switching brain mode to {which}.")

    if any(word in text for word in _STATUS_WORDS):
        return (None, f"Brain mode is {now}.")

    return (None, f"'{request}' does not name a mode. The modes are UI TARS "
                  "(act on the PC) and GEMINI (conversation). "
                  f"Right now it is {now}.")


__all__ = ["MODE_GEMINI", "MODE_UI_TARS", "MODES", "resolve"]
