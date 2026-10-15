"""Deprecation shim — implementation moved to voice.sfx.

This module is a transparent alias: importing `core/sfx` loads `voice.sfx`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from voice import sfx as _impl  # noqa: F401
_sys.modules[__name__] = _impl
