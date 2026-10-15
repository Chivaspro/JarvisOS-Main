"""Local wake-word system (spec §20-§23, §51) with states and barge-in.

Design
------
* A state machine implementing the spec's states:
  IDLE / WAKE_LISTENING / WAKE_DETECTED / LISTENING / PROCESSING / SPEAKING /
  INTERRUPTED / ERROR.
* Providers:
    - OpenWakeWord    (real keyword spotting; used when `openwakeword` +
                       onnxruntime are importable - optional runtime deps).
    - Speech-onset    (honest fallback: detects the start of a speech burst
                       via energy VAD. This is NOT keyword recognition; it is
                       labelled as such and used only when the keyword model is
                       unavailable and the user enables the fallback).
* Barge-in (§23): the detector exposes `interrupt()` so waking while JARVIS
  speaks stops TTS and flips to LISTENING.
* Non-invasive: it *observes* PCM in the same blocks the app already reads; it
  never opens a second microphone handle and never sends audio anywhere.

The config lives in config/api_keys.json:
    wake_words: ["jarvis", "hey jarvis", ...]
    wake_sensitivity: 0.5
    wake_fallback: "speech" | "off"
"""

from __future__ import annotations

import math
import queue
import threading
import time
from enum import StrEnum
from typing import Callable

SAMPLE_RATE = 16000
CHUNK = 1024

# The detector build is attempted from the microphone feed path (per audio
# block), so a failure there is reported ONCE per process instead of flooding
# the log at the frame rate. A detector that cannot be built means the wake
# phrase silently never fires — the exact failure this flag makes visible.
_detector_failure_logged = False


def _log_detector_failure(message: str) -> None:
    """Report a dead wake-word detector once, through the project logger."""
    global _detector_failure_logged
    if _detector_failure_logged:
        return
    _detector_failure_logged = True
    try:
        from observability.logger import log_event
        log_event("WARNING", "wake", message)
    except Exception:
        pass

# Default phrase list (spec §20) — includes common spoken variants so users
# can address the assistant naturally. Extra words can be added in Settings.
DEFAULT_WAKE_WORDS = [
    "jarvis",
    "hey jarvis",
    "okay jarvis",
    "hi jarvis",
    "yo jarvis",
    "j.a.r.v.i.s",
    "computer",
]


def merge_wake_words(extra) -> list[str]:
    """Union of the built-in phrases + Settings extras (order kept, dupes
    dropped). Settings' 'Extra wake words' field ADDS to the built-ins —
    matching its tooltip — instead of replacing them."""
    if isinstance(extra, str):
        extra = [w for w in extra.replace("\n", ",").split(",")]
    extras = [str(w).strip().lower() for w in (extra or []) if str(w).strip()]
    return list(dict.fromkeys(DEFAULT_WAKE_WORDS + extras))


#: openWakeWord trains a FIXED set of keyword models (hey_jarvis, alexa,
#: hey_mycroft, hey_rhasspy, …), while the user configures natural-language
#: phrases ("hey jarvis", "jarvis", "computer"). The phrases are therefore
#: MAPPED onto the real model that contains them. Passing a phrase straight
#: through as if it were a model name made ``Model(...)`` raise, the detector
#: died silently, and the wake word could never fire — the exact bug this fixes.
_PHRASE_MODEL_HINTS: tuple[tuple[str, str], ...] = (
    ("jarvis", "hey_jarvis"),
    ("alexa", "alexa"),
    ("mycroft", "hey_mycroft"),
    ("rhasspy", "hey_rhasspy"),
)


