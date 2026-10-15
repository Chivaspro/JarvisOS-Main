"""The DGM layer — improve a capability that already exists.

Job split, enforced by ``evolution.manager``: the **Forge** creates capabilities
that do not exist; this package improves ones that do.

``adapter`` is the only interface the rest of J.A.R.V.I.S. talks to — nothing else
imports anything from here. The design follows the Darwin Gödel Machine reference
architecture (`lemoz/darwin-godel-machine`: controller, self-modification,
evaluation, archive) reduced to what fits this project:

* **diagnose** — run the capability's own tests and read the real failures;
* **propose/implement** — ask the model for one candidate improvement;
* **evaluate** — verify the candidate in the existing Skill Crucible;
* **compare** — keep it only when it genuinely beats the incumbent;
* **archive** — keep every version and its metrics, so a rollback is always one
  call away.

Nothing here is a second application: it has no entry point, no UI and no
scheduler. It is a library behind one adapter.
"""

from __future__ import annotations

__all__ = ["adapter", "archive", "policy"]
