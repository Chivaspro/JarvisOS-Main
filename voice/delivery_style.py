"""JARVIS voice-delivery style engine (Delivery panel extension).

Single owner of the extended voice-style settings: the parameter schema,
defaults, normalization, the Gemini Native Audio delivery-instruction
compiler and the session-temperature mapping.

Mechanism honesty (spec: never pretend a control is functional): the
installed google-genai Live API exposes only ``voice_name`` on the native
audio voice config — there are no DSP prosody fields. Every parameter here
therefore works through the two real mechanisms:

- ``prompt``   — a [VOICE STYLE] directive block compiled into the Live
  session's system_instruction (the same proven mechanism as the existing
  [VOICE DELIVERY] mode text). Applies at the next session (re)connect.
- ``temperature`` — the listed variance-style parameters additionally map
  onto the Live session's real ``temperature`` field, modulating how dynamic
  the rendered delivery is.

Pure functions over config dicts — no file I/O, no Qt imports — so the UI
and the voice pipeline can share one source of truth.
"""

from __future__ import annotations

from typing import Any

STYLE_KEY = "voice_style"          # nested dict in config/api_keys.json

# Sections in fixed display order (DELIVERY itself is the pre-existing panel
# controls: delivery mode + speed/pitch/volume — not redefined here).
SECTIONS: tuple[tuple[str, str], ...] = (
    ("character", "VOICE CHARACTER"),
    ("expression", "EXPRESSION"),
    ("speech", "SPEECH CONTROL"),
    ("advanced", "ADVANCED"),
    ("jarvis", "JARVIS MODE"),
)

# Parameters that additionally modulate the Live session temperature
# (variance-style controls). Everything else is prompt-only.
TEMPERATURE_KEYS = (
    "expressiveness", "emotion_intensity", "dramatic_intensity",
    "speaking_variation", "micro_variation", "stability",
)

# schema: key -> (section, label, default, low directive, high directive)
# Directives are imperatives for the delivery-instruction compiler; a
# parameter left in its neutral band compiles no directive at all.
VOICE_STYLE_PARAMS: dict[str, dict[str, Any]] = {
    # ── VOICE CHARACTER ───────────────────────────────────────────────────
    "tone":              ("character", "Voice tone", 55,
                          "Keep the tone measured and composed rather than energetic.",
                          "Bring steady, upbeat energy into the tone."),
    "warmth":            ("character", "Voice warmth", 65,
                          "Keep the timbre cool and reserved.",
                          "Add warmth and friendliness to the timbre."),
    "depth":             ("character", "Voice depth", 60,
                          "Keep the voice light and easy.",
                          "Give the voice more depth and gravitas."),
    "resonance":         ("character", "Voice resonance", 60,
                          "Keep the resonance soft and understated.",
                          "Let the voice resonate fully, chest-led and rich."),
    "brightness":        ("character", "Voice brightness", 50,
                          "Favour a darker, rounder timbre.",
                          "Favour a brighter, more forward timbre."),
    "richness":          ("character", "Voice richness", 60,
                          "Keep the voice thin and economical.",
                          "Make the voice full and rounded."),
    # ── EXPRESSION ────────────────────────────────────────────────────────
    "expressiveness":    ("expression", "Expressiveness", 45,
                          "Stay restrained and even in expression.",
                          "Be openly expressive in delivery."),
    "emotion_intensity": ("expression", "Emotion intensity", 40,
                          "Keep emotion subtle and contained.",
                          "Let emotion come through strongly."),
    "dramatic_intensity": ("expression", "Dramatic intensity", 35,
                          "Keep the delivery understated, never theatrical.",
                          "Deliver lines with cinematic drama."),
    "emphasis":          ("expression", "Emphasis", 60,
                          "Keep word stress even and level.",
                          "Stress key words clearly and deliberately."),
    "confidence":        ("expression", "Confidence", 70,
                          "Sound reserved and tentative where unsure.",
                          "Sound authoritative and assured."),
    "urgency":           ("expression", "Urgency", 30,
                          "Keep the pace relaxed and unhurried.",
                          "Convey urgency and momentum."),
    # ── SPEECH CONTROL ────────────────────────────────────────────────────
    "pause_length":      ("speech", "Pause length", 50,
                          "Keep pauses short and speech flowing.",
                          "Allow longer, deliberate pauses between thoughts."),
    "response_rhythm":   ("speech", "Response rhythm", 55,
                          "Answer rapidly and to the point.",
                          "Answer at a measured, considered pace."),
    "sentence_spacing":  ("speech", "Sentence spacing", 50,
                          "Keep sentences tightly connected.",
                          "Leave clear space between sentences."),
    "articulation":      ("speech", "Articulation", 65,
                          "Keep articulation relaxed and casual.",
                          "Articulate each word with precision."),
    "pronunciation_clarity": ("speech", "Pronunciation clarity", 70,
                          "Pronounce naturally, letting words flow together.",
                          "Pronounce with exceptional clarity."),
    "speaking_variation": ("speech", "Speaking variation", 55,
                          "Keep delivery consistent and even throughout.",
                          "Vary pacing and inflection dynamically."),
    # ── ADVANCED ──────────────────────────────────────────────────────────
    "breathiness":       ("advanced", "Breathiness", 20,
                          "Keep the voice clean, with no audible breath.",
                          "Let a soft, breathy quality into the voice."),
    "stability":         ("advanced", "Stability", 60,
                          "Allow the voice to shift freely with context.",
                          "Keep the voice consistent and stable."),
    "clarity":           ("advanced", "Clarity", 60,
                          "Keep the sound soft and rounded.",
                          "Keep the sound crisp and defined."),
    "vocal_energy":      ("advanced", "Vocal energy", 50,
                          "Keep vocal energy low and calm.",
                          "Project high vocal energy."),
    "naturalness":       ("advanced", "Naturalness", 75,
                          "A polished, deliberate delivery is acceptable.",
                          "Sound completely natural, like unhurried human speech."),
    "micro_variation":   ("advanced", "Micro-variation", 60,
                          "Keep the delivery static and uniform.",
                          "Add human-like micro-variation to every phrase."),
    # ── JARVIS MODE ───────────────────────────────────────────────────────
    "voice_presence":    ("jarvis", "Voice presence", 60,
                          "Keep the presence subtle and background.",
                          "Fill the room — a powerful, present voice."),
    "authority":         ("jarvis", "Authority", 65,
                          "Keep the register conversational and peer-like.",
                          "Speak with a commanding, senior-officer authority."),
    "composure":         ("jarvis", "Composure", 60,
                          "Let reaction show openly.",
                          "Stay impeccably calm and composed, whatever happens."),
    "intelligence_tone": ("jarvis", "Intelligence tone", 70,
                          "Keep explanations casual and light.",
                          "Sound precise and analytical when explaining."),
    "cinematic_delivery": ("jarvis", "Cinematic delivery", 35,
                          "Deliver naturally, without film-style pacing.",
                          "Deliver with cinematic weight and pacing."),
    "conversational_mode": ("jarvis", "Conversational mode", 55,
                          "Stay formal and professional.",
                          "Be relaxed and conversational in register."),
    "alert_mode":        ("jarvis", "Alert mode", 20,
                          "Announce alerts calmly.",
                          "Announce alerts with clear, controlled urgency."),
    "whisper_mode":      ("jarvis", "Whisper mode", 0,
                          "Speak at full, normal volume.",
                          "Drop to a soft, near-whisper delivery."),
    "adaptive_expression": ("jarvis", "Adaptive expression", 65,
                          "Keep delivery fixed regardless of context.",
                          "Adapt the delivery to the mood and content of each exchange."),
}

