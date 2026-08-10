"""Rust runtime client boundary for the TL controller."""

from .effects import (
    TOOL_METHODS,
    ChildSpec,
    CompletedTask,
    EffectClient,
    EffectTransport,
    ToolResult,
)
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    DecodeError,
    ServerError,
    ServerTimeout,
    ServerUnreachable,
    TransportClient,
    TransportError,
    resolve_socket_path,
)

__all__ = [
    "TOOL_METHODS",
    "ChildSpec",
    "CompletedTask",
    "DEFAULT_TIMEOUT_SECONDS",
    "DecodeError",
    "EffectClient",
    "EffectTransport",
    "ServerError",
    "ServerTimeout",
    "ServerUnreachable",
    "TransportClient",
    "TransportError",
    "ToolResult",
    "resolve_socket_path",
]
