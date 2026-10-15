"""Proactive Auto-Heal — the self-repairing half of J.A.R.V.I.S.

* ``recovery.analyzer``    — traceback -> first-party file, line and risk class
* ``recovery.patcher``     — patch synthesis, atomic apply, verification, rollback
* ``recovery.service``     — the background sentry (hooks, queue, policy, events)
* ``recovery.boot_sentry`` — startup rollback when the last patch crashed the app
* ``recovery.tool``        — the ``self_repair`` tool the model can call

The guarantee the whole package is built around: a patch is *always* reversible
(backup + history row), it never reaches disk without parsing cleanly, and
whether it may be applied without approval is decided by file risk, not by the
model's confidence.
"""

from __future__ import annotations

__all__ = ["analyzer", "boot_sentry", "patcher", "service", "tool"]
