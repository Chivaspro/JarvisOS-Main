"""Deprecation shim — implementation moved to app.config_guard.

This module is a transparent alias: importing `core/config_guard` loads `app.config_guard`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from app import config_guard as _impl  # noqa: F401
_sys.modules[__name__] = _impl
