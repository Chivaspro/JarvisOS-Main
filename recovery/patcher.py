"""The patcher: synthesise one surgical fix, prove it compiles, install it, and
be able to undo it — always.

The order of operations is the safety property, not a detail:

1. read the file, take the ±25-line window around the failing line;
2. ask the model for an exact ``target_chunk`` -> ``replacement_chunk`` pair
   (a minimal patch with an explanation, never a rewrite);
3. stage the replacement **in memory** and AST-parse it — a broken patch never
   reaches the disk;
4. copy the original to ``data/recovery/backups/``;
5. write, then ``py_compile`` the file; any failure restores the backup
   immediately and the patch is recorded as ``rolled_back``;
6. append an immutable history row (patch id, file, line, exception, backup
   path, status) so ``rollback`` and the boot sentry can undo it by id.

Nothing else in the project writes patches, and nothing here ever raises: every
failure path returns ``{"ok": False, "message": ...}``.
"""

from __future__ import annotations

import ast
import json
import os
import py_compile
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from recovery import analyzer

__all__ = [
    "apply_patch",
    "backup",
    "history",
    "propose",
    "rollback",
]

_DIR_NAME = "recovery"
_CONTEXT_LINES = 25


def _data_dir() -> Path:
    try:
        from config.settings import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = analyzer.root() / "data"
    return base / _DIR_NAME


def _history_path() -> Path:
    return _data_dir() / "patch_history.json"


def _backups_dir() -> Path:
    return _data_dir() / "backups"


# ── history + backups ─────────────────────────────────────────────────────────

def _load_history() -> list[dict[str, Any]]:
    try:
        data = json.loads(_history_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(rows: list[dict[str, Any]]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def history(limit: int = 10) -> list[dict[str, Any]]:
    """Most recent patches, newest first."""
    return list(reversed(_load_history()))[:max(1, int(limit or 10))]


def backup(file_path: Path) -> Path | None:
    """Copy *file_path* into the backup vault; None when that is impossible."""
    try:
        target_dir = _backups_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        destination = target_dir / f"{file_path.stem}.bak_{stamp}{file_path.suffix}"
        shutil.copy2(file_path, destination)
        return destination
    except Exception:
        return None


# ── synthesis (the only model-facing part) ────────────────────────────────────

_SYSTEM = """You are an elite Python debugging engineer fixing a bug in the
J.A.R.V.I.S. desktop assistant.

Produce the SMALLEST surgical fix: a null check, a bounds check, a missing
default, a corrected attribute name, a guarded call. Never restructure the
module, never rename things, never add dependencies, never change behaviour
beyond the reported exception, and never delete functionality.

Return ONLY this JSON object:
{
  "explanation": "one sentence: what the bug was and what the fix does",
  "target_chunk": "EXACT verbatim text copied from the file context, with the original indentation and whitespace",
  "replacement_chunk": "the text that replaces it"
}

`target_chunk` must appear exactly once in the file context and must be as short
as possible while staying unique."""


def _ask(prompt: str) -> dict[str, Any]:
    from evolution import forge
    raw, _provider = forge._ask_model(prompt, _SYSTEM, None)
    return forge.parse_json_response(raw)


def propose(traceback_text: str, *, parsed: dict[str, Any] | None = None,
            context_notes: str = "") -> dict[str, Any]:
    """Synthesise (but do not apply) a patch for one traceback."""
    parsed = parsed or analyzer.parse(traceback_text)
    if not parsed.get("found"):
        return {"ok": False, "parsed": parsed,
                "message": ("I could not identify a first-party file to patch "
                            "from that traceback.")}
    # Protected files (main.py, ui*.py, ...) are synthesised like any other so
    # the repair card can show the patch preview and wait for the owner's
    # explicit approval — the policy layer (_auto_allowed) never auto-applies
    # a protected risk.

    target_path = Path(str(parsed["file"]))
    try:
        full_source = target_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "parsed": parsed,
                "message": f"I could not read {target_path.name}: {exc}"}

    source_lines = full_source.splitlines(keepends=True)
    line_num = int(parsed.get("line") or 1)
    start = max(0, line_num - _CONTEXT_LINES)
    end = min(len(source_lines), line_num + _CONTEXT_LINES)
    code_context = "".join(source_lines[start:end])

    prompt = (
        f"File: {target_path.name}\n"
        f"Failing line: {line_num}\n"
        f"Function: {parsed.get('function') or 'unknown'}\n"
        f"Exception: {parsed.get('exception_type')}: {parsed.get('exception_message')}\n"
        f"Extra context: {context_notes or 'none'}\n\n"
        f"Source window:\n{code_context}\n"
    )
    try:
        spec = _ask(prompt)
    except Exception as exc:
        return {"ok": False, "parsed": parsed,
                "message": f"Patch synthesis failed: {exc}"}

    target_chunk = str(spec.get("target_chunk") or "")
    replacement = spec.get("replacement_chunk")
    if not target_chunk or not isinstance(replacement, str):
        return {"ok": False, "parsed": parsed,
                "message": "The model did not return a usable patch."}
    if full_source.count(target_chunk) != 1:
        return {"ok": False, "parsed": parsed,
                "message": ("The proposed patch did not match the file exactly once, "
                            "so I did not apply it.")}

    staged = full_source.replace(target_chunk, replacement, 1)
    ok, reason = _validate(staged, target_path.name)
    if not ok:
        return {"ok": False, "parsed": parsed,
                "message": f"I rejected my own patch: {reason}"}

    return {
        "ok": True,
        "parsed": parsed,
        "file": str(target_path),
        "line": line_num,
        "risk": parsed.get("risk", "high"),
        "explanation": str(spec.get("explanation") or "Bug hotfix."),
        "target_chunk": target_chunk,
        "replacement_chunk": replacement,
        "staged": staged,
        "message": (f"Patch ready for {target_path.name} line {line_num}: "
                    f"{str(spec.get('explanation') or '').strip()}"),
    }


def _validate(code: str, name: str) -> tuple[bool, str]:
    try:
        ast.parse(code, filename=name)
        return True, ""
    except SyntaxError as exc:
        return False, f"syntax error on line {exc.lineno}: {exc.msg}"
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def apply_patch(proposal: dict[str, Any]) -> dict[str, Any]:
    """Install an already-synthesised proposal (see :func:`propose`)."""
    if not proposal.get("ok"):
        return proposal
    target_path = Path(str(proposal["file"]))
    staged = str(proposal.get("staged") or "")
    try:
        original = target_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "message": f"Could not re-read {target_path.name}: {exc}"}

    saved = backup(target_path)
    if saved is None:
        return {"ok": False,
                "message": (f"I could not back up {target_path.name}, so I did not "
                            f"patch it.")}

    try:
        target_path.write_text(staged, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "message": f"Writing the patch failed: {exc}"}

    try:
        py_compile.compile(str(target_path), doraise=True)
    except Exception as exc:
        try:
            target_path.write_text(original, encoding="utf-8")
        except Exception:
            pass
        _record(target_path, proposal, saved, "rolled_back",
                detail=f"post-write compilation failed: {exc}")
        return {"ok": False,
                "message": (f"The patched file did not compile, so I restored the "
                            f"backup: {exc}")}

    patch_id = _record(target_path, proposal, saved, "applied")
    return {
        "ok": True,
        "patch_id": patch_id,
        "file": str(target_path),
        "backup": str(saved),
        "explanation": proposal.get("explanation", ""),
        "message": (f"Patched {target_path.name} (line {proposal.get('line')}, "
                    f"patch {patch_id}). Backup kept."),
    }


