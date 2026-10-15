"""Native media apps first — real players beat a web page.

"Play music" had exactly two destinations: the internal WebView, or the YouTube
action. A machine with Spotify installed therefore got a browser page instead of
the software the user actually owns, because nothing in that path could see
installed applications at all — even though the desktop controller already
resolved ``spotify.exe`` for ``open_application``.

This controller closes that gap and nothing else:

* it probes which media applications are really installed (PATH, the usual
  install directories, and the Windows ``App Paths`` registry key) and which are
  already running;
* it starts or focuses the chosen player through that player's own URI scheme
  (``spotify:search:<query>``), waits for its real window, then — only when the
  player is not already playing — uses the Windows media keys, which every
  player understands;
* it says exactly what it did, and exactly what it could NOT verify.

Why the playback check exists
-----------------------------
The play/pause key is a TOGGLE. Sending it to a player that is already playing
the user's music pauses it — the assistant would be interrupting the very thing
it was asked to do. Windows' own media session manager answers that one question (``playback_state``)
and can start playback too (``start_playback``), through PowerShell, so no extra
Python package is needed. The manager also reports the state a moment AFTER the
request, which is what lets the result say "it started" instead of assuming a
synthesized key was heard. When the platform offers no session at all, the key is
only sent when the state is known or the player was just started (a fresh player
cannot have been playing), and the result says so.

Verification honesty
--------------------
The media keys are the only transport a desktop player exposes without a
private API. Spotify's Web API could start an exact track, but that needs OAuth
credentials this project does not hold, so "search URI + play key" is the
honest best effort — and the result string says that instead of claiming the
exact track started. Nothing is reported as open unless the player's window (or
at least its process) was confirmed, and a transport key is only sent for a
request that asked for playback.

Design rules follow the rest of ``capabilities/``: every public function returns
a dict, nothing raises, no secrets, and the model's words are only ever used as
data — an application key, a search string, an action name.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

_WIN = sys.platform == "win32"

#: How long we wait for a player to come up after asking it to open something.
WINDOW_WAIT_S = 12.0
#: How long the window itself is preferred over the (always available) process
#: signal, before confirming on the process and getting on with playback.
_WINDOW_GRACE_S = 2.5
_WINDOW_POLL_S = 0.4

#: One entry per player J.A.R.V.I.S. can drive. ``search_uri`` is that player's
#: own URI scheme — the difference between "opened the app" and "opened the thing
#: the user asked for", and it needs no API key.
APPS: dict[str, dict] = {
    "spotify": {
        "label": "Spotify",
        "exe": "spotify.exe",
        "search_uri": "spotify:search:{query}",
        "processes": ("spotify.exe",),
        "install_paths": (
            r"%APPDATA%\Spotify\Spotify.exe",
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe",
            r"%PROGRAMFILES%\Spotify\Spotify.exe",
        ),
        "window": "spotify",
    },
    "vlc": {
        "label": "VLC",
        "exe": "vlc.exe",
        # VLC registers no search protocol handler: it can be opened and driven
        # with the media keys, but it cannot be handed a query.
        "search_uri": "",
        "processes": ("vlc.exe",),
        "install_paths": (
            r"%PROGRAMFILES%\VideoLAN\VLC\vlc.exe",
            r"%PROGRAMFILES(X86)%\VideoLAN\VLC\vlc.exe",
        ),
        "window": "vlc",
    },
}

#: Preference order when the user has not chosen an application.
DEFAULT_ORDER: tuple[str, ...] = ("spotify", "vlc")

#: The Windows media keys every player answers. Volume and mute are handled by
#: the OS itself, which is why volume control needs no player support at all.
MEDIA_KEYS: dict[str, int] = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}

_KEYEVENTF_KEYUP = 0x0002

#: Windows' media session manager, queried through PowerShell so that reading the
#: playback state and requesting playback cost no extra dependency. It is the
#: difference between "start the music" and "pause whatever is already on".
#: ``@@MATCH@@`` is filled with a process name from :data:`APPS` and ``@@ACTION@@``
#: with a method name from :data:`_SMTC_METHODS` — both are our own constants, so
#: nothing from the user ever reaches this script.
_SMTC_TEMPLATE = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($op, $t) { $m = $ext.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); [void]$task.Wait(4000); $task.Result }
$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$mgr = Await ($mgrType::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$s = $mgr.GetSessions() | Where-Object { $_.SourceAppUserModelId -like '*@@MATCH@@*' } | Select-Object -First 1
if ($null -eq $s) { Write-Output 'session: no' } else {
  Write-Output 'session: yes'
@@ACTION@@  Write-Output ("state: {0}" -f $s.GetPlaybackInfo().PlaybackStatus)
}
"""

