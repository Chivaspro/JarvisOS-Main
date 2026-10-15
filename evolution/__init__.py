"""J.A.R.V.I.S. self-evolution subsystem — the Skill Crucible.

Four small modules, one job each:

* ``evolution.crucible``  — static + sandbox verification for candidate code
* ``evolution.forge``     — natural language goal -> verified plugin source
* ``evolution.vault``     — writes a verified plugin into ``plugins/`` and
                            hot-registers it through ``app.plugin_loader``
* ``evolution.discovery`` — detects "learn how to ..." directives and turns a
                            missing-capability result into a forge request
* ``evolution.registry``  — list / toggle / delete / telemetry for forged skills

Nothing here decides anything on its own: the model asks for a tool, the
Crucible either proves it works or refuses it, and the vault installs it as an
ordinary Jarvis plugin — the same shape a hand-written plugin has.
"""

from __future__ import annotations

__all__ = ["crucible", "dgm", "discovery", "forge", "manager", "registry", "vault"]
