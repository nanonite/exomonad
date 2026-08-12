"""Rust runtime client boundary for the TL controller."""

from .effects import (
    TOOL_METHODS,
    CompletedTask,
    EffectClient,
    EffectTransport,
    ToolResult,
)
from .readonly import MUTATING_METHODS, READ_METHODS, MutationBlocked, ReadOnlyEffectClient
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
    "DEFAULT_TIMEOUT_SECONDS",
    "MUTATING_METHODS",
    "READ_METHODS",
    "TOOL_METHODS",
    "CompletedTask",
    "DecodeError",
    "EffectClient",
    "EffectTransport",
    "MutationBlocked",
    "ReadOnlyEffectClient",
    "ServerError",
    "ServerTimeout",
    "ServerUnreachable",
    "ToolResult",
    "TransportClient",
    "TransportError",
    "resolve_socket_path",
]
