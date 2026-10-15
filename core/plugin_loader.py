"""Deprecation shim — implementation moved to app.plugin_loader.

This module is a transparent alias: importing `core/plugin_loader` loads `app.plugin_loader`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from app import plugin_loader as _impl  # noqa: F401
_sys.modules[__name__] = _impl
