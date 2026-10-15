"""Deprecation shim — implementation moved to app.model_router.

This module is a transparent alias: importing `core/model_router` loads `app.model_router`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from app import model_router as _impl  # noqa: F401
_sys.modules[__name__] = _impl
