"""Display metadata for Composio toolkits — presentation, never truth.

The authoritative list of what J.A.R.V.I.S. can connect to is the Composio
catalog at runtime (``manager.list_toolkits()``). This module answers one
presentational question only: *how should this app look?* — a brand colour, the
initials drawn inside the badge, and a clean label.

An app missing from the table is neither a fake integration nor an error: it gets
a neutral badge built from the name Composio returned. That is exactly why the
table is not a whitelist.

Badges are drawn natively (rounded square + brand colour + initials) rather than
shipped as image files, so the Connector page needs no network fetch and no icon
font to look right on a cold start.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["asset_path", "badge_colors", "initials_for", "label_for", "lookup",
           "meta"]

#: slug → (label, brand colour, initials)
_CATALOG: dict[str, tuple[str, str, str]] = {
    # music · media
    "spotify": ("Spotify", "#1DB954", "SP"),
    "youtube": ("YouTube", "#FF0033", "YT"),
    "youtube_music": ("YouTube Music", "#FF0033", "YM"),
    "apple_music": ("Apple Music", "#FA243C", "AM"),
    "soundcloud": ("SoundCloud", "#FF5500", "SC"),
    # messaging · social
    "telegram": ("Telegram", "#2AABEE", "TG"),
    "whatsapp": ("WhatsApp", "#25D366", "WA"),
    "discord": ("Discord", "#5865F2", "DC"),
    "slack": ("Slack", "#611F69", "SL"),
    "signal": ("Signal", "#3A76F0", "SG"),
    "messenger": ("Messenger", "#0084FF", "MS"),
    "facebook": ("Facebook", "#1877F2", "FB"),
    "instagram": ("Instagram", "#E1306C", "IG"),
    "x": ("X", "#1A1A1A", "X"),
    "twitter": ("X", "#1A1A1A", "X"),
    "linkedin": ("LinkedIn", "#0A66C2", "LI"),
    "tiktok": ("TikTok", "#00F2EA", "TT"),
    "reddit": ("Reddit", "#FF4500", "RD"),
    "threads": ("Threads", "#1A1A1A", "TH"),
    "bluesky": ("Bluesky", "#0085FF", "BS"),
    "mastodon": ("Mastodon", "#6364FF", "MA"),
    "twilio": ("Twilio", "#F22F46", "TW"),
    # mail · calendar · docs
    "gmail": ("Gmail", "#EA4335", "GM"),
    "outlook": ("Outlook", "#0A66C2", "OL"),
    "email": ("Email", "#5B6B7A", "EM"),
    "sendgrid": ("SendGrid", "#1A82E2", "SG"),
    "google_calendar": ("Google Calendar", "#4285F4", "GC"),
    "outlook_calendar": ("Outlook Calendar", "#0A66C2", "OC"),
    "calcom": ("Cal.com", "#2F2F2F", "CC"),
    "googledrive": ("Google Drive", "#1FA463", "GD"),
    "dropbox": ("Dropbox", "#0061FF", "DB"),
    "onedrive": ("OneDrive", "#0364D8", "OD"),
    "google_docs": ("Google Docs", "#4285F4", "DO"),
    "google_sheets": ("Google Sheets", "#0F9D58", "SH"),
    "google_slides": ("Google Slides", "#F4B400", "GS"),
    "notion": ("Notion", "#1A1A1A", "NO"),
    "confluence": ("Confluence", "#172B4D", "CF"),
    "obsidian": ("Obsidian", "#7C68EE", "OB"),
    # code · product
    "github": ("GitHub", "#1A1A1A", "GH"),
    "gitlab": ("GitLab", "#FC6D26", "GL"),
    "bitbucket": ("Bitbucket", "#0052CC", "BB"),
    "jira": ("Jira", "#0052CC", "JI"),
    "linear": ("Linear", "#5E6AD2", "LN"),
    "asana": ("Asana", "#F06A6A", "AS"),
    "trello": ("Trello", "#0079BF", "TR"),
    "clickup": ("ClickUp", "#7B68EE", "CU"),
    "figma": ("Figma", "#A259FF", "FG"),
    "vercel": ("Vercel", "#1A1A1A", "VC"),
    "sentry": ("Sentry", "#362D59", "SE"),
    # business · data · meetings
    "hubspot": ("HubSpot", "#FF7A59", "HS"),
    "salesforce": ("Salesforce", "#0A66C2", "SF"),
    "stripe": ("Stripe", "#635BFF", "ST"),
    "paypal": ("PayPal", "#0070BA", "PP"),
    "shopify": ("Shopify", "#95BF47", "SP"),
    "quickbooks": ("QuickBooks", "#2CA01C", "QB"),
    "xero": ("Xero", "#13B5EA", "XE"),
    "airtable": ("Airtable", "#FCB400", "AT"),
    "postgres": ("PostgreSQL", "#336791", "PG"),
    "supabase": ("Supabase", "#3ECF8E", "SB"),
    "mongodb": ("MongoDB", "#47A248", "MO"),
    "zoom": ("Zoom", "#2D8CFF", "ZM"),
    "google_meet": ("Google Meet", "#00897B", "MT"),
}

#: One spelling of a slug finds one badge, whatever the catalog calls it.
_ALIASES = {
    "google-mail": "gmail", "googlemail": "gmail",
    "google-calendar": "google_calendar", "googlecalendar": "google_calendar",
    "google-drive": "googledrive", "google-docs": "google_docs",
    "google-sheets": "google_sheets", "google-slides": "google_slides",
    "google-meet": "google_meet", "microsoft-outlook": "outlook",
    "apple-music": "apple_music", "youtube-music": "youtube_music",
    "cal-com": "calcom", "x-twitter": "x",
}

#: Used when Composio returns an app this table has never seen. Chosen from the
#: slug, so the same app always gets the same badge.
_FALLBACK = ("#3E7BFA", "#12B886", "#F76707", "#AE3EC9", "#F03E3E",
             "#0CA678", "#4263EB", "#E8590C", "#7048E8", "#1098AD")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _normalize(slug: str) -> str:
    text = str(slug or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_").replace(".", "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def lookup(slug: str) -> tuple[str, str, str] | None:
    """``(label, colour, initials)`` for a toolkit slug, or None."""
    key = _normalize(slug)
    if not key:
        return None
    key = _ALIASES.get(key.replace("_", "-"), key)
    return _CATALOG.get(key) or _CATALOG.get(key.replace("_", ""))


def label_for(slug: str, name: str = "") -> str:
    """The app's display name: the table's, else what Composio sent."""
    hit = lookup(slug)
    if hit:
        return hit[0]
    clean = " ".join(str(name or "").split())
    if clean:
        return clean
    return (str(slug or "").replace("_", " ").replace("-", " ").strip().title()
            or "App")


def initials_for(slug: str, name: str = "") -> str:
    """Up to two letters for the badge, derived from the label when unknown."""
    hit = lookup(slug)
    if hit:
        return hit[2]
    words = [w for w in label_for(slug, name).split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def badge_colors(slug: str) -> tuple[str, str]:
    """``(foreground, background)`` — the brand colour, or a stable fallback."""
    hit = lookup(slug)
    if hit:
        return ("#FFFFFF", hit[1])
    index = sum(ord(ch) for ch in _normalize(slug)) % len(_FALLBACK)
    return ("#FFFFFF", _FALLBACK[index])


def asset_path(slug: str) -> str:
    """A bundled icon for this app, when the project ships one ('' otherwise).

    Checked on disk rather than assumed: ``assets/icons/<slug>.png`` is optional,
    and a missing file is the normal case, not a failure.
    """
    key = _normalize(slug)
    if not key:
        return ""
    base = _PROJECT_ROOT / "assets" / "icons"
    for suffix in (".png", ".svg", ".ico", ".webp"):
        try:
            candidate = base / f"{key}{suffix}"
            if candidate.is_file():
                return str(candidate)
        except OSError:
            return ""
    return ""


def meta(slug: str, name: str = "") -> dict:
    """Everything the Connector page needs to draw one app.

    ``known`` says whether the app was in the local table — a styling hint, never
    a statement that the app exists in Composio.
    """
    foreground, background = badge_colors(slug)
    return {"slug": _normalize(slug), "label": label_for(slug, name),
            "initials": initials_for(slug, name), "color": background,
            "foreground": foreground, "asset": asset_path(slug),
            "known": lookup(slug) is not None}