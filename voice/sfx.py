"""Futuristic JARVIS SFX — MP3 assets first, synth fallback second.

Assets (in <app>/assets): "UI Popup.mp3" for window open/close,
"Hover.mp3" for button hover. Anything else (click/send/error/…)
uses a tiny procedural synth. Playback is Qt-Multimedia based so no
extra decoder dependency is needed; from a non-GUI thread the call is
a silent no-op (Qt audio objects live on the GUI thread).
Toggle + volume via configure() / features['enable_sfx'].
"""
from __future__ import annotations

import math
import threading
from pathlib import Path

_VOLUME = 0.35
_ENABLED = True

_ASSET_FILES = {
    "open": "UI Popup.mp3",
    "close": "UI Popup.mp3",
    "hover": "Hover.mp3",
}
_players: dict[str, tuple] = {}


def _base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def configure(enabled: bool = True, volume: float = 0.35) -> None:
    global _ENABLED, _VOLUME
    _ENABLED = bool(enabled)
    try:
        _VOLUME = max(0.0, min(1.0, float(volume)))
    except Exception:
        pass
    for _p, _a in _players.values():
        try:
            _a.setVolume(min(1.0, _VOLUME * 2.2))
        except Exception:
            pass


def _on_gui_thread() -> bool:
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QThread
        app = QApplication.instance()
        return app is not None and QThread.currentThread() == app.thread()
    except Exception:
        return False


def _play_asset(name: str) -> bool:
    """Play an MP3 asset. Returns True if playback started."""
    if not _ENABLED or not _on_gui_thread():
        return False
    fname = _ASSET_FILES.get(name)
    if not fname:
        return False
    path = _base_dir() / "assets" / fname
    if not path.is_file():
        return False
    try:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        key = f"{name}:{fname}"
        entry = next((e for k, e in _players.items() if k == key), None)
        if entry is None:
            player, audio = QMediaPlayer(app), QAudioOutput(app)
            try:
                audio.setVolume(min(1.0, _VOLUME * 2.2))
            except Exception:
                pass
            player.setAudioOutput(audio)
            _players[key] = (player, audio)
            entry = (player, audio)
        player, _audio = entry
        try:
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                player.stop()
        except Exception:
            pass
        player.setSource(QUrl.fromLocalFile(str(path)))
        player.play()
        return True
    except Exception:
        return False


def _tone(freq0: float, freq1: float, ms: int, kind: str = "sine"):
    try:
        import numpy as np
    except Exception:
        return None
    try:
        sr = 22050
        n = max(1, int(sr * ms / 1000))
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)
        freq = freq0 + (freq1 - freq0) * t
        phase = 2.0 * math.pi * np.cumsum(freq) / sr
        if kind == "square":
            wav = np.sign(np.sin(phase))
        elif kind == "saw":
            wav = 2.0 * ((phase / (2.0 * math.pi)) % 1.0) - 1.0
        else:
            wav = np.sin(phase)
        env = np.minimum(1.0, t * 12.0) * (1.0 - t) ** 1.6
        return (wav * env * _VOLUME).astype(np.float32), sr
    except Exception:
        return None


def _play(pcm_sr) -> None:
    if not _ENABLED or pcm_sr is None:
        return

    def _run():
        try:
            import sounddevice as sd
            pcm, sr = pcm_sr
            sd.play(pcm, sr)
            sd.wait()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def click() -> None:
    if not _play_asset("click"):
        _play(_tone(880, 1320, 70))

def open() -> None:
    if not _play_asset("open"):
        _play(_tone(420, 1250, 160))

def close() -> None:
    if not _play_asset("close"):
        _play(_tone(1250, 380, 150))

def hover() -> None:
    # Asset only, kept quiet by the file itself — no synth fallback so
    # hovering never gets noisy when the asset is missing.
    _play_asset("hover")

def send() -> None:
    _play(_tone(700, 1400, 110))

def success() -> None:
    _play(_tone(660, 1980, 180))

def error() -> None:
    _play(_tone(320, 140, 220, kind="saw"))

def listening() -> None:
    _play(_tone(520, 1040, 130))
