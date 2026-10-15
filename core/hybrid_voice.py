"""Deprecation shim — implementation moved to voice.hybrid_voice.

This module is a transparent alias: importing `core/hybrid_voice` loads `voice.hybrid_voice`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from voice import hybrid_voice as _impl  # noqa: F401
_sys.modules[__name__] = _impl
