"""JarvisOS Connect — a paired phone or browser reaching this desktop.

The pieces:

* ``devices.py``  — the pairing code, the per-device tokens and the registry
* ``commands.py`` — the closed set of things a device may ask for, each gated by
  the same Settings switch the voice UI uses
* ``gateway.py``  — the HTTP + WebSocket server (loopback unless the user opts
  out) and the confirmation bridge for destructive commands
* ``web/``        — the client, served by the gateway, so nothing has to be
  installed to use it
* ``tool.py``     — the ``connect_devices`` tool the assistant talks through
* ``runtime.py``  — how the gateway reaches the live desktop without touching a
  widget from a worker thread
"""

from __future__ import annotations

__all__ = ["commands", "devices", "gateway", "runtime", "tool"]
