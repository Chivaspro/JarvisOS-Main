"""AI provider hub — pick the brain behind J.A.R.V.I.S.

Supported providers: gemini (native voice session), openai, anthropic,
groq, ollama (local) and fully custom OpenAI-compatible endpoints.

Only `gemini` drives the real-time voice session. Every other provider
handles typed text commands through a plain HTTP chat-completion call, so
an API key + model is all it takes — no extra dependencies (requests only).
"""
from __future__ import annotations


GEMINI_DEFAULT_MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
# The Gemini provider exists to drive the real-time voice session, so every
# model it offers must actually be Live-capable. Verified against the API with
# this project's own key: a non-live name is refused at connect time with
# "… is not found for API version v1alpha, or is not supported for
# bidiGenerateContent", which takes the whole voice pipeline down.
GEMINI_LIVE_MODELS = [
    GEMINI_DEFAULT_MODEL,
    "models/gemini-2.5-flash-native-audio-latest",
    "models/gemini-2.5-flash-native-audio-preview-09-2025",
]

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "label": "Google Gemini (voice + text)",
        "models": list(GEMINI_LIVE_MODELS),
        "needs_key": True,
        "key_field": "gemini_api_key",
    },
    "openai": {
        "label": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "o3-mini", "gpt-4.1-mini"],
        "needs_key": True,
        "key_field": "openai_api_key",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "endpoint": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
        "needs_key": True,
        "key_field": "anthropic_api_key",
    },
    "groq": {
        "label": "Groq (fast + free tier)",
        "endpoint": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "needs_key": True,
        "key_field": "groq_api_key",
    },
    "openrouter": {
        # One key, every frontier model. OpenRouter is OpenAI-compatible, so it
        # rides the exact same HTTP path as the other providers — and it is the
        # first choice of the self-evolution engine's synthesis brain when it is
        # configured, because a forge run is a large, code-heavy prompt.
        "label": "OpenRouter (one key, many models)",
        "endpoint": "https://openrouter.ai/api/v1",
        "models": [
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-4.1",
            "google/gemini-2.5-flash",
            "deepseek/deepseek-chat-v3.1:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-coder:free",
        ],
        "needs_key": True,
        "key_field": "openrouter_api_key",
    },
    "ollama": {
        "label": "Ollama (local, no key)",
        "endpoint": "http://localhost:11434/v1",
        "models": ["llama3.1", "qwen2.5", "mistral", "phi3"],
        "needs_key": False,
        "key_field": "",
    },
    "custom": {
        "label": "Custom (OpenAI-compatible)",
        "endpoint": "",
        "models": [],
        "needs_key": True,
        "key_field": "custom_ai_api_key",
    },
}


def provider_names() -> list[str]:
    return list(PROVIDERS)


# The names that identify a real-time (bidiGenerateContent) model in the
# Gemini catalog — verified against models.list() with this project's key:
# every Live/native-audio entry carries one of these, and every entry without
# one is rejected at connect time.
LIVE_MODEL_HINTS = ("live", "native-audio")


def is_live_model(model: str) -> bool:
    """Could *model* serve the real-time voice session at all?"""
    low = str(model or "").strip().lower()
    return bool(low) and any(hint in low for hint in LIVE_MODEL_HINTS)


def resolve_live_model(config: dict) -> tuple[str, str]:
    """The model the LIVE voice session will actually connect to, + a note.

    Settings ▸ AI writes ``ai_model`` and the voice session used to obey it
    blindly, so picking any non-Live model there silently killed audio (the
    connect was rejected) — a configuration that lied about the runtime. The
    session now takes an explicit ``live_model`` when present, otherwise the
    configured model when it really is Live-capable, otherwise the known-good
    default; a substitution is always REPORTED, never swallowed.

    Returns ``(model_id, note)`` — note is "" when nothing was substituted.
    """
    cfg = config or {}
    explicit = str(cfg.get("live_model") or "").strip()
    if explicit:
        if is_live_model(explicit):
            return explicit, ""
        fallback = GEMINI_DEFAULT_MODEL
        return fallback, (
            f"live_model={explicit!r} is not a real-time voice model — "
            f"using {fallback} instead")
    configured = str(cfg.get("ai_model") or "").strip()
    if not configured:
        return GEMINI_DEFAULT_MODEL, ""
    if is_live_model(configured):
        return configured, ""
    # The stored provider is not Gemini (or an old install kept a text model):
    # the voice session must still get a Live model.
    if str(cfg.get("ai_provider") or "gemini").strip().lower() != "gemini":
        return GEMINI_DEFAULT_MODEL, ""
    return GEMINI_DEFAULT_MODEL, (
        f"{configured!r} cannot serve the real-time voice session "
        f"(not Live-capable) — native audio uses {GEMINI_DEFAULT_MODEL}. "
        "Pick a Gemini Live model in Settings ▸ AI to change it.")



def models_for(provider: str) -> list[str]:
    return list(PROVIDERS.get(provider, {}).get("models", []))


