"""Provider-neutral voice orchestration for JARVIS.

Concrete providers remain in ``core.tts`` and ``core.stt``.  This module keeps
the assistant, UI, and future wake-word/VAD providers independent of them.
"""
from __future__ import annotations

import queue
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol


class VoiceMode(StrEnum):
    STANDARD = "standard"
    EXECUTIVE = "executive"
    FRIENDLY = "friendly"
    FOCUS = "focus"
    EMERGENCY = "emergency"
    CINEMATIC = "cinematic"


@dataclass(frozen=True)
class VoiceProfile:
    """A delivery profile, independent of a provider-specific voice ID."""

    name: str
    mode: VoiceMode = VoiceMode.STANDARD
    speed: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0
    british_english: bool = True
    ssml: bool = False

    def __post_init__(self) -> None:
        if not 0.25 <= self.speed <= 3.0:
            raise ValueError("voice speed must be between 0.25 and 3.0")
        if not -24.0 <= self.pitch <= 24.0:
            raise ValueError("voice pitch must be between -24 and 24 semitones")
        if not 0.0 <= self.volume <= 1.0:
            raise ValueError("voice volume must be between 0 and 1")


DEFAULT_PROFILES: dict[VoiceMode, VoiceProfile] = {
    VoiceMode.STANDARD: VoiceProfile("JARVIS Standard"),
    VoiceMode.EXECUTIVE: VoiceProfile("JARVIS Executive", VoiceMode.EXECUTIVE, 0.92, -1.0),
    VoiceMode.FRIENDLY: VoiceProfile("JARVIS Friendly", VoiceMode.FRIENDLY, 1.02, 0.5),
    VoiceMode.FOCUS: VoiceProfile("JARVIS Focus", VoiceMode.FOCUS, 1.08, 0.0),
    VoiceMode.EMERGENCY: VoiceProfile("JARVIS Emergency", VoiceMode.EMERGENCY, 1.18, -1.0),
    VoiceMode.CINEMATIC: VoiceProfile("JARVIS Cinematic", VoiceMode.CINEMATIC, 0.88, -1.5),
}


class TextToSpeech(ABC):
    """Provider contract for blocking or streaming speech synthesis."""

    @abstractmethod
    def speak(self, text: str, profile: VoiceProfile) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        """Stop playback when supported by the provider."""


class SpeechToText(Protocol):
    def transcribe(self, audio: object) -> str:
        ...


class WakeWordDetector(Protocol):
    def process(self, audio: object) -> bool:
        ...


class VoiceActivityDetector(Protocol):
    def is_speech(self, audio: object) -> bool:
        ...


class PronunciationManager:
    """Applies explicit pronunciation replacements before synthesis."""

    def __init__(self, rules: dict[str, str] | None = None) -> None:
        self._rules: dict[str, str] = dict(rules or {})

    def set(self, term: str, pronunciation: str) -> None:
        term = term.strip()
        pronunciation = pronunciation.strip()
        if not term or not pronunciation:
            raise ValueError("pronunciation terms and values are required")
        self._rules[term] = pronunciation

    def remove(self, term: str) -> None:
        self._rules.pop(term, None)

    def apply(self, text: str) -> str:
        for term, pronunciation in self._rules.items():
            text = re.sub(rf"\b{re.escape(term)}\b", pronunciation, text, flags=re.IGNORECASE)
        return text

    def rules(self) -> dict[str, str]:
        return dict(self._rules)


class ExistingTTSAdapter(TextToSpeech):
    """Adapts the existing ``TTSPlayer`` to the voice architecture."""

    def __init__(self, player: object) -> None:
        if not hasattr(player, "speak") or not hasattr(player, "stop"):
            raise TypeError("player must expose speak() and stop()")
        self._player = player

    def speak(self, text: str, profile: VoiceProfile) -> None:
        self._player.speak(text)

    def stop(self) -> None:
        self._player.stop()


class VoiceDirector:
    """Queues interruptible speech and chooses delivery profiles by context."""

    def __init__(
        self,
        engine: TextToSpeech,
        profiles: dict[VoiceMode, VoiceProfile] | None = None,
        pronunciation: PronunciationManager | None = None,
    ) -> None:
        self.engine = engine
        self.profiles = dict(profiles or DEFAULT_PROFILES)
        self.pronunciation = pronunciation or PronunciationManager()
        self._queue: queue.Queue[tuple[str, VoiceProfile] | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run, name="jarvis-voice", daemon=True)
        self._worker.start()

    def select_profile(self, mode: VoiceMode | str) -> VoiceProfile:
        selected = VoiceMode(mode)
        return self.profiles[selected]

    def update_profile(self, mode: VoiceMode | str, **changes: object) -> VoiceProfile:
        selected = VoiceMode(mode)
        profile = replace(self.profiles[selected], **changes)
        self.profiles[selected] = profile
        return profile

    def speak(self, text: str, mode: VoiceMode | str = VoiceMode.STANDARD) -> None:
        if not text.strip():
            raise ValueError("speech text cannot be empty")
        profile = self.select_profile(mode)
        self._queue.put((self.pronunciation.apply(text.strip()), profile))

    def interrupt(self) -> None:
        self._stop_event.set()
        self.engine.stop()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._stop_event.clear()

    def close(self) -> None:
        self._stop_event.set()
        self.engine.stop()
        self._queue.put(None)
        if self._worker.is_alive():
            self._worker.join(timeout=2)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            text, profile = item
            if not self._stop_event.is_set():
                self.engine.speak(text, profile)