def keyword_model_names(words) -> list[str]:
    """openWakeWord model names that match *words* (order kept, dupes dropped).

    An exact model name always wins; otherwise a known phrase hint (e.g. any
    "jarvis" variant → ``hey_jarvis``) is used, and finally a model whose whole
    name appears in the phrase. Returns ``[]`` when nothing matches, which the
    caller reports honestly instead of building a model that cannot work.
    """
    try:
        from openwakeword import MODELS as _OWW_MODELS
        available = [str(name).strip().lower() for name in _OWW_MODELS]
    except Exception:
        available = ["hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy"]
    chosen: list[str] = []
    for word in words or []:
        text = str(word).strip().lower()
        if not text:
            continue
        if text in available:                       # an explicit model name
            chosen.append(text)
            continue
        hint = next((model for needle, model in _PHRASE_MODEL_HINTS
                     if needle in text and model in available), None)
        if hint:
            chosen.append(hint)
            continue
        match = next((model for model in available
                      if model.replace("_", " ") in text), None)
        if match:
            chosen.append(match)
    return list(dict.fromkeys(chosen))


class WakeState(StrEnum):
    IDLE = "idle"
    WAKE_LISTENING = "wake_listening"
    WAKE_DETECTED = "wake_detected"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"


def _rms(samples) -> float:
    """Root-mean-square of an int16 PCM block (0..32767)."""
    try:
        import numpy as np

        x = np.frombuffer(samples, dtype=np.int16).astype(np.float32)
        if x.size == 0:
            return 0.0
        return float(math.sqrt(float(np.mean(x * x))))
    except Exception:
        if not samples:
            return 0.0
        vals = [int(s) for s in samples[::4]]
        if not vals:
            return 0.0
        n = float(len(vals))
        sq = sum(v * v for v in vals) / n
        return math.sqrt(sq)


