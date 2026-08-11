"""Rust runtime client boundary for the TL controller."""

from .effects import (
    TOOL_METHODS,
    ChildSpec,
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
    "MUTATING_METHODS",
    "READ_METHODS",
    "MutationBlocked",
    "ReadOnlyEffectClient",
    "resolve_socket_path",
]
