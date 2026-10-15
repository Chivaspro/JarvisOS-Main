"""JARVIS task memory — multi-step work that survives interruption.

The `tasks` table (goal, ordered steps, completed steps, errors, files,
decisions) already existed in the brain memory database but nothing kept it
current while a job was actually running: a task only became useful once
something wrote to it. This module is that writer.

It is a thin facade over the existing memory system — no second database, no
second memory format:

* ``start``   open a plan for a job with 3+ dependent steps
* ``done``    a step finished (moved to steps_done)
* ``fail``    a step failed (kept in steps_remaining, recorded in errors)
* ``block``   the user must do something first (log in, solve a CAPTCHA, choose)
* ``resume``  what is left, and what it is waiting for
* ``finish``  the whole job is verified complete
* ``abandon`` the user dropped it

``context_block`` renders the live tasks into the system instruction so a new
session (or a model that lost the thread) knows exactly where the work stands,
and ``handle_task_command`` is the single entry point used by the ``task_plan``
tool.
"""

from __future__ import annotations

import threading
import time
from typing import Iterable, Mapping, Sequence

__all__ = ["TaskTracker", "get_task_tracker", "handle_task_command"]

_FINISHED_ACTIONS = {"finish", "complete", "completed", "done_all", "close"}


def _clean_list(value) -> list[str]:
    """Coerce a model-supplied value into a list of short strings."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        # Accept "a, b, c" and newline-separated lists from the model.
        parts = [p.strip(" -•\t") for p in text.replace("\n", ",").split(",")]
        return [p for p in parts if p][:24]
    out: list[str] = []
    try:
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
    except Exception:
        return []
    return out[:24]


class TaskTracker:
    """Facade over the memory database's task records (thread-safe, silent)."""

    def __init__(self, memory=None):
        self._memory = memory
        self._lock = threading.RLock()
        self._last_tid: int | None = None

    # ── wiring ────────────────────────────────────────────────────────────────

    @property
    def memory(self):
        """BrainMemory, resolved lazily so importing this module is free."""
        if self._memory is None:
            from memory.manager import get_brain_memory

            self._memory = get_brain_memory()
        return self._memory

    @property
    def db(self):
        return self.memory.db

    # ── internal helpers ──────────────────────────────────────────────────────

    def _fetch(self, tid: int | None) -> dict | None:
        """Task by id, or the most recent active one when tid is absent."""
        try:
            if tid is not None:
                task = self.db.get_task(int(tid))
                if task:
                    return task
            active = self.db.list_tasks(status="active", limit=1)
            return active[0] if active else None
        except Exception:
            return None

    def _update(self, tid: int, **fields) -> None:
        try:
            self.db.update_task(int(tid), fields)
        except Exception:
            pass

    def _append(self, tid: int, field_name: str, value: str) -> None:
        try:
            task = self.db.get_task(int(tid)) or {}
            items = list(task.get(field_name) or [])
            if value not in items:
                items.append(value)
            self._update(tid, **{field_name: items[-30:]})
        except Exception:
            pass

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, goal: str, steps: Sequence[str] | None = None,
              project_id: str = "", files: Sequence[str] | None = None) -> str:
        """Open a plan. Returns the spoken-ready confirmation text."""
        goal = str(goal or "").strip() or "Untitled task"
        clean_steps = _clean_list(steps)
        try:
            tid = self.memory.start_task(goal, project_id=project_id,
                                         steps_remaining=clean_steps)
            with self._lock:
                self._last_tid = tid
            extra: dict = {}
            clean_files = _clean_list(files)
            if clean_files:
                extra["files"] = clean_files
            if extra:
                self._update(tid, **extra)
            if clean_steps:
                return (f"Plan opened (task #{tid}).\n"
                        f"Goal: {goal}\n"
                        + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(clean_steps))
                        + "\nWork the steps in order and call task_plan with "
                          "action='done' after each one.")
            return (f"Plan opened (task #{tid}) for: {goal}. "
                    "Record each step as you finish it.")
        except Exception as exc:
            return f"Could not open a task plan: {exc}"

    def mark_done(self, tid: int | None, step: str, detail: str = "") -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task to update."
        step = str(step or "").strip()
        if not step:
            remaining = list(task.get("steps_remaining") or [])
            step = remaining[0] if remaining else "step"
        try:
            self.memory.add_task_step_done(task["id"], step)
            with self._lock:
                self._last_tid = task["id"]
            if detail:
                self._append(task["id"], "solutions", str(detail)[:200])
            task = self._fetch(task["id"]) or task
            remaining = list(task.get("steps_remaining") or [])
            if remaining:
                return (f"Step recorded on task #{task['id']}: {step}. "
                        f"Next: {remaining[0]}")
            return (f"All recorded steps of task #{task['id']} are done "
                    f"({step} was the last). Verify the result, then call "
                    "task_plan action='finish'.")
        except Exception as exc:
            return f"Could not record the step: {exc}"

    def mark_failed(self, tid: int | None, step: str, error: str = "") -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task to update."
        step = str(step or "").strip()
        note = f"{step}: {error}".strip(": ") or "step failed"
        self._append(task["id"], "errors", note[:240])
        return (f"Recorded a failure on task #{task['id']}: {note}. "
                "Try a real alternative if one exists, otherwise tell the user "
                "plainly what failed.")

    def block(self, tid: int | None, reason: str) -> str:
        """The user has to do something the assistant must not do itself."""
        task = self._fetch(tid)
        if not task:
            return "No active task to update."
        reason = str(reason or "user input required").strip()
        self._append(task["id"], "errors", f"[blocked] waiting for the user: {reason}"[:240])
        return (f"Task #{task['id']} is paused: {reason}. "
                "Ask the user for exactly that, in one short sentence, and let "
                "them do it themselves — never ask for a password and never "
                "attempt to bypass security. Continue when they say it is done.")

    def add_note(self, tid: int | None, note: str) -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task to update."
        note = str(note or "").strip()
        if not note:
            return "Nothing to record."
        self._append(task["id"], "decisions", note[:200])
        return f"Recorded on task #{task['id']}: {note}"

    def add_files(self, tid: int | None, files: Iterable[str]) -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task to update."
        clean = _clean_list(files)
        if not clean:
            return "No files given."
        try:
            existing = list(task.get("files") or [])
            for path in clean:
                if path not in existing:
                    existing.append(path)
            self._update(task["id"], files=existing[-30:])
            return f"Linked {len(clean)} file(s) to task #{task['id']}."
        except Exception as exc:
            return f"Could not link files: {exc}"

    def resume(self, tid: int | None = None) -> str:
        task = self._fetch(tid)
        if not task:
            return "There is no unfinished task on record."
        remaining = list(task.get("steps_remaining") or [])
        done = list(task.get("steps_done") or [])
        blockers = [e for e in (task.get("errors") or []) if str(e).startswith("[blocked]")]
        lines = [f"Task #{task['id']} — {task['title']}"]
        if done:
            lines.append("Already done: " + "; ".join(done[-6:]))
        if blockers:
            lines.append("Waiting on the user: " + "; ".join(
                str(b).replace("[blocked] waiting for the user: ", "") for b in blockers[-2:]))
        if remaining:
            lines.append("Still to do: " + "; ".join(remaining))
        elif not blockers:
            lines.append("Every recorded step is done — verify, then finish it.")
        with self._lock:
            self._last_tid = task["id"]
        return "\n".join(lines)

    def finish(self, tid: int | None = None, summary: str = "") -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task to close."
        summary = str(summary or "").strip()
        try:
            self.memory.finish_task(task["id"], summary)
            if summary:
                try:
                    self.memory.record_experience(
                        f"{task['title']} — {summary}"[:300],
                        kind="workflow", outcome="success",
                        context=str(task.get("project_id") or ""),
                    )
                except Exception:
                    pass
            with self._lock:
                self._last_tid = None
            tail = " Saved as a completed workflow." if summary else ""
            return f"Task #{task['id']} closed: {task['title']}.{tail}"
        except Exception as exc:
            return f"Could not close the task: {exc}"

    def abandon(self, tid: int | None = None, reason: str = "") -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task to drop."
        try:
            self._update(task["id"], status="abandoned")
            if reason:
                self._append(task["id"], "errors", f"[abandoned] {reason}"[:200])
            with self._lock:
                self._last_tid = None
            return f"Dropped task #{task['id']}: {task['title']}."
        except Exception as exc:
            return f"Could not drop the task: {exc}"

    # ── reporting ─────────────────────────────────────────────────────────────

    def list_active(self, limit: int = 6) -> str:
        try:
            tasks = self.db.list_tasks(status="active", limit=limit)
        except Exception:
            return "Task memory is unavailable."
        if not tasks:
            return "No unfinished tasks on record."
        lines = ["Unfinished tasks:"]
        for t in tasks:
            remaining = list(t.get("steps_remaining") or [])
            nxt = remaining[0] if remaining else "all steps recorded"
            lines.append(f"- #{t['id']} {t['title']} — next: {nxt}")
        return "\n".join(lines)

    def status(self, tid: int | None = None) -> str:
        task = self._fetch(tid)
        if not task:
            return "No active task. Nothing is in flight."
        done = len(task.get("steps_done") or [])
        remaining = list(task.get("steps_remaining") or [])
        errors = list(task.get("errors") or [])
        return (f"Task #{task['id']} ({task['status']}): {task['title']}\n"
                f"steps done: {done} | remaining: {len(remaining)} | notes: {len(errors)}")

    def context_block(self, limit: int = 3) -> str:
        """Live task state for the system instruction (empty when idle)."""
        try:
            tasks = self.db.list_tasks(status="active", limit=max(1, int(limit)))
        except Exception:
            return ""
        if not tasks:
            return ""
        lines = [
            "[TASK MEMORY]",
            "Work in flight. Continue from here instead of restarting, and never "
            "merge two tasks together.",
        ]
        for t in tasks:
            lines.append(f"- Task #{t['id']}: {t['title']}")
            for s in (t.get("steps_done") or [])[-4:]:
                lines.append(f"    done: {s}")
            for s in (t.get("steps_remaining") or [])[:4]:
                lines.append(f"    remaining: {s}")
            for e in (t.get("errors") or [])[-2:]:
                lines.append(f"    note: {e}")
            for f in (t.get("files") or [])[:3]:
                lines.append(f"    file: {f}")
        lines.append("Update it with the task_plan tool as you go.")
        return "\n".join(lines)


