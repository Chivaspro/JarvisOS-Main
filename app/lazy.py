"""core/lazy.py — lazy heavy-module proxy.

Importing cv2, requests or google.genai at module
import time delays JARVIS becoming ready. These proxies take the place of the
real module at import time and perform the actual import on the first
attribute access, so every existing ``cv2.foo(...)`` / ``requests.get``
call site keeps working unchanged while the cost moves to the first *use*
(inside a tool worker), not the boot.

    from app.lazy import lazy_module

    def _setup(m):             # runs once, right after the real import
        ...

    cv2 = lazy_module("cv2", _setup)
"""
from __future__ import annotations


class lazy_module:
    """Stand-in for a module imported on first attribute access.

    ``bool(proxy)`` forces the import (cached) and reports whether it
    succeeded — so a caller can cheaply test whether the module is available
    without paying the import cost twice.
    """

    __slots__ = ("_name", "_setup", "_real", "_failed")

    def __init__(self, name: str, setup=None):
        self._name   = name
        self._setup  = setup
        self._real   = None
        self._failed = False

    def _load(self):
        if self._real is not None:
            return self._real
        if self._failed:
            raise ImportError(f"No module named {self._name!r}")
        try:
            real = __import__(self._name)
        except Exception:
            self._failed = True
            raise
        if self._setup is not None:
            try:
                self._setup(real)
            except Exception:
                pass
        self._real = real
        return real

    def __getattr__(self, item):
        return getattr(self._load(), item)

    def __bool__(self):
        try:
            self._load()
            return True
        except Exception:
            return False