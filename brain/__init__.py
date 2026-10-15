"""JARVIS Brain package.

A central orchestration layer (Cognitive Engine) that coordinates the
subsystems: memory, personality, learning, config, vision, voice, wake word,
web, tools, automation.

Design rules:
* The Brain never rewrites source code; it improves routing/context/preferences.
* One subsystem failing must not crash the assistant.
* Everything else in the project stays reachable; the Brain is additive.
"""

from __future__ import annotations

from brain.personality import Personality
from brain.learning import LearningEngine
from brain.engine import Brain, get_brain

__all__ = ["Brain", "Personality", "LearningEngine", "get_brain"]