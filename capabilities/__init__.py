"""Unified capability layer — Gemini stays the brain, this layer owns the hands.

J.A.R.V.I.S. reasons about a request here and then delegates the work to exactly
one installed controller per capability:

    computer_control  ->  Agent TARS   (PC: launch/run apps; browser when needed)
    browser_control   ->  Agent TARS   (real-browser web workflow automation)
    web_research      ->  Agent Reach  (internet research channels)
    pc_control        ->  pywinauto then PyAutoGUI   (one desktop action at a time)
    visual_control    ->  UI-TARS prompt + parser on the Gemini model
    browser_action    ->  Playwright  (one page element at a time)
    power_control     ->  Windows itself  (lock, sleep, restart, volume, brightness)

Selection is deterministic and reported: every result names the controller that
actually ran (see registry.py for the action vocabulary, actions.py for the
handlers and docs/pc_web_control.md for the whole picture). decide.py answers
"which controller should serve this request" from live probes, and media.py
takes music to the player the user already has installed instead of a web page.

Agent TARS v0.3.0 is a Node runtime (``@agent-tars/cli``, pinned in the project's
``node_modules``) whose own agent loop runs on the SAME Gemini configuration
J.A.R.V.I.S. already uses. Agent Reach runs in its own Python 3.11 virtual
environment; J.A.R.V.I.S. runs on 3.12, so nothing here imports an engine
directly — each adapter drives an out-of-process process and exchanges one JSON
document per task (see capabilities/proc.py).

Design rules (mirrors coding/agent_router.py, the project's existing external
backend pattern):
* One router, one adapter per engine, no duplicate control engines.
* Only one engine may own a target at a time (capabilities/locks.py).
* Availability is *probed*, never assumed, and a missing backend is reported
  honestly instead of being silently substituted.
* No secrets in this package and none in its logs.
* The model's words are data, never code: an action is validated against the
  registry and dispatched to a handler.
"""

from capabilities.router import (  # noqa: F401
    CapabilityResult,
    route_computer_control,
    route_browser_control,
    route_web_research,
)

__all__ = [
    "CapabilityResult",
    "route_computer_control",
    "route_browser_control",
    "route_web_research",
]
