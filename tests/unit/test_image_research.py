"""``research_image`` — finding, ranking, downloading and showing a web image.

The pipeline has three stages and each one can fail on its own, so the tests
cover the happy path plus every degradation: no candidates, a model that cannot
choose, a candidate that will not download, and a URL that serves HTML instead
of a picture. In all of them the tool must return readable text and never raise,
because it runs inside a voice turn.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from actions import image_research as mod  # noqa: E402

PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def _png_payload(tag: bytes = b"a", size: int = 6000) -> bytes:
    """A payload that passes the module's real-image checks (magic + size)."""
    body = PNG_HEAD + tag
    return body + b"\x00" * max(0, size - len(body))


def _candidate(index: int, title: str = "picture") -> dict:
    return {
        "url": f"https://cdn.example.com/image_{index}.png",
        "title": f"{title} {index}",
        "source": f"https://example.com/page-{index}",
        "width": 800 + index,
        "height": 600 + index,
    }


class _FakeResponse:
    def __init__(self, payload: bytes, content_type: str = "image/png"):
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, size: int):
        for start in range(0, len(self._payload), size):
            yield self._payload[start:start + size]


class _FakePlayer:
    """The slice of ``ui.JarvisUI`` the tool uses."""

    def __init__(self):
        self.logs: list[str] = []
        self.shown: list[tuple[str, str]] = []

    def write_log(self, message) -> None:
        self.logs.append(str(message))

    def show_image_path(self, path, caption: str = "") -> None:
        self.shown.append((str(path), str(caption)))


@pytest.fixture()
def research_dir(tmp_path, monkeypatch):
    directory = tmp_path / "researched_images"
    monkeypatch.setattr(mod, "RESEARCH_DIR", directory)
    return directory


@pytest.fixture()
def candidates(monkeypatch):
    items = [_candidate(i) for i in range(4)]
    monkeypatch.setattr(mod, "_search_images", lambda query, count: list(items))
    return items


def _serve(monkeypatch, by_url: dict[str, bytes], content_type: str = "image/png"):
    """Install a ``requests.get`` that serves ``by_url`` (or raises KeyError)."""
    import requests

    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        payload = by_url.get(url)
        if payload is None:
            raise RuntimeError("404 not found")
        return _FakeResponse(payload, content_type)

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


# ── happy path ───────────────────────────────────────────────────────────────

def test_best_candidate_is_saved_shown_and_described(candidates, research_dir, monkeypatch):
    payloads = {c["url"]: _png_payload(str(i).encode()) for i, c in enumerate(candidates)}
    _serve(monkeypatch, payloads)
    monkeypatch.setattr(mod, "_rank_with_model", lambda *a, **k: {
        "index": 2, "why": "matches the subject exactly", "description": "a cyan arc reactor",
    })
    player = _FakePlayer()

    result = mod.image_research({"query": "arc reactor"}, player=player)

    assert "Best image for: arc reactor" in result
    assert "a cyan arc reactor" in result
    assert "matches the subject exactly" in result
    assert len(player.shown) == 1
    path, caption = player.shown[0]
    assert Path(path).read_bytes() == payloads[candidates[2]["url"]], "the chosen candidate is the one saved"
    assert path.endswith(".png")
    assert "a cyan arc reactor" in caption
    assert "arc reactor" in " ".join(player.logs)


def test_heuristic_pick_is_used_when_the_model_cannot_choose(candidates, research_dir, monkeypatch):
    payloads = {c["url"]: _png_payload(str(i).encode()) for i, c in enumerate(candidates)}
    _serve(monkeypatch, payloads)
    monkeypatch.setattr(mod, "_rank_with_model", lambda *a, **k: None)
    player = _FakePlayer()

    result = mod.image_research({"query": "arc reactor", "prefer": "photo"}, player=player)

    assert "Best image for: arc reactor" in result
    assert len(player.shown) == 1, "a heuristic pick must still be displayed"
    assert "best resolution and title match" in result


def test_prefer_hint_is_added_to_the_search_query(candidates, research_dir, monkeypatch):
    payloads = {c["url"]: _png_payload() for c in candidates}
    _serve(monkeypatch, payloads)
    monkeypatch.setattr(mod, "_rank_with_model", lambda *a, **k: None)
    seen: list[str] = []
    monkeypatch.setattr(mod, "_search_images", lambda query, count: seen.append(query) or list(candidates))

    mod.image_research({"query": "logo", "prefer": "wallpaper"}, player=_FakePlayer())

    assert "wallpaper" in seen[0]


