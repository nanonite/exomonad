"""Deterministic DAG readiness and bounded slice scheduling."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import TypeAlias

from tl_loop.state.schema import (
    SliceState,
    SliceStatus,
    SuspendedDependencyState,
    WaitReason,
    _patterns_overlap,
)
from tl_loop.state.slice_transition import SliceStatusChanged, slice_transition

SliceCollection: TypeAlias = Mapping[str, SliceState] | Iterable[SliceState]
_LIVE_STATUSES = frozenset({SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING})

#: Statuses a slice can never leave. A dependency in one of these can no longer
#: merge, so no dependent it names can ever satisfy its precondition.
#: ``dispatch_failed`` is deliberately absent: a refused spawn holds an
#: operator gate the operator may still answer, exactly like ``parked``.
_FAILED_STATUSES = frozenset({SliceStatus.FAILED, SliceStatus.BLOCKED})


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


def ready(slices: SliceCollection, max_parallel_slices: int | None = None) -> list[SliceState]:
    """Return deterministic pending slices that can safely be spawned now.

    Dependencies must already be merged. Active slices consume the width
    ceiling, and selected candidates reserve disjoint path ownership within
    this result. A pending graph with no active or dependency-ready slice
    raises ScheduleDeadlock instead of leaving the controller waiting forever.
    """
    if max_parallel_slices is not None and max_parallel_slices < 0:
        raise ValueError("max_parallel_slices must be non-negative")
    by_id = _index(slices)
    occupied = tuple(state for state in by_id.values() if state.status in _LIVE_STATUSES)
    active = tuple(state for state in occupied if state.recovery is None)
    pending = tuple(state for state in by_id.values() if state.status is SliceStatus.PENDING)
    capacity = None if max_parallel_slices is None else max(0, max_parallel_slices - len(active))
    eligible = [state for state in pending if not _unsatisfied(state, by_id)]
    if capacity == 0:
        return []
    selected: list[SliceState] = []
    for candidate in eligible:
        if _overlaps_any(candidate, (*occupied, *selected)):
            continue
        selected.append(candidate)
        if capacity is not None and len(selected) >= capacity:
            break
    if not selected and not occupied and pending and not eligible:
        # A pending graph with no live and no dependency-ready slice is only a
        # deadlock when at least one pending slice can never progress. A pending
        # slice waiting on a live or human-gated prerequisite is an ordinary
        # wait, and parking it would report an operator problem that does not
        # exist.
        dead = {
            state.id: _unsatisfied(state, by_id)
            for state in pending
            if unsatisfiable_dependencies(by_id, state.id)
        }
        if dead:
            raise ScheduleDeadlock(dead)
    return selected


def active_count(slices: SliceCollection) -> int:
    """Count slices consuming the parallel width ceiling."""
    return sum(
        state.status in _LIVE_STATUSES and state.recovery is None
        for state in _index(slices).values()
    )


def suspend_dependents(
    slices: SliceCollection, blocker_id: str, recovery_generation: int
) -> dict[str, SliceState]:
    """Suspend only the transitive dependency closure of a recovering slice.

    The previous lifecycle is retained in durable metadata while the scheduler
    sees a pending state. Reapplying the same recovery signal is a no-op.
    """
    if not blocker_id:
        raise ValueError("blocker_id must be non-empty")
    if type(recovery_generation) is not int or recovery_generation < 0:
        raise ValueError("recovery_generation must be non-negative")
    by_id = _index(slices)
    updated = dict(by_id)
    frontier = {blocker_id}
    seen: set[str] = set()
    while frontier:
        next_frontier: set[str] = set()
        for candidate in by_id.values():
            if candidate.id in seen or candidate.id in frontier:
                continue
            if candidate.status in {
                SliceStatus.MERGED,
                SliceStatus.FAILED,
                SliceStatus.PARKED,
                SliceStatus.BLOCKED,
                SliceStatus.DISPATCH_FAILED,
            }:
                continue
            if not any(dependency in frontier for dependency in candidate.depends_on):
                continue
            seen.add(candidate.id)
            prior_status = (
                candidate.suspended_dependency.prior_status
                if candidate.suspended_dependency is not None
                and candidate.suspended_dependency.blocked_by == blocker_id
                and candidate.suspended_dependency.recovery_generation == recovery_generation
                else candidate.status
            )
            suspension = SuspendedDependencyState(
                blocked_by=blocker_id,
                prior_status=prior_status,
                recovery_generation=recovery_generation,
            )
            if (
                candidate.suspended_dependency != suspension
                or candidate.status is not SliceStatus.PENDING
            ):
                updated[candidate.id] = replace(
                    slice_transition(candidate, SliceStatusChanged(SliceStatus.PENDING)),
                    suspended_dependency=suspension,
                )
            next_frontier.add(candidate.id)
        frontier = next_frontier
    return updated


def restore_dependents(
    slices: SliceCollection, recovered_id: str, recovery_generation: int
) -> dict[str, SliceState]:
    """Clear matching dependency suspensions once recovery has completed."""
    by_id = _index(slices)
    blocker = by_id.get(recovered_id)
    if blocker is None or blocker.recovery is not None:
        return by_id
    updated = dict(by_id)
    for candidate in by_id.values():
        suspension = candidate.suspended_dependency
        if suspension is None:
            continue
        if (
            suspension.blocked_by != recovered_id
            or suspension.recovery_generation != recovery_generation
        ):
            continue
        updated[candidate.id] = replace(
            slice_transition(candidate, SliceStatusChanged(suspension.prior_status)),
            suspended_dependency=None,
        )
    return updated


def propagate_abandonment(
    slices: SliceCollection, blocker_id: str, recovery_generation: int
) -> dict[str, SliceState]:
    """Terminally block suspended dependents without touching siblings."""
    by_id = _index(slices)
    updated = dict(by_id)
    for candidate in by_id.values():
        suspension = candidate.suspended_dependency
        if suspension is None:
            continue
        if (
            suspension.blocked_by != blocker_id
            or suspension.recovery_generation != recovery_generation
        ):
            continue
        updated[candidate.id] = replace(
            slice_transition(candidate, SliceStatusChanged(SliceStatus.BLOCKED)),
            blocked_by=blocker_id,
            suspended_dependency=None,
        )
    return updated


def withhold_unsatisfiable_dependents(
    slices: SliceCollection,
) -> dict[str, SliceState]:
    """Terminally block dependents of a prerequisite that can never merge.

    A pending slice whose transitive dependency closure contains a slice in a
    status it can never leave can never satisfy its own precondition, so it is
    blocked with ``blocked_by`` naming the immediate prerequisite on the walk
    to that status. The operator therefore reads the edge that actually blocked
    the slice rather than a transitive summary.

    A live or human-gated prerequisite is untouched: the operator may still
    answer the gate, so the dependent stays pending and waits. Only slices this
    call actually changes are returned, which keeps the caller's durable write
    limited to a real transition.
    """
    by_id = _index(slices)
    updated = dict(by_id)
    for state in by_id.values():
        if state.status is not SliceStatus.PENDING:
            continue
        blockers = unsatisfiable_dependencies(by_id, state.id)
        if not blockers:
            continue
        updated[state.id] = replace(
            slice_transition(state, SliceStatusChanged(SliceStatus.BLOCKED)),
            blocked_by=blockers[0],
            suspended_dependency=None,
        )
    return updated


def _index(slices: SliceCollection) -> dict[str, SliceState]:
    if isinstance(slices, Mapping):
        return dict(slices)
    result: dict[str, SliceState] = {}
    for state in slices:
        if state.id in result:
            raise ValueError(f"duplicate slice id {state.id!r}")
        result[state.id] = state
    return result


def dependencies_satisfied(slices: SliceCollection, slice_id: str) -> bool:
    """Whether every declared dependency of ``slice_id`` has merged.

    ``MERGED`` is the only status that satisfies an edge: it is the one
    terminal state in which the prerequisite's work is actually integrated into
    the base branch the dependent will be dispatched against. ``PARKED`` and a
    live slice do not satisfy an edge either, but they are not failures — the
    operator may still answer the gate — so the dependent simply waits rather
    than being blocked.
    """
    by_id = _index(slices)
    state = by_id.get(slice_id)
    if state is None:
        return False
    return not _unsatisfied(state, by_id)


def unsatisfiable_dependencies(
    slices: SliceCollection, slice_id: str
) -> tuple[str, ...]:
    """Return the unmet dependencies of ``slice_id`` that can never resolve.

    A dependency whose slice is absent, or sits in a status it can never leave,
    can no longer merge, so the edge is permanently unsatisfiable. A dependency
    that is merely live or human-gated is absent from this result: it is still
    a wait, not a dead end.
    """
    by_id = _index(slices)
    state = by_id.get(slice_id)
    if state is None:
        return ()
    return tuple(
        dependency
        for dependency in _unsatisfied(state, by_id)
        if _can_never_merge(dependency, by_id, frozenset())
    )


def _can_never_merge(
    slice_id: str, by_id: Mapping[str, SliceState], visiting: frozenset[str]
) -> bool:
    if slice_id in visiting:
        # A cycle can never be broken by any future merge, so it is a dead end
        # rather than a wait.
        return True
    candidate = by_id.get(slice_id)
    if candidate is None:
        return True
    if candidate.status in _FAILED_STATUSES:
        return True
    nested = visiting | {slice_id}
    return any(
        _can_never_merge(dependency, by_id, nested) for dependency in candidate.depends_on
    )


def _unsatisfied(state: SliceState, by_id: Mapping[str, SliceState]) -> tuple[str, ...]:
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


__all__ = [
    "ScheduleDeadlock",
    "ScheduleError",
    "SuspendedDependencyState",
    "WaitReason",
    "active_count",
    "dependencies_satisfied",
    "propagate_abandonment",
    "ready",
    "restore_dependents",
    "suspend_dependents",
    "unsatisfiable_dependencies",
    "withhold_unsatisfiable_dependents",
]
