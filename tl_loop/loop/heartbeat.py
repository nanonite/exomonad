"""Configured idle-wave heartbeat and liveness reconciliation."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TypeAlias

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.loop.escalate import park
from tl_loop.state.schema import (
    GoalState,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
)
from tl_loop.state.store import RunStore

JsonMapping: TypeAlias = Mapping[str, object]
LiveEffects: TypeAlias = EffectClient | ReadOnlyEffectClient


class HeartbeatError(RuntimeError):
    """A heartbeat observation cannot be reconciled without guessing."""


class HeartbeatDeadlineExceeded(HeartbeatError):
    """The configured goal deadline elapsed before completion."""


@dataclass(frozen=True)
class HeartbeatConfig:
    """Explicit idle interval and no-progress threshold in seconds."""

    interval_seconds: float = 30.0
    stall_threshold_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("heartbeat interval_seconds must be positive")
        if self.stall_threshold_seconds <= 0:
            raise ValueError("heartbeat stall_threshold_seconds must be positive")


@dataclass(frozen=True)
class SyntheticHeartbeatEvent:
    """A deterministic local event produced by one reconciled observation."""

    event_id: str
    kind: str
    slice_id: str
    source: str
    payload: JsonMapping


@dataclass(frozen=True)
class HeartbeatResult:
    """One idempotent heartbeat attempt and its durable result."""

    fired: bool
    progress: bool
    state: RunState
    events: tuple[SyntheticHeartbeatEvent, ...]
    parked_slice_ids: tuple[str, ...]


def heartbeat_due(goals: GoalState, now: float, interval_seconds: float) -> bool:
    """Return whether the configured interval has elapsed since the last fire."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    return goals.last_heartbeat_at is None or now - goals.last_heartbeat_at >= interval_seconds


def heartbeat_once(
    state: RunState,
    store: RunStore,
    effects: LiveEffects,
    config: HeartbeatConfig,
    *,
    now: float | None = None,
) -> HeartbeatResult:
    """Poll liveness and reconcile one idle wave through the durable store."""
    current_time = time.time() if now is None else now
    if not heartbeat_due(state.goals, current_time, config.interval_seconds):
        return HeartbeatResult(False, False, state, (), ())
    deadline_elapsed = state.goals.deadline and current_time >= state.goals.deadline
    deadline_event = (
        _event(
            "goal.deadline_elapsed",
            "heartbeat",
            "controller",
            {"deadline": state.goals.deadline, "observed_at": current_time},
        )
        if deadline_elapsed
        else None
    )

    active = _active_slices(state)
    if not active:
        return HeartbeatResult(
            False,
            False,
            state,
            (deadline_event,) if deadline_event is not None else (),
            (),
        )

    worker_rows = _poll_workers(effects, tuple(active))
    events: list[SyntheticHeartbeatEvent] = []
    if deadline_event is not None:
        events.append(deadline_event)
    parked: list[str] = []
    progress = False

    for slice_state in active:
        row = worker_rows.get(slice_state.id)
        if row is None or _retired_or_unrouted(row):
            continue
        if row.get("pane_alive") is False:
            if isinstance(effects, ReadOnlyEffectClient):
                raise HeartbeatError(
                    f"dead slice {slice_state.id!r} requires an active effect client to park"
                )
            park(
                slice_state,
                ParkCause.STALL_DETECTED,
                store=store,
                issue_creator=effects,
                ledger=state.budgets,
            )
            parked.append(slice_state.id)
            progress = True
            events.append(
                _event(
                    "worker.dead",
                    "poll_workers",
                    slice_state.id,
                    {"pane_alive": False},
                )
            )

    current = store.load()
    for slice_state in _active_slices(current):
        if slice_state.pr_number is None:
            continue
        watcher = _watch_pr(effects, slice_state.pr_number)
        updated, observed = _reconcile_pr(slice_state, watcher)
        if updated == slice_state:
            continue
        current = store.checkpoint(
            current.fsm,
            {**current.slices, slice_state.id: updated},
            current.budgets,
            current.events.last_consumed_offset,
        )
        progress = True
        events.append(
            _event(
                observed,
                "watcher_pr_state",
                slice_state.id,
                _pr_payload(watcher),
            )
        )

    prior_progress = current.goals.last_progress_at
    progress_at = current_time if progress or prior_progress is None else prior_progress
    if (
        not progress
        and prior_progress is not None
        and current_time - prior_progress >= config.stall_threshold_seconds
    ):
        for slice_state in _active_slices(current):
            events.append(
                _event(
                    "wave.stalled",
                    "heartbeat",
                    slice_state.id,
                    {
                        "stall_threshold_seconds": config.stall_threshold_seconds,
                        "action": "observe",
                    },
                )
            )

    updated_goals = replace(
        current.goals,
        last_heartbeat_at=current_time,
        last_progress_at=progress_at,
    )
    current = store.set_goals(updated_goals)
    return HeartbeatResult(
        True,
        progress,
        current,
        tuple(events),
        tuple(dict.fromkeys(parked)),
    )


