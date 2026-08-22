"""Strict, recursive normalization for durable JSON documents."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Any


class DurableWriteError(TypeError):
    """A durable JSON document could not be encoded safely."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        target: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.target = target

    def with_context(self, *, operation: str, target: str) -> DurableWriteError:
        return DurableWriteError(
            f"{operation} for {target!r} could not be journaled: {self}",
            operation=operation,
            target=target,
        )


def to_jsonable(value: object) -> object:
    """Return a JSON-compatible copy while preserving unsupported-type errors."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(value: object, **kwargs: Any) -> str:
    """Serialize a value after recursively normalizing mapping-like containers."""
    try:
        return json.dumps(to_jsonable(value), **kwargs)
    except (TypeError, ValueError) as error:
        raise DurableWriteError(str(error)) from error


__all__ = ["DurableWriteError", "dumps", "to_jsonable"]
