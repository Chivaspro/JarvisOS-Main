"""Music must play in the app the user actually owns.

The complaint that started this: "I said play music and it opened the internal
WebView and Chrome — I already have Spotify." `play_music` had exactly two
destinations and could not see installed applications at all, so a machine with
Spotify installed got a web page.

These tests pin the new routing: detect the installed player (probed, never
assumed), open it on its own search URI, drive it with the media keys, report
only what was verified, and fall back honestly — with a reason — when no player
can serve the request.

Nothing here touches a real player, a real registry key or the screen: every
probe is monkeypatched, so the suite stays headless and side-effect free.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from capabilities import media  # noqa: E402

MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
SETTINGS_SOURCE = (ROOT / "ui_settings.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_real_config(monkeypatch):
    """Keep the suite independent of whatever this machine has saved."""
    monkeypatch.setattr(media, "_setting", lambda key, default=None: default)


# ── opening the right thing ───────────────────────────────────────────────

def test_spotify_target_is_a_search_uri_with_an_encoded_query():
    target = media.playback_target("spotify", "bohemian rhapsody")

    assert target == "spotify:search:bohemian%20rhapsody"
    assert target.startswith("spotify:")


def test_query_characters_that_would_break_the_uri_are_encoded():
    target = media.playback_target("spotify", "AC/DC & friends? #1")

    assert target.startswith("spotify:search:")
    assert " " not in target and "/" not in target[len("spotify:search:"):]
    assert "%2F" in target and "%26" in target


def test_a_player_with_no_search_scheme_falls_back_to_its_executable(monkeypatch):
    monkeypatch.setattr(media, "installed", lambda app: r"C:\vlc\vlc.exe")

    assert media.playback_target("vlc", "anything") == r"C:\vlc\vlc.exe"


def test_unknown_app_has_no_target():
    assert media.playback_target("winamp", "song") == ""


# ── detection ─────────────────────────────────────────────────────────────

def test_installed_finds_a_player_on_path(monkeypatch):
    monkeypatch.setattr(media.shutil, "which",
                        lambda exe: r"C:\apps\Spotify.exe" if exe == "spotify.exe" else None)

    assert media.installed("spotify") == r"C:\apps\Spotify.exe"


def test_installed_falls_back_to_the_registry(monkeypatch):
    # Neutralise the other two probes so this test proves the registry step
    # (and stays independent of whether this machine really has Spotify).
    monkeypatch.setattr(media.shutil, "which", lambda exe: None)
    monkeypatch.setitem(media.APPS["spotify"], "install_paths", ())
    monkeypatch.setattr(media, "_app_paths",
                        lambda exe: r"C:\Store\Spotify\Spotify.exe")

    assert media.installed("spotify") == r"C:\Store\Spotify\Spotify.exe"


def test_installed_is_empty_for_an_unknown_app(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda exe: None)
    monkeypatch.setattr(media, "_app_paths", lambda exe: "")

    assert media.installed("winamp") == ""


def test_installed_apps_lists_only_what_is_really_there(monkeypatch):
    monkeypatch.setattr(media, "installed",
                        lambda app: r"C:\apps\spotify.exe" if app == "spotify" else "")

    assert media.installed_apps() == {"spotify": r"C:\apps\spotify.exe"}


# ── which player serves the request ───────────────────────────────────────

def test_preferred_app_honours_an_explicit_choice(monkeypatch):
    monkeypatch.setattr(media, "installed", lambda app: "")

    # Named explicitly: honoured even when it is missing, so the caller reports
    # "not installed" instead of silently substituting something else.
    assert media.preferred_app("vlc") == "vlc"


def test_preferred_app_honours_the_saved_preference(monkeypatch):
    monkeypatch.setattr(media, "_setting", lambda key, default=None: "vlc")
    monkeypatch.setattr(media, "installed", lambda app: r"C:\apps\vlc.exe")

    assert media.preferred_app() == "vlc"


def test_preferred_app_uses_the_first_installed_player_in_order(monkeypatch):
    monkeypatch.setattr(media, "installed",
                        lambda app: r"C:\vlc\vlc.exe" if app == "vlc" else "")

    assert media.preferred_app() == "vlc"


def test_preferred_app_is_empty_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(media, "installed", lambda app: "")

    assert media.preferred_app() == ""


def test_status_reports_what_was_probed(monkeypatch):
    monkeypatch.setattr(media, "installed",
                        lambda app: r"C:\apps\spotify.exe" if app == "spotify" else "")
    monkeypatch.setattr(media, "running", lambda app: app == "spotify")

    state = media.status()

    assert state["available"] is True
    assert state["preferred"] == "spotify"
    assert state["running"] == ["spotify"]
    assert "Spotify" in state["detail"]


# ── play(): what it does, and what it refuses to claim ────────────────────

def _arm_play(monkeypatch, *, window=True, how="window", key_ok=True,
              was_running=False, state="", path=r"C:\apps\Spotify.exe",
              session=False, accepted=True, after="playing"):
    """Fakes for the whole playback path; returns the calls it observed.

    ``session=False`` is the default because most players have no OS media
    session in a headless test box; that is the branch where the global media
    key is the fallback. Pass ``session=True`` to exercise the media-session path.
    """
    calls: list = []
    monkeypatch.setattr(media, "installed", lambda app: path)
    monkeypatch.setattr(media, "running", lambda app: was_running)
    monkeypatch.setattr(media, "_open",
                        lambda target, exe="": calls.append(("open", target)) or True)
    monkeypatch.setattr(media, "_wait_for_open",
                        lambda spec, app, timeout: (window, how if window else ""))
    monkeypatch.setattr(media, "_send_key",
                        lambda vk: calls.append(("key", vk)) or key_ok)
    # The OS playback probe is never run for real here: it would touch the
    # machine's real media session (and take a PowerShell round trip).
    monkeypatch.setattr(media, "playback_state", lambda app: state)
    monkeypatch.setattr(media, "start_playback",
                        lambda app: {"session": session, "requested": session,
                                     "accepted": bool(accepted) if session else False,
                                     "state": after if session else ""})
    return calls


def test_play_opens_the_installed_player_on_the_search_uri(monkeypatch):
    calls = _arm_play(monkeypatch)

    result = media.play("daft punk")

    assert result["ok"] is True
    assert result["controller"] == "media"
    assert ("open", "spotify:search:daft%20punk") in calls
    assert ("key", media.MEDIA_KEYS["play_pause"]) in calls
    assert result["evidence"]["window_confirmed"] is True
    assert result["evidence"]["searched"] is True


def test_play_reports_that_the_exact_track_cannot_be_confirmed(monkeypatch):
    _arm_play(monkeypatch)

    detail = media.play("daft punk")["detail"]

    # The honesty rule: an action may never be reported as more than it verified.
    assert "cannot be confirmed" in detail
    assert "Spotify" in detail


def test_play_does_not_send_a_key_when_the_window_never_appeared(monkeypatch):
    calls = _arm_play(monkeypatch, window=False)

    result = media.play("daft punk")

    assert result["ok"] is True, "the app was opened; only the window was unconfirmed"
    assert not [c for c in calls if c[0] == "key"], "no transport key without evidence"
    assert "did not appear" in result["detail"]
    assert result["evidence"]["confirmed"] is False


def test_play_says_when_the_player_was_already_running(monkeypatch):
    _arm_play(monkeypatch, was_running=True)

    assert "already running" in media.play("daft punk")["detail"]


def test_play_never_pauses_a_player_that_is_already_playing(monkeypatch):
    """The media key is a toggle; "play" must not interrupt live music."""
    calls = _arm_play(monkeypatch, was_running=True, state="playing")

    result = media.play("daft punk")

    assert not [c for c in calls if c[0] == "key"]
    assert "already playing" in result["detail"]
    assert result["evidence"]["playback_state"] == "playing"


def test_play_starts_a_player_windows_reports_as_paused(monkeypatch):
    calls = _arm_play(monkeypatch, was_running=True, state="paused")

    result = media.play("daft punk")

    assert ("key", media.MEDIA_KEYS["play_pause"]) in calls
    assert "paused" in result["detail"]


def test_play_leaves_playback_alone_when_the_state_cannot_be_read(monkeypatch):
    calls = _arm_play(monkeypatch, was_running=True, state="")

    result = media.play("daft punk")

    assert not [c for c in calls if c[0] == "key"]
    assert "could not report its playback state" in result["detail"]
    assert result["evidence"]["playback_state"] == "unknown"


def test_play_starts_a_player_it_just_launched_even_without_a_state(monkeypatch):
    calls = _arm_play(monkeypatch, was_running=False, state="")

    media.play("daft punk")

    assert ("key", media.MEDIA_KEYS["play_pause"]) in calls


# ── driving playback through the OS media session ──────────────────────────

def test_play_starts_playback_through_the_os_media_session(monkeypatch):
    calls = _arm_play(monkeypatch, session=True, accepted=True, after="playing",
                      state="paused", was_running=True)

    result = media.play("daft punk")

    assert not [c for c in calls if c[0] == "key"]
    assert result["evidence"]["playback_started"] is True
    assert "it now reports Spotify as playing" in result["detail"]


def test_play_reports_when_the_request_played_nothing(monkeypatch):
    """Spotify's search page has no track queued, and saying it started would be a lie."""
    _arm_play(monkeypatch, session=True, accepted=True, after="paused", state="paused")

    result = media.play("daft punk")

    assert result["evidence"]["playback_started"] is False
    assert "nothing queued" in result["detail"]


