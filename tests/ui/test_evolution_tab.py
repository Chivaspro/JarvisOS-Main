"""Settings ▸ EVOLUTION — the self-evolution surface, with the engine stubbed.

Nothing here forges a real skill or calls a model. The window is built
offscreen; the registry and the provider chain are stubbed so the assertions
are about the wiring this tab adds:

* the tab exists and renders the engine's own provider diagnostics (so a
  stored value that is not a key is visible, not hidden);
* forged skills are listed with their live / disabled / on-disk state;
* the FORGE button drives ``evolution.registry.handler`` on a worker thread and
  reports the verdict it returns — the GUI thread never runs the synthesis.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JARVIS_DISABLE_WEBENGINE", "1")

pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture()
def settings(shared_qapplication):
    """A Settings window on a private config file.

    Deliberately ``tempfile`` rather than pytest's ``tmp_path``: this machine's
    pytest temp base is not writable (the same pre-existing issue noted in
    tests/unit/test_evolution_forge.py).
    """
    if shared_qapplication is None:
        pytest.skip("PyQt6 is not installed")
    import ui_settings
    with tempfile.TemporaryDirectory(prefix="jarvis-evolution-ui-") as folder:
        cfg = Path(folder) / "api_keys.json"
        cfg.write_text(json.dumps({
            "gemini_api_key": "AQ." + "x" * 40,
            "features": {},
        }), encoding="utf-8")
        win = ui_settings.SettingsWindow(config_path=cfg)
        win._test_cfg_path = cfg
        yield win
        win.close()


def test_the_evolution_tab_is_registered_with_its_widgets(settings):
    names = [settings.tabs.tabText(i) for i in range(settings.tabs.count())]
    assert "EVOLUTION" in names
    assert hasattr(settings, "_evo_goal") and hasattr(settings, "_evo_forge_btn")
    assert hasattr(settings, "_evo_list") and hasattr(settings, "_evo_chain_label")
    assert hasattr(settings, "_evo_pending") and hasattr(settings, "_evo_timer")


def test_the_chain_readout_comes_from_the_engine_diagnostics(settings, monkeypatch):
    from evolution import forge as _forge
    monkeypatch.setattr(_forge, "provider_diagnostics", lambda config=None: [
        {"provider": "openrouter", "field": "openrouter_api_key",
         "state": "usable", "detail": "ok", "in_chain": True},
        {"provider": "gemini", "field": "gemini_api_key",
         "state": "usable", "detail": "ok", "in_chain": True},
        {"provider": "openai", "field": "openai_api_key",
         "state": "missing", "detail": "No openai key is set.", "in_chain": False},
        {"provider": "groq", "field": "groq_api_key",
         "state": "skipped", "detail": "Not used: it is pasted text, not a key.",
         "in_chain": False},
    ])
    settings._refresh_evolution()
    text = settings._evo_chain_label.text()
    assert "OpenRouter → Gemini" in text
    assert "OpenAI: missing" in text
    assert "Groq: skipped" in text


def test_forged_skills_are_listed_with_their_state(settings, monkeypatch):
    from evolution import registry as _registry
    monkeypatch.setattr(_registry, "list_skills", lambda directory=None: [
        {"name": "iss_tracker", "version": "1.0.0", "invocations": 3,
         "goal": "track the ISS", "live": True, "enabled": True,
         "last_error": None, "path": "/tmp/x"},
        {"name": "old_tool", "version": "1.0.0", "invocations": 0,
         "goal": "", "live": False, "enabled": False,
         "last_error": "boom", "path": "/tmp/y"},
    ])
    settings._reload_forged_skills()
    rows = [settings._evo_list.item(i).text() for i in range(settings._evo_list.count())]
    assert any("[LIVE]" in row and "iss_tracker" in row and "3 call(s)" in row
               for row in rows)
    assert any("[DISABLED]" in row and "old_tool" in row for row in rows)
    assert settings._evo_list.item(1).toolTip() == "Last error: boom"
    assert "2 forged skill(s)" in settings._evo_skill_status.text()


def test_the_forge_button_runs_the_registry_off_the_gui_thread(settings, monkeypatch):
    from evolution import registry as _registry
    calls: list[dict] = []

    def _fake_handler(args, player=None):
        calls.append(dict(args))
        return ("Skill 'iss_tracker' was forged with OpenRouter, verified in "
                "the Crucible and is live.")

    monkeypatch.setattr(_registry, "handler", _fake_handler)
    settings._evo_goal.setText("track the International Space Station")
    settings._forge_skill()
    # The button is disabled immediately, while the work runs elsewhere.
    assert settings._evo_forge_btn.isEnabled() is False
    for _ in range(200):
        settings._drain_evolution()
        if "forge" not in settings._evo_pending:
            break
        time.sleep(0.02)
    assert calls and calls[0]["action"] == "forge"
    assert "International Space Station" in calls[0]["goal"]
    assert "forged with OpenRouter" in settings._evo_status.text()
    assert settings._evo_forge_btn.isEnabled() is True


def test_a_failed_forge_reports_the_verdict_instead_of_claiming_success(
        settings, monkeypatch):
    from evolution import registry as _registry
    monkeypatch.setattr(_registry, "handler",
                        lambda args, player=None: "Skill synthesis failed: every "
                                                 "provider failed with HTTP 401.")
    settings._evo_goal.setText("something that will not verify")
    settings._forge_skill()
    for _ in range(200):
        settings._drain_evolution()
        if "forge" not in settings._evo_pending:
            break
        time.sleep(0.02)
    assert "synthesis failed" in settings._evo_status.text()


def test_a_too_short_goal_never_starts_a_forge(settings, monkeypatch):
    from evolution import registry as _registry
    monkeypatch.setattr(_registry, "handler",
                        lambda *a, **k: pytest.fail("no forge may start"))
    settings._evo_goal.setText("hi")
    settings._forge_skill()
    assert "Describe the skill first" in settings._evo_status.text()
    assert "forge" not in settings._evo_pending
    assert settings._evo_forge_btn.isEnabled() is True
