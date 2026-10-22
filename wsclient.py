"""Minimal WebSocket client (RFC 6455), standard library only.

Just enough for one long-lived wss:// connection driven from a single
thread: text and binary frames out, text frames in, ping/pong, close.
recv() takes a timeout and returns None when nothing arrived, so the
caller can interleave sending and receiving without a second thread
(SSL sockets should not be read and written from two threads at once).
"""

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class HandshakeError(Exception):
    def __init__(self, status, body):
        super().__init__(f"WebSocket handshake failed: HTTP {status} {body}")
        self.status = status


class ConnectionClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, headers=None, timeout=10, user_agent="debate-transcriber/1.0"):
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.send_timeout = timeout
        sock = socket.create_connection((host, port), timeout=timeout)
        if parts.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        self.sock = sock
        self.buf = b""
        self.fragments = []
        self.fragment_op = None
        self.closed = False

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"User-Agent: {user_agent}",
        ]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        while b"\r\n\r\n" not in self.buf:
            data = sock.recv(4096)
            if not data:
                raise ConnectionClosed("connection closed during handshake")
            self.buf += data
            if len(self.buf) > 65536:
                raise HandshakeError("?", "response headers too large")
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        status = int(status_line.split()[1])
        resp_headers = {}
        for line in header_lines:
            k, _, v = line.partition(":")
            resp_headers[k.strip().lower()] = v.strip()
        if status != 101:
            body = self.buf[:300].decode("utf-8", "replace").strip()
            self.close_socket()
            raise HandshakeError(status, body)
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if resp_headers.get("sec-websocket-accept") != expected:
            self.close_socket()
            raise HandshakeError(101, "bad Sec-WebSocket-Accept")

    # --- sending ---------------------------------------------------------------

    def _send_frame(self, opcode, payload):
        if self.closed:
            raise ConnectionClosed("socket closed")
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.settimeout(self.send_timeout)
        try:
            self.sock.sendall(header + mask + _mask(payload, mask))
        except OSError as e:
            self.closed = True
            raise ConnectionClosed(f"send failed: {e}") from e

    def send_text(self, text):
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data):
        self._send_frame(OP_BINARY, data)

    # --- receiving -------------------------------------------------------------

    def _parse_frame(self):
        """Pop one complete frame from the buffer, or return None."""
        buf = self.buf
        if len(buf) < 2:
            return None
        fin, opcode = buf[0] & 0x80, buf[0] & 0x0F
        masked, n = buf[1] & 0x80, buf[1] & 0x7F
        pos = 2
        if n == 126:
            if len(buf) < 4:
                return None
            n = struct.unpack("!H", buf[2:4])[0]
            pos = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n = struct.unpack("!Q", buf[2:10])[0]
            pos = 10
        mask = b""
        if masked:
            mask = buf[pos:pos + 4]
            pos += 4
        if len(buf) < pos + n:
            return None
        payload = buf[pos:pos + n]
        if masked:
            payload = _mask(payload, mask)
        self.buf = buf[pos + n:]
        return bool(fin), opcode, payload

    def recv(self, timeout):
        """Return ("text", str), ("binary", bytes), ("close", (code, reason)) or None on timeout."""
        while True:
            frame = self._parse_frame()
            if frame is None:
                if self.closed:
                    raise ConnectionClosed("socket closed")
                self.sock.settimeout(timeout)
                try:
                    data = self.sock.recv(65536)
                except (socket.timeout, ssl.SSLWantReadError):
                    return None
                except OSError as e:
                    self.closed = True
                    raise ConnectionClosed(f"recv failed: {e}") from e
                if not data:
                    self.closed = True
                    raise ConnectionClosed("connection closed by server (no close frame)")
                self.buf += data
                continue
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass
            elif opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                try:
                    self._send_frame(OP_CLOSE, payload[:2])
                except ConnectionClosed:
                    pass
                self.close_socket()
                return "close", (code, reason)
            else:
                if opcode != OP_CONT:
                    self.fragment_op, self.fragments = opcode, []
                self.fragments.append(payload)
                if fin:
                    data = b"".join(self.fragments)
                    self.fragments = []
                    if self.fragment_op == OP_TEXT:
                        return "text", data.decode("utf-8", "replace")
                    return "binary", data

    def close(self, code=1000):
        if not self.closed:
            try:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
            except ConnectionClosed:
                pass
        self.close_socket()

    def close_socket(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _mask(payload, mask):
    # XOR via big ints: fast enough for ~3 KB audio frames without numpy.
    n = len(payload)
    if n == 0:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
