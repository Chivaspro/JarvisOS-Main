"""Research the best real image on the web for a topic and show it in JARVIS.

The tool is a three-stage pipeline:

1. **Find** — DuckDuckGo image search (the same ``ddgs`` dependency the
   ``web_search`` action already uses) collects real image candidates with
   their titles, source pages and pixel sizes.
2. **Rank** — Gemini picks the best candidate and writes a short description.
   The ranking call is optional: if the model or the API key is unavailable the
   pipeline falls back to a deterministic score (resolution, image type, title
   overlap) so the tool still returns a sensible image.
3. **Show** — the winner is downloaded once, validated (real image payload,
   sane size), cached under ``researched_images/`` and handed to the UI through
   ``player.show_image_path``, which renders it in the title-less Image View
   panel. The return value is plain text the assistant can read out loud: what
   the image is, where it came from, and the runner-up options.

Nothing here raises into the caller: every stage degrades to the next one and
the failure is logged, because a research request must never take JARVIS down.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

MAX_DOWNLOAD_BYTES = 14 * 1024 * 1024      # never pull an unbounded file
MIN_DOWNLOAD_BYTES = 5 * 1024              # below this it is an icon or a teaser
MIN_CANDIDATES = 3
MAX_CANDIDATES = 12

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),
)

_PREFER_HINTS = {
    "photo": "photo",
    "photograph": "photo",
    "logo": "logo transparent png",
    "icon": "icon png",
    "wallpaper": "wallpaper 4k",
    "diagram": "diagram",
    "screenshot": "screenshot",
    "art": "artwork",
    "artwork": "artwork",
    "render": "3d render",
}


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
RESEARCH_DIR = BASE_DIR / "researched_images"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _log(player, message: str) -> None:
    """Best-effort log to the JARVIS activity window and the console."""
    try:
        if player is not None and hasattr(player, "write_log"):
            player.write_log(str(message))
    except Exception:
        pass
    try:
        print(f"[ImageResearch] {message}")
    except Exception:
        pass


def _slug(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", str(text or "image")).strip("_")
    return (clean[:60] or "image").lower()


def _normalise_candidates(raw: list, limit: int) -> list[dict]:
    """Keep only well-formed candidates, de-duplicated by image URL."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("image") or "").strip()
        if not url or url in seen or not url.lower().startswith(("http://", "https://")):
            continue
        seen.add(url)
        title = str(item.get("title") or "").strip()
        out.append({
            "url": url,
            "title": title or "untitled",
            "source": str(item.get("url") or "").strip(),
            "width": int(item.get("width") or 0) or 0,
            "height": int(item.get("height") or 0) or 0,
        })
        if len(out) >= limit:
            break
    return out


def _search_images(query: str, count: int) -> list[dict]:
    """DuckDuckGo image search through the shared ``ddgs`` dependency."""
    try:
        from actions.web_search import _get_ddgs
        DDGS = _get_ddgs()
    except Exception as exc:                     # pragma: no cover - import guard
        _log(None, f"image backend unavailable ({exc})")
        return []
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.images(query, max_results=max(count * 2, count + 4)))
    except Exception as exc:
        _log(None, f"image search failed: {exc}")
        return []
    return _normalise_candidates(raw, count)


def _heuristic_score(candidate: dict, query: str, prefer: str) -> float:
    """Deterministic ranking used when the model cannot choose."""
    score = 0.0
    area = candidate.get("width", 0) * candidate.get("height", 0)
    if area:
        score += min(area / 1_000_000.0, 6.0)            # prefer real resolution
    else:
        score += 0.5
    url = candidate["url"].lower()
    title = candidate["title"].lower()
    if prefer in ("photo", "wallpaper") and url.endswith(".svg"):
        score -= 2.0
    if prefer == "logo" and ("logo" in url or "logo" in title):
        score += 2.0
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    hits = sum(1 for w in words if w in title or w in url)
    if words:
        score += 2.5 * (hits / len(words))
    if url.endswith((".jpg", ".jpeg", ".png", ".webp")):
        score += 0.4
    return score


def _rank_with_model(candidates: list[dict], query: str, prefer: str) -> dict | None:
    """Ask Gemini which candidate best matches the request.

    Returns ``{"index": int, "why": str, "description": str}`` or ``None`` when
    the model is unavailable — the caller then keeps the heuristic pick.
    """
    listing = "\n".join(
        f"{i}. {c['title'][:90]} | {c['width']}x{c['height']} | {c['source'][:90]} | {c['url'][:150]}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        "You are choosing the single best image for a user request inside a desktop assistant.\n"
        f"User request: {query}\n"
        f"Preferred kind: {prefer}\n"
        "Candidates:\n"
        f"{listing}\n\n"
        "Reply with JSON only, no prose, using this shape:\n"
        '{"index": <0-based candidate number>, "why": "<why it is the best match, one short sentence>", '
        '"description": "<what the chosen image actually shows, one short sentence>"}'
    )
    try:
        from google import genai
        from actions.web_search import _get_api_key
        from core import model_router

        client = genai.Client(api_key=_get_api_key())
        response = model_router.generate(client, contents=prompt, role="fast")
        text = ""
        for part in getattr(response, "candidates", [])[0].content.parts:  # type: ignore[index]
            if getattr(part, "text", None):
                text += part.text
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        index = int(parsed.get("index", -1))
        if not 0 <= index < len(candidates):
            return None
        return {
            "index": index,
            "why": str(parsed.get("why") or "").strip(),
            "description": str(parsed.get("description") or "").strip(),
        }
    except Exception as exc:
        _log(None, f"ranking model unavailable ({exc}) — using heuristic pick")
        return None


