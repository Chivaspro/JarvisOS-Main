"""The Centralized API Hub — one home for every external service link.

The Settings ▸ API tab used to be a wall of key fields with no way to find out
whether a key actually works. This module is the single place that knows what
each service needs and how to test it, so the tab can show a real state per
service (ready / key missing / unavailable / switched off) and a TEST button
that performs a genuine, tiny round trip.

Two rules hold everywhere in here:

* **A probe measures, it never guesses.** Every answer is derived from a real
  call (an HTTP status, an import, a file on disk, a TCP connect) or from the
  absence of a credential. "Not configured" is never dressed up as "working".
* **A probe is cheap and bounded.** Short timeouts, one request, no retries, no
  data written. A probe that cannot reach the internet fails with the reason it
  was given instead of hanging the Settings window.

Nothing here holds a conversation, plans work or touches a tool: the callers are
the Settings window (status pill) and the ``service_hub`` tool (the assistant
answering "is my Spotify link working?").
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "SERVICES", "PROVIDERS", "Service", "ProviderKey", "describe", "probe",
    "probe_target", "status_all", "row_status", "key_targets", "status_targets",
    "summary", "provider_summary",
]

#: Root of the project (…/services/hub.py → …).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: How long a single probe may take before it is reported as a timeout. Kept
#: short on purpose: the Settings window must stay responsive.
DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class Service:
    """One external link: what it is, what it needs, and how to test it."""

    id: str
    label: str
    summary: str
    #: Settings switch that must be on for the service to be usable ("" = none).
    switch: str = ""
    #: Config fields the service reads, in the order the UI shows them.
    #: ``(field_name, label, secret, optional)``.
    fields: tuple[tuple[str, str, bool, bool], ...] = ()
    #: Anything worth telling the user before they press TEST.
    note: str = ""
    probe_name: str = ""
    #: Where the credential comes from: the page that hands it out, the page
    #: that explains it, and the one-line "how to get it" the UI shows on the
    #: GET API KEY button. Empty ``key_url`` means there is nothing to fetch —
    #: the value is not a key (a device password, a self-generated secret) or
    #: the service needs none at all (weather, radar).
    key_url: str = ""
    help_url: str = ""
    key_help: str = ""
    #: Which block of the Settings window these credentials belong to.
    group: str = "Services"


@dataclass(frozen=True)
class ProviderKey:
    """A credential that is part of a *pipeline* rather than a link of its own.

    The AI, voice and optional-memory keys each feed a provider the user picks
    elsewhere, so they have no state to report beyond "present, and does the
    provider accept it" — which is exactly what a row here offers: where to get
    the key, and one real request that answers whether it works.

    ``probe_name`` of "" is honest, not lazy: Mem0 has no safe read-only endpoint
    to spend a key on, so the row says the key is present and that the first
    real use is the actual test.
    """

    id: str
    label: str
    field: str
    summary: str
    group: str = "AI"
    secret: bool = True
    optional: bool = False
    key_url: str = ""
    help_url: str = ""
    key_help: str = ""
    probe_name: str = ""


#: Every service the hub knows how to describe and test.
SERVICES: tuple[Service, ...] = (
    Service(
        id="gemini",
        label="Google Gemini",
        summary="The realtime voice session, the AI agent and UI-TARS vision all "
                "run on this key.",
        fields=(("gemini_api_key", "Gemini API key", True, False),),
        probe_name="_probe_gemini",
        key_url="https://aistudio.google.com/app/apikey",
        help_url="https://ai.google.dev/gemini-api/docs/api-key",
        key_help="Google AI Studio ▸ Get API key ▸ Create API key. The free tier "
                 "covers the realtime voice session; a card is only needed for "
                 "higher limits.",
        group="AI",
    ),
    Service(
        id="openrouter",
        label="OpenRouter",
        summary="One key for hundreds of models, including free tiers — used by "
                "the Skill Crucible and for text replies.",
        fields=(("openrouter_api_key", "OpenRouter API key", True, False),),
        note="Free models are listed without a key; a key raises the rate limits.",
        probe_name="_probe_openrouter",
        key_url="https://openrouter.ai/keys",
        help_url="https://openrouter.ai/docs/quickstart",
        key_help="OpenRouter ▸ Keys ▸ Create key. Copy it once — it is only shown "
                 "in full at creation.",
        group="AI",
    ),
    Service(
        id="anthropic",
        label="Anthropic",
        summary="Claude models, used as a fallback text provider when Gemini is "
                "unavailable.",
        fields=(("anthropic_api_key", "Anthropic API key", True, True),),
        probe_name="_probe_anthropic",
        key_url="https://console.anthropic.com/settings/keys",
        help_url="https://docs.anthropic.com/en/docs/get-started",
        key_help="Anthropic Console ▸ Settings ▸ API keys ▸ Create key.",
        group="AI",
    ),
    Service(
        id="spotify",
        label="Spotify",
        summary="Playback control through the official Spotify MCP server, hosted "
                "locally over stdio.",
        switch="spotify",
        # Both are required: without an app's client ID and secret there is
        # nothing to authorise against, so a blank field is a missing key and
        # not an optional one.
        fields=(("spotify_client_id", "Spotify client ID", False, False),
                ("spotify_client_secret", "Spotify client secret", True, False)),
        note="Needs Node 18+ and a Spotify Premium account. The first run opens a "
             "browser once to authorise the app.",
        probe_name="_probe_spotify",
        key_url="https://developer.spotify.com/dashboard",
        help_url="https://developer.spotify.com/documentation/web-api/concepts/apps",
        key_help="Spotify for Developers ▸ Dashboard ▸ Create app. Copy the Client "
                 "ID from the app, then 'View client secret'.",
    ),
    Service(
        id="weather",
        label="Weather",
        summary="Forecasts and current conditions; the open-meteo endpoint needs "
                "no account at all.",
        fields=(("weather_api_key", "Weather API key (optional)", True, True),),
        note="Works out of the box on the keyless public endpoint.",
        probe_name="_probe_weather",
        key_help="Nothing to fetch: the keyless open-meteo endpoint is used. A key "
                 "here is only for a private endpoint you host yourself.",
    ),
    Service(
        id="radar",
        label="Rain radar",
        summary="Live precipitation radar tiles for the globe and weather reports.",
        fields=(("radar_api_key", "Radar API key (optional)", True, True),),
        probe_name="_probe_radar",
        key_help="Nothing to fetch: the public RainViewer tiles are keyless.",
    ),
    Service(
        id="instagram",
        label="Instagram",
        summary="Read and act on the user's own Instagram account through the "
                "private API.",
        switch="instagram",
        fields=(("instagram_username", "Instagram username", False, True),
                ("instagram_password", "Instagram password", True, True)),
        # A saved session is an alternative to the password, which is why these
        # two stay optional and the probe checks for either.
        note="Uses the optional instagrapi package. Login is rate-limited by "
             "Instagram; a session file is reused once created.",
        probe_name="_probe_instagram",
        help_url="https://subzeroid.github.io/instagrapi/",
        key_help="There is no key page: this is your own account login. Instagram "
                 "rate-limits sign-ins, so the first request may take a moment.",
    ),
    Service(
        id="smart_home",
        label="Smart home",
        summary="Local Kasa/Tapo plugs and bulbs over the LAN — no cloud account.",
        switch="smart_home",
        fields=(("smart_home_hosts", "Device addresses (comma separated, optional)",
                 False, True),
                ("smart_home_username", "Device username (optional)", False, True),
                ("smart_home_password", "Device password", True, True)),
        note="Uses the optional python-kasa package and talks to devices on this "
             "network only.",
        probe_name="_probe_smart_home",
        help_url="https://python-kasa.readthedocs.io/en/stable/",
        key_help="No key page: these are your own devices. Use the Kasa/Tapo "
                 "account e-mail and password, or a device password set in the app.",
    ),
    Service(
        id="connect",
        label="JarvisOS Connect",
        summary="The desktop gateway that lets a paired phone or browser reach "
                "this machine.",
        switch="connect",
        fields=(("connect_pairing_secret", "Pairing secret", True, True),),
        note="Pairing is refused until a secret is set.",
        probe_name="_probe_connect",
        key_help="Nothing to fetch: invent a long random string and type the same "
                 "one on the phone. It is the only thing that may pair.",
    ),
    Service(
        id="composio",
        label="External apps (Composio)",
        summary="One connector for Spotify, Gmail, GitHub, Telegram, Slack and "
                "every other app the assistant reaches through Composio, instead "
                "of one integration per app.",
        # Its own switch. NOT "connect", which is the JarvisOS phone-pairing
        # gateway's flag and is read in eight places; sharing it would let
        # revoking phone pairing silently kill every external account.
        switch="external_apps",
        fields=(("composio_api_key", "Composio project API key", True, False),),
        note="Needs the optional 'composio' package (install it from "
             "setup_jarvis --install-missing). The key is a project key, never an "
             "account password, and it is stored in config/api_keys.json.",
        probe_name="_probe_composio",
        key_url="https://app.composio.dev/settings/api-keys",
        help_url="https://docs.composio.dev",
        key_help="Composio dashboard ▸ Settings ▸ API keys ▸ Create. Copy the "
                 "project key — it starts with ak_.",
    ),
    Service(
        id="documents",
        label="Document engines",
        summary="Word, Excel, PowerPoint and PDF writers used by the document "
                "architect.",
        switch="document_architect",
        fields=(),
        probe_name="_probe_documents",
    ),
)

#: The credentials that belong to a provider the user picks elsewhere. They are
#: rows in Settings like a service credential, but they are not links with a
#: state of their own: the provider is only exercised when the pipeline uses it.
PROVIDERS: tuple[ProviderKey, ...] = (
    ProviderKey(
        id="openai", label="OpenAI", field="openai_api_key",
        summary="OpenAI models, selectable as a text provider on the AI tab.",
        key_url="https://platform.openai.com/api-keys",
        help_url="https://platform.openai.com/docs/overview",
        key_help="OpenAI platform ▸ API keys ▸ Create new secret key. The key is "
                 "shown once, so paste it straight away.",
        probe_name="_probe_openai",
    ),
    ProviderKey(
        id="groq", label="Groq", field="groq_api_key",
        summary="Very fast hosted models, used when a provider is set to Groq.",
        key_url="https://console.groq.com/keys",
        help_url="https://console.groq.com/docs/quickstart",
        key_help="Groq Console ▸ API keys ▸ Create API key.",
        probe_name="_probe_groq",
    ),
    ProviderKey(
        id="custom_ai", label="Custom provider", field="custom_ai_api_key",
        summary="The key for the OpenAI-compatible endpoint configured on the AI "
                "tab.",
        optional=True,
        key_help="No fixed page: it comes from whichever OpenAI-compatible "
                 "service you configured on the AI tab (token, gateway, or a "
                 "local server that needs none).",
    ),
    ProviderKey(
        id="fish_audio", label="Fish Audio", field="fish_audio_api_key",
        summary="Cloud voice provider (Settings ▸ Voice ▸ Fish Audio).",
        group="VOICE",
        key_url="https://fish.audio/go-api/",
        help_url="https://docs.fish.audio/",
        key_help="Fish Audio ▸ API. Copy the key from the dashboard's API section.",
        probe_name="_probe_fish_audio",
    ),
    ProviderKey(
        id="elevenlabs", label="ElevenLabs", field="elevenlabs_api_key",
        summary="Cloud voice provider (Settings ▸ Voice ▸ ElevenLabs).",
        group="VOICE",
        key_url="https://elevenlabs.io/app/settings/api-keys",
        help_url="https://elevenlabs.io/docs",
        key_help="ElevenLabs ▸ Settings ▸ API keys ▸ Create API key.",
        probe_name="_probe_elevenlabs",
    ),
    ProviderKey(
        id="mem0", label="Mem0", field="mem0_api_key",
        summary="Cloud long-term memory, used only while its switch is on.",
        group="MEMORY", optional=True,
        key_url="https://app.mem0.ai/dashboard/api-keys",
        help_url="https://docs.mem0.ai/",
        key_help="Mem0 dashboard ▸ API keys ▸ Create key. The MEM0_API_KEY "
                 "environment variable works as well.",
        probe_name="_probe_mem0",
    ),
    ProviderKey(
        id="qdrant_url", label="Qdrant server URL", field="qdrant_url",
        summary="The Qdrant instance the knowledge base reads from.",
        group="MEMORY", optional=True, secret=False,
        help_url="https://qdrant.tech/documentation/quickstart/",
        key_help="Run the local server (docker run -p 6333:6333 qdrant/qdrant) or "
                 "copy the cluster URL from Qdrant Cloud. Blank means "
                 "http://localhost:6333.",
        probe_name="_probe_qdrant",
    ),
    ProviderKey(
        id="qdrant_api_key", label="Qdrant API key", field="qdrant_api_key",
        summary="Only needed for a Qdrant Cloud cluster; a local server has none.",
        group="MEMORY", optional=True,
        key_url="https://cloud.qdrant.io/",
        help_url="https://qdrant.tech/documentation/cloud/authentication/",
        key_help="Qdrant Cloud ▸ Cluster ▸ Data access control ▸ API keys.",
        probe_name="_probe_qdrant",
    ),
)

_BY_ID = {service.id: service for service in SERVICES}
_PROVIDER_BY_ID = {provider.id: provider for provider in PROVIDERS}
_BY_FIELD = {}
for _service in SERVICES:
    for _name, _label, _secret, _optional in _service.fields:
        _BY_FIELD[_name] = {
            "field": _name, "label": _label, "secret": _secret,
            "optional": _optional, "group": _service.group,
            "owner": _service.id, "owner_label": _service.label,
            "is_service": True, "key_url": _service.key_url,
            "help_url": _service.help_url, "key_help": _service.key_help,
            "summary": _service.summary, "note": _service.note,
            "test": _service.id,
        }
for _provider in PROVIDERS:
    _BY_FIELD[_provider.field] = {
        "field": _provider.field, "label": _provider.label,
        "secret": _provider.secret, "optional": _provider.optional,
        "group": _provider.group, "owner": _provider.id,
        "owner_label": _provider.label, "is_service": False,
        "key_url": _provider.key_url, "help_url": _provider.help_url,
        "key_help": _provider.key_help, "summary": _provider.summary,
        "note": "", "test": _provider.id if _provider.probe_name else "",
    }


# ── small helpers ─────────────────────────────────────────────────────────────

def _config() -> dict:
    """The live configuration, read through the existing accessor."""
    try:
        from memory.config_manager import load_api_keys
        return dict(load_api_keys() or {})
    except Exception:
        try:
            from config.settings import get
            return {"gemini_api_key": get("gemini_api_key", "")}
        except Exception:
            return {}


def _value(config: dict, key: str) -> str:
    return str(config.get(key) or os.environ.get(key.upper()) or "").strip()


def _feature_on(name: str) -> bool:
    if not name:
        return True
    try:
        from config.settings import get_feature
        return bool(get_feature(name, True))
    except Exception:
        return True


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _result(state: str, detail: str, ok: bool = False, **extra: Any) -> dict:
    return {"ok": bool(ok), "state": state, "detail": detail, **extra}


def _credential_shape_problem(value: str) -> str:
    """'' when *value* could be a credential, else why it plainly cannot be.

    A key is one unbroken token, and this machine has already stored a whole
    pasted paragraph in a key field. Only the value itself is judged — no guess
    about a provider's format — so an obviously unusable value is reported
    before a request is spent on it.
    """
    text = str(value or "")
    if not text:
        return ""                      # empty is "missing", handled separately
    if any(ch.isspace() for ch in text):
        return "contains spaces or line breaks, so it is pasted text rather than a key"
    if len(text) < 16:
        return f"is only {len(text)} character(s) long, far shorter than a real key"
    return ""


def _get(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT):
    """One GET through requests (the project's HTTP client), or a clear reason."""
    import requests
    return requests.get(url, headers=headers or {}, timeout=timeout)


def _missing_fields(service: Service, config: dict) -> list[str]:
    labels = [label for name, label, _secret, optional in service.fields
              if not optional and not _value(config, name)]
    return labels


def _switched_off(service: Service) -> str:
    if service.switch and not _feature_on(service.switch):
        return (f"{service.label} is switched off in Settings ▸ Permissions, so the "
                f"link is not in use.")
    return ""


# ── probes ────────────────────────────────────────────────────────────────────

#: Text models a Gemini key probe may use, cheapest first. The probe used to pin
#: one model name, and when Google retired it for new accounts the key test
#: reported a perfectly good key as unavailable ("404 … no longer available to
#: new users"). The aliases are the stable entry points, and the *lite* alias is
#: tried first because it is the cheapest request to spend on a probe; the
#: pinned names are fallbacks for an account where an alias has not rolled
#: forward yet. (Measured on a live key: gemini-flash-latest stalled into HTTP
#: 503/504 while gemini-flash-lite-latest answered "ready" in ~2s — a busy
#: full-size model must not decide the verdict when another model answers.)
_GEMINI_PROBE_MODELS = ("gemini-flash-lite-latest", "gemini-flash-latest",
                        "gemini-3.8-flash")

#: The Gemini API refuses a manually-set request deadline below 10 seconds
#: ("Manually set deadline 8s is too short. Minimum allowed deadline is 10s.")
#: while this hub's DEFAULT_TIMEOUT is 8s — so every TEST in Settings ▸ API came
#: back UNAVAILABLE even with a perfectly good key: the 400 was raised before the
#: key was ever considered. The floor sits above the API's minimum so rounding
#: can never drop below it, and it leaves room for a slow-but-healthy reply:
#: measured live, a working flash model can take ~16s, while an overloaded
#: model answers 503 at once and a stalled one is cut at the deadline before
#: the next model in the list is tried. (Verified: 8s → 400 INVALID_ARGUMENT;
#: 12s → 504 DEADLINE_EXCEEDED on a stalled model; 30s → the lite model
#: replies "ready".)
_GEMINI_MIN_DEADLINE_S = 30.0


def _gemini_deadline_ms(timeout: float) -> int:
    return int(max(_GEMINI_MIN_DEADLINE_S,
                   float(timeout or _GEMINI_MIN_DEADLINE_S)) * 1000)


def _gemini_failure_reason(exc: Exception) -> tuple[str, bool]:
    """(plain reason, is_transient) for one failed Gemini call.

    A rejection, a rate limit and a malformed request are three different
    answers; the probe reports which one actually happened instead of dressing
    the last failed model name up as the diagnosis.
    """
    text = str(exc)
    low = text.lower()
    code = getattr(exc, "code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if (code in (401, 403) or "api key not valid" in low
            or "api_key_invalid" in low or "unauthenticated" in low
            or "permission denied" in low):
        return "the key was rejected by Google", False
    if code == 429 or "resource_exhausted" in low or "rate limit" in low or "quota" in low:
        return "the key is rate-limited right now", True
    if code in (500, 502, 503, 504) or "unavailable" in low or "overloaded" in low:
        return "the Gemini service is busy right now", True
    if code == 404 or "not found" in low or "not supported" in low:
        return "that model is not offered to this account", False
    if "deadline" in low:
        return "the probe's own request timeout was rejected by the API", False
    return f"the request failed ({text[:160]})", False


def _gemini_walk_is_futile(exc: Exception) -> bool:
    """True when trying another model cannot change the answer.

    A rejected key stays rejected and a rate limit belongs to the account, not
    to one model — walking the fallbacks would only collect more of the same
    and bury the real reason. A single model that is busy, slow, timed-out or
    retired is exactly what the next entry in the list is for.
    """
    reason, _transient = _gemini_failure_reason(exc)
    return reason in ("the key was rejected by Google",
                      "the key is rate-limited right now")


def _probe_gemini(config: dict, timeout: float) -> dict:
    key = _value(config, "gemini_api_key")
    if not key:
        return _result("missing_key", "No Gemini API key is configured. Nothing "
                                      "else can be tested without it.")
    shape = _credential_shape_problem(key)
    if shape:
        return _result("unavailable", f"The stored Gemini key {shape}. Copy a "
                                      f"fresh key from aistudio.google.com and "
                                      f"press TEST again (a key is one unbroken token).")
    if not _has_module("google.genai"):
        return _result("unavailable", "The google-genai package is not installed "
                                      "(pip install google-genai).")
    from google import genai
    client = genai.Client(
        api_key=key,
        http_options={"api_version": "v1beta",
                      "timeout": _gemini_deadline_ms(timeout)},
    )
    failures: list[str] = []
    failure_was_transient: list[bool] = []
    for model in _GEMINI_PROBE_MODELS:
        try:
            resp = client.models.generate_content(
                model=model, contents="Reply with the single word: ready",
                # max_output_tokens must cover the thinking pass of the newer
                # models, or the reply arrives empty; AFC is off because a probe
                # must not start a tool loop (and the SDK warns about it).
                config={"temperature": 0.0, "max_output_tokens": 32,
                        "automatic_function_calling": {"disable": True}})
            text = str(getattr(resp, "text", "") or "").strip()
            return _result("ready",
                           f"The Gemini key answered ({text[:40] or 'one reply'}).",
                           ok=True, model=model, attempts=len(failures) + 1)
        except Exception as exc:
            reason, is_transient = _gemini_failure_reason(exc)
            failures.append(f"{model}: {reason}")
            failure_was_transient.append(is_transient)
            if _gemini_walk_is_futile(exc):
                # A rejection or an account-wide rate limit: no other model can
                # answer differently, and the remaining 404s would bury it.
                break
    # The key is only "valid but busy" when none of the failures was an answer
    # in its own right (a rejection, a rate limit, a retirement): those are
    # verdicts, not congestion.
    transient = bool(failures) and all(failure_was_transient)
    lead = ("The Gemini key looks valid but the service is not answering right "
            "now — try again in a minute" if transient
            else "The Gemini test failed")
    return _result("unavailable", lead + ": " + "; ".join(failures) + ".",
                   transient=transient, attempts=len(failures))


def _probe_openrouter(config: dict, timeout: float) -> dict:
    key = _value(config, "openrouter_api_key")
    shape = _credential_shape_problem(key)
    if shape:
        # The stored value is not a key, so no request is spent on it: a real
        # forge run would only get a 401 from it.
        return _result("unavailable",
                       f"The stored OpenRouter key {shape}. Copy the key from "
                       f"openrouter.ai/keys (it starts with sk-or-) and press TEST again.")
    if not key:
        try:
            resp = _get("https://openrouter.ai/api/v1/models", {}, timeout)
            if resp.status_code != 200:
                return _result("unavailable",
                               f"OpenRouter answered HTTP {resp.status_code}.")
            models = (resp.json() or {}).get("data") or []
            return _result("key_optional",
                           f"OpenRouter reachable: {len(models)} models; no key "
                           f"set, so rate limits are lower and the Skill Forge "
                           f"will use another provider.",
                           ok=True, models=len(models))
        except Exception as exc:
            return _result("unavailable", f"OpenRouter could not be reached: {exc}")
    # A key IS stored, so the key itself is what gets tested. The public /models
    # list ignores authentication, which is how a 116-character paragraph was
    # reported as READY; /api/v1/key answers with the key's real state.
    try:
        resp = _get("https://openrouter.ai/api/v1/key",
                    {"Authorization": f"Bearer {key}"}, timeout)
        if resp.status_code in (401, 403):
            return _result("unavailable",
                           f"OpenRouter rejected the stored key (HTTP {resp.status_code}). "
                           f"Create or copy a fresh key at openrouter.ai/keys, paste "
                           f"it here and press TEST again.")
        if resp.status_code != 200:
            return _result("unavailable",
                           f"OpenRouter answered HTTP {resp.status_code} while "
                           f"checking the key.")
        data = (resp.json() or {}).get("data") or {}
        detail = "OpenRouter accepted the key"
        label = str(data.get("label") or "").strip()
        if label:
            detail += f" ({label})"
        try:
            usage, limit = data.get("usage"), data.get("limit")
            if limit not in (None, 0):
                detail += f" — {float(usage or 0):.2f} of {float(limit):.2f} credits used"
        except (TypeError, ValueError):
            pass
        return _result("ready", detail + ".", ok=True)
    except Exception as exc:
        return _result("unavailable", f"OpenRouter could not be reached: {exc}")


def _probe_anthropic(config: dict, timeout: float) -> dict:
    key = _value(config, "anthropic_api_key")
    if not key:
        return _result("key_optional", "No Anthropic key is set. Claude models stay "
                                       "unavailable as a fallback provider.")
    try:
        resp = _get("https://api.anthropic.com/v1/models",
                    {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        if resp.status_code != 200:
            return _result("unavailable",
                           f"Anthropic answered HTTP {resp.status_code} — the key "
                           f"was rejected or the account has no access.")
        models = (resp.json() or {}).get("data") or []
        return _result("ready", f"Anthropic key accepted ({len(models)} models "
                                f"available).", ok=True, models=len(models))
    except Exception as exc:
        return _result("unavailable", f"Anthropic could not be reached: {exc}")


def _probe_spotify(config: dict, timeout: float) -> dict:
    """The Spotify MCP integration was removed from this build.

    It used to probe ``mcp_host.servers.spotify`` (server + credentials +
    token); that component is gone by request, so the link reports that
    plainly instead of surfacing a module-load error.
    """
    return _result("unavailable",
                   "The Spotify MCP integration has been removed from this "
                   "build, so this link is not available.")


def _probe_composio(config: dict, timeout: float) -> dict:
    """The connector is two questions before it is one: the package, then the key.

    The round trip is delegated to the connector's own health check rather than
    reimplemented here, so the Settings window and the assistant can never
    disagree about whether Composio answers. The first two branches touch no
    network at all, which is what keeps a TEST button cheap: an absent package or
    an absent key is settled locally, exactly as ``_probe_spotify`` settles its
    credentials locally before it asks anything.
    """
    from connectors import composio_client, manager

    local = composio_client.status()
    if not local.get("package"):
        return _result("unavailable", composio_client.PACKAGE_HINT)
    if not local.get("configured"):
        return _result("missing_key",
                       "Composio has no project API key yet. Paste one in "
                       "Settings ▸ API ▸ External apps and press SAVE, or set "
                       "COMPOSIO_API_KEY.")
    health = manager.status(probe=True)
    if not health.get("reachable"):
        return _result("unavailable",
                       health.get("detail") or "Composio did not answer.")
    return _result(
        "ready",
        f"{health.get('detail') or 'Composio is answering.'} "
        "The External apps switch in Settings ▸ PERMISSIONS decides whether it "
        "may be used.",
        ok=True, apps=health.get("toolkits", 0),
        connected=health.get("connected", 0),
        pending=health.get("pending", 0),
        expired=health.get("expired", 0))


def _probe_weather(config: dict, timeout: float) -> dict:
    key = _value(config, "weather_api_key")
    try:
        resp = _get("https://api.open-meteo.com/v1/forecast"
                    "?latitude=51.5072&longitude=-0.1276&current=temperature_2m",
                    None, timeout)
        if resp.status_code != 200:
            return _result("unavailable",
                           f"The weather endpoint answered HTTP {resp.status_code}.")
        current = ((resp.json() or {}).get("current") or {})
        temp = current.get("temperature_2m")
        detail = (f"Open-meteo answered: {temp}°C in London."
                  if temp is not None else "Open-meteo answered.")
        return _result("ready", detail + (" A private weather key is also set."
                                          if key else " No key needed."),
                       ok=True, sample_c=temp)
    except Exception as exc:
        return _result("unavailable", f"The weather endpoint could not be reached: {exc}")


def _probe_radar(config: dict, timeout: float) -> dict:
    key = _value(config, "radar_api_key")
    try:
        resp = _get("https://api.rainviewer.com/public/weather-maps.json", None, timeout)
        if resp.status_code != 200:
            return _result("unavailable", f"The radar endpoint answered HTTP "
                                          f"{resp.status_code}.")
        frames = len(((resp.json() or {}).get("radar") or {}).get("past") or [])
        detail = f"RainViewer answered with {frames} recent radar frame(s)."
        return _result("ready", detail + (" A radar key is also set." if key
                                          else " No key needed."),
                       ok=True, frames=frames)
    except Exception as exc:
        return _result("unavailable", f"The radar endpoint could not be reached: {exc}")


def _probe_instagram(config: dict, timeout: float) -> dict:
    if not _has_module("instagrapi"):
        return _result("unavailable", "The optional instagrapi package is not "
                                      "installed (pip install instagrapi).")
    username = _value(config, "instagram_username")
    password = _value(config, "instagram_password")
    session = _value(config, "instagram_session")
    if not session and not (username and password):
        return _result("missing_key", "Instagram needs a username and password, or "
                                      "a saved session, before it can log in.")
    if session:
        try:
            payload = json.loads(Path(session).read_text(encoding="utf-8"))
            who = str(payload.get("username") or username or "the saved account")
            return _result("ready", f"A saved Instagram session is present for {who}. "
                                    f"The live login is only attempted when you ask "
                                    f"for something.", ok=True)
        except Exception as exc:
            return _result("unavailable", f"The saved Instagram session could not be "
                                          f"read: {exc}")
    return _result("ready", f"instagrapi is installed and credentials are set for "
                            f"{username}. Instagram rate-limits logins, so the first "
                            f"real request may take a moment.", ok=True)


def _probe_smart_home(config: dict, timeout: float) -> dict:
    if not _has_module("kasa"):
        return _result("unavailable", "The optional python-kasa package is not "
                                      "installed (pip install python-kasa).")
    hosts = [h.strip() for h in _value(config, "smart_home_hosts").split(",") if h.strip()]
    if not hosts:
        return _result("key_optional", "python-kasa is installed. No device addresses "
                                       "are configured, so discovery would be used "
                                       "on this network instead.", ok=True)
    reachable: list[str] = []
    refused: list[str] = []
    for host in hosts[:8]:
        try:
            with socket.create_connection((host, 9999), timeout=min(timeout, 2.0)):
                reachable.append(host)
        except Exception:
            refused.append(host)
    if not reachable:
        return _result("unavailable", "None of the configured devices answered on "
                                      "port 9999: " + ", ".join(refused) + ".")
    detail = f"{len(reachable)} device(s) answered on port 9999: " + ", ".join(reachable)
    if refused:
        detail += f". No answer from: {', '.join(refused)}"
    return _result("ready", detail + ".", ok=True, reachable=len(reachable))


def _probe_connect(config: dict, timeout: float) -> dict:
    try:
        from connect import gateway as connect_gateway
    except Exception as exc:
        return _result("unavailable", f"The Connect gateway module is not available: "
                                      f"{exc}")
    port = 8765
    try:
        port = int(_value(config, "connect_port") or connect_gateway.DEFAULT_PORT)
    except Exception:
        pass
    secret = _value(config, "connect_pairing_secret")
    listening = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=min(timeout, 1.5)):
            listening = True
    except Exception:
        listening = False
    state = "ready" if secret else "missing_key"
    detail = (f"The gateway is {'running' if listening else 'not running'} on port "
              f"{port}. ")
    detail += ("A pairing secret is set, so a phone or browser can pair."
               if secret else
               "No pairing secret is set, so pairing is refused until you add one.")
    return _result(state, detail, ok=bool(secret), listening=listening, port=port)


def _probe_documents(config: dict, timeout: float) -> dict:
    try:
        from documents import tool as documents_tool
        detail = documents_tool.status()
    except Exception as exc:
        return _result("unavailable", f"The document builders could not be inspected: "
                                      f"{exc}")
    ready = detail.count(": ready")
    total = detail.count(": ready") + detail.count(": not installed")
    return _result("ready" if ready else "unavailable",
                   f"{ready} of {total} document engines are ready.\n{detail}",
                   ok=bool(ready))


# ── provider-key probes ───────────────────────────────────────────────────────
# One real request each, to the cheapest authenticated read the provider offers.
# A provider with no such endpoint says so rather than inventing a test.

def _probe_openai(config: dict, timeout: float) -> dict:
    key = _value(config, "openai_api_key")
    if not key:
        return _result("key_optional", "No OpenAI key is set, so OpenAI is not "
                                       "available as a text provider.")
    try:
        resp = _get("https://api.openai.com/v1/models",
                    {"Authorization": f"Bearer {key}"}, timeout)
        if resp.status_code in (401, 403):
            return _result("unavailable", f"OpenAI rejected the key (HTTP "
                                          f"{resp.status_code}).")
        if resp.status_code != 200:
            return _result("unavailable", f"OpenAI answered HTTP {resp.status_code}.")
        models = (resp.json() or {}).get("data") or []
        return _result("ready", f"OpenAI accepted the key ({len(models)} model(s) "
                                f"listed).", ok=True, models=len(models))
    except Exception as exc:
        return _result("unavailable", f"OpenAI could not be reached: {exc}")


def _probe_groq(config: dict, timeout: float) -> dict:
    key = _value(config, "groq_api_key")
    if not key:
        return _result("key_optional", "No Groq key is set, so Groq is not "
                                       "available as a provider.")
    try:
        resp = _get("https://api.groq.com/openai/v1/models",
                    {"Authorization": f"Bearer {key}"}, timeout)
        if resp.status_code in (401, 403):
            return _result("unavailable", f"Groq rejected the key (HTTP "
                                          f"{resp.status_code}).")
        if resp.status_code != 200:
            return _result("unavailable", f"Groq answered HTTP {resp.status_code}.")
        models = (resp.json() or {}).get("data") or []
        return _result("ready", f"Groq accepted the key ({len(models)} model(s) "
                                f"listed).", ok=True, models=len(models))
    except Exception as exc:
        return _result("unavailable", f"Groq could not be reached: {exc}")


def _probe_fish_audio(config: dict, timeout: float) -> dict:
    key = _value(config, "fish_audio_api_key")
    if not key:
        return _result("key_optional", "No Fish Audio key is set, so the Fish "
                                       "Audio voice stays unavailable.")
    try:
        resp = _get("https://api.fish.audio/model?page_size=1",
                    {"Authorization": f"Bearer {key}"}, timeout)
        if resp.status_code in (401, 403):
            return _result("unavailable", f"Fish Audio rejected the key (HTTP "
                                          f"{resp.status_code}).")
        if resp.status_code != 200:
            return _result("unavailable", f"Fish Audio answered HTTP "
                                          f"{resp.status_code}.")
        return _result("ready", "Fish Audio accepted the key.", ok=True)
    except Exception as exc:
        return _result("unavailable", f"Fish Audio could not be reached: {exc}")


def _probe_elevenlabs(config: dict, timeout: float) -> dict:
    key = _value(config, "elevenlabs_api_key")
    if not key:
        return _result("key_optional", "No ElevenLabs key is set, so the "
                                       "ElevenLabs voice stays unavailable.")
    try:
        resp = _get("https://api.elevenlabs.io/v1/user",
                    {"xi-api-key": key}, timeout)
        if resp.status_code in (401, 403):
            return _result("unavailable", f"ElevenLabs rejected the key (HTTP "
                                          f"{resp.status_code}).")
        if resp.status_code != 200:
            return _result("unavailable", f"ElevenLabs answered HTTP "
                                          f"{resp.status_code}.")
        who = str((resp.json() or {}).get("subscription", {}).get("tier") or "")
        detail = "ElevenLabs accepted the key" + (f" ({who} plan)." if who else ".")
        return _result("ready", detail, ok=True, plan=who or None)
    except Exception as exc:
        return _result("unavailable", f"ElevenLabs could not be reached: {exc}")


def _probe_mem0(config: dict, timeout: float) -> dict:
    """Mem0 has no safe read-only endpoint to spend a key on, and the hub does
    not invent one: this reports 'present', never 'working'."""
    key = _value(config, "mem0_api_key")
    if not key:
        return _result("key_optional", "No Mem0 key is set; long-term memory "
                                       "stays in the local store.")
    env = bool(os.environ.get("MEM0_API_KEY"))
    extra = " (the MEM0_API_KEY environment variable is set as well)" if env else ""
    return _result("configured", f"A Mem0 key is stored{extra}. Mem0 offers no "
                                 f"read-only call to test a key against, so this "
                                 f"says the key is present — the first memory "
                                 f"write is the real test.", present=True)


def _probe_qdrant(config: dict, timeout: float) -> dict:
    """Qdrant is testable for real: the collections list is a cheap read."""
    key = _value(config, "qdrant_api_key")
    url = _value(config, "qdrant_url") or "http://localhost:6333"
    if "://" not in url:
        url = "http://" + url
    try:
        headers = {"api-key": key} if key else {}
        resp = _get(url.rstrip("/") + "/collections", headers, timeout)
        if resp.status_code in (401, 403):
            return _result("unavailable", f"Qdrant rejected the request "
                                          f"(HTTP {resp.status_code}) — the API "
                                          f"key does not fit this cluster.")
        if resp.status_code != 200:
            return _result("unavailable", f"{url} answered HTTP "
                                          f"{resp.status_code} — is a Qdrant "
                                          f"server listening there?")
        collections = (((resp.json() or {}).get("result") or {}).get("collections") or [])
        detail = f"Qdrant at {url} answered with {len(collections)} collection(s)."
        if not key and "localhost" not in url and "127.0.0.1" not in url:
            detail += " No API key is set — a cloud cluster needs one."
        return _result("ready", detail, ok=True, collections=len(collections))
    except Exception as exc:
        return _result("unavailable", f"Qdrant at {url} could not be reached: {exc}")


# ── public API ────────────────────────────────────────────────────────────────

def _probe_name(service: Service) -> str:
    return service.probe_name or f"_probe_{service.id}"


def _provider_probe_name(provider: ProviderKey) -> str:
    return provider.probe_name or ""


def describe(service_id: str) -> dict | None:
    """UI metadata for one service (no probing, no network)."""
    service = _BY_ID.get(str(service_id or "").strip().lower())
    if service is None:
        return None
    return {
        "id": service.id,
        "label": service.label,
        "summary": service.summary,
        "switch": service.switch,
        "note": service.note,
        "fields": [{"name": name, "label": label, "secret": secret,
                    "optional": optional}
                   for name, label, secret, optional in service.fields],
    }


def probe(service_id: str, config: dict | None = None,
          timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Test one service for real. Never raises, never hangs beyond *timeout*."""
    service = _BY_ID.get(str(service_id or "").strip().lower())
    if service is None:
        return _result("unknown", f"'{service_id}' is not a service the hub knows "
                                 f"about. Services: "
                                 + ", ".join(s.id for s in SERVICES) + ".")
    config = dict(config) if config is not None else _config()
    started = time.monotonic()
    off = _switched_off(service)
    if off:
        return _result("disabled", off, switch=service.switch)
    probe_fn: Callable[[dict, float], dict] = globals()[_probe_name(service)]
    try:
        result = probe_fn(config, float(timeout))
    except Exception as exc:
        result = _result("unavailable", f"The {service.label} test failed: "
                                        f"{type(exc).__name__}: {exc}")
    result["service"] = service.id
    result["label"] = service.label
    result["switch"] = service.switch
    result["elapsed_s"] = round(time.monotonic() - started, 2)
    return result


def _service_state(service: Service, config: dict, timeout: float) -> tuple[str, str]:
    """The offline state of one service, without a socket."""
    off = _switched_off(service)
    if off:
        return "disabled", off
    missing = _missing_fields(service, config)
    configured = len(service.fields) - len(missing)
    if missing:
        return "missing_key", "Not fully configured: " + ", ".join(missing) + " missing."
    if service.id == "documents":
        return _probe_documents(config, timeout).get("state", ""), \
            "Ready to check which engines are installed."
    return "configured", ("Configured." if not service.fields else
                           f"{configured} credential(s) set. Press TEST to check "
                           f"the link.")


def status_all(config: dict | None = None, timeout: float = 3.0) -> list[dict]:
    """The cheap state of every service: what is configured, without network.

    Used to paint the status pills when the Settings window opens; the TEST
    button is what performs the real round trip. Nothing here touches a socket.
    """
    config = dict(config or _config())
    out: list[dict] = []
    for service in SERVICES:
        state, detail = _service_state(service, config, timeout)
        out.append({**describe(service.id), "state": state, "detail": detail,
                    "ok": state in ("configured", "key_optional")})
    return out


def key_meta(field: str) -> dict:
    """What the hub knows about the credential stored in *field*.

    The Settings window renders every key row from this: the label, whether it
    is secret, where to get it, how to get it, and which probe answers whether
    it works. An empty dict means the field is not a credential the hub knows —
    the UI then falls back to a plain field instead of growing a second table.
    """
    return dict(_BY_FIELD.get(str(field or "").strip().lower(), {}))


#: The blocks of the Settings window, in the order a person sets things up.
_GROUP_ORDER = ("AI", "VOICE", "MEMORY", "Services")


def key_targets() -> list[dict]:
    """Every credential row the UI can show, in display order."""
    rows = [dict(meta) for meta in _BY_FIELD.values()]
    rows.sort(key=lambda row: (_GROUP_ORDER.index(row["group"])
                               if row["group"] in _GROUP_ORDER
                               else len(_GROUP_ORDER)))
    return rows


def _row_state(row: dict, config: dict) -> tuple[str, str]:
    """Offline state for one credential row (never a socket)."""
    value = _value(config, row["field"])
    if row["is_service"]:
        service = _BY_ID.get(str(row["owner"]))
        if service is not None:
            off = _switched_off(service)
            if off:
                return "disabled", off
        if value:
            return "configured", "Set. Press TEST for a real request."
        if row["optional"]:
            return "key_optional", f"Optional — {row['label']} is not set."
        return "missing_key", (f"{row['label']} is required by "
                               f"{row.get('owner_label', 'this link')}.")
    provider = _PROVIDER_BY_ID.get(str(row["owner"]))
    if value:
        return "configured", "Set. Press TEST for a real request."
    if row["optional"]:
        if str(row["field"]) == "qdrant_url":
            return "key_optional", ("Blank — the local server at "
                                    "http://localhost:6333 is used.")
        return "key_optional", f"Optional — {row['label']} is not set."
    if provider is not None:
        return "missing_key", f"{provider.label} needs this key to be usable."
    return "missing_key", f"{row['label']} is not set."


def row_status(field: str, config: dict | None = None) -> dict:
    """The cheap, offline state of one credential row ({} if unknown).

    An explicitly passed mapping (including ``{}``) is used as given: a caller
    asking "what does this row say about *this* configuration?" must not have
    the live config merged back in behind it.
    """
    meta = key_meta(field)
    if not meta:
        return {}
    state, detail = _row_state(meta, dict(config) if config is not None else _config())
    return {**meta, "state": state, "detail": detail,
            "ok": state in ("configured", "key_optional")}


def status_targets(config: dict | None = None) -> list[dict]:
    """The cheap, offline state of every credential row (no sockets)."""
    config = dict(config) if config is not None else _config()
    out: list[dict] = []
    for row in key_targets():
        state, detail = _row_state(row, config)
        out.append({**row, "state": state, "detail": detail,
                    "ok": state in ("configured", "key_optional")})
    return out


def probe_target(target: str, config: dict | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Test one row of the hub for real, whether it is a link or a provider."""
    key = str(target or "").strip().lower()
    if key in _BY_ID:
        return probe(key, config, timeout)
    provider = _PROVIDER_BY_ID.get(key)
    if provider is None:
        return _result("unknown", f"'{target}' is not a link or a provider the "
                                 f"hub can test. Links: "
                                 + ", ".join(s.id for s in SERVICES)
                                 + ". Providers: "
                                 + ", ".join(p.id for p in PROVIDERS) + ".")
    config = dict(config or _config())
    name = _provider_probe_name(provider)
    if not name:
        state, detail = _row_state(key_meta(provider.field), config)
        return _result(state, detail, provider=provider.id, label=provider.label)
    started = time.monotonic()
    try:
        result = globals()[name](config, float(timeout))
    except Exception as exc:
        result = _result("unavailable", f"The {provider.label} test failed: "
                                        f"{type(exc).__name__}: {exc}")
    result["provider"] = provider.id
    result["label"] = provider.label
    result["field"] = provider.field
    result["elapsed_s"] = round(time.monotonic() - started, 2)
    return result


def provider_status_all(config: dict | None = None) -> list[dict]:
    """The cheap state of every provider key (no network)."""
    config = dict(config) if config is not None else _config()
    out: list[dict] = []
    for provider in PROVIDERS:
        state, detail = _row_state(key_meta(provider.field), config)
        out.append({"id": provider.id, "label": provider.label,
                    "field": provider.field, "summary": provider.summary,
                    "key_url": provider.key_url, "help_url": provider.help_url,
                    "key_help": provider.key_help, "group": provider.group,
                    "state": state, "detail": detail,
                    "ok": state in ("configured", "key_optional")})
    return out


def provider_summary(config: dict | None = None) -> str:
    """One text block for the assistant: every provider key and its state."""
    lines = ["PROVIDER KEYS"]
    for row in provider_status_all(config):
        lines.append(f"- {row['label']} ({row['field']}): {row['state']} — "
                     f"{row['detail']}")
    return "\n".join(lines)


def summary(config: dict | None = None) -> str:
    """One text block for the assistant: every link and its honest state."""
    rows = status_all(config)
    lines = ["SERVICE LINKS"]
    for row in rows:
        lines.append(f"- {row['label']}: {row['state']} — {row['detail']}")
    lines.append("")
    lines.append(provider_summary(config))
    return "\n".join(lines)
