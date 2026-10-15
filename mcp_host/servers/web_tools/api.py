"""Tools for the Web Tools MCP Server."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from mcp_host.server import Tool

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 JARVIS-MCP/1.0"
)


def _fetch_webpage(args: dict) -> dict:
    """Fetch URL and extract clean text content."""
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "Missing 'url' argument"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    max_chars = max(500, min(int(args.get("max_length", 4000)), 20000))

    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    try:
        start_t = time.monotonic()
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_chars * 4)
            elapsed = round(time.monotonic() - start_t, 3)
            charset = resp.headers.get_content_charset() or "utf-8"
            html_text = raw.decode(charset, errors="replace")

        if BeautifulSoup:
            soup = BeautifulSoup(html_text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
                tag.decompose()
            text = soup.get_text(separator="\n")
        else:
            text = re.sub(r"<[^>]+>", " ", html_text)

        # Normalize excess whitespace
        clean_lines = [line.strip() for line in text.splitlines() if line.strip()]
        clean_text = "\n".join(clean_lines)
        if len(clean_text) > max_chars:
            clean_text = clean_text[:max_chars] + "\n...[truncated]"

        return {
            "url": url,
            "status": 200,
            "elapsed_seconds": elapsed,
            "content_type": content_type,
            "text": clean_text,
            "char_count": len(clean_text),
        }
    except Exception as exc:
        return {"error": f"Failed fetching webpage: {exc}", "url": url}


def _search_web(args: dict) -> dict:
    """Search the web using DuckDuckGo HTML and extract results."""
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "Missing 'query' argument"}
    max_results = max(1, min(int(args.get("max_results", 5)), 15))

    encoded_q = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{encoded_q}"
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})

    try:
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        results = []
        if BeautifulSoup:
            soup = BeautifulSoup(html, "html.parser")
            for result in soup.find_all("div", class_="result"):
                title_tag = result.find("a", class_="result__a")
                snippet_tag = result.find("a", class_="result__snippet")
                if title_tag:
                    raw_link = title_tag.get("href", "")
                    # Extract target URL if redirect
                    parsed = urllib.parse.urlparse(raw_link)
                    qs = urllib.parse.parse_qs(parsed.query)
                    target_url = qs.get("uddg", [raw_link])[0]
                    results.append({
                        "title": title_tag.get_text().strip(),
                        "url": target_url,
                        "snippet": snippet_tag.get_text().strip() if snippet_tag else "",
                    })
                if len(results) >= max_results:
                    break
        else:
            # Fallback regex search
            for match in re.finditer(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html):
                results.append({
                    "title": re.sub(r'<[^>]+>', '', match.group(2)).strip(),
                    "url": match.group(1),
                    "snippet": "",
                })
                if len(results) >= max_results:
                    break

        return {"query": query, "count": len(results), "results": results}
    except Exception as exc:
        return {"error": f"Search failed: {exc}", "query": query}


def _http_request(args: dict) -> dict:
    """Perform a custom HTTP request with method, headers, and body."""
    url = str(args.get("url", "")).strip()
    method = str(args.get("method", "GET")).upper().strip()
    if not url:
        return {"error": "Missing 'url' argument"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = dict(args.get("headers") or {})
    if "User-Agent" not in headers:
        headers["User-Agent"] = DEFAULT_UA

    body_data = None
    if method in ("POST", "PUT", "PATCH") and "body" in args:
        body = args["body"]
        if isinstance(body, dict):
            body_data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        else:
            body_data = str(body).encode("utf-8")

    req = urllib.request.Request(url, data=body_data, headers=headers, method=method)
    try:
        start_t = time.monotonic()
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            elapsed = round(time.monotonic() - start_t, 3)
            raw = resp.read(50000)
            charset = resp.headers.get_content_charset() or "utf-8"
            body_text = raw.decode(charset, errors="replace")
            response_headers = {k: v for k, v in resp.headers.items()}
            return {
                "status": resp.status,
                "elapsed_seconds": elapsed,
                "headers": response_headers,
                "body_preview": body_text[:4000],
            }
    except urllib.error.HTTPError as err:
        return {
            "status": err.code,
            "error": str(err.reason),
            "body": err.read(2000).decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return {"error": f"Request failed: {exc}"}


def _check_url_status(args: dict) -> dict:
    """Test URL endpoint reachability, latency, and response status code."""
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "Missing 'url' argument"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA}, method="HEAD")
    try:
        start_t = time.monotonic()
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            latency_ms = round((time.monotonic() - start_t) * 1000, 1)
            return {
                "ok": True,
                "url": url,
                "status": resp.status,
                "latency_ms": latency_ms,
                "server": resp.headers.get("Server", "Unknown"),
            }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


TOOLS: list[Tool] = [
    Tool(
        name="fetchWebpage",
        description="Fetch a web page by URL and extract clean text content.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Web URL to fetch"},
                "max_length": {"type": "integer", "description": "Max text characters to return (default 4000)"},
            },
            "required": ["url"],
        },
        handler=_fetch_webpage,
    ),
    Tool(
        name="searchWeb",
        description="Search the web using DuckDuckGo and return top search results.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results to return (default 5)"},
            },
            "required": ["query"],
        },
        handler=_search_web,
    ),
    Tool(
        name="httpRequest",
        description="Perform a custom HTTP request (GET, POST, PUT, DELETE) to an API endpoint.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target URL"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "HEAD"], "description": "HTTP method"},
                "headers": {"type": "object", "description": "HTTP request headers"},
                "body": {"type": "string", "description": "Request body data"},
            },
            "required": ["url"],
        },
        handler=_http_request,
    ),
    Tool(
        name="checkUrlStatus",
        description="Test endpoint reachability, HTTP status code, and latency in ms.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to check"},
            },
            "required": ["url"],
        },
        handler=_check_url_status,
    ),
]
