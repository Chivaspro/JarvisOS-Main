"""The Connector Manager — J.A.R.V.I.S.'s one route to every external app.

Everything above this module (the HUD, the ``connector`` tool, the agent's tool
calls) and everything below it (Composio, and through Composio Spotify, Gmail,
GitHub, Telegram …) meet here. The rule this file exists to hold is the
architecture rule: **there is exactly one connector**, so no app ever gets a
class, a settings page, a token store or an OAuth flow of its own.

    J.A.R.V.I.S. → manager → Composio → toolkit → tool → service

Sessions, not direct execution
------------------------------
Composio's current integration is the **session**: ``composio.create(user_id=…)``
returns one object carrying tool discovery, the user's connected accounts and
toolkit versioning. Direct execution (``tools.execute`` with a bare user id) is a
legacy path, so this module uses it only as a last resort.

Authentication is Composio's, not ours
--------------------------------------
``connect()`` asks Composio for a **Connect Link** and opens it in the browser.
No provider OAuth app is built here, no client secret is stored here, and no
access or refresh token ever passes through this process — Composio holds them.
The only credential J.A.R.V.I.S. owns is the project API key, and that lives in
:mod:`connectors.composio_client`.

Every call returns a dict, never raises
---------------------------------------
``{"ok": bool, "detail": str, "reason": str, …}`` is the shape throughout, so the
Settings window and the model can report a failure honestly without a traceback
reaching the user. A missing package, a missing key, a refused key, a cancelled
authorization and a rate limit are five different sentences.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from connectors import composio_client as _composio

__all__ = [
    "ACCOUNT_STATES", "capabilities", "connect", "disconnect", "drop_session",
    "execute", "list_connected", "list_toolkits", "reconnect", "report",
    "search_tools", "session", "status", "summary", "supported_toolkits",
    "toolkits_for_app", "verify",
]

#: How long a toolkit listing is trusted before the next call asks again. The
#: catalog changes rarely; a minute keeps the Connector page snappy and is short
#: enough that a newly enabled app appears without a restart.
TOOLKIT_CACHE_S = 60.0

#: Composio's connection states, translated once for every surface showing one.
#: ``ACTIVE`` is the only state that means "usable".
ACCOUNT_STATES = {
    "ACTIVE": "connected", "INITIATED": "pending", "INITIALIZING": "pending",
    "EXPIRED": "expired", "FAILED": "failed", "REVOKED": "revoked",
    "INACTIVE": "disabled",
}

_lock = threading.RLock()
_sessions: dict[str, Any] = {}
_toolkits_cache: tuple[float, list[dict]] | None = None

#: Why the last catalog read came back empty ('' when it succeeded). Kept apart
#: from the client's own error so "no key" and "key refused by the catalog" are
#: still two different sentences in status().
_catalog_error = ""


# ── reading Composio's response objects ─────────────────────────────────────
# The SDK returns pydantic models whose attribute names differ from the JSON the
# REST API serves (``createdAt`` vs ``created_at``). One reader, tried in a fixed
# order, keeps every caller from inventing its own guess.

def _field(obj: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) not in (None, ""):
                return obj[name]
            continue
        try:
            value = getattr(obj, name, None)
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    return default


def _items(payload: Any) -> list[Any]:
    """The list inside a ``…ListResponse``, whatever it is called."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return list(payload)
    for name in ("items", "data", "results", "accounts", "toolkits"):
        value = _field(payload, name, default=None)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            inner = _field(value, "items", "data", default=None)
            if isinstance(inner, list):
                return list(inner)
    return []


def _ok(detail: str, **extra: Any) -> dict:
    return {"ok": True, "detail": str(detail or ""), "reason": "", **extra}


def _fail(detail: str, reason: str = "action_failed", **extra: Any) -> dict:
    return {"ok": False, "detail": str(detail or ""), "reason": str(reason),
            **extra}


def _unavailable() -> dict | None:
    """A failure dict when the client cannot be built, else ``None``.

    Checked once per call so every public function opens with the same three
    sentences (no package / no key / client refused to build) instead of three
    different exceptions.
    """
    if _composio.client() is not None:
        return None
    return _fail(_composio.error() or "Composio is unavailable.",
                 _composio.error_reason() or "unavailable")


