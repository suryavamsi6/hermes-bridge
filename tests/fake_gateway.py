"""A fake Hermes gateway: the private control socket (identify, session-ticket) and the loopback
/api/ws JSON-RPC endpoint with ticket subprotocols, enough for hermes_bridge.gateway."""
import base64
import hashlib
import json
import os
import socket
import socketserver
import struct
import threading
from pathlib import Path

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOCAL = "local-0123456789abcdef"
CATALOG = {
    "pairs": [["/status", "Show session info"]],
    "canon": {"/status": "/status", "/new": "/new", "/reset": "/new", "/q": "/queue", "/queue": "/queue"},
    "categories": [
        {"name": "Session", "pairs": [["/new", "Start a new session (usage: /new [name])"], ["/queue", "Queue a prompt"]]},
        {"name": "Info", "pairs": [["/status", "Show session info"], ["/new", "duplicate is dropped"], "not-a-pair"]},
        {"name": "User commands", "pairs": [["/deploy", "Run deploy.sh"]]},
    ],
    "skills": {"/arxiv": {}}, "plugin_commands": {"/weather": {}},
}


class FakeGateway:
    def __init__(self, root: Path, served=("default",)):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.instance = "inst-1"
        self.tickets, self.calls, self.ws_methods = set(), [], []
        self.state = "ready"
        self.served = served
        self.api_origin = None
        self.refuse = {}  # command -> (reason, detail)

    def homes(self):
        return [self.root if p == "default" else self.root / "profiles" / p for p in self.served]

    def start(self):
        fake = self

        class WS(socketserver.BaseRequestHandler):
            def handle(self):
                fake._ws(self.request)

        self.http = socketserver.ThreadingTCPServer(("127.0.0.1", 0), WS)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.api_origin = f"http://127.0.0.1:{self.http.server_address[1]}"
        path = self.root / "gateway.sock"
        old = os.umask(0o177)
        try:
            self.ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.ctl.bind(str(path))
        finally:
            os.umask(old)
        self.ctl.listen(8)
        threading.Thread(target=self._control_loop, daemon=True).start()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.ctl.close()
        (self.root / "gateway.sock").unlink(missing_ok=True)

    # ---- control socket
    def _control_loop(self):
        while True:
            try:
                conn, _ = self.ctl.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data)
                self.calls.append(req["verb"])
                if req["verb"] == "identify":
                    res = {"kind": "hermes-gateway", "instance_id": self.instance, "runtime_protocol": 1, "state": self.state,
                           "api_origin": self.api_origin, "capabilities": ["session-authority-v1"],
                           "served_profiles": [{"profile_id": str(h), "home": str(h)} for h in self.homes()]}
                elif req["verb"] == "session-ticket":
                    p = req["params"]
                    assert p["instance_id"] == self.instance and p["purpose"] == "interactive", p
                    ticket = f"t{len(self.tickets)}"
                    self.tickets.add(ticket)
                    res = {"ticket": ticket, "instance_id": self.instance, "profile_id": p["profile_id"], "expires_in_seconds": 30}
                else:
                    conn.sendall(json.dumps({"ok": False, "id": req.get("id")}).encode() + b"\n")
                    continue
                conn.sendall(json.dumps({"ok": True, "id": req.get("id"), "result": res}).encode() + b"\n")

    # ---- websocket
    def _ws(self, sock):
        data = b""
        while b"\r\n\r\n" not in data:
            data += sock.recv(4096)
        lines = data.decode().split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        protos = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",")]
        tickets = [p[len("hermes-gateway-ticket."):] for p in protos if p.startswith("hermes-gateway-ticket.")]
        if "hermes-gateway-v1" not in protos or len(tickets) != 1 or tickets[0] not in self.tickets:
            sock.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        self.tickets.discard(tickets[0])  # single use
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: hermes-gateway-v1\r\n\r\n").encode())
        _send(sock, json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready"}}))
        _send(sock, b"", opcode=0x9)  # a ping the client must answer
        while True:
            msg = _recv(sock)
            if msg is None:
                return
            req = json.loads(msg)
            self.ws_methods.append(req["method"])
            _send(sock, json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "noise"}}))
            _send(sock, json.dumps({"jsonrpc": "2.0", "id": req["id"], **self._answer(req["method"], req["params"])}), split=True)

    def _answer(self, method, params):
        if method == "commands.catalog":
            return {"result": CATALOG}
        if method == "slash.exec":
            cmd = params["command"]
            name = cmd.split()[0].lstrip("/")
            if params["session_id"] != LOCAL:
                return {"error": {"code": 4001, "message": "not_found", "data": {"reason": "not_found"}}}
            if name in self.refuse:
                reason, detail = self.refuse[name]
                return {"error": {"code": 4001, "message": reason, "data": {"reason": reason, **({"detail": detail} if detail else {})}}}
            if name == "plan":
                return {"result": {"type": "send", "message": "[/plan] plan it: " + cmd[6:]}}
            return {"result": {"type": "exec", "output": f"ran {cmd}" + ("x" * 70000 if name == "big" else "")}}
        return {"error": {"code": 4001, "message": "unknown_method", "data": {"reason": "unknown_method"}}}


def _send(sock, payload, opcode=0x1, split=False):
    payload = payload.encode() if isinstance(payload, str) else payload
    chunks = [payload[: len(payload) // 2], payload[len(payload) // 2:]] if split and len(payload) > 1 else [payload]
    for i, chunk in enumerate(chunks):
        op = opcode if i == 0 else 0x0
        fin = 0x80 if i == len(chunks) - 1 else 0
        n = len(chunk)
        head = bytes([fin | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack("!H", n) if n < 65536 else bytes([127]) + struct.pack("!Q", n))
        sock.sendall(head + chunk)


def _recv(sock):
    def take(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf
    try:
        while True:
            b0, b1 = take(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", take(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", take(8))[0]
            assert b1 & 0x80, "client frames must be masked"
            mask = take(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(take(n)))
            op = b0 & 0x0F
            if op == 0x8:
                return None
            if op == 0xA:
                continue
            return payload.decode()
    except (EOFError, OSError):
        return None
