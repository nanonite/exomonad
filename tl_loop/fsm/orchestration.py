"""Public recursive TL transition facade.

The durable values, scope events, runtime completion helpers, post-merge FSM,
and repository lane FSM live in focused modules.  This module preserves the
stable import surface while composing their guarded transitions.
"""

from __future__ import annotations

from .child import ChildKind, ChildRecord, TLOrchestrationEvent
from .evidence import require_text as _require_text
from .lane import (
    LaneBookkeepingStarted,
    LaneIntegrationStarted,
    LaneParkRequested,
    LanePhase,
    LaneRecoveryRequested,
    LaneReleased,
    LaneReserved,
    LaneState,
)
from .lane import transition_lane as _transition_lane
from .post_merge import PostMergePhase, PostMergeState
from .post_merge_events import (
    ChangelogCommitted,
    ChangelogPending,
    IssueCloseConfirmed,
    IssueClosePending,
    MergeAdopted,
    ParentBranchSynced,
    ParentPushPending,
    PostMergeComplete,
    PostMergeRebuildRequested,
)
from .post_merge_evidence import PushReceipt
from .scope import (
    FailureRecorded,
    FinalizationComplete,
    FinalizationRequested,
    ParkRequested,
    PhaseValue,
    ScopeRole,
    StageReleased,
    TLAllMerged,
    TLDone,
    TLFailed,
    TLFinalizing,
    TLParked,
    TLPlanning,
    TLPRFiled,
    TLRunning,
    WorkerCompleted,
)
from .scope_runtime import (
    IllegalTransition,
    complete_worker,
    post_merge_transition,
    release_first_stage,
)


def transition_lane(lane: LaneState, event: TLOrchestrationEvent) -> LaneState:
    """Expose lane transitions with the scope reducer's error type."""
    try:
        return _transition_lane(lane, event)
    except ValueError as exc:
        raise IllegalTransition(str(exc)) from exc


def stable_integration_order(child_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return deterministic child-ID order independent of review arrival."""
    if not child_ids or any(
        not isinstance(child_id, str) or not child_id for child_id in child_ids
    ):
        raise ValueError("integration child IDs must be non-empty")
    if len(set(child_ids)) != len(child_ids):
        raise ValueError("integration child IDs must be unique")
    return tuple(sorted(child_ids))


def transition(phase: PhaseValue, event: TLOrchestrationEvent) -> PhaseValue:
    """Apply one scope event; terminal phases have no automatic arms."""
    if isinstance(event, FailureRecorded) and isinstance(
        phase, (TLPlanning, TLRunning, TLAllMerged, TLFinalizing)
    ):
        _require_text(event.reason, "failure reason")
        return TLFailed(event.reason)
    if isinstance(event, ParkRequested) and isinstance(
        phase, (TLPlanning, TLRunning, TLAllMerged, TLFinalizing)
    ):
        _require_text(event.cause, "park cause")
        _require_text(event.diagnostic, "park diagnostic")
        return TLParked(event.cause, event.diagnostic)
    if isinstance(phase, TLPlanning) and isinstance(event, StageReleased):
        return release_first_stage(phase, event)
    if isinstance(phase, TLRunning):
        if isinstance(
            event,
            (
                MergeAdopted,
                ParentBranchSynced,
                IssueClosePending,
                IssueCloseConfirmed,
                ChangelogPending,
                ChangelogCommitted,
                ParentPushPending,
                PostMergeComplete,
                PostMergeRebuildRequested,
            ),
        ):
            return post_merge_transition(phase, event)
        if isinstance(event, WorkerCompleted):
            return complete_worker(phase, event)
    if isinstance(phase, TLAllMerged) and isinstance(event, FinalizationRequested):
        if not isinstance(event.role, ScopeRole):
            raise TypeError("finalization role is invalid")
        return TLFinalizing(event.role, phase.scope_path, phase.plan_digest)
    if isinstance(phase, TLFinalizing) and isinstance(event, FinalizationComplete):
        if event.role is not phase.role:
            raise IllegalTransition("finalization role does not match scope")
        _validate_finalization_evidence(event)
        if event.role is ScopeRole.ROOT:
            return TLDone(phase.scope_path, phase.plan_digest, event.evidence)
        return TLPRFiled(
            aggregate_pr=event.evidence["aggregate_pr"],
            head_sha=event.evidence["head_sha"],
            base_sha=event.evidence["base_sha"],
            parent_branch=event.evidence["parent_branch"],
            handoff=event.evidence["handoff"],
            scope_path=phase.scope_path,
            plan_digest=phase.plan_digest,
        )
    raise IllegalTransition(f"no transition for {type(phase).__name__} and {type(event).__name__}")


def _validate_finalization_evidence(event: FinalizationComplete) -> None:
    required = (
        ("root_branch", "local_checkout")
        if event.role is ScopeRole.ROOT
        else ("aggregate_pr", "head_sha", "base_sha", "parent_branch", "handoff")
    )
    if any(not event.evidence.get(key) for key in required):
        raise ValueError("finalization evidence is incomplete for scope role")


__all__ = [
    "ChangelogCommitted",
    "ChangelogPending",
    "ChildKind",
    "ChildRecord",
    "FailureRecorded",
    "FinalizationComplete",
    "FinalizationRequested",
    "IllegalTransition",
    "IssueCloseConfirmed",
    "IssueClosePending",
    "LaneBookkeepingStarted",
    "LaneIntegrationStarted",
    "LaneParkRequested",
    "LanePhase",
    "LaneRecoveryRequested",
    "LaneReleased",
    "LaneReserved",
    "LaneState",
    "MergeAdopted",
    "ParentBranchSynced",
    "ParentPushPending",
    "ParkRequested",
    "PhaseValue",
    "PostMergeComplete",
    "PostMergePhase",
    "PostMergeRebuildRequested",
    "PostMergeState",
    "PushReceipt",
    "ScopeRole",
    "StageReleased",
    "TLAllMerged",
    "TLDone",
    "TLFailed",
    "TLFinalizing",
    "TLOrchestrationEvent",
    "TLPRFiled",
    "TLParked",
    "TLPlanning",
    "TLRunning",
    "WorkerCompleted",
    "stable_integration_order",
    "transition",
    "transition_lane",
]
