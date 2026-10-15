"""Multimodal vision provider abstraction.

JARVIS is not coupled to a single vendor.  A :class:`VisionProvider` is a
thin adapter exposing the four operations the vision coordinator needs:

* ``analyze_image``          → arbitrary question about a frame
* ``describe_scene``         → free-form "what do you see"
* ``identify_object``        → "what is this thing"
* ``answer_visual_question`` → scene/counting/colour/semantic queries

Providers are selected via ``vision.multimodal_provider``.  Secrets come
from ``config/api_keys.json`` or ``JARVIS_*`` environment variables — never
from source code.  Every provider degrades gracefully: an unavailable
provider returns an error string instead of raising into JARVIS.
"""

from __future__ import annotations

import base64
import io
from typing import Optional, Tuple

from vision.config import VisionConfig, _env, _read_config
from vision.models import Rect
from vision.utils import logger

try:
    import numpy as np
    _NP = True
except Exception:  # pragma: no cover
    np = None  # type: ignore
    _NP = False


class VisionProviderError(RuntimeError):
    pass


class VisionProvider:
    """Base class + duck-typed protocol."""

    name = "base"

    def is_available(self) -> bool:
        return False

    def _jpeg_bytes(self, frame_bgr) -> bytes:
        from vision.utils import encode_jpeg
        return encode_jpeg(frame_bgr)

    # ── high level API (subclasses override at least one) ──────────────────

    def analyze_image(self, frame_bgr, question: str) -> str:
        raise NotImplementedError

    def describe_scene(self, frame_bgr) -> str:
        return self.analyze_image(
            frame_bgr,
            "Describe the scene in 2-4 concise English sentences. "
            "Name the objects, people, actions and where things are.",
        )

    def identify_object(self, frame_bgr, hint: str = "") -> str:
        return self.analyze_image(
            frame_bgr,
            "Identify the main object the person is holding or pointing at. "
            "Answer with a short name like 'a black wireless mouse'. "
            "If you cannot tell, say exactly 'I cannot identify this confidently.'",
        )

    def answer_visual_question(self, frame_bgr, question: str) -> str:
        return self.analyze_image(frame_bgr,
                                  f"Answer the user's question about the image concisely. "
                                  f"Question: {question}")

    # ── region-aware crop helper ────────────────────────────────────────────

    def analyze_region(self, frame_bgr, question: str,
                       region: Optional[Rect] = None) -> str:
        """Ask about a cropped sub-region of the frame.

        If ``region`` is None, behaves exactly like ``analyze_image``.
        """
        if region is None or not _NP or frame_bgr is None:
            return self.analyze_image(frame_bgr, question)
        try:
            import cv2
            h, w = frame_bgr.shape[:2]
            x0 = int(max(0, region.x * w))
            y0 = int(max(0, region.y * h))
            x1 = int(min(w, (region.x + region.w) * w))
            y1 = int(min(h, (region.y + region.h) * h))
            pad = max(1, int(0.12 * min(w, h)))
            x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
            x1 = min(w, x1 + pad); y1 = min(h, y1 + pad)
            crop = frame_bgr[y0:y1, x0:x1]
            if crop.size < 64:
                return self.analyze_image(frame_bgr, question)
            return self.analyze_image(crop, question)
        except Exception as exc:
            logger.warn(f"region analysis fell back to full frame: {exc}")
            return self.analyze_image(frame_bgr, question)


# ── Gemini ─────────────────────────────────────────────────────────────────

_GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"


class GeminiVisionProvider(VisionProvider):
    name = "gemini"

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._model = (cfg.vision_model or _GEMINI_DEFAULT_MODEL).strip()
        self._key = cfg.gemini_api_key
        self._client = None

    def _load_client(self):
        if self._client is not None:
            return self._client
        if not self._key:
            raise VisionProviderError(
                "Gemini API key is missing — add it to config/api_keys.json "
                "or set JARVIS_GEMINI_API_KEY.")
        try:
            from google import genai
            self._client = genai.Client(
                api_key=self._key,
                http_options={"api_version": "v1beta"},
            )
            return self._client
        except Exception as exc:
            raise VisionProviderError(f"Gemini SDK unavailable: {exc}")

    def is_available(self) -> bool:
        if not self._key:
            return False
        try:
            import google.genai  # noqa: F401
            return True
        except Exception:
            return False

    def analyze_image(self, frame_bgr, question: str) -> str:
        client = self._load_client()
        try:
            from google.genai import types as gtypes
            prompt = _stylize(question)
            response = client.models.generate_content(
                model=self._model,
                contents=[
                    gtypes.Part.from_bytes(data=self._jpeg_bytes(frame_bgr),
                                           mime_type="image/jpeg"),
                    prompt,
                ],
                config=gtypes.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=600,
                ),
            )
            text = (response.text or "").strip()
            if not text:
                raise VisionProviderError("Gemini returned an empty answer")
            return text
        except VisionProviderError:
            raise
        except Exception as exc:
            raise VisionProviderError(f"Gemini vision request failed: {exc}")


# ── OpenAI (vision-capable chat completions) ───────────────────────────────

