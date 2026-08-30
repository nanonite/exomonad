"""Shared validation for durable reducer evidence."""

from __future__ import annotations

from collections.abc import Mapping


def require_text(value: str, field: str) -> None:
    """Require a durable identity or digest to be present."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty")


def require_positive(value: int, field: str) -> None:
    """Require an epoch, generation, or external numeric identity."""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be positive")


def require_fields(evidence: Mapping[str, str], fields: tuple[str, ...], context: str) -> None:
    """Require all fields that define one durable evidence phase."""
    if any(not evidence.get(field) for field in fields):
        raise ValueError(f"{context} requires complete evidence")


__all__ = ["require_fields", "require_positive", "require_text"]
