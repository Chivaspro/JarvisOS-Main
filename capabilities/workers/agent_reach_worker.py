# -*- coding: utf-8 -*-
"""Agent Reach worker — runs INSIDE the AgentReach virtual environment.

This script is spawned by capabilities/agent_reach.py with that environment's
own interpreter. It must never import J.A.R.V.I.S. code: the two interpreters are
different Python versions and J.A.R.V.I.S.'s dependency tree must stay intact.

Contract: read one JSON object on stdin, write one JSON object on the final
stdout line. All channel work is done with the upstream tools Agent Reach itself
routes to, so the research J.A.R.V.I.S. reports is real content, not a stub.

Actions
  doctor|status -> which channels are live and through which backend
  read     -> fetch one URL through the channel that owns it
  research -> search one or more channels for a query and return findings
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

# Keep stdout machine-readable regardless of the Windows code page.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                  line_buffering=True)
except Exception:
    pass

_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# A tool installed *into this virtualenv* (yt-dlp, mcporter shims) lives next to
# this interpreter and is invisible to shutil.which() unless the venv's bin dir
# is on PATH. Agent Reach's own doctor probes with which(), so without this the
# engine would honestly but wrongly report those channels as unavailable.
_BIN_DIR = os.path.dirname(os.path.abspath(sys.executable))
if _BIN_DIR not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _BIN_DIR + os.pathsep + os.environ.get("PATH", "")


# ── small helpers ─────────────────────────────────────────────────────────────

def _out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
    sys.exit(0 if payload.get("ok") else 1)


def _text(value, limit: int = 400) -> str:
    """One-line, length-capped view of anything (never raises)."""
    try:
        s = value if isinstance(value, str) else str(value or "")
    except Exception:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _shell(cmd, timeout: float = 60.0):
    """Run an upstream CLI. Returns (ok, stdout, stderr)."""
    if not cmd or not shutil.which(cmd[0]):
        return False, "", f"tool not installed: {cmd[0]}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=max(5.0, timeout), **_NO_WINDOW)
        return r.returncode == 0, (r.stdout or ""), (r.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {int(timeout)} s"
    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"


def _result(title, url="", snippet="", source="", backend=""):
    return {"title": _text(title, 200), "url": _text(url, 300),
            "snippet": _text(snippet, 400), "source": source, "backend": backend}


def _channels():
    from agent_reach.channels import ALL_CHANNELS
    return ALL_CHANNELS


def _channel(name: str):
    from agent_reach.channels import get_channel
    return get_channel(name)


def _config():
    from agent_reach.config import Config
    return Config(read_only=True)


def _doctor_map():
    """{channel_name: {status, message, active_backend}} from the real checker."""
    from agent_reach.doctor import check_all
    try:
        raw = check_all(_config())
    except Exception as exc:
        return {}, f"doctor failed: {type(exc).__name__}: {exc}"
    out = {}
    for name, info in (raw or {}).items():
        if isinstance(info, dict):
            out[name] = {
                "status": info.get("status", ""),
                "message": _text(info.get("message"), 220),
                "active_backend": info.get("active_backend"),
                "tier": info.get("tier"),
            }
    return out, ""


# ── per-channel fetchers ──────────────────────────────────────────────────────
# Each fetcher calls the backend Agent Reach documents for that channel and
# normalises the output. A missing upstream tool is reported, never faked.

def fetch_exa_search(query, limit, timeout):
    cmd = ["mcporter", "call", "exa.web_search_exa",
           f"query={query}", f"numResults={limit}"]
    ok, so, se = _shell(cmd, timeout)
    if not ok:
        return None, _text(se or so)
    return _parse_jsonish(so), "Exa via mcporter"


def fetch_github(query, limit, timeout):
    cmd = ["gh", "search", "repos", query, "--sort", "stars",
           "--limit", str(limit), "--json", "fullName,description,url,stargazersCount"]
    ok, so, se = _shell(cmd, timeout)
    if ok:
        return _parse_jsonish(so), "gh CLI"
    # Retry chain: unauthenticated gh can still search through the API path.
    cmd2 = ["gh", "search", "repos", query, "--limit", str(limit)]
    ok2, so2, se2 = _shell(cmd2, timeout)
    if ok2:
        return _parse_lines(so2), f"gh CLI (plain output; json attempt: {_text(se)}"
    return None, _text(se2 or se)


def fetch_youtube(query, limit, timeout):
    """yt-dlp is installed in this environment; use it as a module."""
    try:
        import yt_dlp
    except Exception as exc:
        return None, f"yt-dlp unavailable: {exc}"
    opts = {"quiet": True, "skip_download": True, "extract_flat": True,
            "playlistend": max(1, limit)}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        entries = (info or {}).get("entries") or []
        out = []
        for e in entries:
            if not e:
                continue
            out.append(_result(
                e.get("title"), e.get("url") or e.get("webpage_url") or "",
                _text(e.get("description"), 240), "youtube", "yt-dlp"))
        return out, "yt-dlp"
    except Exception as exc:
        return None, f"yt-dlp search failed: {type(exc).__name__}: {exc}"


def fetch_web(query, limit, timeout):
    """Jina Reader through Agent Reach's own WebChannel.read()."""
    ch = _channel("web")
    if ch is None:
        return None, "web channel not registered"
    url = query if str(query).lower().startswith(("http://", "https://")) else ""
    if not url:
        return None, "web channel needs a URL"
    try:
        body = ch.read(url)
        return {"text": _text(body, 20000), "url": url}, "Jina Reader"
    except Exception as exc:
        return None, f"web read failed: {type(exc).__name__}: {exc}"