def test_play_reports_a_request_the_player_refused(monkeypatch):
    _arm_play(monkeypatch, session=True, accepted=False, after="paused", state="paused")

    assert "refused the play request" in media.play("daft punk")["detail"]


# ── reading the OS playback state (no extra dependency) ──────────────────

def test_media_session_output_is_parsed(monkeypatch):
    class _Proc:
        stdout = ("session: yes\r\nresult: True\r\nstate: Playing\r\n"
                  "#< CLIXML\r\n"
                  '<Objs Version="1.1.0.1" xmlns="x">noise</Objs>')

    monkeypatch.setattr(media, "_WIN", True)
    monkeypatch.setattr(media.shutil, "which",
                        lambda name: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: _Proc())

    assert media._smtc_query("spotify.exe", "TryPlayAsync") == {
        "session": True, "requested": True, "accepted": True,
        "state": "playing"}


def test_media_session_output_without_a_session_is_reported_as_such(monkeypatch):
    class _Proc:
        stdout = "session: no\r\n"

    monkeypatch.setattr(media, "_WIN", True)
    monkeypatch.setattr(media.shutil, "which", lambda name: "powershell.exe")
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: _Proc())

    assert media._smtc_query("vlc.exe", "TryPlayAsync") == {
        "session": False, "requested": False, "accepted": None, "state": ""}


