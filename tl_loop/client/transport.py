"""HTTP-over-Unix-domain-socket transport for the ExoMonad server."""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
from pathlib import Path
from typing import Any, TypeAlias, cast
from urllib.parse import quote

DEFAULT_TIMEOUT_SECONDS = 120.0
LOGGER = logging.getLogger(__name__)

JsonPrimitive: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class TransportError(Exception):
    """Base class for failures crossing the Rust server boundary."""


class ServerUnreachable(TransportError):
    """The configured server socket does not exist or cannot be connected."""


class ServerTimeout(TransportError):
    """The server did not complete a request before the configured timeout."""


class ServerError(TransportError):
    """The server returned an HTTP error response."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"ExoMonad server returned HTTP {status}: {body}")


class DecodeError(TransportError):
    """The server response was not valid JSON or had an unexpected shape."""


class UnixHTTPConnection(http.client.HTTPConnection):
    """`http.client` connection that dials a Unix-domain socket."""

    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


def resolve_socket_path(
    socket_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> Path:
    """Resolve an explicit socket, environment override, or project default."""
    if socket_path is not None:
        candidate = Path(socket_path)
    elif configured_socket := os.environ.get("EXOMONAD_SOCKET"):
        candidate = Path(configured_socket)
    else:
        root = Path(project_root) if project_root is not None else Path.cwd()
        candidate = root / ".exo" / "server.sock"
    return candidate.expanduser().resolve()


class TransportClient:
    """JSON transport client for the ExoMonad REST surface over UDS."""

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        project_root: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.socket_path = resolve_socket_path(socket_path, project_root)
        self.timeout = timeout
        self.logger = logger or LOGGER

    def get_json(self, path: str) -> JsonValue:
        """Issue a GET request and decode its JSON response."""
        return self._request("GET", path)

    def post_json(self, path: str, body: JsonValue) -> JsonValue:
        """Issue a JSON POST request and decode its JSON response."""
        return self._request("POST", path, body)

    def list_tools(self, role: str, name: str) -> list[JsonValue]:
        """List the tools exposed for an agent identity."""
        path = f"/agents/{quote(role, safe='')}/{quote(name, safe='')}/tools"
        envelope = self.get_json(path)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("tools"), list):
            raise DecodeError(f"Tool-list response has no tools array: {envelope!r}")
        return cast(list[JsonValue], envelope["tools"])

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        """Call one server-side tool without interpreting its effect result."""
        path = f"/agents/{quote(role, safe='')}/{quote(name, safe='')}/tools/call"
        response = self.post_json(
            path,
            {"name": tool_name, "arguments": arguments},
        )
        if not isinstance(response, dict):
            raise DecodeError(f"Tool-call response is not an object: {response!r}")
        return cast(JsonObject, response)

    def _request(self, method: str, path: str, body: JsonValue | None = None) -> JsonValue:
        encoded_body, headers = _encode_request_body(body)
        self.logger.info(
            "[TL loop transport] request method=%s path=%s socket=%s",
            method,
            path,
            self.socket_path,
        )
        self._ensure_socket()
        status, response_body = self._send_request(method, path, encoded_body, headers)

        self.logger.info(
            "[TL loop transport] response method=%s path=%s status=%d size=%d",
            method,
            path,
            status,
            len(response_body),
        )
        return _decode_response(self.logger, method, path, status, response_body)

    def _ensure_socket(self) -> None:
        if self.socket_path.exists():
            return
        error = ServerUnreachable(f"Server socket does not exist: {self.socket_path}")
        self.logger.error("[TL loop transport] request failed: %s", error)
        raise error

    def _send_request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        connection = UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            connection.request(method, path, body=body or None, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        except TimeoutError as timeout_error:
            wrapped = ServerTimeout(
                f"Timed out after {self.timeout:.3f}s waiting for {method} {path}"
            )
            self.logger.error("[TL loop transport] request timeout: %s", timeout_error)
            raise wrapped from timeout_error
        except (ConnectionError, OSError) as connection_error:
            unreachable = ServerUnreachable(
                f"Could not connect to server socket {self.socket_path}: {connection_error}"
            )
            self.logger.error("[TL loop transport] request failed: %s", connection_error)
            raise unreachable from connection_error
        finally:
            connection.close()


def _encode_request_body(body: JsonValue | None) -> tuple[bytes, dict[str, str]]:
    encoded_body = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", "Host": "localhost"}
    if body is not None:
        headers.update(
            {"Content-Type": "application/json", "Content-Length": str(len(encoded_body))}
        )
    return encoded_body, headers


def _decode_response(
    logger: logging.Logger,
    method: str,
    path: str,
    status: int,
    response_body: bytes,
) -> JsonValue:
    text_body = response_body.decode("utf-8", errors="replace")
    if status >= 400:
        server_error = ServerError(status, text_body)
        logger.error("[TL loop transport] server error body=%s", text_body)
        raise server_error
    try:
        decoded: Any = json.loads(text_body)
    except json.JSONDecodeError as decode_error:
        invalid_json = DecodeError(f"Invalid JSON response from {method} {path}: {text_body}")
        logger.error("[TL loop transport] decode failed body=%s", text_body)
        raise invalid_json from decode_error
    logger.info(
        "[TL loop transport] success method=%s path=%s result_type=%s",
        method,
        path,
        type(decoded).__name__,
    )
    return cast(JsonValue, decoded)
