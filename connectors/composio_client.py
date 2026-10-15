"""The single doorway to the Composio platform.

Everything in J.A.R.V.I.S. that touches Composio comes through :func:`client`,
which keeps four decisions in one place instead of six:

* **Where the key lives.** ``COMPOSIO_API_KEY`` in the environment wins, then
  ``composio_api_key`` in ``config/api_keys.json`` — the same guarded file the
  Settings window writes. The key is never printed, logged or returned; only a
  hash of it is kept, so saving a new one takes effect without a restart.
* **When the package is imported.** ``composio`` is optional and heavy, so it is
  imported on first use. A machine without it still starts J.A.R.V.I.S.
* **What a failure means.** No package, no key, and a client that refuses to
  build are three different sentences, because the fix differs for each.
* **Who the user is.** Composio scopes connected accounts to a ``user_id``, so
  one stable id is passed and connections belong to this installation's owner.

Building a client is local: nothing here touches the network. The first real
round trip belongs to :mod:`connectors.manager`.
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

__all__ = [
    "CONFIG_API_KEY", "CONFIG_USER_ID", "DEFAULT_USER_ID", "ENV_API_KEY",
    "PACKAGE_HINT", "api_key_present", "client", "error", "error_reason",
    "reset", "status", "user_id",
]

#: Said the same way everywhere for the missing-package case.
PACKAGE_HINT = ("The Composio connector needs the optional 'composio' package, "
                "which is not installed — run 'pip install composio' (or "
                "setup_jarvis --install-missing) and ask me again. Nothing was "
                "sent to Composio.")

ENV_API_KEY = os.getenv("ENV_API_KEY", "")
CONFIG_API_KEY = os.getenv("CONFIG_API_KEY", "")
CONFIG_USER_ID = "composio_user_id"
DEFAULT_USER_ID = "default"

# A placeholder saved as a credential is worse than none: every later error would
# blame Composio instead of the config file. A key is one unbroken token.
_PLACEHOLDERS = ("your-", "your_", "changeme", "placeholder", "example",
                 "insert", "xxx", "<", "...", "***", "todo", "none", "null")

_lock = threading.RLock()
_client: Any = None
_digest = ""
_error = ""
_reason = ""
_have_package = False


# ── configuration ───────────────────────────────────────────────────────────

def _config(key: str, default: str = "") -> str:
    """One config read, tolerant of the settings module being unavailable."""
    try:
        from config.settings import get
        return str(get(key, default) or "").strip()
    except Exception:
        return str(default or "").strip()


def _usable(value: str) -> bool:
    """Is this plausibly a real key rather than a template or pasted prose?"""
    text = str(value or "").strip()
    if len(text) < 8 or any(ch.isspace() for ch in text):
        return False
    low = text.lower()
    return not any(marker in low for marker in _PLACEHOLDERS)


def _api_key() -> str:
    env = str(os.environ.get(ENV_API_KEY) or "").strip()
    if _usable(env):
        return env
    stored = _config(CONFIG_API_KEY)
    return stored if _usable(stored) else ""


def api_key_present() -> bool:
    """Whether a usable project key exists — never the key itself."""
    return bool(_api_key())


def user_id() -> str:
    """The Composio identity every connected account is scoped to."""
    return _config(CONFIG_USER_ID, DEFAULT_USER_ID) or DEFAULT_USER_ID


# ── the last failure, in words ───────────────────────────────────────────────

def _remember(detail: str, why: str) -> None:
    global _error, _reason
    _error, _reason = str(detail or ""), str(why or "")


def error() -> str:
    """Why there is no client right now ('' when healthy)."""
    with _lock:
        return _error


def error_reason() -> str:
    with _lock:
        return _reason


# ── the optional package ────────────────────────────────────────────────────

def _import_package() -> bool:
    """Cached only on success.

    A negative result is never remembered, so installing the package and then
    pressing SAVE in Settings works without restarting the assistant.
    """
    global _have_package
    with _lock:
        if _have_package:
            return True
        try:
            import composio  # type: ignore  # noqa: F401
        except Exception:
            return False
        _have_package = True
        return True


def _version() -> str:
    if not _import_package():
        return ""
    try:
        import composio  # type: ignore
        return str(getattr(composio, "__version__", "") or "")
    except Exception:
        return ""


# ── the client ──────────────────────────────────────────────────────────────

def reset() -> None:
    """Drop the cached client — called after the key or the user id changes."""
    global _client, _digest
    with _lock:
        old, _client, _digest = _client, None, ""
        _remember("", "")
    # Closing the previous client is best effort: a stale HTTP client must never
    # be the reason a settings save fails.
    try:
        closer = getattr(getattr(old, "client", None), "close", None)
        if callable(closer):
            closer()
    except Exception:
        pass


def client() -> Any | None:
    """The cached ``composio.Composio``, or ``None`` with :func:`error` set.

    Callers treat ``None`` as "the connector is unusable right now" and read
    :func:`error` for the sentence to show the user. Never raises, never touches
    the network.
    """
    global _client, _digest
    with _lock:
        if not _import_package():
            _remember(PACKAGE_HINT, "missing_dependency")
            return None

        key = _api_key()
        if not key:
            _remember("Composio is not configured yet. Add your project API key "
                      "on Settings ▸ API, or set COMPOSIO_API_KEY, then "
                      "press SAVE.", "missing_key")
            return None

        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if _client is not None and _digest == digest:
            return _client

        try:
            from composio import Composio  # type: ignore
            built = Composio(api_key=key)
        except Exception as exc:
            _client, _digest = None, ""
            _remember(f"Composio could not be initialised: "
                      f"{type(exc).__name__}: {exc}", "unavailable")
            return None

        _client, _digest = built, digest
        _remember("", "")
        return _client


# ── honest health ───────────────────────────────────────────────────────────

def status() -> dict:
    """What the connector layer can report without a single network request.

    ``available`` means exactly that: a client object exists. Whether Composio
    *accepts* the key is a separate question the manager answers with a real
    call, and the UI never dresses one up as the other.
    """
    package = _import_package()
    configured = bool(_api_key())
    ok, detail, why = False, "", ""

    if not package:
        detail, why = PACKAGE_HINT, "missing_dependency"
    elif not configured:
        detail = ("Composio is not configured yet. Add your project API key on "
                  "Settings ▸ API, or set COMPOSIO_API_KEY.")
        why = "missing_key"
    elif client() is not None:
        ok, detail = True, "Composio is configured and the connector is ready."
    else:
        detail, why = error() or "Composio is unavailable.", (
            error_reason() or "unavailable")

    return {"available": ok, "configured": configured, "package": package,
            "version": _version(), "reason": "" if ok else why,
            "detail": detail, "user_id": user_id()}