def test_playback_state_looks_up_that_players_session(monkeypatch):
    seen: list = []
    monkeypatch.setattr(media, "running", lambda app: True)
    monkeypatch.setattr(media, "_smtc_query",
                        lambda match, method="": seen.append((match, method)) or {
                            "session": True, "requested": False,
                            "accepted": None, "state": "Playing"})

    assert media.playback_state("spotify") == "playing"
    assert seen == [("spotify.exe", "")]


def test_playback_state_is_unknown_when_the_player_is_not_running(monkeypatch):
    monkeypatch.setattr(media, "running", lambda app: False)
    monkeypatch.setattr(media, "_smtc_query",
                        lambda *a, **k: pytest.fail("a stopped player needs no probe"))

    assert media.playback_state("spotify") == ""


def test_a_playback_probe_that_cannot_run_is_unknown_not_a_crash(monkeypatch):
    monkeypatch.setattr(media, "_WIN", True)
    monkeypatch.setattr(media.shutil, "which", lambda name: "")

    assert media._smtc_query("spotify.exe") == {
        "session": False, "requested": False, "accepted": None, "state": ""}


def test_play_without_a_query_is_refused(monkeypatch):
    _arm_play(monkeypatch)

    result = media.play("   ")

    assert result["ok"] is False
    assert result["reason"] == "no_query"


