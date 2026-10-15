"""J.A.R.V.I.S. never sleeps — the HUD side of the contract.

The sources are checked in ``tests/unit/test_never_asleep.py``; this file proves
the *behaviour*: a sleeping state asked for by anything (an older config, a
plugin, a dashboard client) lands on an awake reactor, and the two states that
used to look asleep — a muted microphone and a reconnecting session — are now
their own live states.
"""
from __future__ import annotations

import pytest

from .test_camera_pipeline import qapp, ui  # noqa: F401  (fixtures)


@pytest.fixture()
def logo(qapp):
    widget = ui.RefinedArcLogo(size=200)
    yield widget
    widget.deleteLater()


@pytest.fixture()
def panel(qapp):
    host = ui.RefinedCommandWindow(None)
    qapp.processEvents()
    yield host
    try:
        host.close()
    except Exception:
        pass
    qapp.processEvents()


# ── the reactor ──────────────────────────────────────────────────────────────

def test_the_reactor_has_no_sleeping_target_at_all(logo):
    assert "SLEEPING" not in logo._STATE_TARGETS
    assert "SLEEP" not in logo._STATE_TARGETS


@pytest.mark.parametrize("asked", ["SLEEPING", "SLEEP", "ASLEEP", "DORMANT", "sleeping"])
def test_asking_for_sleep_lands_on_idle(logo, asked):
    assert logo.set_state(asked) is None
    assert logo.state == "IDLE", f"{asked} must not put the reactor to sleep"


def test_sleeping_looks_awake_not_dim(logo):
    """IDLE's own energy/glow/dim — the sleeping look is gone, not renamed."""
    logo.set_state("SLEEPING")
    idle = dict(logo._STATE_TARGETS["IDLE"])
    for key in ("energy", "glow", "dim", "spread"):
        assert float(logo._target[key]) == pytest.approx(float(idle[key])), key


def test_a_muted_microphone_keeps_the_reactor_live(logo):
    logo.set_state("MUTED")
    assert logo.state == "MUTED"
    idle = logo._STATE_TARGETS["IDLE"]
    assert float(logo._target["energy"]) >= float(idle["energy"])
    assert float(logo._target["dim"]) >= 1.0, "a muted mic must not dim the reactor"


def test_connecting_and_reconnecting_share_the_live_state(logo):
    logo.set_state("CONNECTING")
    assert logo.state == "RECONNECTING", (
        "coming up and coming back are the same live state, not 'busy'")


def test_reconnecting_is_a_live_state_not_a_sleeping_one(logo):
    logo.set_state("RECONNECTING")
    assert logo.state == "RECONNECTING"
    assert float(logo._target["energy"]) > float(logo._STATE_TARGETS["IDLE"]["energy"])
    assert float(logo._target["dim"]) >= 1.0


def test_needing_a_key_is_an_attention_state(logo):
    logo.set_state("NEEDS KEY")
    assert logo.state == "NEEDS KEY"
    assert float(logo._target["dim"]) >= 1.0


# ── the status rail ──────────────────────────────────────────────────────────

def test_the_rail_says_mic_off_rather_than_asleep(panel):
    panel.set_status("MUTED")
    assert "MIC OFF" in panel._status_signal.text()
    assert panel._status_mode.text() == "MUTED"


def test_the_rail_names_the_retry(panel):
    panel.set_status("RECONNECTING")
    assert "RECONNECTING" in panel._status_signal.text()
    assert panel._status_mode.text() == "LINK"


def test_the_rail_never_prints_a_sleepy_word(panel):
    for state in ("SLEEPING", "SLEEP", "ASLEEP"):
        panel.set_status(state)
        assert "SLEEP" not in panel._status_signal.text().upper()
        assert "SLEEP" not in panel._status_mode.text().upper()
        assert panel._status_signal.text(), "a state must still produce a label"


def test_the_rail_still_reports_the_normal_states(panel):
    for state, expected in (("IDLE", "READY"), ("THINKING", "PROCESSING"),
                            ("SPEAKING", "SPEAKING"), ("LISTENING", "LISTENING")):
        panel.set_status(state)
        assert expected in panel._status_signal.text(), state