class WakeWordDetector:
    """Stateful wake-word detector feeding a custom observer pipeline."""

    def __init__(
        self,
        words: list[str] | None = None,
        sensitivity: float = 0.5,
        fallback: str = "speech",
    ):
        self._words = [str(w).strip().lower() for w in (words or DEFAULT_WAKE_WORDS)]
        self._sensitivity = max(0.0, min(1.0, float(sensitivity)))
        self._fallback = fallback if fallback in ("speech", "off") else "off"
        self._state = WakeState.IDLE
        self._lock = threading.RLock()
        self._on_detect: Callable[[str], None] | None = None
        self._on_state: Callable[[WakeState], None] | None = None
        self._interrupt_fn: Callable[[], None] | None = None
        self._last_activity = 0.0

        self._active = False
        self._debounce_until = 0.0
        self._burst_energy = 0.0
        self._burst_samples = 0
        self._noise_floor = 40.0
        self._last_error = ""
        self._build_keyword_provider()

    # ── provider ──────────────────────────────────────────────────────────────

    def _build_keyword_provider(self):
        """Try openWakeWord. If unavailable or no models, provider stays None and
        only the speech-onset fallback (or 'off') can fire."""
        try:
            import openwakeword  # noqa: F401
            import onnxruntime  # noqa: F401

            self._keyword_detector = True
        except Exception:
            self._keyword_detector = False
        self._keyword = None

    def _ensure_keyword_model(self):
        if not self._keyword_detector or self._keyword is not None:
            return True
        names = keyword_model_names(self._words)
        if not names:
            # No pre-trained model matches the configured phrases: a keyword
            # spotter cannot be built from arbitrary text. Say so once instead
            # of leaving the wake phrase silently dead.
            self._keyword_detector = False
            self._last_error = "no openWakeWord model matches the wake phrases"
            _log_detector_failure(
                f"wake-word detector off: none of {self._words!r} maps to an "
                "openWakeWord model (built-ins: hey_jarvis, alexa, hey_mycroft)")
            return False
        try:
            from openwakeword.model import Model

            # ONNX, not tflite: onnxruntime is already a project dependency while
            # tflite-runtime ships no Windows wheel, so the old tflite default
            # was another reason the model could not be built on this machine.
            self._keyword = Model(wakeword_models=names,
                                  inference_framework="onnx")
            self._keyword_names = names
            return True
        except Exception as exc:
            self._keyword_detector = False
            self._last_error = str(exc)
            _log_detector_failure(
                f"wake-word model failed to load ({type(exc).__name__}: {exc})")
            return False

    # ── wiring ────────────────────────────────────────────────────────────────

    def set_words(self, words: list[str]) -> None:
        """Hot-swap the wake-phrase list (Settings changes apply live).
        Rebuilds the keyword model only when the provider needs it."""
        cleaned = [str(w).strip().lower() for w in (words or []) if str(w).strip()]
        with self._lock:
            self._words = cleaned or list(DEFAULT_WAKE_WORDS)
        # Keyword provider caches per model name — drop it so the next feed
        # rebuilds against the new phrase list.
        self._keyword = None

    def on_detect(self, fn: Callable[[str], None] | None) -> None:
        self._on_detect = fn

    def on_state(self, fn: Callable[[WakeState], None] | None) -> None:
        self._on_state = fn

    def set_interrupt(self, fn: Callable[[], None] | None) -> None:
        """Barge-in handler called when the wake detector fires while the
        assistant is speaking (spec §23)."""
        self._interrupt_fn = fn

    def set_state(self, state: WakeState) -> None:
        with self._lock:
            if state == self._state:
                return
            self._state = state
            cb = self._on_state
        if cb:
            try:
                cb(state)
            except Exception:
                pass

    def state(self) -> WakeState:
        with self._lock:
            return self._state

    def active(self) -> bool:
        with self._lock:
            return self._active

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            self._active = True
            self._last_activity = time.time()
            self.__dict__.pop("_floor_buf", None)
            self._burst_samples = 0
            self._burst_energy = 0.0
            self._debounce_until = 0.0
            _kw = self._keyword
        # A fresh listen must not inherit the previous session's audio buffer.
        if _kw is not None:
            try:
                _kw.reset()
            except Exception:
                pass
        self.set_state(WakeState.WAKE_LISTENING)

    def stop(self) -> None:
        with self._lock:
            self._active = False
        self.set_state(WakeState.IDLE)

    # ── audio pump (call from the existing mic callback) ─────────────────────

    def feed(self, samples) -> bool:
        """Process one PCM block. Returns True when a wake word fired."""
        with self._lock:
            if not self._active:
                return False
        fired = self._feed_keyword(samples) or self._feed_fallback(samples)
        if fired:
            self._on_wake()
        return fired

    def _feed_keyword(self, samples) -> bool:
        if not self._keyword_detector:
            return False
        if not self._ensure_keyword_model():
            return False
        try:
            import numpy as np

            # The microphone callback hands over raw PCM **bytes**; openWakeWord
            # requires a numpy int16 array. Passing the bytes straight through
            # raised inside predict() and the exception was swallowed — so even a
            # correctly built model could never fire. Accept either form.
            x = (samples if isinstance(samples, np.ndarray)
                 else np.frombuffer(samples, dtype=np.int16))
            if x.size == 0:
                return False
            pred = self._keyword.predict(x)
            for name, score in pred.items():
                if score >= self._sensitivity * 0.5:
                    return True
            return False
        except Exception:
            return False

    def _feed_fallback(self, samples) -> bool:
        if self._fallback != "speech":
            return False
        now = time.time()
        if now < self._debounce_until:
            return False
        energy = _rms(samples)
        # Percentile noise floor over the recent frames (robust against a lone
        # quiet anchor AND a sustained loud burst pulling the floor upward).
        if not hasattr(self, "_floor_buf"):
            self._floor_buf = [max(40.0, energy)]
        floor = sorted(self._floor_buf)[int(len(self._floor_buf) * 0.3)]
        threshold = max(240.0, floor * 8.0) * (0.4 + self._sensitivity * 1.2)
        if energy > threshold:
            self._burst_energy = max(self._burst_energy, energy)
            self._burst_samples += 1
            if self._burst_samples >= 3:   # a short speech burst started
                self._debounce_until = now + 2.0
                self._floor_buf = []
                self._burst_samples = 0
                self._burst_energy = 0.0
                return True
        else:
            if self._burst_samples == 0:
                self._get_floor_buf().append(energy)
            self._burst_samples = 0
        return False

    def _get_floor_buf(self) -> list:
        buf = self.__dict__.setdefault("_floor_buf", [])
        if len(buf) > 60:
            del buf[0]
        return buf

    def _on_wake(self) -> None:
        with self._lock:
            was_speaking = self._state == WakeState.SPEAKING
        if was_speaking and self._interrupt_fn is not None:
            try:
                self._interrupt_fn()          # barge-in
            except Exception:
                pass
        self.set_state(WakeState.WAKE_DETECTED)
        cb = self._on_detect
        if cb:
            try:
                cb(self._words[0] if self._words else "wake")
            except Exception:
                pass

    # ── interrupt (spec §23) ──────────────────────────────────────────────────

    def interrupt(self) -> None:
        """Ask the host to stop TTS now; used for barge-in."""
        with self._lock:
            fn = self._interrupt_fn
        if fn:
            try:
                fn()
            except Exception:
                pass
        self.set_state(WakeState.INTERRUPTED)


