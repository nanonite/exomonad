"""Public recursive TL transition facade.

The durable values, scope events, runtime completion helpers, post-merge FSM,
and repository lane FSM live in focused modules.  This module preserves the
stable import surface while composing their guarded transitions.
"""

from __future__ import annotations

from .child import ChildKind, ChildRecord, TLOrchestrationEvent
from .evidence import require_text as _require_text
from .lane import (
    LaneAbandoned,
    LaneBookkeepingStarted,
    LaneIntegrationStarted,
    LaneParkRequested,
    LanePhase,
    LaneRecoveryRequested,
    LaneRecoveryResolved,
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
    BaseInvalidated,
    ChildDispatchRequested,
    ChildSpawned,
    ChildTerminal,
    CIObserved,
    FailureRecorded,
    FinalizationComplete,
    FinalizationRequested,
    Heartbeat,
    IntegrationValidated,
    LeafCompleted,
    MergeRequested,
    ParkRequested,
    PhaseValue,
    PlanLoaded,
    PostMergeObserved,
    PublicationFiled,
    RecoveryObserved,
    RepairRequested,
    ReviewObserved,
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
    complete_leaf,
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
    if isinstance(event, PlanLoaded):
        if not isinstance(phase, TLPlanning):
            raise IllegalTransition("a plan can only be loaded while planning")
        if event.scope_path != phase.scope_path or event.plan_digest != phase.plan_digest:
            raise IllegalTransition("loaded plan identity does not match scope")
        return phase
    if isinstance(event, FailureRecorded) and isinstance(
        phase, (TLPlanning, TLRunning, TLAllMerged, TLFinalizing)
    ):
        _require_text(event.reason, "failure reason")
        return TLFailed(event.reason, getattr(phase, "scope_path", ("root",)))
    if isinstance(event, ParkRequested) and isinstance(
        phase, (TLPlanning, TLRunning, TLAllMerged, TLFinalizing)
    ):
        _require_text(event.cause, "park cause")
        _require_text(event.diagnostic, "park diagnostic")
        return TLParked(event.cause, event.diagnostic, getattr(phase, "scope_path", ("root",)))
    if isinstance(phase, TLPlanning) and isinstance(event, StageReleased):
        return release_first_stage(phase, event)
    if isinstance(phase, TLRunning):
        if isinstance(event, ChildDispatchRequested):
            _active_child(phase, event.child_id)
            return phase
        if isinstance(event, ChildSpawned):
            _active_child(phase, event.child_id)
            return phase
        if isinstance(event, ChildTerminal):
            return _child_terminal(phase, event)
        if isinstance(
            event,
            (
                PublicationFiled,
                ReviewObserved,
                CIObserved,
                BaseInvalidated,
                IntegrationValidated,
                MergeRequested,
                PostMergeObserved,
                RepairRequested,
                RecoveryObserved,
                Heartbeat,
            ),
        ):
            if hasattr(event, "child_id"):
                _active_child(phase, event.child_id)
            return phase
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
        if isinstance(event, LeafCompleted):
            return complete_leaf(phase, event)
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


def _active_child(phase: TLRunning, child_id: str) -> ChildRecord:
    records = (*phase.parallel_pending, *phase.pending_by_order.get(phase.current_order, ()))
    for record in records:
        if record.child_id == child_id:
            return record
    raise IllegalTransition("scope event names a child outside the active barrier")


def _child_terminal(phase: TLRunning, event: ChildTerminal) -> PhaseValue:
    record = _active_child(phase, event.child_id)
    if record.kind is ChildKind.WORKER:
        result_digest = event.evidence.get("result_digest")
        if event.outcome not in {"success", "completed"} or not result_digest:
            raise IllegalTransition("worker terminal evidence requires a result digest")
        return complete_worker(phase, WorkerCompleted(event.child_id, result_digest))
    if event.outcome not in {"success", "completed"}:
        raise IllegalTransition("PR-producing child failure requires a failure event")
    return phase


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
    "LaneAbandoned",
    "LaneBookkeepingStarted",
    "LaneIntegrationStarted",
    "LaneParkRequested",
    "LanePhase",
    "LaneRecoveryRequested",
    "LaneRecoveryResolved",
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
