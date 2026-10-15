"""MCP over stdio: framing, message shapes and schema conversion.

The transport is newline-delimited JSON on the server's stdin/stdout (stderr is
logging and never carries protocol traffic). That is the whole wire format:
one JSON-RPC 2.0 message per line. Keeping the framing here — small, pure and
testable — means the client below only has to think about processes and timeouts.

The second half of the file converts a server's JSON-Schema tool definition into
the uppercase dialect the Live API expects, because that is the boundary where
"MCP tool" becomes "tool the model may call".
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "MODERN_PROTOCOL_VERSION",
    "LEGACY_PROTOCOL_VERSIONS",
    "META_PROTOCOL_VERSION",
    "META_CLIENT_INFO",
    "META_CLIENT_CAPABILITIES",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "JsonRpcError",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "PARSE_ERROR",
    "dumps",
    "error_message",
    "gemini_schema",
    "loads",
    "notification",
    "request",
    "result_text",
]

#: The current MCP specification. Since 2026-07-28 the protocol core is
#: stateless: there is no ``initialize`` handshake and no ``Mcp-Session-Id``,
#: and every request describes itself by carrying its protocol version, client
#: identity and capabilities in ``_meta``. ``LATEST_PROTOCOL_VERSION`` is what a
#: modern request declares; the legacy versions below are still spoken by the
#: servers already deployed, and the transport detects which era it is talking
#: to rather than assuming.
MODERN_PROTOCOL_VERSION = "2026-07-28"
LATEST_PROTOCOL_VERSION = MODERN_PROTOCOL_VERSION

#: Everything this host can interoperate with. A legacy server answers the
#: ``initialize`` handshake with the version it chose; a modern one ignores it.
SUPPORTED_PROTOCOL_VERSIONS = (
    "2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: The revisions that still use the connection-scoped session + handshake.
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26",
                            "2024-11-05")

#: The namespaced ``_meta`` keys every self-describing request carries.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

PARSE_ERROR = -32700
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class JsonRpcError(RuntimeError):
    """An ``error`` object returned by the server, or a transport failure."""

    def __init__(self, message: str, code: int = INTERNAL_ERROR, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def dumps(message: dict) -> str:
    """One compact line — the framing is newline-delimited, so no newlines."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def loads(line: str) -> dict:
    """Parse one line into a message. Raises JsonRpcError on nonsense."""
    try:
        message = json.loads(line)
    except Exception as exc:
        raise JsonRpcError(f"the server sent a line that is not JSON: {exc}",
                           PARSE_ERROR) from exc
    if not isinstance(message, dict):
        raise JsonRpcError("the server sent a JSON value that is not an object",
                           PARSE_ERROR)
    return message


def request(method: str, params: dict | None, request_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method,
            "params": dict(params or {})}


def notification(method: str, params: dict | None = None) -> dict:
    message = {"jsonrpc": "2.0", "method": method}
    if params:
        message["params"] = dict(params)
    return message


def error_message(message: dict) -> str:
    """The readable sentence from an ``error`` object."""
    error = message.get("error") or {}
    if isinstance(error, dict):
        text = str(error.get("message") or "the server reported an error")
        if error.get("data"):
            text += f" ({str(error['data'])[:200]})"
        return text
    return str(error) or "the server reported an error"


def result_text(result: Any) -> str:
    """Flatten a ``tools/call`` result into the text the model reads.

    Content blocks may be text, images or embedded resources. Everything is
    rendered as text — an image becomes its declared description or its mime
    type — because the assistant's tool channel is text. ``isError`` results are
    returned verbatim: the failure is the answer.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)

    blocks = result.get("content")
    parts: list[str] = []
    if isinstance(blocks, str):
        parts.append(blocks)
    elif isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                kind = str(block.get("type") or "text")
                if kind == "text":
                    parts.append(str(block.get("text") or ""))
                elif kind == "image":
                    parts.append(f"[image {block.get('mimeType') or 'image'} — "
                                 f"not rendered here]")
                elif kind == "resource":
                    resource = block.get("resource") or {}
                    parts.append(str(resource.get("text")
                                      or resource.get("uri") or "[resource]"))
                else:
                    parts.append(str(block))
    if not parts:
        structured = result.get("structuredContent")
        if structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=1))
    text = "\n".join(part for part in parts if part).strip()
    if result.get("isError"):
        text = f"[the tool reported an error] {text}" if text else \
            "[the tool reported an error]"
    return text


#: JSON-Schema type name → the Live API's dialect.
_TYPE_MAP = {
    "string": "STRING", "number": "NUMBER", "integer": "INTEGER",
    "boolean": "BOOLEAN", "array": "ARRAY", "object": "OBJECT", "null": "STRING",
}


def _property(raw: dict) -> dict:
    """One property, converted (recursively for arrays and nested objects)."""
    kind = _TYPE_MAP.get(str(raw.get("type") or "string").lower(), "STRING")
    entry: dict[str, Any] = {"type": kind}
    description = str(raw.get("description") or "").strip()
    if description:
        entry["description"] = description[:1000]
    enum = raw.get("enum")
    if isinstance(enum, list) and enum and all(
            isinstance(item, (str, int, float, bool)) for item in enum):
        entry["enum"] = [str(item) for item in enum]
    if kind == "ARRAY":
        items = raw.get("items")
        entry["items"] = _property(items if isinstance(items, dict)
                                   else {"type": "string"})
    elif kind == "OBJECT" and isinstance(raw.get("properties"), dict):
        nested = gemini_schema(raw)
        entry["properties"] = nested["properties"]
        if nested.get("required"):
            entry["required"] = nested["required"]
    return entry


def gemini_schema(schema: Any) -> dict:
    """Convert a tool's JSON Schema into the Live API function-parameters shape.

    Unknown or unsupported keywords are dropped rather than guessed at: a
    declaration that cannot be expressed must not end up describing something the
    server will not accept. An untyped property is declared as STRING, which is
    what every MCP server the project ships accepts for it.
    """
    if not isinstance(schema, dict):
        return {"type": "OBJECT", "properties": {}}
    properties: dict[str, dict] = {}
    raw_properties = schema.get("properties")
    if isinstance(raw_properties, dict):
        for name, raw in raw_properties.items():
            if isinstance(raw, dict):
                properties[str(name)] = _property(raw)
    required = schema.get("required")
    declared: dict[str, Any] = {"type": "OBJECT", "properties": properties}
    if isinstance(required, list):
        keep = [str(name) for name in required if str(name) in properties]
        if keep:
            declared["required"] = keep
    return declared
