"""Wiring regressions that are cheaper and clearer to assert at the source.

These pin the exact behaviours that were broken or newly required, in the file
that owns them, without standing up a Gemini Live session or the Qt HUD.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
UI = (ROOT / "ui.py").read_text(encoding="utf-8")


# ── startup greeting ──────────────────────────────────────────────────────────

def test_greeting_turn_is_protected_from_automated_interruption():
    assert "_greeting_in_flight" in MAIN
    # A voice (mic echo / barge-in) interrupt is ignored while it is in flight.
    assert "self._greeting_in_flight" in MAIN
    assert "startup greeting in flight" in MAIN


def test_greeting_is_logged_only_after_it_is_handed_to_the_renderer():
    start = MAIN.index("async def _send_startup_briefing")
    end = MAIN.index("def _greeting_release_later")
    body = MAIN[start:end]
    # The renderer is resolved once, and the mode check precedes the log line.
    assert "_mode = self.voice_mode()" in body
    assert body.index("_mode = self.voice_mode()") < body.index(
        'write_log(f"{self._asst_name}: {greeting}")')
    # Each mode hands the whole line to exactly one renderer.
    assert "_svc.speak(greeting)" in body          # gemini_piper
    assert "self.speak_local, args=(greeting,)" in body  # local
    assert "send_client_content" in body           # gemini native


def test_a_failed_greeting_send_is_not_reported_as_spoken():
    start = MAIN.index("async def _send_startup_briefing")
    end = MAIN.index("def _greeting_release_later")
    body = MAIN[start:end]
    # The failure branch must say it could not be spoken and return.
    assert "Greeting could not be spoken" in body


def test_duplicate_suppression_is_exact_match_only():
    """A substring test used to swallow the second sentence of the greeting."""
    assert "in _sg.strip()" not in MAIN
    assert "full_out.strip() == _sg.strip()" in MAIN


# ── chat activity indicator ───────────────────────────────────────────────────

def test_transcript_fragments_do_not_clear_the_activity_label():
    start = MAIN.index("if sc.output_transcription")
    end = MAIN.index("if sc.input_transcription")
    assert "clear_chat_activity" not in MAIN[start:end], (
        "transcript fragments must not hide the real activity mid-task")


def test_thinking_is_set_from_real_speech_input():
    assert 'set_chat_activity("Thinking")' in MAIN


def test_coding_and_reviewing_are_real_activity_labels():
    """Activity rows carry the ROUTED CATEGORY vocabulary (the same labels
    the Laya decision AI produces), not the legacy free-form strings."""
    assert "'code_helper'" in MAIN
    assert "'CODE'" in MAIN and "'REVIEW'" in MAIN
    assert "'SEARCH'" in MAIN and "'AUTOMATE'" in MAIN
    # Tools without a static mapping still label honestly through the Laya
    # classification fallback — never an invented row.
    assert "laya_decision.last_classification()" in MAIN


def test_the_activity_label_is_driven_by_the_gui_thread():
    assert "def set_chat_activity" in UI
    assert "_chat_activity_sig" in UI
    assert "def clear_chat_activity" in UI


def test_background_ui_updates_survive_a_closed_window():
    """The backend's daemon threads outlive the HUD at shutdown. Emitting on a
    deleted MainWindow used to raise RuntimeError out of JarvisLive.run() and
    be logged as a backend failure; every thread-facing UI update now goes
    through a guard that drops the update instead of killing the process."""
    start = UI.index("def _post(self, fn, *args, **kw):")
    body = UI[start:start + 900]
    assert "except RuntimeError" in body
    facade = UI[start:]                      # the thread-facing facade only
    for method in ("def set_state(", "def write_log(", "def set_chat_activity(",
                   "def set_live_transcript(", "def set_laya_status(",
                   "def clear_doing(", "def set_doing("):
        at = facade.index(method)
        assert "self._post(" in facade[at:at + 260], f"{method} bypasses the guard"


# ── long-text formatting ──────────────────────────────────────────────────────

def test_chat_messages_are_formatted_into_paragraphs():
    assert "def format_chat_paragraphs(" in UI
    start = UI.index("def append_msg(self, who: str, text: str, you: bool = False):")
    body = UI[start:start + 1200]
    assert "format_chat_paragraphs(body)" in body


# ── malware removal confirmation ──────────────────────────────────────────────

def test_malware_removal_goes_through_the_ui_confirmation_gate():
    start = MAIN.index('elif name == "malware_scanner"')
    end = MAIN.index('elif action == "status"')
    body = MAIN[start:end]
    assert "confirm.request(" in body
    assert "malware_remove" in body
    # The model-authored flag is no longer read as authorization.
    assert 'args.get("confirmed"' not in body


def test_malware_tool_description_forbids_false_claims():
    start = MAIN.index('"name": "malware_scanner"')
    body = MAIN[start:start + 900]
    assert "Never claim malware was found" in body
    assert "never claim the device is clean" in body
