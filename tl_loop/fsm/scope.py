"""Recursive TL scope values and scope-level events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeAlias

from .child import ChildKind, ChildRecord
from .evidence import require_text as _require_text
from .post_merge import PostMergePhase, PostMergeState
from .scope_events import (
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
    PlanLoaded,
    PostMergeObserved,
    PublicationFiled,
    RecoveryObserved,
    RepairRequested,
    ReviewObserved,
    ScopeRole,
    StageReleased,
    WorkerCompleted,
)
from .scope_validation import sorted_records as _sorted_records
from .scope_validation import validate_ordered_children as _validate_ordered_children
from .scope_validation import validate_scope as _validate_scope
from .scope_validation import validate_unique_records as _validate_unique_records


@dataclass(frozen=True)
class TLPlanning:
    """A loaded recursive scope whose first block has not started."""

    ordered_children: tuple[tuple[int, tuple[ChildRecord, ...]], ...] = ()
    scope_path: tuple[str, ...] = ("root",)
    plan_digest: str = "plan"
    parallel_children: tuple[ChildRecord, ...] = ()

    def __post_init__(self) -> None:
        _validate_scope(self.scope_path, self.plan_digest)
        parallel = _sorted_records(self.parallel_children)
        if any(record.kind is ChildKind.SUB_TL for record in parallel):
            raise ValueError("sub-TL children belong in ordered stages")
        ordered = []
        for order, records in self.ordered_children:
            if type(order) is not int or order <= 0:
                raise ValueError("ordered child groups must use positive orders")
            normalized = _sorted_records(records)
            if any(record.kind is not ChildKind.SUB_TL for record in normalized):
                raise ValueError("ordered stages accept only sub-TL children")
            ordered.append((order, normalized))
        _validate_ordered_children(tuple(ordered))
        _validate_unique_records(parallel, [record for _, group in ordered for record in group])
        object.__setattr__(self, "parallel_children", parallel)
        object.__setattr__(self, "ordered_children", tuple(ordered))


@dataclass(frozen=True)
class TLRunning:
    """A running scope with independent parallel and ordered work."""

    current_order: int
    pending_by_order: Mapping[int, tuple[ChildRecord, ...]]
    scope_path: tuple[str, ...] = ("root",)
    plan_digest: str = "plan"
    parallel_pending: tuple[ChildRecord, ...] = ()
    completed_children: Mapping[str, ChildRecord] = field(default_factory=dict)
    post_merge: Mapping[str, PostMergeState] = field(default_factory=dict)
    dispatch_intents: Mapping[str, str] = field(default_factory=dict)
    evidence: Mapping[str, str] = field(default_factory=dict)
    lane_bindings: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_scope(self.scope_path, self.plan_digest)
        if type(self.current_order) is not int or self.current_order < 0:
            raise ValueError("running current order must be non-negative")
        groups = tuple(
            (order, _sorted_records(records)) for order, records in self.pending_by_order.items()
        )
        _validate_ordered_children(
            groups, first_order=1 if self.current_order == 0 else self.current_order
        )
        parallel = _sorted_records(self.parallel_pending)
        if self.current_order == 0 and not parallel:
            raise ValueError("order zero requires pending direct workers or leaves")
        if self.current_order > 0 and self.current_order not in dict(groups):
            raise ValueError("running current order must have pending children")
        _validate_unique_records(parallel, [record for _, group in groups for record in group])
        completed = dict(self.completed_children)
        if any(not isinstance(record, ChildRecord) for record in completed.values()):
            raise TypeError("completed children require ChildRecord values")
        _validate_unique_records(
            parallel, [record for _, group in groups for record in group], completed.values()
        )
        all_records = (
            list(parallel)
            + [record for _, group in groups for record in group]
            + list(completed.values())
        )
        all_ids = {record.child_id for record in all_records}
        post = dict(self.post_merge)
        for record in all_records:
            post.setdefault(
                record.child_id,
                PostMergeState(
                    PostMergePhase.NOT_REQUIRED
                    if record.kind is ChildKind.WORKER
                    else PostMergePhase.NOT_STARTED
                ),
            )
        if any(child_id not in all_ids for child_id in post):
            raise ValueError("post-merge state contains an unknown child")
        for child_id, record in completed.items():
            state = post[child_id]
            if record.kind is ChildKind.WORKER:
                if state.phase is not PostMergePhase.NOT_REQUIRED:
                    raise ValueError("completed workers require NOT_REQUIRED post-merge state")
                if not state.evidence.get("worker_result"):
                    raise ValueError("completed workers require result evidence")
            elif record.kind is ChildKind.LEAF:
                if state.phase is PostMergePhase.COMPLETE:
                    continue
                if state.phase is not PostMergePhase.NOT_REQUIRED or not state.evidence.get(
                    "leaf_result"
                ):
                    raise ValueError("completed leaves require result evidence")
            elif state.phase is not PostMergePhase.COMPLETE:
                raise ValueError("completed PR children require COMPLETE post-merge state")
        object.__setattr__(self, "pending_by_order", MappingProxyType(dict(groups)))
        object.__setattr__(self, "parallel_pending", parallel)
        object.__setattr__(self, "completed_children", MappingProxyType(completed))
        object.__setattr__(self, "post_merge", MappingProxyType(post))
        object.__setattr__(self, "dispatch_intents", MappingProxyType(dict(self.dispatch_intents)))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "lane_bindings", MappingProxyType(dict(self.lane_bindings)))


@dataclass(frozen=True)
class TLAllMerged:
    """All direct children reached an evidence-backed completion state."""

    scope_path: tuple[str, ...] = ("root",)
    plan_digest: str = "plan"
    completed_children: Mapping[str, PostMergeState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_scope(self.scope_path, self.plan_digest)
        completed = dict(self.completed_children)
        if any(not isinstance(state, PostMergeState) for state in completed.values()):
            raise TypeError("all-merged children require PostMergeState values")
        for state in completed.values():
            if state.phase is PostMergePhase.COMPLETE:
                continue
            if state.phase is PostMergePhase.NOT_REQUIRED and (
                state.evidence.get("worker_result") or state.evidence.get("leaf_result")
            ):
                continue
            raise ValueError("all-merged confirmations must carry completion evidence")
        object.__setattr__(self, "completed_children", MappingProxyType(completed))


@dataclass(frozen=True)
class TLFinalizing:
    """The scope is finalizing its root checkout or parent handoff."""

    role: ScopeRole
    scope_path: tuple[str, ...] = ("root",)
    plan_digest: str = "plan"
    evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.role, ScopeRole):
            raise TypeError("finalization role must be a ScopeRole")
        _validate_scope(self.scope_path, self.plan_digest)
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class TLDone:
    """The root scope's branch/local-checkout finalization is complete."""

    scope_path: tuple[str, ...] = ("root",)
    plan_digest: str = "plan"
    finalization_evidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_scope(self.scope_path, self.plan_digest)
        object.__setattr__(
            self, "finalization_evidence", MappingProxyType(dict(self.finalization_evidence))
        )


