"""The Skill Crucible: static validation, dependencies and sandbox execution.

Nothing here touches the network, the model, or the real plugin directory —
every test works on a candidate string and a temporary directory.
"""
from __future__ import annotations

import textwrap

import pytest

from evolution import crucible


GOOD_SKILL = textwrap.dedent('''
    """A tiny forged tool used by the tests."""

    import json
    import os


    def run(parameters: dict, player=None, session_memory=None) -> str:
        name = str(parameters.get("name") or "world").strip()
        return f"Hello, {name}."
''')


# ── stage 1: AST ──────────────────────────────────────────────────────────────

def test_valid_candidate_passes():
    ok, reason = crucible.validate_ast(GOOD_SKILL)
    assert ok, reason


def test_missing_entry_point_is_refused():
    ok, reason = crucible.validate_ast("def execute(**kwargs):\n    return 1\n")
    assert not ok
    assert "run" in reason


def test_syntax_error_names_the_line():
    ok, reason = crucible.validate_ast("def run(:\n    pass\n")
    assert not ok
    assert "line" in reason.lower()


def test_banned_term_is_refused_even_inside_a_string():
    ok, reason = crucible.validate_ast(
        'def run(parameters=None, player=None, session_memory=None):\n'
        '    return "now running diskpart on C:"\n'
    )
    assert not ok
    assert "diskpart" in reason


def test_empty_candidate_is_refused():
    ok, reason = crucible.validate_ast("   ")
    assert not ok
    assert "empty" in reason.lower()


def test_declared_name_reads_the_plugin_block():
    assert crucible.declared_name('PLUGIN = {"name": "stock_chart"}\n') == "stock_chart"
    assert crucible.declared_name("x = 1\n") == ""


# ── stage 2: dependencies ─────────────────────────────────────────────────────

def test_stdlib_and_local_imports_are_not_dependencies():
    deps = crucible.extract_dependencies(
        "import os\nimport json\nfrom pathlib import Path\n"
        "from memory import service\nfrom actions import weather_report\n"
    )
    assert deps == []


def test_third_party_imports_are_detected_and_mapped():
    deps = crucible.extract_dependencies(
        "import requests\nfrom PIL import Image\nimport bs4\nimport psutil\n"
    )
    assert deps == ["PIL", "bs4", "psutil", "requests"]


def test_resolve_dependencies_reports_missing_when_installing_is_off():
    ok, message = crucible.resolve_dependencies(["surely_not_installed_xyz"],
                                                allow_install=False)
    assert not ok
    assert "surely_not_installed_xyz" in message


def test_resolve_dependencies_is_a_noop_without_dependencies():
    ok, message = crucible.resolve_dependencies([])
    assert ok
    assert "no external" in message.lower()


# ── stage 3: sandbox ──────────────────────────────────────────────────────────

def test_sandbox_runs_the_real_function_and_returns_its_output():
    passed, message, telemetry = crucible.run_sandbox_test(
        GOOD_SKILL, [{"input": {}}, {"input": {"name": "sir"}}])
    assert passed, message
    assert telemetry["results"][1]["output"].startswith("Hello, sir")
    assert telemetry["elapsed_s"] >= 0


def test_sandbox_fails_when_the_skill_raises():
    broken = ("def run(parameters=None, player=None, session_memory=None):\n"
              "    return parameters['missing']\n")
    passed, message, _ = crucible.run_sandbox_test(broken, [{"input": {}}])
    assert not passed
    assert "missing" in message


def test_sandbox_rejects_a_skill_that_returns_an_error_object():
    bad = ("def run(parameters=None, player=None, session_memory=None):\n"
           "    return {'error': 'the network is down'}\n")
    passed, message, _ = crucible.run_sandbox_test(bad, [{"input": {}}])
    assert not passed
    assert "network is down" in message


def test_sandbox_catches_a_module_level_failure():
    passed, message, _ = crucible.run_sandbox_test(
        "import totally_missing_module_xyz\n\n"
        "def run(parameters=None, player=None, session_memory=None):\n"
        "    return 'never'\n", [{"input": {}}])
    assert not passed
    assert "sandbox" in message.lower()


