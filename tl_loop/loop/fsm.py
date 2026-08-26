"""Pure Mealy transition boundary for the TL controller.

The driver still owns effect execution and legacy lifecycle migration.  This
module makes the input alphabet and the pure decision seam explicit so those
concerns can be moved incrementally without adding another reducer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TypeAlias

from tl_loop.events.envelope import EventEnvelope, EventKind
from tl_loop.fsm.event import TLEvent
from tl_loop.loop.heartbeat import SyntheticHeartbeatEvent
from tl_loop.loop.observation import WatcherObservation
from tl_loop.loop.reconcile import (
    ExternalIntent,
    InternalTransition,
    Quiescent,
    derive_next_action,
    reconcile_slice,
)
from tl_loop.state.schema import RunState


@dataclass(frozen=True)
class WatcherObservationEvent:
    """A host watcher observation projected from the immutable ledger."""

    slice_id: str
    observation: WatcherObservation
    authoritative_owner_id: str | None = None


@dataclass(frozen=True)
class HeartbeatTick:
    """Synthetic heartbeat input; effects are intentionally not available."""

    observed_at: float


Event: TypeAlias = EventEnvelope | WatcherObservationEvent | HeartbeatTick | TLEvent
Action: TypeAlias = ExternalIntent | InternalTransition


@dataclass(frozen=True)
class Transition:
    """Result of one pure Mealy step and its authorised controller actions."""

    state: RunState
    actions: tuple[Action, ...] = ()
    ignored_reason: str | None = None


def step(state: RunState, event: Event) -> Transition:
    """Apply one total, deterministic transition without touching effects."""
    if isinstance(event, HeartbeatTick):
        decision = derive_next_action(state, reviewer_max_rounds=state.reviewer_max_rounds)
        if isinstance(decision, Quiescent):
            return Transition(state, ignored_reason=decision.reason)
        return Transition(state, (decision,))
    if isinstance(event, WatcherObservationEvent):
        return _step_watcher(state, event)
    if isinstance(event, EventEnvelope):
        if event.kind in {
            EventKind.WATCHER_PR_OBSERVATION,
            EventKind.WATCHER_OWNERSHIP_UNRESOLVED,
        }:
            slice_id = event.slice_id or _data_text(event.data, "slice_id")
            if slice_id is None:
                return Transition(state, ignored_reason="watcher_observation_missing_slice_id")
            return _step_watcher(
                state,
                WatcherObservationEvent(slice_id, WatcherObservation.from_response(event.data)),
            )
        return Transition(state, ignored_reason=f"ledger_event_not_migrated:{event.event_type}")
    if isinstance(event, SyntheticHeartbeatEvent):
        return Transition(state, ignored_reason="legacy_heartbeat_event_not_migrated")
    return Transition(
        state, ignored_reason=f"legacy_lifecycle_event_not_migrated:{type(event).__name__}"
    )


def _step_watcher(state: RunState, event: WatcherObservationEvent) -> Transition:
    current = state.slices.get(event.slice_id)
    if current is None:
        return Transition(state, ignored_reason=f"unknown_slice:{event.slice_id}")
    result = reconcile_slice(
        current,
        authoritative_owner_id=event.authoritative_owner_id,
        watcher=event.observation,
    )
    updated = replace(current, reconciliation=result.as_state())
    next_state = replace(state, slices={**state.slices, event.slice_id: updated})
    if result.next_action in {"no_action", "await_authoritative_evidence", "await_merge_event"}:
        return Transition(next_state, ignored_reason=result.next_action)
    return Transition(
        next_state,
        (
            ExternalIntent(
                operation=result.next_action,
                target_id=event.slice_id,
                arguments={"reconciliation": result.as_state()},
            ),
        ),
    )


def _data_text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


__all__ = ["Action", "Event", "HeartbeatTick", "Transition", "WatcherObservationEvent", "step"]
