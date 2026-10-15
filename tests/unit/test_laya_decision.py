"""Laya AI local decision layer (change: laya-ai-gemini).

Pins the behaviour the feature exists to provide:

* detection reports the REAL on-disk state of laya-multilingual/ — never a
  hard-coded "installed";
* the enable switch and the "Save as Default" persistence both write the one
  shared config store and obey the documented precedence;
* classification NEVER raises: disabled, missing model or broken inference all
  degrade to None so the existing AI routing stays in charge;
* the AI task router maps every category to a role/target and falls back to
  Gemini's default role for anything unknown;
* main.py wiring: startup init, activity-monitor classification, and the
  tool-dispatcher fallback are present in the file that owns them.

No torch/laya import is needed: the service is exercised around the model, and
one opt-in test talks to the real checkpoint when it is installed.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core import laya_decision as ld  # noqa: E402
from app import model_router as mr  # noqa: E402

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point the service at a throwaway config in a throwaway project root.

    BASE_DIR has no laya-multilingual/ inside the sandbox, so enabling never
    loads the real 322M checkpoint in tests — the cheap not-found fallback
    path runs instead, which is itself one of the behaviours under test."""
    proj = tmp_path / "proj"
    (proj / "config").mkdir(parents=True)
    cfg = proj / "config" / "api_keys.json"
    cfg.write_text(json.dumps({"features": {"laya_ai": True}}), encoding="utf-8")
    monkeypatch.setattr(ld, "CONFIG_PATH", cfg)
    monkeypatch.setattr(ld, "BASE_DIR", proj)
    ld._config_cache = None
    yield cfg
    ld._config_cache = None


# ── detection (real filesystem) ───────────────────────────────────────────────

def test_detect_reports_the_installed_model_truthfully():
    d = ld.detect()
    # This checkout ships models/laya-multilingual with the full manifest.
    assert d["detected"] is True
    assert Path(d["path"]).is_dir()
    assert d["missing"] == []


def test_detect_missing_directory_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "BASE_DIR", tmp_path)
    d = ld.detect()
    assert d["detected"] is False
    assert d["path"] == ""
    # status() must not raise on a machine without the model either.
    st = ld.status()
    assert st["detected"] is False