@dataclass(frozen=True)
class TLPRFiled:
    """A non-root scope's terminal aggregate publication handoff."""

    aggregate_pr: str
    head_sha: str
    base_sha: str
    parent_branch: str
    handoff: str
    scope_path: tuple[str, ...] = ("root",)
    plan_digest: str = "plan"

    def __post_init__(self) -> None:
        for name in ("aggregate_pr", "head_sha", "base_sha", "parent_branch", "handoff"):
            _require_text(getattr(self, name), f"non-root {name}")
        _validate_scope(self.scope_path, self.plan_digest)


@dataclass(frozen=True)
class TLFailed:
    """A failure awaiting explicit operator recovery."""

    reason: str
    scope_path: tuple[str, ...] = ("root",)
    last_evidence: Mapping[str, str] = field(default_factory=dict)
    next_transition: str = "operator_recovery"

    def __post_init__(self) -> None:
        _require_text(self.reason, "failure reason")
        _validate_scope(self.scope_path, "failure")
        object.__setattr__(self, "last_evidence", MappingProxyType(dict(self.last_evidence)))


@dataclass(frozen=True)
class TLParked:
    """A typed external or human gate prevents automatic progression."""

    cause: str
    diagnostic: str
    scope_path: tuple[str, ...] = ("root",)
    next_transition: str = "operator_recovery"

    def __post_init__(self) -> None:
        _require_text(self.cause, "park cause")
        _require_text(self.diagnostic, "park diagnostic")
        _validate_scope(self.scope_path, "park")


PhaseValue: TypeAlias = (
    TLPlanning | TLRunning | TLAllMerged | TLFinalizing | TLDone | TLPRFiled | TLFailed | TLParked
)


__all__ = [
    "BaseInvalidated",
    "CIObserved",
    "ChildDispatchRequested",
    "ChildSpawned",
    "ChildTerminal",
    "FailureRecorded",
    "FinalizationComplete",
    "FinalizationRequested",
    "Heartbeat",
    "IntegrationValidated",
    "LeafCompleted",
    "MergeRequested",
    "ParkRequested",
    "PhaseValue",
    "PlanLoaded",
    "PostMergeObserved",
    "PublicationFiled",
    "RecoveryObserved",
    "RepairRequested",
    "ReviewObserved",
    "ScopeRole",
    "StageReleased",
    "TLAllMerged",
    "TLDone",
    "TLFailed",
    "TLFinalizing",
    "TLPRFiled",
    "TLParked",
    "TLPlanning",
    "TLRunning",
    "WorkerCompleted",
]