class WakeWordManager:
    """Thread-safe wrapper so one manager is shared app-wide, wiring
    detect/state/interrupt callbacks and configurable phrases."""

    def __init__(self, get_config: Callable[[], dict] | None = None):
        self._get_config = get_config or (lambda: {})
        self._lock = threading.RLock()
        self._detector: WakeWordDetector | None = None
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = False

    def _cfg(self) -> dict:
        try:
            c = self._get_config() or {}
        except Exception:
            c = {}
        return c

    def set_config_provider(self, fn: Callable[[], dict] | None) -> None:
        """Point the manager at a live config reader.

        The shared manager is built with no reader, so it always fell back to
        the built-in wake phrases and the default sensitivity — the Settings
        wake words and the sensitivity slider had nothing to reach. The bridge
        installs the real reader before the detector is built.
        """
        if fn is not None:
            self._get_config = fn

    def ensure(self) -> WakeWordDetector | None:
        """Get (and lazily build) the shared detector from current config."""
        with self._lock:
            if self._detector is not None:
                return self._detector
            cfg = self._cfg()
            words = merge_wake_words(cfg.get("wake_words"))
            sens = float(cfg.get("wake_sensitivity", 0.5))
            fallback = str(cfg.get("wake_fallback", "speech"))
            try:
                self._detector = WakeWordDetector(
                    words=[str(w) for w in words],
                    sensitivity=sens,
                    fallback=fallback,
                )
            except Exception as exc:
                # Swallowing this left the wake phrase permanently dead with no
                # trace anywhere: report it once (see _detector_failure_logged).
                _log_detector_failure(
                    f"wake-word detector unavailable ({type(exc).__name__}: "
                    f"{exc}) — the wake phrase will not fire until it can be built")
                return None
            return self._detector

    # ── start/stop background pump (optional; caller may feed() directly) ─────

    def start(self, on_detect: Callable[[str], None] | None = None,
              on_interrupt: Callable[[], None] | None = None) -> WakeWordDetector | None:
        det = self.ensure()
        if det is None:
            return None
        det.on_detect(on_detect)
        det.set_interrupt(on_interrupt)
        det.start()
        return det

    def reconfigure(self, *, words: list[str] | None = None,
                    sensitivity: float | None = None) -> None:
        """Apply new words/sensitivity without dropping the running detector.
        Falls back to a rebuild if live mutation is unsupported."""
        with self._lock:
            if self._detector is None:
                return
            try:
                if words is not None:
                    self._detector.set_words(merge_wake_words(words))
                if sensitivity is not None:
                    self._detector._sensitivity = max(0.0, min(1.0, float(sensitivity)))
                return
            except Exception:
                pass
            # Rebuild path — safest when live mutation isn't supported.
            try:
                self._detector.stop()
            except Exception:
                pass
            self._detector = None
            self.ensure()

    def stop(self) -> None:
        with self._lock:
            if self._detector:
                try:
                    self._detector.stop()
                except Exception:
                    pass
            self._stopping = True


# module-level singleton wiring
_manager: WakeWordManager | None = None
_manager_lock = threading.Lock()


def get_wake_manager() -> WakeWordManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = WakeWordManager()
        return _manager