def _app_slug(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text.replace("-", "_").replace(" ", "_")


# ── the session ─────────────────────────────────────────────────────────────

def session(user_id: str = "", *, refresh: bool = False) -> Any | None:
    """The cached Composio session for *user_id*, created on first use.

    A session is a server-side object, so creating one per tool call would spend
    a request on bookkeeping rather than work. They are cached by user id.
    ``refresh=True`` forces a new one, which is what a reconnect uses.

    Raises ``RuntimeError`` with a full sentence when Composio refuses; callers
    that must not raise use :func:`status` first or wrap the call.
    """
    uid = str(user_id or "").strip() or _composio.user_id()
    if _unavailable() is not None:
        return None
    with _lock:
        if not refresh and uid in _sessions:
            return _sessions[uid]
        comp = _composio.client()
        try:
            # manage_connections stays on: an interactive agent must be able to
            # hand back a Connect Link mid-conversation rather than replying
            # "not connected" with no way forward.
            built = comp.create(user_id=uid, manage_connections=True)
        except Exception as exc:
            _sessions.pop(uid, None)
            raise RuntimeError(f"Composio could not open a session: {exc}") from exc
        _sessions[uid] = built
        return built


def drop_session(user_id: str = "") -> None:
    """Forget the cached session for *user_id* (or every one of them)."""
    uid = str(user_id or "").strip()
    with _lock:
        if uid:
            _sessions.pop(uid, None)
        else:
            _sessions.clear()


# ── toolkits: what Composio can reach at all ────────────────────────────────

def list_toolkits(*, refresh: bool = False, limit: int = 400) -> list[dict]:
    """Every toolkit the configured Composio project exposes, as plain dicts.

    This is the catalog, not a whitelist: an app appears here because Composio
    said so, never because this project guessed that it should. On failure the
    list comes back empty and :func:`status` carries the reason, so the Connector
    page says "Composio is not configured" instead of showing an empty shelf.
    """
    global _toolkits_cache, _catalog_error
    if _unavailable() is not None:
        _catalog_error = _composio.error() or "Composio is unavailable."
        return []
    now = time.monotonic()
    with _lock:
        if (not refresh and _toolkits_cache is not None
                and (now - _toolkits_cache[0]) < TOOLKIT_CACHE_S):
            return list(_toolkits_cache[1])
    comp = _composio.client()
    rows: list[dict] = []
    try:
        cursor = ""
        for _page in range(12):  # bounded: a runaway catalog cannot spin forever
            kwargs: dict[str, Any] = {"limit": float(min(limit, 100))}
            if cursor:
                kwargs["cursor"] = cursor
            payload = comp.toolkits.list(**kwargs)
            page = _items(payload)
            for entry in page:
                slug = _app_slug(_field(entry, "slug", "name", default=""))
                if not slug:
                    continue
                rows.append({
                    "slug": slug,
                    "name": str(_field(entry, "label", "name", "displayName",
                                       default=slug) or slug),
                    "description": str(_field(entry, "description", "summary",
                                              default="") or ""),
                    "category": str(_field(entry, "category", "group",
                                           default="") or ""),
                    "managed_by": str(_field(entry, "managedBy", "managed_by",
                                             default="") or ""),
                })
            cursor = str(_field(payload, "nextCursor", "next_cursor",
                                default="") or "")
            if not page or not cursor or len(rows) >= limit:
                break
    except Exception as exc:
        # A refused key and a network failure both land here. The sentence goes
        # to status(); the list simply comes back empty.
        _catalog_error = f"Composio's toolkit catalog could not be read: {exc}"
        return []
    _catalog_error = ""
    rows.sort(key=lambda row: row["name"].lower())
    with _lock:
        _toolkits_cache = (now, rows)
    return list(rows)


def supported_toolkits() -> dict[str, str]:
    """``{slug: name}`` for everything Composio offers — the availability map.

    Every surface that shows an app checks it against this first. An app that is
    not here is not offered, which is how this project avoids fake integrations.
    """
    return {row["slug"]: row["name"] for row in list_toolkits()}


def toolkits_for_app(*terms: str) -> list[str]:
    """Resolve what the user said to the toolkit slugs that match it.

    "Instagram" and "ig" and "insta" all land on the real slug Composio uses,
    and a term matching nothing returns an empty list rather than a guess.
    """
    catalog = list_toolkits()
    wanted = " ".join(str(term or "") for term in terms).strip().lower()
    if not wanted:
        return []
    needle = _app_slug(wanted)
    exact = [row["slug"] for row in catalog
             if row["slug"] == needle or _app_slug(row["name"]) == needle]
    if exact:
        return exact
    loose = [row["slug"] for row in catalog
             if needle and (needle in row["slug"]
                            or needle in _app_slug(row["name"]))]
    return loose


# ── connected accounts: what this user has actually authorised ───────────────

def _account_name(entry: Any) -> str:
    """The human label for an account: its alias, else a name inside ``data``.

    ``data`` is provider-shaped — Gmail puts an email there, Spotify a display
    name — so this reads a short list of likely keys and falls back to the alias.
    It never walks the whole block: an unknown shape must not become an unknown
    amount of data leaving the connector.
    """
    alias = str(_field(entry, "alias", default="") or "").strip()
    if alias:
        return alias
    data = _field(entry, "data", default=None)
    if isinstance(data, dict):
        for key in ("name", "displayName", "display_name", "email", "username",
                    "user_name", "accountName", "id"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


# ── connecting: the Connect Link, and nothing else ──────────────────────────

def _classify(exc: Exception) -> dict:
    """Which of Composio's refusals this is — the classification comes first.

    A revoked key, a cancelled authorization and a rate limit are three sentences
    the user must act on differently, and testing the exception *type* before its
    message would report every throttle as a generic failure.
    """
    text = " ".join(str(exc).split())
    low = text.lower()
    name = type(exc).__name__
    if "multiple connected" in low or "multipleconnected" in name.lower():
        return _fail("That app is already connected. Reconnect or disconnect it "
                     "first.", "already_connected", exception=name)
    if ("invalid_key" in low or "invalidapikey" in name.lower()
            or "unauthorized" in low or "forbidden" in low
            or ("api key" in low and ("invalid" in low or "rejected" in low
                                      or "revoked" in low or "expired" in low))):
        return _fail("Composio rejected the project API key. Check it on "
                     "Settings ▸ API.", "invalid_key", exception=name)
    if "timeout" in low or "timed out" in low:
        return _fail("Composio took too long to answer. Nothing was changed — "
                     "try again in a moment.", "timeout", exception=name)
    if "429" in text or "rate limit" in low or "too many" in low:
        return _fail("Composio is rate limiting this project. It clears in a "
                     "moment; I will not hammer it.", "rate_limited",
                     exception=name)
    if ("connectionerror" in low or "getaddrinfo" in low or "network" in low
            or "could not be reached" in low or "name resolution" in low):
        return _fail("Composio could not be reached — check the internet "
                     "connection.", "network", exception=name)
    if "notfound" in name.lower() or "not found" in low or "unknown toolkit" in low:
        return _fail(f"Composio has no such app: {text}", "unknown_toolkit",
                     exception=name)
    if "permission" in low or "scope" in low or "quota" in low:
        return _fail(f"Composio refused that request ({text}).", "forbidden",
                     exception=name)
    return _fail(f"{name}: {text}", "action_failed", exception=name)


def connect(toolkit: str, *, user_id: str = "", alias: str = "",
            callback_url: str = "", open_browser: bool = False) -> dict:
    """Ask Composio for a Connect Link for *toolkit*.

    This is the whole of J.A.R.V.I.S.'s OAuth involvement: it asks, gets a URL,
    and hands it to whoever pressed the button. No provider client secret is built
    here, no consent screen is rendered here, and no token ever arrives here —
    Composio runs the exchange and keeps the result.

    The link comes from the **session's** ``authorize()``, which is Composio's
    current path. The older ``connected_accounts.initiate()`` wraps a REST
    endpoint that retires for Composio-managed OAuth, so this module never calls
    it, and neither should anything added later.

    ``open_browser`` serves the agent path; the Settings window opens the link
    itself so it can show Authorizing… while the browser has focus.
    """
    problem = _unavailable()
    if problem is not None:
        return problem
    slug = _app_slug(toolkit)
    if not slug:
        return _fail("Which app shall I connect, sir?", "no_target")

    catalog = supported_toolkits()
    label = catalog.get(slug, "")
    if catalog and not label:
        # Only apps Composio actually offers are ever shown as connectable.
        matches = toolkits_for_app(slug)
        if matches:
            slug, label = matches[0], catalog.get(matches[0], matches[0])
        else:
            return _fail(f"Composio does not offer '{toolkit}', so there is "
                         f"nothing to connect. That is not a missing integration "
                         f"— the app is simply not in this project's catalog.",
                         "unsupported")

    uid = str(user_id or "").strip() or _composio.user_id()
    try:
        sess = session(uid)
        if sess is None:
            return _fail(_composio.error() or "Composio is unavailable.",
                         _composio.error_reason() or "unavailable")
        kwargs: dict[str, Any] = {}
        if alias:
            kwargs["alias"] = str(alias)
        if callback_url:
            kwargs["callback_url"] = str(callback_url)
        request = sess.authorize(slug, **kwargs)
    except Exception as exc:
        return _classify(exc)

    url = str(_field(request, "redirect_url", "redirectUri", "url",
                     default="") or "")
    connection_id = str(_field(request, "id", "connection_id", "connectionId",
                               default="") or "")
    if not url:
        return _fail("Composio returned no authorization link, so nothing was "
                     "opened. Try again, or check the toolkit in the Composio "
                     "dashboard.", "no_link", connection_id=connection_id)

    result = _ok(f"To connect {label or slug}, authorize it in the browser.",
                 redirect_url=url, connection_id=connection_id, toolkit=slug,
                 state="pending")
    if not open_browser:
        return result
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as exc:
        # The link is still valid and still returned: a browser that will not
        # open is an inconvenience, not a failed authorization.
        result["detail"] = (f"Composio gave me the link, but the browser would "
                            f"not open ({exc}). Open it yourself to finish "
                            f"connecting {label or slug}.")
    return result


def _account_row(entry: Any) -> dict:
    """One connected account, flattened to the fields the UI and agent need.

    Deliberately **no token-shaped key survives this function**: Composio's
    response objects carry more than this, and the only thing that ever leaves
    the manager is a label, a state and a couple of timestamps.
    """
    # The names below are Composio's own, read off the live SDK: id, toolkit,
    # status, alias, created_at, updated_at, user_id, and a data block holding
    # whatever the provider calls the account. The camelCase spellings are tried
    # second, so a future SDK that renames them degrades rather than blanks out.
    raw_status = str(_field(entry, "status", "state", default="") or "").upper()
    slug = _app_slug(_field(entry, "toolkit", "toolkit_slug", "toolkitSlug",
                            default=""))
    account_id = str(_field(entry, "id", "nanoid", default="") or "")
    name = _account_name(entry) or slug
    if bool(_field(entry, "is_disabled", "isDisabled", default=False)) and (
            raw_status == "ACTIVE"):
        raw_status = "INACTIVE"
    return {
        "id": account_id,
        "slug": slug,
        "name": name,
        "state": ACCOUNT_STATES.get(raw_status, raw_status.lower() or "unknown"),
        "raw_status": raw_status,
        "created_at": str(_field(entry, "created_at", "createdAt", default="") or ""),
        "updated_at": str(_field(entry, "updated_at", "updatedAt", default="") or ""),
        "reason": str(_field(entry, "status_reason", "statusReason", default="") or ""),
        "user_id": str(_field(entry, "user_id", "userId", default="") or ""),
    }


def list_connected(*, user_id: str = "", toolkit: str = "") -> list[dict]:
    """Every connected account for this J.A.R.V.I.S. user, newest first.

    Scoped by ``user_ids`` on the request rather than filtered afterwards, so a
    multi-account deployment cannot leak one user's Spotify link into another's
    Connector page. ``toolkit`` narrows it to one app.
    """
    problem = _unavailable()
    if problem is not None:
        return []
    uid = str(user_id or "").strip() or _composio.user_id()
    comp = _composio.client()
    kwargs: dict[str, Any] = {"user_ids": [uid], "limit": 100.0}
    slug = _app_slug(toolkit)
    if slug:
        kwargs["toolkit_slugs"] = [slug]
    try:
        payload = comp.connected_accounts.list(**kwargs)
    except Exception as exc:
        _remember_accounts(f"The connected accounts could not be read: {exc}")
        return []
    rows = [_account_row(entry) for entry in _items(payload)]
    rows = [row for row in rows if row["id"]]
    rows.sort(key=lambda row: (row["created_at"] or "", row["name"]), reverse=True)
    _remember_accounts("")
    return rows


_accounts_error = ""


def _remember_accounts(detail: str) -> None:
    global _accounts_error
    _accounts_error = str(detail or "")


def verify(connection_id: str, *, timeout: float = 120.0) -> dict:
    """Wait for one pending connection to become ACTIVE.

    Called after the user finishes in the browser. The wait polls inside the SDK,
    so this must run on a worker thread — never the GUI thread.
    """
    problem = _unavailable()
    if problem is not None:
        return problem
    cid = str(connection_id or "").strip()
    if not cid:
        return _fail("There is no pending connection to verify.", "no_target")
    comp = _composio.client()
    try:
        payload = comp.connected_accounts.wait_for_connection(cid, timeout)
    except Exception as exc:
        text = " ".join(str(exc).split()).lower()
        if "timeout" in text:
            return _fail("The authorization was not completed in time. It may "
                         "have been closed in the browser — try again.",
                         "cancelled")
        if "terminal state" in text:
            return _fail("The connection was refused or failed before it became "
                         "active. " + str(exc), "denied")
        return _classify(exc)
    row = _account_row(payload)
    if row["state"] != "connected":
        return _fail(f"That app is not connected yet — its state is "
                     f"{row['raw_status'] or row['state']}.", "not_connected",
                     account=row)
    # A new account changes what the session can see, so the cached session goes.
    drop_session(row["user_id"])
    return _ok(f"{row['name'] or row['slug']} is connected.", account=row,
               state="connected")


def disconnect(account_id: str) -> dict:
    """Revoke one connected account. The tokens die on Composio's side, not ours."""
    problem = _unavailable()
    if problem is not None:
        return problem
    cid = str(account_id or "").strip()
    if not cid:
        return _fail("Which connection shall I remove, sir?", "no_target")
    comp = _composio.client()
    try:
        payload = comp.connected_accounts.revoke(cid)
    except Exception as exc:
        text = " ".join(str(exc).split())
        low = text.lower()
        exc_name = type(exc).__name__.lower()
        if "notsupported" in exc_name or "does not support" in low:
            # Revoking upstream needs provider support; the record can still go.
            try:
                comp.connected_accounts.delete(cid, revoke_on_delete=True)
                drop_session()
                return _ok("The connection was removed. The provider does not "
                           "support programmatic token revocation, so revoke "
                           "access inside that app too if it matters.",
                           account_id=cid)
            except Exception as inner:
                return _fail(f"That app could not be disconnected: {inner}",
                             "disconnect_failed")
        if "notrevokable" in exc_name or "revokable state" in low:
            return _fail("That connection is already inactive, so there is "
                         "nothing to revoke.", "not_revokable")
        return _classify(exc)
    account = _field(payload, "connected_account", default=payload)
    slug = _app_slug(_field(account, "toolkit", default=""))
    drop_session()
    return _ok(f"{slug or 'That app'} is disconnected.", account_id=cid,
               toolkit=slug)


def reconnect(toolkit: str, *, account_id: str = "", user_id: str = "",
              open_browser: bool = False) -> dict:
    """Re-authorize an app whose connection expired, was denied or drifted.

    The old account is revoked first when its id is known, so a reconnect cannot
    leave two live connections competing for one toolkit.
    """
    problem = _unavailable()
    if problem is not None:
        return problem
    if str(account_id or "").strip():
        try:
            _composio.client().connected_accounts.revoke(str(account_id).strip())
        except Exception:
            # A dead connection is exactly what a reconnect is for; failing to
            # revoke it must not block asking for a new one.
            pass
    drop_session(user_id)
    return connect(toolkit, user_id=user_id, open_browser=open_browser)


# ── executing: what makes this a connector and not a dashboard ──────────────

#: How Composio says "this account is not authorized yet", in every shape it has
#: used. Matched on the message because the SDK raises the same exception class
#: for a throttle and for a dead connection.
_NOT_CONNECTED_MARKERS = (
    "not connected", "no connected account", "unauthorized", "unauthenticated",
    "connection_required", "requires_connection", "auth_required",
    "token has expired", "revoked", "reconnect", "401",
)


def _needs_connection(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _NOT_CONNECTED_MARKERS)


def _app_label(slug: str) -> str:
    """A display name for a toolkit slug, without inventing an integration."""
    from connectors import icons
    return icons.label_for(slug, slug.replace("_", " "))


def _not_connected(toolkit: str, uid: str, log_id: str = "") -> dict:
    """The friendly answer for a dead connection: name it, offer the link.

    A raw SDK exception quoted at a user is noise; "Spotify needs to be
    reconnected" plus a working Connect Link is an answer. The link is fetched
    once and reused, never opened from here — the caller decides.
    """
    label = _app_label(toolkit)
    link = connect(toolkit, user_id=uid)
    out = _fail(f"{label} needs to be reconnected before I can do that.",
                "not_connected", toolkit=toolkit, log_id=log_id)
    if link.get("ok"):
        out["redirect_url"] = str(link.get("redirect_url") or "")
        out["connection_id"] = str(link.get("connection_id") or "")
        out["detail"] = (f"{label} needs to be reconnected. I have a fresh "
                         f"authorization link ready for you.")
    return out


def _flatten_output(value: Any, depth: int = 0) -> Any:
    """Reduce a provider payload to something a model can actually read.

    Composio returns whatever the service returned, often nested several layers
    deep inside pagination envelopes. Unwrapping happens here once, so no caller
    re-invents its own walk and no raw blob reaches the chat, where the user
    would otherwise have to read it.
    """
    if depth > 4:
        return str(value)[:2000]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_flatten_output(item, depth + 1) for item in list(value)[:60]]
    if isinstance(value, dict):
        # A single-payload envelope is unwrapped, so the model reads the tracks
        # rather than {"data": {"data": …}}.
        for key in ("data", "output", "result", "results", "items", "response"):
            if len(value) == 1 and key in value:
                return _flatten_output(value[key], depth + 1)
        return {str(k): _flatten_output(v, depth + 1)
                for k, v in list(value.items())[:40]}
    return str(value)[:2000]


def execute(tool_slug: str, arguments: dict | None = None, *,
            account: str = "", user_id: str = "",
            on_progress: Any = None) -> dict:
    """Run one Composio tool through the session.

    This is what makes the connector real rather than decorative: the agent names
    a tool slug, Composio runs it against the user's connected account, and the
    result comes back saying what actually happened. No app-specific code is
    involved, which is precisely why adding a Composio app adds no connector.

    ``on_progress`` receives short human lines ("Searching Spotify…") for the
    J.A.R.V.I.S. activity feed. It is called best-effort; a broken listener can
    never fail the action it was reporting on.
    """
    problem = _unavailable()
    if problem is not None:
        return problem
    slug = str(tool_slug or "").strip()
    if not slug:
        return _fail("Which action shall I run, sir?", "no_target")
    uid = str(user_id or "").strip() or _composio.user_id()
    toolkit = _app_slug(slug.split("_")[0])
    label = _app_label(toolkit)

    def say(line: str) -> None:
        if not callable(on_progress):
            return
        try:
            on_progress(str(line))
        except Exception:
            pass

    say(f"Using {label}…")
    try:
        sess = session(uid)
        if sess is None:
            return _fail(_composio.error() or "Composio is unavailable.",
                         _composio.error_reason() or "unavailable")
        kwargs: dict[str, Any] = {"arguments": dict(arguments or {})}
        if account:
            kwargs["account"] = str(account)
        payload = sess.execute(slug, **kwargs)
    except Exception as exc:
        classified = _classify(exc)
        if _needs_connection(classified.get("detail", "")) or _needs_connection(
                str(exc)):
            say(f"{label} is not connected — asking Composio for a link.")
            return _not_connected(toolkit, uid)
        return classified

    success = _field(payload, "success", "is_success", "ok", default=None)
    error_text = str(_field(payload, "error", "error_message", "message",
                            default="") or "")
    data = _flatten_output(_field(payload, "data", "output", "result",
                                  default=payload))
    log_id = str(_field(payload, "id", "log_id", "logId", "request_id",
                        default="") or "")

    if success is False or (success is None and error_text):
        if _needs_connection(error_text):
            say(f"{label} is not connected — asking Composio for a link.")
            return _not_connected(toolkit, uid, log_id)
        return _fail(error_text or "Composio reported a failure.",
                     "tool_failed", data=data, log_id=log_id, toolkit=toolkit)

    say(f"{label} answered.")
    return _ok(f"{label} carried out {slug}.", data=data, log_id=log_id,
               toolkit=toolkit, slug=slug)


# ── discovery: the agent learns what an app can do at runtime ───────────────

def _tool_row(entry: Any) -> dict:
    """One Composio tool, flattened to what the model needs to call it."""
    return {
        "slug": str(_field(entry, "slug", "name", default="") or ""),
        "name": str(_field(entry, "name", default="") or ""),
        "description": str(_field(entry, "human_description", "description",
                                  default="") or ""),
        "toolkit": _app_slug(_field(entry, "toolkit", default="")),
        "no_auth": bool(_field(entry, "no_auth", default=False)),
        "scopes": [str(scope) for scope in
                   (_field(entry, "scopes", default=[]) or [])][:20],
        "input_parameters": _flatten_output(
            _field(entry, "input_parameters", default={}), 3),
    }


def _narrow(rows: list[dict], toolkit: str, limit: int) -> list[dict]:
    """Keep the rows for one app and cap the list. An empty slug is dropped."""
    slug = _app_slug(toolkit)
    if slug:
        rows = [row for row in rows if not row["toolkit"] or row["toolkit"] == slug]
    rows = [row for row in rows if row["slug"]]
    return rows[:max(1, int(limit or 25))]


_tools_error = ""


def _remember_tools(detail: str) -> None:
    global _tools_error
    _tools_error = str(detail or "")


def search_tools(query: str, *, toolkit: str = "", limit: int = 25,
                 user_id: str = "") -> list[dict]:
    """Find Composio tools by meaning, for the agent to choose from.

    Dynamic discovery is the point of the whole architecture: nothing in
    J.A.R.V.I.S. hardcodes what Spotify can do, so a capability Composio adds
    next month is usable the day it appears, with no new connector.

    The raw catalog is used rather than the session's semantic search because
    that is the documented, stable shape (`get_raw_composio_tools` accepting
    ``toolkits``/``search``/``limit``). A session search would rank better but
    takes no limit and returns a different envelope, so it is not guessed at.
    """
    problem = _unavailable()
    if problem is not None:
        return []
    text = " ".join(str(query or "").split())
    slugs = toolkits_for_app(toolkit) if toolkit else []
    if not text and not slugs:
        return []
    comp = _composio.client()
    try:
        kwargs: dict[str, Any] = {"limit": max(1, int(limit) * 2)}
        if slugs:
            kwargs["toolkits"] = slugs
        if text:
            kwargs["search"] = text
        entries = comp.tools.get_raw_composio_tools(**kwargs)
    except Exception as exc:
        _remember_tools(f"Composio's tool catalog could not be read: {exc}")
        return []
    _remember_tools("")
    return _narrow([_tool_row(entry) for entry in entries or []], toolkit, limit)


def capabilities(toolkit: str, *, user_id: str = "", limit: int = 40) -> list[dict]:
    """What one connected app can actually do — the Manage view's list.

    Built from the live catalog rather than a table in this project, so the
    capability list is a fact about the account, not a promise about the code.
    """
    slug = _app_slug(toolkit)
    if not slug:
        return []
    return search_tools("", toolkit=slug, limit=limit, user_id=user_id)


# ── honest health ───────────────────────────────────────────────────────────

def status(*, probe: bool = False, user_id: str = "") -> dict:
    """The connector's state, told in layers that are never confused.

    ``available``  — a client object exists (local, no network)
    ``reachable``  — Composio accepted the key and answered the catalog
    ``connected``  — at least one app is authorized for this user

    Collapsing those three is the classic way an integration lies: a stored key
    is not a working key, and a working key is not a connected account. With
    ``probe=False`` (the default) no network is touched at all, which is what the
    Settings window uses for its instant paint before the background refresh.
    """
    base = _composio.status()
    out = {
        "available": bool(base.get("available")),
        "configured": bool(base.get("configured")),
        "package": bool(base.get("package")),
        "version": str(base.get("version") or ""),
        "reason": str(base.get("reason") or ""),
        "detail": str(base.get("detail") or ""),
        "user_id": str(base.get("user_id") or ""),
        "reachable": False,
        "toolkits": 0,
        "connected": 0,
        "pending": 0,
        "expired": 0,
        "catalog_error": "",
        "accounts_error": "",
        "tools_error": "",
        "probed": False,
    }
    if not out["available"]:
        return out
    if not probe:
        # Offline answer, honestly labelled: the layer is configured, but nothing
        # is known about whether Composio agrees. The UI shows "not checked".
        out["detail"] = out["detail"] or "Composio is configured; not checked yet."
        out["reason"] = "not_checked"
        return out

    rows = list_toolkits()
    out["toolkits"] = len(rows)
    out["catalog_error"] = _catalog_error
    accounts = list_connected(user_id=user_id)
    out["accounts_error"] = _accounts_error
    out["tools_error"] = _tools_error
    out["connected"] = sum(1 for row in accounts if row["state"] == "connected")
    out["pending"] = sum(1 for row in accounts if row["state"] == "pending")
    out["expired"] = sum(1 for row in accounts
                         if row["state"] in ("expired", "failed", "revoked"))
    out["probed"] = True

    if rows:
        out["reachable"] = True
        out["reason"] = ""
        out["detail"] = (f"Composio answered with {len(rows)} app(s); "
                         f"{out['connected']} connected, {out['pending']} pending.")
    elif _catalog_error:
        # A refused key and a dead network both empty the catalog; the sentence
        # already says which, so it is passed through rather than reworded.
        out["detail"] = _catalog_error
        out["reason"] = ("invalid_key" if "key" in _catalog_error.lower()
                         else "unavailable")
    else:
        out["detail"] = ("Composio returned no apps — check the project in the "
                         "Composio dashboard.")
        out["reason"] = "empty_catalog"
    return out


def report(*, user_id: str = "", refresh: bool = False) -> dict:
    """Everything the Connector page draws, in one call.

    Catalog and accounts are joined here so the UI never has to learn that one
    comes from a paginated list and the other from a different endpoint. Only
    apps Composio actually offers appear in ``available``: an app present in the
    local icon table but absent from the catalog is decoration, not an
    integration, and is deliberately never offered as connectable. An app with a
    pending, expired or revoked account leaves ``available`` and appears in
    ``connected`` carrying that state, so the page can offer Reconnect.

    This makes network requests. The Settings window calls it from a worker
    thread and applies the result on the GUI thread.
    """
    from connectors import icons

    health = status(probe=True, user_id=user_id)
    catalog = list_toolkits(refresh=refresh)
    accounts = list_connected(user_id=user_id)

    by_slug: dict[str, list[dict]] = {}
    for account in accounts:
        if account["slug"]:
            by_slug.setdefault(account["slug"], []).append(account)

    connected: list[dict] = []
    for slug, group in sorted(by_slug.items()):
        entry = next((row for row in catalog if row["slug"] == slug), None)
        name = (entry or {}).get("name") or group[0]["name"] or slug
        best = max(group, key=lambda row: (row["state"] == "connected",
                                           row["created_at"]))
        connected.append({
            **icons.meta(slug, name),
            "account": best,
            "accounts": group,
            "state": best["state"],
            "count": len(group),
            "in_catalog": entry is not None,
        })
    connected.sort(key=lambda row: (row["state"] != "connected",
                                    row["label"].lower()))

    available = [{**icons.meta(row["slug"], row["name"]),
                  "description": row["description"],
                  "category": row["category"],
                  "state": "available"}
                 for row in catalog if row["slug"] not in by_slug]

    return {"health": health, "connected": connected, "available": available,
            "total_available": len(available), "user_id": health["user_id"],
            "powered_by": "Composio"}


def summary(*, user_id: str = "") -> str:
    """One paragraph for the assistant: the honest state of the connector.

    Reads through :func:`report` so the whole answer costs one probe, not three.
    """
    data = report(user_id=user_id)
    health = data["health"]
    if not health["available"]:
        return health["detail"]
    if not health["reachable"]:
        return f"Composio is configured but not answering: {health['detail']}"

    lines = [f"Composio is the only connector ({health['toolkits']} apps offered, "
             f"{health['connected']} connected)."]
    if health["expired"]:
        lines.append(f"{health['expired']} connection(s) need reconnecting.")
    if health["pending"]:
        lines.append(f"{health['pending']} authorization(s) are waiting on you.")
    names = [row["label"] for row in data["connected"]
             if row["state"] == "connected"]
    if names:
        lines.append("Connected: " + ", ".join(names[:12]) + ".")
    else:
        lines.append("Nothing is connected yet — use the connector's connect "
                     "action, or Settings ▸ API for the key, or ask "
                     "me to connect an app and I will bring you the link.")
    return " ".join(lines)