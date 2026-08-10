"""Transport tests using a real HTTP-speaking Unix-domain socket."""

from __future__ import annotations

import http
import os
import socketserver
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tl_loop.client.transport import (
    ServerError,
    ServerTimeout,
    ServerUnreachable,
    TransportClient,
    resolve_socket_path,
)


@dataclass(frozen=True)
class ResponseSpec:
    status: int
    body: bytes
    delay: float = 0.0


ResponseFactory = Callable[[str, bytes], ResponseSpec]


class StubRequestHandler(socketserver.StreamRequestHandler):
    """Read one HTTP request and write the configured response."""

    def handle(self) -> None:
        request_line = self.rfile.readline().decode("ascii").strip()
        headers: dict[str, str] = {}
        while line := self.rfile.readline():
            decoded = line.decode("ascii").strip()
            if not decoded:
                break
            key, value = decoded.split(":", 1)
            headers[key.lower()] = value.strip()
        body = self.rfile.read(int(headers.get("content-length", "0")))
        server = cast(StubServer, self.server)
        response = server.response_factory(request_line, body)
        if response.delay:
            time.sleep(response.delay)
        payload = response.body
        reason = http.HTTPStatus(response.status).phrase
        response_head = (
            f"HTTP/1.1 {response.status} {reason}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            self.wfile.write(response_head + payload)
        except BrokenPipeError:
            # The timeout test intentionally closes before the delayed response.
            pass


class ThreadedUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class StubServer(ThreadedUnixStreamServer):
    """Unix socket server carrying a response factory for each request."""

    def __init__(self, socket_path: str, response_factory: ResponseFactory) -> None:
        self.response_factory = response_factory
        super().__init__(socket_path, StubRequestHandler)


@contextmanager
def stub_server(response_factory: ResponseFactory) -> Iterator[Path]:
    """Run a one-socket HTTP stub and clean up its temporary path."""
    with tempfile.TemporaryDirectory() as directory:
        socket_path = Path(directory) / "server.sock"
        server = StubServer(str(socket_path), response_factory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield socket_path
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)


class TransportTest(unittest.TestCase):
    """The transport exposes server responses and failures without fallback."""

    def test_successful_list_and_call(self) -> None:
        def response(request_line: str, body: bytes) -> ResponseSpec:
            if request_line.startswith("GET /agents/root/root/tools"):
                return ResponseSpec(200, b'{"tools":[{"name":"ping"}]}')
            if not request_line.startswith("POST /agents/root/root/tools/call"):
                return ResponseSpec(400, b'{"error":"unexpected request"}')
            if b'"name": "ping"' not in body:
                return ResponseSpec(400, b'{"error":"unexpected body"}')
            return ResponseSpec(200, b'{"success":true,"result":null,"error":null}')

        with stub_server(response) as socket_path:
            client = TransportClient(socket_path=socket_path, timeout=0.5)
            self.assertEqual(client.list_tools("root", "root"), [{"name": "ping"}])
            self.assertEqual(
                client.call_tool("root", "root", "ping", {}),
                {"success": True, "result": None, "error": None},
            )

    def test_four_hundred_response_raises_server_error(self) -> None:
        with stub_server(lambda _request, _body: ResponseSpec(400, b'{"error":"bad"}')) as path, self.assertRaises(
            ServerError
        ) as caught:
            TransportClient(socket_path=path).get_json("/bad")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.body, '{"error":"bad"}')

    def test_five_hundred_response_raises_server_error(self) -> None:
        with stub_server(lambda _request, _body: ResponseSpec(503, b'{"error":"down"}')) as path, self.assertRaises(
            ServerError
        ) as caught:
            TransportClient(socket_path=path).get_json("/bad")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.body, '{"error":"down"}')

    def test_timeout_raises_server_timeout(self) -> None:
        with stub_server(lambda _request, _body: ResponseSpec(200, b"{}", delay=0.2)) as path, self.assertRaises(
            ServerTimeout
        ):
            TransportClient(socket_path=path, timeout=0.02).get_json("/slow")

    def test_missing_socket_raises_server_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sock"
            with self.assertRaises(ServerUnreachable):
                TransportClient(socket_path=missing).get_json("/health")

    def test_socket_path_precedence_is_explicit_env_then_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            explicit = Path(directory) / "explicit.sock"
            configured = Path(directory) / "configured.sock"
            root.mkdir()
            with patch.dict(os.environ, {"EXOMONAD_SOCKET": str(configured)}):
                self.assertEqual(resolve_socket_path(project_root=root), configured.resolve())
                self.assertEqual(
                    resolve_socket_path(socket_path=explicit, project_root=root), explicit.resolve()
                )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    resolve_socket_path(project_root=root), (root / ".exo" / "server.sock").resolve()
                )