class OpenAIVisionProvider(VisionProvider):
    name = "openai"

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._model = (cfg.vision_model or "gpt-4o-mini").strip()
        self._key = _env("OPENAI_API_KEY") or str(
            _read_config().get("openai_api_key") or "")
        self._base_url = _env("OPENAI_BASE_URL") or "https://api.openai.com/v1"

    def is_available(self) -> bool:
        return bool(self._key)

    def analyze_image(self, frame_bgr, question: str) -> str:
        if not self._key:
            raise VisionProviderError("OpenAI API key is missing.")
        try:
            import requests
            jpg = self._jpeg_bytes(frame_bgr)
            b64 = base64.b64encode(jpg).decode("ascii")
            payload = {
                "model": self._model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _stylize(question)},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
                "max_tokens": 600,
            }
            resp = requests.post(
                f"{self._base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"},
                json=payload, timeout=min(self._cfg.multimodal_timeout, 90),
            )
            if resp.status_code == 401:
                raise VisionProviderError("OpenAI rejected the API key (401).")
            if resp.status_code != 200:
                raise VisionProviderError(
                    f"OpenAI error {resp.status_code}: {resp.text[:200]}")
            text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if not text:
                raise VisionProviderError("OpenAI returned an empty answer")
            return text
        except VisionProviderError:
            raise
        except Exception as exc:
            raise VisionProviderError(f"OpenAI vision request failed: {exc}")


# ── Anthropic ──────────────────────────────────────────────────────────────

class AnthropicVisionProvider(VisionProvider):
    name = "anthropic"

    def __init__(self, cfg: VisionConfig):
        self._cfg = cfg
        self._model = (cfg.vision_model or "claude-3-5-sonnet-latest").strip()
        self._key = _env("ANTHROPIC_API_KEY") or str(
            _read_config().get("anthropic_api_key") or "")

    def is_available(self) -> bool:
        return bool(self._key)

    def analyze_image(self, frame_bgr, question: str) -> str:
        if not self._key:
            raise VisionProviderError("Anthropic API key is missing.")
        try:
            import requests
            jpg = self._jpeg_bytes(frame_bgr)
            b64 = base64.b64encode(jpg).decode("ascii")
            payload = {
                "model": self._model,
                "max_tokens": 600,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        }},
                        {"type": "text", "text": _stylize(question)},
                    ],
                }],
            }
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload, timeout=min(self._cfg.multimodal_timeout, 90),
            )
            if resp.status_code == 401:
                raise VisionProviderError("Anthropic rejected the API key (401).")
            if resp.status_code != 200:
                raise VisionProviderError(
                    f"Anthropic error {resp.status_code}: {resp.text[:200]}")
            blocks = resp.json().get("content", [])
            text = " ".join(b.get("text", "") for b in blocks
                            if b.get("type") == "text").strip()
            if not text:
                raise VisionProviderError("Anthropic returned an empty answer")
            return text
        except VisionProviderError:
            raise
        except Exception as exc:
            raise VisionProviderError(f"Anthropic vision request failed: {exc}")


# ── factory ────────────────────────────────────────────────────────────────

_PROVIDERS = {
    "gemini": GeminiVisionProvider,
    "openai": OpenAIVisionProvider,
    "openai_compatible": OpenAIVisionProvider,
    "anthropic": AnthropicVisionProvider,
    "claude": AnthropicVisionProvider,
}


def get_vision_provider(cfg: VisionConfig) -> VisionProvider:
    """Create the configured provider. Falls back to Gemini when the chosen
    provider is unknown or its SDK is missing — never returns None."""
    name = (cfg.multimodal_provider or "gemini").strip().lower()

    if cfg.local_only:
        return _UnavailableProvider(
            "local_only mode is enabled — frames are never sent to a vision API.")

    cls = _PROVIDERS.get(name)
    if cls is None:
        # "custom"/"ollama"/local endpoints default to the OpenAI format.
        if name in ("custom", "ollama", "lmstudio", "groq"):
            key = _env("OPENAI_API_KEY") or str(_read_config().get("openai_api_key") or "")
            if not key and name == "ollama":
                # Ollama runs locally without a key.
                base = _env("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
                return _OpenAICompatLocal(cfg, base_url=base)
            return OpenAIVisionProvider(cfg)
        logger.warn(f"unknown multimodal provider {name}; using gemini")
        return GeminiVisionProvider(cfg)

    try:
        return cls(cfg)
    except Exception as exc:
        logger.warn(f"provider {name} init failed ({exc}); falling back to gemini")
        try:
            return GeminiVisionProvider(cfg)
        except Exception:
            return _UnavailableProvider(str(exc))


class _OpenAICompatLocal(OpenAIVisionProvider):
    def __init__(self, cfg: VisionConfig, base_url: str):
        self._cfg = cfg
        self._model = (cfg.vision_model or "qwen2.5-vl").strip()
        self._base_url = base_url
        self._key = "ollama"

    def is_available(self) -> bool:
        return True


class _UnavailableProvider(VisionProvider):
    name = "unavailable"

    def __init__(self, reason: str = ""):
        self._reason = reason

    def is_available(self) -> bool:
        return False

    def _err(self) -> str:
        return self._reason or "No vision provider is available."

    def analyze_image(self, frame_bgr, question: str) -> str:
        return self._err()

    def describe_scene(self, frame_bgr) -> str:
        return self._err()

    def identify_object(self, frame_bgr) -> str:
        return self._err()

    def answer_visual_question(self, frame_bgr, question: str) -> str:
        return self._err()


def _stylize(question: str) -> str:
    """Wrap a user question with the JARVIS visual-analysis instruction."""
    q = " ".join(str(question or "").split()).strip() or "What do you see?"
    return (
        "You are JARVIS, a premium British AI assistant analysing a live "
        "webcam frame. Be concise (1-4 sentences), address the user as 'sir', "
        "and hedge uncertainty with phrases like 'it appears to be', 'it looks "
        "like', or 'I cannot identify this confidently'. Never pretend you "
        f"guarantee an identification. Task: {q}"
    )


__all__ = [
    "VisionProvider", "VisionProviderError", "get_vision_provider",
    "GeminiVisionProvider", "OpenAIVisionProvider", "AnthropicVisionProvider",
]