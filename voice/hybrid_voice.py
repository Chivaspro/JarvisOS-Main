"""Hybrid Voice System coordinator.

Single owner for microphone capture, Gemini Live session, ONNX engine,
and audio playback.  Only one instance of each resource exists at any
time; components cooperate rather than duplicate each other.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Callable, Optional

try:
    import numpy as np
except Exception:
    np = None

try:
    import sounddevice as sd
except Exception:
    sd = None

from voice.tts import (
    OnnxTTSEngine,
    TTSPlayer,
    create_tts_player,
    load_voice_config,
    validate_voice_config,
    voice_status,
    _set_status,
)

# ── Voice state machine ────────────────────────────────────────────────────────
class VoiceState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ONNX_LOADING = "onnx_loading"
    ONNX_READY = "onnx_ready"
    VOICE_ERROR = "voice_error"


# Valid transitions from each state.
_TRANSITIONS = {
    VoiceState.IDLE: {VoiceState.LISTENING, VoiceState.CONNECTING, VoiceState.VOICE_ERROR},
    VoiceState.LISTENING: {VoiceState.THINKING, VoiceState.INTERRUPTED, VoiceState.IDLE, VoiceState.VOICE_ERROR},
    VoiceState.THINKING: {VoiceState.SPEAKING, VoiceState.INTERRUPTED, VoiceState.IDLE, VoiceState.VOICE_ERROR},
    VoiceState.SPEAKING: {VoiceState.LISTENING, VoiceState.INTERRUPTED, VoiceState.IDLE, VoiceState.VOICE_ERROR},
    VoiceState.INTERRUPTED: {VoiceState.LISTENING, VoiceState.IDLE, VoiceState.VOICE_ERROR},
    VoiceState.CONNECTING: {VoiceState.CONNECTED, VoiceState.RECONNECTING, VoiceState.VOICE_ERROR},
    VoiceState.CONNECTED: {VoiceState.LISTENING, VoiceState.RECONNECTING, VoiceState.VOICE_ERROR},
    VoiceState.RECONNECTING: {VoiceState.CONNECTING, VoiceState.CONNECTED, VoiceState.VOICE_ERROR},
    VoiceState.ONNX_LOADING: {VoiceState.ONNX_READY, VoiceState.VOICE_ERROR},
    VoiceState.ONNX_READY: {VoiceState.IDLE, VoiceState.VOICE_ERROR},
    VoiceState.VOICE_ERROR: {VoiceState.IDLE, VoiceState.CONNECTING},
}


class VoiceMode(StrEnum):
    """Which component renders JARVIS's audible reply.

    GEMINI (Native Audio) is the default pipeline: the realtime Live session
    speaks the reply itself — lowest latency, no local synthesis step.
    GEMINI_PIPER is kept for users who prefer the local jarvis-high ONNX
    voice. HYBRID is kept only so old configuration values still parse —
    it behaves as GEMINI_PIPER.
    """
    GEMINI = "gemini"
    GEMINI_PIPER = "gemini_piper"
    LOCAL = "local"
    HYBRID = "hybrid"          # legacy alias, resolved to GEMINI_PIPER


# The three real voice output modes, and the config key that records that the
# one-time gemini -> gemini_piper lift already happened (so a deliberate choice
# of Gemini Native Audio is never overridden again).
VOICE_OUTPUT_MODES = ("gemini", "gemini_piper", "local")
VOICE_PIPELINE_MIGRATED_KEY = "voice_pipeline_migrated"
# Gemini Native Audio is the default pipeline: the realtime Live voice speaks
# the reply itself, with no local synthesis hop in the path.
DEFAULT_VOICE_OUTPUT_MODE = "gemini"
# One-time lift for installs whose stored mode predates this default flip: an
# explicit legacy "gemini_piper" (or its aliases) becomes "gemini" once, so
# existing installs land on the realtime native-audio voice. After the marker
# is written the user's deliberate pipeline choice is respected.
PIPER_TO_NATIVE_LIFT_KEY = "voice_piper_to_native_lifted"
_LEGACY_MODE_ALIASES = {
    "hybrid": "gemini_piper",
    "piper": "gemini_piper",
    "gemini_piper": "gemini_piper",
    "onnx": "local",
    "native": "gemini",
    "gemini_native": "gemini",
    "native_audio": "gemini",
}


def normalize_voice_output_mode(value, migrated: bool = False,
                                piper_lifted: bool = False) -> str:
    """Resolve a stored voice output mode to ``gemini`` | ``gemini_piper`` | ``local``.

    Backward compatible by design: a legacy ``hybrid`` becomes ``gemini_piper``
    (it never actually rendered anything), a missing value becomes the new
    default. The default pipeline is Gemini Native Audio (``gemini``): the
    realtime Live voice speaks the reply itself.

    Migration is recorded once per install via two markers, so a deliberate
    choice is never overridden again:

    - ``migrated`` (the ``voice_pipeline_migrated`` marker) — the one-time
      historical gemini → gemini_piper lift;
    - ``piper_lifted`` (the ``voice_piper_to_native_lifted`` marker) — the
      one-time piper-pipeline → native-audio flip. While it is absent, a
      stored ``gemini_piper`` (or a legacy alias) becomes the new default;
      after the first deliberate save it is respected.
    """
    raw = str(value or "").strip().lower()
    if raw in _LEGACY_MODE_ALIASES:
        mode = _LEGACY_MODE_ALIASES[raw]
    elif raw in VOICE_OUTPUT_MODES:
        mode = raw
    else:
        mode = DEFAULT_VOICE_OUTPUT_MODE
    if mode == "gemini" and not migrated:
        return DEFAULT_VOICE_OUTPUT_MODE
    if mode == "gemini_piper" and not piper_lifted:
        return DEFAULT_VOICE_OUTPUT_MODE
    return mode


def resolve_voice_mode(cfg: dict | None = None):
    """Return ``(mode, needs_migration_marker)`` for a configuration mapping.

    ``needs_migration_marker`` is True for a configuration that still stores a
    piper-pipeline mode (or legacy ``hybrid``/``piper``) without the lift
    marker; the caller can tell the user and the next settings save records
    the marker. Nothing is written to disk here.
    """
    cfg = cfg or {}
    raw = str(cfg.get("voice_output_mode") or cfg.get("mode") or "").strip().lower()
    migrated = bool(cfg.get(VOICE_PIPELINE_MIGRATED_KEY))
    lifted = bool(cfg.get(PIPER_TO_NATIVE_LIFT_KEY))
    mode = normalize_voice_output_mode(raw, migrated=migrated, piper_lifted=lifted)
    return mode, (not lifted and raw in ("gemini_piper", "hybrid", "piper"))


def resolve_active_mode(cfg: dict | None = None) -> str:
    """The runtime renderer, derived from the ACTIVE PROVIDER (not the last
    touched settings field).

    Returns the same ``gemini | gemini_piper | local`` vocabulary as
    ``resolve_voice_mode`` so every downstream consumer (audio suppression,
    Piper service, prewarm/greeting routes) is unchanged:

    - gemini provider, renderer ``piper``  -> ``gemini_piper``
    - gemini provider, renderer ``native`` -> ``gemini``
    - any other provider                   -> ``local`` (that provider speaks)

    Falls back to the legacy mode resolution when no provider block can be
    derived (never raises).
    """
    try:
        from voice.providers_config import active_provider, providers_block
        provider = active_provider(cfg or {})
        if provider == "gemini":
            renderer = str(providers_block(cfg or {}).get("geminiRenderer") or "native")
            return "gemini" if renderer == "native" else "gemini_piper"
        if provider:
            return "local"
    except Exception:
        pass
    try:
        return resolve_voice_mode(cfg or {})[0]
    except Exception:
        return DEFAULT_VOICE_OUTPUT_MODE


# ── Configuration paths ────────────────────────────────────────────────────────
_BASE = Path(__file__).resolve().parent.parent
_API_CONFIG = _BASE / "config" / "api_keys.json"
_VOICE_CONFIG = _BASE / "config" / "voice_config.json"

_HYBRID_DEFAULTS = {
    "mode": "hybrid",
    "mic_enabled": True,
    "gemini_api_key": "",
    "gemini_model": "models/gemini-2.5-flash-native-audio-preview-12-2025",
    "onnx_voice_model": "",
    "onnx_voice_config": "",
    "onnx_voice_speaker": "default",
    "onnx_threads": 2,
    "gemini_threads": 1,
    "audio_threads": 1,
    "max_audio_queue": 4,
    "max_synth_queue": 4,
    "reconnect_backoff_max": 30.0,
    "reconnect_max_attempts": 0,
    "enable_diagnostics": True,
}


def _log(msg: str) -> None:
    try:
        print(f"[HybridVoice] {msg}")
    except Exception:
        pass


def _read_api_config() -> dict:
    try:
        if _API_CONFIG.exists():
            return json.loads(_API_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _read_voice_config_merged() -> dict:
    cfg = dict(_read_api_config())
    try:
        vcfg = load_voice_config()
        for k in ("engine", "voice", "model", "config", "speaker", "speed",
                  "pitch", "volume", "sample_rate", "output_format", "latency"):
            if k in vcfg:
                cfg.setdefault(k, vcfg[k])
    except Exception:
        pass
    return cfg


def _resolve_onnx_path(path: str) -> str:
    if not path:
        return path
    p = Path(path)
    if p.is_absolute() and p.exists():
        return str(p)
    cand = _BASE / path
    return str(cand) if cand.exists() else str(p)


def _validate_onnx_model(model_path: str, config_path: str):
    if not model_path:
        return False, "ONNX model path is empty"
    resolved = _resolve_onnx_path(model_path)
    if not Path(resolved).exists():
        return False, f"ONNX model not found: {resolved}"
    if config_path:
        cpath = _resolve_onnx_path(config_path)
        if not Path(cpath).exists():
            return False, f"ONNX voice config not found: {cpath}"
    return True, ""


def _validate_gemini_config(api_key: str, model: str):
    if not api_key:
        return False, "Gemini API key is missing"
    if not model:
        return False, "Gemini model name is empty"
    return True, ""


def _validate_voice_settings(cfg: dict):
    try:
        validate_voice_config(dict(cfg))
    except Exception as exc:
        return False, f"Voice config invalid: {exc}"
    mode = str(cfg.get("voice_output_mode") or cfg.get("mode") or "").strip().lower()
    # Legacy spellings stay valid; anything genuinely unknown is still rejected.
    _known = set(VOICE_OUTPUT_MODES) | set(_LEGACY_MODE_ALIASES)
    if mode and mode not in _known:
        return False, f"Unknown voice mode: {mode}"
    mode = normalize_voice_output_mode(
        mode or DEFAULT_VOICE_OUTPUT_MODE,
        migrated=bool(cfg.get(VOICE_PIPELINE_MIGRATED_KEY)),
    )
    if mode in {"gemini", "gemini_piper"}:
        ok, err = _validate_gemini_config(
            str(cfg.get("gemini_api_key") or _read_api_config().get("gemini_api_key", "")),
            str(cfg.get("gemini_model") or cfg.get("ai_model") or _HYBRID_DEFAULTS["gemini_model"]),
        )
        if not ok:
            return False, err
    if mode in {"gemini_piper", "local"}:
        model = str(cfg.get("onnx_voice_model") or cfg.get("onnx_voice_model") or "")
        config = str(cfg.get("onnx_voice_config") or cfg.get("onnx_voice_config") or "")
        if model:
            ok, err = _validate_onnx_model(model, config)
            if not ok:
                return False, err
    return True, ""


def save_voice_config(cfg: dict):
    try:
        existing = _read_api_config()
    except Exception:
        existing = {}
    merged = dict(existing)
    for k in ("voice_output_mode", "mode", VOICE_PIPELINE_MIGRATED_KEY,
              "onnx_execution_provider", "gemini_api_key", "gemini_model",
              "onnx_voice_model", "onnx_voice_config", "onnx_voice_speaker",
              "onnx_threads", "gemini_threads", "audio_threads",
              "max_audio_queue", "max_synth_queue", "reconnect_backoff_max"):
        if k in cfg:
            merged[k] = cfg[k]
    for k in ("tts_engine", "tts_voice", "tts_speed", "voice_speed",
              "voice_pitch", "voice_volume", "voice_mode", "voice_name"):
        if k in cfg:
            merged[k] = cfg[k]
        elif k in existing:
            merged[k] = existing[k]
    tmp = _API_CONFIG.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _API_CONFIG)
    except Exception as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False, f"Failed to save voice config: {exc}"
    return True, "Voice configuration saved."

# ── Shared microphone bus ──────────────────────────────────────────────────────
class MicBus:
    """Exactly one sounddevice.InputStream feeds every consumer.

    Consumers register callbacks that receive (level: float, samples: bytes).
    The bus pumps in its own audio thread and never blocks the UI.
    """

    def __init__(self, samplerate: int = 16000, channels: int = 1,
                 blocksize: int = 1024, max_queue: int = 4):
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self.max_queue = max_queue
        self._callbacks = []
        self._lock = threading.RLock()
        self._stream = None
        self._active = False
        self._last_cb = time.monotonic()
        self._level = 0.0
        self._device = None

    def register(self, cb):
        with self._lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def unregister(self, cb):
        with self._lock:
            try:
                self._callbacks.remove(cb)
            except ValueError:
                pass

    def _make_callback(self):
        def _cb(indata, frames, time_info, status):  # noqa: ARG001
            self._last_cb = time.monotonic()
            try:
                data = bytes(indata)
                level = _pcm_level(data)
            except Exception:
                data = b""
                level = 0.0
            self._level = level
            cbs = list(self._callbacks)
            for cb in cbs:
                try:
                    cb(level, data)
                except Exception:
                    pass
        return _cb

    def start(self, device=None) -> bool:
        if sd is None:
            _log("sounddevice unavailable — mic bus not started")
            return False
        with self._lock:
            if self._active:
                return True
            self._device = device
            try:
                self._stream = sd.InputStream(
                    samplerate=self.samplerate,
                    channels=self.channels,
                    dtype="int16",
                    blocksize=self.blocksize,
                    device=device,
                    callback=self._make_callback(),
                )
                self._stream.start()
                self._active = True
                _log("MicBus started")
                return True
            except Exception as exc:
                _log(f"MicBus start failed: {exc}")
                self._stream = None
                return False

    def stop(self) -> None:
        with self._lock:
            self._active = False
            try:
                if self._stream is not None:
                    self._stream.stop()
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
            _log("MicBus stopped")

    @property
    def active(self) -> bool:
        return self._active

    @property
    def level(self) -> float:
        return self._level

    def stalled(self) -> bool:
        return self._active and (time.monotonic() - self._last_cb) > 3.0


def _pcm_level(samples) -> float:
    if np is None or samples is None:
        return 0.0
    try:
        # sounddevice supplies an int16 ndarray, while the shared MicBus
        # forwards raw bytes.  Decode both forms explicitly so the visual
        # meter never becomes falsely quiet on a byte-buffer callback.
        if isinstance(samples, (bytes, bytearray, memoryview)):
            x = np.frombuffer(samples, dtype=np.int16).astype(np.float32)
        else:
            x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return 0.0
        # This is display-only gain. PCM sent to Gemini is never altered.
        if float(np.max(np.abs(x))) <= 2.0:
            x *= 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    # Normal speech from laptop/headset microphones is often below the old
    # ceiling. Use a lower knee and bounded visual gain without amplifying
    # the actual recognition signal or turning low-level noise into a spike.
    floor, full = 70.0, 1450.0
    if rms <= floor:
        return 0.0
    level = (rms - floor) / (full - floor)
    return min(1.0, max(0.0, level * 1.65) ** 0.86)

# ── ONNX engine cache ──────────────────────────────────────────────────────────
class OnnxCachedEngine:
    """Wraps OnnxTTSEngine so the model loads exactly once and is reused."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, model_path: str = "", config_path: str = "",
                 speaker: str = "default", sample_rate: int = 22050,
                 volume: float = 1.0, speed: float = 1.0):
        self.model_path = model_path
        self.config_path = config_path
        self.speaker = speaker
        self.sample_rate = sample_rate
        self.volume = volume
        self.speed = speed
        self._engine = None
        self._lock = threading.RLock()
        self._loaded = False
        self._load_ms = 0.0

    @classmethod
    def get(cls, **kwargs):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
            else:
                for k in ("speaker", "volume", "speed"):
                    if k in kwargs:
                        setattr(cls._instance, k, kwargs[k])
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.close()
                except Exception:
                    pass
            cls._instance = None

    def warmup_once(self) -> bool:
        with self._lock:
            if self._loaded:
                return True
            t0 = time.perf_counter()
            try:
                if not self.model_path:
                    _log("ONNX warmup skipped — no model path")
                    return False
                self._engine = OnnxTTSEngine(
                    model_path=self.model_path,
                    config_path=self.config_path,
                    speaker=self.speaker,
                    sample_rate=self.sample_rate,
                    volume=self.volume,
                    speed=self.speed,
                )
                self._engine.warmup()
                self._loaded = True
                self._load_ms = (time.perf_counter() - t0) * 1000
                _log(f"ONNX ready in {self._load_ms:.0f} ms")
                return True
            except Exception as exc:
                _log(f"ONNX warmup failed: {exc}")
                self._engine = None
                return False

    def speak(self, text: str) -> bool:
        if not self._loaded:
            if not self.warmup_once():
                return False
        assert self._engine is not None
        try:
            self._engine.speak(text)
            return True
        except Exception as exc:
            _log(f"ONNX speak failed: {exc}")
            return False

    def close(self) -> None:
        with self._lock:
            self._engine = None
            self._loaded = False


