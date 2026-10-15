"""The model-facing ``connector`` tool — one tool, every external app.

This is the thin, honest edge between Gemini and :mod:`connectors.manager`. It
owns no integration logic of its own: it validates what the model asked for,
calls the manager, and flattens the answer into one sentence the voice layer can
speak plus a payload the HUD can draw. Every action returns a string, because
that is the dialect the Live session's tool results are read in.

Why one tool instead of one per app
-----------------------------------
The alternative is what this project is moving away from: a ``spotify`` tool, an
``instagram`` tool, a ``gmail`` tool, each with a hardcoded action enum. That
scales badly in two directions — Gemini's schema grows with every app, and every
new app needs code. Here the model sees five verbs and the *catalog*, so the
day Composio adds an app, J.A.R.V.I.S. can use it with no change in this file.

* ``status``   — is the connector alive, and what is connected
* ``list``     — what apps exist at all, and what this account can do
* ``connect``  — bring back the authorization link
* ``run``      — execute one tool slug with arguments
* ``disconnect`` — revoke one account (always confirms in the HUD)

The gate that stays
-------------------
Every verb is gated on the single ``connect`` permission switch, so turning the
connector off in Settings closes all six verbs at once — including ``status``,
because a half-open connector is worse than a closed one.

There is deliberately **no per-action HUD confirmation** on this path: the
connector is not in ``capabilities/registry.py``, so it does not inherit the
registry's confirm gate, and pretending otherwise would be a lie in a docstring.
What it does have instead is that every result names the app it touched and the
Composio log id, the user can revoke any account in the Composio dashboard, and
``disconnect`` is reversible by ``connect``. Reading is free; acting is reported.
"""

from __future__ import annotations

from typing import Any, Callable

from connectors import manager

__all__ = ["DECLARATION", "handle_connector", "PARAMS", "VERBS"]

#: The five verbs the model may use. Deliberately small: the vocabulary of an
#: app lives in Composio's catalog, not in this enum.
VERBS = ("status", "list", "connect", "verify", "run", "disconnect")

#: The shared parameter superset, in the Live function-declaration dialect
#: (upper-case type names, STRING/OBJECT/ARRAY/NUMBER/BOOLEAN).
PARAMS: dict[str, tuple[str, str]] = {
    "action": ("STRING", "The operation: status, list, connect, run or "
                          "disconnect."),
    "app": ("STRING", "The app to act on, by name or slug — 'spotify', "
                      "'gmail', 'github', 'telegram'. Matched against the "
                      "apps Composio actually offers."),
    "tool": ("STRING", "For run: the exact tool slug to execute, as reported "
                       "by list — e.g. 'spotify_search_playlists'."),
    "arguments": ("OBJECT", "For run: the arguments for that tool slug, "
                            "exactly as list described them."),
    "query": ("STRING", "For list: what to search the catalog for, in plain "
                        "words. Omit to list everything an app can do."),
    "account": ("STRING", "For run: which connected account to act as, when the "
                          "same app is connected more than once. For verify: the "
                          "connection reference that connect returned."),
}

#: The one line the model reads when a verb is missing or invented. Naming the
#: allowed verbs is what lets a mishear recover in the same turn.
_VERB_HELP = ("connector takes one of: " + ", ".join(VERBS) + ".")