_DEFAULTS = {key: meta[2] for key, meta in VOICE_STYLE_PARAMS.items()}


def defaults() -> dict[str, int]:
    """The default value for every parameter (independent state)."""
    return dict(_DEFAULTS)


def normalize_style(raw: Any) -> dict[str, int]:
    """Clamp/validate a stored style dict; unknown keys and junk are dropped.

    Missing parameters fall back to their default, so a config saved by an
    older build still loads completely.
    """
    out: dict[str, int] = {}
    raw_map = raw if isinstance(raw, dict) else {}
    for key, meta in VOICE_STYLE_PARAMS.items():
        try:
            val = int(float(raw_map.get(key, meta[2])))
        except (TypeError, ValueError):
            val = meta[2]
        out[key] = max(0, min(100, val))
    return out


def style_from_config(cfg: dict | None) -> dict[str, int]:
    """The effective style for a configuration mapping (never raises)."""
    try:
        return normalize_style((cfg or {}).get(STYLE_KEY))
    except Exception:
        return defaults()


def mechanism(key: str) -> str:
    """How a parameter reaches Gemini Native Audio: 'prompt+temperature' | 'prompt'."""
    return "prompt+temperature" if key in TEMPERATURE_KEYS else "prompt"


def _band(value: int) -> str | None:
    """Neutral band (36–64) compiles no directive — keeps the prompt lean."""
    if value <= 35:
        return "low"
    if value >= 65:
        return "high"
    return None


def compile_prompt(cfg_or_style: dict | None, voice_mode: str = "") -> str:
    """Compile the style into a [VOICE STYLE] system-instruction block.

    Deterministic; returns '' when every parameter sits in its neutral band
    (the caller then appends nothing). ``cfg_or_style`` accepts either a full
    config mapping (reads the ``voice_style`` key) or a bare style dict.
    """
    src = cfg_or_style or {}
    style = normalize_style(src.get(STYLE_KEY, src) if STYLE_KEY in src else src)
    lines: list[str] = []
    for key, meta in VOICE_STYLE_PARAMS.items():
        band = _band(style[key])
        if band is None:
            continue
        phrase = meta[3] if band == "low" else meta[4]
        if phrase:
            lines.append(phrase)
    if not lines and not voice_mode:
        return ""
    head = "[VOICE STYLE]"
    if voice_mode:
        head += f"\nDelivery mode: {voice_mode}."
    if not lines:
        return head
    return head + "\n" + "\n".join(lines)


def temperature_for(cfg_or_style: dict | None) -> float | None:
    """Map the variance-style parameters onto the Live session temperature.

    Stability pulls toward a fixed low temperature; the expression/variation
    group pushes toward a dynamic high. Returns a value clamped to
    [0.4, 1.5]; never raises.
    """
    try:
        src = cfg_or_style or {}
        style = normalize_style(src.get(STYLE_KEY, src) if STYLE_KEY in src else src)
        variance = sum(style[k] for k in TEMPERATURE_KEYS if k != "stability")
        variance /= max(1, len(TEMPERATURE_KEYS) - 1)
        blended = 0.8 * variance + 0.2 * (100 - style["stability"])
        return round(0.4 + (blended / 100.0) * 1.1, 2)
    except Exception:
        return None
