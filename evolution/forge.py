"""The Forge — natural language goal in, verified Jarvis plugin out.

The pipeline, in order:

1. ask the synthesis brain for a JSON package ``{manifest, code, test_cases}``:
   OpenRouter leads whenever its stored key is usable (one key, many models),
   then the provider selected in Settings ▸ AI, then Anthropic → OpenAI → Groq
   → Gemini. A stored value that is not a key — a pasted paragraph, a stub — is
   skipped and reported instead of being sent, so a forge run never spends its
   first attempt on a guaranteed 401;
2. validate the code statically (AST + safety bans);
3. resolve its third-party imports (installing what is missing, when allowed);
4. run it in the sandbox against the supplied test cases;
5. on any failure, hand the exact error back to the model and retry, up to
   ``max_repair_attempts`` times;
6. hand the verified source to ``evolution.vault``, which writes it as an
   ordinary plugin and hot-registers it.

Nothing is installed until all of that passes — the sandbox is the only place a
candidate ever executes before it becomes a Jarvis tool.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from evolution import crucible, vault, discovery

__all__ = ["forge", "synthesis_providers", "parse_json_response"]

_SYSTEM = '''You are the J.A.R.V.I.S. autonomous skill architect.

You invent, architect and write ONE complete, production-ready Python plugin
for the J.A.R.V.I.S. desktop assistant, from the user's plain-language request.

Contract (non-negotiable):
1. The code defines exactly one required entry point:
       def run(parameters: dict, player=None, session_memory=None) -> str
   It returns a SHORT natural-language sentence that will be spoken back to the
   user. English only. It must never return a dict.
2. Robust defaults: read every expected key from `parameters` with a safe
   fallback, e.g. `query = str(parameters.get("query") or "").strip()`. The
   function MUST execute cleanly when called with `{}` — no KeyError, no
   TypeError, no exception escaping at all. Wrap the body in try/except and
   return a spoken fallback sentence on failure.
3. Do NOT define PLUGIN or FEATURE_METADATA — the installer writes those.
   Only imports, helpers and `run` (module-level docstring optional).
4. Self-contained: never require an API key of its own. Use public, keyless
   APIs when live data is needed, or pure local computation. If a network call
   fails or times out, catch it and answer with a useful fallback sentence.
5. Third-party packages: only ones importable from pip and worth the cost
   (requests, pillow, psutil, python-docx, openpyxl, python-pptx, pypdf,
   matplotlib, numpy, bs4). Import them at the TOP LEVEL so the installer can
   detect and install them. Prefer none when the standard library suffices.
6. Deliverables that are images/charts/documents must be written to a real file
   path (e.g. under the user's Pictures folder or `data/forge/`) and the return
   sentence must name that path.
7. Never touch the operating system destructively, never modify the Jarvis
   source tree, never spawn elevated processes.

Output format — return ONLY this JSON object, no markdown fences:
{
  "manifest": {
    "name": "snake_case_name",
    "description": "One or two sentences telling the model exactly when to call this tool, naming overlapping built-in tools to avoid when relevant.",
    "parameters": {
      "type": "OBJECT",
      "properties": {"query": {"type": "STRING", "description": "..."}},
      "required": []
    },
    "triggers": ["direct phrase", "natural variation"],
    "aliases": ["shortname"]
  },
  "code": "full python source, imports + helpers + def run(...)",
  "test_cases": [{"input": {}}, {"input": {"query": "a realistic value"}}]
}

`parameters`, `triggers` and `aliases` may be empty ({}/[]) for a tool that
takes no arguments or needs no special routing.
'''


# ── provider selection ────────────────────────────────────────────────────────

#: Provider id → the credential field its key lives in. Display order: the
#: preferred synthesis brain first, then the rest.
PROVIDER_KEY_FIELDS: dict[str, str] = {
    "openrouter": "openrouter_api_key",
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "groq": "groq_api_key",
    "gemini": "gemini_api_key",
}

#: How each provider is named in sentences the user reads or hears.
PROVIDER_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "groq": "Groq",
    "gemini": "Gemini",
}


def key_shape_problem(value: object) -> str:
    """'' when *value* could be a provider key, else why it plainly cannot be.

    A key is one unbroken token. A whole pasted paragraph was stored in one of
    these fields once; that value is skipped before any request is spent on it,
    and the reason is carried into the status report the user sees.
    """
    text = str(value or "")
    if not text:
        return ""                      # empty is "missing", handled separately
    if any(ch.isspace() for ch in text):
        return "the stored value contains spaces — it is pasted text, not a key"
    if len(text) < 16:
        return f"the stored value is only {len(text)} characters long"
    return ""


def _config_view(config: dict | None = None) -> dict:
    """api_keys.json merged with explicit overrides, never raising."""
    data: dict[str, Any] = {}
    try:
        from memory.config_manager import load_api_keys
        data.update(load_api_keys() or {})
    except Exception:
        pass
    try:
        from config.settings import get as _cfg_get  # type: ignore
        for key in ("gemini_api_key", "openrouter_api_key", "anthropic_api_key",
                    "openai_api_key", "groq_api_key"):
            if not data.get(key):
                value = _cfg_get(key, "")
                if value:
                    data[key] = value
    except Exception:
        pass
    data.update({k: v for k, v in dict(config or {}).items() if v is not None})
    return data


def _usable_providers(cfg: dict) -> list[tuple[str, dict]]:
    """Providers whose key is present AND shaped like a key, best first."""
    configured = str(cfg.get("ai_provider") or "gemini").strip().lower()
    order = ["openrouter", configured, "anthropic", "openai", "groq", "gemini"]
    seen: set[str] = set()
    chain: list[tuple[str, dict]] = []
    for provider in order:
        if not provider or provider in seen or provider not in PROVIDER_KEY_FIELDS:
            continue
        seen.add(provider)
        value = str(cfg.get(PROVIDER_KEY_FIELDS[provider]) or "").strip()
        if not value or key_shape_problem(value):
            continue
        chain.append((provider, cfg))
    return chain


def synthesis_providers(config: dict | None = None) -> list[tuple[str, dict]]:
    """Every provider that could synthesise, best first, with a usable key.

    OpenRouter leads whenever its key is usable — one key across many models,
    and the brain the user picked for self-evolution. The provider selected in
    Settings ▸ AI follows it, then Anthropic → OpenAI → Groq → Gemini (whose
    text path is the generative API, not the voice session). Providers with no
    key, or with a stored value that is not a key, are omitted —
    :func:`provider_diagnostics` says which and why.
    """
    return _usable_providers(_config_view(config))


def provider_diagnostics(config: dict | None = None) -> list[dict]:
    """Every synthesis-capable provider with its honest readiness.

    ``state`` is ``usable`` | ``missing`` | ``skipped``. This is what the
    Settings ▸ EVOLUTION tab and the spoken status report read, so a stored
    value that is not a key is reported as skipped rather than looking ready —
    the state that let a pasted paragraph sit in the OpenRouter field while
    every forge attempt burned on its 401.
    """
    cfg = _config_view(config)
    in_chain = {provider for provider, _cfg in _usable_providers(cfg)}
    rows: list[dict] = []
    for provider, field in PROVIDER_KEY_FIELDS.items():
        value = str(cfg.get(field) or "").strip()
        problem = key_shape_problem(value)
        if not value:
            state, detail = "missing", f"No {provider} key is set."
        elif problem:
            state, detail = "skipped", f"Not used: {problem}."
        else:
            state, detail = "usable", "The key is present and shaped like a key."
        rows.append({"provider": provider, "field": field, "state": state,
                     "detail": detail, "in_chain": provider in in_chain})
    return rows


def skipped_providers(config: dict | None = None) -> list[dict]:
    """Providers with a stored value that is not a key (never sent)."""
    return [row for row in provider_diagnostics(config) if row["state"] == "skipped"]


def _gemini_text(prompt: str, system: str, config: dict) -> str:
    """One Gemini synthesis call through the project's model router.

    The router owns model health (master spec §6/§58): when the role model is
    overloaded (503) or rate-limited it moves to a healthy model instead of
    retrying the same name blind — a forge run is a large code prompt, so it
    uses the 'coding' role and the response must be JSON. A hard 503 used to
    end the whole forge with "Gemini text model unavailable".
    """
    from google import genai
    from google.genai import types as gtypes
    from app import model_router

    client = genai.Client(api_key=str(config.get("gemini_api_key") or ""),
                          http_options={"api_version": "v1beta"})
    resp = model_router.generate(
        client,
        contents=prompt,
        role="coding",
        config=gtypes.GenerateContentConfig(
            temperature=0.2,
            system_instruction=system,
            response_mime_type="application/json",
            # A synthesis call must never start a tool loop, and the SDK warns
            # about AFC in generate_content when it is left on.
            automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(
                disable=True),
        ),
    )
    text = str(getattr(resp, "text", "") or "")
    if not text.strip():
        raise RuntimeError("Gemini returned an empty synthesis reply")
    return text


def _ask_model(prompt: str, system: str, config: dict) -> tuple[str, str]:
    """One synthesis call through the best available provider.

    Returns ``(raw_text, provider_name)``; raises the last error when every
    provider fails, so the caller can report exactly what happened.
    """
    chain = synthesis_providers(config)
    if not chain:
        skipped = skipped_providers(config)
        note = (" These stored values are not keys and were not used: "
                + ", ".join(f"{PROVIDER_LABELS.get(r['provider'], r['provider'])} "
                            f"({r['detail']})" for r in skipped)
                if skipped else "")
        raise RuntimeError(
            "No AI provider key is available for skill synthesis. Add an "
            "OpenRouter, Anthropic, Gemini, OpenAI or Groq key in "
            "Settings ▸ API HUB." + note)

    last_error: Exception | None = None
    for provider, cfg in chain:
        try:
            if provider == "gemini":
                return _gemini_text(prompt, system, cfg), provider
            from app import ai_providers
            model = str(cfg.get("ai_model") or "").strip()
            if provider != str(cfg.get("ai_provider") or "").strip().lower() or not model:
                models = ai_providers.models_for(provider)
                model = models[0] if models else model
            call_cfg = dict(cfg)
            call_cfg["ai_provider"] = provider
            call_cfg["ai_model"] = model
            text = ai_providers.chat(call_cfg, prompt, timeout=240, system=system,
                                     temperature=0.2, max_tokens=8192)
            return text, provider
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Every synthesis provider failed. Last error: {last_error}")


# ── JSON parsing ──────────────────────────────────────────────────────────────

def parse_json_response(text: str) -> dict:
    """Extract one JSON object from model output, tolerating fences and prose."""
    clean = str(text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*\n?", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"\n?```\s*$", "", clean).strip()
    try:
        data = json.loads(clean)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    start = clean.find("{")
    if start != -1:
        try:
            obj, _end = json.JSONDecoder().raw_decode(clean[start:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        for end in range(len(clean) - 1, start, -1):
            if clean[end] != "}":
                continue
            try:
                obj = json.loads(clean[start:end + 1])
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("no JSON object could be read from the model's reply")


def _coerce_package(data: dict) -> tuple[dict, str, list[dict]]:
    manifest = data.get("manifest")
    code = data.get("code")
    if not isinstance(manifest, dict) or not isinstance(code, str) or not code.strip():
        raise ValueError("the reply is missing a manifest object or a code string")
    cases = data.get("test_cases")
    if not isinstance(cases, list) or not cases:
        cases = [{"input": {}}]
    clean_cases = [c for c in cases if isinstance(c, dict)]
    return manifest, code, clean_cases or [{"input": {}}]


# ── the forge ─────────────────────────────────────────────────────────────────

def _emit(progress, phase: str, detail: str = "") -> None:
    """Report one real phase to the caller. Never fatal, never fabricated.

    Each call sits at the boundary of the work it names, so the HUD shows exactly
    the phases that actually ran, in the order they ran — no timer, no synthetic
    percentage.
    """
    if progress is None:
        return
    try:
        progress(str(phase), str(detail))
    except Exception:
        pass


def forge(goal: str, *, name: str | None = None, context: str = "",
          config: dict | None = None, max_repair_attempts: int = 2,
          allow_install: bool | None = None, directory=None,
          hot_register: bool = True, progress=None) -> dict:
    """Synthesise, verify and install a brand-new Jarvis skill.

    Always returns a dict; ``message`` is safe to speak verbatim.
    """
    goal = " ".join(str(goal or "").split())
    if len(goal) < 6:
        return {"ok": False, "message": "I need a clearer description of the skill "
                                        "to forge, sir."}

    if allow_install is None:
        try:
            from config.settings import get_feature
            allow_install = bool(get_feature("self_evolution_auto_install_deps", True))
        except Exception:
            allow_install = True

    name_hint = vault.sanitize_name(name or discovery.suggest_name(goal))
    parsed = discovery.analyze_command(goal)
    if parsed and not name:
        name_hint = parsed.get("suggested_name") or name_hint

    prompt = (f"User request: {goal}\n"
              f"Preferred name: {name_hint or 'choose one'}\n"
              f"Extra context: {context or 'none'}\n")

    _emit(progress, "ANALYZE", f"goal: {goal[:70]}")
    _emit(progress, "GENERATE", f"asking the model to write '{name_hint or 'a new skill'}'")
    try:
        raw, provider = _ask_model(prompt, _SYSTEM, config)
        manifest, code, test_cases = _coerce_package(parse_json_response(raw))
    except Exception as exc:
        return {"ok": False, "message": f"Skill synthesis failed: {exc}"}

    # An explicit name from the caller wins; otherwise the synthesised manifest's
    # name is used, and the goal-derived hint is the last resort.
    requested = vault.sanitize_name(name) if name else ""
    actual_name = (requested
                   or vault.sanitize_name(str(manifest.get("name") or name_hint))
                   or "forged_skill")
    attempts = 0
    telemetry: dict[str, Any] = {}

    while attempts <= max(0, int(max_repair_attempts)):
        attempts += 1
        _emit(progress, "VERIFY", f"attempt {attempts}: contract and safety rules")
        ok, reason = crucible.validate_ast(code)
        if not ok:
            _emit(progress, "REPAIR", f"attempt {attempts}: {reason[:90]}")
            repaired = _repair(code, reason, goal, config, attempts,
                               max_repair_attempts)
            if repaired is None:
                return {"ok": False, "name": actual_name, "provider": provider,
                        "message": f"The Crucible refused it: {reason}"}
            code = repaired
            continue

        deps = crucible.extract_dependencies(code)
        deps_ok, deps_msg = crucible.resolve_dependencies(deps, allow_install=bool(allow_install))
        if not deps_ok:
            return {"ok": False, "name": actual_name, "provider": provider,
                    "message": f"Dependency check failed: {deps_msg}"}

        _emit(progress, "VERIFY", f"attempt {attempts}: sandbox tests")
        passed, message, telemetry = crucible.run_sandbox_test(code, test_cases)
        if passed:
            break
        _emit(progress, "REPAIR", f"attempt {attempts}: {message[:90]}")
        repaired = _repair(code, message, goal, config, attempts, max_repair_attempts)
        if repaired is None:
            return {"ok": False, "name": actual_name, "provider": provider,
                    "message": f"The sandbox rejected it: {message}",
                    "telemetry": telemetry}
        code = repaired

    description = str(manifest.get("description") or "").strip()
    if not description:
        description = f"Forged capability that handles: {goal}"

    # The Crucible report — the same structured verdict `crucible.verify` returns,
    # assembled from the checks this run already performed (no second sandbox
    # execution). It is written into the sidecar and returned, so the Settings UI
    # and the registry can show the quality score and any hazard notes without
    # re-running anything.
    verification = {
        "ok": True,
        "attempts": attempts,
        "hazards": crucible.scan_hazards(code),
        "metrics": crucible.analyze_code(code),
        "contract": crucible.validate_output_contract(code)[0],
        "dependencies": crucible.extract_dependencies(code),
        "telemetry": telemetry,
        "verified_at": time.time(),
    }

    _emit(progress, "REGISTER", f"installing '{actual_name}' as a plugin")
    result = vault.install(
        name=actual_name, description=description, code=code, goal=goal,
        parameters=manifest.get("parameters") or {"type": "OBJECT", "properties": {}},
        triggers=manifest.get("triggers") or [],
        aliases=manifest.get("aliases") or [],
        test_cases=test_cases, manifest=manifest, directory=directory,
        hot_register=hot_register, verification=verification,
    )
    if not result.get("ok"):
        return {"ok": False, "name": actual_name, "provider": provider,
                "message": result.get("message") or "the skill could not be installed"}
    _emit(progress, "COMPLETE",
          f"'{result.get('name') or actual_name}' verified in {attempts} attempt(s)")
    registration = str(result.get("registration") or "hot")
    # Say what actually happened. A forge that could only land on disk must not
    # claim to be live: the user would then ask for the skill and be told it does
    # not exist while the log insisted it was running — the same class of
    # dishonesty as a fabricated progress message.
    if registration == "hot":
        outcome = "is live."
    elif registration == "next_launch":
        outcome = ("is saved and loads on the next launch — no live plugin "
                   "registry was available to register it into.")
    else:
        outcome = f"is installed ({registration})."
    return {
        "ok": True,
        "name": result.get("name") or actual_name,
        "path": result.get("path", ""),
        "provider": provider,
        "attempts": attempts,
        "registration": registration,
        "description": description,
        "message": (f"Skill '{result.get('name') or actual_name}' was forged with "
                    f"{PROVIDER_LABELS.get(provider, provider)}, verified in the "
                    f"Crucible and {outcome}"),
        "telemetry": telemetry,
        "skipped_providers": [row["provider"] for row in skipped_providers(config)],
        "verification": verification,
    }


def _repair(code: str, error: str, goal: str, config: dict | None,
            attempt: int, max_attempts: int) -> str | None:
    """Ask the model to fix a verification failure; None when no attempts remain."""
    if attempt > max(0, int(max_attempts)):
        return None
    prompt = (
        f"This J.A.R.V.I.S. plugin failed verification in the Crucible.\n"
        f"Original request: {goal}\n"
        f"Verification error: {error}\n\n"
        f"Broken code:\n{code}\n\n"
        "Fix it. Keep the same contract: `def run(parameters: dict, player=None, "
        "session_memory=None) -> str`, safe defaults for missing keys, no "
        "PLUGIN/FEATURE_METADATA block, no exception escaping. If the failure was "
        "inside the sandbox, make the failing path degrade to a spoken fallback "
        "sentence instead of raising.\n"
        'Return ONLY {"code": "<full corrected source>"} as JSON.'
    )
    try:
        raw, _provider = _ask_model(prompt, _SYSTEM, config)
        data = parse_json_response(raw)
        repaired = data.get("code")
        if isinstance(repaired, str) and repaired.strip():
            return repaired
    except Exception:
        return None
    return None