#: Filled into ``@@ACTION@@`` when playback is actually requested. The settle
#: delay lets the player react before the state is read back, so the caller can
#: tell "started" apart from "the request was accepted but nothing played".
_SMTC_ACTION = ("  $r = Await ($s.@@METHOD@@()) ([bool])\n"
                "  Write-Output (\"result: {0}\" -f $r)\n"
                "  Start-Sleep -Milliseconds 900\n")

#: Our transport words → the media session manager's methods.
_SMTC_METHODS = {"play": "TryPlayAsync", "pause": "TryPauseAsync",
                 "play_pause": "TryTogglePlayPauseAsync", "next": "TrySkipNextAsync",
                 "previous": "TrySkipPreviousAsync", "stop": "TryStopAsync"}

#: How long the media session probe may take before the state is reported as
#: unknown (the PowerShell round trip measures ~0.6s when it works).
_SMTC_TIMEOUT_S = 8.0

#: Playback states the manager reports that mean "the user is listening now".
_PLAYING_STATES = ("playing",)


def _no_window() -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _WIN else 0
    return {"creationflags": flags} if flags else {}


def _fail(reason: str, detail: str, **evidence) -> dict:
    return {"ok": False, "controller": "media", "reason": reason,
            "detail": str(detail), "evidence": dict(evidence)}


def _done(detail: str, app: str = "", **evidence) -> dict:
    return {"ok": True, "controller": "media", "reason": "", "detail": str(detail),
            "app": app, "evidence": dict(evidence)}


def _setting(key: str, default=None):
    """One config value, read through the project's single config reader."""
    try:
        from config.settings import get as _get
        return _get(key, default)
    except Exception:
        return default


# ── detection (probed, never assumed) ─────────────────────────────────────────

def _app_paths(exe: str) -> str:
    """The path Windows itself registered for *exe*, when it exists.

    ``App Paths`` is how an application that is installed anywhere (including
    the Microsoft Store, whose packages live in a versioned Program Files
    directory) can still be found by name.
    """
    if not _WIN:
        return ""
    try:
        import winreg
    except Exception:
        return ""
    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for root, access in ((winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
                         (winreg.HKEY_LOCAL_MACHINE,
                          winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)),
                         (winreg.HKEY_LOCAL_MACHINE,
                          winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0))):
        try:
            with winreg.OpenKey(root, subkey, 0, access) as handle:
                value, _kind = winreg.QueryValueEx(handle, "")
            path = str(value or "").strip().strip('"')
            if path and Path(path).is_file():
                return path
        except OSError:
            continue
        except Exception:
            continue
    return ""


def installed(app: str) -> str:
    """The executable path of one known player, or "" when it is not installed."""
    spec = APPS.get(str(app or "").strip().lower())
    if not spec:
        return ""
    found = shutil.which(spec["exe"])
    if found:
        return str(found)
    for raw in spec.get("install_paths", ()):
        try:
            candidate = Path(os.path.expandvars(str(raw)))
            if candidate.is_file():
                return str(candidate)
        except Exception:
            continue
    return _app_paths(spec["exe"])


def installed_apps() -> dict[str, str]:
    """Every known player that is really on this machine: ``{key: path}``."""
    return {key: path for key in APPS if (path := installed(key))}


def _process_names() -> set[str]:
    try:
        import psutil
        names = set()
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
            except Exception:
                name = None
            if name:
                names.add(str(name).lower())
        return names
    except Exception:
        pass
    try:
        proc = subprocess.run(["tasklist"], capture_output=True, text=True,
                              timeout=10.0, **_no_window())
        names = set()
        for line in (proc.stdout or "").splitlines()[1:]:
            parts = line.split()
            if parts:
                names.add(parts[0].lower())
        return names
    except Exception:
        return set()


def running(app: str) -> bool:
    """True when that player's process is up right now."""
    spec = APPS.get(str(app or "").strip().lower())
    if not spec:
        return False
    names = _process_names()
    return any(str(proc).lower() in names for proc in spec.get("processes", ()))


