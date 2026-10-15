"""Configuration guard: backup → validate → repair → verify → rollback.

Settings used to be written with no validation, no backup and no way back:
a bad value (a slider bug, a hand-edited file, a half-written save) stayed bad
until the user noticed. This module gives the configuration the same discipline
the master spec asks for (§39, §46, §50, §60):

* ``backup`` — timestamped copies in config/backups/, pruned to the newest 20.
* ``validate`` — structural problems only, in plain sentences.
* ``repair`` — fixes the safe ones (clamp ranges, flag contradictions) and says
  what it changed and what it refused to touch.
* ``diff`` — ADDED / REMOVED / MODIFIED between two configs ("what changed?").
* ``restore`` — roll a config back, keeping the replaced one as a backup.
* ``guard_write`` — the write path used by Settings: back up, write atomically,
  re-read and verify, and roll back automatically if the file comes back bad.

Everything is guarded; a failure here must never block a save the user made.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
BACKUP_DIR = BASE_DIR / "config" / "backups"
MAX_BACKUPS = 20

_lock = threading.RLock()

# Range repairs mirror core.diagnostics._RANGES; kept local so this module has
# no import cost of its own.
_RANGES = {
    "ui_opacity": (0, 100), "reactor_opacity": (0, 100),
    "reactor_stroke_opacity": (0, 100), "compact_size": (80, 900),
    "chat_font_scale": (50, 400), "world_monitor_refresh": (5, 3600),
    "camera_index": (0, 32),
}
_FLOAT_RANGES = {
    "equalizer_sensitivity": (0.0, 5.0), "reactor_animation_speed": (0.1, 5.0),
    "voice_volume": (0.0, 3.0), "voice_speed": (0.25, 4.0),
    "voice_pitch": (-24.0, 24.0),
}

# Keys that must never be written to a backup or a log in clear text.
SECRET_KEYS = ("gemini_api_key", "openai_api_key", "anthropic_api_key",
               "groq_api_key", "custom_ai_api_key", "elevenlabs_api_key",
               "fish_audio_api_key", "api_key", "token", "password")


def read(path: Path | str = CONFIG_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def masked(data: dict) -> dict:
    """A copy safe to log or show: secrets replaced by presence flags."""
    out = {}
    for k, v in (data or {}).items():
        if any(s in str(k).lower() for s in SECRET_KEYS):
            out[k] = "***set***" if v else ""
        elif isinstance(v, dict):
            out[k] = masked(v)
        else:
            out[k] = v
    return out


# ── backups ───────────────────────────────────────────────────────────────────

def backup(tag: str = "manual", data: dict | None = None) -> str:
    """Snapshot the configuration. Returns the backup path ("" on failure).

    Backups contain the API keys (they are a config restore point, not a log),
    live only inside the project, and the folder keeps the newest MAX_BACKUPS.
    """
    with _lock:
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            payload = data if isinstance(data, dict) else read()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe_tag = "".join(ch for ch in str(tag) if ch.isalnum() or ch in "-_")[:24] or "manual"
            path = BACKUP_DIR / f"api_keys_{stamp}_{safe_tag}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=4), encoding="utf-8")
            tmp.replace(path)
            prune()
            return str(path)
        except Exception:
            return ""


def list_backups() -> list[dict]:
    """Newest first, with size and timestamp."""
    out: list[dict] = []
    try:
        for p in sorted(BACKUP_DIR.glob("api_keys_*.json"), reverse=True):
            try:
                st = p.stat()
            except Exception:
                continue
            out.append({"path": str(p), "name": p.name, "size": st.st_size,
                        "mtime": st.st_mtime})
    except Exception:
        pass
    return out


def prune(keep: int = MAX_BACKUPS) -> int:
    """Keep the newest `keep` backups. Returns how many were removed."""
    removed = 0
    try:
        files = sorted(BACKUP_DIR.glob("api_keys_*.json"), reverse=True)
        for p in files[max(1, int(keep)):]:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
    except Exception:
        pass
    return removed


def last_good() -> dict | None:
    """The most recent backup that parses and passes validation."""
    for entry in list_backups():
        data = read(entry["path"])
        if data and not validate(data):
            return {"path": entry["path"], "data": data}
    for entry in list_backups():
        data = read(entry["path"])
        if data:
            return {"path": entry["path"], "data": data}
    return None


def restore(path: str | Path | None = None) -> str:
    """Restore a backup over the live config (the replaced file is backed up)."""
    with _lock:
        if path is None:
            good = last_good()
            if not good:
                return "No backup is available to restore."
            path = good["path"]
        data = read(path)
        if not data:
            return f"That backup could not be read: {path}"
        backup("pre-restore")
        old = read()
        ok, msg = guard_write(data, tag="restore")
        if not ok:
            return f"Restore failed and was rolled back: {msg}"
        changed = diff(old, data)
        return f"Restored {Path(path).name}.\n{describe_diff(changed)}"


# ── validation ────────────────────────────────────────────────────────────────

def validate(data: dict) -> list[str]:
    """Structural problems in a config dict, in plain sentences (spec §39)."""
    try:
        from observability.diagnostics import validate_settings
        return validate_settings(data)
    except Exception:
        problems: list[str] = []
        if not isinstance(data, dict):
            return ["configuration is not an object"]
        for key, (lo, hi) in {**_RANGES, **_FLOAT_RANGES}.items():
            if data.get(key) is None:
                continue
            try:
                val = float(data[key])
            except Exception:
                problems.append(f"{key} is not a number ({data[key]!r})")
                continue
            if not (lo <= val <= hi):
                problems.append(f"{key} is {data[key]} (allowed {lo}–{hi})")
        return problems


def repair(data: dict | None = None, apply: bool = True) -> dict:
    """Fix only the safe problems; report everything (spec §46 output shape).

    Safe = clamping a number back into range and recording a contradiction.
    Unsafe = guessing intent. Anything needing a key, a model choice or a
    password is reported and left alone.
    """
    with _lock:
        cfg = dict(data if isinstance(data, dict) else read())
        before = json.loads(json.dumps(cfg))
        fixed: list[str] = []
        remaining: list[str] = []
        refused: list[str] = []

        for key, (lo, hi) in _RANGES.items():
            if cfg.get(key) is None:
                continue
            try:
                val = int(float(cfg[key]))
            except Exception:
                remaining.append(f"{key} is not a number ({cfg[key]!r})")
                continue
            if val < lo or val > hi:
                cfg[key] = max(lo, min(hi, val))
                fixed.append(f"{key}: {before.get(key)} → {cfg[key]}")
        for key, (lo, hi) in _FLOAT_RANGES.items():
            if cfg.get(key) is None:
                continue
            try:
                val = float(cfg[key])
            except Exception:
                remaining.append(f"{key} is not a number ({cfg[key]!r})")
                continue
            if val < lo or val > hi:
                cfg[key] = max(lo, min(hi, val))
                fixed.append(f"{key}: {before.get(key)} → {cfg[key]}")

        feats = cfg.get("features")
        if feats is not None and not isinstance(feats, dict):
            cfg["features"] = {}
            fixed.append("features: replaced a non-object value with an empty block")
            feats = {}
        feats = feats or {}

        # model_roles: values must be strings; unknown roles and empty strings
        # are dropped so a typo cannot shadow a real model permanently.
        roles = cfg.get("model_roles")
        if roles is not None and not isinstance(roles, dict):
            cfg["model_roles"] = {}
            fixed.append("model_roles: replaced a non-object value with defaults")
        else:
            from app import model_router as _mr
            clean_roles: dict[str, str] = {}
            for rname, rval in (roles or {}).items():
                rkey = rname.strip().lower() if isinstance(rname, str) else ""
                if rkey not in _mr.ROLES:
                    fixed.append(f"model_roles.{rname}: dropped (not a known role)")
                    continue
                if not isinstance(rval, str) or not rval.strip():
                    fixed.append(f"model_roles.{rkey}: dropped (value is empty or not text)")
                    continue
                clean_roles[rkey] = rval.strip()
            if isinstance(roles, dict) and clean_roles != roles:
                fixed.append("model_roles: normalised (trimmed, invalid entries removed)")
            cfg["model_roles"] = clean_roles

        # Contradictions: the more decisive switch wins, and it is stated.
        if feats.get("webview_music") and not feats.get("webview_engine", True):
            feats["webview_music"] = False
            fixed.append("webview_music: off (it needs the WebView engine, which is off)")
        if feats.get("barge_in") and not feats.get("voice_interruption", True):
            feats["barge_in"] = False
            fixed.append("barge_in: off (voice interruption is off, so it could never fire)")
        if feats.get("push_to_talk") and feats.get("hands_free", True):
            feats["hands_free"] = False
            fixed.append("hands_free: off (push-to-talk is on; they cannot both be active)")
        cfg["features"] = feats

        if not isinstance(cfg.get("wake_words"), list) and "wake_words" in cfg:
            raw = cfg.get("wake_words")
            cfg["wake_words"] = [w for w in str(raw).split(",") if w.strip()]
            fixed.append("wake_words: converted text into a list")
        try:
            name = str(cfg.get("assistant_name") or "").strip()
            if not name:
                refused.append("assistant_name is empty (what should JARVIS be called?)")
        except Exception:
            refused.append("assistant_name is not valid text")

        if apply and cfg != before:
            backup("pre-repair")
            ok, msg = guard_write(cfg, tag="repair")
            if not ok:
                return {"checked": True, "fixed": [], "remaining": remaining + [msg],
                        "refused": refused, "rolled_back": True,
                        "health": "backup restore required"}

        problems = validate(cfg)
        remaining = sorted(set(remaining + problems))
        return {
            "checked": True,
            "fixed": fixed,
            "remaining": remaining,
            "refused": refused,
            "rolled_back": False,
            "health": "OK" if not remaining else "ATTENTION",
        }


def report_repair(result: dict | None = None) -> str:
    """The §46 answer format, as text JARVIS can read out."""
    res = result or repair(apply=False)
    lines = ["SETTINGS CHECK",
             f"Settings checked: {'yes' if res.get('checked') else 'no'}"]
    fixed = res.get("fixed") or []
    remaining = res.get("remaining") or []
    refused = res.get("refused") or []
    lines.append(f"Problems found: {len(fixed) + len(remaining) + len(refused)}")
    lines.append("Problems fixed: " + (", ".join(fixed) if fixed else "none"))
    lines.append("Problems remaining: " + ("; ".join(remaining) if remaining else "none"))
    if refused:
        lines.append("Needs your decision: " + "; ".join(refused))
    lines.append(f"Rollback events: {'yes' if res.get('rolled_back') else 'none'}")
    lines.append(f"Final health state: {res.get('health', 'unknown')}")
    return "\n".join(lines)


# ── diff ("what changed?") ────────────────────────────────────────────────────

def diff(old: dict, new: dict, prefix: str = "") -> dict:
    """ADDED / REMOVED / MODIFIED between two configs, one level deep (spec §50)."""
    out = {"added": [], "removed": [], "modified": []}
    old = old or {}
    new = new or {}
    for key in sorted(set(old) | set(new)):
        if key not in old:
            out["added"].append(f"{prefix}{key} = {masked({key: new[key]}).get(key)}")
        elif key not in new:
            out["removed"].append(f"{prefix}{key} (was {masked({key: old[key]}).get(key)})")
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            if key == "features":
                sub = diff(old[key], new[key], prefix="features.")
                for k in out:
                    out[k].extend(sub[k])
            elif old[key] != new[key]:
                out["modified"].append(f"{prefix}{key}: {old[key]} → {new[key]}")
        elif old[key] != new[key]:
            show_old = masked({key: old[key]})[key]
            show_new = masked({key: new[key]})[key]
            out["modified"].append(f"{prefix}{key}: {show_old} → {show_new}")
    return out


def describe_diff(changes: dict) -> str:
    added, removed, modified = (changes.get("added") or [],
                               changes.get("removed") or [],
                               changes.get("modified") or [])
    if not (added or removed or modified):
        return "Nothing changed."
    lines = ["WHAT CHANGED"]
    if added:
        lines.append("ADDED: " + "; ".join(added[:12]))
    if removed:
        lines.append("REMOVED: " + "; ".join(removed[:12]))
    if modified:
        lines.append("MODIFIED: " + "; ".join(modified[:12]))
    lines.append(f"POTENTIALLY AFFECTED: {_affected(added + modified + removed)}")
    return "\n".join(lines)


_AFFECTS = {
    "voice": "voice", "tts": "voice", "sapi": "voice", "onnx": "voice",
    "elevenlabs": "voice", "fish": "voice",
    "ai": "ai", "model": "ai", "provider": "ai", "openai": "ai",
    "anthropic": "ai", "groq": "ai", "gemini": "ai",
    "camera": "camera", "vision": "vision",
    "web": "browser", "webview": "browser",
    "memory": "memory", "brain": "memory",
    "reactor": "interface", "ui_": "interface", "chat": "interface",
    "font": "interface", "color": "interface", "compact": "interface",
    "world_monitor": "world monitor", "wake": "wake word",
    "mic": "microphone", "audio": "audio", "tts_engine": "voice",
    "terminal": "terminal", "file_control": "file control",
}


def _affected(items) -> str:
    seen: list[str] = []
    for item in items:
        low = str(item).lower()
        for needle, label in _AFFECTS.items():
            if needle in low and label not in seen:
                seen.append(label)
    return ", ".join(seen) if seen else "nothing obvious"


# ── the guarded write path ────────────────────────────────────────────────────

def guard_write(data: dict, tag: str = "save", path: Path | str = CONFIG_PATH) -> tuple[bool, str]:
    """Write `data` to the config atomically, verify it, roll back if it fails.

    Returns (ok, message). On failure the previous file is restored and the
    caller still learns what happened — a save must never leave a config that
    cannot be parsed.
    """
    p = Path(path)
    with _lock:
        previous = read(p)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:
            return False, f"could not write configuration: {exc}"
        verify = read(p)
        if verify != data:
            try:
                if previous:
                    p.write_text(json.dumps(previous, indent=4), encoding="utf-8")
            except Exception:
                pass
            return False, "the saved file did not read back identically — restored the previous configuration"
        problems = validate(verify)
        if problems:
            # Written, parsable, but wrong: keep it (the user's intent is
            # preserved) and tell them exactly what is off.
            return True, "saved with warnings: " + "; ".join(problems[:4])
        return True, "saved and verified"


def save_with_history(data: dict, tag: str = "save",
                      path: Path | str = CONFIG_PATH) -> tuple[bool, str, str]:
    """Backup → write → verify. Returns (ok, message, backup_path).

    `path` is the file to write. The Settings window can be pointed at a
    temporary configuration (a fresh install, a test, a second profile), and a
    guarded save must write *that* file — not the default one — or the guard
    would quietly redirect the save away from where the window was opened.
    """
    p = Path(path)
    # The snapshot is of the file being written, and only when there is one: a
    # brand-new configuration has nothing to back up (and the history folder is
    # reserved for the real install's own config).
    bak = backup(tag, read(p)) if p.exists() else ""
    ok, msg = guard_write(data, tag=tag, path=p)
    return ok, msg, bak


__all__ = [
    "CONFIG_PATH", "BACKUP_DIR", "read", "masked", "backup", "list_backups",
    "prune", "last_good", "restore", "validate", "repair", "report_repair",
    "diff", "describe_diff", "guard_write", "save_with_history",
]
