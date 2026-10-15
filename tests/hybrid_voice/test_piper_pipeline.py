"""Gemini Live → Piper ONNX pipeline tests.

Everything here is pure Python: no microphone, no speaker, no network and no
Gemini session. The Piper voice itself is faked, so the tests exercise the
pipeline's own behaviour — mode migration, chunking, the bounded reusable
worker, interruption, provider selection and the regression that started this
change (SAVE AS DEFAULT VOICE forcing `local`).
"""
import io
import re
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import core.hybrid_voice as hv
from core import tts as tts_mod
from core.hybrid_voice import (
    DEFAULT_VOICE_OUTPUT_MODE,
    PiperSpeechService,
    VOICE_OUTPUT_MODES,
    normalize_voice_output_mode,
    resolve_voice_mode,
    split_speech_chunks,
)
from core.tts import (
    OnnxTTSEngine,
    normalize_onnx_execution_provider,
    onnx_available_providers,
    resolve_onnx_providers,
)


def _wait(pred, timeout=5.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class FakePlayer:
    """Stands in for the cached TTSPlayer, recording what was spoken."""

    def __init__(self, blocked=False):
        self.calls = []
        self.cancelled = []
        self.warmups = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def warmup(self):
        self.warmups += 1

    def speak(self, text, cancel=None, on_audio=None, **kwargs):
        self.calls.append(text)
        if on_audio is not None:
            on_audio()
        self.entered.set()
        while not self.release.is_set():
            if cancel is not None and cancel.is_set():
                self.cancelled.append(text)
                return
            time.sleep(0.01)
        if cancel is not None and cancel.is_set():
            self.cancelled.append(text)

    def stop(self):
        pass


def _service(player=None, max_queue=12, cfg=None):
    PiperSpeechService.reset()
    svc = PiperSpeechService.get(cfg or {"voice_output_mode": "gemini_piper"},
                                 )
    if max_queue != 12:
        svc.max_queue = max_queue
        svc._queue = type(svc._queue)(maxsize=max_queue)
    if player is not None:
        svc._ensure_player = lambda: player
    return svc


# ── 1. Mode normalisation + one-time migration ────────────────────────────────

def test_three_modes_are_the_only_real_modes():
    assert set(VOICE_OUTPUT_MODES) == {"gemini", "gemini_piper", "local"}
    assert DEFAULT_VOICE_OUTPUT_MODE == "gemini"


def test_missing_mode_defaults_to_native_audio():
    assert normalize_voice_output_mode("") == "gemini"
    assert normalize_voice_output_mode(None) == "gemini"
    assert resolve_voice_mode({})[0] == "gemini"


def test_legacy_hybrid_lifts_to_native_once_then_is_respected():
    assert normalize_voice_output_mode("hybrid") == "gemini"
    assert normalize_voice_output_mode("hybrid", piper_lifted=True) == "gemini_piper"
    lifted = {"voice_output_mode": "hybrid", "voice_piper_to_native_lifted": True}
    assert resolve_voice_mode(lifted)[0] == "gemini_piper"


def test_explicit_local_is_kept_and_piper_lifts_once():
    assert normalize_voice_output_mode("local") == "local"
    assert normalize_voice_output_mode("gemini_piper") == "gemini"
    assert normalize_voice_output_mode("gemini_piper", piper_lifted=True) == "gemini_piper"


def test_unlifted_piper_config_reports_the_lift_marker():
    mode, needs_marker = resolve_voice_mode({"voice_output_mode": "gemini_piper"})
    assert mode == "gemini"
    assert needs_marker is True


def test_piper_choice_is_respected_after_the_lift_marker():
    mode, needs_marker = resolve_voice_mode({
        "voice_output_mode": "gemini_piper",
        "voice_piper_to_native_lifted": True,
    })
    assert mode == "gemini_piper"
    assert needs_marker is False


def test_gemini_choice_is_respected_after_the_marker():
    mode, needs_marker = resolve_voice_mode({
        "voice_output_mode": "gemini",
        "voice_pipeline_migrated": True,
    })
    assert mode == "gemini"
    assert needs_marker is False


def test_validation_accepts_new_modes_and_rejects_unknown():
    # The Gemini key is passed so validation is hermetic — otherwise it falls
    # back to this machine's real config file and the result depends on it.
    assert hv._validate_voice_settings({"voice_output_mode": "gemini_piper", "gemini_api_key": "k"})[0] is True
    assert hv._validate_voice_settings({"voice_output_mode": "local"})[0] is True
    ok, err = hv._validate_voice_settings({"voice_output_mode": "telepathy"})
    assert ok is False and "Unknown voice mode" in err


def test_legacy_onnx_mode_still_parses():
    assert normalize_voice_output_mode("onnx") == "local"


# ── 2. Streaming text → sentence chunks ──────────────────────────────────────

def test_chunks_split_on_sentence_terminators():
    chunks, rest = split_speech_chunks("Good evening, sir. All systems are online.")
    assert chunks == ["Good evening, sir.", "All systems are online."]
    assert rest == ""


def test_the_startup_greeting_is_never_truncated_to_its_first_sentence():
    """The reported bug: the chat showed the whole greeting but only the first
    sentence was heard. Speaking the greeting as one complete line must produce
    EVERY sentence, with no tail left waiting for a later flush."""
    player = FakePlayer()
    svc = _service(player)
    chunks, rest = split_speech_chunks("Good afternoon, sir. How may I be of service?")
    assert rest == "", "nothing may be left buffered behind a terminator"
    assert chunks == ["Good afternoon, sir.", "How may I be of service?"]
    svc.speak("Good afternoon, sir. How may I be of service?")
    assert _wait(lambda: len(player.calls) == 2), player.calls
    assert player.calls == ["Good afternoon, sir.", "How may I be of service?"]
    svc.shutdown()


def test_chunker_never_splits_a_decimal():
    chunks, rest = split_speech_chunks("Pi is 3.14 and that is that. Right.")
    assert chunks[0].startswith("Pi is 3.14")
    assert not any(c.strip() == "3." for c in chunks)


def test_chunker_hard_caps_long_unpunctuated_text():
    text = "word " * 120          # no terminator at all
    chunks, rest = split_speech_chunks(text)
    assert chunks, "a long unpunctuated span must still start speaking"
    assert all(len(c) <= hv._SPEECH_MAX_CHUNK for c in chunks)


def test_incremental_fragments_combine_into_one_chunk():
    svc = _service(FakePlayer())
    svc.feed("Good eve")
    svc.feed("ning, sir.")
    assert _wait(lambda: len(svc.status()["model"]) >= 0)  # no-op: worker starts lazily
    svc.flush()
    assert svc._queue.qsize() == 1
    svc.shutdown()


def test_cumulative_transcript_is_not_spoken_twice():
    player = FakePlayer()
    svc = _service(player)
    svc.feed("Good evening, sir.")
    svc.feed("Good evening, sir. All systems are online.")
    svc.flush()
    assert _wait(lambda: len(player.calls) == 2), player.calls
    assert player.calls == ["Good evening, sir.", "All systems are online."]
    svc.shutdown()


def test_flush_speaks_the_remainder_without_terminator():
    player = FakePlayer()
    svc = _service(player)
    svc.feed("All systems nominal")
    assert player.calls == []          # nothing complete yet
    svc.flush()
    assert _wait(lambda: player.calls == ["All systems nominal"]), player.calls
    svc.shutdown()


# ── 3. One reusable bounded worker ───────────────────────────────────────────

def test_single_worker_thread_is_reused_for_many_replies():
    player = FakePlayer()
    svc = _service(player)
    for i in range(4):
        svc.speak(f"Message number {i} is ready.")
    assert _wait(lambda: len(player.calls) == 4), player.calls
    names = [t.name for t in threading.enumerate() if t.name == "jarvis-piper-tts"]
    assert len(names) == 1, names
    svc.shutdown()


def test_queue_is_bounded_and_overflow_is_dropped_not_blocking():
    player = FakePlayer(blocked=True)
    svc = _service(player, max_queue=2)
    queued_total = 0
    for i in range(30):
        queued_total += svc.speak(f"Chunk {i} of a long reply.")
    assert svc.status()["queued"] <= 2
    assert svc.status()["dropped"] > 0
    assert queued_total <= 3
    player.release.set()
    svc.shutdown()


def test_synthesis_errors_do_not_escape_the_worker():
    class Boom(FakePlayer):
        def speak(self, text, cancel=None, on_audio=None, **kwargs):
            raise RuntimeError("onnx exploded")

    svc = _service(Boom())
    svc.speak("This will fail.")
    assert _wait(lambda: svc.phase() == "idle")
    assert svc.status()["phase"] == "idle"
    svc.shutdown()


def test_worker_resolves_the_cached_player_off_the_calling_thread():
    """The caller (Gemini event loop / Qt thread) must only ever enqueue."""
    captured = {}
    caller = threading.current_thread().name

    class SlowPlayer(FakePlayer):
        def __init__(self):
            super().__init__()
            self.threads = []

        def speak(self, text, cancel=None, on_audio=None, **kwargs):
            self.threads.append(threading.current_thread().name)
            time.sleep(0.05)
            super().speak(text, cancel=cancel, on_audio=on_audio)

    player = SlowPlayer()

    def fake_create(cfg):
        captured["cfg"] = cfg
        captured["thread"] = threading.current_thread().name
        return player

    PiperSpeechService.reset()
    svc = PiperSpeechService.get({"voice_output_mode": "gemini_piper",
                                  "tts_engine": "edgetts"})
    with mock.patch.object(tts_mod, "create_tts_player", side_effect=fake_create):
        started = time.perf_counter()
        svc.feed("A sentence that takes real time to synthesize. ")
        svc.flush()
        caller_cost = time.perf_counter() - started
        assert caller_cost < 0.02, f"feeding must not synthesize on the caller ({caller_cost:.3f}s)"
        assert _wait(lambda: player.threads), "the worker never spoke"
    assert captured["thread"] == "jarvis-piper-tts", captured["thread"]
    assert set(player.threads) == {"jarvis-piper-tts"}, player.threads
    assert caller not in player.threads
    assert captured["cfg"]["tts_engine"] == "onnx", captured["cfg"]
    assert Path(captured["cfg"]["onnx_voice_model"]).name == "jarvis-high.onnx"
    PiperSpeechService.reset()
    assert svc.phase() == "idle"


# ── 4. Interruption ──────────────────────────────────────────────────────────

def test_interrupt_stops_playback_and_clears_the_queue():
    player = FakePlayer(blocked=True)
    svc = _service(player)
    for i in range(6):
        svc.speak(f"Sentences queued while the user starts talking {i}.")
    assert _wait(lambda: player.entered.is_set())
    calls_before = len(player.calls)
    cleared = svc.interrupt()
    assert cleared >= 1
    assert svc.status()["queued"] == 0
    assert svc.phase() == "idle"
    player.release.set()
    assert _wait(lambda: bool(player.cancelled)), "the current chunk was not cancelled"
    time.sleep(0.2)
    assert len(player.calls) == calls_before, "queued speech resumed after the interrupt"
    svc.shutdown()


def test_interrupted_text_never_resumes_on_the_next_turn():
    player = FakePlayer()
    svc = _service(player)
    svc.feed("This reply was interrupted before it ended")
    svc.interrupt()
    svc.feed("A brand new reply. It should be spoken.")
    svc.flush()
    assert _wait(lambda: player.calls), player.calls
    assert all("interrupted before" not in c for c in player.calls), player.calls
    svc.shutdown()


def test_new_speech_after_interrupt_uses_a_fresh_cancel_token():
    player = FakePlayer()
    svc = _service(player)
    svc.interrupt()
    svc.speak("Speaking again after the interrupt.")
    assert _wait(lambda: player.calls == ["Speaking again after the interrupt."])
    assert player.cancelled == []
    svc.shutdown()


# ── 5. Preload / lifecycle ───────────────────────────────────────────────────

def test_warmup_async_is_non_blocking_and_idempotent():
    class SlowWarm(FakePlayer):
        def warmup(self):
            time.sleep(0.4)
            super().warmup()

    svc = PiperSpeechService.get({"voice_output_mode": "gemini_piper"})
    svc._ensure_player = lambda: svc.__dict__.setdefault("_fake", SlowWarm()) and svc._fake
    started = time.perf_counter()
    assert svc.warmup_async() is True
    assert time.perf_counter() - started < 0.2      # did not block the caller
    assert svc.warmup_async() is False               # already running
    assert _wait(lambda: svc.status()["warm"] is True, timeout=5.0)
    assert svc.warmup_async() is False               # already warm: cheap no-op
    PiperSpeechService.reset()


def test_warmup_failure_is_survivable():
    class BadWarm(FakePlayer):
        def warmup(self):
            raise RuntimeError("piper-tts missing")

    svc = PiperSpeechService.get({"voice_output_mode": "gemini_piper"})
    svc._ensure_player = lambda: BadWarm()
    assert svc.warmup_now() is False
    assert svc.status()["warm"] is False
    PiperSpeechService.reset()


def test_reset_joins_the_worker_and_hands_back_a_fresh_service():
    svc = PiperSpeechService.get({"voice_output_mode": "gemini_piper"})
    svc._ensure_worker()
    assert any(t.name == "jarvis-piper-tts" for t in threading.enumerate())
    PiperSpeechService.reset()
    assert not any(t.name == "jarvis-piper-tts" for t in threading.enumerate())
    assert PiperSpeechService.get({"voice_output_mode": "gemini_piper"}) is not svc
    PiperSpeechService.reset()


def test_singleton_service_is_reused():
    PiperSpeechService.reset()
    a = PiperSpeechService.get({"voice_output_mode": "gemini_piper"})
    b = PiperSpeechService.get({"voice_output_mode": "gemini_piper"})
    assert a is b
    PiperSpeechService.reset()


def test_configure_drops_the_cached_player_but_not_the_model():
    svc = PiperSpeechService.get({"voice_output_mode": "gemini_piper"})
    svc._player = FakePlayer()
    svc._warm_ok = True
    svc.configure({"voice_output_mode": "gemini_piper",
                   "onnx_execution_provider": "gpu"})
    assert svc._player is None
    assert svc._warm_ok is False
    # create_tts_player is still the cache that owns the loaded ONNX model, so
    # dropping the reference re-resolves the same (already loaded) player.
    PiperSpeechService.reset()


# ── 6. ONNX execution providers (CPU / GPU / Auto) ───────────────────────────

def test_execution_provider_normalisation():
    assert normalize_onnx_execution_provider("cpu") == "cpu"
    assert normalize_onnx_execution_provider("GPU") == "gpu"
    assert normalize_onnx_execution_provider("Auto") == "auto"
    assert normalize_onnx_execution_provider("cuda") == "gpu"
    assert normalize_onnx_execution_provider("") == "cpu"
    assert normalize_onnx_execution_provider(None) == "cpu"
    assert normalize_onnx_execution_provider("banana") == "cpu"


def test_cpu_only_host_falls_back_with_a_reason():
    with mock.patch.object(tts_mod, "onnx_available_providers",
                           return_value=["AzureExecutionProvider", "CPUExecutionProvider"]):
        providers, label, reason = resolve_onnx_providers("gpu")
        assert providers == ["CPUExecutionProvider"]
        assert label == "CPUExecutionProvider"
        assert "no GPU execution provider" in reason

        providers, label, reason = resolve_onnx_providers("auto")
        assert providers == ["CPUExecutionProvider"]
        assert reason, "auto must still say why the CPU was chosen"


def test_cpu_mode_never_reports_a_fallback():
    with mock.patch.object(tts_mod, "onnx_available_providers",
                           return_value=["CPUExecutionProvider"]):
        providers, label, reason = resolve_onnx_providers("cpu")
        assert providers == ["CPUExecutionProvider"]
        assert reason == ""


def test_gpu_is_used_when_available():
    with mock.patch.object(tts_mod, "onnx_available_providers",
                           return_value=["CUDAExecutionProvider", "CPUExecutionProvider"]):
        assert resolve_onnx_providers("gpu")[0] == ["CUDAExecutionProvider"]
        assert resolve_onnx_providers("auto")[0] == ["CUDAExecutionProvider", "CPUExecutionProvider"]


def test_gpu_provider_probe_never_raises():
    assert isinstance(onnx_available_providers(), list)


def test_engine_records_the_requested_provider_without_loading():
    assert OnnxTTSEngine("x.onnx", execution_provider="gpu").execution_provider == "gpu"
    assert OnnxTTSEngine("x.onnx").execution_provider == "cpu"


def test_execution_provider_is_part_of_the_engine_cache_key():
    base = {"onnx_voice_model": "config/voices/jarvis-high.onnx",
            "onnx_voice_config": "config/voices/jarvis-high.json",
            "tts_engine": "onnx"}
    vcfg = tts_mod.load_voice_config()
    cpu = tts_mod._tts_cache_key(dict(base, onnx_execution_provider="cpu"), vcfg, "onnx")
    cpu2 = tts_mod._tts_cache_key(dict(base, onnx_execution_provider="cpu"), vcfg, "onnx")
    gpu = tts_mod._tts_cache_key(dict(base, onnx_execution_provider="gpu"), vcfg, "onnx")
    assert cpu == cpu2
    assert cpu != gpu


def test_three_modes_get_distinct_players_and_reuse_the_same_one():
    base = {"tts_engine": "onnx", "onnx_voice_model": "config/voices/jarvis-high.onnx",
            "onnx_voice_config": "config/voices/jarvis-high.json"}
    tts_mod.clear_tts_cache()
    a = tts_mod.create_tts_player(dict(base, onnx_execution_provider="cpu"))
    b = tts_mod.create_tts_player(dict(base, onnx_execution_provider="cpu"))
    c = tts_mod.create_tts_player(dict(base, onnx_execution_provider="gpu"))
    assert a is b, "an unchanged arrangement must reuse the cached loaded model"
    assert a is not c
    tts_mod.clear_tts_cache()


# ── 7. Structural guards for the runtime wiring ──────────────────────────────

def _main_source() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


def test_main_has_no_inline_voice_output_mode_comparisons():
    src = _main_source()
    assert "voice_output_mode" not in re.sub(r"#.*", "", src), (
        "mode decisions must go through resolve_voice_mode/voice_mode()"
    )


def test_gemini_audio_is_suppressed_before_the_speaker_stream_is_opened():
    src = _main_source()
    play = src[src.index("async def _play_audio(self):"):]
    play = play[:play.index("async def _send_startup_briefing")]
    gate = play.index('if _vmode in ("local", "gemini_piper")')
    open_spk = play.index("stream = _open_spk(_spk_dev)")
    assert gate < open_spk, "the mode gate must run before the output stream is opened"


def test_main_streams_the_transcript_into_piper():
    src = _main_source()
    assert "self._piper.feed(txt)" in src
    assert "self._piper.flush()" in src
    assert "self._piper.interrupt()" in src


def test_main_no_longer_starts_the_duplicate_microphone_coordinator():
    src = _main_source()
    assert "HybridVoiceSystem.get(" not in src
    assert "PiperSpeechService" in src


def test_save_default_voice_does_not_force_local_mode():
    src = (ROOT / "ui_settings.py").read_text(encoding="utf-8")
    assert 'data["voice_output_mode"] = "local"' not in src
    assert "data[\"voice_output_mode\"] = \"local\"" not in src
    # The provider-scoped save path is where a deliberate choice is recorded:
    # it must set the migration marker (so the one-time gemini -> gemini_piper
    # lift never overrides a choice made after this point).
    method = src[src.index("def _save_provider_panel"):]
    method = method[:method.index("def _test_provider")]
    assert "voice_pipeline_migrated" in method
    # The legacy entry point still routes through the scoped save.
    legacy = src[src.index("def _save_default_voice"):]
    legacy = legacy[:legacy.index("def _test_tts_engine")]
    assert "_save_provider_panel" in legacy


def test_settings_default_to_native_audio():
    import ui_settings
    assert ui_settings.DEFAULTS["voice_output_mode"] == "gemini"
    assert ui_settings.DEFAULTS["onnx_execution_provider"] == "cpu"
    assert Path(ui_settings.DEFAULTS["onnx_voice_model"]).name == "jarvis-high.onnx"


def test_hud_accepts_the_piper_state_labels():
    src = (ROOT / "ui.py").read_text(encoding="utf-8")
    for label in ("PIPER SYNTHESIZING", "PIPER PLAYING", "GEMINI LIVE", "MIC LISTENING"):
        assert label in src, label
    assert "'PIPER PLAYING': 'SPEAKING'" in src
    assert "'PIPER SYNTHESIZING': 'THINKING'" in src


def test_configured_piper_voice_files_exist():
    assert (ROOT / "config" / "voices" / "jarvis-high.onnx").exists()
    assert (ROOT / "config" / "voices" / "jarvis-high.json").exists()