def test_play_with_no_installed_player_explains_itself(monkeypatch):
    monkeypatch.setattr(media, "installed", lambda app: "")

    result = media.play("daft punk")

    assert result["ok"] is False
    assert result["reason"] == "no_native_media_app"
    assert "No installed music application" in result["detail"]


def test_play_naming_a_missing_app_says_it_is_missing(monkeypatch):
    monkeypatch.setattr(media, "installed", lambda app: "")

    result = media.play("daft punk", app="spotify")

    assert result["ok"] is False
    assert result["reason"] == "media_app_missing"
    assert "not installed" in result["detail"]


def test_play_reports_a_launch_that_failed(monkeypatch):
    _arm_play(monkeypatch)
    monkeypatch.setattr(media, "_open", lambda target, exe="": False)

    result = media.play("daft punk")

    assert result["ok"] is False
    assert result["reason"] == "launch_failed"


# ── confirming the player is really up ────────────────────────────────────

class _FakeDesktop:
    """A stand-in for capabilities.desktop's window enumeration."""

    def __init__(self, window=None, raise_error=False):
        self.window = window
        self.raise_error = raise_error
        self.calls = 0

    def find_window(self, app="", title=""):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("no UIA backend")
        return (self.window, "" if self.window else "not found")


def _inject_desktop(monkeypatch, fake):
    """Replace ``capabilities.desktop`` for one test, whatever ran before it.

    ``from capabilities import desktop`` reads the attribute the package already
    carries when the real module was imported earlier in the session, so faking
    ``sys.modules`` alone is not enough — the package attribute has to be
    replaced too, or these tests would pass alone and fail in a full run.
    """
    module = types.ModuleType("capabilities.desktop")
    module.find_window = fake.find_window
    monkeypatch.setitem(sys.modules, "capabilities.desktop", module)
    import capabilities
    monkeypatch.setattr(capabilities, "desktop", module, raising=False)
    return fake


def test_window_found_is_reported_as_a_window(monkeypatch):
    _inject_desktop(monkeypatch, _FakeDesktop(window=object()))
    monkeypatch.setattr(media, "running", lambda app: False)

    assert media._wait_for_open(media.APPS["spotify"], "spotify", 1.0) == (True, "window")


def test_a_title_that_never_matches_does_not_block_playback(monkeypatch):
    """Spotify's window title is the current track, not "Spotify".

    So the process signal has to be enough — otherwise the play key would only
    ever be sent after the full timeout, and never at all on a matching miss.
    """
    desktop = _inject_desktop(monkeypatch, _FakeDesktop(window=None))
    monkeypatch.setattr(media, "running", lambda app: True)
    monkeypatch.setattr(media, "_WINDOW_GRACE_S", 0.05)

    started = time.monotonic()
    confirmed, how = media._wait_for_open(media.APPS["spotify"], "spotify", 12.0)
    elapsed = time.monotonic() - started

    assert (confirmed, how) == (True, "process")
    assert elapsed < 12.0, "confirming on the process must not wait out the window budget"
    assert desktop.calls >= 1


def test_nothing_up_is_reported_as_unconfirmed(monkeypatch):
    _inject_desktop(monkeypatch, _FakeDesktop(window=None))
    monkeypatch.setattr(media, "running", lambda app: False)

    assert media._wait_for_open(media.APPS["spotify"], "spotify", 0.1) == (False, "")


