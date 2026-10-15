"""Deprecation shim — implementation moved to security.undo.

This module is a transparent alias: importing `core/undo` loads `security.undo`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from security import undo as _impl  # noqa: F401
_sys.modules[__name__] = _impl