def test_sandbox_supports_async_entry_points():
    async_skill = ("import asyncio\n\n"
                   "async def run(parameters=None, player=None, session_memory=None):\n"
                   "    await asyncio.sleep(0)\n"
                   "    return 'async done'\n")
    passed, message, telemetry = crucible.run_sandbox_test(async_skill, [{"input": {}}])
    assert passed, message
    assert "async done" in telemetry["results"][0]["output"]


def test_sandbox_never_imports_the_candidate_into_our_process():
    # A candidate that writes a module-level side effect must not affect us: it
    # runs in a child process, so the module is absent from this interpreter.
    marker = "crucible_marker_module_xyz"
    import sys
    crucible.run_sandbox_test(
        f"import sys\nsys.modules['{marker}'] = object()\n\n"
        "def run(parameters=None, player=None, session_memory=None):\n"
        "    return 'ok'\n", [{"input": {}}])
    assert marker not in sys.modules


@pytest.mark.parametrize("timeout", [0.4])
def test_sandbox_times_out(timeout):
    slow = ("import time\n\n"
            "def run(parameters=None, player=None, session_memory=None):\n"
            "    time.sleep(10)\n"
            "    return 'too late'\n")
    passed, message, _ = crucible.run_sandbox_test(slow, [{"input": {}}], timeout=timeout)
    assert not passed
    assert "timed out" in message.lower()


# ── advanced static analysis (hazards / contract / quality) ───────────────────

def test_hazards_are_reported_but_never_block():
    code = ("import subprocess\n"
            "import os\n\n"
            "def run(parameters=None, player=None, session_memory=None):\n"
            "    os.system('echo hi')\n"
            "    subprocess.run(['echo', 'hi'])\n"
            "    return 'done'\n")
    ok, reason = crucible.validate_ast(code)
    assert ok, reason                        # allowed, not refused
    kinds = {h["hazard"] for h in crucible.scan_hazards(code)}
    assert "os.system" in kinds and "subprocess" in kinds


def test_hazards_are_empty_for_a_clean_skill():
    assert crucible.scan_hazards(GOOD_SKILL) == []


def test_output_contract_refuses_a_bare_return():
    ok, reason = crucible.validate_output_contract(
        "def run(parameters=None, player=None, session_memory=None):\n"
        "    if parameters:\n"
        "        return\n"
        "    return 'ok'\n")
    assert not ok
    assert "bare" in reason.lower()


def test_output_contract_refuses_a_missing_return():
    ok, reason = crucible.validate_output_contract(
        "def run(parameters=None, player=None, session_memory=None):\n"
        "    x = 1\n")
    assert not ok
    assert "return" in reason.lower()


def test_output_contract_accepts_a_spoken_reply():
    ok, reason = crucible.validate_output_contract(GOOD_SKILL)
    assert ok, reason


def test_analyze_code_scores_a_documented_guarded_skill_higher():
    plain = crucible.analyze_code(
        "def run(parameters=None, player=None, session_memory=None):\n    return 'x'\n")
    good = crucible.analyze_code(GOOD_SKILL)
    assert good["quality_score"] > plain["quality_score"]
    assert good["has_docstring"] is True
    assert 0 <= plain["quality_score"] <= 100


# ── the orchestrator ──────────────────────────────────────────────────────────

def test_verify_reports_every_stage_for_a_good_skill():
    report = crucible.verify(GOOD_SKILL)
    assert report["ok"], report["message"]
    assert set(report["stages"]) >= {"ast", "contract", "dependencies", "sandbox"}
    assert report["metrics"]["quality_score"] > 0
    assert "Quality" in report["message"]


def test_verify_stops_at_the_first_failing_stage():
    report = crucible.verify("def execute(**kwargs):\n    return 1\n")
    assert not report["ok"]
    assert "ast" in report["stages"]
    assert "sandbox" not in report["stages"]


def test_verify_rejects_a_broken_skill_at_the_sandbox():
    broken = ("def run(parameters=None, player=None, session_memory=None):\n"
              "    return parameters['missing']\n")
    report = crucible.verify(broken)
    assert not report["ok"]
    assert report["stages"]["sandbox"]["ok"] is False


def test_sandbox_telemetry_carries_idempotence_and_output_types():
    passed, _message, telemetry = crucible.run_sandbox_test(GOOD_SKILL, [{"input": {}}])
    assert passed
    assert telemetry["idempotent"] is True
    assert telemetry["output_types"] == ["str"]
    assert telemetry["results"][0]["elapsed_ms"] >= 0.0