def test_a_broken_window_probe_falls_back_to_the_process(monkeypatch):
    _inject_desktop(monkeypatch, _FakeDesktop(raise_error=True))
    monkeypatch.setattr(media, "running", lambda app: True)
    monkeypatch.setattr(media, "_WINDOW_GRACE_S", 0.05)

    assert media._wait_for_open(media.APPS["spotify"], "spotify", 0.2) == (True, "process")


def test_without_any_window_backend_the_process_is_the_evidence(monkeypatch):
    # No backend means the import itself must fail, so the package attribute
    # the real module may have left behind is removed for this test too.
    monkeypatch.setitem(sys.modules, "capabilities.desktop", None)
    import capabilities
    monkeypatch.delattr(capabilities, "desktop", raising=False)
    monkeypatch.setattr(media, "running", lambda app: app == "spotify")

    assert media._wait_for_open(media.APPS["spotify"], "spotify", 1.0) == (True, "process")


# ── transport keys ────────────────────────────────────────────────────────

def test_transport_sends_the_media_key_for_each_action(monkeypatch):
    sent: list = []
    monkeypatch.setattr(media, "_send_key", lambda vk: sent.append(vk) or True)

    for name in ("play_pause", "next", "previous", "stop", "volume_up",
                 "volume_down", "mute"):
        assert media.transport(name)["ok"] is True

    assert sent == [media.MEDIA_KEYS[n] for n in
                    ("play_pause", "next", "previous", "stop", "volume_up",
                     "volume_down", "mute")]


def test_transport_understands_spoken_aliases(monkeypatch):
    sent: list = []
    monkeypatch.setattr(media, "_send_key", lambda vk: sent.append(vk) or True)

    media.transport("skip")
    media.transport("louder")
    media.transport("pause")

    assert sent == [media.MEDIA_KEYS["next"], media.MEDIA_KEYS["volume_up"],
                    media.MEDIA_KEYS["play_pause"]]


def test_transport_names_an_action_it_does_not_know(monkeypatch):
    monkeypatch.setattr(media, "_send_key",
                        lambda vk: pytest.fail("no key for an unknown action"))

    result = media.transport("rewind")

    assert result["ok"] is False
    assert result["reason"] == "unknown_media_action"


def test_transport_reports_a_key_that_could_not_be_sent(monkeypatch):
    monkeypatch.setattr(media, "_send_key", lambda vk: False)

    result = media.transport("next")

    assert result["ok"] is False
    assert result["reason"] == "media_key_failed"


# ── wiring: the tool asks the installed player FIRST ──────────────────────

def test_play_music_consults_the_installed_player_before_the_webview():
    start = MAIN_SOURCE.index('elif name == "play_music":')
    end = MAIN_SOURCE.index('elif name == "brain_mode":', start)
    block = MAIN_SOURCE[start:end]

    assert "capabilities import media" in block
    assert "_media.play(query)" in block
    assert block.index("_media.play(query)") < block.index("play_web_music"), (
        "the installed player must be tried before the internal WebView player"
    )
    # The fallback must explain itself rather than switch silently.
    assert "no installed player was used" in block


def test_the_native_music_switch_is_gated_and_settings_writes_it():
    start = MAIN_SOURCE.index('elif name == "play_music":')
    end = MAIN_SOURCE.index('elif name == "brain_mode":', start)
    # The gate the backend reads...
    assert 'self.ui.feature_enabled("native_music_apps")' in MAIN_SOURCE[start:end]
    # ...is the same key the Settings checkbox saves, so the switch really works.
    assert '"native_music_apps": True' in SETTINGS_SOURCE
    assert 'features["native_music_apps"] = self._native_music.isChecked()' in SETTINGS_SOURCE


def test_the_tool_description_no_longer_promises_only_the_webview():
    start = MAIN_SOURCE.index('"name": "play_music"')
    block = MAIN_SOURCE[start:start + 900]

    assert "INSTALLED" in block
    assert "Spotify" in block
