"""Mark LII coding-agent layer (change: modernize-jarvis-architecture).

Routes code work to the right backend — Aider for focused, precise edits;
OpenHands (Docker) for larger autonomous tasks — with an exclusive per-tree
lock, availability probing, and self-tree approval via the security layer.

* ``coding_lock.py``   — cross-process exclusive lock per working tree
* ``aider_agent.py``   — Aider backend (native pip, subprocess)
* ``openhands_agent.py`` — OpenHands backend (Docker probe, never imported)
* ``agent_router.py``  — scope routing, fallback chain, approval, execution
"""

from coding.agent_router import (
    execute_coding_task,
    route_scope,
    backend_availability,
    skill_availability_report,
)
from coding.coding_lock import get_coding_lock, CodingLockBusy

__all__ = [
    "execute_coding_task", "route_scope", "backend_availability",
    "skill_availability_report", "get_coding_lock", "CodingLockBusy",
]
