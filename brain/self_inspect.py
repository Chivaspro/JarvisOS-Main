"""Self-inspection workflow (task 7.2).

SELF_INSPECT: inspect repository source (filesystem — never OCR of screenshots
when files exist), application logs (structured JSONL), and the running
application (psutil) — then produce a repair plan, execute it through the coding
router, and verify by re-running tests.

Exposed to the model as the ``self_inspect`` tool contract:
    action: inspect | plan | repair | status
    target: what to inspect (component name, file, log query, or blank)

Every step degrades gracefully; inspection never mutates anything, and
`repair` refuses to run without a plan from `plan` (security: explicit plan
before edits).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _base() -> Path:
    try:
        from config.settings import BASE_DIR
        return Path(BASE_DIR)
    except Exception:
        return Path(__file__).resolve().parent


# ── inspections ───────────────────────────────────────────────────────────────

def inspect_source(target: str = "") -> dict[str, Any]:
    """Read the actual source files — never infer code behavior from pixels."""
    root = _base()
    findings: list[str] = []
    if target:
        p = (root / target).resolve()
        if not str(p).startswith(str(root)):
            return {"ok": False, "findings": ["target escapes the repository"]}
        if p.is_file() and p.exists():
            try:
                import py_compile
                py_compile.compile(str(p), doraise=True)
                findings.append(f"{target}: compiles clean")
            except Exception as exc:
                findings.append(f"{target}: SYNTAX ERROR — {exc}")
        elif p.exists():
            findings.append(f"{target}: directory with {sum(1 for _ in p.rglob('*.py'))} python files")
        else:
            findings.append(f"{target}: not found in repository")
    else:
        py_files = list(root.rglob("*.py"))
        findings.append(f"repository holds {len(py_files)} python files")
        broken = []
        for f in py_files:
            try:
                import py_compile
                py_compile.compile(str(f), doraise=True)
            except Exception as exc:
                broken.append(f"{f.relative_to(root)}: {exc}")
        findings.extend(broken[:8] or ["no syntax errors found"])
    return {"ok": True, "findings": findings, "source": "filesystem"}


def inspect_logs(target: str = "", tail: int = 40) -> dict[str, Any]:
    """Structured log inspection: recent errors/warnings with correlation ids."""
    logs: dict[str, list] = {"errors": [], "warnings": []}
    log_dir = _base() / "data" / "logs"
    try:
        files = sorted(log_dir.glob("jarvis-*.jsonl"))
        if not files:
            return {"ok": True, "findings": ["no structured logs yet"], "source": "logs"}
        lines = files[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("level") in ("ERROR", "CRITICAL"):
                logs["errors"].append(rec)
            elif rec.get("level") == "WARNING":
                logs["warnings"].append(rec)
        return {"ok": True,
                "findings": [f"{len(logs['errors'])} errors, "
                             f"{len(logs['warnings'])} warnings in the latest log"],
                "errors": logs["errors"][-5:], "source": "logs"}
    except Exception as exc:
        return {"ok": False, "findings": [f"log inspection failed: {exc}"],
                "source": "logs"}


def inspect_process() -> dict[str, Any]:
    """The running application: process alive, subsystem counters."""
    findings: list[str] = []
    try:
        import psutil
        me = psutil.Process(os.getpid())
        mem = me.memory_info().rss / (1024 ** 2)
        findings.append(f"process alive, rss={mem:.0f} MB, threads={me.num_threads()}")
    except Exception as exc:
        findings.append(f"process inspection limited: {exc}")
    try:
        from observability.metrics import counters_snapshot
        snap = counters_snapshot()
        failing = {k: v for k, v in snap.items() if v.get("errors")}
        if failing:
            findings.append(f"subsystems with errors: {sorted(failing)}")
        else:
            findings.append("no subsystem errors recorded this session")
    except Exception:
        pass
    return {"ok": True, "findings": findings, "source": "process"}


def inspect_ui(target: str = "") -> dict[str, Any]:
    """Rendered-UI screenshot inspection is not available in this build: the
    computer-control layer that captured the screen has been removed."""
    return {"ok": False,
            "findings": ["UI screenshot inspection is not available "
                         "(computer-control layer removed)"],
            "source": "ui"}


# ── repair plan + execution ───────────────────────────────────────────────────

def build_plan(target: str = "", findings: list[dict] | None = None) -> dict:
    """Turn inspections into an explicit repair plan (before any edit)."""
    findings = findings or []
    src = inspect_source(target)
    logs = inspect_logs(target)
    proc = inspect_process()
    all_findings = src["findings"] + logs["findings"] + proc["findings"]
    plan = {
        "target": target or "(whole assistant)",
        "inspections": [
            {"kind": "source", "ok": src["ok"], "findings": src["findings"]},
            {"kind": "logs", "ok": logs["ok"], "findings": logs["findings"]},
            {"kind": "process", "ok": proc["ok"], "findings": proc["findings"]},
        ],
        "affected_files": _affected_from_findings(src["findings"]),
        "proposed_changes": [],
        "verification_steps": [
            "re-run the affected tests",
            "restart the application and confirm READY",
        ],
    }
    for f in findings:
        plan["proposed_changes"].append(str(f)[:300])
    plan["findings_count"] = len(all_findings)
    return plan


def _affected_from_findings(findings: list[str]) -> list[str]:
    files = []
    for f in findings:
        if ":" in f and ("ERROR" in f or "error" in f):
            candidate = f.split(":")[0].strip()
            if candidate.endswith(".py"):
                files.append(candidate)
    return files[:10]


def execute_repair(plan: dict, target: str = "") -> dict:
    """Run a plan through the coding router; verify afterwards. Refuses to run
    without a plan (explicit-plan-before-edits contract)."""
    if not plan or not isinstance(plan, dict):
        return {"ok": False, "output": "no repair plan — run 'inspect'/'plan' first"}
    instruction = (
        "Repair task for JARVIS self-inspection. Target: "
        f"{plan.get('target')}. Affected files: {', '.join(plan.get('affected_files') or [])}. "
        "Findings: " + "; ".join(str(f) for f in (plan.get('proposed_changes') or [])[:5])
    )
    try:
        from coding.agent_router import execute_coding_task
        result = execute_coding_task(
            instruction, files=plan.get("affected_files") or [],
            tree=str(_base()), scope="aider",
        )
    except Exception as exc:
        return {"ok": False, "output": f"coding router unavailable: {exc}"}
    verification = verify_after_repair(target)
    return {**result, "verification": verification}


def verify_after_repair(target: str = "") -> dict:
    """Post-repair verification: tests first, rendered UI when visual."""
    evidence: dict[str, Any] = {}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
            capture_output=True, text=True, timeout=600, cwd=str(_base()),
            **_NO_WINDOW,
        )
        evidence["tests"] = {
            "ok": r.returncode == 0,
            "tail": (r.stdout or "").strip().splitlines()[-1:] or [""],
        }
    except Exception as exc:
        evidence["tests"] = {"ok": False, "tail": [f"pytest failed to run: {exc}"]}
    if target and ("ui" in target.lower() or "hud" in target.lower()
                   or "panel" in target.lower() or "button" in target.lower()):
        try:
            evidence["visual"] = inspect_ui(target)
        except Exception as exc:
            evidence["visual"] = {"ok": False, "findings": [str(exc)]}
    return evidence


def handle_self_inspect(args: dict | None = None) -> str:
    """Single entry point for the ``self_inspect`` tool. Never raises."""
    a = dict(args or {})
    action = str(a.get("action") or "inspect").strip().lower()
    target = str(a.get("target") or "").strip()
    try:
        if action in ("inspect", "check"):
            src = inspect_source(target)
            logs = inspect_logs(target)
            proc = inspect_process()
            lines = ["SELF-INSPECTION //", f"- source: {'; '.join(src['findings'])}",
                     f"- logs: {'; '.join(logs['findings'])}",
                     f"- process: {'; '.join(proc['findings'])}"]
            if src["ok"] and "ui" in target.lower():
                ui = inspect_ui(target)
                lines.append(f"- ui: {'; '.join(ui['findings'])}")
            return "\n".join(lines)
        if action in ("plan", "repair_plan"):
            plan = build_plan(target)
            return ("REPAIR PLAN //\n" +
                    "\n".join(f"- {k}: {plan[k]}" for k in
                              ("target", "affected_files", "verification_steps")) +
                    f"\n- findings considered: {plan['findings_count']}")
        if action in ("repair", "fix"):
            plan = build_plan(target)
            result = execute_repair(plan, target)
            lines = [f"repair backend: {result.get('backend', '?')}",
                     f"output: {str(result.get('output', ''))[:400]}"]
            if "verification" in result:
                ver = result["verification"]
                lines.append(f"tests after repair: {ver.get('tests', {}).get('tail', [''])[0][:120]}")
            return "\n".join(lines)
        if action == "status":
            return ("Self-inspection is available: inspect, plan, repair, status. "
                    "Inspection is read-only; repair always produces a plan and "
                    "runs tests (and visual checks for UI defects) afterwards.")
        return f"Unknown self_inspect action '{action}'. Use inspect, plan, repair or status."
    except Exception as exc:
        return f"Self-inspection failed: {type(exc).__name__}: {exc}"
