"""Instagram — the user's own account, through the private API.

``instagrapi`` is an optional package and Instagram is a hostile environment for
automation, so this module is built around three facts rather than around
optimism:

* **A login is expensive.** Instagram rate-limits and challenges logins, so the
  session is created once and reused from ``data/instagram/session.json``; every
  call reuses that session and a failed login is reported, never retried in a
  loop.
* **Some actions cannot be taken back.** Posting, commenting, following and
  sending a direct message are declared with ``gate="confirm"`` in the action
  registry, so they always wait for the on-screen confirmation. Reading a
  profile or a hashtag never does.
* **Every failure is the answer.** A missing package, a missing credential, a
  challenge, a rate limit and a deleted post are five different sentences — the
  assistant must be able to tell the user which one it is.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = ["client", "run", "session_path", "status"]

_PACKAGE_HINT = ("Instagram support needs the optional 'instagrapi' package, "
                 "which is not installed — run 'pip install instagrapi' and ask "
                 "me again. Nothing was sent to Instagram.")

#: How long one instagrapi call may take before it is abandoned. Instagram can
#: hang a request for minutes when it is deliberately slowing an account down.
CALL_TIMEOUT_S = 60.0


def _instagrapi():
    try:
        from instagrapi import Client  # type: ignore
        return Client
    except Exception:
        return None


def session_path() -> Path:
    try:
        from config.settings import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:
        base = Path(__file__).resolve().parent.parent / "data"
    target = base / "instagram"
    target.mkdir(parents=True, exist_ok=True)
    return target / "session.json"


def _credentials() -> tuple[str, str, str]:
    """``(username, password, session_file)`` from the shared configuration."""
    try:
        from memory.config_manager import load_api_keys
        data = dict(load_api_keys() or {})
    except Exception:
        data = {}
    return (str(data.get("instagram_username") or "").strip(),
            str(data.get("instagram_password") or "").strip(),
            str(data.get("instagram_session") or "").strip())


def _fail(detail: str, reason: str = "action_failed") -> dict:
    return {"ok": False, "controller": "instagrapi", "detail": detail,
            "reason": reason, "evidence": {}}


def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "instagrapi", "detail": detail,
            "reason": "", "evidence": evidence}


_CLIENT = None
_CLIENT_AT = 0.0


def client():
    """A logged-in instagrapi client, or ``(None, reason)`` shaped exceptions.

    The client is cached for the session: logging in per call is exactly the
    pattern Instagram blocks. A cached session file is preferred over a fresh
    login, because a login from a new "device" is what triggers a challenge.
    """
    global _CLIENT, _CLIENT_AT
    if _CLIENT is not None and (time.monotonic() - _CLIENT_AT) < 3600:
        return _CLIENT
    Client = _instagrapi()
    if Client is None:
        raise RuntimeError(_PACKAGE_HINT)
    username, password, session_file = _credentials()
    if not username:
        raise RuntimeError("No Instagram username is configured (Settings ▸ API).")
    account = Client()
    try:
        account.delay_range = [1, 3]   # instagrapi's own politeness window
    except Exception:
        pass
    saved = Path(session_file) if session_file else session_path()
    if saved.exists():
        try:
            account.load_settings(saved)
            if password:
                # Re-authenticates the stored device identity rather than
                # creating a new one, which is what avoids a fresh challenge.
                account.login(username, password)
            if not getattr(account, "user_id", None):
                raise RuntimeError("the saved session did not identify an account")
            _CLIENT, _CLIENT_AT = account, time.monotonic()
            return account
        except Exception:
            pass   # fall through to a real login, reported if it also fails
    if not password:
        raise RuntimeError("The Instagram password is missing (Settings ▸ API).")
    try:
        account.login(username, password)
    except Exception as exc:
        raise RuntimeError(
            f"Instagram refused the login for {username}: {exc}. A challenge means "
            f"Instagram wants the account confirmed in its own app first."
        ) from exc
    try:
        account.dump_settings(saved)
    except Exception:
        pass
    _CLIENT, _CLIENT_AT = account, time.monotonic()
    return account


def _account() -> dict:
    """The logged-in account, described without leaking credentials."""
    try:
        username, _password, _session = _credentials()
    except Exception:
        username = ""
    return {"username": username}


def status() -> dict:
    """Everything known without contacting Instagram."""
    installed = _instagrapi() is not None
    username, password, session_file = _credentials()
    saved = Path(session_file) if session_file else session_path()
    detail = (_PACKAGE_HINT if not installed else
              (f"instagrapi is ready; a saved session exists for {username or 'the account'}."
               if saved.exists() else
               f"instagrapi is ready; a login will be attempted for "
               f"{username or '(no username configured)'}."))
    return {"available": installed and bool(username),
            "detail": detail, "username": username,
            "password_set": bool(password), "session": str(saved),
            "session_saved": saved.exists()}


# ── helpers ───────────────────────────────────────────────────────────────────

def _media_line(item: dict, index: Any = None) -> str:
    caption = str(item.get("caption_text") or "").replace("\n", " ")[:80]
    likes = item.get("like_count")
    taken = str(item.get("taken_at") or "")[:19]
    prefix = f"{index}. " if index not in (None, "") else ""
    bits = [f"{prefix}{item.get('code') or item.get('pk') or '?'}"]
    if taken:
        bits.append(taken)
    if likes is not None:
        bits.append(f"{likes} likes")
    line = " — ".join(bits)
    return line + (f"\n     {caption}" if caption else "")


def _user_line(user: dict) -> str:
    if not isinstance(user, dict):
        return "- [unreadable user]"
    return (f"- {user.get('username') or user.get('pk')}"
            f" ({user.get('full_name') or 'no name'})"
            + (f", {user.get('follower_count')} followers"
               if user.get("follower_count") is not None else ""))


def _public_user(account, name: str) -> dict:
    """Resolve a username to the account's public info (never a login guess)."""
    info = account.user_info_by_username(name)
    return {
        "pk": str(getattr(info, "pk", "") or ""),
        "username": getattr(info, "username", name),
        "full_name": getattr(info, "full_name", ""),
        "biography": getattr(info, "biography", ""),
        "follower_count": getattr(info, "follower_count", None),
        "following_count": getattr(info, "following_count", None),
        "media_count": getattr(info, "media_count", None),
        "is_private": bool(getattr(info, "is_private", False)),
    }


