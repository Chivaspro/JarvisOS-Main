"""The Skill Crucible — the verification gate every forged tool must pass.

Three stages, in order, cheapest first:

1. **AST validation** (``validate_ast``) — the candidate must parse, define the
   Jarvis plugin entry point ``run(parameters, player=None, session_memory=None)``,
   and contain none of the banned destructive terms. This runs in-process on a
   string; it never executes anything.
2. **Dependency resolution** (``extract_dependencies`` / ``resolve_dependencies``)
   — top-level third-party imports are detected and probed in a child process.
   A missing package is installed into the *running* interpreter
   (``sys.executable -m pip install``) when installation is allowed, exactly
   like a human adding a requirement.
3. **Sandbox execution** (``run_sandbox_test``) — the candidate source is
   executed by a **child process** that builds a namespace, calls ``run()`` for
   every supplied test case and prints a JSON verdict. The module is never
   imported into the live interpreter before it passes, so a hostile or just
   broken candidate can only fail its own process.

A failure at any stage returns ``(False, reason)`` with a sentence the assistant
can read out; nothing here raises for a rejected candidate.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "IMPORT_TO_PIP",
    "REQUIRED_ENTRY",
    "analyze_code",
    "extract_dependencies",
    "resolve_dependencies",
    "run_sandbox_test",
    "scan_hazards",
    "validate_ast",
    "validate_output_contract",
    "verify",
]

#: The entry point every forged tool must expose (the Jarvis plugin contract).
REQUIRED_ENTRY = "run"

#: Terms that must never appear in generated code or its string literals.
#: Deliberately short and unambiguous: these are the things that destroy a
#: machine or the assistant itself, not merely "risky" operations.
BANNED_TERMS: tuple[str, ...] = (
    "format c:",
    "format d:",
    "format /fs",
    "diskpart",
    "vssadmin",
    "bcdedit",
    "\\system32",
    "\\syswow64",
    "shutil.rmtree(\"c:",
    "del /f /s /q c:",
)

#: Names that belong to Jarvis itself; a forged tool may import them but may
#: never be *named* one of them (the vault applies the same rule).
PROTECTED_NAMES: frozenset[str] = frozenset({
    "main", "ui", "ui_settings", "ui_theme", "config", "security",
    "auto_heal", "patch", "boot_sentry", "requirements",
})

STANDARD_LIB_MODULES: frozenset[str] = frozenset({
    "abc", "argparse", "array", "ast", "asyncio", "base64", "binascii", "bisect",
    "calendar", "cmath", "collections", "colorsys", "concurrent", "configparser",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime", "decimal",
    "difflib", "dis", "doctest", "email", "enum", "errno", "faulthandler",
    "filecmp", "fileinput", "fnmatch", "fractions", "functools", "gc", "getopt",
    "getpass", "gettext", "glob", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "imaplib", "imghdr", "importlib", "inspect", "io", "ipaddress",
    "itertools", "json", "keyword", "linecache", "locale", "logging", "lzma",
    "math", "mimetypes", "mmap", "multiprocessing", "netrc", "numbers",
    "operator", "os", "pathlib", "pickle", "pkgutil", "platform", "plistlib",
    "poplib", "pprint", "queue", "quopri", "random", "re", "reprlib", "resource",
    "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtplib", "socket", "socketserver", "sqlite3", "ssl",
    "stat", "statistics", "string", "stringprep", "struct", "subprocess", "sys",
    "sysconfig", "tabnanny", "tarfile", "tempfile", "textwrap", "threading",
    "time", "timeit", "tkinter", "token", "tokenize", "trace", "traceback",
    "tracemalloc", "types", "typing", "unicodedata", "unittest", "urllib",
    "uuid", "venv", "warnings", "wave", "weakref", "webbrowser", "wsgiref",
    "xml", "xmlrpc", "zipapp", "zipfile", "zlib", "zoneinfo",
})

#: Import name -> pip name, only where they differ.
IMPORT_TO_PIP: dict[str, str] = {
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "speedtest": "speedtest-cli",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "serial": "pyserial",
    "websocket": "websocket-client",
    "win32com": "pywin32",
    "win32api": "pywin32",
    "kasa": "python-kasa",
    "instagrapi": "instagrapi",
}

#: Import roots that always resolve inside this project.
LOCAL_ROOTS: frozenset[str] = frozenset({
    "actions", "app", "assets", "brain", "capabilities", "coding", "config",
    "core", "dashboard", "data", "documents", "evolution", "knowledge", "mcp",
    "memory", "observability", "recovery", "security", "vision", "voice",
})


def _python_executable() -> str:
    """The interpreter that must own any installed dependency.

    A ``pythonw.exe`` launch (the .vbs entry point) has no console and
    ``pythonw -m pip`` is a coin flip, so the console twin is preferred when it
    exists — the same rule ``capabilities/proc.py`` uses for its workers.
    """
    exe = sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[:-len("pythonw.exe")] + "python.exe"
        if Path(candidate).exists():
            return candidate
    return exe


# ── stage 1: static analysis ──────────────────────────────────────────────────

def validate_ast(code: str) -> tuple[bool, str]:
    """Parse *code* and enforce the plugin contract + safety bans.

    Returns ``(True, "")`` when the candidate may continue to the sandbox.
    """
    if not code or not str(code).strip():
        return False, "The generated skill was empty."

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        line = f" (line {exc.lineno})" if exc.lineno else ""
        return False, f"Syntax error{line}: {exc.msg}"
    except Exception as exc:  # pragma: no cover - ast.parse only raises SyntaxError
        return False, f"Could not parse the candidate: {exc}"

    has_entry = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == REQUIRED_ENTRY
        for node in tree.body
    )
    if not has_entry:
        return False, (f"The skill must define a top-level '{REQUIRED_ENTRY}"
                       f"(parameters, player=None, session_memory=None)' function.")

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower().replace("/", "\\")
            for banned in BANNED_TERMS:
                if banned in low:
                    return False, (f"Refused on safety grounds: the generated code "
                                   f"mentions '{banned}'.")

    return True, ""


def declared_name(code: str) -> str:
    """Best-effort read of ``PLUGIN['name']`` from candidate source ("" if absent)."""
    try:
        tree = ast.parse(code)
    except Exception:
        return ""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            continue
        if isinstance(value, dict):
            return str(value.get("name") or "").strip()
    return ""


# ── advanced static analysis (hazards, contract, quality) ─────────────────────

#: Constructs that are not banned outright but are worth *surfacing*: a forged
#: skill may legitimately use them, so the report names what the tool does rather
#: than refusing it. Deliberately a warning list — blocking legitimate tools was
#: never the goal, and a silent tool is a worse tool.
_HAZARD_RULES: tuple[tuple[str, str, str], ...] = (
    ("os.system", "shell", "calls the OS shell via os.system"),
    ("subprocess", "process", "spawns or controls a subprocess"),
    ("ctypes", "native", "loads native/OS libraries via ctypes"),
    ("pickle.loads", "deserialise", "deserialises pickle data"),
    ("marshal.loads", "deserialise", "deserialises marshalled data"),
    ("eval", "dynamic-code", "evaluates code dynamically (eval)"),
    ("exec", "dynamic-code", "executes code dynamically (exec)"),
    ("socket", "network", "opens raw network sockets"),
    ("shutil.rmtree", "filesystem", "removes directory trees"),
    ("winreg", "registry", "reads or writes the Windows registry"),
    ("pyautogui", "input", "drives the mouse and keyboard"),
    ("keyboard", "input", "hooks the keyboard"),
)


def scan_hazards(code: str) -> list[dict]:
    """Structured list of risky-but-allowed constructs the candidate uses.

    Returns ``[{"hazard", "kind", "detail"}]``. Pure AST: never executes the
    candidate and never raises. Used as a report note, not a gate.
    """
    out: list[dict] = []
    try:
        tree = ast.parse(code)
    except Exception:
        return out
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            cur = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            names.add(".".join(reversed(parts)))
        elif isinstance(node, ast.Name):
            names.add(node.id)
    for needle, kind, detail in _HAZARD_RULES:
        if any(n == needle or n.endswith("." + needle) for n in names):
            out.append({"hazard": needle, "kind": kind, "detail": detail})
    return out


def validate_output_contract(code: str) -> tuple[bool, str]:
    """Static check that ``run`` always returns something.

    The forge contract is a spoken sentence. A candidate whose ``run`` has no
    ``return`` at all, or a bare ``return`` on some path, is refused here rather
    than at runtime, where the plugin loader would report "returned nothing"
    long after the Crucible had said it was fine.
    """
    try:
        tree = ast.parse(code)
    except Exception as exc:
        return False, f"Could not parse the candidate: {exc}"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == REQUIRED_ENTRY:
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            if not returns:
                return False, (f"'{REQUIRED_ENTRY}' never returns anything — it must "
                               f"return a short spoken sentence (str).")
            if any(r.value is None for r in returns):
                return False, (f"'{REQUIRED_ENTRY}' contains a bare 'return' — every "
                               f"path must return a spoken sentence (str).")
            return True, ""
    return False, f"The skill must define a top-level '{REQUIRED_ENTRY}' function."


def _max_nesting(node: ast.AST, depth: int = 0) -> int:
    """Deepest nesting of control-flow blocks under *node* (indentation proxy)."""
    best = depth
    for child in ast.iter_child_nodes(node):
        step = 1 if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While,
                                       ast.With, ast.AsyncWith, ast.Try)) else 0
        best = max(best, _max_nesting(child, depth + step))
    return best


def analyze_code(code: str) -> dict:
    """Static quality metrics plus a 0-100 score (never raises).

    The score is a coarse, honest signal — documented module, guarded failure
    paths, bounded nesting — not a judgement of the tool's usefulness. It exists
    so the Evolution tab can show *something* real about a skill besides "it ran".
    """
    metrics = {
        "sloc": 0, "functions": 0, "classes": 0, "imports": 0,
        "has_docstring": False, "try_blocks": 0, "max_depth": 0,
        "quality_score": 0,
    }
    try:
        tree = ast.parse(code)
    except Exception:
        return metrics
    metrics["sloc"] = sum(1 for line in str(code).splitlines()
                          if line.strip() and not line.strip().startswith("#"))
    metrics["functions"] = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                               for n in ast.walk(tree))
    metrics["classes"] = sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
    metrics["imports"] = sum(isinstance(n, (ast.Import, ast.ImportFrom))
                             for n in ast.walk(tree))
    metrics["try_blocks"] = sum(isinstance(n, ast.Try) for n in ast.walk(tree))
    doc = ast.get_docstring(tree)
    metrics["has_docstring"] = bool(doc and doc.strip())
    metrics["max_depth"] = _max_nesting(tree)

    score = 40
    if metrics["has_docstring"]:
        score += 8
    if metrics["functions"] >= 1:
        score += 10
    score += min(12, metrics["try_blocks"] * 6)
    score += min(10, metrics["imports"] * 2)
    if metrics["sloc"] >= 8:
        score += 8
    if metrics["max_depth"] <= 4:
        score += 12
    metrics["quality_score"] = max(0, min(100, score))
    return metrics


# ── stage 2: dependencies ─────────────────────────────────────────────────────

def extract_dependencies(code: str) -> list[str]:
    """Top-level third-party import roots the candidate needs."""
    try:
        tree = ast.parse(code)
    except Exception:
        return []

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if node.level == 0 else []
        else:
            continue
        for name in names:
            root = name.split(".")[0].strip()
            if not root:
                continue
            if root in STANDARD_LIB_MODULES or root in LOCAL_ROOTS:
                continue
            found.add(root)
    return sorted(found)


def _probe_import(module: str, timeout: float = 10.0) -> bool:
    """Is *module* importable by the interpreter that will run the skill?"""
    try:
        proc = subprocess.run(
            [_python_executable(), "-c", f"import {module}"],
            capture_output=True, timeout=timeout,
        )
        return proc.returncode == 0
    except Exception:
        return False


def resolve_dependencies(dependencies: Iterable[str], *,
                         allow_install: bool = True,
                         timeout: float = 180.0) -> tuple[bool, str]:
    """Make sure every dependency is importable, installing what is missing.

    ``allow_install=False`` turns the stage into a pure check: a missing package
    is reported as a failure instead of being installed. Returns
    ``(ok, sentence)`` — the sentence always names exactly what happened.
    """
    deps = [str(d).strip() for d in dependencies if str(d).strip()]
    if not deps:
        return True, "No external dependencies required."

    missing = [d for d in deps if not _probe_import(d)]
    if not missing:
        return True, "All dependencies already installed."

    if not allow_install:
        return False, ("Missing dependencies: " + ", ".join(missing)
                       + " (installation is switched off in Settings ▸ EVOLUTION).")

    exe = _python_executable()
    installed: list[str] = []
    for dep in missing:
        pip_name = IMPORT_TO_PIP.get(dep, dep)
        try:
            proc = subprocess.run(
                [exe, "-m", "pip", "install", pip_name, "--quiet"],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"Timed out installing '{pip_name}'."
        except Exception as exc:
            return False, f"Could not run pip for '{pip_name}': {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            sample = detail[-1][:180] if detail else "unknown pip error"
            return False, f"Failed to install '{pip_name}': {sample}"
        if not _probe_import(dep):
            return False, (f"'{pip_name}' reported a successful install but still "
                           f"cannot be imported by {Path(exe).name}.")
        installed.append(pip_name)

    return True, "Installed: " + ", ".join(installed)


# ── stage 3: sandbox execution ────────────────────────────────────────────────

_HARNESS = r'''
import asyncio
import inspect
import json
import sys
import time
import traceback

SOURCE = __SOURCE__
TEST_CASES = json.loads(__CASES__)

namespace = {"__name__": "__forged_skill__"}
try:
    exec(compile(SOURCE, "<forged_skill>", "exec"), namespace)
except Exception:
    print(json.dumps({"fatal": traceback.format_exc()[-1500:]}))
    sys.exit(0)

entry = namespace.get("run")
if not callable(entry):
    print(json.dumps({"fatal": "the candidate does not define run()"}))
    sys.exit(0)

if not TEST_CASES:
    TEST_CASES = [{"input": {}}]

signature = inspect.signature(entry)
parameters = signature.parameters
accepts_var_kw = any(p.kind == p.VAR_KEYWORD for p in parameters.values())

results = []
for index, case in enumerate(TEST_CASES):
    if not isinstance(case, dict):
        case = {}
    args = case.get("input") if isinstance(case.get("input"), dict) else {}
    # Called exactly the way app/plugin_loader._call_run calls a plugin: the
    # argument dict first, then only the context parameters the skill declares.
    kwargs = {}
    if accepts_var_kw or "parameters" in parameters:
        kwargs["parameters"] = args
    if accepts_var_kw or "player" in parameters:
        kwargs["player"] = None
    if accepts_var_kw or "session_memory" in parameters:
        kwargs["session_memory"] = None
    try:
        started = time.perf_counter()
        if inspect.iscoroutinefunction(entry):
            value = asyncio.run(entry(**kwargs)) if kwargs else asyncio.run(entry(args))
        else:
            value = entry(**kwargs) if kwargs else entry(args)
    except Exception as exc:
        results.append({"index": index, "success": False, "error": str(exc),
                        "elapsed_ms": 0.0,
                        "traceback": traceback.format_exc()[-1200:]})
        continue
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)

    if value is None:
        results.append({"index": index, "success": True, "output_type": "NoneType",
                        "repeat_ok": False, "elapsed_ms": elapsed_ms,
                        "output": "Operation completed with no output."})
        continue

    failed, reason = False, ""
    if isinstance(value, dict):
        if value.get("error"):
            failed, reason = True, str(value["error"])
        elif value.get("success") is False:
            failed, reason = True, str(value.get("message") or "run() reported success=False")
    elif isinstance(value, str) and value.lower().startswith(("error:", "failed:")):
        failed, reason = True, value

    # Idempotency probe: call the skill a second time with the SAME input. A
    # crash or error-object on the repeat is recorded, but it never fails the
    # candidate -- some tools legitimately consume their input (delete a file,
    # play a track), so non-idempotence is reported, not punished.
    repeat_ok = True
    try:
        if inspect.iscoroutinefunction(entry):
            again = asyncio.run(entry(**kwargs)) if kwargs else asyncio.run(entry(args))
        else:
            again = entry(**kwargs) if kwargs else entry(args)
        if isinstance(again, dict) and (again.get("error") or again.get("success") is False):
            repeat_ok = False
    except Exception:
        repeat_ok = False

    results.append({"index": index, "success": not failed,
                    "error": reason, "output_type": type(value).__name__,
                    "repeat_ok": repeat_ok, "elapsed_ms": elapsed_ms,
                    "output": str(value)[:400]})

print(json.dumps({"results": results}))
'''


def run_sandbox_test(code: str, test_cases: Sequence[dict] | None = None, *,
                     timeout: float = 75.0,
                     env: dict[str, str] | None = None,
                     cwd: str | Path | None = None) -> tuple[bool, str, dict]:
    """Execute *code* in a child process against *test_cases*.

    Returns ``(passed, message, telemetry)``. The telemetry always carries
    ``elapsed_s`` and, on a run, the per-case verdicts.
    """
    cases = list(test_cases or [{"input": {}}])
    harness = (_HARNESS
               .replace("__SOURCE__", repr(str(code)))
               .replace("__CASES__", repr(json.dumps(cases))))

    started = time.time()
    try:
        proc = subprocess.run(
            [_python_executable(), "-c", harness],
            capture_output=True, text=True, timeout=timeout,
            env=env, cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        return False, (f"The sandbox timed out after {timeout:g}s — the skill "
                       f"never returned."), {"elapsed_s": timeout}
    except Exception as exc:
        return False, f"The sandbox could not start: {exc}", {"elapsed_s": 0.0}

    elapsed = round(time.time() - started, 2)
    payload: dict[str, Any] = {}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break

    if payload.get("fatal"):
        detail = str(payload["fatal"]).strip().splitlines()
        return False, ("The skill failed to load in the sandbox: "
                       + (detail[-1][:240] if detail else "unknown error")), {
                           "elapsed_s": elapsed, "output": str(payload["fatal"])[-800:]}

    results = payload.get("results")
    if not isinstance(results, list):
        stderr = (proc.stderr or "").strip().splitlines()
        detail = stderr[-1][:240] if stderr else "the sandbox produced no verdict"
        return False, f"The sandbox produced no verdict: {detail}", {"elapsed_s": elapsed}

    failed = [r for r in results if not r.get("success")]
    telemetry = {
        "elapsed_s": elapsed,
        "results": results,
        # A second run with the same input that still succeeded. Informational:
        # it never decides pass/fail, it is shown in the report.
        "idempotent": all(bool(r.get("repeat_ok")) for r in results) if results else True,
        "output_types": sorted({str(r.get("output_type") or "unknown") for r in results}),
    }
    if failed:
        first = failed[0]
        return False, (f"Sandbox test {int(first.get('index', 0)) + 1} failed: "
                       f"{first.get('error') or 'unknown error'}"), telemetry
    return True, f"Passed {len(results)}/{len(results)} sandbox tests in {elapsed:g}s", telemetry


# ── the orchestrator: one call, one structured report ─────────────────────────

def verify(code: str,
           test_cases: Sequence[dict] | None = None, *,
           allow_install: bool = True,
           timeout: float = 75.0,
           require_output_contract: bool = True) -> dict:
    """Run every Crucible stage and return a structured verdict.

    This is the single entry point the Forge, the registry and the Settings UI
    share, so all three report the *same* thing. The returned dict is::

        {
          "ok": bool,
          "message": str,               # one sentence, speakable
          "stages": {ast, contract, dependencies, sandbox: {ok, detail}},
          "hazards": [{hazard, kind, detail}],
          "metrics": {..., "quality_score": int},
          "dependencies": [str],
          "telemetry": {...},           # from the sandbox run
          "verified_at": float,
        }

    Never raises. A rejected candidate simply carries ``ok=False`` and the reason.
    """
    report: dict = {
        "ok": False, "message": "", "stages": {}, "hazards": [],
        "metrics": {}, "dependencies": [], "telemetry": {},
        "verified_at": time.time(),
    }

    ok, message = validate_ast(code)
    report["stages"]["ast"] = {"ok": ok, "detail": message}
    if not ok:
        report["message"] = message
        return report

    if require_output_contract:
        ok, message = validate_output_contract(code)
        report["stages"]["contract"] = {"ok": ok, "detail": message}
        if not ok:
            report["message"] = message
            return report

    report["hazards"] = scan_hazards(code)
    report["metrics"] = analyze_code(code)

    deps = extract_dependencies(code)
    report["dependencies"] = deps
    ok, message = resolve_dependencies(deps, allow_install=allow_install)
    report["stages"]["dependencies"] = {"ok": ok, "detail": message}
    if not ok:
        report["message"] = message
        return report

    passed, message, telemetry = run_sandbox_test(code, test_cases, timeout=timeout)
    report["stages"]["sandbox"] = {"ok": passed, "detail": message}
    report["telemetry"] = telemetry
    if not passed:
        report["message"] = message
        return report

    report["ok"] = True
    score = int(report["metrics"].get("quality_score") or 0)
    note = (f"; {len(report['hazards'])} hazard note(s)" if report["hazards"] else "")
    report["message"] = f"Verified in the Crucible — {message}. Quality {score}/100{note}."
    return report
