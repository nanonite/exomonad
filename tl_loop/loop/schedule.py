"""Deterministic DAG readiness and bounded slice scheduling."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import TypeAlias

from tl_loop.state.schema import (
    SliceState,
    SliceStatus,
    _patterns_overlap,
)

SliceCollection: TypeAlias = Mapping[str, SliceState] | Iterable[SliceState]
_LIVE_STATUSES = frozenset(
    {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
)


class ScheduleError(RuntimeError):
    """Base error for a schedule that cannot make safe progress."""


class ScheduleDeadlock(ScheduleError):
    """No live or ready slice can make progress."""

    def __init__(self, blocked: Mapping[str, Sequence[str]]) -> None:
        self.blocked = MappingProxyType(
            {slice_id: tuple(dependencies) for slice_id, dependencies in blocked.items()}
        )
        self.blocked_slices = tuple(self.blocked)
        details = "; ".join(
            f"{slice_id} (unsatisfied dependencies: {', '.join(dependencies) or 'none'})"
            for slice_id, dependencies in self.blocked.items()
        )
        super().__init__(f"schedule deadlock: {details}")


def ready(
    slices: SliceCollection, max_parallel_slices: int | None = None
) -> list[SliceState]:
    """Return deterministic pending slices that can safely be spawned now.

    Dependencies must already be merged. Active slices consume the width
    ceiling, and selected candidates reserve disjoint path ownership within
    this result. A pending graph with no active or dependency-ready slice
    raises ScheduleDeadlock instead of leaving the controller waiting forever.
    """
    if max_parallel_slices is not None and max_parallel_slices < 0:
        raise ValueError("max_parallel_slices must be non-negative")
    by_id = _index(slices)
    active = tuple(
        state for state in by_id.values() if state.status in _LIVE_STATUSES
    )
    pending = tuple(
        state for state in by_id.values() if state.status is SliceStatus.PENDING
    )
    capacity = (
        None
        if max_parallel_slices is None
        else max(0, max_parallel_slices - len(active))
    )
    eligible = [
        state
        for state in pending
        if not _unsatisfied(state, by_id)
    ]
    if capacity == 0:
        return []
    selected: list[SliceState] = []
    for candidate in eligible:
        if _overlaps_any(candidate, (*active, *selected)):
            continue
        selected.append(candidate)
        if capacity is not None and len(selected) >= capacity:
            break
    if not selected and not active and pending and not eligible:
        raise ScheduleDeadlock(
            {state.id: _unsatisfied(state, by_id) for state in pending}
        )
    return selected


def active_count(slices: SliceCollection) -> int:
    """Count slices consuming the parallel width ceiling."""
    return sum(state.status in _LIVE_STATUSES for state in _index(slices).values())


def _index(slices: SliceCollection) -> dict[str, SliceState]:
    if isinstance(slices, Mapping):
        return dict(slices)
    result: dict[str, SliceState] = {}
    for state in slices:
        if state.id in result:
            raise ValueError(f"duplicate slice id {state.id!r}")
        result[state.id] = state
    return result


def _unsatisfied(
    state: SliceState, by_id: Mapping[str, SliceState]
) -> tuple[str, ...]:
    return tuple(
        dependency
        for dependency in state.depends_on
        if dependency not in by_id or by_id[dependency].status is not SliceStatus.MERGED
    )


def _overlaps_any(candidate: SliceState, others: Sequence[SliceState]) -> bool:
    return any(
        _patterns_overlap(left, right)
        for left in candidate.paths
        for other in others
        for right in other.paths
    )


__all__ = ["ScheduleDeadlock", "ScheduleError", "active_count", "ready"]
