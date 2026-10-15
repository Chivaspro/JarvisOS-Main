"""Phase 3 wiring: every new tool is declared, dispatched, gated and switched.

These tests are the ones that catch the failures a unit test of a single module
cannot see — a tool declared but never dispatched, a dispatch branch for a name
the model can never send, a permission switch that does not exist in the config,
or a runner map that has fallen behind the registry.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from capabilities import actions, registry, router
from config.settings import FEATURE_DEFAULTS

PROJECT = Path(__file__).resolve().parents[2]

#: Everything this phase added, and the switch each one is gated on.
NEW_TOOLS = {
    "system_control": "process_control",
    "window_control": "window_control",
    "clipboard_sentry": "clipboard_sentry",
    "documents": "document_architect",
    "service_hub": "",
    "live_vision": "live_vision",
    "smart_home": "smart_home",
    "instagram": "instagram",
    "connect_devices": "connect",
}


def _main_source() -> str:
    return (PROJECT / "main.py").read_text(encoding="utf-8")


def _declared_tool_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "name"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    names.add(value.value)
    return names


def test_every_new_tool_is_declared_to_the_model():
    declared = _declared_tool_names(_main_source())
    missing = sorted(name for name in NEW_TOOLS if name not in declared)
    assert not missing, f"not declared in main.py: {missing}"


def test_every_new_tool_is_dispatched_by_execute_tool():
    source = _main_source()
    for name in NEW_TOOLS:
        assert f'"{name}"' in source, name
    # The grouped local tools share one dispatch branch; make sure the branch
    # actually names them rather than relying on a plugin fallback.
    for name in ("system_control", "window_control", "clipboard_sentry",
                 "documents", "smart_home", "instagram"):
        assert f'"{name}": _local_actions.handle' in source, name


def test_every_new_switch_exists_in_the_configuration():
    for tool, switch in NEW_TOOLS.items():
        if not switch:
            continue
        assert switch in FEATURE_DEFAULTS, f"{tool} is gated on missing {switch}"


def test_the_registry_and_the_runner_map_agree():
    for tool in registry.TOOLS:
        assert tool in actions._RUNNERS, f"{tool} has no runner"
        assert tool in router.LOCK_OF, f"{tool} has no lock domain"
        assert tool in router.ENGINE_OF, f"{tool} has no engine name"
        assert tool in router.LABEL_OF, f"{tool} has no label"
    assert set(actions._RUNNERS) == set(registry.TOOLS)


def test_a_tool_without_a_runner_is_refused_cleanly(monkeypatch):
    monkeypatch.setattr(actions, "_RUNNERS",
                        {k: v for k, v in actions._RUNNERS.items()
                         if k != registry.TOOL_WINDOW})
    out = actions.handle_window_control({"action": "tile_left"},
                                        enabled=lambda name: True)
    assert "no local runner" in out


def test_the_service_hub_switch_is_not_a_local_permission():
    # service_hub has its own switch per service (spotify, instagram, ...), so
    # it deliberately declares none itself.
    assert NEW_TOOLS["service_hub"] == ""


def test_every_new_permission_has_a_human_label():
    from security.permissions import label_for
    for switch in ("process_control", "system_settings", "window_control",
                   "clipboard_sentry", "document_architect", "live_vision",
                   "smart_home", "instagram", "connect"):
        label = label_for(switch)
        assert label and label != switch, f"{switch} has no label"
        assert "_" not in label, f"{switch} fell back to its key"


@pytest.mark.parametrize("tool,args", [
    ("documents", {"action": "create_docx", "content": "{}"}),
    ("smart_home", {"action": "turn_on", "host": "10.0.0.5"}),
    ("instagram", {"action": "profile", "user": "someone"}),
    ("system_control", {"action": "wifi_status"}),
    ("window_control", {"action": "snap_grid", "position": "left"}),
    # ...and the clipboard's WRITE side, which is the gated half: reading the
    # clipboard is deliberately free.
    ("clipboard_sentry", {"action": "write", "text": "x"}),
])
def test_a_switched_off_permission_refuses_before_the_runner(tool, args):
    out = actions._handle(tool, args, enabled=lambda name: False)
    assert "status=NOT_DONE" in out
    assert "switched off in Settings" in out


def test_the_settings_window_offers_every_new_switch():
    source = (PROJECT / "ui_settings.py").read_text(encoding="utf-8")
    for switch in ("process_control", "system_settings", "window_control",
                   "clipboard_sentry", "document_architect", "live_vision",
                   "smart_home", "instagram", "spotify", "connect"):
        assert f'"{switch}"' in source, f"{switch} is missing from Settings"


def test_the_api_tab_has_the_service_hub_and_the_mcp_list():
    source = (PROJECT / "ui_settings.py").read_text(encoding="utf-8")
    assert "SERVICE HUB" in source
    assert "_test_service" in source and "_authorise_spotify" in source
    assert "MCP SERVERS" in source


def test_the_repair_and_vision_paths_still_import():
    # The shutdown path references the live-vision stopper; a typo there would
    # only show up at exit, which is the worst place to find it.
    from vision import live_stream
    assert callable(live_stream.stop_live_vision_streams)
    source = _main_source()
    assert "stop_live_vision_streams" in source
    assert "vision.live_stream" in source