def _text(value: Any, limit: int = 4000) -> str:
    """Flatten anything into a sentence-shaped string, bounded.

    A tool result goes straight into the model's context and often into the
    voice queue, so an unbounded provider payload is a real hazard: it is both
    unreadable and a way to spend a whole turn on JSON noise.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        try:
            import json
            text = json.dumps(value, ensure_ascii=False, default=str, indent=None)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _progress(on_event: Callable[[str], None] | None, line: str) -> None:
    """Report real progress, and never let the reporter break the action."""
    if not callable(on_event):
        return
    try:
        on_event(str(line))
    except Exception:
        pass


def _result(sentence: str, **payload: Any) -> str:
    """The one exit shape: a spoken sentence, plus a compact data trailer.

    The HUD and the agent read the sentence; the trailer carries the identifiers
    (a connection id, a slug) that a later turn needs but a voice reply does
    not. Keeping them in one string is how every other local tool in this
    project already reports, so the connector is not special here.
    """
    text = _text(sentence) or "Done."
    # Emptiness is tested BY TYPE, never by truthiness. `None` and empty
    # containers carry no information and would only bloat the trailer, but a
    # count of 0 and a flag of False ARE the answer — "0 apps connected" is real
    # news, and `0 == False` in Python is exactly how a truthiness filter throws
    # it away. Dropping a zero would make the connector lie by omission.
    keep = {key: value for key, value in payload.items()
            if value is not None
            and not (isinstance(value, (str, bytes, list, tuple, dict, set))
                     and not value)}
    if not keep:
        return text
    try:
        import json
        trailer = json.dumps(keep, ensure_ascii=False, default=str)[:1200]
    except Exception:
        return text
    return f"{text}\n[connector data: {trailer}]"


# ── the permission switch ────────────────────────────────────────────────────

#: The single switch that gates this whole tool.
#:
#: It is NOT called ``connect``. That flag already belongs to JarvisOS Connect —
#: the paired phone and browser gateway — and is load-bearing in eight places:
#: FEATURE_DEFAULTS, _PERMISSION_LABEL, the ``Service(id="connect")`` row in
#: services/hub.py, the PERMISSIONS checkbox, the quick-setup toggle, the
#: ``connect_devices`` tool map, and recovery/analyzer.py's path classifier.
#: Reusing it would have meant that turning off phone pairing silently killed
#: every external app, and vice versa. capabilities/registry.py:48-51 states the
#: rule this honours: each capability is its own relationship with its own
#: switch, so revoking one right must not revoke another.
#:
#: ``external_apps`` rather than ``connector`` on purpose: ``connect`` and
#: ``connector`` differ by three characters and mean the same thing in English,
#: which is a trap for the next reader grepping for one and finding the other.
FEATURE = "external_apps"


def _allowed(enabled: Any) -> bool:
    """Is the connector switched on? Tolerates every shape `enabled` arrives in.

    main.py passes the UI's callable, tests pass a dict, and a caller may pass
    nothing at all — in which case the saved configuration decides. A missing
    settings module must not crash a voice turn, so the fallback is "on".
    """
    if callable(enabled):
        try:
            return bool(enabled(FEATURE))
        except Exception:
            return True
    if isinstance(enabled, dict):
        return bool(enabled.get(FEATURE, True))
    try:
        from config.settings import get_feature
        return bool(get_feature(FEATURE, True))
    except Exception:
        return True


# ── the verbs ────────────────────────────────────────────────────────────────

def _verb_status(args: dict) -> str:
    health = manager.status(probe=True)
    if not health["available"]:
        return _result(health["detail"] or "Composio is not available.",
                       reason=health["reason"])
    if not health["reachable"]:
        return _result(health["detail"], reason=health["reason"])
    return _result(
        health["detail"], apps_offered=health["toolkits"],
        connected=health["connected"], pending=health["pending"],
        needs_reconnecting=health["expired"], user_id=health["user_id"],
        version=health["version"])


def _verb_list(args: dict) -> str:
    app = str(args.get("app") or "").strip()
    query = str(args.get("query") or "").strip()
    if app or query:
        rows = (manager.search_tools(query, toolkit=app) if query
                else manager.capabilities(app))
        if rows:
            brief = [{"slug": row["slug"],
                      "what": _text(row["description"], 160),
                      "needs_auth": not row["no_auth"]} for row in rows[:20]]
            return _result(f"{len(rows)} action(s) are available"
                           + (f" in {app}" if app else "") + ".", tools=brief)
        if app and manager.toolkits_for_app(app):
            return _result("Composio offered no actions for that request.",
                           reason="no_results", app=app, query=query)
        return _result(f"Composio does not offer '{app}', so there is nothing to "
                       f"list. Ask for status to see what is available.",
                       reason="unsupported", app=app)
    data = manager.report()
    health = data["health"]
    if not health["reachable"]:
        return _result(health["detail"], reason=health["reason"])
    connected = [{"app": row["label"], "state": row["state"],
                  "as": _text(row["account"]["name"], 80)}
                 for row in data["connected"][:25]]
    return _result(
        f"{health['connected']} app(s) connected, {data['total_available']} more "
        f"offered by Composio. Name one to see what it can do.",
        connected=connected, apps_available=data["total_available"])


def _verb_connect(args: dict, on_event) -> str:
    app = str(args.get("app") or "").strip()
    if not app:
        return _result("Which app shall I connect, sir?", reason="no_target")
    _progress(on_event, f"Asking Composio for a {app} authorization link…")
    result = manager.connect(app, open_browser=True)
    if not result.get("ok"):
        return _result(result.get("detail") or "Composio refused that request.",
                       reason=result.get("reason", "action_failed"))
    return _result(
        f"I have the link for {app}. Open it, approve access, and tell me when "
        f"you are done — I will confirm the connection rather than assume it.",
        url=result.get("redirect_url", ""),
        connection_id=result.get("connection_id", ""),
        app=result.get("toolkit", ""))


def _verb_verify(args: dict) -> str:
    ref = str(args.get("account") or args.get("connection_id") or "").strip()
    if not ref:
        return _result("Give me the connection reference from the connect "
                       "result and I will check it.", reason="no_target")
    result = manager.verify(ref)
    account = result.get("account") or {}
    if not result.get("ok"):
        return _result(result.get("detail") or "Not verified.",
                       reason=result.get("reason") or "not_connected",
                       app=account.get("slug", ""),
                       state=account.get("state", ""))
    return _result(result.get("detail") or "Verified.",
                   app=account.get("slug", ""), state=account.get("state", ""))


def _verb_run(args: dict, on_event) -> str:
    slug = str(args.get("tool") or "").strip()
    if not slug:
        return _result("Name the action to run — list an app first to see the "
                       "slugs Composio offers.", reason="no_target")
    raw = args.get("arguments")
    payload = dict(raw) if isinstance(raw, dict) else {}
    result = manager.execute(slug, payload,
                             account=str(args.get("account") or "").strip(),
                             on_progress=lambda line: _progress(on_event, line))
    if not result.get("ok"):
        extra = {"reason": result.get("reason", "action_failed")}
        if result.get("redirect_url"):
            extra["reconnect_url"] = result["redirect_url"]
        return _result(result.get("detail") or "Composio refused that action.",
                       **extra)
    return _result(result.get("detail") or "Composio carried that out.",
                   app=result.get("toolkit", ""), slug=slug,
                   data=result.get("data"), log_id=result.get("log_id", ""))


def _verb_disconnect(args: dict) -> str:
    app = str(args.get("app") or "").strip()
    ref = str(args.get("account") or "").strip()
    if not ref:
        if not app:
            return _result("Which app shall I disconnect, sir?",
                           reason="no_target")
        rows = [row for slug in manager.toolkits_for_app(app)
                for row in manager.list_connected(toolkit=slug)]
        if not rows:
            return _result(f"{app} is not connected, so there is nothing to "
                           f"disconnect.", reason="not_connected", app=app)
        rows.sort(key=lambda row: (row["state"] != "connected",
                                   row["created_at"] or ""))
        ref = rows[0]["id"]
    result = manager.disconnect(ref)
    if not result.get("ok"):
        return _result(result.get("detail") or "That could not be disconnected.",
                       reason=result.get("reason") or "disconnect_failed",
                       app=result.get("toolkit", ""), account_id=ref)
    return _result(result.get("detail") or "Disconnected.",
                   app=result.get("toolkit", ""), account_id=ref)


_VERBS = {
    "status": lambda args, on_event: _verb_status(args),
    "list": lambda args, on_event: _verb_list(args),
    "connect": _verb_connect,
    "verify": lambda args, on_event: _verb_verify(args),
    "run": _verb_run,
    "disconnect": lambda args, on_event: _verb_disconnect(args),
}


def handle_connector(args: dict | None = None, enabled: Any = None,
                     on_event: Callable[[str], None] | None = None) -> str:
    """The entry point main.py dispatches to. Always returns a string.

    The one guarantee this layer makes is that a crash becomes a sentence: a
    traceback escaping here would end the voice turn in silence, which is the
    worst possible outcome for something the user asked for out loud.
    """
    args = dict(args or {})
    verb = str(args.get("action") or "").strip().lower()
    if verb not in _VERBS:
        return _result(_VERB_HELP, reason="unknown_action")
    if not _allowed(enabled):
        return _result("The connector is switched off. Turn it on under "
                       "Settings ▸ PERMISSIONS, the External apps switch, "
                       "and ask me again.",
                       reason="disabled", switch=FEATURE)
    try:
        return _VERBS[verb](args, on_event)
    except Exception as exc:
        return _result(f"The connector failed to run that: "
                       f"{type(exc).__name__}: {exc}", reason="crashed")


def parameters() -> dict:
    """The OBJECT schema, in the same dialect capabilities/registry.py emits.

    Flat superset, one required key: the Live function-declaration dialect cannot
    express per-action parameter subsets, so every verb sees every field and each
    verb reads only the ones it documents. Fields no verb uses are simply absent
    from PARAMS, so the schema cannot drift ahead of the code.
    """
    props: dict[str, dict] = {
        "action": {"type": "STRING",
                   "description": ("The operation. One of: "
                                   + ", ".join(VERBS) + "."),
                   "enum": list(VERBS)},
    }
    for key in ("app", "tool", "query", "account", "arguments"):
        kind, help_text = PARAMS[key]
        props[key] = {"type": kind, "description": help_text}
    return {"type": "OBJECT", "properties": props, "required": ["action"]}


#: The declaration main.py appends to TOOL_DECLARATIONS. The description is one
#: implicit string concatenation and never a tuple: a stray comma here would make
#: the Live setup message reject every connect, which is exactly the failure
#: _sanitize_tool_declarations exists to catch — and a tool that repairs itself is
#: one that was written wrong.
DECLARATION = {
    "name": "connector",
    "description": (
        "The single route to every external app J.A.R.V.I.S. talks to — Spotify, "
        "Gmail, GitHub, Telegram, Slack, Calendar, Notion and anything else "
        "Composio offers — through one connector instead of one integration per "
        "app. Actions: status (is the connector live, and what is connected), "
        "list (with 'app': everything that app can do, straight from Composio's "
        "own catalog; without it: which apps are connected and how many more are "
        "offered), connect (bring back the authorization link for one app), "
        "verify (check a connection the user just finished in the browser, using "
        "the reference connect returned), run (execute one tool slug with "
        "'arguments'), disconnect (revoke one app). Use it for 'connect my "
        "Gmail', 'what can you do with Spotify', 'play something on Spotify', "
        "'who am I connected to', 'disconnect Telegram'. Discover before acting: "
        "list an app to get the exact tool slug and its arguments, then run it — "
        "never invent a slug from memory, and never assume an app exists when "
        "status says Composio does not offer it. Credentials belong to Composio: "
        "never ask the user for a password or an access token, and never repeat "
        "a link or a token back to them. Report only what the result says — do "
        "not claim an app is connected, a message was sent or a track played "
        "unless the result confirms it, and when a connection needs "
        "reauthorizing, hand the user the link the result returned instead of "
        "retrying the action."
    ),
    "parameters": parameters(),
}


__all__ = ["DECLARATION", "FEATURE", "PARAMS", "VERBS", "handle_connector",
           "parameters"]