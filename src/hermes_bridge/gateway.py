"""Hermes's own command handlers, reached the way Hermes's own clients reach them.

The gateway that owns a profile's chats (``hermes gateway run``) runs the messaging command handlers
(the ones Telegram uses) for the sessions it owns. Its local clients (the TUI, the Desktop app) attach
in two steps, and so does this module:

1. The gateway's private control socket (``$HERMES_HOME/gateway.sock``, mode 0600) answers
   ``identify`` (instance id, loopback ``api_origin``) and mints a 30-second ``session-ticket``. Only a
   process running as the same OS user can connect; the peer's uid is checked both ways.
2. A WebSocket to ``<api_origin>/api/ws`` with the ticket as a subprotocol, then one JSON-RPC call.

Only ``commands.catalog`` and ``slash.exec`` are ever sent (see ``METHODS``); Hermes decides which
commands a session may run. Nothing here listens on a port, and ``api_origin`` must be loopback.
Standard library only: a minimal RFC 6455 client, enough for one request and its reply.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import socket
import stat
import struct
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

METHODS = frozenset({"commands.catalog", "slash.exec"})
WS_PROTOCOL = "hermes-gateway-v1"
TICKET_PREFIX = "hermes-gateway-ticket."
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_CONTROL_REPLY = 512 * 1024
MAX_MESSAGE = 16 * 1024 * 1024
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class GatewayUnavailable(Exception):
    """No usable gateway for this profile. ``code`` is safe to show; details stay in the log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GatewayRefused(Exception):
    """The gateway answered the call with a JSON-RPC error (``reason`` like ``unsupported_command``)."""

    def __init__(self, reason: str, detail: Optional[str] = None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def profile_home(root: Path, profile: str) -> Path:
    return root if profile == "default" else root / "profiles" / profile


class Gateway:
    def __init__(self, hermes_root: Path, timeout: float = 30.0):
        self.root = Path(hermes_root).expanduser()
        self.timeout = timeout

    def call(self, profile: str, method: str, params: dict, timeout: Optional[float] = None) -> Any:
        if method not in METHODS:
            raise ValueError(f"method {method!r} is not allowed")
        deadline = time.monotonic() + (timeout or self.timeout)
        home = profile_home(self.root, profile)
        control, ident, profile_id = self._discover(home)
        grant = _control_request(control, {"protocol": 1, "id": 1, "verb": "session-ticket", "params": {
            "profile_id": profile_id, "instance_id": ident["instance_id"], "purpose": "interactive"}}, deadline)
        result = grant.get("result") if grant.get("ok") is True else None
        if (not isinstance(result, dict) or not isinstance(result.get("ticket"), str)
                or result.get("instance_id") != ident["instance_id"] or result.get("profile_id") != profile_id):
            raise GatewayUnavailable("gateway_refused_ticket", "Hermes's gateway refused to open a session for the bridge.")
        host, port = _loopback_origin(ident.get("api_origin"))
        with _WebSocket(host, port, "/api/ws", [WS_PROTOCOL, TICKET_PREFIX + result["ticket"]], deadline) as ws:
            return ws.rpc(method, params)

    # ------------------------------------------------------------------ discovery
    def _discover(self, home: Path) -> tuple[Path, dict, str]:
        """``(control socket, identify result, profile_id)`` for the gateway serving *home*: its own
        socket, or the default home's multiplexer when that lists *home* among its served profiles."""
        candidates = [home] + ([self.root] if home != self.root else [])
        for owner in candidates:
            try:
                control = _socket_path(owner)
            except FileNotFoundError:
                continue
            try:
                reply = _control_request(control, {"protocol": 1, "verb": "identify", "id": 1},
                                         time.monotonic() + min(self.timeout, 5.0))
            except (FileNotFoundError, ConnectionRefusedError):
                continue
            ident = reply.get("result") if reply.get("ok") is True else None
            if not isinstance(ident, dict) or ident.get("kind") != "hermes-gateway":
                raise GatewayUnavailable("gateway_incompatible", "The process on Hermes's control socket isn't a Hermes gateway.")
            served = {_resolved(p.get("home")): p.get("profile_id") for p in ident.get("served_profiles") or []
                      if isinstance(p, dict) and isinstance(p.get("profile_id"), str)}
            profile_id = served.get(_resolved(str(home)))
            if profile_id is None:
                continue  # the default gateway runs, but doesn't serve this profile
            if ident.get("runtime_protocol") != 1 or "session-authority-v1" not in (ident.get("capabilities") or []):
                raise GatewayUnavailable("gateway_incompatible", "This Hermes gateway can't run commands for other clients; update Hermes.")
            if ident.get("state") != "ready" or not isinstance(ident.get("instance_id"), str):
                raise GatewayUnavailable("gateway_not_ready", "Hermes's gateway is starting or stopping; try again in a moment.")
            return control, ident, profile_id
        raise GatewayUnavailable("gateway_not_running",
                                 "Hermes's gateway isn't running for this profile, or the bridge can't reach its "
                                 "control socket (the bridge must run as the same user as Hermes).")


def _resolved(path: Any) -> Optional[str]:
    if not isinstance(path, str) or not path:
        return None
    with contextlib.suppress(OSError):
        return str(Path(path).resolve())
    return path


def _private(path: Path, kind: str) -> None:
    info = path.lstat()
    ok = stat.S_ISSOCK(info.st_mode) if kind == "socket" else stat.S_ISREG(info.st_mode)
    if not ok or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise GatewayUnavailable("gateway_unsafe_socket",
                                 "Hermes's control socket isn't private to this user, so the bridge won't use it.")


def _socket_path(home: Path) -> Path:
    """``home/gateway.sock``, or the private pointer ``gateway.sock.path`` Hermes writes when the home's
    path is too long for a Unix socket. FileNotFoundError when neither exists."""
    direct = home / "gateway.sock"
    if os.path.lexists(direct):
        _private(direct, "socket")
        return direct
    pointer = home / "gateway.sock.path"
    _private(pointer, "file")  # raises FileNotFoundError when absent
    target = Path(pointer.read_text(encoding="utf-8").strip())
    if not target.is_absolute() or target.name != "control.sock" or not target.parent.name.startswith("hermes-gw-"):
        raise GatewayUnavailable("gateway_unsafe_socket", "Hermes's control socket pointer looks wrong.")
    _private(target, "socket")
    return target


def _peer_uid(sock: socket.socket) -> Optional[int]:
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def _control_request(path: Path, request: dict, deadline: float) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_left(deadline))
        sock.connect(str(path))
        uid = _peer_uid(sock)
        if uid is not None and uid != os.getuid():
            raise GatewayUnavailable("gateway_unsafe_socket", "Hermes's control socket is served by another user.")
        sock.sendall(json.dumps(request).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            sock.settimeout(_left(deadline))
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_CONTROL_REPLY:
                raise GatewayUnavailable("gateway_incompatible", "Hermes's control socket answered too much.")
    except socket.timeout:
        raise GatewayUnavailable("gateway_timeout", "Hermes's gateway didn't answer in time.") from None
    finally:
        sock.close()
    try:
        reply = json.loads(bytes(data).split(b"\n", 1)[0])
    except ValueError:
        raise GatewayUnavailable("gateway_incompatible", "Hermes's control socket sent something unexpected.") from None
    if not isinstance(reply, dict):
        raise GatewayUnavailable("gateway_incompatible", "Hermes's control socket sent something unexpected.")
    return reply


def _loopback_origin(origin: Any) -> tuple[str, int]:
    parts = urlsplit(origin) if isinstance(origin, str) else None
    if not parts or parts.scheme != "http" or parts.hostname not in LOOPBACK or not parts.port:
        raise GatewayUnavailable("gateway_incompatible", "Hermes's gateway API isn't on a loopback address, so the bridge won't use it.")
    return parts.hostname, parts.port


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise GatewayUnavailable("gateway_timeout", "Hermes's gateway didn't answer in time.")
    return left


class _WebSocket:
    """A client for one JSON-RPC exchange: handshake, masked text frames out, text frames in."""

    def __init__(self, host: str, port: int, path: str, protocols: list[str], deadline: float):
        self.deadline = deadline
        self._next_id = 0
        try:
            self.sock = socket.create_connection((host, port), timeout=_left(deadline))
        except OSError:
            raise GatewayUnavailable("gateway_unreachable", "Hermes's gateway API didn't accept a connection.") from None
        try:
            self._handshake(host, port, path, protocols)
        except BaseException:
            self.sock.close()
            raise

    def _handshake(self, host: str, port: int, path: str, protocols: list[str]) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        hostport = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"Sec-WebSocket-Protocol: {', '.join(protocols)}\r\n\r\n").encode())
        self.buf = bytearray()
        head = self._read_until(b"\r\n\r\n", 65536).decode("latin-1")
        lines = head.split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        want = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        if not lines[0].startswith("HTTP/1.1 101"):
            raise GatewayUnavailable("gateway_refused_ticket", "Hermes's gateway refused the bridge's connection.")
        if headers.get("sec-websocket-accept") != want or headers.get("sec-websocket-protocol") != WS_PROTOCOL:
            raise GatewayUnavailable("gateway_incompatible", "Hermes's gateway answered with an unexpected WebSocket handshake.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with contextlib.suppress(OSError):
            self._send(0x8, struct.pack("!H", 1000))
        self.sock.close()

    def rpc(self, method: str, params: dict) -> Any:
        self._next_id += 1
        rid = f"bridge-{self._next_id}"
        self._send(0x1, json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode())
        while True:
            try:
                frame = json.loads(self._message())
            except ValueError:
                continue
            if not isinstance(frame, dict) or frame.get("id") != rid:
                continue  # events (gateway.ready, session updates) are not for us
            if "error" in frame:
                err = frame["error"] if isinstance(frame["error"], dict) else {}
                data = err.get("data") if isinstance(err.get("data"), dict) else {}
                reason = data.get("reason") or err.get("message") or "error"
                detail = data.get("detail")
                raise GatewayRefused(str(reason)[:80], str(detail)[:500] if isinstance(detail, str) else None)
            return frame.get("result")

    # ---- framing
    def _recv(self, n: int) -> None:
        while len(self.buf) < n:
            self.sock.settimeout(_left(self.deadline))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise GatewayUnavailable("gateway_timeout", "Hermes's gateway didn't answer in time.") from None
            if not chunk:
                raise GatewayUnavailable("gateway_closed", "Hermes's gateway closed the connection.")
            self.buf.extend(chunk)

    def _take(self, n: int) -> bytes:
        self._recv(n)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buf:
            if len(self.buf) > limit:
                raise GatewayUnavailable("gateway_incompatible", "Hermes's gateway sent an oversized handshake.")
            self._recv(len(self.buf) + 1)
        i = self.buf.index(marker) + len(marker)
        out = bytes(self.buf[:i])
        del self.buf[:i]
        return out

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack("!H", n) if n < 65536
                                                    else bytes([0x80 | 127]) + struct.pack("!Q", n))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _message(self) -> bytes:
        parts: list[bytes] = []
        total = 0
        while True:
            b0, b1 = self._take(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._take(8))[0]
            mask = self._take(4) if b1 & 0x80 else None
            total += n
            if total > MAX_MESSAGE:
                raise GatewayUnavailable("gateway_incompatible", "Hermes's gateway sent an oversized message.")
            payload = self._take(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise GatewayUnavailable("gateway_closed", "Hermes's gateway closed the connection.")
            if opcode == 0x9:
                self._send(0xA, payload)
                total -= n
                continue
            if opcode == 0xA:
                total -= n
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts)
