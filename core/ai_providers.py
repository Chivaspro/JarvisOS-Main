"""Deprecation shim — implementation moved to app.ai_providers.

This module is a transparent alias: importing `core/ai_providers` loads `app.ai_providers`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from app import ai_providers as _impl  # noqa: F401
_sys.modules[__name__] = _impl