def preferred_app(explicit: str = "") -> str:
    """Which player should serve a music request.

    Explicit choice first (so a named app is honoured even when it turns out to
    be missing — that produces an honest "not installed" rather than a silent
    substitution), then the saved preference, then the first installed player in
    :data:`DEFAULT_ORDER`.
    """
    name = str(explicit or "").strip().lower()
    if name in APPS:
        return name
    if not name:
        saved = str(_setting("media_app", "") or "").strip().lower()
        if saved in APPS:
            return saved
    for key in DEFAULT_ORDER:
        if installed(key):
            return key
    for key in APPS:                      # anything the registry knows about
        if key not in DEFAULT_ORDER and installed(key):
            return key
    return ""


def status() -> dict:
    """What the media layer can really do on this machine (probed)."""
    found = installed_apps()
    live = [key for key in found if running(key)]
    preferred = preferred_app()
    if not found:
        detail = ("no native music application was found (checked "
                  + ", ".join(APPS[key]["label"] for key in APPS)
                  + ") — music falls back to the internal player")
    else:
        detail = ("installed: " + ", ".join(APPS[k]["label"] for k in found)
                  + f"; preferred: {APPS[preferred]['label'] if preferred else 'none'}"
                  + (f"; running: {', '.join(APPS[k]['label'] for k in live)}" if live else ""))
    return {"available": bool(found), "installed": found, "running": live,
            "preferred": preferred, "media_keys": _WIN, "detail": detail}


# ── playback ─────────────────────────────────────────────────────────────────

def playback_target(app: str, query: str) -> str:
    """The URI (preferred) or executable that opens *query* in *app*.

    A player that registers a search protocol handler gets the query itself, so
    the results are on screen when its window appears; one that does not gets
    launched plainly, and that difference is reported to the caller.
    """
    spec = APPS.get(str(app or "").strip().lower())
    if not spec:
        return ""
    template = str(spec.get("search_uri") or "")
    if template:
        return template.format(query=quote(" ".join(str(query or "").split()), safe=""))
    return installed(app) or str(spec.get("exe") or "")


def _open(target: str, exe: str = "") -> bool:
    """Open an application URI, falling back to launching its executable."""
    if not _WIN:
        return False
    if target and exe and Path(target).is_file():
        try:
            subprocess.Popen([target], close_fds=True, **_no_window())
            return True
        except Exception:
            pass
    for attempt in (target, exe):
        if not attempt:
            continue
        try:
            os.startfile(attempt)          # type: ignore[attr-defined]
            return True
        except Exception:
            continue
    return False


