#!/usr/bin/env python3
import base64
import hashlib
import os
import socketserver
import sys
import time


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame_text(payload: bytes) -> bytes:
    length = len(payload)
    if length < 126:
        return bytes([0x81, length]) + payload
    if length < 65536:
        return bytes([0x81, 126]) + length.to_bytes(2, "big") + payload
    return bytes([0x81, 127]) + length.to_bytes(8, "big") + payload


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk

        key = None
        for line in data.decode("latin1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
                break
        if not key:
            return

        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        self.request.sendall(response.encode("ascii"))

        deadline = time.time() + int(os.environ.get("TANGLED_RELAY_TIMEOUT_SECONDS", "900"))
        while time.time() < deadline:
            if os.path.exists(self.server.event_file):
                with open(self.server.event_file, "rb") as f:
                    payload = f.read().strip()
                if payload:
                    self.request.sendall(frame_text(payload))
                    while time.time() < deadline:
                        time.sleep(1)
                    return
            time.sleep(1)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def main():
    if len(sys.argv) != 3:
        print("usage: knot-event-relay.py <port> <event-file>", file=sys.stderr)
        return 2
    port = int(sys.argv[1])
    event_file = sys.argv[2]
    with Server(("127.0.0.1", port), Handler) as server:
        server.event_file = event_file
        print(f"knot event relay listening on 127.0.0.1:{port}, event_file={event_file}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
