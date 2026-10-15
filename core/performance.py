"""Deprecation shim — implementation moved to observability.performance.

This module is a transparent alias: importing `core/performance` loads `observability.performance`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from observability import performance as _impl  # noqa: F401
_sys.modules[__name__] = _impl
