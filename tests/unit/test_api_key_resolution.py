"""One credential, one resolver — the bug that left the live session mute.

The regression these tests pin: ``main._get_api_key`` (and six ``actions/``
helpers) read ``config/api_keys.json`` directly, while Settings and the API HUB
resolve the key through ``config.settings`` (``GEMINI_API_KEY`` environment
variable first). A key present only in the environment was therefore reported as
configured by the API HUB while the live session connected with an empty string —
J.A.R.V.I.S. was silent, with no error shown anywhere.

Every reader now goes through ``config.settings.api_key``, and the environment
wins, so the interface and the brain can never disagree again.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config.settings as settings  # noqa: E402

#: Every module that needs the Gemini key in order to build a client.
GEMINI_READERS = (
    "main.py",
    "actions/code_helper.py",
    "actions/dev_agent.py",
    "actions/web_search.py",
    "actions/youtube_video.py",
    "actions/file_processor.py",
    "actions/screen_processor.py",
)

#: The exact shape that caused the bug: the key read out of the file alone.
_NAKED_JSON_READ = re.compile(
    r'json\.load\(\s*f\s*\)\s*\[\s*["\']gemini_api_key["\']\s*\]')


@pytest.fixture()
def blank_config(monkeypatch):
    """A real api_keys.json with no gemini key, and no environment override.

    Deliberately ``tempfile`` rather than pytest's ``tmp_path``: this machine's
    pytest temp base is not writable (the same pre-existing issue the evolution
    tests document).
    """
    with tempfile.TemporaryDirectory(prefix="jarvis-key-") as folder:
        path = Path(folder) / "api_keys.json"
        path.write_text(json.dumps({"gemini_api_key": "",
                                    "assistant_name": "JARVIS"}),
                        encoding="utf-8")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr(settings, "API_KEYS_FILE", path)
        # A fresh cache dict, so nothing read from the real config leaks in.
        monkeypatch.setattr(settings, "_cache", {"mtime": None, "data": None})
        yield path


# ── the precedence itself ─────────────────────────────────────────────────────

def test_the_environment_wins_over_the_file(blank_config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-1234567890")
    assert settings.api_key("gemini_api_key") == "env-key-1234567890"
    assert settings.gemini_api_key() == "env-key-1234567890"


def test_the_file_is_used_when_the_environment_is_absent(blank_config):
    blank_config.write_text(json.dumps({"gemini_api_key": "file-key-1234567890"}),
                            encoding="utf-8")
    assert settings.api_key("gemini_api_key") == "file-key-1234567890"


def test_no_key_anywhere_is_an_empty_string(blank_config):
    assert settings.api_key("gemini_api_key") == ""


def test_a_whitespace_only_key_is_not_a_key(blank_config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert settings.api_key("gemini_api_key") == ""


# ── the contract every reader must keep ───────────────────────────────────────

@pytest.mark.parametrize("relpath", GEMINI_READERS)
def test_no_reader_parses_api_keys_json_for_the_gemini_key(relpath):
    """A JSON-only read ignores the environment — the exact disagreement."""
    source = (ROOT / relpath).read_text(encoding="utf-8")
    assert not _NAKED_JSON_READ.search(source), (
        f"{relpath} reads gemini_api_key straight out of api_keys.json; it must "
        f"resolve through config.settings.api_key so GEMINI_API_KEY is honoured")


@pytest.mark.parametrize("relpath", GEMINI_READERS)
def test_every_reader_delegates_to_the_canonical_resolver(relpath):
    source = (ROOT / relpath).read_text(encoding="utf-8")
    body = source[source.index("def _get_api_key"):][:700]
    assert "config.settings" in body and "api_key" in body, (
        f"{relpath}._get_api_key must delegate to config.settings.api_key")


def test_a_local_file_fallback_never_precedes_the_resolver():
    """Where a helper keeps a file fallback, the canonical call still comes first."""
    for relpath in ("main.py", "actions/screen_processor.py"):
        body = (ROOT / relpath).read_text(encoding="utf-8")
        body = body[body.index("def _get_api_key"):][:900]
        fallback = max(body.find("_read_cached_json"),
                       body.find('_load_config().get("gemini_api_key"'))
        assert fallback == -1 or body.find("config.settings") < fallback, (
            f"{relpath} tries a local read before the canonical resolver")


# ── functional proof on the modules that are cheap to import ──────────────────

def test_action_readers_return_the_environment_key(blank_config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-1234567890")
    from actions import web_search
    assert web_search._get_api_key() == "env-key-1234567890"


def test_action_readers_return_the_file_key(blank_config):
    blank_config.write_text(json.dumps({"gemini_api_key": "file-key-1234567890"}),
                            encoding="utf-8")
    from actions import web_search
    assert web_search._get_api_key() == "file-key-1234567890"


# ── the brain itself ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def jarvis_main():
    """main.py, imported once — its imports cost ~12 s, paid a single time."""
    return pytest.importorskip("main")


def test_the_live_session_resolves_the_environment_key(jarvis_main, blank_config,
                                                       monkeypatch):
    """The exact bug the user saw: the hub said ready, the brain had no key."""
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-1234567890")
    assert jarvis_main._get_api_key() == "env-key-1234567890"


def test_the_live_session_reports_no_key_rather_than_a_stale_one(jarvis_main,
                                                                 blank_config,
                                                                 monkeypatch):
    """With no key in the environment AND none in the file, the answer is empty.

    Both sources are pointed at the blank file: the canonical resolver reads
    ``config.settings.API_KEYS_FILE`` (patched by ``blank_config``), and the
    documented last-resort read uses main's own cached ``API_CONFIG_PATH``.
    """
    monkeypatch.setattr(jarvis_main, "API_CONFIG_PATH", blank_config)
    assert jarvis_main._get_api_key() == ""
