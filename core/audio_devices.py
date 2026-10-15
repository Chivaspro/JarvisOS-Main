"""Deprecation shim — implementation moved to voice.audio_devices.

This module is a transparent alias: importing `core/audio_devices` loads `voice.audio_devices`
(including private names), so plugins and scripts keep working unchanged.
Kept for one release (change: modernize-jarvis-architecture, task 8.2);
import from the new location instead.
"""

import sys as _sys

from voice import audio_devices as _impl  # noqa: F401
_sys.modules[__name__] = _impl
