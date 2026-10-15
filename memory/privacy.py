"""Privacy helpers for the brain memory system.

* Never store obvious secrets as permanent memory unless explicitly OK'd.
* Support privacy levels on memories (normal / personal / sensitive).
* `redact()` scrubs secret-looking tokens from content before storage so the
  database never silently accumulates passwords or API keys.

This is a best-effort guard; JARVIS still prompts for consent before storing
content the user tags as private.
"""

from __future__ import annotations

import re

# Patterns that look like credentials. Conservative: a plain word that merely
# *contains* 'key' is not redacted - only high-entropy or labelled secrets are.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}", re.I),                    # OpenAI-style
    re.compile(r"AIza[0-9A-Za-z_-]{20,}", re.I),                   # Google API key
    re.compile(r"ghp_[0-9A-Za-z]{20,}", re.I),                     # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}", re.I),                         # AWS access key
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}", re.I),             # Slack token
    re.compile(r"eyJ[0-9A-Za-z_-]{20,}\.[0-9A-Za-z_-]{10,}", re.I),# JWT
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b\s*[:=]\s*\S{6,}"),
]

_SENSITIVE_WORDS = (
    "password", "passwd", "api key", "api_key", "secret", "private key",
    "bank account", "credit card", "social security",
)


def looks_like_secret(text: str) -> bool:
    """True when `text` contains a token that should not be stored verbatim."""
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def redact(text: str) -> str:
    """Replace credential-looking substrings with a placeholder."""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def sensitivity(text: str) -> str:
    """Guess the privacy level of `text`: normal / personal / sensitive."""
    low = (text or "").lower()
    if looks_like_secret(text):
        return "sensitive"
    if any(w in low for w in _SENSITIVE_WORDS):
        return "sensitive"
    return "normal"


def is_private(text: str, default_level: str = "normal") -> bool:
    """Honour the configured default: 'sensitive' default makes anything with a
    sensitive marker (or a secret) require explicit consent to store."""
    if sensitivity(text) == "sensitive":
        return True
    if default_level == "sensitive" and _implies_personal(text):
        return True
    return False


def _implies_personal(text: str) -> bool:
    low = (text or "").lower()
    personal = (
        "my phone number", "my address", "my email", "my ssn",
        "my social", "my bank", "my card", "my id",
    )
    return any(p in low for p in personal)