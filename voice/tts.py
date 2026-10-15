"""
Text-to-Speech engines for MARK XL.

EdgeTTS     – free Microsoft TTS (internet required, no API key)
Kokoro      – fully offline neural TTS (~330 MB model)
ElevenLabs  – cloud API (API key required, best quality)
"""
from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd



# USE_TF=0 stops transformers from importing TensorFlow (saves 4-8 s startup).
# Do NOT set USE_TORCH or USE_JAX explicitly — forcing those values breaks
# transformers' lazy-loader on certain versions, causing AutoModel and other
# classes to vanish from the public namespace.  Auto-detection is reliable.
os.environ.setdefault("USE_TF",                 "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


# ---------------------------------------------------------------------------
# Audio playback helpers
# ---------------------------------------------------------------------------

def _to_numpy(samples) -> np.ndarray:
    """Convert samples to float32 numpy array.

    Handles both numpy arrays and PyTorch tensors (Kokoro >= 0.9).

    PyTorch built against numpy 1.x raises RuntimeError('Numpy is not available')
    when numpy 2.x is installed.  The .tolist() fallback always works regardless
    of PyTorch / numpy version pairing.
    """
    if hasattr(samples, "detach"):                  # PyTorch tensor
        t = samples.detach().cpu().float()
        try:
            return t.numpy()                        # fast path (compatible versions)
        except RuntimeError:
            # PyTorch/numpy version mismatch — convert via Python list (always safe)
            return np.asarray(t.tolist(), dtype=np.float32)
    return np.asarray(samples, dtype=np.float32)


def _compress_silence(
    arr: np.ndarray,
    sample_rate: int    = 24_000,
    max_silence_ms: int = 500,    # cap punctuation pauses — keeps natural rhythm
    threshold: float    = 0.003,  # RMS below this = silence; lower = less clipping
) -> np.ndarray:
    """
    Shorten Kokoro's very long punctuation pauses (1-2 s → ≤500 ms).
    Conservative settings preserve natural prosody; only trims extreme pauses.
    """
    max_samp  = int(max_silence_ms * sample_rate / 1000)
    frame_len = 240                   # ~10 ms at 24 kHz
    out: list[np.ndarray] = []
    silent_acc = 0

    for i in range(0, len(arr), frame_len):
        chunk = arr[i : i + frame_len]
        if np.sqrt(np.mean(chunk ** 2) + 1e-12) < threshold:
            silent_acc += len(chunk)
            if silent_acc <= max_samp:
                out.append(chunk)
        else:
            silent_acc = 0
            out.append(chunk)

    return np.concatenate(out) if out else arr


def _play_np(samples, sample_rate: int) -> None:
    """Play float32 mono (or stereo) audio via sounddevice.
    Accepts numpy arrays or PyTorch tensors.
    """
    arr = _to_numpy(samples)
    with _reactive_envelope(arr, sample_rate):
        sd.play(arr, sample_rate)
        sd.wait()


def _play_audio_bytes(audio_bytes: bytes) -> None:
    """Decode MP3/WAV/OGG bytes and play via sounddevice (uses miniaudio)."""
    try:
        import miniaudio
    except ImportError:
        raise RuntimeError("Voice playback needs the 'miniaudio' package. Run: pip install miniaudio")
    decoded = miniaudio.decode(
        audio_bytes,
        output_format=miniaudio.SampleFormat.FLOAT32,
        nchannels=1,
    )
    samples = np.array(decoded.samples, dtype=np.float32)
    with _reactive_envelope(samples, decoded.sample_rate):
        sd.play(samples, decoded.sample_rate)
        sd.wait()


# ---------------------------------------------------------------------------
# Streaming playback (cloud voices: Fish Audio, ElevenLabs)
# ---------------------------------------------------------------------------
# Cloud TTS used to fetch the WHOLE reply as one file before a single sample was
# heard. With a 2-4 sentence answer that is several seconds of silence — exactly
# the "Fish Audio / ElevenLabs is slow" complaint. Nothing can make the far end
# answer instantly; what CAN change is when playback starts:
#
#   BEFORE   [request ][synthesise everything][download][play all]
#   AFTER    [request][first sentence][play ▸▸▸]
#                                     [next sentence synthesises meanwhile]
#
# So: the reply is split at sentence boundaries, each sentence is requested
# separately (streaming where the provider supports it), and a producer thread
# opens the NEXT request while the current sentence is still playing. Audio
# begins at the first sentence's time-to-first-byte instead of the whole reply's
# render time, and an interruption (``cancel``) can now cut a cloud voice short
# mid-reply — it used to be ignored entirely.

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _accepts_stream_kwargs(engine) -> bool:
    """Does this engine's speak() take ``cancel`` / ``on_audio``?

    Checked rather than assumed: a subclass or replacement engine with the old
    one-argument signature must keep working (it is simply played the old way)
    instead of raising "unexpected keyword argument".
    """
    try:
        import inspect
        params = inspect.signature(engine.speak).parameters
    except Exception:
        return False
    return "cancel" in params and "on_audio" in params


def _streaming_enabled() -> bool:
    """Is sentence-streamed cloud TTS on? (Settings ▸ Automation → Streaming TTS.)

    Defaults to True when the setting layer is unavailable: a missing config is
    not a reason to fall back to the slow path.
    """
    try:
        from config.settings import get_feature
        return bool(get_feature("tts_streaming", True))
    except Exception:
        return True


def split_sentences(text: str, max_chars: int = 240) -> list:
    """Split a reply into speakable sentences.

    A sentence longer than ``max_chars`` is broken at a comma/space so one huge
    run-on line still starts playing quickly. Whitespace-only input returns [].
    """
    out: list = []
    for raw in _SENTENCE_SPLIT.split(str(text or "").strip()):
        piece = raw.strip()
        if not piece:
            continue
        if len(piece) <= max_chars:
            out.append(piece)
            continue
        buf = ""
        for part in re.split(r"(?<=[,;:])\s+", piece):
            if buf and len(buf) + 1 + len(part) > max_chars:
                out.append(buf.strip())
                buf = ""
            buf = f"{buf} {part}".strip()
            while len(buf) > max_chars:
                cut = buf.rfind(" ", 0, max_chars) or max_chars
                out.append(buf[:cut].strip())
                buf = buf[cut:].strip()
        if buf:
            out.append(buf.strip())
    return [s for s in out if s]


def _push_reactive(pcm_bytes: bytes, sample_rate: int = 24000) -> None:
    """Feed the REAL streamed audio to the logo-reactive controller.

    The streaming path cannot use ``_ReactiveEnvelope`` (it plays through its
    own long-lived stream rather than one ``sd.play`` per buffer), so the level
    is derived here from the exact bytes being written to the speaker.
    """
    try:
        from core import audio_reactive as _ar
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        if arr.size == 0:
            return
        rms = float(np.sqrt(np.mean((arr.astype(np.float32) / 32768.0) ** 2)))
        _ar.push_output_level(min(1.0, rms * 2.2))
    except Exception:
        pass


def _open_pcm_stream(sample_rate: int):
    st = sd.RawOutputStream(samplerate=int(sample_rate), channels=1,
                            dtype="int16", blocksize=1024)
    st.start()
    return st


def _play_pcm_frames(frames, cancel=None, on_audio=None) -> bool:
    """Play ``(pcm_bytes, sample_rate)`` frames as they arrive.

    One output stream is kept open across the whole reply (re-opened only if a
    provider switches sample rate), so there is no device gap between
    sentences. Returns True if any audio was written. Never raises: a stream
    failure must not kill the reply, and the caller's fallback path stays
    usable.
    """
    stream = None
    rate = 0
    played = False
    started = False
    try:
        for chunk, sr in frames:
            if cancel is not None and cancel.is_set():
                break
            if not chunk:
                continue
            sr = int(sr or 24000)
            if stream is None or sr != rate:
                try:
                    if stream is not None:
                        stream.stop()
                        stream.close()
                except Exception:
                    pass
                stream = _open_pcm_stream(sr)
                rate = sr
            if not started:
                started = True
                if on_audio is not None:
                    try:
                        on_audio()
                    except Exception:
                        pass
            stream.write(bytes(chunk))
            played = True
            _push_reactive(bytes(chunk), sr)
    except Exception as exc:
        print(f"[TTS] stream playback error: {exc}")
    finally:
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
    return played


def _play_sentence_stream(text: str, make_frames, cancel=None,
                          on_audio=None) -> bool:
    """Pipeline sentence synthesis against playback.

    ``make_frames(sentence)`` returns an iterator of ``(pcm_bytes, sample_rate)``
    and is expected to open its request lazily, so the FIRST ``next()`` is what
    pays the network latency. A producer thread takes one sentence ahead: by the
    time the current sentence finishes the next one's audio is already arriving.
    """
    sentences = split_sentences(text)
    if not sentences:
        return False
    q: "_queue.Queue" = _queue.Queue(maxsize=2)
    errors: list = []

    def _producer():
        try:
            for sentence in sentences:
                if cancel is not None and cancel.is_set():
                    break
                it = iter(make_frames(sentence))
                first = next(it, None)      # the request happens here
                if first is None:
                    continue
                q.put((first, it))
        except Exception as exc:
            errors.append(exc)
        finally:
            q.put(None)

    threading.Thread(target=_producer, name="jarvis-tts-stream",
                     daemon=True).start()

    def _frames():
        while True:
            item = q.get()
            if item is None:
                break
            first, rest = item
            yield first
            for frame in rest:
                yield frame

    played = _play_pcm_frames(_frames(), cancel=cancel, on_audio=on_audio)
    if errors and not played:
        raise errors[0]
    if errors:
        print(f"[TTS] stream error (partial audio played): {errors[0]}")
    return played


def _strip_wav_header(head: bytes, stream) -> tuple:
    """Recover ``(sample_rate, remaining_pcm)`` from a streaming WAV response.

    A streamed WAV has no usable length fields, so the header is located by its
    ``data`` marker (robust against extra chunks) and everything after it is raw
    PCM. Falls back to 24 kHz when the header cannot be read.
    """
    rate = 24000
    try:
        i = head.find(b"fmt ")
        if i >= 0 and len(head) >= i + 16:
            rate = int.from_bytes(head[i + 12:i + 14], "little") or rate
    except Exception:
        pass
    j = head.find(b"data")
    if j >= 0 and len(head) >= j + 8:
        return rate, head[j + 8:]
    # No data chunk seen yet: treat the whole head as PCM after the 44-byte
    # canonical header, which is what WaveWriter emits.
    return rate, head[44:]


class _ReactiveEnvelope:
    """Drive the shared logo-reactive controller from the REAL audio being played.

    A short-lived daemon thread walks the actual short-time RMS of the exact
    samples handed to ``sd.play`` in step with the playback duration and feeds
    each value to :mod:`core.audio_reactive`. When playback ends the thread stops
    and the controller's own release brings the logos smoothly back to normal
    size. This is genuinely the voice signal — never a sine, random value, or
    timer-only fake. Fully guarded: any failure is ignored and never affects
    playback (the local TTS path is otherwise silent to the reactive system).
    """
    _FRAME_S = 0.04   # 40 ms steps ≈ 25 fps of real amplitude

    def __enter__(self):
        self._stop = threading.Event()
        self._thread = None
        try:
            from core import audio_reactive as _ar
            self._ar = _ar
        except Exception:
            self._ar = None
        return self

    def _start(self, samples, sample_rate):
        if self._ar is None or samples is None or sample_rate <= 0:
            return
        try:
            arr = np.asarray(samples, dtype=np.float32).reshape(-1)
        except Exception:
            return
        if arr.size == 0:
            return
        frame = max(1, int(self._FRAME_S * sample_rate))
        peak = float(np.max(np.abs(arr))) or 1.0
        self._thread = threading.Thread(
            target=self._run, args=(arr, frame, peak), daemon=True,
            name="jarvis-reactive")
        self._thread.start()

    def _run(self, arr, frame, peak):
        import time as _t
        norm = 32768.0 if np.abs(arr).max() > 4.0 else 1.0  # int16 vs float range
        i = 0
        n = arr.size
        step = self._FRAME_S
        while i < n and not self._stop.is_set():
            chunk = arr[i:i + frame]
            i += frame
            if chunk.size == 0:
                break
            try:
                rms = float(np.sqrt(np.mean((chunk / norm) ** 2)))
            except Exception:
                rms = 0.0
            self._ar.push_output_level(min(1.0, rms * 2.2))
            _t.sleep(step)
        # Release: signal silence so the logos settle (controller still smooths).
        try:
            self._ar.push_output_level(0.0)
        except Exception:
            pass

    def __exit__(self, *exc):
        self._stop.set()
        th = self._thread
        if th is not None:
            th.join(timeout=0.2)
        return False


def _reactive_envelope(samples, sample_rate: int) -> "_ReactiveEnvelope":
    env = _ReactiveEnvelope()
    env.__enter__()
    env._start(samples, sample_rate)
    return env


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

class SapiTTSEngine:
    """Windows SAPI5 offline voice (pyttsx3). Instant init, zero network,
    always present on Windows — the fastest possible engine and a safe
    fallback when network engines (EdgeTTS/ElevenLabs/Fish) are unreachable.
    Voice selection uses the Settings "tts_voice" field (matched loosely
    against installed SAPI voices, e.g. "Zira", "David")."""

    def __init__(self, voice: str = "", rate: int = 180, volume: float = 1.0) -> None:
        self.voice = (voice or "").strip()
        self.rate = max(80, min(400, int(rate or 180)))
        self.volume = max(0.1, min(1.0, float(volume or 1.0)))
        self._tts = None                     # cached pyttsx3 engine
        self._lock = threading.Lock()

    def warmup(self) -> None:
        """Build the SAPI engine once (cheap: ~50 ms)."""
        with self._lock:
            if self._tts is None:
                import pyttsx3
                self._tts = pyttsx3.init()
                try:
                    self._tts.setProperty("rate", self.rate)
                    self._tts.setProperty("volume", self.volume)
                except Exception:
                    pass

    def _match_voice(self) -> None:
        if not self.voice:
            return
        try:
            needle = self.voice.lower()
            for v in self._tts.getProperty("voices"):  # type: ignore[attr-defined]
                hay = f"{getattr(v, 'id', '')} {getattr(v, 'name', '')}".lower()
                if needle in hay or any(p in hay for p in needle.split()):
                    self._tts.setProperty("voice", v.id)
                    return
        except Exception:
            pass

    def speak(self, text: str) -> None:
        self.warmup()
        _set_status(state="PLAYING")
        with self._lock:
            try:
                self._match_voice()
                self._tts.say(text)
                self._tts.runAndWait()
            finally:
                _set_status(state="READY")


class EdgeTTSEngine:
    """Microsoft EdgeTTS – free, requires internet."""

    def __init__(
        self,
        voice: str = "en-GB-RyanNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
    ):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch

    def speak(self, text: str) -> None:
        loop = asyncio.new_event_loop()
        try:
            audio_bytes = loop.run_until_complete(self._synth(text))
        finally:
            loop.close()
        if audio_bytes:
            _play_audio_bytes(audio_bytes)

    async def _synth(self, text: str) -> bytes:
        import edge_tts
        comm = edge_tts.Communicate(
            text,
            self.voice,
            rate=self.rate,
            pitch=self.pitch,
        )
        buf  = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)


