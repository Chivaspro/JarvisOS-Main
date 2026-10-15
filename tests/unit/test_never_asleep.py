"""J.A.R.V.I.S. never sleeps — the invariants, read straight from the sources.

The sleep state used to be real: the reconnect loop set SLEEPING at the end of
every connection cycle, an invalid key set SLEEPING, and muting the microphone
made the reactor *look* asleep. These checks are deliberately source-level and
cheap: they fail if any of those three comes back, whoever writes it and
whatever the runtime state happens to be.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
UI = (ROOT / "ui.py").read_text(encoding="utf-8")


def test_nothing_in_the_backend_sets_a_sleeping_state():
    calls = re.findall(r"set_state\(\s*['\"]([^'\"]+)['\"]", MAIN)
    assert calls, "the backend must still report its states"
    sleeping = [c for c in calls if "SLEEP" in c.upper()
                or c.strip().upper() in ("HIBERNATE", "DORMANT", "ASLEEP")]
    assert sleeping == [], (
        "the backend asks the HUD to sleep again: " + ", ".join(sleeping))


def test_the_backend_never_broadcasts_a_sleeping_status():
    states = re.findall(r"['\"]state['\"]\s*:\s*['\"]([^'\"]+)['\"]", MAIN)
    assert states, "the backend must still report a status"
    sleeping = [s for s in states if "SLEEP" in s.upper()]
    assert sleeping == [], "a dashboard client is told the host is asleep: " + str(sleeping)


def test_the_backend_reports_the_two_states_that_replaced_it():
    assert 'set_state("RECONNECTING")' in MAIN, (
        "the end of a connection cycle is a retry, and says so")
    assert 'set_state("NEEDS KEY")' in MAIN, (
        "an invalid key is an attention state, not a sleeping one")
    assert '"state": "reconnecting"' in MAIN
    assert '"state": "needs_key"' in MAIN


def test_the_reactor_has_no_sleeping_target():
    block = UI[UI.index("_STATE_TARGETS = {"):]
    block = block[: block.index("\n        }")]
    names = re.findall(r"'([A-Z ]+)':\s*\{", block)
    assert "IDLE" in names and "LISTENING" in names
    assert not [n for n in names if "SLEEP" in n], names


def test_the_reactor_gained_awake_states_for_mute_and_reconnect():
    block = UI[UI.index("_STATE_TARGETS = {"):]
    block = block[: block.index("\n        }")]
    for state in ("MUTED", "RECONNECTING", "NEEDS KEY"):
        assert f"'{state}'" in block, state


def test_muting_is_no_longer_an_alias_for_sleeping():
    assert "'MUTED': 'SLEEPING'" not in UI
    assert "'SLEEPING': 'SLEEPING'" not in UI


def test_the_status_rail_can_never_print_a_sleepy_word():
    assert "'MUTED': ('⊘ MIC OFF'" in UI
    assert "'RECONNECTING': ('⟳ RECONNECTING'" in UI
    # The fallback is the only path a state with no line of its own can take,
    # and it refuses every sleepy word rather than printing one.
    assert "elif any(word in s for word in ('SLEEP', 'DORMANT', 'ASLEEP')):" in UI
    assert "('● READY', C.GREEN, 'ONLINE')" in UI


def test_the_mute_toggle_still_reports_a_real_state():
    """Muting sets MUTED (a live state now), and unmuting returns to LISTENING."""
    assert "_apply_state('MUTED' if self._muted else 'LISTENING')" in UI


def test_the_sleeping_of_the_machine_is_untouched():
    """Only J.A.R.V.I.S. stopped sleeping: the power action stays available."""
    assert '"lock", "sleep", "hibernate"' in MAIN or "sleep, hibernate" in MAIN


def test_the_wake_detector_is_unchanged_and_still_opt_in():
    """No wake word gates a reply — the detector only arms barge-in."""
    source = (ROOT / "app" / "brain_bridge.py").read_text(encoding="utf-8")
    assert "def wake_enabled() -> bool:" in source
    assert "if not bool(feats.get(\"wake_word\")):" in source