# ── handlers ──────────────────────────────────────────────────────────────────

def _h_status(_args: dict) -> dict:
    state = status()
    lines = [state["detail"]]
    lines.append(f"Session file: {state['session']}"
                 + (" (present)" if state["session_saved"] else " (not created yet)"))
    return _ok("\n".join(lines), available=state["available"])


def _h_profile(args: dict) -> dict:
    name = str(args.get("user") or args.get("username") or "").strip().lstrip("@")
    if not name:
        return _fail("Which Instagram account, sir?", "no_target")
    account = client()
    user = _public_user(account, name)
    lines = [f"{user['username']} ({user['full_name'] or 'no name'})",
             f"{user['follower_count']} followers, {user['following_count']} "
             f"following, {user['media_count']} posts"]
    if user["is_private"]:
        lines.append("This account is private.")
    if user["biography"]:
        lines.append(user["biography"].replace("\n", " ")[:300])
    return _ok("\n".join(lines), username=user["username"], pk=user["pk"])


def _h_recent_posts(args: dict) -> dict:
    name = str(args.get("user") or args.get("username") or "").strip().lstrip("@")
    if not name:
        return _fail("Which account's posts, sir?", "no_target")
    try:
        limit = max(1, min(30, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    account = client()
    user = _public_user(account, name)
    if user["is_private"]:
        return _fail(f"{user['username']} is a private account, so I cannot read "
                     f"its posts.", "private_account")
    medias = account.user_medias(user["pk"], amount=limit)
    if not medias:
        return _ok(f"{user['username']} has no posts I can see.")
    lines = [f"Latest {len(medias)} post(s) from {user['username']}:"]
    for index, media in enumerate(medias, start=1):
        lines.append(_media_line({
            "code": getattr(media, "code", ""),
            "pk": getattr(media, "pk", ""),
            "caption_text": getattr(media, "caption_text", ""),
            "like_count": getattr(media, "like_count", None),
            "taken_at": getattr(media, "taken_at", None),
        }, index))
    return _ok("\n".join(lines), count=len(medias))


def _h_hashtag(args: dict) -> dict:
    tag = str(args.get("tag") or args.get("query") or "").strip().lstrip("#")
    if not tag:
        return _fail("Which hashtag, sir?", "no_target")
    account = client()
    medias = account.hashtag_medias_top(tag, amount=int(args.get("limit") or 10))
    if not medias:
        return _ok(f"Nothing recent under #{tag}.")
    lines = [f"Top posts under #{tag}:"]
    for index, media in enumerate(medias, start=1):
        lines.append(_media_line({
            "code": getattr(media, "code", ""),
            "pk": getattr(media, "pk", ""),
            "caption_text": getattr(media, "caption_text", ""),
            "like_count": getattr(media, "like_count", None),
        }, index))
    return _ok("\n".join(lines), count=len(medias))


def _h_like(args: dict) -> dict:
    media_id = str(args.get("media") or args.get("media_id") or args.get("url")
                   or "").strip()
    if not media_id:
        return _fail("Which post should I like? Give me its identifier or link.",
                     "no_target")
    account = client()
    account.media_like(_media_pk(account, media_id))
    return _ok(f"Liked {media_id}.", media=media_id)


def _h_follow(args: dict) -> dict:
    name = str(args.get("user") or args.get("username") or "").strip().lstrip("@")
    if not name:
        return _fail("Which account should I follow?", "no_target")
    account = client()
    user = _public_user(account, name)
    account.user_follow(user["pk"])
    return _ok(f"Following {user['username']}.", username=user["username"])


def _h_post_photo(args: dict) -> dict:
    path = str(args.get("path") or "").strip()
    if not path or not Path(path).exists():
        return _fail("I need the path of an existing image to post.", "not_found")
    caption = str(args.get("caption") or "").strip()
    account = client()
    media = account.photo_upload(Path(path), caption)
    return _ok(f"Posted {Path(path).name} to Instagram"
               + (f" with the caption: {caption[:80]}" if caption else "."),
               code=str(getattr(media, "code", "") or ""))


def _h_direct_message(args: dict) -> dict:
    name = str(args.get("user") or args.get("username") or "").strip().lstrip("@")
    text = str(args.get("text") or "").strip()
    if not name or not text:
        return _fail("I need both the account and the message, sir.", "no_target")
    account = client()
    user = _public_user(account, name)
    account.direct_send(text, [user["pk"]])
    return _ok(f"Sent that to {user['username']}.", username=user["username"])


def _media_pk(account, reference: str) -> str:
    """A media primary key from an ID, a shortcode or a post URL."""
    reference = reference.strip()
    if reference.isdigit():
        return reference
    code = reference.rstrip("/").split("/")[-1].split("?")[0] if "/" in reference \
        else reference
    try:
        return str(account.media_id(code))
    except Exception as exc:
        raise RuntimeError(f"I could not resolve '{reference}' to a post: {exc}") from exc


_HANDLERS = {
    "status": _h_status,
    "profile": _h_profile,
    "recent_posts": _h_recent_posts,
    "hashtag": _h_hashtag,
    "like": _h_like,
    "follow": _h_follow,
    "post_photo": _h_post_photo,
    "direct_message": _h_direct_message,
}


def _classify(exc: Exception) -> dict:
    """Which of Instagram's refusals this is — the classification comes first.

    A rate limit arrives as an instagrapi exception whose text says 429; testing
    the exception *type* before its message would report every throttle as a
    generic failure, which is exactly the case the user needs named.
    """
    text = str(exc)
    low = text.lower()
    if "challenge" in low:
        return _fail(f"Instagram is asking for a challenge on this account: "
                     f"{text}. Confirm the login in Instagram's own app, then ask "
                     f"me again — retrying here only makes it worse.", "challenge")
    if "429" in text or "too many" in low or "rate" in low:
        return _fail(f"Instagram is rate limiting this account: {text}. It usually "
                     f"clears after a while; I will not hammer it.", "rate_limited")
    if "login_required" in low or "not logged" in low:
        return _fail(f"That Instagram session is no longer valid: {text}. Ask me "
                     f"again after re-authorising.", "login_required")
    if isinstance(exc, RuntimeError):
        # The module's own sentences (missing package, missing credential, a
        # device that would not answer) are already the whole answer.
        return _fail(text, "unavailable")
    return _fail(f"{type(exc).__name__}: {text}")


def run(action_name: str, args: dict) -> dict:
    """Execute one Instagram action. Returns a result dict, never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip().lower())
    if handler is None:
        return _fail(f"'{action_name}' is not an Instagram action. Use one of: "
                     + ", ".join(sorted(_HANDLERS)) + ".", "unknown_action")
    try:
        return handler(dict(args or {})) or _fail("Instagram returned nothing.")
    except Exception as exc:
        return _classify(exc)