# ── Playback manager ───────────────────────────────────────────────────────────
class PlaybackManager:
    """Single playback path: stops current output before starting new output."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active = False
        self._stop_requested = False

    def play(self, audio_bytes: bytes, sample_rate: int = 24000) -> bool:
        if sd is None:
            return False
        with self._lock:
            self._stop_requested = False
            if self._active:
                try:
                    sd.stop()
                except Exception:
                    pass
            self._active = True
        try:
            import numpy as _np
            arr = _np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            sd.play(arr, sample_rate)
            sd.wait()
            return True
        except Exception as exc:
            _log(f"Playback failed: {exc}")
            return False
        finally:
            with self._lock:
                self._active = False

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            self._active = False
        try:
            sd.stop()
        except Exception:
            pass

    @property
    def active(self) -> bool:
        return self._active
# ── Piper speech service: one reusable worker + bounded queue ──────────────────

# Sentence boundaries the streamer will cut on, plus the length window that
# keeps chunks long enough for natural prosody and short enough to start
# speaking before a long reply finishes.
_SPEECH_TERMINATORS = ".!?…。！？\n"        # sentence ends: cut as soon as it is a sentence
_SPEECH_WEAK_TERMINATORS = ";:—"       # clause ends: only cut once the chunk is long enough
_SPEECH_MIN_CHUNK = 12
_SPEECH_MIN_WEAK_CHUNK = 24
_SPEECH_MAX_CHUNK = 220
_CLOSERS = "\"'”’)]}"

# The bundled JARVIS voice. Used when the Piper pipeline is active but the
# configuration has no ONNX voice selected yet.
_PIPER_DEFAULT_MODEL = "config/voices/jarvis-high.onnx"
_PIPER_DEFAULT_CONFIG = "config/voices/jarvis-high.json"


def split_speech_chunks(text: str, min_len: int = _SPEECH_MIN_CHUNK,
                        max_len: int = _SPEECH_MAX_CHUNK):
    """Split text into speakable chunks; returns ``(chunks, remainder)``.

    The remainder is what has not reached a natural boundary yet, so a caller
    streaming transcript fragments can keep feeding the next fragment without
    losing or repeating text. Sentence terminators cut immediately (unless a
    digit follows, so ``3.14`` is never split); clause terminators and a hard
    length cap keep a long unpunctuated span from delaying speech until the end
    of the turn.
    """
    text = str(text or "")
    chunks: list[str] = []
    buf = ""
    idx = 0
    total = len(text)
    while idx < total:
        ch = text[idx]
        buf += ch
        idx += 1
        nxt = text[idx] if idx < total else ""
        sentence_end = (ch in _SPEECH_TERMINATORS
                        and not nxt.isdigit()
                        and (not nxt or nxt.isspace() or nxt in _CLOSERS))
        if sentence_end and len(buf.strip()) >= min_len:
            chunks.append(buf.strip())
            buf = ""
        elif ch in _SPEECH_WEAK_TERMINATORS and len(buf.strip()) >= _SPEECH_MIN_WEAK_CHUNK:
            chunks.append(buf.strip())
            buf = ""
        elif len(buf) >= max_len:
            cut = max(buf.rfind(" "), buf.rfind(","))
            if cut < min_len:
                cut = len(buf)
            chunks.append(buf[:cut].strip())
            buf = buf[cut:]
    return [c for c in chunks if c], buf.strip()


class PiperSpeechService:
    """The single reusable Piper ONNX speech worker for the whole process.

    Owns one bounded queue and one long-lived worker thread. Reply text (fed as
    it streams in, or as a whole line) is buffered into sentence-sized chunks,
    synthesized by the cached Piper voice and played one chunk at a time.

    Interrupting bumps an epoch and sets a cancel event, so neither the
    sentence being spoken nor anything already queued survives — an interrupted
    reply can never resume later. Synthesis runs only on the worker thread, so
    the Qt thread and the Gemini event loop never block on it.
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, cfg: dict | None = None, max_queue: int = 12):
        self.max_queue = max(1, int(max_queue or 12))
        self._queue: "queue.Queue[tuple[int, str] | None]" = queue.Queue(maxsize=self.max_queue)
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._epoch = 0
        self._fed = ""          # transcript text already submitted this turn
        self._pending = ""      # text still waiting for a sentence boundary
        self._phase = "idle"    # idle | synthesizing | playing | error
        self._player = None
        self._cfg = dict(cfg or {})
        self._warm_running = False
        self._warm_ok = False
        self._last_reason = ""
        self._dropped = 0
        self._spoken = 0
        self._on_state: list = []

    # ── singleton ────────────────────────────────────────────────────────────
    @classmethod
    def get(cls, cfg: dict | None = None) -> "PiperSpeechService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(cfg)
            elif cfg is not None:
                cls._instance.configure(cfg)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:
            inst = cls._instance
            cls._instance = None
        if inst is not None:
            try:
                inst.shutdown()
            except Exception:
                pass

    # ── state ────────────────────────────────────────────────────────────────
    def on_state(self, cb) -> None:
        with self._lock:
            if cb not in self._on_state:
                self._on_state.append(cb)

    def _set_phase(self, phase: str) -> None:
        with self._lock:
            if phase == self._phase:
                return
            self._phase = phase
        for cb in list(self._on_state):
            try:
                cb(phase)
            except Exception:
                pass

    def phase(self) -> str:
        return self._phase

    @property
    def provider(self) -> str:
        try:
            return voice_status().provider
        except Exception:
            return ""

    @property
    def provider_reason(self) -> str:
        """Why a GPU request degraded to the CPU, when it did. Read from the
        engine so the interface can show it without parsing the log."""
        try:
            return voice_status().fallback
        except Exception:
            return ""

    def status(self) -> dict:
        cfg = self._cfg or {}
        return {
            "phase": self._phase,
            "speaking": self.is_speaking(),
            "queued": self._queue.qsize(),
            "max_queue": self.max_queue,
            "dropped": self._dropped,
            "spoken": self._spoken,
            "provider": self.provider,
            "provider_reason": self.provider_reason,
            "model": Path(str(cfg.get("onnx_voice_model") or "")).name,
            # A synthesis failure is more urgent than the standing provider note.
            "reason": self._last_reason or self.provider_reason,
            "warm": self._warm_ok,
        }

    def is_speaking(self) -> bool:
        with self._lock:
            return self._phase in ("synthesizing", "playing") or not self._queue.empty()

    @staticmethod
    def _log(msg: str) -> None:
        try:
            print(f"[PIPER] {msg}")
        except Exception:
            pass

    # ── configuration ────────────────────────────────────────────────────────
    def configure(self, cfg: dict | None = None) -> None:
        """Adopt new settings. The cached player is dropped so the next chunk is
        synthesized by the newly selected voice/provider; the model itself is
        then loaded once for the new arrangement (never per reply)."""
        with self._lock:
            self._cfg = dict(cfg if cfg is not None else _read_api_config())
            self._player = None
            self._warm_ok = False
            self._warm_running = False
        self._stop_playback()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def warmup_async(self) -> bool:
        """Preload the Piper voice off the caller's thread. Cheap no-op once the
        voice is loaded, so a reconnecting session does not reload the model."""
        with self._lock:
            if self._warm_ok or self._warm_running:
                return False
            self._warm_running = True
        threading.Thread(target=self._warm, name="jarvis-piper-warmup",
                         daemon=True).start()
        return True

    def warmup_now(self) -> bool:
        """Synchronous warm-up (TEST PIPER VOICE, tests)."""
        self._ensure_worker()
        player = self._ensure_player()
        if player is None:
            return False
        try:
            player.warmup()
        except Exception as exc:
            self._last_reason = str(exc)
            self._log(f"warm-up failed: {exc}")
            return False
        with self._lock:
            self._warm_ok = True
        return True

    def _warm(self) -> None:
        ok = False
        try:
            ok = self.warmup_now()
        except Exception as exc:                     # never escape the thread
            self._log(f"warm-up error: {exc}")
        finally:
            with self._lock:
                self._warm_running = False
        if ok:
            try:
                st = voice_status()
                self._log(f"warm-up complete (model={st.model or '?'}, "
                          f"provider={st.provider or '?'})")
            except Exception:
                pass

    def shutdown(self) -> None:
        """Stop speaking, drop queued speech and join the worker."""
        try:
            self.interrupt(log=False)
        except Exception:
            pass
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            try:
                worker.join(timeout=2.0)
            except Exception:
                pass
            self._log("worker stopped")

    # ── speech intake ────────────────────────────────────────────────────────
    def feed(self, text) -> int:
        """Submit a streaming transcript fragment. Returns the chunks queued.

        Only the *new* suffix of the fragment is used: Gemini's transcription
        arrives as deltas in some SDK versions and as a growing cumulative
        string in others, and this makes both safe (nothing is spoken twice).
        """
        delta = str(text or "")
        if not delta:
            return 0
        with self._lock:
            if self._fed and delta.startswith(self._fed):
                new = delta[len(self._fed):]
            else:
                new = delta
            self._fed += new
            self._pending += new
            chunks, self._pending = split_speech_chunks(self._pending)
        return self._enqueue_many(chunks)

    def speak(self, text) -> int:
        """Submit a complete line (whole replies, tests). Splits into chunks."""
        chunks, rest = split_speech_chunks(str(text or ""))
        if rest:
            chunks.append(rest)
        return self._enqueue_many(chunks)

    def flush(self) -> int:
        """Speak the buffered remainder of a finished turn and reset the turn."""
        with self._lock:
            tail, self._pending = self._pending.strip(), ""
            self._fed = ""
        return self._enqueue_many([tail] if tail else [])

    def _enqueue_many(self, chunks) -> int:
        if not chunks:
            return 0
        self._ensure_worker()
        queued = 0
        for chunk in chunks:
            with self._lock:
                epoch = self._epoch
            try:
                self._queue.put_nowait((epoch, chunk))
                queued += 1
            except queue.Full:
                # Bounded on purpose: never stall the Gemini receive loop and
                # never grow a backlog of stale speech.
                self._dropped += 1
                self._log(f"speech queue full ({self.max_queue}) - dropped "
                          f"{len(chunk)} chars")
        return queued

    # ── interruption ─────────────────────────────────────────────────────────
    def interrupt(self, log: bool = True) -> int:
        """Stop Piper now: cancel the current sentence, clear the queue, reset
        the turn buffer. Everything from the interrupted reply is discarded."""
        with self._lock:
            self._epoch += 1
            self._fed = ""
            self._pending = ""
            old_cancel = self._cancel
            self._cancel = threading.Event()
            cleared = 0
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    cleared += 1
                except queue.Empty:
                    break
        old_cancel.set()
        self._stop_playback()
        self._set_phase("idle")
        if log:
            self._log(f"interrupted - playback stopped, {cleared} queued chunk(s) cleared")
        return cleared

    # ── worker ───────────────────────────────────────────────────────────────
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run, name="jarvis-piper-tts",
                                            daemon=True)
            self._worker.start()

    def _ensure_player(self):
        """The cached TTS player (never built on the caller's thread)."""
        with self._lock:
            if self._player is not None:
                return self._player
        cfg = dict(self._cfg or _read_api_config())
        # The Piper pipeline ALWAYS renders with the local ONNX voice, whatever
        # the local-engine picker is set to. Otherwise "Gemini → Piper ONNX"
        # would quietly speak through edgetts/kokoro instead of jarvis-high.
        cfg["tts_engine"] = "onnx"
        cfg.setdefault("onnx_voice_model", "")
        if not str(cfg.get("onnx_voice_model") or "").strip():
            cfg["onnx_voice_model"] = _PIPER_DEFAULT_MODEL
        if not str(cfg.get("onnx_voice_config") or "").strip():
            cfg["onnx_voice_config"] = _PIPER_DEFAULT_CONFIG
        try:
            from voice.tts import create_tts_player
            player = create_tts_player(cfg)
        except Exception as exc:
            self._last_reason = str(exc)
            self._set_phase("error")
            self._log(f"engine unavailable: {exc}")
            return None
        with self._lock:
            self._player = player
        return player

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                epoch, text = item
                with self._lock:
                    stale = epoch != self._epoch
                    cancel = self._cancel
                if stale or cancel.is_set():
                    continue                                  # interrupted turn
                player = self._ensure_player()
                if player is None:
                    continue
                self._set_phase("synthesizing")
                try:
                    player.speak(text, cancel=cancel,
                                 on_audio=lambda: self._set_phase("playing"))
                    self._spoken += 1
                except Exception as exc:
                    self._last_reason = str(exc)
                    self._log(f"synthesis failed: {exc}")
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._set_phase("idle")

    @staticmethod
    def _stop_playback() -> None:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass


