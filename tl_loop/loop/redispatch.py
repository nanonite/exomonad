"""Explicit operator recovery for abandoned slice attempts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.client.readonly import ReadOnlyEffectClient
from tl_loop.fsm.phase import ChildHandle, TLPlanning, TLWaiting
from tl_loop.loop.driver import (
    TLLoopConfig,
    WorkPlan,
    _dispatch_children,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.schema import ParkCause, RunState, SliceStatus
from tl_loop.state.slice_transition import RedispatchRequested, slice_transition
from tl_loop.state.store import RunStore

DEFAULT_ABANDONMENT_RETRY_CEILING = 3
_RETRYABLE_CAUSE = ParkCause.ATTEMPT_ABANDONED


class RedispatchError(RuntimeError):
    """A slice cannot be safely redispatched."""


def redispatch_slice(
    project_root: str | Path,
    run_id: str,
    slice_id: str,
    plan: WorkPlan | Mapping[str, object],
    *,
    effects: EffectClient | ReadOnlyEffectClient,
    max_attempts: int = DEFAULT_ABANDONMENT_RETRY_CEILING,
    dispatch: bool = True,
    config: TLLoopConfig | None = None,
) -> dict[str, object]:
    """Re-dispatch one explicitly abandoned slice from its plan specification.

    Only ``ATTEMPT_ABANDONED`` is resumable. The persisted attempt count is
    charged by the normal dispatch boundary exactly once, and the old PR,
    branch, worktree, and review evidence are never carried into the fresh
    attempt. ``dispatch=False`` is useful for a controller restart handoff;
    the next normal run consumes the resulting ``PENDING`` slice.
    """
    if type(max_attempts) is not int or max_attempts <= 0:
        raise RedispatchError("max_attempts must be a positive integer")
    state_root = Path(project_root).expanduser().resolve() / ".exo" / "tl-loop"
    store = RunStore(run_id, state_root)
    state = store.load()
    current = state.slices.get(slice_id)
    if current is None:
        raise RedispatchError(f"slice {slice_id!r} does not exist")
    if current.status is not SliceStatus.PARKED or current.park_cause is not _RETRYABLE_CAUSE:
        raise RedispatchError(
            f"slice {slice_id!r} is not operator-abandoned; only ATTEMPT_ABANDONED is resumable"
        )
    if current.attempts >= max_attempts:
        exhausted = replace(current, park_cause=ParkCause.RETRIES_EXHAUSTED)
        state = store.checkpoint(
            state.fsm,
            {**state.slices, slice_id: exhausted},
            state.budgets,
            state.events.last_consumed_offset,
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
        )
        return {
            "status": "retries_exhausted",
            "slice_id": slice_id,
            "attempts": state.slices[slice_id].attempts,
            "max_attempts": max_attempts,
        }

    work_plan = plan if isinstance(plan, WorkPlan) else WorkPlan.from_mapping(plan)
    dispatch_plan = _plan_for_slice(work_plan, slice_id)
    task = _task_for_slice(dispatch_plan, slice_id) if dispatch_plan is not None else None
    if task is None:
        raise RedispatchError(f"slice {slice_id!r} is absent from the supplied plan specification")
    runtime_name = _runtime_name(slice_id, current.attempts + 1)
    reset = replace(
        slice_transition(current, RedispatchRequested()),
        park_cause=None,
        park_issue_id=None,
        pr_number=None,
        branch=None,
        worktree=None,
        agent_type=getattr(task, "agent_type", None),
        model=None,
        dispatch_intent_id=None,
        dispatch_started_at=None,
        dispatch_last_boundary="redispatch_requested",
        dispatch_error=None,
        dispatch_agent_id=None,
        dispatch_invocation_id=None,
        dispatch_authoritative_event_seq=None,
        reconciliation=None,
    )
    state = store.checkpoint(
        _redispatch_phase(state),
        {**state.slices, slice_id: reset},
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    if not dispatch:
        return {
            "status": "dispatchable",
            "slice_id": slice_id,
            "attempts": state.slices[slice_id].attempts,
            "runtime_name": runtime_name,
        }

    selected = config or TLLoopConfig(
        active=True,
        root_dir=state_root,
        project_root=Path(project_root).expanduser().resolve(),
        run_id=run_id,
        branch=state.owner_branch or "main",
        dispatch_names={slice_id: runtime_name},
    )
    if slice_id not in selected.dispatch_names:
        selected = replace(
            selected,
            dispatch_names={**selected.dispatch_names, slice_id: runtime_name},
        )
    journal = EffectJournal(run_id, store.run_dir / "action-journal.json")
    state = _dispatch_children(dispatch_plan, state, selected, effects, journal, store)
    current = state.slices[slice_id]
    return {
        "status": "dispatched",
        "slice_id": slice_id,
        "attempts": current.attempts,
        "runtime_name": runtime_name,
        "dispatch_status": current.status.value,
    }


def _task_for_slice(plan: WorkPlan, slice_id: str) -> object | None:
    for task in (*plan.workers, *plan.leaves, *plan.sub_tls):
        if task.name == slice_id:
            return task
    for sub_tl in plan.sub_tls:
        nested = (
            sub_tl.plan if isinstance(sub_tl.plan, WorkPlan) else WorkPlan.from_mapping(sub_tl.plan)
        )
        found = _task_for_slice(nested, slice_id)
        if found is not None:
            return found
    return None


def _plan_for_slice(plan: WorkPlan, slice_id: str) -> WorkPlan | None:
    if any(task.name == slice_id for task in (*plan.workers, *plan.leaves, *plan.sub_tls)):
        return plan
    for sub_tl in plan.sub_tls:
        nested = (
            sub_tl.plan if isinstance(sub_tl.plan, WorkPlan) else WorkPlan.from_mapping(sub_tl.plan)
        )
        found = _plan_for_slice(nested, slice_id)
        if found is not None:
            return found
    return None


def _runtime_name(slice_id: str, attempt: int) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slice_id).strip("-") or "slice"
    return f"{slug}-attempt-{attempt}"


def _redispatch_phase(state: RunState) -> object:
    """Keep unrelated active children in the durable waiting set."""
    active = {
        slice_state.id: ChildHandle(
            slice_state.id,
            slice_state.branch or "",
            slice_state.agent_type or "unknown",
        )
        for slice_state in state.slices.values()
        if slice_state.status in {SliceStatus.SPAWNED, SliceStatus.IN_REVIEW, SliceStatus.REPAIRING}
    }
    return TLWaiting(active) if active else TLPlanning()


__all__ = [
    "DEFAULT_ABANDONMENT_RETRY_CEILING",
    "RedispatchError",
    "redispatch_slice",
]