def _active_slices(state: RunState) -> tuple[SliceState, ...]:

    active_statuses = {
        SliceStatus.SPAWNED,
        SliceStatus.IN_REVIEW,
        SliceStatus.REPAIRING,
    }
    return tuple(
        slice_state
        for slice_state in state.slices.values()
        if slice_state.status in active_statuses
    )


def _poll_workers(effects: LiveEffects, agents: tuple[SliceState, ...]) -> dict[str, JsonMapping]:
    result = effects.poll_workers(
        include_dead=True,
        agents=tuple(slice_state.id for slice_state in agents),
    )
    payload = _result_object(result, "poll_workers")
    rows = payload.get("workers")
    if not isinstance(rows, list):
        raise HeartbeatError("poll_workers result has no workers array")
    dead_rows = payload.get("dead_workers", [])
    if dead_rows is None:
        dead_rows = []
    elif not isinstance(dead_rows, list):
        raise HeartbeatError("poll_workers dead_workers must be an array")
    indexed: dict[str, JsonMapping] = {}
    _index_worker_rows(rows, indexed, dead=False)
    _index_worker_rows(dead_rows, indexed, dead=True)
    return indexed


def _index_worker_rows(
    rows: list[object],
    indexed: dict[str, JsonMapping],
    *,
    dead: bool,
) -> None:
    for row in rows:
        if not isinstance(row, Mapping):
            raise HeartbeatError("poll_workers worker array contains a non-object")
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        indexed[name] = {**row, "pane_alive": False} if dead else row


def _watch_pr(effects: LiveEffects, pr_number: int) -> JsonMapping:
    return _result_object(effects.watcher_pr_state(pr_number=pr_number), "watcher_pr_state")


def _result_object(result: ToolResult, operation: str) -> JsonMapping:
    if result.success is False:
        raise HeartbeatError(result.error or f"{operation} returned failure")
    if not isinstance(result.result, Mapping):
        raise HeartbeatError(f"{operation} result must be an object")
    return result.result


def _reconcile_pr(slice_state: SliceState, payload: JsonMapping) -> tuple[SliceState, str]:
    head_sha = payload.get("head_sha")
    if head_sha is not None and not isinstance(head_sha, str):
        raise HeartbeatError("watcher_pr_state head_sha must be a string or null")
    if head_sha and head_sha != slice_state.reviewed_head:
        return replace(
            slice_state, reviewed_head=head_sha, verdict=None, verdict_at=None
        ), "pr.updated"
    return slice_state, "pr.review"


def _pr_payload(payload: JsonMapping) -> dict[str, object]:
    return {
        key: payload[key] for key in ("head_sha", "review_state", "ci_status") if key in payload
    }


def _retired_or_unrouted(row: JsonMapping) -> bool:
    lifecycle = row.get("lifecycle_status")
    if not isinstance(lifecycle, str):
        return False
    normalized = lifecycle.upper()
    return normalized.startswith("RETIRED") or normalized == "NO-ROUTING-RECORDED"


def _event(kind: str, source: str, slice_id: str, payload: JsonMapping) -> SyntheticHeartbeatEvent:
    fingerprint = "|".join(f"{key}={payload[key]!r}" for key in sorted(payload))
    return SyntheticHeartbeatEvent(
        event_id=f"heartbeat:{source}:{kind}:{slice_id}:{fingerprint}",
        kind=kind,
        slice_id=slice_id,
        source=source,
        payload=payload,
    )


__all__ = [
    "HeartbeatConfig",
    "HeartbeatDeadlineExceeded",
    "HeartbeatError",
    "HeartbeatResult",
    "SyntheticHeartbeatEvent",
    "heartbeat_due",
    "heartbeat_once",
]
