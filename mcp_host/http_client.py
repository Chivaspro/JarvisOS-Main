"""Streamable HTTP: one POST per message, JSON or request-scoped SSE back.

This binding follows the **current MCP specification (2026-07-28) first**:

* the protocol core is stateless — there is no ``initialize`` handshake and no
  ``Mcp-Session-Id``; every request describes itself by carrying its protocol
  version, client identity and capabilities in ``_meta``;
* method and tool names travel in the ``Mcp-Method`` / ``Mcp-Name`` HTTP
  headers so an intermediary can route on them without reading the body (the
  body stays the source of truth);
* a reply arrives as a JSON object or as a request-scoped SSE stream;
* optionally, ``server/discover`` is called once to learn the server's
  capabilities up front.

Servers that still speak an earlier revision are the majority in the field, so
the transport **detects the era instead of assuming one**: if the modern
``server/discover`` call (or a plain list call) comes back as "method not
found", it falls back to the session-based flow of 2025-06-18 (``initialize`` →
``notifications/initialized`` → the ``Mcp-Session-Id`` echoed on every later
request), and remembers the era for the rest of the session.

Security posture for remote servers:

* HTTPS is required unless the user explicitly allows plain HTTP for that
  server;
* credentials are never forwarded across a redirect to a different host;
* a TLS failure, a refused key, a wrong endpoint, a rate limit and a dead
  network are five different sentences, because the user must act differently
  on each.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import urlparse

import requests

from mcp_host import protocol

__all__ = ["StreamableHttpTransport"]

#: The mirror headers the current specification requires on every request.
_PROTOCOL_HEADER = "MCP-Protocol-Version"
_METHOD_HEADER = "Mcp-Method"
_NAME_HEADER = "Mcp-Name"
_SESSION_HEADER = "Mcp-Session-Id"

_ACCEPT = "application/json, text/event-stream"

#: Bound the error body quoted back to the user: a 400 from a proxy can carry a
#: whole HTML page, and a sentence is what the UI needs.
_BODY_SNIPPET = 300


def _looks_like_no_such_method(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in (
        "not found", "not implemented", "not supported", "unknown method",
        "method_not_found", "no such method", "does not implement",
        "unrecognized method", "unsupported method"))


class StreamableHttpTransport:
    """A remote MCP server over HTTP, modern-first with a legacy fallback."""

    def __init__(self, name: str, url: str, *, headers: dict | None = None,
                 timeout: float = 60.0, start_timeout: float = 20.0,
                 allow_insecure_http: bool = False,
                 protocol_override: str = "") -> None:
        self.name = str(name or "mcp")
        self.url = str(url or "").strip()
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.timeout = float(timeout)
        self.start_timeout = float(start_timeout)
        self.allow_insecure_http = bool(allow_insecure_http)
        self.protocol_override = str(protocol_override or "").strip().lower()

        self._era = "modern"
        self._session_id = ""
        self._running = False
        self._dead = False
        self._error = ""
        self._server_info: dict = {}
        self._capabilities: dict = {}
        self._instructions = ""
        self._protocol_version = protocol.MODERN_PROTOCOL_VERSION
        self._next_id = 1
        self._lock = threading.Lock()
        self._cancel_ids: set[int] = set()
        #: Server-initiated notifications seen on an SSE stream, kept bounded.
        self.notifications: list[dict] = []

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def era(self) -> str:
        return self._era

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        problem = self._validate_url()
        if problem:
            self._fail(problem)
            return False
        self._running = True
        if self.protocol_override in ("legacy", "2025", "session"):
            return self._start_legacy()
        if self.protocol_override in ("modern", "stateless", "2026"):
            self._era = "modern"
            self._protocol_version = protocol.MODERN_PROTOCOL_VERSION
            self._discover()          # best effort; a failure is not fatal
            return True
        # Auto: try the modern discovery call first; fall back to the legacy
        # handshake only when the server says it does not implement it.
        try:
            self._request_modern("server/discover", {}, timeout=self.start_timeout,
                                 allow_fallback=False)
            self._era = "modern"
            self._protocol_version = protocol.MODERN_PROTOCOL_VERSION
            self._capture_discovery(self._last_result)
            return True
        except protocol.JsonRpcError as exc:
            if _looks_like_no_such_method(str(exc)):
                return self._start_legacy()
            # An auth failure or a dead network is reported as the real problem:
            # a legacy fallback would send the same credentials to the same host
            # and receive the same refusal.
            self._fail(str(exc))
            return False

    def _start_legacy(self) -> bool:
        try:
            message = self._post(protocol.request(
                "initialize", {
                    "protocolVersion": protocol.LEGACY_PROTOCOL_VERSIONS[0],
                    "capabilities": {},
                    "clientInfo": {"name": "jarvis-os", "version": "4.0"},
                }, self._ident()), timeout=self.start_timeout)
        except protocol.JsonRpcError as exc:
            self._fail(str(exc))
            return False
        if isinstance(message, dict) and message.get("error"):
            self._fail(protocol.error_message(message))
            return False
        result = (message or {}).get("result") or {}
        self._era = "legacy"
        self._protocol_version = str(result.get("protocolVersion")
                                     or protocol.LEGACY_PROTOCOL_VERSIONS[0])
        self._server_info = dict(result.get("serverInfo") or {})
        self._capabilities = dict(result.get("capabilities") or {})
        self._instructions = str(result.get("instructions") or "")
        try:
            self.notify("notifications/initialized")
        except protocol.JsonRpcError:
            pass
        return True

    def _discover(self) -> None:
        """Best-effort ``server/discover``; a failure leaves the capability map
        empty, which the discovery calls below handle by simply trying them."""
        try:
            self._request_modern("server/discover", {}, timeout=self.start_timeout,
                                 allow_fallback=False)
            self._capture_discovery(self._last_result)
        except protocol.JsonRpcError:
            pass

    def _capture_discovery(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        self._server_info = dict(result.get("serverInfo") or {})
        self._capabilities = dict(result.get("capabilities") or {})
        self._instructions = str(result.get("instructions") or "")
        version = str(result.get("protocolVersion") or "").strip()
        if version:
            self._protocol_version = version

    def close(self, reason: str = "") -> None:
        was_running = self._running
        self._running = False
        self._dead = True
        if was_running and self._era == "legacy" and self._session_id:
            # The legacy binding terminates a session with DELETE. Best effort:
            # a server that will not answer must not hold up shutdown.
            try:
                requests.delete(self.url, headers=self._headers("notifications/cancelled"),
                                timeout=5.0, allow_redirects=False)
            except Exception:
                pass
        self._error = self._error or (f"the {self.name} server was stopped"
                                      + (f" ({reason})" if reason else ""))

    # ── requests ──────────────────────────────────────────────────────────────

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None,
                request_id: int | None = None) -> dict:
        ident = self._ident() if request_id is None else int(request_id)
        if self._era == "legacy":
            message = self._post(protocol.request(method, params, ident),
                                 method=method, params=params,
                                 timeout=timeout or self.timeout,
                                 request_id=ident)
            if "error" in (message or {}):
                raise protocol.JsonRpcError(protocol.error_message(message))
            return message
        return self._request_modern(method, params, timeout or self.timeout,
                                    request_id=ident)

    def notify(self, method: str, params: dict | None = None) -> None:
        body = protocol.notification(method, params)
        if self._era == "modern":
            body["params"] = self._with_meta(params)
        self._post(body, method=method, params=params,
                   timeout=self.timeout, expect_reply=False)

    def cancel(self, request_id: int) -> None:
        with self._lock:
            self._cancel_ids.add(int(request_id))

    # ── internals ─────────────────────────────────────────────────────────────

    _last_result: Any = None

    def _ident(self) -> int:
        with self._lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _validate_url(self) -> str:
        if not self.url:
            return "no server URL is configured"
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https"):
            return (f"'{self.url}' is not an HTTP URL — a Streamable HTTP server "
                    f"needs an http:// or https:// endpoint")
        if parsed.scheme == "http" and not self.allow_insecure_http:
            return (f"{self.url} is plain HTTP, so the connection would not be "
                    f"encrypted. Allow insecure HTTP for this server if that is "
                    f"really what you want, or use an https:// endpoint.")
        if not parsed.netloc:
            return f"'{self.url}' has no host"
        return ""

    def _with_meta(self, params: dict | None) -> dict:
        out = dict(params or {})
        if "_meta" not in out:
            out["_meta"] = {
                protocol.META_PROTOCOL_VERSION: protocol.MODERN_PROTOCOL_VERSION,
                protocol.META_CLIENT_INFO: {"name": "jarvis-os", "version": "4.0"},
                protocol.META_CLIENT_CAPABILITIES: {},
            }
        return out

    def _name_for(self, method: str, params: dict | None) -> str:
        """The addressed name for the ``Mcp-Name`` mirror header, when there is
        one: the tool/prompt name, or the resource URI."""
        if not isinstance(params, dict):
            return ""
        for key in ("name", "uri"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _headers(self, method: str, params: dict | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
            _PROTOCOL_HEADER: (protocol.MODERN_PROTOCOL_VERSION
                               if self._era == "modern"
                               else (self._protocol_version
                                     or protocol.LEGACY_PROTOCOL_VERSIONS[0])),
            _METHOD_HEADER: str(method or ""),
        }
        name = self._name_for(method, params)
        if name:
            headers[_NAME_HEADER] = name
        if self._era == "legacy" and self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        headers.update(self.headers)
        return headers

    def _request_modern(self, method: str, params: dict | None, *,
                        timeout: float, allow_fallback: bool = True,
                        request_id: int | None = None) -> dict:
        if request_id is None:
            request_id = self._ident()
        body = protocol.request(method, self._with_meta(params), request_id)
        try:
            message = self._post(body, method=method, params=params,
                                 timeout=timeout, request_id=request_id)
        except protocol.JsonRpcError as exc:
            if allow_fallback and _looks_like_no_such_method(str(exc)):
                # A legacy server that never got its handshake: adopt the old
                # flow and retry this request through it.
                if self._start_legacy():
                    return self.request(method, params, timeout=timeout)
            raise
        if isinstance(message, dict) and "error" in message:
            raise protocol.JsonRpcError(protocol.error_message(message))
        self._last_result = (message or {}).get("result")
        return message or {}

    def _post(self, body: dict, *, method: str = "", params: dict | None = None,
              timeout: float | None = None, expect_reply: bool = True,
              request_id: Any = None) -> dict:
        if not self._running:
            raise protocol.JsonRpcError(
                f"the {self.name} server has not been connected")
        headers = self._headers(method, params)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        attempt_url = self.url
        redirects = 0
        while True:
            try:
                response = requests.post(
                    attempt_url, data=payload.encode("utf-8"), headers=headers,
                    timeout=float(timeout or self.timeout), stream=True,
                    allow_redirects=False)
            except requests.exceptions.SSLError as exc:
                raise protocol.JsonRpcError(
                    f"the {self.name} server's TLS certificate could not be "
                    f"verified: {exc}") from exc
            except (requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout) as exc:
                raise protocol.JsonRpcError(
                    f"the {self.name} server did not answer within "
                    f"{float(timeout or self.timeout):.0f}s") from exc
            except requests.exceptions.ConnectionError as exc:
                raise protocol.JsonRpcError(
                    f"the {self.name} server could not be reached at "
                    f"{self.url}: {exc}") from exc
            except requests.exceptions.RequestException as exc:
                raise protocol.JsonRpcError(
                    f"the {self.name} server refused the request: {exc}") from exc

            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get("Location") or ""
                _close(response)
                if not target:
                    raise protocol.JsonRpcError(
                        f"the {self.name} server redirected without saying where")
                if _same_origin(self.url, target):
                    attempt_url, redirects = target, redirects + 1
                    if redirects <= 3:
                        continue
                    raise protocol.JsonRpcError(
                        f"the {self.name} server redirected too many times")
                if self.headers:
                    # Credentials must not follow a redirect to another host.
                    raise protocol.JsonRpcError(
                        f"the {self.name} server redirected to a different host "
                        f"({target}); I will not send your credentials there. "
                        f"Use that server's own URL instead.")
                attempt_url, redirects = target, redirects + 1
                if redirects <= 3:
                    continue
                raise protocol.JsonRpcError(
                    f"the {self.name} server redirected too many times")

            if response.status_code >= 400:
                detail = _read_snippet(response)
                raise protocol.JsonRpcError(
                    _http_failure(self.name, response.status_code, detail))

            if not expect_reply:
                _close(response)
                return {}
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                return self._read_sse(response, request_id)
            return self._read_json(response, request_id)

    def _read_json(self, response, request_id: Any) -> dict:
        chunks: list[bytes] = []
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if self._cancelled(request_id):
                    _close(response)
                    raise protocol.JsonRpcError(
                        f"the {self.name} request was cancelled")
                if chunk:
                    chunks.append(chunk)
        except protocol.JsonRpcError:
            raise
        except requests.exceptions.RequestException as exc:
            raise protocol.JsonRpcError(
                f"the {self.name} server's reply could not be read: {exc}") from exc
        finally:
            _close(response)
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not raw:
            # A notification or a 202 with no body is a valid empty reply.
            return {}
        try:
            message = protocol.loads(raw)
        except protocol.JsonRpcError as exc:
            raise protocol.JsonRpcError(
                f"the {self.name} server sent a reply that is not JSON: {exc}") from exc
        return message

    def _read_sse(self, response, request_id: Any) -> dict:
        """Read a request-scoped SSE stream until the matching reply arrives.

        Per the specification the reply to *this* request is the event whose
        ``id`` matches; notifications that share the stream are recorded and
        ignored here. Closing this stream is the binding's cancellation
        mechanism, which is exactly what ``cancel()`` triggers.
        """
        try:
            data_lines: list[str] = []
            for line in response.iter_lines(decode_unicode=True):
                if self._cancelled(request_id):
                    _close(response)
                    raise protocol.JsonRpcError(
                        f"the {self.name} request was cancelled")
                if line is None:
                    continue
                text = line.strip()
                if not text:
                    message = self._flush_event(data_lines)
                    data_lines = []
                    if message is not None:
                        return message
                    continue
                if text.startswith(":"):
                    continue                      # keep-alive comment
                if text.startswith("data:"):
                    data_lines.append(text[5:].lstrip())
            message = self._flush_event(data_lines)
            if message is not None:
                return message
        finally:
            _close(response)
        raise protocol.JsonRpcError(
            f"the {self.name} server closed the event stream without answering")

    def _flush_event(self, data_lines: list[str]) -> dict | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines).strip()
        data_lines.clear()
        if not raw:
            return None
        try:
            message = protocol.loads(raw)
        except protocol.JsonRpcError:
            return None
        if not isinstance(message, dict):
            return None
        if "id" in message and ("result" in message or "error" in message):
            return message
        if "method" in message:
            self._note_notification(message)
        return None

    def _note_notification(self, message: dict) -> None:
        with self._lock:
            self.notifications.append(message)
            if len(self.notifications) > 100:
                del self.notifications[:-100]

    def _cancelled(self, request_id: Any) -> bool:
        if request_id is None:
            return False
        with self._lock:
            if int(request_id) in self._cancel_ids:
                self._cancel_ids.discard(int(request_id))
                return True
        return False

    def _fail(self, message: str) -> None:
        self._dead = True
        self._running = False
        self._error = str(message or f"the {self.name} server failed")

    # ── state ─────────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running and not self._dead

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def error(self) -> str:
        return str(self._error or "")

    @property
    def server_info(self) -> dict:
        return dict(self._server_info or {})

    @property
    def protocol_version(self) -> str:
        return str(self._protocol_version or "")

    @property
    def capabilities(self) -> dict:
        return dict(self._capabilities or {})

    @property
    def instructions(self) -> str:
        return str(self._instructions or "")

    def stderr_tail(self, limit: int = 5) -> str:
        with self._lock:
            return "\n".join(str(item) for item in self.notifications[-limit:])

    def status(self) -> dict:
        return {
            "name": self.name,
            "running": self.running,
            "server": dict(self._server_info),
            "protocol": self.protocol_version,
            "era": self._era,
            "transport": "http",
            "url": self.url,
            "error": self._error,
            "stderr": self.stderr_tail(3),
        }


def _same_origin(a: str, b: str) -> bool:
    left, right = urlparse(a), urlparse(b)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def _close(response) -> None:
    try:
        response.close()
    except Exception:
        pass


def _read_snippet(response) -> str:
    try:
        raw = response.raw.read(4096, decode_content=True) or b""
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    text = " ".join(text.split())
    return text[:_BODY_SNIPPET]


def _http_failure(name: str, status: int, detail: str) -> str:
    """Translate one HTTP status into the sentence the user needs.

    Each status is a different action for the user: fix the key, fix the URL,
    wait, or check the server. Collapsing them into "request failed" would make
    the Settings window useless exactly when it matters.
    """
    tail = f" ({detail})" if detail else ""
    if status == 401:
        return (f"the {name} server refused the credentials (401). Check the "
                f"API key or token for this server.{tail}")
    if status == 403:
        return (f"the {name} server denied access (403) — the credential may be "
                f"expired, revoked or missing a required scope.{tail}")
    if status == 404:
        return (f"the {name} server's URL does not exist (404). Check the "
                f"endpoint address.{tail}")
    if status in (405, 501):
        return (f"the {name} server does not accept this request ({status}) — it "
                f"may not speak the current MCP HTTP binding.{tail}")
    if status == 429:
        return (f"the {name} server is rate limiting this client (429). It "
                f"clears in a moment; I will not hammer it.{tail}")
    if status in (502, 503, 504):
        return (f"the {name} server is unavailable right now ({status}).{tail}")
    return f"the {name} server answered HTTP {status}.{tail}"