# ── degradation paths ────────────────────────────────────────────────────────

def test_no_candidates_is_reported_clearly(monkeypatch, research_dir):
    monkeypatch.setattr(mod, "_search_images", lambda query, count: [])
    player = _FakePlayer()

    result = mod.image_research({"query": "a thing that does not exist"}, player=player)

    assert "could not find any image" in result
    assert player.shown == []
    assert any("could not find" in line for line in player.logs)


def test_a_failing_candidate_falls_through_to_the_next(candidates, research_dir, monkeypatch):
    payloads = {candidates[1]["url"]: _png_payload(b"second")}
    calls = _serve(monkeypatch, payloads)
    monkeypatch.setattr(mod, "_rank_with_model", lambda *a, **k: {
        "index": 0, "why": "looks right", "description": "the first choice",
    })
    player = _FakePlayer()

    result = mod.image_research({"query": "arc reactor"}, player=player)

    assert calls == [candidates[0]["url"], candidates[1]["url"]]
    assert len(player.shown) == 1
    assert Path(player.shown[0][0]).read_bytes() == payloads[candidates[1]["url"]]
    assert "could not be downloaded" not in result
    assert any("skipped" in line for line in player.logs)


def test_all_downloads_failing_is_reported(candidates, research_dir, monkeypatch):
    _serve(monkeypatch, {})                      # every URL raises
    monkeypatch.setattr(mod, "_rank_with_model", lambda *a, **k: None)
    player = _FakePlayer()

    result = mod.image_research({"query": "arc reactor"}, player=player)

    assert "none could be downloaded" in result
    assert "404 not found" in result, "the reason is reported back to the assistant"
    assert player.shown == []


def test_html_served_for_an_image_url_is_refused(research_dir, monkeypatch):
    import requests
    candidate = _candidate(0)
    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: _FakeResponse(b"<html>not an image</html>", "text/html"),
    )

    path, reason = mod._download(candidate, "arc reactor")

    assert path == "" and "not an image response" in reason


def test_tiny_and_oversized_payloads_are_refused(research_dir, monkeypatch):
    import requests
    candidate = _candidate(0)
    monkeypatch.setattr(requests, "get", lambda url, **kw: _FakeResponse(PNG_HEAD + b"\x00" * 20))
    _path, reason = mod._download(candidate, "arc reactor")
    assert "too small" in reason

    monkeypatch.setattr(
        requests, "get",
        lambda url, **kw: _FakeResponse(b"\x00" * (mod.MAX_DOWNLOAD_BYTES + 1), "image/png"),
    )
    _path, reason = mod._download(candidate, "arc reactor")
    assert "too large" in reason


def test_ranking_degrades_when_no_api_key_is_available(monkeypatch):
    from actions import web_search

    def _boom():
        raise RuntimeError("no api key configured")

    monkeypatch.setattr(web_search, "_get_api_key", _boom)
    assert mod._rank_with_model([_candidate(0)], "arc reactor", "photo") is None


def test_missing_query_asks_for_one():
    assert "what the image should show" in mod.image_research({}, player=_FakePlayer())


def test_candidates_without_a_usable_url_are_dropped():
    cleaned = mod._normalise_candidates(
        [{"image": "", "title": "no url"},
         {"image": "/relative.png", "title": "not absolute"},
         {"image": "https://cdn.example.com/ok.png", "title": "fine", "width": 10, "height": 10},
         {"image": "https://cdn.example.com/ok.png", "title": "duplicate"}],
        limit=10,
    )
    assert [c["title"] for c in cleaned] == ["fine"]


def test_result_text_lists_the_alternatives(candidates, research_dir, monkeypatch):
    payloads = {c["url"]: _png_payload() for c in candidates}
    _serve(monkeypatch, payloads)
    monkeypatch.setattr(mod, "_rank_with_model", lambda *a, **k: None)

    result = mod.image_research({"query": "arc reactor"}, player=_FakePlayer())

    assert "Other candidates you could use instead" in result
    assert candidates[3]["url"] in result
