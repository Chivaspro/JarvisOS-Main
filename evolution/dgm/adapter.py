"""The DGM adapter — one interface for improving a capability that exists.

Nothing outside ``evolution.manager`` imports this module. The flow is the Darwin
Gödel Machine loop reduced to what genuinely fits J.A.R.V.I.S., with every step
built on rails that already exist in this project:

    ANALYZE   read the incumbent and run its own tests     (evolution.crucible)
    EVOLVE    ask the model for ONE candidate improvement  (evolution.forge._ask_model)
    TEST      verify the candidate in the Crucible         (evolution.crucible)
    COMPARE   keep it only if it genuinely beats the incumbent
    KEEP      archive the incumbent, then write the candidate  (evolution.dgm.archive)
    REJECT    leave the incumbent byte-for-byte as it was
    ROLLBACK  restore an archived version on request       (this module)

What it may change is decided by ``evolution.dgm.policy``, never by the model:
only ``plugins/**`` (forged skills) and ``evolution/adapters/**`` are writable,
and the Gemini configuration, API keys, the security layer, startup code, core
system files and the Crucible itself are refused with a named reason.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from evolution.dgm import archive, policy

__all__ = ["evaluate", "improve_skill", "rollback", "status", "versions"]

#: When both versions pass every test, a candidate may not be slower than the
#: incumbent by more than this factor — a rewrite that only adds latency is not
#: an improvement, whatever it does to the code's shape.
MAX_SLOWDOWN = 1.25


def _emit(progress, phase: str, detail: str = "") -> None:
    """Report one real phase to the caller. Never fatal, never fabricated."""
    if progress is None:
        return
    try:
        progress(str(phase), str(detail))
    except Exception:
        pass


def _plugins_dir() -> Path:
    from evolution import vault
    return vault.plugins_dir()


def _safe_name(name: object) -> str:
    from evolution import vault
    return vault.sanitize_name(str(name or ""))


def read_incumbent(name: str) -> tuple[str, dict]:
    """``(source, metadata)`` for one forged skill; ``('', {})`` when unreadable.

    The policy check runs first: a target outside the writable areas is treated
    exactly like a missing one, so a mistake upstream can never turn into a
    protected file being opened for editing.
    """
    clean = _safe_name(name)
    if not clean:
        return "", {}
    if policy.refusal(f"plugins/{clean}/plugin.py"):
        return "", {}
    folder = _plugins_dir() / clean
    try:
        source = (folder / "plugin.py").read_text(encoding="utf-8")
    except Exception:
        return "", {}
    meta: dict = {}
    try:
        loaded = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        meta = {}
    return source, meta


def evaluate(source: str, test_cases: list[dict] | None = None) -> dict:
    """Verify *source* in the Crucible and score it. Never raises.

    The score is how many of the capability's own tests pass, plus how long the
    sandbox took. Both come from the same gate every forged skill already passes,
    so the comparison is made on evidence rather than on the diff's appearance.
    """
    from evolution import crucible
    cases = list(test_cases or [{"input": {}}])
    ok, reason = crucible.validate_ast(source)
    if not ok:
        return {"passed": 0, "total": len(cases), "verified": False,
                "detail": reason, "elapsed_s": 0.0}
    passed, detail, telemetry = crucible.run_sandbox_test(source, cases)
    results = list((telemetry or {}).get("results") or [])
    total = len(results) or len(cases)
    good = sum(1 for r in results if r.get("success")) if results else int(bool(passed))
    return {"passed": good, "total": total, "verified": bool(passed),
            "detail": detail, "elapsed_s": float((telemetry or {}).get("elapsed_s") or 0.0)}


def _is_better(candidate: dict, incumbent: dict) -> tuple[bool, str]:
    """Does the candidate genuinely beat the incumbent — and why, either way.

    Strictly evidence-based: more tests passing wins; the same tests passing is
    only a win when it is also meaningfully faster; anything else is rejected, so
    an unproven rewrite can never replace a version that works.
    """
    if not candidate.get("verified"):
        return False, (f"the candidate failed verification "
                       f"({candidate.get('detail') or 'no reason given'})")
    c_pass = int(candidate.get("passed") or 0)
    i_pass = int(incumbent.get("passed") or 0)
    c_total = int(candidate.get("total") or 0)
    i_total = int(incumbent.get("total") or 0)
    if c_pass > i_pass:
        return True, (f"it passes {c_pass}/{c_total} tests where the current "
                      f"version passes {i_pass}/{i_total}")
    if c_pass < i_pass:
        return False, (f"it passes fewer tests ({c_pass}/{c_total} vs "
                       f"{i_pass}/{i_total})")
    c_s = float(candidate.get("elapsed_s") or 0.0)
    i_s = float(incumbent.get("elapsed_s") or 0.0)
    if i_s > 0 and c_s > i_s * MAX_SLOWDOWN:
        return False, (f"it passes the same tests but is {c_s / i_s:.2f}x slower "
                       f"({c_s:g}s vs {i_s:g}s)")
    if i_s > 0 and c_s < i_s * 0.9:
        return True, f"it passes the same tests and is faster ({c_s:g}s vs {i_s:g}s)"
    return False, "it is not measurably better than the current version"


_CANDIDATE_SYSTEM = '''You are the J.A.R.V.I.S. capability-improvement engine.

You are given ONE existing, working Python plugin together with the real result of
running that plugin's own tests. Produce ONE improved version of the same plugin.

Rules:
1. Keep the contract exactly:
       def run(parameters: dict, player=None, session_memory=None) -> str
   It returns a SHORT spoken sentence. It must never raise, and it must work when
   called with `{}`.
2. Improve only what the evidence shows is weak: a failing test, a crash, a
   missing default, a slow path, or a clearer result sentence. Do not rename the
   module, do not change its purpose, do not add capabilities nobody asked for.
3. No PLUGIN or FEATURE_METADATA block. No destructive operating-system calls.
4. Return ONLY this JSON object, no markdown fences:
{"code": "<full improved python source>", "reason": "<one sentence: what you changed and why>"}
'''


def _ask_candidate(name: str, source: str, meta: dict,
                   baseline: dict) -> tuple[str, str, str]:
    """``(candidate_source, reason, error)`` — the EVOLVE step."""
    from evolution import forge
    prompt = (
        f"Capability name: {name}\n"
        f"Purpose: {str(meta.get('description') or meta.get('goal') or '')[:600]}\n"
        f"Its own tests: {int(baseline.get('passed') or 0)}/"
        f"{int(baseline.get('total') or 0)} passed in "
        f"{float(baseline.get('elapsed_s') or 0.0):g}s.\n"
        f"Verification verdict: {baseline.get('detail') or 'no verdict'}\n\n"
        f"Current source:\n{source}\n\nReturn the improved source."
    )
    try:
        raw, _provider = forge._ask_model(prompt, _CANDIDATE_SYSTEM, None)
    except Exception as exc:
        return "", "", f"{type(exc).__name__}: {exc}"
    data = forge.parse_json_response(raw) or {}
    code = str(data.get("code") or "").strip()
    if not code:
        return "", "", "the model returned no code"
    return code, str(data.get("reason") or "").strip(), ""


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary sibling, then replace — never a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _bump(version: str) -> str:
    """``1.0.0`` → ``1.0.1``. Anything unparseable restarts at ``1.0.1``."""
    parts = str(version or "").split(".")
    try:
        if len(parts) < 3:
            raise ValueError(version)
        major, minor, patch = (int(p) for p in parts[:3])
    except Exception:
        return "1.0.1"
    return f"{major}.{minor}.{patch + 1}"


def _write_plugin(name: str, source: str, meta: dict, metrics: dict,
                  why: str) -> bool:
    """Write one improved capability, with its metadata. False on any failure.

    Both files are written through a temporary sibling, so the skill on disk is
    always a complete, parseable version — never a half-written one.
    """
    if policy.refusal(f"plugins/{name}/plugin.py"):
        return False
    folder = _plugins_dir() / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _atomic_write(folder / "plugin.py", source)
        record = dict(meta)
        record["version"] = _bump(str(meta.get("version") or "1.0.0"))
        history = [h for h in list(record.get("dgm_history") or []) if isinstance(h, dict)]
        history.append({"at": time.time(), "reason": str(why or ""),
                        "passed": metrics.get("passed"), "total": metrics.get("total"),
                        "elapsed_s": metrics.get("elapsed_s")})
        record["dgm_history"] = history[-20:]
        _atomic_write(folder / "plugin.json", json.dumps(record, indent=2))
        return True
    except Exception:
        return False


def improve_skill(name: str, *, player=None, progress=None) -> str:
    """Improve one existing forged skill, keeping the old version recoverable."""
    clean = _safe_name(name)
    if not clean:
        return f"'{name}' is not a capability I can work on, sir."
    source, meta = read_incumbent(clean)
    if not source:
        return (f"I have no editable capability called '{clean}', sir. Only skills "
                f"I forged myself can be improved — my core, my configuration and "
                f"my safety layer are protected.")
    cases = [c for c in (meta.get("test_cases") or []) if isinstance(c, dict)]
    cases = cases or [{"input": {}}]

    _emit(progress, "ANALYZE", f"running '{clean}' against its own {len(cases)} test(s)")
    baseline = evaluate(source, cases)                                     # ANALYZE
    _emit(progress, "EVOLVE", f"asking the model to improve '{clean}'")
    candidate, reason, error = _ask_candidate(clean, source, meta, baseline)  # EVOLVE
    if not candidate:
        return f"I could not draft an improvement for '{clean}', sir — {error}."
    if policy.refusal(f"plugins/{clean}/plugin.py"):
        return f"I am not permitted to modify '{clean}', sir."
    _emit(progress, "TEST", "verifying the candidate in the Crucible")
    metrics = evaluate(candidate, cases)                                   # TEST
    better, why = _is_better(metrics, baseline)                            # COMPARE

    if not better:                                                         # REJECT
        _emit(progress, "REJECT", why)
        return (f"I drafted an improvement for '{clean}' and rejected it, sir — "
                f"{why}. The version that works is untouched.")
    archived = archive.save(clean, source, baseline,
                            reason="incumbent kept before a DGM improvement")
    if not _write_plugin(clean, candidate, meta, metrics, reason or why):   # KEEP
        return (f"The improvement for '{clean}' verified, sir, but I could not "
                f"write it — the working version is untouched.")
    _emit(progress, "KEEP", why)                                           # KEEP
    keep_note = f" Archived: {archived}." if archived else ""
    spoken_reason = reason or why
    return (f"I improved '{clean}', sir — {spoken_reason}. The previous version is "
            f"archived and I can roll it back on request.{keep_note}")


def rollback(name: str) -> str:
    """Restore the newest archived version of one capability."""
    clean = _safe_name(name)
    if not clean:
        return f"'{name}' is not a capability I can roll back, sir."
    newest = archive.latest(clean)
    if newest is None:
        return f"I have no archived version of '{clean}' to restore, sir."
    source, metrics = newest
    _current, meta = read_incumbent(clean)
    if not _write_plugin(clean, source, meta, metrics, "rollback"):
        return f"I could not restore '{clean}', sir — nothing was changed."
    return f"Restored the archived version of '{clean}', sir."


def versions(name: str) -> str:
    """The archived versions of one capability, newest first."""
    rows = archive.versions(_safe_name(name))
    if not rows:
        return f"I have no archived versions of '{name}', sir."
    return "\n".join(
        f"- {r.get('stamp')} · passed "
        f"{int((r.get('metrics') or {}).get('passed') or 0)}/"
        f"{int((r.get('metrics') or {}).get('total') or 0)} · {r.get('reason') or ''}".rstrip()
        for r in rows[:20])


def status() -> str:
    """One sentence about the improvement layer (read by ``manager.status``)."""
    count = 0
    try:
        base = archive.root()
        count = sum(1 for _ in base.glob("*/*.json")) if base.is_dir() else 0
    except Exception:
        count = 0
    return (f"Improvement archive: {count} archived version(s) — a candidate is kept "
            f"only when it measurably beats the incumbent. Writable areas: "
            f"{', '.join(policy.ALLOWED_PREFIXES)}.")
