"""The JarvisOS Connect gateway: a paired phone or browser, reaching this PC.

The gateway is a small HTTP server bound to ``127.0.0.1`` by default. That is a
deliberate default: reaching it from a phone on the same Wi-Fi is opt-in (the user
sets the bind address in Settings), and it is never exposed to the internet by
this project. When it *is* bound to the LAN, three things protect it:

* **A token per device.** Pairing exchanges the short code for a long random
  token; every later request carries it. A device can be revoked on its own.
* **The desktop's own permissions.** Every command is checked against the same
  Settings switches the voice UI uses (``connect.commands``), so a phone cannot
  do what the machine has refused.
* **A confirmation for what cannot be undone.** Restart, shut down and anything
  else declared ``confirm`` is parked behind the on-screen HUD confirmation, and
  the phone is told that it is waiting rather than told it succeeded.

The transport is the standard library (``http.server`` + a hand-rolled WebSocket
handshake) because the project ships no web framework, and adding one to serve
one dashboard would be a poor trade. The web client is ``connect/web/index.html``,
served from here, so nothing has to be installed to use it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from connect import commands, devices

__all__ = ["DEFAULT_PORT", "Gateway", "get_gateway", "status", "stop_gateway"]

DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent / "web"
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _json(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "JarvisConnect/1.0"
    protocol_version = "HTTP/1.1"

    # ── plumbing ─────────────────────────────────────────────────────────────

    def log_message(self, fmt, *args):  # keep the console for the assistant
        try:
            gateway = getattr(self.server, "gateway", None)
            if gateway is not None:
                gateway.log(f"{self.address_string()} {fmt % args}")
        except Exception:
            pass

    @property
    def gateway(self) -> "Gateway":
        return getattr(self.server, "gateway")  # type: ignore[attr-defined]

    def _send(self, code: int, payload: Any, content_type: str = "application/json",
              extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else _json(payload)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(min(length, 1_000_000))
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _token(self) -> str:
        header = str(self.headers.get("Authorization") or "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return str(parse_qs(urlparse(self.path).query).get("token", [""])[0]).strip()

    # ── routes ───────────────────────────────────────────────────────────────

    def do_GET(self):  # noqa: N802 - the stdlib's spelling
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_web("index.html")
        if path == "/health":
            return self._send(200, {"ok": True, "name": "JarvisOS Connect",
                                    "version": "1.0",
                                    "devices": len(devices.list_devices())})
        if path == "/commands":
            return self._send(200, {"ok": True, "commands": commands.describe_all()})
        if path == "/devices":
            if not self._authorised():
                return self._send(401, {"ok": False, "detail": "A device token is "
                                                              "required."})
            return self._send(200, {"ok": True, "devices": devices.list_devices()})
        if path == "/events":
            return self._websocket()
        return self._send(404, {"ok": False, "detail": f"No route {path}."})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()

        if path == "/pair":
            name = str(body.get("name") or "device")
            platform = str(body.get("platform") or "unknown")
            device, token = devices.add_device(name, platform,
                                               str(body.get("code") or ""))
            if device is None:
                return self._send(403, {"ok": False, "detail": token})
            self.gateway.log(f"paired {device['name']} ({device['platform']})")
            try:
                from connect import runtime
                runtime.notify(f"Connect: {device['name']} paired.")
            except Exception:
                pass
            return self._send(200, {"ok": True, "device": device, "token": token,
                                    "commands": commands.describe_all()})

        if path == "/command":
            device = self._device()
            if device is None:
                return self._send(401, {"ok": False, "detail": "A device token is "
                                                              "required."})
            action = str(body.get("action") or "")
            args = body.get("args") if isinstance(body.get("args"), dict) else body
            spec = commands.describe(action)
            if not spec:
                return self._send(404, {"ok": False,
                                        "detail": f"'{action}' is not a command."})
            devices.touch(str(device.get("id") or ""))
            if spec["confirm"]:
                result = self.gateway.confirm(action, args, device)
                return self._send(200 if result.get("ok") else 409, result)
            enabled = self.gateway.feature_enabled
            result = commands.execute(action, args, enabled=enabled)
            self.gateway.log(f"{device.get('name')} → {action}: "
                             f"{'ok' if result.get('ok') else result.get('reason')}")
            return self._send(200 if result.get("ok") else 400, result)

        if path == "/revoke":
            if not self._authorised():
                return self._send(401, {"ok": False, "detail": "A device token is "
                                                              "required."})
            ok, message = devices.revoke_device(str(body.get("device") or ""))
            return self._send(200 if ok else 404, {"ok": ok, "detail": message})

        return self._send(404, {"ok": False, "detail": f"No route {path}."})

    # ── helpers ──────────────────────────────────────────────────────────────

    def _authorised(self) -> bool:
        return self._device() is not None

    def _device(self) -> dict | None:
        token = self._token()
        return devices.find_by_token(token) if token else None

    def _serve_web(self, name: str) -> None:
        path = WEB_DIR / name
        if not path.exists():
            return self._send(404, {"ok": False,
                                    "detail": "The web client is missing from this "
                                              "installation."})
        try:
            body = path.read_bytes()
        except Exception as exc:
            return self._send(500, {"ok": False, "detail": str(exc)})
        return self._send(200, body, "text/html; charset=utf-8")

    def _websocket(self):
        """A minimal RFC 6455 handshake: the client is told when things change."""
        key = str(self.headers.get("Sec-WebSocket-Key") or "")
        if not key:
            return self._send(400, {"ok": False, "detail": "Not a WebSocket request."})
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self.wfile.flush()
        except Exception:
            return
        self.gateway.add_socket(self)

    # ── outbound frames (called by the gateway) ──────────────────────────────

    def ws_send(self, payload: dict) -> None:
        data = _json(payload)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += length.to_bytes(2, "big")
        else:
            header.append(127)
            header += length.to_bytes(8, "big")
        try:
            self.wfile.write(bytes(header) + data)
            self.wfile.flush()
        except Exception:
            raise


class Gateway:
    """Owns the HTTP server, the confirmation bridge and the event sockets."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 ui=None) -> None:
        self.host = str(host or "127.0.0.1")
        self.port = int(port or DEFAULT_PORT)
        self.ui = ui
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self._sockets: list[Any] = []
        self._lock = threading.Lock()
        self.log_lines: list[str] = []
        self.started_at = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> tuple[bool, str]:
        if self.server is not None:
            return True, f"The gateway is already listening on {self.host}:{self.port}."
        try:
            # allow_reuse_address defaults to 1, and on Windows that lets a second
            # listener take a port that is already in use — silently splitting
            # traffic between two gateways. This one is a single, explicit
            # listener, so the bind must fail loudly instead.
            ThreadingHTTPServer.allow_reuse_address = False
            self.server = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as exc:
            return False, (f"The gateway could not listen on {self.host}:{self.port} "
                           f"({exc}). Another program may be using that port.")
        self.server.gateway = self          # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="connect-gateway", daemon=True)
        self.thread.start()
        self.started_at = time.time()
        self.log(f"gateway listening on http://{self.host}:{self.port}")
        return True, (f"JarvisOS Connect is listening on "
                      f"http://{self.host}:{self.port} — pair a device from the "
                      f"dashboard, or open that address in a browser on this "
                      f"machine.")

    def stop(self, reason: str = "") -> str:
        if self.server is None:
            return "The gateway was not running."
        with self._lock:
            sockets = list(self._sockets)
            self._sockets.clear()
        for handler in sockets:
            try:
                handler.wfile.close()
            except Exception:
                pass
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.server = None
        self.log(f"gateway stopped" + (f" ({reason})" if reason else ""))
        return "JarvisOS Connect stopped."

    @property
    def running(self) -> bool:
        return self.server is not None

    # ── the pieces the handler uses ───────────────────────────────────────────

    def feature_enabled(self, name: str) -> bool:
        """The desktop's own permission lookup (the UI's when it is available)."""
        ui = self.ui
        if ui is not None:
            try:
                return bool(ui.feature_enabled(name))
            except Exception:
                pass
        try:
            from config.settings import get_feature
            return bool(get_feature(name, True))
        except Exception:
            return True

    def confirm(self, action: str, args: dict, device: dict) -> dict:
        """Park a destructive command behind the HUD confirmation.

        The same ``security.confirm`` gate the assistant uses, so the phone gets
        the identical treatment: the desktop must be looking at it to approve,
        and the phone is told it is waiting rather than told it worked.
        """
        spec = commands.describe(action)
        label = str(device.get("name") or "a paired device")
        try:
            from security import confirm as confirm_gate
        except Exception as exc:
            return {"ok": False, "reason": "unavailable",
                    "detail": f"The confirmation layer is unavailable, so that "
                              f"command was not run: {exc}"}

        def _run() -> str:
            result = commands.execute(action, args, enabled=self.feature_enabled)
            self.broadcast({"type": "command", "action": action,
                            "ok": bool(result.get("ok")),
                            "detail": result.get("detail")})
            return str(result.get("detail") or "")

        try:
            message = confirm_gate.request(
                key=f"connect:{action}",
                title=f"{label} asked to {action}",
                detail=(f"{spec.get('summary') or action}. Approve on this screen "
                        f"to let it run."),
                run=_run)
        except Exception as exc:
            return {"ok": False, "reason": "unavailable",
                    "detail": f"The confirmation could not be shown: {exc}"}
        return {"ok": False, "reason": "awaiting_confirmation",
                "command": action,
                "detail": (f"{message} — this machine is waiting for you to "
                           f"approve it on screen.")}

    # ── events ───────────────────────────────────────────────────────────────

    def add_socket(self, handler) -> None:
        with self._lock:
            self._sockets.append(handler)
        try:
            handler.ws_send({"type": "hello", "devices": len(devices.list_devices()),
                             "commands": commands.names()})
        except Exception:
            pass

    def broadcast(self, payload: dict) -> int:
        """Send one event to every connected client. Returns how many got it."""
        with self._lock:
            sockets = list(self._sockets)
        sent = 0
        dead = []
        for handler in sockets:
            try:
                handler.ws_send(payload)
                sent += 1
            except Exception:
                dead.append(handler)
        if dead:
            with self._lock:
                self._sockets = [s for s in self._sockets if s not in dead]
        return sent

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        self.log_lines.append(line)
        if len(self.log_lines) > 200:
            del self.log_lines[:-200]
        try:
            print(f"[Connect] {message}")
        except Exception:
            pass

    def status(self) -> dict:
        with self._lock:
            clients = len(self._sockets)
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "url": f"http://{self.host}:{self.port}",
            "clients": clients,
            "paired_devices": devices.list_devices(),
            "pairing_code": devices.pairing_code(),
            "has_secret": bool(devices.pairing_secret()),
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at
            else 0.0,
            "log": list(self.log_lines[-15:]),
        }


