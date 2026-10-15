"""Boot sentry: if the last patch is what killed the launch, undo it.

Called once, before any heavy module loads. The evidence is a real crash log
written *after* the patch was applied — ``core/stability.py`` keeps
``data/logs/jarvis_crash.log``, so a modification time later than the patch's
timestamp means the process died with that patch in place. In that case the
backup is restored immediately, before the UI, the plugin scan or the voice
stack get a chance to fail on the same broken line again.

A rollback is only attempted once per patch (the history row records it), so a
patch that was rolled back and re-applied is not rolled back again on the next
start.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from recovery import patcher

__all__ = ["check_and_recover", "crash_log_path"]

#: How long after a patch a crash still counts as "caused by that patch".
_CRASH_WINDOW_S = 15 * 60.0


def crash_log_path() -> Path:
    try:
        from config.settings import LOGS_DIR
        return Path(LOGS_DIR) / "jarvis_crash.log"
    except Exception:
        return Path(__file__).resolve().parent.parent / "data" / "logs" / "jarvis_crash.log"


def _last_crash_time() -> float:
    try:
        return float(crash_log_path().stat().st_mtime)
    except Exception:
        return 0.0


def check_and_recover() -> dict[str, Any]:
    """Restore the previous source if the last patch is implicated in a crash."""
    row = patcher.last_applied_with_backup()
    if not row:
        return {"recovered": False, "reason": "no applied patch with a backup"}

    patched_at = float(row.get("timestamp") or 0.0)
    crashed_at = _last_crash_time()
    if crashed_at < patched_at or (crashed_at - patched_at) > _CRASH_WINDOW_S:
        return {"recovered": False, "reason": "no crash after the last patch"}

    target = Path(str(row.get("file") or ""))
    saved = Path(str(row.get("backup") or ""))
    if not target.exists() or not saved.exists():
        return {"recovered": False, "reason": "backup or target missing"}

    try:
        shutil.copy2(saved, target)
    except Exception as exc:
        return {"recovered": False, "reason": f"rollback failed: {exc}"}

    patcher.mark_recovered(str(row.get("patch_id") or ""),
                           "app crashed on the next start after this patch")
    message = (f"[BootSentry] rolled back patch {row.get('patch_id')} on "
               f"{target.name} (crash {time.strftime('%H:%M:%S', time.localtime(crashed_at))})")
    try:
        print(message)
    except Exception:
        pass
    return {"recovered": True, "patch_id": row.get("patch_id"),
            "file": str(target), "message": message}


def _self_test() -> str:
    return json.dumps({"crash_log": str(crash_log_path()),
                       "exists": crash_log_path().exists()}, indent=2)