# ── singleton + tool entry point ──────────────────────────────────────────────

_tracker: TaskTracker | None = None
_tracker_lock = threading.Lock()


def get_task_tracker() -> TaskTracker:
    """Process-wide tracker (cheap; the database handle is resolved on use)."""
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = TaskTracker()
    return _tracker


def _plan_steps(goal: str, limit: int = 6) -> list[str]:
    """Skill-derived step list for a goal ([] when the registry is unavailable).

    Deterministic and offline: "look at this, fix it and test it" becomes
    Vision → Engineering → Debugging → Verification without a model round trip,
    and a permission-gated skill never appears.
    """
    try:
        from core.brain_bridge import plan_request
        plan = plan_request(goal) or {}
        steps = [str(s) for s in (plan.get("steps") or []) if str(s).strip()]
        return steps[:max(1, int(limit))]
    except Exception:
        return []


def handle_task_command(args: Mapping[str, object] | None = None) -> str:
    """Single entry point for the ``task_plan`` tool. Never raises."""
    a = dict(args or {})
    action = str(a.get("action") or "").strip().lower()
    tid_raw = a.get("task_id", a.get("id"))
    try:
        tid = int(tid_raw) if str(tid_raw or "").strip() not in ("", "None") else None
    except Exception:
        tid = None
    step = str(a.get("step") or "").strip()
    for key in ("result", "summary", "reason", "error", "detail", "note"):
        if not step and a.get(key):
            step = str(a.get(key)).strip()
    if action in _FINISHED_ACTIONS and action != "done":
        action = "finish"
    if action == "done" and not step and not tid:
        action = "finish"   # bare "done" with nothing to record closes the plan

    tracker = get_task_tracker()
    try:
        if action in ("start", "create", "plan", "open", "begin"):
            goal = str(a.get("goal") or a.get("title") or a.get("task") or "").strip()
            steps = _clean_list(a.get("steps"))
            if not steps and goal:
                # No steps supplied: derive them from the skill registry rather
                # than storing an empty plan that leaves the work unstarted.
                steps = _plan_steps(goal)
            return tracker.start(
                goal=goal,
                steps=steps,
                project_id=str(a.get("project_id") or "").strip(),
                files=_clean_list(a.get("files")),
            )
        if action in ("done", "step_done", "complete_step", "advance"):
            return tracker.mark_done(tid, step, detail=str(a.get("detail") or ""))
        if action in ("fail", "failed", "error"):
            return tracker.mark_failed(tid, step, error=str(a.get("error") or a.get("reason") or ""))
        if action in ("block", "blocked", "need_user", "pause", "handoff"):
            return tracker.block(tid, str(a.get("reason") or a.get("detail") or step or "user input required"))
        if action in ("note", "decision", "record"):
            return tracker.add_note(tid, str(a.get("note") or a.get("detail") or step or ""))
        if action in ("file", "files", "attach"):
            return tracker.add_files(tid, a.get("files") or a.get("file") or [])
        if action in ("resume", "continue", "next"):
            return tracker.resume(tid)
        if action == "finish":
            return tracker.finish(tid, summary=str(a.get("summary") or a.get("detail") or ""))
        if action in ("abandon", "cancel", "drop", "forget"):
            return tracker.abandon(tid, reason=str(a.get("reason") or ""))
        if action in ("list", "all"):
            return tracker.list_active()
        if action in ("status", "state"):
            return tracker.status(tid)
        if not action:
            return tracker.resume(tid)
        return (f"Unknown task action '{action}'. Use start, done, fail, block, "
                "note, file, resume, finish, abandon, list or status.")
    except Exception as exc:
        return f"Task memory error: {exc}"
