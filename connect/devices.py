"""Paired devices and the short code that pairs them.

Two secrets, doing two different jobs:

* The **pairing secret** is the long-lived setting (Settings ▸ API) that proves
  the person holding the phone is the person sitting at the machine. From it a
  short **pairing code** is derived for a five-minute window, so the user reads
  six digits off the HUD and types them once — nothing long-lived is ever typed
  on a phone keyboard.
* Each paired device then gets its own **token**, stored here, so a device can be
  revoked on its own without changing anything for the others.

The registry lives in ``data/connect/devices.json`` and holds only what is needed
to recognise a device: a name, a platform, when it paired, when it was last seen,
and the token. No screen content, no commands, no assistant history.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

__all__ = ["add_device", "device_code", "find_by_token", "list_devices",
           "pairing_code", "pairing_secret", "registry_path", "revoke_device",
           "set_pairing_secret", "touch"]

#: How long a pairing code stays valid. Long enough to walk to the phone,
#: short enough that a code seen over a shoulder is worthless later.
CODE_WINDOW_S = 300.0


def registry_path() -> Path:
    try:
        from config.settings import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent / "data"
    target = base / "connect"
    target.mkdir(parents=True, exist_ok=True)
    return target / "devices.json"


def _read() -> dict:
    path = registry_path()
    if not path.exists():
        return {"devices": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            return {"devices": []}
        return data
    except Exception:
        return {"devices": []}


def _write(data: dict) -> None:
    path = registry_path()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def pairing_secret() -> str:
    """The configured pairing secret, or "" when the user has not set one."""
    try:
        from memory.config_manager import load_api_keys
        return str(dict(load_api_keys() or {}).get("connect_pairing_secret")
                   or "").strip()
    except Exception:
        return ""


def set_pairing_secret(value: str) -> tuple[bool, str]:
    """Store a new pairing secret through the guarded config writer."""
    value = str(value or "").strip()
    if len(value) < 6:
        return False, "A pairing secret needs at least 6 characters."
    try:
        from core import config_guard
        from config.settings import API_KEYS_FILE
        data = dict(config_guard.read(API_KEYS_FILE) or {})
        data["connect_pairing_secret"] = value
        ok, message = config_guard.guard_write(data, tag="connect-pairing")
        if not ok:
            raise RuntimeError(message)
        return True, "Pairing secret saved."
    except Exception as exc:
        return False, f"The pairing secret could not be saved: {exc}"


def pairing_code(secret: str = "", window: float | None = None) -> str:
    """A six-digit code for the current five-minute window.

    Derived from the secret (HMAC-SHA256) rather than stored, so there is no
    list of live codes anywhere and an old code simply stops matching.
    """
    secret = secret or pairing_secret()
    if not secret:
        return ""
    slot = int((time.time() if window is None else window) // CODE_WINDOW_S)
    digest = hmac.new(secret.encode("utf-8"), f"jarvis-connect-{slot}".encode(),
                      hashlib.sha256).digest()
    number = int.from_bytes(digest[:4], "big") % 1_000_000
    return f"{number:06d}"


def device_code(name: str, platform: str) -> str:
    """The per-device code shown next to a newly paired device."""
    digest = hashlib.sha256(f"{name}|{platform}".encode("utf-8")).hexdigest()
    return f"{name[:12] or 'device'}-{digest[:4]}"


def _code_matches(code: str, secret: str = "") -> bool:
    """Accept the current window's code, or the previous one (clock slop)."""
    code = str(code or "").strip()
    if not code:
        return False
    for offset in (0.0, -CODE_WINDOW_S):
        expected = pairing_code(secret, time.time() + offset)
        if expected and hmac.compare_digest(code, expected):
            return True
    # A long secret typed in full is also accepted: it is what a user who does
    # not want to read the HUD would do.
    typed_secret = secret or pairing_secret()
    return bool(typed_secret) and hmac.compare_digest(code, typed_secret)


def list_devices() -> list[dict]:
    """Paired devices, without their tokens."""
    out = []
    for device in _read().get("devices", []):
        if not isinstance(device, dict):
            continue
        out.append({"id": str(device.get("id") or ""),
                    "name": str(device.get("name") or "device"),
                    "platform": str(device.get("platform") or "unknown"),
                    "paired_at": float(device.get("paired_at") or 0),
                    "last_seen": float(device.get("last_seen") or 0)})
    return out


def add_device(name: str, platform: str = "unknown",
               pairing_code_value: str = "") -> tuple[dict | None, str]:
    """Pair one device. Returns ``(device, token)`` or ``(None, reason)``."""
    secret = pairing_secret()
    if not secret:
        return None, ("No pairing secret is set, so pairing is refused. Set one on "
                      "the Settings ▸ API tab (or ask me to generate one).")
    if not _code_matches(pairing_code_value, secret):
        return None, "That pairing code is not valid (or has expired)."
    name = str(name or "").strip()[:40] or "device"
    token = secrets.token_urlsafe(32)
    record = {
        "id": device_code(name, platform),
        "name": name,
        "platform": str(platform or "unknown")[:40],
        "token": token,
        "paired_at": time.time(),
        "last_seen": time.time(),
        "address": "",
    }
    data = _read()
    devices = [d for d in data.get("devices", [])
               if isinstance(d, dict) and d.get("id") != record["id"]]
    devices.append(record)
    data["devices"] = devices
    _write(data)
    return {"id": record["id"], "name": record["name"],
            "platform": record["platform"]}, token


def find_by_token(token: str) -> dict | None:
    """The device owning a token, or None. Comparison is constant time."""
    token = str(token or "").strip()
    if not token:
        return None
    for device in _read().get("devices", []):
        if not isinstance(device, dict):
            continue
        stored = str(device.get("token") or "")
        if stored and hmac.compare_digest(stored, token):
            return device
    return None


def touch(device_id: str) -> None:
    """Record that a device just spoke to the gateway."""
    data = _read()
    changed = False
    for device in data.get("devices", []):
        if isinstance(device, dict) and device.get("id") == device_id:
            device["last_seen"] = time.time()
            changed = True
    if changed:
        _write(data)


def revoke_device(reference: str) -> tuple[bool, str]:
    """Revoke one device by its id, name or token."""
    reference = str(reference or "").strip()
    if not reference:
        return False, "Which device should I revoke?"
    data = _read()
    kept, removed = [], ""
    for device in data.get("devices", []):
        if not isinstance(device, dict):
            continue
        if (reference in (device.get("id"), device.get("name"))
                or hmac.compare_digest(str(device.get("token") or ""), reference)):
            removed = str(device.get("name") or device.get("id"))
            continue
        kept.append(device)
    if not removed:
        return False, f"No paired device matches '{reference}'."
    data["devices"] = kept
    _write(data)
    return True, f"Revoked {removed}. Its token no longer works."
