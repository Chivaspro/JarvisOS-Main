"""Regressions for the realtime STT -> existing-textbox -> correction pipeline.

Pipeline assertions check source wiring (cheap, no Qt / no Gemini session);
the correction assertions exercise the real engine.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
UI = (ROOT / "ui.py").read_text(encoding="utf-8")

from core.speech_correction import correct_speech  # noqa: E402


def test_obvious_recognition_error_resolves_to_known_command():
    assert correct_speech("Open Web Brow", is_final=True) == "Open Webcam"


def test_spaced_brand_resolves_and_is_recased():
    assert correct_speech("open you tube", is_final=True) == "Open YouTube"
    assert correct_speech("open chrome", is_final=True) == "Open Chrome"


def test_device_commands_resolve():
    assert correct_speech("check web cam", is_final=True) == "Check Webcam"
    assert correct_speech("turn on web cam", is_final=True) == "Turn On Webcam"


def test_already_correct_recognition_is_preserved():
    for phrase in ("Open Webcam", "Open YouTube", "Hello Jarvis"):
        assert correct_speech(phrase, is_final=True) == phrase


def test_free_form_speech_is_not_rewritten_or_truncated():
    assert correct_speech("what is the weather in new york", is_final=True) == \
        "what is the weather in new york"
    assert "tommorow" in correct_speech("weather report tommorow", is_final=True)


def test_correction_never_raises_on_garbage():
    for junk in ("", "   ", "!!!", "zzz qqq xyzzy", "a"):
        assert isinstance(correct_speech(junk, is_final=True), str)


def test_live_transcript_renders_into_the_existing_textbox_not_a_label():
    assert "self._live_transcript_label" not in UI, \
        "the obsolete separate transcription widget must be fully removed"
    start = UI.index("def set_live_transcript(self, text: str, final: bool = False)")
    body = UI[start:start + 2200]
    assert "'input'" in body, "transcript must target the existing textbox"


def test_interim_transcript_replaces_and_is_corrected():
    start = MAIN.index("if sc.input_transcription")
    end = MAIN.index("# Server-side barge-in")
    block = MAIN[start:end]
    assert "_correct_speech" in block, "interim results must pass through correction"
    assert "set_live_transcript" in block


def test_final_result_is_corrected_and_sent_once():
    start = MAIN.index('full_in = " ".join(in_buf).strip()')
    end = MAIN.index("in_buf = []", start)
    block = MAIN[start:end]
    assert "self._correct_speech(_final_raw, final=True)" in block
    assert "send_client_content" not in block
    assert "_send_backend_command" not in block


def test_transcript_signal_carries_final_flag():
    assert "_live_transcript_sig = pyqtSignal(str, bool)" in UI