def _send_key(vk: int) -> bool:
    """Press and release one Windows virtual key (the media keys are global)."""
    if not _WIN:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.keybd_event(int(vk), 0, 0, 0)
        user32.keybd_event(int(vk), 0, _KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


def _smtc_query(match: str, method: str = "") -> dict:
    """One PowerShell round trip to the Windows media session manager.

    Returns ``{"session": bool, "requested": bool, "accepted": bool|None,
    "state": str}``. ``session`` False means the platform knows no media session
    for this player; ``state`` is the manager's status (``playing``, ``paused``,
    ``stopped``) or ``""`` when it could not be read. Nothing here raises.
    """
    blank = {"session": False, "requested": False, "accepted": None,
             "state": ""}
    if not _WIN:
        return blank
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return blank
    action = ""
    if method:
        if method not in _SMTC_METHODS.values():
            return blank
        action = _SMTC_ACTION.replace("@@METHOD@@", method)
    script = (_SMTC_TEMPLATE.replace("@@MATCH@@", str(match or ""))
              .replace("@@ACTION@@", action))
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    except Exception:
        return blank
    try:
        proc = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-OutputFormat", "Text", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=_SMTC_TIMEOUT_S, **_no_window())
    except Exception:
        return blank
    answer = dict(blank)
    for line in str(proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("#<") or line.startswith("<Objs"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip().lower()
        if key == "session":
            answer["session"] = value == "yes"
        elif key == "result":
            answer["accepted"] = value == "true"
            answer["requested"] = True
        elif key == "state":
            answer["state"] = value
    return answer


def _app_match(app: str) -> str:
    """The process name used to find that player's media session."""
    spec = APPS.get(str(app or "").strip().lower()) or {}
    names = spec.get("processes") or (spec.get("exe") or "",)
    return str(names[0]) if names else ""


def playback_state(app: str) -> str:
    """``playing``, ``paused`` or ``stopped`` for one player; ``""`` if unknown.

    The one fact the media keys cannot report: whether sending play/pause would
    start the music or pause it.
    """
    if not APPS.get(str(app or "").strip().lower()) or not running(app):
        return ""
    return str(_smtc_query(_app_match(app)).get("state") or "").lower()


def start_playback(app: str) -> dict:
    """Ask the OS media session to start one player. Never raises.

    ``{"session": bool, "requested": bool, "accepted": bool, "state": str}`` —
    ``session`` False means there is no media session to drive, so the caller may
    fall back to the global media key. ``state`` is what the manager reported a
    moment after the request, which is what makes "it started" verifiable.
    """
    if not APPS.get(str(app or "").strip().lower()) or not running(app):
        return {"session": False, "requested": False, "accepted": False, "state": ""}
    answer = _smtc_query(_app_match(app), _SMTC_METHODS["play"])
    return {"session": bool(answer["session"]),
            "requested": bool(answer["requested"]),
            "accepted": answer["accepted"] is True,
            "state": str(answer["state"] or "").lower()}


def _wait_for_open(spec: dict, app: str, timeout: float) -> tuple[bool, str]:
    """Wait for the player to really be up. Returns ``(confirmed, how)``.

    ``how`` is ``"window"`` when the player's window was found through UIA, or
    ``"process"`` when only its process could be confirmed. ``""`` means neither.

    Both signals are polled, deliberately, because a player's window title is
    usually its CURRENT TRACK and not its own name: Spotify's title is
    "Artist - Song", so a title match can never be the only way to confirm it.
    The window is still preferred while it is plausible to find one, and the
    process is accepted a little later — waiting the full timeout on a title
    that will never match would stall the play key for no reason.
    """
    try:
        from capabilities import desktop
    except Exception:
        desktop = None                                  # type: ignore[assignment]

    if desktop is None:
        # No window backend installed at all: process liveness is the strongest
        # evidence available, and it is reported as exactly that.
        return (running(app), "process" if running(app) else "")

    name = str(spec.get("window") or spec.get("label") or "")
    start = time.monotonic()
    deadline = start + max(0.0, float(timeout))
    window_deadline = start + min(_WINDOW_GRACE_S, max(0.0, float(timeout)))
    process_up = False

    while True:
        try:
            window, _reason = desktop.find_window(title=name)
        except Exception:
            window = None
        if window is not None:
            return (True, "window")

        if not process_up and running(app):
            process_up = True

        now = time.monotonic()
        if process_up and now >= window_deadline:
            return (True, "process")
        if now >= deadline:
            break
        time.sleep(_WINDOW_POLL_S)

    # Nothing matched within the budget: say so rather than assuming.
    return (running(app), "process" if running(app) else "")


def _play_detail(spec: dict, query: str, was_running: bool, confirmed: bool,
                 how: str, searched: bool, state: str, request: dict,
                 key_sent: bool) -> str:
    label = spec["label"]
    opened = (f"{label} was already running, so I brought it forward"
              if was_running else f"I started {label}")
    if searched:
        action = f"{opened} on the search results for \u201c{query}\u201d"
    else:
        action = (f"{opened}; {label} registers no search handler, so \u201c{query}\u201d "
                  "could not be queued in it")
    if not confirmed:
        return (f"{action}, but its window did not appear within "
                f"{WINDOW_WAIT_S:.0f}s, so nothing further was sent.")
    where = "its window is on screen" if how == "window" else "its process is running"
    if state in _PLAYING_STATES:
        return (f"{action}, and {where}. {label} is already playing, so I left "
                "playback alone rather than pausing it.")
    if request.get("session"):
        after = str(request.get("state") or "")
        if after in _PLAYING_STATES:
            return (f"{action}, and {where}. I asked Windows to start playback and "
                    f"it now reports {label} as playing. Which track is playing "
                    "cannot be confirmed — the OS media session does not expose it "
                    "to this call.")
        if request.get("accepted"):
            return (f"{action}, and {where}. Windows accepted the play request, but "
                    f"{label} still reports {after or 'no playback state'} — its "
                    "search page has nothing queued, so pick a track (or ask me to "
                    "press play once one is selected).")
        return (f"{action}, and {where}. {label} refused the play request from "
                "Windows, so nothing was started.")
    if not key_sent:
        why = ("Windows could not report its playback state"
               if not state else "the play key was refused")
        return (f"{action}, and {where}. {why}, so I did not send the play key — "
                "say 'press play' or ask me to resume it.")
    knew = f"Windows reported it was {state}, so " if state else ""
    return (f"{action}, and {where}. No OS media session was available, so I "
            f"{knew}sent the global play/pause key; whether playback started cannot "
            "be confirmed from here.")


def play(query: str, app: str = "", verify: bool = True) -> dict:
    """Play *query* in an installed player, or explain why it could not.

    Never falls back to another engine itself: the caller decides what to do
    when no native player can serve the request, and the honest reason is in the
    returned ``detail``.
    """
    text = " ".join(str(query or "").split()).strip()
    if not text:
        return _fail("no_query", "Nothing to play: no song, artist or playlist was given.")

    key = preferred_app(app)
    if not key:
        known = ", ".join(APPS[k]["label"] for k in APPS)
        return _fail("no_native_media_app",
                     f"No installed music application was found (checked {known}). "
                     "Install one, or play it in the internal player instead.")
    spec = APPS[key]
    path = installed(key)
    if not path:
        return _fail("media_app_missing",
                     f"{spec['label']} is not installed on this PC, so nothing was opened.",
                     app=key, expected=spec["exe"])

    was_running = running(key)
    searched = bool(spec.get("search_uri"))
    target = playback_target(key, text)
    if not target:
        return _fail("launch_failed", f"No way to open {spec['label']} was found.",
                     app=key)
    if not _open(target, path):
        return _fail("launch_failed",
                     f"{spec['label']} could not be opened ({target}).", app=key)

    confirmed, how = (False, "")
    if verify:
        confirmed, how = _wait_for_open(spec, key, WINDOW_WAIT_S)

    # The play/pause key TOGGLES: sending it to a player that is already playing
    # would pause the user's music. So the state is read first where Windows can
    # report it. When the player is not playing, playback is requested through
    # the OS media session (which also reports whether anything actually began);
    # the global key is only the fallback for players Windows cannot drive, and
    # then only when the state is known or the player was just started.
    state = playback_state(key) if confirmed else ""
    request: dict = {}
    key_sent = False
    if confirmed and state not in _PLAYING_STATES:
        request = start_playback(key)
        if not request.get("session") and (state or not was_running):
            key_sent = _send_key(MEDIA_KEYS["play_pause"])

    after = str(request.get("state") or "")
    detail = _play_detail(spec, text, was_running, confirmed, how, searched, state,
                          request, key_sent)
    return _done(detail, app=key, target=target, path=path, was_running=was_running,
                 window_confirmed=(how == "window"), confirmed=confirmed,
                 confirmed_by=how, play_key_sent=key_sent, searched=searched,
                 playback_state=state or "unknown",
                 playback_requested=bool(request.get("requested")),
                 playback_accepted=bool(request.get("accepted")),
                 playback_state_after=after or "unknown",
                 playback_started=(after in _PLAYING_STATES))


def transport(action: str) -> dict:
    """One media transport key: ``play_pause``, ``next``, ``previous``, ``stop``,
    ``volume_up``, ``volume_down`` or ``mute``."""
    name = str(action or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {"pause": "play_pause", "play": "play_pause", "toggle": "play_pause",
               "skip": "next", "next_track": "next", "prev": "previous",
               "previous_track": "previous", "vol_up": "volume_up",
               "vol_down": "volume_down", "louder": "volume_up",
               "quieter": "volume_down", "unmute": "mute", "silence": "mute"}
    name = aliases.get(name, name)
    vk = MEDIA_KEYS.get(name)
    if vk is None:
        return _fail("unknown_media_action",
                     f"'{action}' is not a media action I support "
                     f"({', '.join(sorted(MEDIA_KEYS))}).")
    if not _send_key(vk):
        return _fail("media_key_failed",
                     "The Windows media key could not be sent on this system.")
    return _done(f"Sent the {name.replace('_', ' ')} media key.", action=name)


def run(action_name: str, args: dict) -> dict:
    """Entry point shaped like the other controllers (``desktop.run``)."""
    args = dict(args or {})
    name = str(action_name or "").strip().lower()
    if name == "play":
        return play(str(args.get("query") or args.get("text") or ""),
                    app=str(args.get("app") or ""))
    return transport(name)


__all__ = ["APPS", "DEFAULT_ORDER", "MEDIA_KEYS", "WINDOW_WAIT_S", "installed",
           "installed_apps", "play", "playback_state", "playback_target",
           "preferred_app", "running", "start_playback", "status", "transport",
           "run"]
