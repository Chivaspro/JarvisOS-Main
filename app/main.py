"""Entrypoint for the optional Phase 1 service layer.

The established ``main.py`` desktop runtime remains the default launcher.
"""
from .api import create_app
from .core.config import AppConfig

app = create_app(AppConfig.from_environment())
