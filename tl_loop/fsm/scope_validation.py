"""Validation helpers shared by durable recursive scope values."""

from __future__ import annotations

from collections.abc import Collection

from .child import ChildRecord
from .evidence import require_text as _require_text


def sorted_records(
    records: tuple[ChildRecord, ...] | list[ChildRecord],
) -> tuple[ChildRecord, ...]:
    """Normalize a child group to deterministic child-ID order."""
    if any(not isinstance(record, ChildRecord) for record in records):
        raise TypeError("orchestration child groups require ChildRecord values")
    return tuple(sorted(records, key=lambda record: record.child_id))


def validate_unique_records(*groups: Collection[ChildRecord]) -> None:
    """Reject duplicate child identities across all scope groups."""
    ids = [record.child_id for group in groups for record in group]
    if len(ids) != len(set(ids)):
        raise ValueError("orchestration child IDs must be unique")


def validate_ordered_children(
    children: tuple[tuple[int, tuple[ChildRecord, ...]], ...],
    *,
    first_order: int = 1,
) -> None:
    """Require contiguous, non-empty, duplicate-free ordered stages."""
    orders = [order for order, _ in children]
    if orders != list(range(first_order, first_order + len(orders))):
        raise ValueError("ordered child groups must be contiguous from the current order")
    for _, records in children:
        if not records:
            raise ValueError("ordered child groups must not be empty")
        validate_unique_records(records)


def validate_scope(scope_path: tuple[str, ...], plan_digest: str) -> None:
    """Validate the immutable recursive scope identity."""
    if not scope_path or any(not isinstance(item, str) or not item for item in scope_path):
        raise ValueError("scope path must contain non-empty names")
    _require_text(plan_digest, "plan digest")


__all__ = [
    "sorted_records",
    "validate_ordered_children",
    "validate_scope",
    "validate_unique_records",
]