# ── Hybrid Voice System coordinator ────────────────────────────────────────────
class HybridVoiceSystem:
    """Single owner for microphone, Gemini session, ONNX engine, playback."""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, cfg: dict | None = None):
        self._cfg = dict(cfg or _read_api_config())
        self._mode = VoiceMode(
            normalize_voice_output_mode(self._cfg.get("voice_output_mode"),
                                        migrated=bool(self._cfg.get(VOICE_PIPELINE_MIGRATED_KEY)))
        )
        self._lock = threading.RLock()
        self._state = VoiceState.IDLE
        self._on_state: list = []
        self._mic = MicBus(
            max_queue=int(self._cfg.get("max_audio_queue", 4)),
        )
        self._playback = PlaybackManager()
        self._onnx = None
        self._gemini_session = None
        self._gemini_lock = threading.RLock()
        self._gemini_connected = False
        self._gemini_attempts = 0
        self._tasks: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._started = False
        self._on_audio_level: list = []
        self._on_interrupt: list = []
        self._on_state_callbacks: list = []

    # ── singleton ────────────────────────────────────────────────────────────
    @classmethod
    def get(cls, cfg: dict | None = None) -> "HybridVoiceSystem":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(cfg)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.shutdown()
                except Exception:
                    pass
            cls._instance = None
            OnnxCachedEngine.reset()

    # ── state machine ────────────────────────────────────────────────────────
    def set_state(self, state: VoiceState) -> None:
        with self._lock:
            if state == self._state:
                return
            allowed = _TRANSITIONS.get(self._state, set())
            if state not in allowed:
                _log(f"Reject transition {self._state} -> {state}")
                return
            self._state = state
        cbs = list(self._on_state_callbacks)
        for cb in cbs:
            try:
                cb(state)
            except Exception:
                pass

    def state(self) -> VoiceState:
        return self._state

    def on_state(self, cb) -> None:
        with self._lock:
            if cb not in self._on_state_callbacks:
                self._on_state_callbacks.append(cb)

    def register_audio_level(self, cb) -> None:
        with self._lock:
            if cb not in self._on_audio_level:
                self._on_audio_level.append(cb)

    def register_interrupt(self, cb) -> None:
        with self._lock:
            if cb not in self._on_interrupt:
                self._on_interrupt.append(cb)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self._started:
                return True
            self._started = True
        try:
            self._setup_onnx()
            if self._mode in (VoiceMode.HYBRID, VoiceMode.GEMINI):
                self.set_state(VoiceState.CONNECTING)
            else:
                self.set_state(VoiceState.ONNX_LOADING)
            self._setup_mic()
            return True
        except Exception as exc:
            _log(f"HybridVoiceSystem start failed: {exc}")
            self.set_state(VoiceState.VOICE_ERROR)
            return False

    def _setup_onnx(self) -> None:
        model = str(self._cfg.get("onnx_voice_model") or "")
        config = str(self._cfg.get("onnx_voice_config") or "")
        speaker = str(self._cfg.get("onnx_voice_speaker") or "default")
        if not model:
            return
        self.set_state(VoiceState.ONNX_LOADING)
        engine = OnnxCachedEngine.get(
            model_path=model,
            config_path=config,
            speaker=speaker,
        )
        self._onnx = engine
        ok = engine.warmup_once()
        if ok:
            self.set_state(VoiceState.ONNX_READY)

    def _setup_mic(self) -> None:
        if not bool(self._cfg.get("mic_enabled", True)):
            return
        self._mic.register(self._on_mic_frame)
        try:
            from voice import audio_devices as _ad
            name = _ad.get_input_device()
            dev = _ad.resolve(name, "input")
        except Exception:
            dev = None
        ok = self._mic.start(dev)
        if not ok:
            _log("MicBus failed to start — voice input disabled")

    def _on_mic_frame(self, level: float, data: bytes) -> None:
        cbs = list(self._on_audio_level)
        for cb in cbs:
            try:
                cb(level, data)
            except Exception:
                pass
        # Gemini send queue is owned by JarvisLive; we only forward level here.

    def interrupt(self, source: str = "manual") -> None:
        with self._lock:
            self._state = VoiceState.INTERRUPTED
        self._playback.stop()
        cbs = list(self._on_interrupt)
        for cb in cbs:
            try:
                cb()
            except Exception:
                pass

    def speak_local(self, text: str) -> None:
        if not text:
            return
        if self._onnx is None:
            return
        try:
            self.set_state(VoiceState.SPEAKING)
            self._onnx.speak(text)
        finally:
            self.set_state(VoiceState.LISTENING)

    def shutdown(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        try:
            self._playback.stop()
        except Exception:
            pass
        try:
            self._mic.stop()
        except Exception:
            pass
        try:
            self._mic.unregister(self._on_mic_frame)
        except Exception:
            pass
        if self._onnx is not None:
            try:
                self._onnx.close()
            except Exception:
                pass
        with self._lock:
            self._started = False
            self._gemini_connected = False
        _log("HybridVoiceSystem shutdown complete")