def fetch_rss(query, limit, timeout):
    try:
        import feedparser
    except Exception as exc:
        return None, f"feedparser unavailable: {exc}"
    if not str(query).lower().startswith(("http://", "https://")):
        return None, "rss channel needs a feed URL"
    try:
        parsed = feedparser.parse(query)
        entries = []
        for e in (parsed.entries or [])[:max(1, limit)]:
            entries.append(_result(
                e.get("title"), e.get("link"),
                _text(e.get("summary"), 300), "rss", "feedparser"))
        return entries, "feedparser"
    except Exception as exc:
        return None, f"rss parse failed: {exc}"


def fetch_v2ex(query, limit, timeout):
    ch = _channel("v2ex")
    if ch is None:
        return None, "v2ex channel not registered"
    try:
        if query:
            items = ch.search(str(query), limit=max(1, limit)) or []
        else:
            items = ch.get_hot_topics(limit=max(1, limit)) or []
        out = []
        for e in items:
            if isinstance(e, dict):
                out.append(_result(e.get("title"), e.get("url") or e.get("href"),
                                   _text(e.get("content"), 240), "v2ex", "built-in"))
        return out, "built-in"
    except Exception as exc:
        return None, f"v2ex failed: {type(exc).__name__}: {exc}"


def fetch_opencli(site, query, limit, timeout):
    cmd = ["opencli", site, "search", str(query), "-f", "yaml"]
    ok, so, se = _shell(cmd, timeout)
    if not ok:
        return None, _text(se or so)
    return _parse_lines(so), "OpenCLI"


def fetch_cli(cmd, source_label, timeout):
    ok, so, se = _shell(cmd, timeout)
    if not ok:
        return None, _text(se or so)
    return _parse_jsonish(so) or _parse_lines(so), source_label