def _detect_image_type(payload: bytes, url: str) -> str:
    """Return the real image suffix from the payload magic, never the URL."""
    for magic, suffix in _IMAGE_MAGIC:
        if payload.startswith(magic):
            if suffix == ".webp" and payload[8:12] != b"WEBP":
                continue
            return suffix
    return ""


def _download(candidate: dict, query: str) -> tuple[str, str]:
    """Download one candidate. Returns ``(path, "")`` or ``("", reason)``."""
    import requests

    url = candidate["url"]
    try:
        response = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (JARVIS image research)"},
            stream=True,
        )
        response.raise_for_status()
        declared = str(response.headers.get("Content-Type") or "").lower()
        if declared and not declared.startswith("image/"):
            return "", f"not an image response ({declared or 'unknown'})"
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                return "", "file is too large to preview safely"
            chunks.append(chunk)
    except Exception as exc:
        return "", f"download failed ({exc})"

    payload = b"".join(chunks)
    if len(payload) < MIN_DOWNLOAD_BYTES:
        return "", "file is too small to be a useful image"
    suffix = _detect_image_type(payload, url)
    if not suffix:
        return "", "payload is not a supported image format"

    try:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = RESEARCH_DIR / f"{_slug(query)}_{stamp}{suffix}"
        path.write_bytes(payload)
    except Exception as exc:
        return "", f"could not save the image ({exc})"
    return str(path), ""


def _caption(query: str, candidate: dict, description: str) -> str:
    """The caption Jarvis sees for this image — never painted on the panel."""
    bits = [description or candidate.get("title") or query]
    if candidate.get("title") and description and candidate["title"].lower() not in description.lower():
        bits.append(candidate["title"])
    if candidate.get("source"):
        bits.append(candidate["source"])
    caption = "  ·  ".join(b.strip() for b in bits if b and b.strip())
    return caption[:220]


def _format_result(query: str, chosen: dict, shown: str, description: str,
                   why: str, candidates: list[dict]) -> str:
    lines = [f"Best image for: {query}"]
    lines.append(f"- Showing: {description or chosen['title']}")
    if why:
        lines.append(f"- Why this one: {why}")
    if chosen.get("width") and chosen.get("height"):
        lines.append(f"- Resolution: {chosen['width']}x{chosen['height']}")
    if chosen.get("source"):
        lines.append(f"- Source page: {chosen['source']}")
    lines.append(f"- Image URL: {chosen['url']}")
    if shown:
        lines.append(f"- Saved locally at: {shown} (displayed in the Image View panel)")
    others = [c for c in candidates if c["url"] != chosen["url"]][:3]
    if others:
        lines.append("Other candidates you could use instead:")
        for other in others:
            lines.append(f"  * {other['title'][:70]} — {other['url'][:110]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def image_research(parameters: dict | None = None, response=None,
                   player=None, speak=None, session_memory=None) -> str:
    """Find the best web image for ``query`` and display it in the Image View."""
    params = parameters or {}
    query = str(params.get("query") or params.get("prompt") or "").strip()
    prefer = str(params.get("prefer") or "any").strip().lower()
    try:
        count = int(params.get("count") or 6)
    except (TypeError, ValueError):
        count = 6
    count = max(MIN_CANDIDATES, min(MAX_CANDIDATES, count))

    if not query:
        return "Please tell me what the image should show."

    hint = _PREFER_HINTS.get(prefer, "")
    search_query = f"{query} {hint}".strip() if hint and hint.lower() not in query.lower() else query
    _log(player, f"[Image] researching: {search_query}")

    candidates = _search_images(search_query, count)
    if not candidates:
        candidates = _search_images(query, count)
    if not candidates:
        message = f"I could not find any image for: {query}"
        _log(player, message)
        return message

    pick = _rank_with_model(candidates, query, prefer)
    if pick is None:
        ordered = sorted(candidates, key=lambda c: _heuristic_score(c, query, prefer), reverse=True)
        chosen, why, description = ordered[0], "best resolution and title match", ""
    else:
        chosen = candidates[pick["index"]]
        ordered = [chosen] + [c for c in candidates if c is not chosen]
        why, description = pick["why"], pick["description"]

    shown = ""
    last_reason = ""
    for candidate in ordered[:4]:
        path, reason = _download(candidate, query)
        if path:
            chosen, shown = candidate, path
            break
        last_reason = reason
        _log(player, f"[Image] skipped {candidate['url'][:70]} — {reason}")

    if not shown:
        detail = f" ({last_reason})" if last_reason else ""
        message = f"I found image results for {query} but none could be downloaded{detail}."
        _log(player, message)
        return message

    caption = _caption(query, chosen, description)
    try:
        if player is not None and hasattr(player, "show_image_path"):
            player.show_image_path(shown, caption)
        else:
            _log(player, "[Image] no display surface available — saved to disk only")
    except Exception as exc:
        _log(player, f"[Image] display failed: {exc}")

    _log(player, f"[Image] showing {Path(shown).name} — {caption[:90]}")
    return _format_result(query, chosen, shown, description, why, ordered)


if __name__ == "__main__":          # pragma: no cover - manual smoke run
    print(image_research({"query": " ".join(sys.argv[1:]) or "arc reactor"}))