# ---------------------------------------------------------------------------
# Kokoro import helper — auto-upgrades on version-mismatch errors
# ---------------------------------------------------------------------------

# Errors that indicate the installed kokoro uses old transformers classes
# (AlbertModel, AutoModel) that are no longer exported at the top level.
_KOKORO_COMPAT_ERRORS = ("AlbertModel", "AutoModel", "cannot import name")


def _import_kokoro_pipeline():
    """Import KPipeline, auto-upgrading kokoro if a version mismatch is found.

    Old kokoro (<0.9) imports AlbertModel / AutoModel from transformers.
    Newer transformers versions no longer export these at the top level,
    causing an ImportError.  kokoro>=0.9 removed these dependencies.

    When the error is detected we:
      1. Upgrade kokoro to >=0.9 via pip (silent, background)
      2. Flush stale kokoro entries from sys.modules
      3. Re-import — this time it should succeed
    """
    import sys

    def _try_import():
        from kokoro import KPipeline  # noqa: PLC0415
        return KPipeline

    try:
        return _try_import()
    except Exception as first_err:
        err_msg = str(first_err)
        if not any(marker in err_msg for marker in _KOKORO_COMPAT_ERRORS):
            # Unrelated error (kokoro not installed, etc.)
            raise RuntimeError(
                f"Kokoro import failed: {first_err}\n"
                "Run: pip install kokoro>=0.9 soundfile"
            ) from first_err

        # ── Version mismatch: upgrade kokoro silently and retry ──────────
        print("[TTS] Kokoro/transformers version mismatch detected — upgrading kokoro…")
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "kokoro>=0.9",
             "--upgrade", "--quiet", "--disable-pip-version-check"],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"Kokoro auto-upgrade failed: {stderr[:200]}\n"
                "Run manually: pip install kokoro>=0.9 soundfile"
            ) from first_err

        # Flush any stale kokoro submodules from the import cache
        stale = [k for k in sys.modules if k == "kokoro" or k.startswith("kokoro.")]
        for key in stale:
            del sys.modules[key]

        print("[TTS] Kokoro upgraded — retrying import…")
        try:
            return _try_import()
        except Exception as retry_err:
            raise RuntimeError(
                f"Kokoro still broken after upgrade: {retry_err}\n"
                "Run manually: pip install --upgrade kokoro transformers"
            ) from retry_err