def active_config(config: dict) -> dict:
    """Resolve provider + model + endpoint + key from the app config."""
    provider = str(config.get("ai_provider", "gemini") or "gemini").strip().lower()
    if provider not in PROVIDERS:
        provider = "gemini"
    meta = PROVIDERS[provider]
    if provider == "gemini":
        return {
            "provider": provider,
            "model": str(config.get("ai_model") or GEMINI_DEFAULT_MODEL).strip(),
            "endpoint": "",
            "api_key": str(config.get("gemini_api_key", "") or "").strip(),
        }
    if provider == "custom":
        return {
            "provider": provider,
            "model": str(config.get("custom_ai_model", "") or "").strip(),
            "endpoint": str(config.get("custom_ai_base_url", "") or "").strip().rstrip("/"),
            "api_key": str(config.get("custom_ai_api_key", "") or "").strip(),
            "name": str(config.get("custom_provider_name", "") or "Custom").strip(),
        }
    key_field = meta.get("key_field", "")
    return {
        "provider": provider,
        "model": str(config.get("ai_model") or (meta["models"][0] if meta["models"] else "")).strip(),
        "endpoint": str(config.get(f"{provider}_base_url") or meta.get("endpoint", "")).strip().rstrip("/"),
        "api_key": str(config.get(key_field, "") or "").strip(),
    }


#: The default persona every plain provider call carries. Callers that need a
#: different role (the self-evolution engine's code synthesizer) pass ``system=``.
DEFAULT_SYSTEM = ("You are JARVIS, a concise refined British executive "
                  "assistant. Always answer in English.")


def _openai_chat(endpoint: str, api_key: str, model: str, message: str,
                 timeout: int = 60, system: str | None = None,
                 temperature: float = 0.7) -> str:
    import requests
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(
        endpoint.rstrip("/") + "/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system or DEFAULT_SYSTEM},
                {"role": "user", "content": message},
            ],
            "temperature": temperature,
        },
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return str(data["choices"][0]["message"]["content"])


def _anthropic_chat(api_key: str, model: str, message: str, timeout: int = 60,
                    system: str | None = None, temperature: float = 0.7,
                    max_tokens: int = 1024) -> str:
    import requests
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system or DEFAULT_SYSTEM,
            "messages": [{"role": "user", "content": message}],
        },
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def chat(config: dict, message: str, timeout: int = 60, system: str | None = None,
         temperature: float = 0.7, max_tokens: int = 1024) -> str:
    """One-shot text answer from the configured non-Gemini provider.

    ``system`` / ``temperature`` / ``max_tokens`` default to the assistant
    persona; the self-evolution engine overrides all three for code synthesis.
    """
    active = active_config(config)
    provider = active["provider"]
    if provider == "gemini":
        raise ValueError("Gemini uses the live voice session — no HTTP call needed.")
    model = active.get("model", "")
    if not model:
        raise ValueError(f"No model selected for provider '{provider}'. Pick one in Control Center → AI.")
    if provider == "anthropic":
        if not active.get("api_key"):
            raise ValueError("Anthropic API key is missing — add it in Control Center → AI.")
        return _anthropic_chat(active["api_key"], model, message, timeout,
                               system=system, temperature=temperature,
                               max_tokens=max_tokens)
    endpoint = active.get("endpoint", "")
    if not endpoint.startswith(("http://", "https://")):
        raise ValueError(
            f"Provider '{provider}' has no valid endpoint URL — set it in Control Center → AI."
        )
    meta = PROVIDERS.get(provider, {})
    if meta.get("needs_key") and not active.get("api_key"):
        raise ValueError(f"API key for '{provider}' is missing — add it in Control Center → AI.")
    return _openai_chat(endpoint, active.get("api_key", ""), model, message, timeout,
                        system=system, temperature=temperature)


def test_connection(config: dict, timeout: float = 20.0) -> str:
    """Make one real request with the configured provider (raises on failure).

    Gemini used to be "tested" by checking that the key was non-empty, which
    certified a dead or malformed key as ready. It now goes through the hub's
    single Gemini probe — the one that knows the API's 10s minimum deadline —
    so AI LINK OK means a reply really came back, and the voice model that will
    actually be connected is named.
    """
    active = active_config(config)
    if active["provider"] == "gemini":
        if not active.get("api_key"):
            raise ValueError("Gemini API key is missing.")
        from services import hub as _hub
        result = _hub.probe("gemini", dict(config or {}), timeout=timeout)
        if result.get("state") != "ready":
            raise RuntimeError(str(result.get("detail")
                                   or "the Gemini probe did not come back ready"))
        model = str(result.get("model") or active.get("model") or "")
        live, note = resolve_live_model(dict(config or {}))
        reply = f"Gemini answered on {model}. Live voice model: {live}"
        if note:
            reply += f" ({note})"
        return reply
    return chat(config, "Reply with exactly: link OK", timeout=30)
