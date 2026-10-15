"""A refused environment key must not cost the user their session.

The measured failure this pins: a dead user-level ``GEMINI_API_KEY`` sat in
front of a perfectly good key in ``config/api_keys.json``.
``config.settings.api_key`` resolves the environment first — correct, and kept —
so the Live service refused the session with 1008 "invalid authentication
credentials. Expected OAuth 2 access token…". Re-entering the key could never
help: the Initialisation overlay and Settings ▸ API both write ``api_keys.json``,
which the environment kept outranking. J.A.R.V.I.S. looped
NEEDS KEY → key prompt → NEEDS KEY and the Chat Panel had no live session at all.

The heal: once Google has refused the resolved key, a *different* non-empty key
stored in ``api_keys.json`` is adopted and the session reconnects, instead of
asking the user to type the same key again.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeUI:
    """Just enough UI to observe what the heal tells the user."""

    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, text):
        self.lines.append(str(text))


@pytest.fixture(scope="module")
def jarvis_main():
    """main.py, imported once — its imports cost ~12 s, paid a single time."""
    return pytest.importorskip("main")


@pytest.fixture()
def refused(jarvis_main, monkeypatch):
    """A JarvisLive whose heavy constructor is skipped: heal state only."""
    with tempfile.TemporaryDirectory(prefix="jarvis-heal-") as folder:
        path = Path(folder) / "api_keys.json"
        path.write_text(json.dumps({"gemini_api_key": "stored-key-1234567890"}),
                        encoding="utf-8")
        monkeypatch.setattr(jarvis_main, "API_CONFIG_PATH", path)
        monkeypatch.setattr(jarvis_main, "_file_cache", {})

        class _Live(jarvis_main.JarvisLive):
            def __init__(self):                      # noqa: super-init-not-called
                self.ui = _FakeUI()
                # The cached client was built with the key the server refused.
                self._client = "client-built-with-the-refused-key"
                self._client_key = "refused-env-key"
                self._client_version = "v1beta"
                self._client_lock = threading.Lock()
                self._auth_fail_count = 2
                self._missing_key_reported = True
                self._key_override = ""

        obj = _Live()
        yield jarvis_main, obj, path, obj.ui


# ── the heal itself ───────────────────────────────────────────────────────────

def test_a_refused_environment_key_switches_to_the_stored_one(refused):
    main, obj, _path, ui = refused

    assert obj._adopt_stored_key() is True
    # The session must use the stored key from here on.
    assert obj._live_api_key() == "stored-key-1234567890"
    # The client built with the refused key may never be reused.
    assert obj._client is None
    assert obj._client_key == ""
    assert obj._client_version == ""
    # The switch is honest: logged, and the refusal count belongs to the key
    # that just got replaced.
    assert any("environment variable was refused" in line for line in ui.lines)
    assert obj._auth_fail_count == 0
    assert obj._missing_key_reported is False


def test_the_stored_key_wins_only_after_it_is_adopted(refused):
    """Before the heal, the canonical resolver is still in charge."""
    main, obj, _path, _ui = refused

    assert obj._live_api_key() == main._get_api_key()


def test_a_stored_key_equal_to_the_refused_one_is_left_alone(refused):
    """A key that is genuinely wrong still gets the honest key prompt."""
    main, obj, path, ui = refused
    path.write_text(json.dumps({"gemini_api_key": "refused-env-key"}),
                    encoding="utf-8")
    main._file_cache.clear()          # the write above changed the file

    assert obj._adopt_stored_key() is False
    assert obj._key_override == ""
    assert ui.lines == []


def test_an_empty_store_keeps_the_key_prompt_honest(refused):
    main, obj, path, ui = refused
    path.write_text(json.dumps({"gemini_api_key": ""}), encoding="utf-8")
    main._file_cache.clear()

    assert obj._adopt_stored_key() is False
    assert obj._key_override == ""
    assert ui.lines == []


# ── the wiring: the loop must actually use it ─────────────────────────────────

def test_the_connect_loop_resolves_the_live_key_and_heals_before_prompting():
    source = (ROOT / "main.py").read_text(encoding="utf-8")

    # The connect resolves through the healable resolver, not the raw one.
    assert "_key     = self._live_api_key()" in source, (
        "the Live connect must resolve the key through _live_api_key()")

    # The authentication-refusal branch adopts a stored key BEFORE it counts the
    # failure and re-opens the key prompt.
    branch = source[source.index("if _auth_failed:"):][:900]
    assert "_adopt_stored_key()" in branch, (
        "a refused key must try the stored key before asking for a new one")
    assert branch.index("_adopt_stored_key()") < branch.index("_auth_fail_count"), (
        "the heal must run before the refusal count that triggers the prompt")