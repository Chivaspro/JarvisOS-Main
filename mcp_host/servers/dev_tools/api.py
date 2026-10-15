"""Tools for the Developer Tools MCP Server."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from mcp_host.server import Tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _run_command(args: dict) -> dict:
    """Execute a CLI command in the workspace directory."""
    cmd_str = str(args.get("command", "")).strip()
    if not cmd_str:
        return {"error": "Missing 'command' argument"}
    cwd_str = str(args.get("cwd", "")).strip()
    cwd = Path(cwd_str).resolve() if cwd_str else PROJECT_ROOT
    timeout = max(1.0, min(float(args.get("timeout", 30.0)), 120.0))

    start_t = time.monotonic()
    try:
        proc = subprocess.run(
            cmd_str,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        elapsed = round(time.monotonic() - start_t, 3)
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        return {
            "exit_code": proc.returncode,
            "elapsed_seconds": elapsed,
            "stdout": stdout[:6000] if stdout else "",
            "stderr": stderr[:2000] if stderr else "",
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout} seconds", "ok": False}
    except Exception as exc:
        return {"error": f"Failed running command: {exc}", "ok": False}


def _git_status(args: dict) -> dict:
    """Get the current git branch, status, and modified files."""
    repo_dir = Path(str(args.get("repo_path", "")).strip() or PROJECT_ROOT).resolve()
    try:
        proc_branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        branch = proc_branch.stdout.strip()

        proc_status = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        raw_status = proc_status.stdout.strip()
        changes = [line.strip() for line in raw_status.splitlines() if line.strip()]
        return {
            "repo_path": str(repo_dir),
            "branch": branch or "detached / none",
            "is_clean": len(changes) == 0,
            "change_count": len(changes),
            "changes": changes[:50],
        }
    except Exception as exc:
        return {"error": f"Failed checking git status: {exc}"}


def _git_diff(args: dict) -> dict:
    """Get git diff of staged or unstaged changes."""
    repo_dir = Path(str(args.get("repo_path", "")).strip() or PROJECT_ROOT).resolve()
    cached = bool(args.get("staged", False))
    cmd = ["git", "diff", "--stat"] if not args.get("full_diff") else ["git", "diff"]
    if cached:
        cmd.append("--cached")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        diff_text = proc.stdout.strip()
        return {
            "staged": cached,
            "diff": diff_text[:5000] if diff_text else "(No changes)",
            "length": len(diff_text),
        }
    except Exception as exc:
        return {"error": f"Failed running git diff: {exc}"}


def _python_eval(args: dict) -> dict:
    """Evaluate a mathematical or Python expression."""
    expr = str(args.get("expression", "")).strip()
    if not expr:
        return {"error": "Missing 'expression' argument"}
    # Safe evaluation environment
    safe_globals = {
        "__builtins__": {
            "abs": abs, "round": round, "min": min, "max": max,
            "sum": sum, "len": len, "range": range, "int": int,
            "float": float, "str": str, "bool": bool, "list": list,
            "dict": dict, "set": set, "tuple": tuple,
        }
    }
    import math
    safe_globals["math"] = math
    try:
        result = eval(expr, safe_globals, {})
        return {"expression": expr, "result": result, "type": type(result).__name__}
    except Exception as exc:
        return {"expression": expr, "error": str(exc)}


TOOLS: list[Tool] = [
    Tool(
        name="runCommand",
        description="Execute a CLI shell command within the workspace directory.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command string to run"},
                "cwd": {"type": "string", "description": "Optional working directory"},
                "timeout": {"type": "number", "description": "Timeout in seconds (default 30.0)"},
            },
            "required": ["command"],
        },
        handler=_run_command,
    ),
    Tool(
        name="gitStatus",
        description="Inspect git repository status, branch name, and uncommitted files.",
        input_schema={
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Repository path (default project root)"},
            },
        },
        handler=_git_status,
    ),
    Tool(
        name="gitDiff",
        description="Inspect git diff summary or staged/unstaged changes.",
        input_schema={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Diff staged / cached changes (default false)"},
                "full_diff": {"type": "boolean", "description": "Return full diff instead of stat summary"},
            },
        },
        handler=_git_diff,
    ),
    Tool(
        name="pythonEval",
        description="Safely evaluate a mathematical or Python expression (supports math module).",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Expression to evaluate"},
            },
            "required": ["expression"],
        },
        handler=_python_eval,
    ),
]