def test_detect_incomplete_manifest_reports_missing_files(tmp_path, monkeypatch):
    p = tmp_path / "models" / "laya-multilingual"
    p.mkdir(parents=True)
    (p / "rl_agent_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ld, "BASE_DIR", tmp_path)
    d = ld.detect()
    assert d["detected"] is False
    assert "model.safetensors" in d["missing"]


# ── configuration + persistence ───────────────────────────────────────────────

def test_set_enabled_persists_to_the_shared_config(sandbox):
    ok, _msg = ld.set_enabled(False)
    assert ok
    data = json.loads(sandbox.read_text(encoding="utf-8"))
    assert data["features"]["laya_ai"] is False
    assert ld.config_enabled() is False
    ld.set_enabled(True)
    assert ld.config_enabled() is True


def test_save_as_default_restores_enabled_state_over_live_flag(sandbox):
    # Save the DISABLED state as the default: it must win on the next boot
    # even though the legacy live flag still says enabled.
    ld.set_enabled(False)
    ld.set_save_as_default(True)
    data = json.loads(sandbox.read_text(encoding="utf-8"))
    assert data["laya"]["save_as_default"] is True
    assert data["laya"]["enabled"] is False
    data["features"]["laya_ai"] = True          # simulate drift of live flag
    sandbox.write_text(json.dumps(data), encoding="utf-8")
    ld._config_cache = None
    assert ld.config_enabled() is False
    # Clearing the default hands control back to the live switch.
    ld.set_save_as_default(False)
    ld._config_cache = None
    assert ld.config_enabled() is True


def test_env_override_forces_laya_off(sandbox, monkeypatch):
    """JARVIS_FEATURE_LAYA_AI follows the project-wide get_feature env
    convention and outranks both the live switch and the saved default."""
    monkeypatch.setenv("JARVIS_FEATURE_LAYA_AI", "off")
    assert ld.config_enabled() is False
    ld.set_save_as_default(True)          # saved default says enabled
    ld._config_cache = None
    assert ld.config_enabled() is False
    monkeypatch.setenv("JARVIS_FEATURE_LAYA_AI", "on")
    assert ld.config_enabled() is True


def test_initialize_disabled_drops_the_model(sandbox, monkeypatch):
    monkeypatch.setattr(ld, "_agent", object())
    ld.set_enabled(False)
    ld.initialize(background=True)
    assert ld.get_state() == ld.STATE_DISABLED
    assert ld._agent is None


# ── classification fallbacks (must never raise) ───────────────────────────────

def test_classify_returns_none_when_disabled(sandbox):
    ld.set_enabled(False)
    assert ld.classify_task("Search Philippines news today") is None


def test_classify_returns_none_when_model_missing(sandbox, monkeypatch):
    monkeypatch.setattr(ld, "BASE_DIR", sandbox.parent / "nowhere")
    ld.shutdown()
    started = time.monotonic()
    assert ld.classify_task("Open Chrome") is None
    # The decision layer NEVER blocks a turn: initialization is background-only,
    # so this call returned immediately instead of waiting on a load.
    assert time.monotonic() - started < 1.0
    assert ld.last_skip()["reason"] in ("model loading", "model unavailable")
    # A missing model leaves the pipeline in the documented fallback state
    # (once the background attempt has finished reporting).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and ld.get_state() == ld.STATE_LOADING:
        time.sleep(0.05)
    assert ld.get_state() in (ld.STATE_NOT_FOUND, "uninitialized", "detecting")


def test_classify_swallows_inference_failure(sandbox, monkeypatch):
    class _Boom:
        def predict(self, *a, **k):
            raise RuntimeError("inference exploded")
    monkeypatch.setattr(ld, "_agent", _Boom())
    monkeypatch.setattr(ld, "_state", ld.STATE_READY)
    assert ld.classify_task("Analyze report.pdf") is None


def test_extract_category_handles_the_real_payload_shape():
    payload = {"model": "laya-rl-agent",
               "answers": {"action": {"type": "choice", "choice": "SEARCH",
                                      "confidence": 0.87, "answer_confidence":
                                      0.94}}}
    category, confidence = ld._extract_category(payload)
    assert category == "SEARCH"
    assert abs(confidence - 0.87) < 1e-9
    assert ld._extract_category({"answers": {"action": "CODE"}})[0] == "CODE"
    assert ld._extract_category({})[0] is None
    assert ld._extract_category(None)[0] is None
    assert ld._extract_category({"answers": {"action": "NONSENSE"}})[0] is None


# ── decision speed (change: laya-decision-speed) ──────────────────────────────

def _fake_agent(category="AUTOMATE", confidence=0.9, delay=0.0):
    """A stand-in for the loaded laya Agent with a controllable cost. Every
    call returns a fresh class, so `.calls` counts one test's inferences."""

    class _Agent:
        calls = 0

        def predict(self, text, questions, **kw):
            type(self).calls += 1
            if delay:
                time.sleep(delay)
            return {"model": "laya-rl-agent",
                    "answers": {"action": {"type": "choice", "choice": category,
                                           "confidence": confidence}},
                    "usage": {"input_tokens": 156, "output_tokens": 0}}
    return _Agent


def _install_agent(monkeypatch, agent):
    """Put the service in the READY state around a fake model (no torch)."""
    ld.shutdown()
    monkeypatch.setattr(ld, "_agent", agent)
    monkeypatch.setattr(ld, "_state", ld.STATE_READY)


def test_cpu_thread_budget_uses_the_cores_it_can_and_keeps_one_free(sandbox):
    """Measured on a 4-logical-CPU box, five identical inputs per run:

        2 torch threads   1.14 s average per decision (1.59 s worst)
        3 torch threads   0.89 s average per decision (1.06 s worst)
        4 torch threads   0.98 s average per decision (1.36 s worst)

    so the budget must NOT be hard-capped at two threads (that is what made
    Laya feel extremely slow), must leave at least one logical CPU for the UI,
    audio and the Gemini session, and must never exceed the hard ceiling.
    """
    import os
    budget = ld._cpu_thread_budget()
    logical = os.cpu_count() or 4
    assert 1 <= budget <= ld._MAX_DECISION_THREADS
    if logical > 2:
        assert budget <= logical - 1
        assert budget >= 2


def test_laya_thread_budget_can_be_overridden(sandbox, monkeypatch):
    """JARVIS_LAYA_THREADS is the documented escape hatch for tuning."""
    monkeypatch.setenv("JARVIS_LAYA_THREADS", "1")
    assert ld._cpu_thread_budget() == 1
    monkeypatch.setenv("JARVIS_LAYA_THREADS", "not-a-number")
    assert ld._cpu_thread_budget() >= 1


def test_identical_request_is_answered_from_the_cache(sandbox, monkeypatch):
    agent = _fake_agent("SEARCH")()
    _install_agent(monkeypatch, agent)
    first = ld.classify_task("Search Philippines news today")
    second = ld.classify_task("search  philippines news today!!")
    assert first and second
    assert first["category"] == second["category"] == "SEARCH"
    # Normalised (case/punctuation-insensitive) repeat: no second forward pass.
    assert second.get("cached") is True
    assert ld.last_timing()["cache_hit"] is True
    assert agent.calls == 1


def test_only_one_inference_runs_at_a_time(sandbox, monkeypatch):
    """A second request must be skipped ('busy'), never queued behind a 2-8 s
    CPU burn — that stacking is what made the whole interface feel slow."""
    agent = _fake_agent("CODE", delay=0.6)()
    _install_agent(monkeypatch, agent)
    results = []

    def _ask(i):
        results.append(ld.classify_task(f"Fix bug number {i} in auth.py"))

    threads = [threading.Thread(target=_ask, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert agent.calls == 1, "stacked inferences: the gate is not holding"
    assert sum(1 for r in results if r is None) == 2
    assert ld.last_skip()["reason"] == "busy"


def test_decision_latency_is_measured_not_claimed(sandbox, monkeypatch):
    agent = _fake_agent("ANALYZE", delay=0.25)()
    _install_agent(monkeypatch, agent)
    received = time.monotonic() - 1.0        # pretend the request arrived 1 s ago
    assert ld.classify_task("Analyze the quarterly report.pdf",
                            received_at=received)
    timing = ld.last_timing()
    assert 200 <= timing["inference_ms"] <= 2000
    assert timing["total_ms"] >= 1000, "routing latency must include the queue"
    assert timing["category"] == "ANALYZE"
    stats = ld.timing_stats()
    assert stats["decisions"] >= 1 and stats["max_ms"] >= 1000
    # Everything the app reports about speed is quoted from these numbers.
    assert "LAYA TIMING" in ld.timing_report()
    assert "LAYA TIMING" in ld.timing_line()


def test_repeated_toggle_restores_the_model_without_reloading(sandbox, monkeypatch):
    """OFF → ON must follow the runtime instantly (spec §24): the loaded model
    is parked for a grace window instead of being re-read from disk (78-164 s)."""
    agent = _fake_agent("VIEW")()
    _install_agent(monkeypatch, agent)
    assert ld.classify_task("Show the camera view") is not None
    ld.set_enabled(False)
    assert ld.get_state() == ld.STATE_DISABLED
    assert ld._agent is None, "disabled Laya must not stay in the pipeline"
    assert ld.classify_task("Show the camera view") is None
    assert ld.last_skip()["reason"] == "disabled"
    ld.set_enabled(True)
    assert ld.get_state() == ld.STATE_READY, "re-enable reuses the parked model"
    assert agent.calls == 1, "the parked model must not be reloaded"
    assert ld.classify_task("Show the other camera view") is not None
    # Re-enabling cancels the pending release (no orphan timer holding the model).
    assert ld._park_timer is None and ld._parked_agent is None


def test_status_carries_the_real_timing_and_skip_reason(sandbox):
    ld.shutdown()
    st = ld.status()
    for key in ("last_timing", "timing", "last_skip", "torch_threads",
                "cache_entries", "cached_model"):
        assert key in st, f"status() stopped reporting {key}"
    assert st["cache_entries"] == 0
    assert str(st["timing"]["decisions"]).isdigit()


def test_state_stays_loading_until_the_first_inference_is_warm(sandbox, monkeypatch):
    """"Ready" must mean a decision can actually be made right now.

    The first forward pass after a load is the expensive one (measured 4.5-23 s
    against 1.6-4 s afterwards), so it happens while the service still reports
    LOADING — otherwise the first user request would be told "ready" and then
    wait on a cold model."""
    seen = []

    class _FakeAgent:
        def predict(self, text, questions, **kw):
            seen.append(("predict", ld.get_state()))
            return {"answers": {"action": {"choice": "CHAT", "confidence": 0.4}}}

    class _FakeLaya:
        @staticmethod
        def load(path, device=None):
            seen.append(("load", ld.get_state()))
            return _FakeAgent()

    monkeypatch.setitem(sys.modules, "laya", _FakeLaya)
    monkeypatch.setattr(ld, "detect", lambda: {
        "detected": True, "path": "models/fake", "relative": "models/fake",
        "missing": []})
    ld.shutdown()
    ld.clear_last_classification()
    ld._load_blocking()

    assert [phase for phase, _state in seen] == ["load", "predict"], \
        "the cold first inference must be paid during initialization"
    assert seen[1][1] == ld.STATE_LOADING, \
        "the model was already published READY before it had answered anything"
    assert ld.get_state() == ld.STATE_READY
    assert ld.last_classification() is None, "the warm-up probe must stay unpublished"
    assert ld._cache_size() == 0, "the warm-up probe must not enter the decision cache"
    assert isinstance(ld.timing_stats()["warm_ms"], float), \
        "the warm-up cost is recorded (0.0 for a fake model, seconds for the real one)"
    ld.shutdown()



# ── AI task router hookup ─────────────────────────────────────────────────────


@pytest.mark.parametrize("category", list(ld.CATEGORIES))
def test_every_category_routes_somewhere(category):
    assert mr.role_for_category(category) in mr.DEFAULTS
    assert mr.target_for_category(category)


def test_unknown_category_falls_back_to_gemini_default():
    assert mr.role_for_category("WAT") == "default"
    assert mr.role_for_category(None) == "default"
    assert mr.target_for_category(None) == "gemini_general"


def test_classify_request_is_silent_when_laya_unavailable(sandbox, monkeypatch):
    monkeypatch.setattr(ld, "BASE_DIR", sandbox.parent / "nowhere")
    ld.shutdown()
    assert mr.classify_request("Search Philippines news today") is None


# ── status truthfulness ───────────────────────────────────────────────────────

def test_status_never_says_ready_without_loading(sandbox):
    ld.shutdown()
    st = ld.status()
    assert st["ready"] is False
    assert st["status_line"] in ("Enabled", "Disabled")


def test_detected_but_uninitialized_is_never_reported_as_ready():
    """Spec §11: 'Ready' requires a successfully loaded model — a file on
    disk alone must never produce it (real BASE_DIR, nothing loaded)."""
    ld.shutdown()
    assert ld.get_state() != ld.STATE_READY
    st = ld.status()
    if st["detected"]:
        assert "Ready" not in st["detail_line"]
        assert st["ready"] is False


def test_status_reports_fallback_when_enabled_without_model(sandbox, monkeypatch):
    monkeypatch.setattr(ld, "BASE_DIR", sandbox.parent / "nowhere")
    ld.shutdown()
    ld._load_blocking()          # synchronous: forces the detection outcome
    st = ld.status()
    assert st["enabled"] is True
    assert st["detected"] is False
    assert "fallback active" in st["detail_line"]


# ── main.py wiring (source-pinned, like the activity-wiring regressions) ─────

def test_startup_initializes_laya_without_blocking():
    assert "laya_decision.initialize(background=True)" in MAIN


def test_typed_and_spoken_requests_are_both_classified():
    # Both paths hand the routing layer the moment the request arrived, so the
    # reported routing latency covers the whole waited-through path (spec §4).
    assert "self._laya_note_request(str(text), _t_recv)" in MAIN   # typed path
    assert "self._laya_note_request(full_in, _t_recv)" in MAIN     # voice turn


def test_routing_timings_are_logged_as_structured_events():
    """Diagnostics are real measurements, recorded on the actual path."""
    assert "def _log_laya_routing" in MAIN
    assert '"routing_completed"' in MAIN
    assert "received_at=t_recv" in MAIN


def test_decision_layer_never_blocks_a_turn():
    """The classify path may only start a BACKGROUND load: a cold decision
    layer reports 'model loading' instead of stalling the request."""
    import inspect
    src = inspect.getsource(ld.classify_task)
    assert "initialize(background=True)" in src
    assert "background=False" not in src



def test_tool_dispatcher_uses_laya_only_as_fallback():
    start = MAIN.index("_doing_category, _doing_detail = _activity_for_tool(name, _activity_args)")
    body = MAIN[start:start + 1100]
    assert "if not _doing_category:" in body
    assert "laya_decision.last_classification()" in body


def test_activity_row_is_cleared_after_the_turn():
    assert "self._laya_clear_activity()" in MAIN