def _parse_jsonish(raw: str):
    """Best-effort extraction of records from CLI output of any shape."""
    text = (raw or "").strip()
    if not text:
        return []
    # Whole payload, or a fenced/indented JSON block inside prose.
    for candidate in (text, _between_braces(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        return _records(data)
    return _parse_lines(text)


def _between_braces(text: str) -> str:
    start = text.find("[")
    brace = text.find("{")
    if start == -1 and brace == -1:
        return ""
    idx = start if (start != -1 and (brace == -1 or start < brace)) else brace
    return text[idx:]


def _records(data) -> list:
    """Flatten whatever structure a CLI returned into result dicts."""
    if isinstance(data, dict):
        for key in ("results", "data", "items", "repos", "entries", "hits",
                    "content", "videos", "posts"):
            if isinstance(data.get(key), list):
                return _records(data[key])
        return [_from_dict(data)]
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append(_from_dict(item))
            elif isinstance(item, str) and item.strip():
                out.append(_result(item, "", "", "", ""))
        return out
    return []


_TITLE_KEYS = ("title", "fullName", "name", "repo", "headline", "wording")
_URL_KEYS = ("url", "link", "href", "webpage_url", "html_url", "homeUrl")
_TEXT_KEYS = ("description", "snippet", "summary", "text", "content", "abstract",
              "highlight", "body")


def _from_dict(d: dict) -> dict:
    title = next((d[k] for k in _TITLE_KEYS if d.get(k)), "")
    url = next((d[k] for k in _URL_KEYS if d.get(k)), "")
    body = next((d[k] for k in _TEXT_KEYS if d.get(k)), "")
    return _result(title, url, body, "", "")


def _parse_lines(raw: str) -> list:
    """Plain-text CLI output: one finding per non-empty line."""
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or set(s) <= {"-", "=", "*", " "}:
            continue
        m = re.search(r"https?://\S+", s)
        out.append(_result(re.sub(r"https?://\S+", "", s).strip(" -•\t"),
                           m.group(0) if m else "", s, "", ""))
    return out


def fetch_reddit(query, limit, timeout):
    """Documented retry chain: OpenCLI first, then the legacy rdt-cli."""
    data, backend = fetch_opencli("reddit", query, limit, timeout)
    if data:
        return data, backend
    first_try = _text(backend, 160)
    data2, backend2 = fetch_cli(["rdt", "search", str(query), "--limit", str(limit)],
                                "rdt-cli", timeout)
    if data2:
        return data2, backend2
    return None, f"opencli reddit: {first_try}; rdt: {_text(backend2, 160)}"


# ── routing ───────────────────────────────────────────────────────────────────

#: channel -> (search fetcher, read fetcher or None)
SEARCH_DISPATCH = {
    "exa_search": lambda q, n, t: fetch_exa_search(q, n, t),
    "github": lambda q, n, t: fetch_github(q, n, t),
    "youtube": lambda q, n, t: fetch_youtube(q, n, t),
    "rss": lambda q, n, t: fetch_rss(q, n, t),
    "v2ex": lambda q, n, t: fetch_v2ex(q, n, t),
    "reddit": lambda q, n, t: fetch_reddit(q, n, t),
    "xiaohongshu": lambda q, n, t: fetch_opencli("xiaohongshu", q, n, t),
    "facebook": lambda q, n, t: fetch_opencli("facebook", q, n, t),
    "instagram": lambda q, n, t: fetch_opencli("instagram", q, n, t),
    "bilibili": lambda q, n, t: fetch_cli(["bili", "search", str(q), "--type",
                                           "video", "-n", str(n)], "bili CLI", t),
    "twitter": lambda q, n, t: fetch_cli(["twitter", "search", str(q), "-n",
                                          str(n)], "twitter-cli", t),
}

PLATFORM_ALIASES = {
    "web": "exa_search", "internet": "exa_search", "search": "exa_search",
    "code search": "exa_search", "x": "twitter", "gh": "github",
    "huggingface": "web_read", "news": "rss", "podcast": "xiaoyuzhou",
}


def _pick_channels(req: dict, live: dict) -> list:
    """Which channels to try, in order, for this request.

    An explicitly requested platform is attempted even when doctor reports it
    off, because the reference retry chain may still reach it and the user must
    hear the real failure rather than a silent skip. Auto routing only walks
    channels doctor currently reports as usable, then falls back to whatever is
    active so a request is never dropped because of a stale override.
    """
    platform = _text(req.get("platform"), 40).lower()
    platform = PLATFORM_ALIASES.get(platform, platform)
    url = _text(req.get("url"), 500)
    explicit = bool(platform and platform != "auto")
    allow = {c.strip().lower() for c in str(req.get("channels_allow") or "").split(",") if c.strip()}

    wanted: list = []
    if explicit:
        wanted.append(platform)
    if url:
        for ch in _channels():
            try:
                if ch.can_handle(url):
                    wanted.append(ch.name)
                    break
            except Exception:
                continue
    if not wanted:
        # Broad research: semantic web search first, then real-code/GitHub,
        # then video — the documented agent-reach research combination.
        wanted = ["exa_search", "github", "youtube", "web"]

    if allow:
        allowed = [w for w in wanted if w in allow]
        wanted = allowed or list(allow)

    if explicit:
        return wanted

    usable = []
    for name in wanted:
        if name == "web":
            usable.append(name)          # Jina fallback path, always reachable
            continue
        info = live.get(name) or {}
        if info.get("active_backend") or info.get("status") in ("ok", "warn"):
            usable.append(name)
    if not usable:
        # Nothing on the preferred list is live: try every channel doctor itself
        # reports as active, in doctor's order.
        usable = [k for k, v in live.items() if v.get("active_backend")][:4] or ["web"]
    return usable


def do_research(req: dict) -> dict:
    live, doctor_error = _doctor_map()
    query = _text(req.get("query"), 400)
    url = _text(req.get("url"), 500)
    limit = int(req.get("max_results") or 8)
    timeout = float(req.get("timeout") or 90)

    if not query and not url:
        return {"ok": False, "error": "No research query or URL was provided."}

    findings, attempts, used = [], [], []
    for name in _pick_channels(req, live):
        if url and name == "web":
            data, backend = fetch_web(url, limit, timeout)
            attempts.append({"channel": name, "ok": data is not None,
                             "detail": backend if data is None else ""})
            if data:
                text = data.get("text", "")
                findings.append(_result(f"Page: {url}", url, text[:1200],
                                        "web", backend))
                used.append(f"web/{backend}")
                break
            continue
        fetcher = SEARCH_DISPATCH.get(name)
        if fetcher is None:
            continue
        q = query or url
        data, detail = fetcher(q, limit, timeout)
        ok = bool(data)
        attempts.append({"channel": name, "ok": ok, "detail": "" if ok else _text(detail, 200)})
        if not ok:
            continue
        records = data if isinstance(data, list) else []
        for r in records[:limit]:
            r["source"] = r.get("source") or name
            r["backend"] = r.get("backend") or detail
            findings.append(r)
        used.append(f"{name}/{detail}")
        if len(findings) >= max(3, limit // 2):
            break

    return {
        "ok": bool(findings),
        "action": "research",
        "query": query or url,
        "summary": _summarise(findings, used, attempts, query or url),
        "data": {
            "findings": findings[: limit * 3],
            "channels_used": used,
            "attempts": attempts,
            "doctor_error": doctor_error,
            "live_channels": {k: v.get("active_backend") for k, v in live.items()
                              if v.get("active_backend")},
        },
    }


def _summarise(findings, used, attempts, query) -> str:
    if not findings:
        blocked = "; ".join(f"{a['channel']}: {a['detail'] or 'unavailable'}"
                           for a in attempts if not a["ok"])
        return (f"Agent Reach returned nothing for '{query}'. "
                f"{'Tried: ' + blocked if blocked else 'No channel could be reached.'}")
    head = f"Agent Reach found {len(findings)} result(s) for '{query}'"
    if used:
        head += " via " + ", ".join(used)
    return head


def do_read(req: dict) -> dict:
    url = _text(req.get("url"), 500)
    if not url:
        return {"ok": False, "error": "read needs a url"}
    data, backend = fetch_web(url, int(req.get("max_results") or 8),
                              float(req.get("timeout") or 90))
    if not data:
        return {"ok": False, "error": _text(backend, 300), "summary": _text(backend, 300)}
    text = data.get("text", "") if isinstance(data, dict) else str(data)
    return {"ok": True, "action": "read",
            "summary": f"Read {url} via {backend} ({len(text)} chars).",
            "data": {"text": text, "url": url, "backend": backend}}


def do_doctor(req: dict) -> dict:
    live, doctor_error = _doctor_map()
    try:
        from agent_reach import __version__ as ver
    except Exception:
        ver = ""
    live_names = [k for k, v in live.items() if v.get("active_backend")]
    return {
        "ok": not doctor_error,
        "action": "doctor",
        "engine_version": ver,
        "summary": (f"Agent Reach {ver}: {len(live_names)} of {len(live)} "
                    f"channels report an active backend"
                    + (f" — doctor error: {doctor_error}" if doctor_error else "")),
        "data": {"channels": live, "active": live_names},
    }


def main() -> None:
    raw = sys.stdin.read()
    try:
        req = json.loads(raw or "{}")
    except Exception as exc:
        _out({"ok": False, "error": f"invalid request json: {exc}",
              "summary": "Bad request."})
        return

    started = time.monotonic()
    action = _text(req.get("action"), 40).strip().lower() or "research"
    if action == "status":
        action = "doctor"          # same question, same honest answer
    try:
        if action == "doctor":
            result = do_doctor(req)
        elif action == "read":
            result = do_read(req)
        else:
            result = do_research(req)
    except Exception as exc:
        result = {"ok": False,
                  "error": f"{type(exc).__name__}: {exc}",
                  "summary": f"Agent Reach worker failed: {exc}"}
    result["elapsed_s"] = round(time.monotonic() - started, 2)
    result["engine"] = "agent_reach"
    _out(result)


if __name__ == "__main__":
    main()
