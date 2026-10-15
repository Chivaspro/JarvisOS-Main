"""Every Live tool declaration must be accepted by LiveConnectConfig.

A stray comma inside the ``system_control`` description turned it into a
tuple; pydantic rejected the whole setup message, so EVERY connect failed:
no voice, no wake word, an endless RECONNECTING loop (231 crashes logged,
safe mode engaged). These tests pin the declarations and the sanitizer that
now stands between a single bad tool and the voice session.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MAIN = (ROOT / "main.py").read_text(encoding="utf-8")


def _load_sanitizer():
    tree = ast.parse(MAIN)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_sanitize_tool_declarations")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["_sanitize_tool_declarations"]


def test_no_tool_description_in_main_is_a_tuple():
    for node in ast.walk(ast.parse(MAIN)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "description":
                    assert not isinstance(value, ast.Tuple), (
                        f"main.py:{value.lineno} description is a tuple (stray comma)")


def test_build_config_sanitizes_declarations():
    assert "_sanitize_tool_declarations(\n                TOOL_DECLARATIONS" in MAIN


def test_sanitizer_repairs_and_drops_bad_entries():
    sanitize = _load_sanitizer()
    out = sanitize([
        {"name": "ok", "description": "fine", "parameters": {"type": "OBJECT"}},
        {"name": "tup", "description": ("a ", "b")},
        {"name": "ok", "description": "duplicate"},
        {"name": "", "description": "no name"},
        {"name": "badparams", "description": "x", "parameters": "nope"},
        "not a dict",
    ])
    assert [d["name"] for d in out] == ["ok", "tup"]
    assert out[1]["description"] == "a b"


def test_sanitized_declarations_pass_live_connect_config():
    import pytest
    types = pytest.importorskip("google.genai.types")
    sanitize = _load_sanitizer()
    decls = sanitize([{"name": "x", "description": ("Operate ", "this PC")}])
    types.LiveConnectConfig(response_modalities=["AUDIO"],
                            tools=[{"function_declarations": decls}])
