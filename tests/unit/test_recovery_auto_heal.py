"""Proactive Auto-Heal: analysis, patch safety, history, rollback and policy.

Every test works on a temporary tree passed as an explicit path, so no real
project file is ever touched and no model is ever called.
"""
from __future__ import annotations

import json
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from recovery import analyzer, patcher, service

BROKEN_SOURCE = textwrap.dedent('''
    """A tiny module used by the auto-heal tests."""


    def greet(name):
        return name.strip()
''')

TRACEBACK = (
    'Traceback (most recent call last):\n'
    '  File "{file}", line 5, in greet\n'
    '    return helpers.clean(name)\n'
    # The innermost frame is third-party: the analyser must walk past it to the
    # project's own frame above it, which is the normal shape of a real failure.
    '  File "C:\\pile\\site-packages\\requests\\api.py", line 61, in get\n'
    '    raise AttributeError("x")\n'
    "AttributeError: 'NoneType' object has no attribute 'strip'\n"
)


@pytest.fixture(autouse=True)
def _no_live_model(monkeypatch):
    """No test in this file may ever reach a real model call."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("patcher._ask was called without a stub")

    monkeypatch.setattr(patcher, "_ask", _boom)
    try:
        from evolution import forge as _forge
        monkeypatch.setattr(_forge, "_ask_model", _boom)
    except Exception:
        pass


@pytest.fixture()
def scratch():
    with tempfile.TemporaryDirectory(prefix="jarvis-heal-") as folder:
        yield Path(folder)


@pytest.fixture()
def broken_file(scratch, monkeypatch):
    """A broken capability file inside a throwaway project tree.

    ``analyzer.root`` is pointed at the scratch directory so the real
    project-relative risk rules are exercised without touching this repository.
    """
    # ``resolve()`` matters: Windows reports the temp directory twice (short
    # "ADMINI~1" and long "Administrator") and the risk check walks the path
    # relative to the root, so both sides must be canonical.
    monkeypatch.setattr(analyzer, "root", lambda: scratch.resolve())
    target = scratch.resolve() / "actions" / "greeter.py"
    target.parent.mkdir(parents=True)
    target.write_text(BROKEN_SOURCE, encoding="utf-8")
    return target


def _tb_for(path: Path) -> str:
    return TRACEBACK.replace("{file}", str(path).replace("\\", "\\\\"))


# ── analyzer ──────────────────────────────────────────────────────────────────

def test_analysis_skips_third_party_and_finds_the_first_party_frame(broken_file):
    parsed = analyzer.parse(_tb_for(broken_file))
    assert parsed["found"]
    assert Path(parsed["file"]) == broken_file.resolve()
    assert parsed["line"] == 5
    assert parsed["function"] == "greet"
    assert parsed["exception_type"] == "AttributeError"
    assert parsed["risk"] == "low"
    assert any("requests" in s for s in parsed["skipped"])


def test_protected_files_are_proposed_but_never_auto_applied():
    """Protected files are candidates so the approval UI can display them.

    They used to be skipped entirely (`found=False`, "no first-party file"),
    which left the repair card's patch preview permanently empty; the policy
    layer must still refuse to auto-apply them.
    """
    parsed = analyzer.parse(
        'Traceback (most recent call last):\n'
        f'  File "{analyzer.root() / "main.py"}", line 10, in run\n'
        '    pass\n'
        "RuntimeError: boom\n")
    assert parsed["found"]
    assert parsed["risk"] == "protected"
    assert service._auto_allowed("protected", "auto") is False
    assert service._auto_allowed("protected", "auto_low_risk") is False


def test_core_files_are_high_risk():
    project_core = analyzer.root() / "core" / "voice.py"
    risk, reason = analyzer.risk_of(project_core)
    assert risk == "high"
    assert "approval" in reason


def test_leaf_capability_files_are_low_risk():
    assert analyzer.risk_of(analyzer.root() / "actions" / "weather_report.py")[0] == "low"
    assert analyzer.risk_of(analyzer.root() / "plugins" / "x" / "plugin.py")[0] == "low"
    assert analyzer.risk_of(analyzer.root() / "main.py")[0] == "protected"


def test_empty_traceback_is_reported_not_raised():
    parsed = analyzer.parse("")
    assert not parsed["found"]


# ── patcher ───────────────────────────────────────────────────────────────────

def _proposal_for(broken_file, staged_text: str, monkeypatch):
    """Build a proposal the way the patcher would, with the model stubbed."""
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    full = broken_file.read_text(encoding="utf-8")
    target_chunk = "def greet(name):\n    return name.strip()\n"
    replacement = "def greet(name):\n    return name.strip()\n" + staged_text
    return {
        "ok": True,
        "parsed": {"found": True, "file": str(broken_file), "line": 5,
                   "exception_type": "AttributeError", "exception_message": "x",
                   "risk": "low", "risk_reason": "leaf capability code (actions/)"},
        "file": str(broken_file),
        "line": 5,
        "risk": "low",
        "explanation": "Handle a missing name.",
        "target_chunk": target_chunk,
        "replacement_chunk": replacement,
        "staged": full.replace(target_chunk, replacement, 1),
    }


def test_apply_creates_a_backup_history_and_a_working_file(broken_file, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    proposal = _proposal_for(broken_file, "", monkeypatch)

    result = patcher.apply_patch(proposal)
    assert result["ok"], result["message"]
    assert Path(result["backup"]).exists()
    rows = patcher.history()
    assert rows[0]["status"] == "applied"
    assert rows[0]["file"] == str(broken_file)
    assert rows[0]["explanation"] == "Handle a missing name."


def test_rollback_restores_the_original_source(broken_file, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    original = broken_file.read_text(encoding="utf-8")
    patcher.apply_patch(_proposal_for(broken_file, "\n# patched marker\n", monkeypatch))
    assert "patched marker" in broken_file.read_text(encoding="utf-8")

    result = patcher.rollback("latest")
    assert result["ok"], result["message"]
    assert broken_file.read_text(encoding="utf-8") == original
    assert patcher.history()[0]["status"] == "rolled_back"


def test_a_patch_that_does_not_compile_is_never_left_on_disk(broken_file, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    original = broken_file.read_text(encoding="utf-8")
    proposal = _proposal_for(broken_file, "", monkeypatch)
    # Force the write to produce invalid syntax: compile() then fails and the
    # backup must come straight back.
    proposal["staged"] = original + "\ndef broken(:\n"
    monkeypatch.setattr(patcher, "_validate", lambda code, name: (True, ""))

    result = patcher.apply_patch(proposal)
    assert not result["ok"]
    assert broken_file.read_text(encoding="utf-8") == original
    statuses = [row["status"] for row in patcher.history()]
    assert "rolled_back" in statuses


def test_proposal_without_a_usable_patch_is_refused(broken_file, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    monkeypatch.setattr(patcher, "_ask", lambda prompt: {
        "explanation": "x", "target_chunk": "", "replacement_chunk": "pass"})
    result = patcher.propose(_tb_for(broken_file))
    assert not result["ok"]
    assert "usable" in result["message"]


def test_proposal_matching_two_places_is_refused(broken_file, monkeypatch):
    """An ambiguous chunk could patch the wrong function, so it is never used."""
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    original = broken_file.read_text(encoding="utf-8")
    broken_file.write_text(original.rstrip("\n") + "\n\n\ndef greet_again(name):\n"
                           "    return name.strip()\n", encoding="utf-8")
    monkeypatch.setattr(patcher, "_ask", lambda prompt: {
        "explanation": "x", "target_chunk": "    return name.strip()\n",
        "replacement_chunk": "    return (name or '').strip()\n"})
    result = patcher.propose(_tb_for(broken_file))
    assert not result["ok"]
    assert "exactly once" in result["message"]


def test_rollback_without_history_is_reported(scratch, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: scratch / "recovery")
    result = patcher.rollback("latest")
    assert not result["ok"]
    assert "no applied patch" in result["message"].lower()


# ── policy + service ──────────────────────────────────────────────────────────

def test_low_risk_files_are_auto_patched_and_high_risk_files_are_only_proposed(
        broken_file, monkeypatch):
    """This is the whole safety contract of auto_low_risk mode."""
    import config.settings as settings
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    monkeypatch.setattr(service, "_mode", lambda: "auto_low_risk")
    monkeypatch.setattr(service, "_recent", {})
    monkeypatch.setattr(service, "_session_patches", 0)
    monkeypatch.setattr(service, "_emit", lambda kind, payload: None)
    monkeypatch.setattr(service, "_say", lambda text: None)

    def _fake_propose(tb, parsed=None, context_notes=""):
        proposal = _proposal_for(broken_file, "", monkeypatch)
        proposal["risk"] = (parsed or {}).get("risk", "low")
        return proposal

    monkeypatch.setattr(patcher, "propose", _fake_propose)
    low = service.handle_now(_tb_for(broken_file), force=True)
    assert low["stage"] == "applied"

    high_parsed = {"found": True, "file": str(broken_file), "line": 5,
                   "exception_type": "AttributeError", "exception_message": "x",
                   "risk": "high", "risk_reason": "core code",
                   "traceback": "tb"}
    monkeypatch.setattr(analyzer, "parse", lambda text: dict(high_parsed))
    high = service.handle_now(_tb_for(broken_file), force=True)
    assert high["stage"] == "proposed"
    assert high["auto"] is False

    protected_parsed = dict(high_parsed, risk="protected",
                            risk_reason="ui.py is on the protected list")
    monkeypatch.setattr(analyzer, "parse", lambda text: dict(protected_parsed))
    protected = service.handle_now(_tb_for(broken_file), force=True)
    assert protected["stage"] == "proposed", \
        "a protected fault must still produce an approval proposal"
    assert protected["auto"] is False
    assert protected["proposal"].get("ok")


def test_propose_mode_never_writes(broken_file, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    monkeypatch.setattr(service, "_mode", lambda: "propose")
    monkeypatch.setattr(service, "_recent", {})
    monkeypatch.setattr(service, "_emit", lambda kind, payload: None)
    monkeypatch.setattr(patcher, "propose",
                        lambda tb, parsed=None, context_notes="": _proposal_for(broken_file, "", monkeypatch))
    original = broken_file.read_text(encoding="utf-8")
    outcome = service.handle_now(_tb_for(broken_file), force=True)
    assert outcome["stage"] == "proposed"
    assert broken_file.read_text(encoding="utf-8") == original


def test_repeated_failures_on_the_same_line_are_deduplicated(broken_file, monkeypatch):
    monkeypatch.setattr(service, "_mode", lambda: "off")
    monkeypatch.setattr(service, "_recent", {})
    monkeypatch.setattr(service, "_emit", lambda kind, payload: None)
    monkeypatch.setattr(service, "_say", lambda text: None)
    monkeypatch.setattr(patcher, "propose", lambda tb, parsed=None, context_notes="": {
        "ok": True, "parsed": parsed or {}, "file": str(broken_file), "line": 5,
        "risk": "low", "explanation": "x", "target_chunk": "a",
        "replacement_chunk": "b", "staged": ""})
    first = service.handle_now(_tb_for(broken_file), force=True)
    second = service.handle_now(_tb_for(broken_file))
    assert first["stage"] != "dedup"
    assert second["stage"] == "dedup"


def test_tool_handler_reports_status_without_any_history(monkeypatch, scratch):
    from recovery import tool
    monkeypatch.setattr(patcher, "_data_dir", lambda: scratch / "recovery")
    monkeypatch.setattr(service, "_mode", lambda: "off")
    out = tool.handler({"action": "status"})
    assert "auto-heal mode" in out.lower()
    assert "no patches" in out.lower()


def test_tool_handler_rejects_an_unknown_action():
    from recovery import tool
    assert "not a self-repair action" in tool.handler({"action": "explode"}).lower()


def test_tool_handler_refuses_a_bad_policy_mode():
    from recovery import tool
    assert "valid modes" in tool.handler({"action": "policy", "mode": "yolo"}).lower()


# ── boot sentry ───────────────────────────────────────────────────────────────

def test_boot_sentry_restores_a_patch_that_was_followed_by_a_crash(broken_file, monkeypatch):
    from recovery import boot_sentry
    data_dir = broken_file.parent / "recovery"
    monkeypatch.setattr(patcher, "_data_dir", lambda: data_dir)
    original = broken_file.read_text(encoding="utf-8")
    patcher.apply_patch(_proposal_for(broken_file, "\n# crashy patch\n", monkeypatch))

    crash = broken_file.parent / "jarvis_crash.log"
    crash.write_text("crash after patch", encoding="utf-8")
    # The crash has to be newer than the patch.
    later = time.time() + 5
    import os
    os.utime(crash, (later, later))
    monkeypatch.setattr(boot_sentry, "crash_log_path", lambda: crash)

    outcome = boot_sentry.check_and_recover()
    assert outcome["recovered"] is True
    assert broken_file.read_text(encoding="utf-8") == original
    assert patcher.history()[0]["status"] == "rolled_back_on_boot"


def test_boot_sentry_does_nothing_without_a_crash(scratch, monkeypatch):
    from recovery import boot_sentry
    monkeypatch.setattr(patcher, "_data_dir", lambda: scratch / "recovery")
    outcome = boot_sentry.check_and_recover()
    assert outcome["recovered"] is False


def test_history_file_is_valid_json(broken_file, monkeypatch):
    monkeypatch.setattr(patcher, "_data_dir", lambda: broken_file.parent / "recovery")
    patcher.apply_patch(_proposal_for(broken_file, "", monkeypatch))
    raw = json.loads((broken_file.parent / "recovery" / "patch_history.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and raw
