"""Typed events for recursive TL scope transitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .child import TLOrchestrationEvent


class ScopeRole(str, Enum):
    """Whether finalization belongs to the root or a direct parent."""

    ROOT = "root"
    NON_ROOT = "non_root"


@dataclass(frozen=True)
class PlanLoaded(TLOrchestrationEvent):
    """Load the immutable declaration before any scope work is released."""

    scope_path: tuple[str, ...]
    plan_digest: str
    child_ids: tuple[str, ...]
    orders: tuple[int, ...]


@dataclass(frozen=True)
class StageReleased(TLOrchestrationEvent):
    """Release direct work or the first ordered sub-TL stage."""

    order: int
    child_ids: tuple[str, ...]
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildDispatchRequested(TLOrchestrationEvent):
    """Persist a dispatch intent for one declared child."""

    child_id: str
    invocation_id: str
    attempt: int
    intent_id: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildSpawned(TLOrchestrationEvent):
    """Confirm a child invocation without changing the active barrier."""

    child_id: str
    invocation_id: str
    attempt: int
    branch: str
    worktree: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChildTerminal(TLOrchestrationEvent):
    """Record a typed child terminal observation."""

    child_id: str
    outcome: str
    evidence: Mapping[str, str]
    scope_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class PublicationFiled(TLOrchestrationEvent):
    """Bind a publication observation to its declared child and exact head."""

    child_id: str
    pr_number: int
    head_sha: str
    base_branch: str
    digest: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewObserved(TLOrchestrationEvent):
    """Observe review evidence without bypassing the slice review reducer."""

    child_id: str
    review_id: int
    verdict: str
    head_sha: str
    evidence: Mapping[str, str]
    scope_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class CIObserved(TLOrchestrationEvent):
    """Observe CI evidence for one exact child head."""

    child_id: str
    head_sha: str
    status: str
    evidence: Mapping[str, str]
    scope_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class BaseInvalidated(TLOrchestrationEvent):
    """Invalidate integration when the expected parent base moved."""

    child_id: str
    expected_base: str
    observed_base: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntegrationValidated(TLOrchestrationEvent):
    """Confirm exact base/head/tree/CI evidence before a merge request."""

    child_id: str
    base_sha: str
    head_sha: str
    tree_sha: str
    ci: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeRequested(TLOrchestrationEvent):
    """Persist a compare-guarded merge intent owned by the direct parent."""

    child_id: str
    pr_number: int
    expected_head_sha: str
    intent_id: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostMergeObserved(TLOrchestrationEvent):
    """Observe a durable post-merge checkpoint without skipping its FSM."""

    child_id: str
    checkpoint: Mapping[str, str]
    scope_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "checkpoint", MappingProxyType(dict(self.checkpoint)))


@dataclass(frozen=True)
class RepairRequested(TLOrchestrationEvent):
    """Queue bounded repair for one child with its actionable findings."""

    child_id: str
    reason: str
    findings: tuple[Mapping[str, str], ...]
    attempt: int
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryObserved(TLOrchestrationEvent):
    """Record reconciliation of an existing effect journal entry."""

    child_id: str
    journal_id: str
    disposition: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class Heartbeat(TLOrchestrationEvent):
    """Record a bounded observation without advancing child work."""

    observed_at: str


@dataclass(frozen=True)
class WorkerCompleted(TLOrchestrationEvent):
    """Complete a typed worker without a PR/post-merge sequence."""

    child_id: str
    result_digest: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeafCompleted(TLOrchestrationEvent):
    """Complete a direct leaf through its own non-worker contract."""

    child_id: str
    result_digest: str
    scope_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalizationRequested(TLOrchestrationEvent):
    """Enter root checkout or non-root publication finalization."""

    role: ScopeRole


@dataclass(frozen=True)
class FinalizationComplete(TLOrchestrationEvent):
    """Confirm role-specific finalization evidence."""

    role: ScopeRole
    evidence: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True)
class FailureRecorded(TLOrchestrationEvent):
    """Stop automatic progression with a durable reason."""

    reason: str


@dataclass(frozen=True)
class ParkRequested(TLOrchestrationEvent):
    """Stop automatic progression behind a durable gate."""

    cause: str
    diagnostic: str


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
    "PlanLoaded",
    "PostMergeObserved",
    "PublicationFiled",
    "RecoveryObserved",
    "RepairRequested",
    "ReviewObserved",
    "ScopeRole",
    "StageReleased",
    "WorkerCompleted",
]