def _record(target_path: Path, proposal: dict[str, Any], saved: Path | None,
            status: str, detail: str = "") -> str:
    patch_id = uuid.uuid4().hex[:8]
    parsed = proposal.get("parsed") or {}
    rows = _load_history()
    rows.append({
        "patch_id": patch_id,
        "timestamp": time.time(),
        "status": status,
        "detail": detail,
        "file": str(target_path),
        "line": proposal.get("line"),
        "risk": proposal.get("risk", "high"),
        "exception": (f"{parsed.get('exception_type')}: "
                      f"{parsed.get('exception_message')}").strip(": "),
        "explanation": proposal.get("explanation", ""),
        "backup": str(saved) if saved else "",
        "verified": status == "applied",
    })
    _save_history(rows)
    return patch_id


def rollback(patch_id: str = "latest") -> dict[str, Any]:
    """Restore the backup for one applied patch (or the newest one)."""
    rows = _load_history()
    chosen: dict[str, Any] | None = None
    for row in reversed(rows):
        if patch_id != "latest" and row.get("patch_id") != patch_id:
            continue
        if row.get("status") != "applied":
            continue
        chosen = row
        break
    if chosen is None:
        return {"ok": False, "message": f"No applied patch matching '{patch_id}'."}

    target = Path(str(chosen.get("file") or ""))
    saved = Path(str(chosen.get("backup") or ""))
    if not target.exists() or not saved.exists():
        return {"ok": False,
                "message": "The backup or the target file is missing, so I cannot "
                           "roll that patch back."}
    try:
        shutil.copy2(saved, target)
        py_compile.compile(str(target), doraise=True)
    except Exception as exc:
        return {"ok": False, "message": f"Rollback failed: {exc}"} 

    chosen["status"] = "rolled_back"
    chosen["rolled_back_at"] = time.time()
    _save_history(rows)
    return {
        "ok": True,
        "patch_id": chosen.get("patch_id"),
        "file": str(target),
        "message": (f"Rolled back patch {chosen.get('patch_id')} on "
                    f"{target.name}."),
    }


def last_applied_with_backup() -> dict[str, Any] | None:
    """The newest applied patch that still has its backup (for boot recovery)."""
    for row in reversed(_load_history()):
        if row.get("status") != "applied":
            continue
        if Path(str(row.get("backup") or "")).exists():
            return row
    return None


def mark_recovered(patch_id: str, reason: str) -> None:
    rows = _load_history()
    for row in rows:
        if row.get("patch_id") == patch_id:
            row["status"] = "rolled_back_on_boot"
            row["rolled_back_at"] = time.time()
            row["detail"] = reason
            break
    _save_history(rows)