# Kokoro voice prefix → KPipeline lang_code mapping
_KOKORO_LANG_CODES = {
    "a": "a",   # American English  (af_*, am_*)
    "b": "b",   # British English   (bf_*, bm_*)
    "j": "j",   # Japanese          (jf_*, jm_*)
    "z": "z",   # Mandarin Chinese  (zf_*, zm_*)
    "s": "s",   # Spanish           (sf_*, sm_*)
    "f": "f",   # French            (ff_*, fm_*)
    "h": "h",   # Hindi             (hf_*, hm_*)
    "i": "i",   # Italian           (if_*, im_*)
    "p": "p",   # Brazilian Portuguese
    "r": "r",   # Russian           (rf_*, rm_*)
    "e": "e",   # German            (ef_*, em_*)
}


class KokoroTTSEngine:
    """Fully offline Kokoro neural TTS.

    Model (~330 MB) is downloaded from HuggingFace on first use,
    then cached locally — subsequent starts load from disk.

    Warmup strategy: _init() runs synchronously in the background
    _do_tts() thread (not the UI thread).  After the pipeline loads,
    a dummy inference compiles the PyTorch JIT graph immediately so
    the first real speak() call has zero compilation overhead.
    """

    def __init__(self, voice: str = "af_heart", speed: float = 1.0):
        self.voice     = voice
        self.speed     = speed
        self._pipeline = None
        self._lock     = threading.Lock()
        self._init()   # blocking, but called from background thread

    @property
    def _lang_code(self) -> str:
        prefix = self.voice[0].lower() if self.voice else "a"
        return _KOKORO_LANG_CODES.get(prefix, "a")

    def _init(self) -> None:
        if self._pipeline is not None:
            return

        lang = self._lang_code

        # Prefer GPU — Kokoro on CUDA is ~10x faster than CPU.
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cpu":
                import os as _os
                n_threads = max(1, min(4, (_os.cpu_count() or 4) // 2))
                try:
                    torch.set_num_threads(n_threads)
                    torch.set_num_interop_threads(2)
                except RuntimeError:
                    pass
                print(
                    f"[TTS] Kokoro on CPU — for faster speech install CUDA PyTorch:\n"
                    "      pip install torch --index-url https://download.pytorch.org/whl/cu118"
                )
        except Exception:
            device = "cpu"

        print(f"[TTS] Kokoro — loading (lang='{lang}', device='{device}')…")

        KPipeline = _import_kokoro_pipeline()

        def _create_pipeline():
            try:
                return KPipeline(lang_code=lang, device=device)
            except TypeError:
                return KPipeline(lang_code=lang)   # older build — no device param

        try:
            self._pipeline = _create_pipeline()
        except Exception as _first_err:
            # Offline flag set but model not cached yet → clear flags and download once.
            # Keywords cover multiple huggingface_hub error message variants across versions.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print("[TTS] Kokoro model not in local cache — downloading (one-time, internet required)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._pipeline = _create_pipeline()
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Kokoro model download failed.\n"
                        f"Internet access is required the first time to download the voice model (~330 MB).\n"
                        f"After the first download it runs fully offline.\n"
                        f"Tip: Switch to EdgeTTS (free, no download) in the Configure panel if offline.\n"
                        f"Details: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        print("[TTS] Kokoro compiling (first-time only)…")
        # Warmup: compiles PyTorch JIT graph so first real speak() call is instant.
        try:
            for _ in self._pipeline("hello", voice=self.voice, speed=self.speed):
                pass
            print("[TTS] Kokoro ready.")
        except Exception as e:
            print(f"[TTS] Kokoro warmup warning: {e}")

    def speak(self, text: str) -> None:
        with self._lock:
            if self._pipeline is None:
                self._init()

        # ── Concurrent synthesise + playback ────────────────────────────────
        # Kokoro generates audio chunks lazily.  Without threading, we:
        #   synthesise chunk N → play N → synthesise N+1 → play N+1 …
        # With a producer/consumer pair, chunk N+1 synthesises WHILE chunk N
        # plays, cutting perceived latency by the playback duration of all but
        # the last chunk (typically 1-3 s on multi-sentence responses).
        audio_q: "_queue.Queue[np.ndarray | None]" = _queue.Queue(maxsize=4)
        synth_error: list[Exception] = []

        def _synth():
            try:
                for _, _, audio in self._pipeline(text, voice=self.voice, speed=self.speed):
                    if audio is not None:
                        arr = _to_numpy(audio)
                        arr = _compress_silence(arr)
                        if arr.size > 0:
                            audio_q.put(arr)          # blocks if player is slow (backpressure)
            except Exception as exc:
                synth_error.append(exc)
            finally:
                audio_q.put(None)                     # sentinel → player exits

        synth_thread = threading.Thread(target=_synth, daemon=True)
        synth_thread.start()

        # Player runs in this thread so sd.wait() doesn't block the synth thread.
        while True:
            arr = audio_q.get()
            if arr is None:
                break
            _play_np(arr, 24000)

        synth_thread.join()

        if synth_error:
            raise synth_error[0]


ELEVENLABS_DEFAULT_VOICE = "pNInz6obpgDQGcFmaJgB"  # Adam
# eleven_flash_v2_5 is the low-latency model: ElevenLabs quotes ~75 ms model
# latency against several hundred for multilingual_v2, which is the difference
# the user hears as "ElevenLabs is slow". Only the DEFAULT changes — an explicit
# model in Settings is honoured exactly as stored.
ELEVENLABS_DEFAULT_MODEL = "eleven_flash_v2_5"
# Raw 24 kHz PCM: no container, so the stream can go straight to the speaker
# without waiting for a whole MP3 to arrive before it can be decoded.
ELEVENLABS_PCM_FORMAT = "pcm_24000"


class ElevenLabsTTSEngine:
    """ElevenLabs cloud TTS – API key + voice ID + model ID.

    Voice IDs are 20-char alphanumeric strings (e.g. Adam's
    pNInz6obpgDQGcFmaJgB). Edge-style names like 'en-US-GuyNeural' are
    rejected early with a clear error instead of a confusing API 404.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str = ELEVENLABS_DEFAULT_VOICE,
        model_id: str = ELEVENLABS_DEFAULT_MODEL,
    ):
        key = (api_key or os.getenv("ELEVENLABS_API_KEY", "")).strip()
        if not key:
            raise ValueError("ElevenLabs API key is missing — add it in Control Center → Voice.")
        vid = (voice_id or "").strip() or ELEVENLABS_DEFAULT_VOICE
        # Real ElevenLabs voice IDs are 20 alphanumeric chars, never contain
        # dashes/spaces/underscores or the word 'Neural'.
        if "neural" in vid.lower() or "-" in vid or "_" in vid or " " in vid or len(vid) > 32:
            raise ValueError(
                f"'{vid}' looks like an EdgeTTS voice, not an ElevenLabs voice ID. "
                f"Use a 20-character ID (default Adam: {ELEVENLABS_DEFAULT_VOICE})."
            )
        self.api_key  = key
        self.voice_id = vid
        self.model_id = (model_id or "").strip() or ELEVENLABS_DEFAULT_MODEL

    def _payload(self, text: str) -> dict:
        return {
            "text":     text,
            "model_id": self.model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

    def _headers(self) -> dict:
        return {
            "xi-api-key":   self.api_key,
            "Content-Type": "application/json",
        }

    def _frame_iterator(self, sentence: str):
        """Stream ONE sentence as ``(pcm_bytes, 24000)`` frames.

        Uses the documented /stream endpoint with raw PCM output and
        ``optimize_streaming_latency=3``, so the first samples arrive while the
        rest of the sentence is still being rendered. Requesting this lazily
        (the first ``next()`` opens the connection) is what lets the sentence
        pipeline overlap network latency with playback.
        """
        import requests
        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream",
            params={"output_format": ELEVENLABS_PCM_FORMAT,
                    "optimize_streaming_latency": "3"},
            json=self._payload(sentence), headers=self._headers(),
            timeout=30, stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"ElevenLabs stream HTTP {resp.status_code} "
                f"{str(resp.text)[:160]}"
            )
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    yield (chunk, 24000)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _fetch_whole(self, text: str) -> bytes:
        """The previous non-streaming request — the honest fallback path."""
        import requests
        try:
            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                json=self._payload(text), headers=self._headers(), timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            detail = ""
            try:
                detail = f" — {resp.text[:160]}"
            except Exception:
                pass
            raise RuntimeError(
                f"ElevenLabs request failed ({exc}){detail} "
                f"[voice={self.voice_id} model={self.model_id}]"
            ) from exc
        return resp.content

    def speak(self, text: str, cancel=None, on_audio=None) -> None:
        """Speak ``text`` sentence by sentence, streaming each one.

        Falls back to the previous whole-reply fetch on ANY streaming failure,
        so a changed API contract degrades to the old (slower) behaviour rather
        than to silence.
        """
        text = str(text or "").strip()
        if not text:
            return
        if _streaming_enabled():
            try:
                if _play_sentence_stream(text, self._frame_iterator,
                                         cancel=cancel, on_audio=on_audio):
                    return
            except Exception as exc:
                print(f"[TTS] ElevenLabs streaming unavailable: {exc} — "
                      f"falling back to whole-reply fetch")
        if cancel is not None and cancel.is_set():
            return
        if on_audio is not None:
            try:
                on_audio()
            except Exception:
                pass
        _play_audio_bytes(self._fetch_whole(text))


def _looks_like_voice_id(value: str) -> bool:
    v = (value or "").strip()
    return len(v) >= 20 and all(ch in "0123456789abcdefABCDEF" for ch in v)


class FishAudioTTSEngine:
    """Fish Audio cloud TTS via the official ``fish-audio-sdk``.

    ``model`` selects the backend (e.g. "s1", "s2.1-pro") and
    ``reference_id`` selects the voice. Because older setups stored the
    voice ID in the Model ID field, an ID-shaped model value is treated
    as the voice and the backend falls back to "s2-pro".
    """

    def __init__(
        self,
        api_key: str,
        model_id: str = "s2-pro",
        voice_id: str = "",
        endpoint: str = "https://api.fish.audio/v1/tts",
        audio_format: str = "mp3",
        latency: str = "normal",
    ):
        api_key = (api_key or os.getenv("FISH_API_KEY", os.getenv("FISH_AUDIO_API_KEY", ""))).strip()
        if not api_key:
            raise ValueError("Fish Audio API key is missing — add it in Control Center → Voice or FISH_API_KEY.")
        endpoint = (endpoint or "").strip().rstrip("/") or "https://api.fish.audio/v1/tts"
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("Fish Audio endpoint must be an HTTP(S) URL")
        model = (model_id or "").strip()
        ref = (voice_id or "").strip()
        if not ref and _looks_like_voice_id(model):
            ref, model = model, "s2-pro"
        if not ref:
            raise ValueError(
                "Fish Audio needs a voice: fill Voice ID in Control Center → Voice."
            )
        self.api_key = api_key
        self.model = model or "s2-pro"
        self.reference_id = ref
        self.endpoint = endpoint
        self.audio_format = audio_format if audio_format in {"mp3", "wav", "opus"} else "mp3"
        # API accepts "normal" | "balanced" ("low" is mapped to "balanced").
        self.latency = {"low": "balanced"}.get(latency, latency)
        if self.latency not in {"normal", "balanced"}:
            self.latency = "normal"

    def _convert_sdk(self, text: str) -> bytes:
        from fishaudio import FishAudio

        client = FishAudio(api_key=self.api_key)
        out = client.tts.convert(
            text=text,
            model=self.model,
            reference_id=self.reference_id,
        )
        if isinstance(out, (bytes, bytearray)):
            return bytes(out)
        chunks = []
        for chunk in out:
            if isinstance(chunk, (bytes, bytearray)):
                chunks.append(bytes(chunk))
        if not chunks:
            raise RuntimeError("Fish Audio returned no audio.")
        return b"".join(chunks)

    def _convert_http(self, text: str) -> bytes:
        import requests

        payload = {
            "text": text,
            "model": self.model,
            "reference_id": self.reference_id,
            "format": self.audio_format,
            "latency": self.latency,
        }
        try:
            response = requests.post(
                self.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=45,
            )
            response.raise_for_status()
        except Exception as exc:
            detail = ""
            try:
                detail = f" — {response.text[:160]}"
            except Exception:
                pass
            raise RuntimeError(f"Fish Audio request failed ({exc}){detail}") from exc
        return response.content

    def _frame_iterator(self, sentence: str):
        """Stream ONE sentence as ``(pcm_bytes, sample_rate)`` frames.

        Streamed audio is requested as WAV: it is the one Fish format whose
        sample rate the payload itself declares, so the raw PCM after the header
        can be written straight to the speaker. The header is read as it
        arrives and the sample rate comes from its ``fmt `` chunk — nothing is
        guessed. The first ``next()`` is what opens the request, which is what
        lets the sentence pipeline hide the network latency behind playback.
        """
        import requests
        payload = {
            "text": sentence,
            "model": self.model,
            "reference_id": self.reference_id,
            "format": "wav",
            "latency": self.latency,
        }
        resp = requests.post(
            self.endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            timeout=45, stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Fish Audio stream HTTP {resp.status_code} "
                f"{str(resp.text)[:160]}"
            )
        try:
            it = resp.iter_content(chunk_size=4096)
            head = b""
            for chunk in it:
                if chunk:
                    head += chunk
                if b"data" in head and len(head) > 44:
                    break
            rate, rest = _strip_wav_header(head, resp)
            if rest:
                yield (rest, rate)
            for chunk in it:
                if chunk:
                    yield (chunk, rate)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def speak(self, text: str, cancel=None, on_audio=None) -> None:
        """Speak ``text`` sentence by sentence, streaming each one.

        Falls back to the previous whole-reply path (SDK first, then HTTP) on
        any streaming failure, so an API change degrades to the old behaviour
        instead of silence.
        """
        text = str(text or "").strip()
        if not text:
            return
        if _streaming_enabled():
            try:
                if _play_sentence_stream(text, self._frame_iterator,
                                         cancel=cancel, on_audio=on_audio):
                    return
            except Exception as exc:
                print(f"[TTS] Fish Audio streaming unavailable: {exc} — "
                      f"falling back to whole-reply fetch")
        if cancel is not None and cancel.is_set():
            return
        try:
            audio = self._convert_sdk(text)
        except ImportError:
            audio = self._convert_http(text)
        except Exception as exc:
            raise RuntimeError(f"Fish Audio request failed ({exc})") from exc
        if on_audio is not None:
            try:
                on_audio()
            except Exception:
                pass
        _play_audio_bytes(audio)


# ---------------------------------------------------------------------------
# Voice-engine status + structured JSON configuration
# ---------------------------------------------------------------------------

_VOICE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "voice_config.json"

# Defaults mirror the flat keys already persisted in config/api_keys.json so the
# two configuration sources stay compatible.
_VOICE_DEFAULTS: dict = {
    "engine": "edgetts",
    "voice": "en-GB-RyanNeural",
    "model": "",
    "config": "",
    "speaker": "default",
    "speed": 1.0,
    "pitch": 0.0,
    "volume": 1.0,
    "sample_rate": 24000,
    "output_format": "mp3",
    "latency": "normal",
    "execution_provider": "cpu",
}

_KNOWN_ENGINES = {"edgetts", "kokoro", "elevenlabs", "fish_audio", "fish", "fishaudio", "onnx"}


@dataclass
class VoiceStatus:
    """Live health/telemetry for the selected voice engine."""
    engine:     str   = ""
    model:      str   = ""
    state:      str   = "IDLE"     # IDLE | LOADING | READY | PLAYING | ERROR
    error:      str   = ""
    latency_ms: float = 0.0
    init_ms:    float = 0.0
    provider:   str   = ""        # ONNX Runtime execution provider in use
    fallback:   str   = ""        # why a GPU request degraded to CPU (if it did)


_status_lock = threading.Lock()
_VOICE_STATUS = VoiceStatus()


def voice_status() -> VoiceStatus:
    """Snapshot of the current voice engine status (safe to read from the UI)."""
    with _status_lock:
        return VoiceStatus(**_VOICE_STATUS.__dict__)


def _set_status(**kw) -> None:
    with _status_lock:
        for k, v in kw.items():
            if hasattr(_VOICE_STATUS, k):
                setattr(_VOICE_STATUS, k, v)


def _clamp(value, lo, hi, default):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def list_onnx_voices() -> list[dict]:
    """Discover local Piper-format ONNX voices for the Settings picker.

    Scans config/voices/ (project-shipped voices) plus config/voice_models/
    and any explicitly configured location, and validates the sidecar JSON so
    a stray file can never offer itself as a selectable voice. Returns dicts
    with label/model/config/name — no dependency on the TTS engine being
    installed, so the picker works before onnxruntime is even present.
    """
    found: dict[str, dict] = {}
    roots = []
    try:
        root = Path(__file__).resolve().parent.parent
    except Exception:
        root = Path.cwd()
    for sub in ("config/voices", "config/voice_models"):
        d = root / sub
        if d.is_dir():
            roots.append(d)
    for d in roots:
        try:
            for m in sorted(d.glob("*.onnx")):
                sidecar = m.with_suffix("").with_suffix(".json")
                if not sidecar.exists():
                    sidecar = Path(str(m) + ".json")
                meta = {}
                if sidecar.exists():
                    try:
                        meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    except Exception:
                        meta = {}
                # Require the essential Piper keys — anything else is noise.
                if "phoneme_id_map" not in meta or "num_symbols" not in meta:
                    continue
                name = meta.get("dataset") or m.stem
                found[name] = {
                    "label": f"{name}  |  {meta.get('language', {}).get('code', '?')}"
                             f"  |  {meta.get('audio', {}).get('quality', '')}".rstrip(" |"),
                    "model": str(m),
                    "config": str(sidecar),
                    "name": name,
                }
        except Exception as exc:
            print(f"[TTS] voice scan skipped {d.name}: {exc}")
    return sorted(found.values(), key=lambda v: v["name"])


def load_voice_config(path: Optional[str] = None) -> dict:
    """Load + validate config/voice_config.json, merged over safe defaults.

    Missing file or malformed JSON degrades to defaults (never raises), so a
    broken voice config can never stop the assistant from starting.
    """
    cfg = dict(_VOICE_DEFAULTS)
    p = Path(path) if path else _VOICE_CONFIG_PATH
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in _VOICE_DEFAULTS})
    except Exception as exc:
        print(f"[TTS] voice_config.json could not be loaded ({exc}); using defaults.")
    return validate_voice_config(cfg)


def validate_voice_config(cfg: dict) -> dict:
    """Normalise + sanity-check a voice config dict in place-safe fashion."""
    out = dict(_VOICE_DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if k in _VOICE_DEFAULTS})
    eng = str(out.get("engine", "edgetts")).strip().lower()
    out["engine"] = eng if eng in _KNOWN_ENGINES else "edgetts"
    out["speed"]  = _clamp(out.get("speed"), 0.25, 4.0, 1.0)
    out["pitch"]  = _clamp(out.get("pitch"), -24.0, 24.0, 0.0)
    out["volume"] = _clamp(out.get("volume"), 0.0, 1.0, 1.0)
    out["sample_rate"] = int(_clamp(out.get("sample_rate"), 8000, 48000, 24000))
    fmt = str(out.get("output_format", "mp3")).strip().lower()
    out["output_format"] = fmt if fmt in {"mp3", "wav", "opus"} else "mp3"
    out["execution_provider"] = normalize_onnx_execution_provider(out.get("execution_provider"))
    return out


# ---------------------------------------------------------------------------
# ONNX execution providers (the local Piper voice runs through ONNX Runtime)
# ---------------------------------------------------------------------------

ONNX_EXECUTION_MODES = ("cpu", "gpu", "auto")

# Preferred accelerators, best first: TensorRT/CUDA on NVIDIA, ROCm on AMD,
# DirectML on any Windows GPU (no CUDA install needed), CoreML on Apple
# silicon. Whichever the *installed* onnxruntime build actually offers is what
# gets used — nothing here requires a GPU to exist or to be usable.
_GPU_EXECUTION_PROVIDERS = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
)
_CPU_EXECUTION_PROVIDER = "CPUExecutionProvider"


def normalize_onnx_execution_provider(value) -> str:
    """Map ``execution_provider`` onto ``cpu`` | ``gpu`` | ``auto``.

    Anything unrecognised (including a missing value) becomes ``cpu``, the one
    setting that is guaranteed to work everywhere.
    """
    mode = str(value or "").strip().lower()
    if mode in {"auto", "automatic"}:
        return "auto"
    if mode in {"gpu", "cuda", "directml", "dml", "rocm", "tensorrt"}:
        return "gpu"
    return "cpu"


def onnx_available_providers() -> list[str]:
    """Execution providers the installed onnxruntime build offers. Never raises:
    a missing/broken onnxruntime must read as "CPU only", not as a crash."""
    try:
        import onnxruntime  # noqa: PLC0415
        return [str(p) for p in onnxruntime.get_available_providers()]
    except Exception as exc:  # pragma: no cover - depends on the install
        print(f"[PIPER] Execution provider probe failed: {exc}")
        return [_CPU_EXECUTION_PROVIDER]


def resolve_onnx_providers(mode: str = "cpu"):
    """Resolve the configured mode into ``(providers, label, fallback_reason)``.

    CPU is always reachable: a GPU that is wanted but unavailable degrades
    here with a reason (and again after a failed session in the engine), so the
    voice can always speak.
    """
    mode = normalize_onnx_execution_provider(mode)
    available = onnx_available_providers()
    gpu = next((p for p in _GPU_EXECUTION_PROVIDERS if p in available), "")
    if mode == "cpu":
        return [_CPU_EXECUTION_PROVIDER], _CPU_EXECUTION_PROVIDER, ""
    if gpu:
        if mode == "gpu":
            return [gpu], gpu, ""
        # Auto: prefer the GPU, keep CPU behind it so ORT can never fail the load.
        return [gpu, _CPU_EXECUTION_PROVIDER], gpu, ""
    return (
        [_CPU_EXECUTION_PROVIDER],
        _CPU_EXECUTION_PROVIDER,
        "GPU requested but this onnxruntime build offers no GPU execution "
        f"provider (available: {', '.join(available) or 'none'}) - using CPU",
    )


# ---------------------------------------------------------------------------
# ONNX engine (local inference, lazy, graceful when deps/models are absent)
# ---------------------------------------------------------------------------

class OnnxTTSEngine:
    """Local ONNX voice adapter.

    Designed around the widely-used Piper-format ONNX voice layout (a `<voice>.onnx`
    graph plus an optional `<voice>.json` describing the phoneme id map and audio
    parameters). The model is loaded lazily on first speak() so startup never
    pays for it, and every failure raises a clear, actionable error instead of
    taking the assistant down.
    """

    def __init__(
        self,
        model_path:       str,
        config_path:      str = "",
        speaker:          str = "default",
        sample_rate:      int = 22050,
        volume:           float = 1.0,
        speed:            float = 1.0,
        execution_provider: str = "cpu",
    ) -> None:
        self.model_path  = (model_path or "").strip()
        self.config_path = (config_path or "").strip()
        self.speaker     = speaker or "default"
        self.sample_rate = int(sample_rate or 22050)
        self.volume      = float(volume or 1.0)
        self.speed       = max(0.5, min(2.0, float(speed or 1.0)))
        # ``cpu`` | ``gpu`` | ``auto`` — see normalize_onnx_execution_provider.
        self.execution_provider = normalize_onnx_execution_provider(execution_provider)
        self.provider        = ""        # what the live session actually uses
        self.provider_reason = ""        # why a GPU request degraded to CPU
        self._voice      = None          # cached PiperVoice — loaded ONCE
        self._meta: dict = {}
        self._lock       = threading.Lock()

    # -- path resolution -----------------------------------------------------
    @staticmethod
    def _resolve(path: str) -> str:
        """Resolve a configured voice path against the project root when the
        process was not started there (start_jarvis.py, .lnk launches)."""
        if not path:
            return path
        p = Path(path)
        if p.is_absolute() and p.exists():
            return str(p)
        try:
            root = Path(__file__).resolve().parent.parent
        except Exception:
            return str(p)
        cand = root / path
        return str(cand) if cand.exists() else str(p)

    # -- sidecar resolution / parsing ----------------------------------------
    def _sidecar_path(self) -> str:
        """The voice's sidecar JSON. Piper only guesses ``<model>.json``, which
        misses the shipped ``config/voices/jarvis-high.json`` next to
        ``jarvis-high.onnx`` — so that spelling is tried too."""
        if self.config_path and Path(self.config_path).exists():
            return self.config_path
        if self.model_path:
            for cand in (Path(self.model_path).with_suffix(".json"),
                         Path(str(self.model_path) + ".json")):
                if cand.exists():
                    return str(cand)
        return self.config_path

    def _read_sidecar(self) -> dict:
        try:
            sidecar = self._sidecar_path()
            if sidecar and Path(sidecar).exists():
                return json.loads(Path(sidecar).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[TTS] ONNX voice config unreadable ({exc}); using defaults.")
        return {}

    def _open_voice(self, providers):
        """Build a PiperVoice on *our* ONNX Runtime session.

        ``PiperVoice.load(use_cuda=...)`` offers only CUDA-or-CPU and never
        reports what it actually got, so the session is created here with the
        resolved provider list (CPU / GPU / Auto) and the realised providers
        are read back for the diagnostics line.
        """
        import onnxruntime  # noqa: PLC0415
        from piper.config import PiperConfig  # type: ignore
        from piper.voice import PiperVoice  # type: ignore

        meta = self._read_sidecar()
        if not meta:
            raise RuntimeError(
                f"ONNX voice sidecar not found for {Path(self.model_path).name} "
                f"(looked for {self._sidecar_path() or '<none>'})."
            )
        session = onnxruntime.InferenceSession(
            self.model_path,
            sess_options=onnxruntime.SessionOptions(),
            providers=list(providers),
        )
        return PiperVoice(config=PiperConfig.from_dict(meta), session=session), meta

    # -- lazy load ---------------------------------------------------------
    def _load(self) -> None:
        """Build the Piper voice once and cache it forever.

        The 114 MB jarvis-high graph took ~7 s to load on the dev machine, so
        loading it per synthesis (the old behaviour) made every sentence pay
        startup cost again. One load, then plain inference."""
        with self._lock:
            if self._voice is not None:
                return
            self.model_path  = self._resolve(self.model_path)
            self.config_path = self._resolve(self.config_path)
            if not self.model_path:
                raise RuntimeError(
                    "ONNX voice not configured. Settings ▸ Voice ▸ ONNX lets you "
                    "pick a local .onnx model (config/voices/jarvis-high.onnx ships "
                    "with JARVIS)."
                )
            if not Path(self.model_path).exists():
                raise RuntimeError(f"ONNX voice model not found: {self.model_path}")
            try:
                from piper.voice import PiperVoice  # type: ignore  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "ONNX voice inference needs the 'piper-tts' runtime. "
                    "Install with: pip install piper-tts onnxruntime"
                ) from exc
            _t0 = time.perf_counter()
            providers, label, reason = resolve_onnx_providers(self.execution_provider)
            _set_status(engine="onnx", model=Path(self.model_path).name, state="LOADING")
            if reason:
                print(f"[PIPER] {reason}")
            try:
                self._voice, self._meta = self._open_voice(providers)
            except Exception as exc:
                if label != _CPU_EXECUTION_PROVIDER:
                    # The accelerator cannot be initialised here (no driver, no
                    # device, ORT built without it) — CPU still has to work.
                    reason = (f"{label} could not be initialised ({exc}); "
                              "falling back to CPUExecutionProvider")  # ASCII: logs go to cp1252 consoles
                    print(f"[PIPER] {reason}")
                    providers, label = [_CPU_EXECUTION_PROVIDER], _CPU_EXECUTION_PROVIDER
                    self._voice, self._meta = self._open_voice(providers)
                else:
                    # Not a provider problem: keep the previous loader working
                    # even if a future piper-tts changes its constructor.
                    print(f"[PIPER] Explicit session failed ({exc}); using piper's loader.")
                    self._voice = PiperVoice.load(
                        self.model_path, config_path=self._sidecar_path() or None
                    )
                    self._meta = self._read_sidecar()
            try:
                self.provider = ", ".join(str(p) for p in self._voice.session.get_providers())
            except Exception:
                self.provider = label
            self.provider_reason = reason
            if isinstance(self._meta, dict) and self._meta:
                try:
                    self.sample_rate = int(
                        self._meta.get("audio", {}).get("sample_rate", self.sample_rate))
                except Exception:
                    pass
            _ms = (time.perf_counter() - _t0) * 1000
            _set_status(state="READY", init_ms=_ms, error="",
                        provider=self.provider, fallback=self.provider_reason)
            # The two lines the user asked to be able to read in the log.
            print(f"[PIPER] Model loaded: {Path(self.model_path).name}")
            print(f"[PIPER] Execution provider: {self.provider}")
            print(f"[TTS] INIT_DONE onnx model={Path(self.model_path).name} "
                  f"({_ms:.0f} ms, requested={self.execution_provider})")

    # -- prewarm -------------------------------------------------------------
    def warmup(self) -> None:
        """Force the one-time model load immediately (TTSPlayer.warmup calls
        this in a background thread so the startup greeting never pays it)."""
        self._load()

    @property
    def inputs(self) -> list[str]:
        self._load()
        return [i.name for i in self._voice.session.get_inputs()]

    def _syn_config(self):
        """Piper inference parameters: the voice JSON's own values, modulated
        by the configured speed/volume/speaker."""
        from piper.config import SynthesisConfig  # type: ignore
        inf = self._meta.get("inference", {}) if isinstance(self._meta, dict) else {}
        try:
            base_length = float(inf.get("length_scale", 1.0) or 1.0)
        except Exception:
            base_length = 1.0
        # speed > 1 must SHORTEN audio → divide the model's length scale.
        cfg = SynthesisConfig(
            length_scale=max(0.4, base_length / self.speed),
            noise_scale=float(inf.get("noise_scale", 0.667) or 0.667),
            noise_w_scale=float(inf.get("noise_w", 0.8) or 0.8),
            volume=max(0.1, min(2.0, self.volume)),
        )
        try:
            n_spk = int(self._meta.get("num_speakers", 1) or 1)
        except Exception:
            n_spk = 1
        if n_spk > 1 and self.speaker not in ("default", ""):
            # Multi-speaker models take a numeric id; accept a name via the
            # map, else fall back to index 0 rather than an ORT shape error.
            spk_map = self._meta.get("speaker_id_map", {})
            if self.speaker in spk_map:
                cfg.speaker_id = int(spk_map[self.speaker])
            elif str(self.speaker).isdigit():
                cfg.speaker_id = int(self.speaker)
            else:
                print(f"[TTS] ONNX speaker '{self.speaker}' unknown — using id 0.")
        return cfg

    def synthesize(self, text: str, cancel=None):
        """Yield ``(float32 samples, sample_rate)`` per Piper audio chunk.

        ``cancel`` is an optional ``threading.Event``: when it is set the
        remaining chunks are abandoned, which is what lets an interruption cut
        the *current* sentence instead of only the ones still queued.
        """
        self._load()
        cfg = self._syn_config()
        for chunk in self._voice.synthesize(text, syn_config=cfg):
            if cancel is not None and cancel.is_set():
                return
            yield (np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16).astype(np.float32)
                   / 32768.0, chunk.sample_rate)

    def speak(self, text: str, cancel=None, on_audio=None) -> None:
        """Synthesize and play *text*, chunk by chunk.

        ``cancel`` abandons the rest of the line (interruption), and
        ``on_audio`` fires once, when the first chunk is actually reaching the
        speaker — which is how the caller can distinguish "synthesizing" from
        "playing" without polling anything.
        """
        self._load()
        _t0 = time.perf_counter()
        started = False
        try:
            for arr, rate in self.synthesize(text, cancel=cancel):
                if cancel is not None and cancel.is_set():
                    break
                if not started:
                    started = True
                    _set_status(state="PLAYING")
                    if on_audio is not None:
                        try:
                            on_audio()
                        except Exception:
                            pass
                _play_np(arr, rate)
        finally:
            _set_status(state="READY", latency_ms=(time.perf_counter() - _t0) * 1000)


# ---------------------------------------------------------------------------
# Thread-safe player wrapper
# ---------------------------------------------------------------------------

class TTSPlayer:
    """
    Wraps any *Engine. Exposes a blocking speak() method
    meant to be called from a dedicated background thread.

    Engine construction is LAZY: pass a ``factory`` (a zero-arg callable) and the
    real engine is only built the first time speak() is called. This keeps
    expensive engines (Kokoro model download/warmup, network clients) completely
    out of the startup path while still caching the instance afterwards.
    """

    def __init__(self, engine=None, factory: Optional[Callable] = None, name: str = ""):
        self._engine  = engine
        self._factory = factory
        self._name    = name or (type(engine).__name__ if engine is not None else "")
        self._playing = False
        self._first_request_seen = False
        self._lock    = threading.Lock()

    @property
    def is_playing(self) -> bool:
        return self._playing

    def warmup(self) -> bool:
        """Build the engine AND pre-load any heavy model now (background
        thread) so the first real speak() has zero init delay. Engines that
        lazy-load inside speak() expose a ``warmup()`` — call it when present."""
        try:
            engine = self._ensure_engine()
            hook = getattr(engine, "warmup", None)
            if callable(hook):
                hook()
            return True
        except Exception as exc:
            print(f"[TTS] WARMUP_FAILED {self._name}: {exc}")
            return False

    def _ensure_engine(self):
        """Build the engine on first use (idempotent, lock-guarded)."""
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is None and self._factory is not None:
                print(f"[TTS] ENGINE_SELECTED {self._name}")
                print(f"[TTS] INIT_BEGIN {self._name}")
                _t0 = time.perf_counter()
                _set_status(engine=self._name, state="LOADING", error="")
                try:
                    self._engine = self._factory()
                except Exception as exc:
                    _set_status(state="ERROR", error=str(exc))
                    raise
                _ms = (time.perf_counter() - _t0) * 1000
                _set_status(state="READY", init_ms=_ms, error="")
                print(f"[TTS] INIT_DONE {self._name} ({_ms:.0f} ms)")
        return self._engine

    def speak(
        self,
        text:     str,
        on_start: Optional[Callable] = None,
        on_done:  Optional[Callable] = None,
        cancel:   Optional[threading.Event] = None,
        on_audio: Optional[Callable] = None,
    ) -> None:
        """Synthesise and play text. BLOCKING – call from a dedicated thread.

        ``cancel`` lets a caller abandon the current line mid-sentence: the
        ONNX/Piper engine checks it between audio chunks (other engines simply
        stop on the next line). ``on_audio`` fires when real playback begins on
        engines that can tell the difference.
        """
        try:
            with self._lock:
                self._playing = True
            engine = self._ensure_engine()
            if not self._first_request_seen:
                self._first_request_seen = True
                print(f"[TTS] FIRST_REQUEST {self._name}")
            if cancel is not None and cancel.is_set():
                return
            if on_start:
                on_start()
            _t0 = time.perf_counter()
            print("[TTS] PLAYBACK_START")
            if isinstance(engine, OnnxTTSEngine) or (
                    isinstance(engine, (ElevenLabsTTSEngine, FishAudioTTSEngine))
                    and _accepts_stream_kwargs(engine)):
                # These understand ``cancel`` (interruption can now cut a cloud
                # reply mid-sentence, which it could not before) and can report
                # the exact moment audio really starts.
                engine.speak(text, cancel=cancel, on_audio=on_audio)
            else:
                if on_audio is not None:
                    try:
                        on_audio()
                    except Exception:
                        pass
                engine.speak(text)
            print("[TTS] PLAYBACK_DONE")
            _set_status(state="READY", latency_ms=(time.perf_counter() - _t0) * 1000)
        except Exception as e:
            _set_status(state="ERROR", error=str(e))
            print(f"[TTS] Error: {e}")
        finally:
            with self._lock:
                self._playing = False
            if on_done:
                on_done()

    def stop(self) -> None:
        sd.stop()
        with self._lock:
            self._playing = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# ── Engine instance cache ─────────────────────────────────────────────────────
# The provider-answer path and the Settings TEST button call create_tts_player
# on EVERY reply/test. Without a cache each call built a NEW engine — for ONNX
# that meant re-loading the 114 MB jarvis-high model (6–9 s) per sentence, so
# the Settings TEST button appeared to hang forever. Keyed by the engine's
# constructor inputs; clear via clear_tts_cache() after settings change.
_TTS_CACHE: dict[tuple, "TTSPlayer"] = {}
_TTS_CACHE_LOCK = threading.Lock()


def _tts_cache_key(config: dict, vcfg: dict, engine_name: str) -> tuple:
    # The active provider and its own subtree values are part of the key so a
    # provider switch (or an edit to one provider's fields) rebuilds the engine,
    # while unrelated saves reuse it.
    try:
        from voice.providers_config import active_provider, providers_block
        _provider = active_provider(config)
        _subtree = providers_block(config)["providers"].get(_provider, {})
        _provider_part = (
            "provider",
            _provider,
            str(sorted((k, str(v)) for k, v in _subtree.items())),
        )
    except Exception:
        _provider_part = ()
    return (
        engine_name,
        _provider_part,
        str(config.get("onnx_voice_model") or vcfg.get("model", "")),
        str(config.get("onnx_voice_config") or vcfg.get("config", "")),
        str(config.get("onnx_voice_speaker") or ""),
        # Part of the key so switching CPU/GPU/Auto selects a voice built with
        # the new provider — and reuses the loaded one while it is unchanged.
        normalize_onnx_execution_provider(
            config.get("onnx_execution_provider") or vcfg.get("execution_provider")),
        str(config.get("tts_voice") or vcfg.get("voice", "")),
        str(config.get("elevenlabs_api_key") or ""),
        str(config.get("elevenlabs_voice_id") or ""),
        str(config.get("elevenlabs_model_id") or ""),
        str(config.get("fish_audio_api_key") or ""),
        str(config.get("fish_audio_voice_id") or ""),
        str(config.get("fish_audio_model_id") or ""),
        float(config.get("voice_speed", vcfg.get("speed", 1.0)) or 1.0),
        float(config.get("voice_volume", vcfg.get("volume", 1.0)) or 1.0),
        float(config.get("voice_pitch", vcfg.get("pitch", 0.0)) or 0.0),
    )


def clear_tts_cache(engine: Optional[str] = None) -> None:
    """Drop cached engines. Called when settings change so the next speak()
    rebuilds with the new voice/engine. ``engine`` filters to one name."""
    with _TTS_CACHE_LOCK:
        if engine is None:
            _TTS_CACHE.clear()
        else:
            for k in [k for k in _TTS_CACHE if k[0] == engine]:
                _TTS_CACHE.pop(k, None)


def create_tts_player(config: dict) -> TTSPlayer:
    """Create a lazy TTS player for the selected engine.

    The engine is NOT constructed here — ``create_tts_player`` is cheap and safe
    to call during startup; the first ``speak()`` builds it. Configuration is
    merged from config/voice_config.json (structured) and the flat api_keys.json
    keys, so existing settings keep working unchanged.
    """
    vcfg = load_voice_config()
    # Provider-scoped values first (nested voice block, dual-read over the flat
    # keys), then the flat keys, then voice_config.json — matching prior
    # behaviour for configs that carry only flat keys.
    try:
        from voice.providers_config import active_provider, providers_block, PROVIDER_ENGINE
        _active = active_provider(config)
        _pv = providers_block(config)["providers"].get(_active, {})
    except Exception:
        _active, _pv = "", {}
    if _active and _pv:
        # The provider layer decides the engine: the active provider maps to a
        # canonical engine name and a legacy tts_engine that disagrees must not
        # resurrect another engine.
        engine_name = PROVIDER_ENGINE.get(_active, "edgetts")
    else:
        # Flat api_keys.json keys win over voice_config.json, matching prior behaviour.
        engine_name = str(config.get("tts_engine") or vcfg.get("engine") or "edgetts").lower()

    _key = _tts_cache_key(config, vcfg, engine_name)
    with _TTS_CACHE_LOCK:
        cached = _TTS_CACHE.get(_key)
    if cached is not None:
        return cached

    if engine_name == "kokoro":
        def _make():
            voice = (_pv.get("voice") or config.get("tts_voice")
                     or vcfg.get("voice", "af_heart"))
            return KokoroTTSEngine(
                voice=voice,
                speed=float((_pv.get("speed") or config.get("tts_speed")
                             or vcfg.get("speed", 1.0))),
            )
    elif engine_name == "elevenlabs":
        def _make():
            return ElevenLabsTTSEngine(
                api_key=_pv.get("apiKey") or config.get("elevenlabs_api_key", ""),
                voice_id=(_pv.get("voice") or config.get("elevenlabs_voice_id")
                          or config.get("tts_voice", ELEVENLABS_DEFAULT_VOICE)),
                model_id=(_pv.get("model") or config.get("elevenlabs_model_id")
                          or ELEVENLABS_DEFAULT_MODEL),
            )
    elif engine_name in {"fish_audio", "fish", "fishaudio"}:
        def _make():
            return FishAudioTTSEngine(
                api_key=_pv.get("apiKey") or config.get("fish_audio_api_key", ""),
                model_id=_pv.get("model") or config.get("fish_audio_model_id", "s2-pro"),
                voice_id=_pv.get("voice") or config.get("fish_audio_voice_id", ""),
                endpoint=(_pv.get("endpoint") or config.get("fish_audio_endpoint")
                          or "https://api.fish.audio/v1/tts"),
                audio_format=(_pv.get("format") or config.get("fish_audio_format")
                              or vcfg.get("output_format", "mp3")),
                latency=(_pv.get("latency") or config.get("fish_audio_latency")
                         or vcfg.get("latency", "normal")),
            )
    elif engine_name in {"sapi", "sapi5", "pyttsx3", "windows"}:
        def _make():
            # Rate: SAPI speaks ~180 wpm at default; map speed 0.5–2.0 onto it.
            speed = float(config.get("voice_speed", vcfg.get("speed", 1.0)) or 1.0)
            return SapiTTSEngine(
                voice=_pv.get("voice") or config.get("sapi_voice") or config.get("tts_voice", ""),
                rate=int(170 * max(0.5, min(2.0, speed))),
                volume=float(config.get("voice_volume", vcfg.get("volume", 1.0)) or 1.0),
            )
    elif engine_name == "onnx":
        def _make():
            return OnnxTTSEngine(
                model_path=(_pv.get("modelPath") or config.get("onnx_voice_model")
                            or vcfg.get("model", "")),
                config_path=(_pv.get("configPath") or config.get("onnx_voice_config")
                             or vcfg.get("config", "")),
                speaker=(_pv.get("speaker") or config.get("onnx_voice_speaker")
                         or vcfg.get("speaker", "default")),
                sample_rate=vcfg.get("sample_rate", 22050),
                volume=float(config.get("voice_volume", vcfg.get("volume", 1.0))),
                speed=float(config.get("voice_speed", vcfg.get("speed", 1.0))),
                execution_provider=(_pv.get("executionProvider")
                                    or config.get("onnx_execution_provider")
                                    or vcfg.get("execution_provider") or "cpu"),
            )
    else:   # edgetts (default)
        def _make():
            voice = _pv.get("voice") or config.get("tts_voice") or vcfg.get("voice", "en-GB-RyanNeural")
            speed = float(config.get("voice_speed", config.get("tts_speed", vcfg.get("speed", 1.0))))
            pitch = float(config.get("voice_pitch", vcfg.get("pitch", 0.0)))
            rate = str(_pv.get("rate") or f"{round((speed - 1.0) * 100):+d}%")
            pitch_hz = str(_pv.get("pitch") or f"{round(pitch * 12):+d}Hz")
            return EdgeTTSEngine(voice=voice, rate=rate, pitch=pitch_hz)

    _set_status(engine=engine_name, state="IDLE", error="")
    print(f"[TTS] ENGINE_SELECTED {engine_name} (lazy)")
    player = TTSPlayer(factory=_make, name=engine_name)
    with _TTS_CACHE_LOCK:
        _TTS_CACHE[_key] = player
    return player
