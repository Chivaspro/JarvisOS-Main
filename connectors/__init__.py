"""The Composio connector system — J.A.R.V.I.S.'s only route to external apps.

The architecture this package exists to enforce::

    J.A.R.V.I.S.  →  connectors.manager  →  Composio  →  toolkit  →  tool

There are deliberately **no per-service connector classes** in here. Spotify,
Telegram, Discord, Gmail, GitHub, Calendar and every other app J.A.R.V.I.S.
talks to are *toolkits inside Composio*, not subsystems of their own. A
``SpotifyConnector`` would be a second integration architecture wearing one
app's name, and this project has exactly one:

.. code-block:: text

    WRONG   J.A.R.V.I.S. ├── SpotifyConnector
                        ├── TelegramConnector
                        └── Composio

    RIGHT   J.A.R.V.I.S. └── Composio ├── spotify
                                      ├── telegram
                                      └── discord …

Layers
------
``composio_client``
    The only module in the project that imports ``composio``. Owns the API key
    lookup and the client object, and nothing else.
``manager``
    The Connector Manager: toolkits, connected accounts, the OAuth connect
    link, and tool execution. This is what the HUD and the agent both call.
``tool``
    The model-facing ``connector`` tool, declared in ``main.py``.
``icons``
    Presentation only — brand colour, initials, label. Never a source of truth
    about what is available.

Rules held everywhere in here
-----------------------------
* **Nothing raises across the boundary.** Every public call returns a dict
  carrying ``ok`` / ``detail`` / ``reason``, so the HUD and the model can report
  honestly instead of unwinding a traceback into the user's face.
* **No secret ever leaves.** Access tokens and refresh tokens are Composio's to
  hold. This layer never stores, returns, logs or displays one; the only
  credential J.A.R.V.I.S. owns is the project API key.
* **Degrade, never crash.** No package, no key, a refused key and a network
  failure are four different sentences, and none of them stops the assistant —
  the same rule the rest of the codebase holds.
* **Heavy imports stay lazy.** ``composio`` is imported on first use, never at
  module scope, so ``import ui`` stays fast and a machine without the package
  still starts.
"""

from __future__ import annotations

__all__ = ["composio_client", "icons", "manager", "tool"]