# ── the process-wide gateway ──────────────────────────────────────────────────

_GATEWAY: Gateway | None = None
_GATEWAY_LOCK = threading.Lock()


def get_gateway(ui=None) -> Gateway:
    global _GATEWAY
    with _GATEWAY_LOCK:
        if _GATEWAY is None:
            host, port = _configured_address()
            _GATEWAY = Gateway(host=host, port=port, ui=ui)
        elif ui is not None and _GATEWAY.ui is None:
            _GATEWAY.ui = ui
        return _GATEWAY


def _configured_address() -> tuple[str, int]:
    """Bind address from the configuration; loopback unless the user opts out."""
    host, port = "127.0.0.1", DEFAULT_PORT
    try:
        from memory.config_manager import load_api_keys
        data = dict(load_api_keys() or {})
        host = str(data.get("connect_host") or host).strip() or host
        port = int(data.get("connect_port") or port)
    except Exception:
        pass
    return host, port


def status() -> dict:
    gateway = _GATEWAY
    if gateway is None:
        host, port = _configured_address()
        return {"running": False, "host": host, "port": port,
                "url": f"http://{host}:{port}", "clients": 0,
                "paired_devices": devices.list_devices(),
                "pairing_code": devices.pairing_code(),
                "has_secret": bool(devices.pairing_secret()),
                "uptime_s": 0.0, "log": []}
    return gateway.status()


def stop_gateway(reason: str = "shutdown") -> str:
    gateway = _GATEWAY
    return gateway.stop(reason) if gateway is not None else "The gateway was not running."


def local_ip() -> str:
    """This machine's LAN address, for the pairing instructions."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            return str(probe.getsockname()[0])
        finally:
            probe.close()
    except Exception:
        return "127.0.0.1"
