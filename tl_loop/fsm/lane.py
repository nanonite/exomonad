"""Serialized repository-lane ownership and push-confirmation transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .child import TLOrchestrationEvent
from .evidence import require_text as _require_text
from .post_merge_evidence import PushReceipt


class LanePhase(str, Enum):
    """Durable ownership phase for one repository/parent-branch lane."""

    IDLE = "idle"
    RESERVED = "reserved"
    INTEGRATING = "integrating"
    BOOKKEEPING = "bookkeeping"
    RECOVERY = "recovery"
    PARKED = "parked"


@dataclass(frozen=True)
class LaneState:
    """A lane keyed by repository identity and parent branch."""

    repository: str
    parent_branch: str
    phase: LanePhase = LanePhase.IDLE
    child_id: str | None = None
    lane_epoch: int | None = None
    expected_base_sha: str | None = None
    head_sha: str | None = None
    merge_journal_id: str | None = None
    push_intent_id: str | None = None
    push_journal_id: str | None = None
    changelog_commit: str | None = None
    last_push_receipt_id: str | None = None
    last_remote_head: str | None = None
    last_ancestry_proof: str | None = None
    last_lane_epoch: int = 0

    def __post_init__(self) -> None:
        _require_text(self.repository, "lane repository")
        _require_text(self.parent_branch, "lane parent branch")
        if not isinstance(self.phase, LanePhase):
            raise TypeError("lane phase must be a LanePhase")
        if type(self.last_lane_epoch) is not int or self.last_lane_epoch < 0:
            raise ValueError("last lane epoch must be a non-negative integer")


@dataclass(frozen=True)
class LaneReserved(TLOrchestrationEvent):
    """Reserve a lane for one child and expected base."""

    child_id: str
    lane_epoch: int
    expected_base_sha: str


@dataclass(frozen=True)
class LaneIntegrationStarted(TLOrchestrationEvent):
    """Start compare-bound integration in a reserved lane."""

    child_id: str
    head_sha: str


@dataclass(frozen=True)
class LaneBookkeepingStarted(TLOrchestrationEvent):
    """Start bookkeeping with its commit and push intent bound."""

    child_id: str
    merge_journal_id: str
    push_intent_id: str
    push_journal_id: str
    changelog_commit: str
    expected_base_sha: str


@dataclass(frozen=True)
class LaneReleased(TLOrchestrationEvent):
    """Release a lane only with an authoritative remote push receipt."""

    child_id: str
    receipt: PushReceipt


@dataclass(frozen=True)
class LaneRecoveryRequested(TLOrchestrationEvent):
    """Move an uncertain lane effect behind recovery."""

    diagnostic: str


@dataclass(frozen=True)
class LaneRecoveryResolved(TLOrchestrationEvent):
    """Re-enter integration after an authoritative merged observation."""

    child_id: str
    head_sha: str


@dataclass(frozen=True)
class LaneParkRequested(TLOrchestrationEvent):
    """Park a lane when automatic progression is unsafe."""

    cause: str
    diagnostic: str


@dataclass(frozen=True)
class LaneAbandoned(TLOrchestrationEvent):
    """Release a lane after its child-owned work has entered a durable gate."""

    cause: str
    diagnostic: str


def transition_lane(lane: LaneState, event: TLOrchestrationEvent) -> LaneState:
    """Compatibility entry point for the dedicated lane reducer."""
    from .lane_transition import transition_lane as apply_lane_transition

    return apply_lane_transition(lane, event)


__all__ = [
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
    "transition_lane",
]
