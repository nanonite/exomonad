"""Runtime child completion helpers for the recursive scope reducer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from .child import ChildKind, ChildRecord
from .post_merge import PostMergePhase, PostMergeState
from .post_merge_transition import advance_post_merge
from .scope import (
    PhaseValue,
    TLAllMerged,
    TLPlanning,
    TLRunning,
)


class IllegalTransition(ValueError):
    """An event is not legal for the current target phase."""


def release_first_stage(phase: TLPlanning, event: object) -> PhaseValue:
    """Release only the first ordered stage or the direct parallel block."""
    if phase.ordered_children:
        order, records = phase.ordered_children[0]
        expected = tuple(record.child_id for record in records)
        if event.order != order or tuple(event.child_ids) != expected:
            raise IllegalTransition("planning may release only its first ordered stage")
        return running_from_plan(phase, order, phase.parallel_children)
    expected = tuple(record.child_id for record in phase.parallel_children)
    if event.order != 0 or tuple(event.child_ids) != expected:
        raise IllegalTransition("planning may release only its direct parallel block")
    if not expected:
        return TLAllMerged(phase.scope_path, phase.plan_digest)
    return running_from_plan(phase, 0, phase.parallel_children)


def running_from_plan(
    phase: TLPlanning,
    current_order: int,
    parallel_pending: tuple[ChildRecord, ...],
) -> TLRunning:
    """Materialize durable runtime records from a loaded plan."""
    all_records = (
        *parallel_pending,
        *(record for _, group in phase.ordered_children for record in group),
    )
    return TLRunning(
        current_order=current_order,
        pending_by_order=dict(phase.ordered_children),
        scope_path=phase.scope_path,
        plan_digest=phase.plan_digest,
        parallel_pending=parallel_pending,
        dispatch_intents={
            record.child_id: record.dispatch_intent_id
            for record in all_records
            if record.dispatch_intent_id is not None
        },
        lane_bindings={
            record.child_id: record.lane_id for record in all_records if record.lane_id is not None
        },
    )


def complete_worker(phase: TLRunning, event: object) -> PhaseValue:
    """Record a worker result and remove that worker from parallel work."""
    record = active_record(phase, event.child_id)
    if record.kind is not ChildKind.WORKER:
        raise IllegalTransition("WorkerCompleted cannot complete a leaf or sub-TL")
    if not event.result_digest:
        raise ValueError("worker result digest must be non-empty")
    post = dict(phase.post_merge)
    post[event.child_id] = PostMergeState(
        PostMergePhase.NOT_REQUIRED,
        {**post[event.child_id].evidence, "worker_result": event.result_digest},
    )
    evidence = dict(phase.evidence)
    evidence[f"{event.child_id}:worker_result"] = event.result_digest
    return remove_active(phase, event.child_id, post, evidence)


def complete_leaf(phase: TLRunning, event: object) -> PhaseValue:
    """Record a direct leaf result without granting worker completion rights."""
    record = active_record(phase, event.child_id)
    if record.kind is not ChildKind.LEAF:
        raise IllegalTransition("LeafCompleted cannot complete a worker or sub-TL")
    if not event.result_digest:
        raise ValueError("leaf result digest must be non-empty")
    post = dict(phase.post_merge)
    post[event.child_id] = PostMergeState(
        PostMergePhase.NOT_REQUIRED,
        {**post[event.child_id].evidence, "leaf_result": event.result_digest},
    )
    evidence = dict(phase.evidence)
    evidence[f"{event.child_id}:leaf_result"] = event.result_digest
    return remove_active(phase, event.child_id, post, evidence)


def post_merge_transition(phase: TLRunning, event: object) -> PhaseValue:
    """Advance one active PR child through the post-merge reducer."""
    current = phase.post_merge.get(event.child_id)
    if current is None:
        raise IllegalTransition("post-merge event names an unknown child")
    if current.phase is PostMergePhase.COMPLETE:
        try:
            repeated = advance_post_merge(current, event)
        except (TypeError, ValueError) as exc:
            raise IllegalTransition(str(exc)) from exc
        if repeated == current:
            return phase
        raise IllegalTransition("post-merge child is already complete")
    record = active_record(phase, event.child_id)
    if record.kind is ChildKind.WORKER:
        raise IllegalTransition("workers do not have a PR post-merge sequence")
    try:
        next_state = advance_post_merge(current, event)
    except (TypeError, ValueError) as exc:
        raise IllegalTransition(str(exc)) from exc
    post = dict(phase.post_merge)
    post[event.child_id] = next_state
    if next_state.phase is not PostMergePhase.COMPLETE:
        return replace(phase, post_merge=post)
    return remove_active(phase, event.child_id, post, dict(phase.evidence))


def remove_active(
    phase: TLRunning,
    child_id: str,
    post_merge: Mapping[str, PostMergeState],
    evidence: Mapping[str, str],
) -> PhaseValue:
    """Remove one completed child and open the next barrier when ready."""
    completed = dict(phase.completed_children)
    completed[child_id] = active_record(phase, child_id)
    if any(record.child_id == child_id for record in phase.parallel_pending):
        remaining = tuple(
            record for record in phase.parallel_pending if record.child_id != child_id
        )
        if remaining:
            return replace(
                phase,
                parallel_pending=remaining,
                completed_children=completed,
                post_merge=post_merge,
                evidence=evidence,
            )
        if phase.current_order == 0:
            return advance_after_parallel(phase, completed, post_merge, evidence)
        return replace(
            phase,
            parallel_pending=(),
            completed_children=completed,
            post_merge=post_merge,
            evidence=evidence,
        )
    pending = {
        order: tuple(record for record in records if record.child_id != child_id)
        for order, records in phase.pending_by_order.items()
    }
    current = pending[phase.current_order]
    if current:
        return replace(
            phase,
            pending_by_order=pending,
            completed_children=completed,
            post_merge=post_merge,
            evidence=evidence,
        )
    del pending[phase.current_order]
    if pending:
        return replace(
            phase,
            current_order=min(pending),
            pending_by_order=pending,
            completed_children=completed,
            post_merge=post_merge,
            evidence=evidence,
        )
    if phase.parallel_pending:
        return replace(
            phase,
            current_order=0,
            pending_by_order={},
            completed_children=completed,
            post_merge=post_merge,
            evidence=evidence,
        )
    return all_merged(phase, post_merge)


def advance_after_parallel(
    phase: TLRunning,
    completed: Mapping[str, ChildRecord],
    post_merge: Mapping[str, PostMergeState],
    evidence: Mapping[str, str],
) -> PhaseValue:
    """Open the first ordered barrier after parallel work drains."""
    if phase.pending_by_order:
        return replace(
            phase,
            current_order=min(phase.pending_by_order),
            parallel_pending=(),
            completed_children=completed,
            post_merge=post_merge,
            evidence=evidence,
        )
    return all_merged(phase, post_merge)


def all_merged(phase: TLRunning, post_merge: Mapping[str, PostMergeState]) -> TLAllMerged:
    """Build the durable all-direct-children completion value."""
    return TLAllMerged(phase.scope_path, phase.plan_digest, post_merge)


def active_record(phase: TLRunning, child_id: str) -> ChildRecord:
    """Find a child in the independent parallel block or current barrier."""
    records = list(phase.parallel_pending)
    records.extend(phase.pending_by_order.get(phase.current_order, ()))
    for record in records:
        if record.child_id == child_id:
            return record
    raise IllegalTransition("child is not pending in the current order or parallel block")


__all__ = [
    "IllegalTransition",
    "active_record",
    "all_merged",
    "complete_leaf",
    "complete_worker",
    "post_merge_transition",
    "release_first_stage",
    "running_from_plan",
]
