"""The Performance settings module: presets, persistence and the scan.

Pins three contracts:

* a named profile resolves to a deterministic bundle of settings, and the
  stored view merges defaults with clamped overrides;
* applying a profile persists through the config writer and never touches a
  memory key;
* the diagnostic scan reports cancellation, handles an unavailable vault, and
  every finding it produces names measured evidence.
"""
from __future__ import annotations

import json
import threading

import pytest

import ui_perfomance as perf


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    from config import settings as cfg
    from app import config_guard
    from memory import backend as memory_backend
    path = tmp_path / "api_keys.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cfg, "API_KEYS_FILE", path)
    monkeypatch.setattr(config_guard, "API_KEYS_FILE", path, raising=False)
    cfg._cache.update({"mtime": None, "data": None})
    # The scan reads the active memory backend, which caches its singleton;
    # reset it around every test so this file never poisons another test's
    # monkeypatched backend wiring.
    memory_backend.reset_memory_backend()
    yield path
    memory_backend.reset_memory_backend()
    cfg._cache.update({"mtime": None, "data": None})


def test_profiles_resolve_deterministically():
    balanced = perf.resolve("balanced")
    ultra = perf.resolve("ultra")
    assert balanced["profile"] == "balanced"
    assert balanced["animations"] is True
    assert ultra["animations"] is False
    assert ultra["neural_max_nodes"] == 400
    assert ultra["vault_watch_s"] > balanced["vault_watch_s"]
    assert perf.resolve("does-not-exist")["profile"] == "balanced"


def test_apply_profile_persists_and_never_touches_memory_keys(_isolate_config):
    before = json.loads(_isolate_config.read_text(encoding="utf-8"))
    settings = perf.apply_profile("ultra")
    after = json.loads(_isolate_config.read_text(encoding="utf-8"))
    assert after["performance"]["profile"] == "ultra"
    assert after["performance"]["neural_max_nodes"] == settings["neural_max_nodes"]
    for key in ("memory_backend", "obsidian_vault_path"):
        assert after.get(key) == before.get(key)


def test_effective_merges_and_clamps(_isolate_config):
    perf.apply_profile("balanced")
    raw = json.loads(_isolate_config.read_text(encoding="utf-8"))
    raw["performance"]["neural_tick_ms"] = 99999
    raw["performance"]["mind_reload_s"] = -5
    _isolate_config.write_text(json.dumps(raw), encoding="utf-8")
    live = perf.effective()
    assert live["neural_tick_ms"] <= perf._LIMITS["neural_tick_ms"][1]
    assert live["mind_reload_s"] >= perf._LIMITS["mind_reload_s"][0]


def test_save_custom_switches_profile_to_custom(_isolate_config):
    perf.apply_profile("balanced")
    settings = perf.save_custom({"animations": False, "vault_watch_s": 45})
    assert settings["profile"] == "custom"
    assert perf.effective()["profile"] == "custom"
    assert perf.effective()["vault_watch_s"] == 45


def test_scan_cancellation_returns_partial_state(tmp_path):
    cancel = threading.Event()
    cancel.set()
    result = perf.collect_scan(cancel=cancel)
    assert result["cancelled"] is True
    assert result["findings"] == []
    assert "performance_settings" in result["metrics"]


def test_scan_handles_missing_vault(tmp_path, monkeypatch):
    from config import settings as cfg
    cfg.set_value("obsidian_vault_path", str(tmp_path / "no-such-vault"))
    result = perf.collect_scan()
    assert result["cancelled"] is False
    vault = result["metrics"]["vault"]
    assert vault["exists"] is False
    issues = [f["issue"] for f in result["findings"]]
    assert any("does not exist" in issue for issue in issues)
    for finding in result["findings"]:
        assert finding["evidence"]
        assert finding["confidence"] in ("measured", "hypothesis")


def test_findings_report_no_issue_when_clean():
    findings = perf.build_findings({
        "performance_settings": perf.resolve("balanced"),
        "vault": {"configured": "", "exists": False},
        "backend": {"active": "local", "selected": "original", "error": ""},
        "graph": {"elapsed_s": 0.1, "nodes": 3, "links": 2},
        "process": {"available": False},
    })
    assert findings and "No performance problems" in findings[0]["issue"]