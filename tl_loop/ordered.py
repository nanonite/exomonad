"""Contracts for ordered recursive sub-TL execution and integration.

The contract is deliberately independent from scheduling.  A stage describes
which sibling sub-TLs share an order; an integration record describes the
evidence needed before their result can be folded into the parent.  Order is
relative to one parent's direct children and therefore resets at recursion.

Existing plans without ``order`` retain the legacy single-stage behavior by
normalizing their sub-TLs to order ``1``.  The compatibility rule is limited to
the missing field: an explicit zero, negative order, unknown contract key, or
ambiguous evidence is rejected rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Final

REVIEW_VERDICTS: Final = frozenset({"GO", "GO-WITH-NITS", "NO-GO"})
CI_STATUSES: Final = frozenset({"unknown", "pending", "success", "failure", "neutral"})


class IntegrationLifecycle(str, Enum):
    """Durable lifecycle of a stage result as it moves toward its parent."""

    RUNNING = "RUNNING"
    CHILDREN_MERGED = "CHILDREN_MERGED"
    AGGREGATE_PR_OPEN = "AGGREGATE_PR_OPEN"
    CODE_REVIEWED = "CODE_REVIEWED"
    READY_FOR_INTEGRATION = "READY_FOR_INTEGRATION"
    NEEDS_BASE_REVALIDATION = "NEEDS_BASE_REVALIDATION"
    INTEGRATION_VALIDATED = "INTEGRATION_VALIDATED"
    MERGING = "MERGING"
    MERGED = "MERGED"
    REPAIRING_AGGREGATE = "REPAIRING_AGGREGATE"
    INTEGRATION_CONFLICT = "INTEGRATION_CONFLICT"
    FAILED = "FAILED"
    PARKED = "PARKED"


class IntegrationTransition(str, Enum):
    """Events accepted by the centralized integration transition table."""

    CHILDREN_MERGED = "children_merged"
    AGGREGATE_PR_OPENED = "aggregate_pr_opened"
    CODE_REVIEW_ACCEPTED = "code_review_accepted"
    BASE_INVALIDATED = "base_invalidated"
    HEAD_INVALIDATED = "head_invalidated"
    INTEGRATION_VALIDATED = "integration_validated"
    MERGE_STARTED = "merge_started"
    MERGED = "merged"
    REPAIR_STARTED = "repair_started"
    REPAIR_COMPLETED = "repair_completed"
    INTEGRATION_CONFLICT = "integration_conflict"
    FAILED = "failed"
    PARKED = "parked"


class IntegrationTransitionError(ValueError):
    """An integration event is not legal from the current lifecycle."""


class ReviewOwner(str, Enum):
    """The owner responsible for one review or repair boundary."""

    LEAF = "leaf"
    AGGREGATE = "aggregate"


@dataclass(frozen=True)
class IntegrationContract:
    """Parent-fold rules shared by every sub-TL in one ordered stage."""

    aggregate_pr_required: bool = True
    base_revalidation_required: bool = True
    leaf_review_owner: ReviewOwner = ReviewOwner.LEAF
    aggregate_review_owner: ReviewOwner = ReviewOwner.AGGREGATE
    aggregate_repair_owner: ReviewOwner = ReviewOwner.AGGREGATE
    merge_strategy: str = "merge"

    def __post_init__(self) -> None:
        if not isinstance(self.aggregate_pr_required, bool):
            raise TypeError("aggregate_pr_required must be a boolean")
        if not isinstance(self.base_revalidation_required, bool):
            raise TypeError("base_revalidation_required must be a boolean")
        for name in ("leaf_review_owner", "aggregate_review_owner", "aggregate_repair_owner"):
            if not isinstance(getattr(self, name), ReviewOwner):
                raise TypeError(f"{name} must be a ReviewOwner")
        if self.merge_strategy not in {"merge", "rebase"}:
            raise ValueError("merge_strategy must be 'merge' or 'rebase'")


@dataclass(frozen=True)
class OrderedStage:
    """One sibling stage; order is positive and scoped to one parent."""

    order: int
    sub_tls: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order <= 0:
            raise ValueError("stage order must be a positive integer")
        if not self.sub_tls:
            raise ValueError("ordered stage must contain at least one sub-TL")
        if any(not isinstance(name, str) or not name for name in self.sub_tls):
            raise ValueError("ordered stage sub-TL names must be non-empty strings")
        if len(set(self.sub_tls)) != len(self.sub_tls):
            raise ValueError("ordered stage sub-TL names must be unique")


@dataclass(frozen=True)
class AggregateCandidate:
    """A child result eligible to participate in the parent aggregate PR."""

    sub_tl_id: str
    pr_number: int
    head_sha: str
    patch_digest: str
    original_base_sha: str

    def __post_init__(self) -> None:
        if not self.sub_tl_id:
            raise ValueError("aggregate candidate sub_tl_id must be non-empty")
        if type(self.pr_number) is not int or self.pr_number <= 0:
            raise ValueError("aggregate candidate pr_number must be positive")
        for name in ("head_sha", "patch_digest", "original_base_sha"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"aggregate candidate {name} must be non-empty")


@dataclass(frozen=True)
class CodeReviewEvidence:
    """Review evidence bound to the exact candidate head and patch."""

    head_sha: str
    patch_digest: str
    verdict: str
    observed_at: str

    def __post_init__(self) -> None:
        if not self.head_sha or not self.patch_digest or not self.observed_at:
            raise ValueError("code review evidence fields must be non-empty")
        if self.verdict not in REVIEW_VERDICTS:
            raise ValueError("code review verdict is not recognized")


@dataclass(frozen=True)
class IntegrationEvidence:
    """Integration evidence bound to both the base and resulting head."""

    base_sha: str
    head_sha: str
    merge_tree_sha: str
    ci_status: str
    observed_at: str

    def __post_init__(self) -> None:
        for name in ("base_sha", "head_sha", "merge_tree_sha", "observed_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"integration evidence {name} must be non-empty")
        if self.ci_status not in CI_STATUSES:
            raise ValueError("integration evidence ci_status is not recognized")


@dataclass(frozen=True)
class IntegrationState:
    """Lifecycle plus the evidence that justifies the current stage state."""

    lifecycle: IntegrationLifecycle = IntegrationLifecycle.RUNNING
    candidate: AggregateCandidate | None = None
    code_review: CodeReviewEvidence | None = None
    integration: IntegrationEvidence | None = None


# Every allowed edge is declared once.  Consumers must use
# ``transition_integration`` rather than branching on lifecycle strings.
_TRANSITIONS: Final[
    dict[IntegrationLifecycle, dict[IntegrationTransition, IntegrationLifecycle]]
] = {
    IntegrationLifecycle.RUNNING: {
        IntegrationTransition.CHILDREN_MERGED: IntegrationLifecycle.CHILDREN_MERGED,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.CHILDREN_MERGED: {
        IntegrationTransition.AGGREGATE_PR_OPENED: IntegrationLifecycle.AGGREGATE_PR_OPEN,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.AGGREGATE_PR_OPEN: {
        IntegrationTransition.CODE_REVIEW_ACCEPTED: IntegrationLifecycle.CODE_REVIEWED,
        IntegrationTransition.REPAIR_STARTED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.CODE_REVIEWED: {
        IntegrationTransition.CODE_REVIEW_ACCEPTED: IntegrationLifecycle.READY_FOR_INTEGRATION,
        IntegrationTransition.BASE_INVALIDATED: IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        IntegrationTransition.HEAD_INVALIDATED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.REPAIR_STARTED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.READY_FOR_INTEGRATION: {
        IntegrationTransition.BASE_INVALIDATED: IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        IntegrationTransition.HEAD_INVALIDATED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.REPAIR_STARTED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.INTEGRATION_VALIDATED: IntegrationLifecycle.INTEGRATION_VALIDATED,
        IntegrationTransition.INTEGRATION_CONFLICT: IntegrationLifecycle.INTEGRATION_CONFLICT,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.NEEDS_BASE_REVALIDATION: {
        IntegrationTransition.INTEGRATION_VALIDATED: IntegrationLifecycle.INTEGRATION_VALIDATED,
        IntegrationTransition.BASE_INVALIDATED: IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        IntegrationTransition.INTEGRATION_CONFLICT: IntegrationLifecycle.INTEGRATION_CONFLICT,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.INTEGRATION_VALIDATED: {
        IntegrationTransition.MERGE_STARTED: IntegrationLifecycle.MERGING,
        IntegrationTransition.BASE_INVALIDATED: IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        IntegrationTransition.INTEGRATION_CONFLICT: IntegrationLifecycle.INTEGRATION_CONFLICT,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
    },
    IntegrationLifecycle.MERGING: {
        IntegrationTransition.MERGED: IntegrationLifecycle.MERGED,
        IntegrationTransition.BASE_INVALIDATED: IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        IntegrationTransition.INTEGRATION_CONFLICT: IntegrationLifecycle.INTEGRATION_CONFLICT,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
    },
    IntegrationLifecycle.REPAIRING_AGGREGATE: {
        IntegrationTransition.REPAIR_COMPLETED: IntegrationLifecycle.AGGREGATE_PR_OPEN,
        IntegrationTransition.HEAD_INVALIDATED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.INTEGRATION_CONFLICT: {
        IntegrationTransition.REPAIR_STARTED: IntegrationLifecycle.REPAIRING_AGGREGATE,
        IntegrationTransition.BASE_INVALIDATED: IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        IntegrationTransition.FAILED: IntegrationLifecycle.FAILED,
        IntegrationTransition.PARKED: IntegrationLifecycle.PARKED,
    },
    IntegrationLifecycle.MERGED: {},
    IntegrationLifecycle.FAILED: {},
    IntegrationLifecycle.PARKED: {},
}


def transition_integration(
    state: IntegrationState, event: IntegrationTransition
) -> IntegrationState:
    """Apply one allowed lifecycle edge or reject it centrally."""
    try:
        next_lifecycle = _TRANSITIONS[state.lifecycle][event]
    except KeyError as error:
        raise IntegrationTransitionError(
            f"cannot apply {event.value} from {state.lifecycle.value}"
        ) from error
    return replace(state, lifecycle=next_lifecycle)


def allowed_integration_transitions(
    lifecycle: IntegrationLifecycle,
) -> frozenset[IntegrationTransition]:
    """Return the immutable event set accepted from one lifecycle."""
    return frozenset(_TRANSITIONS[lifecycle])


__all__ = [
    "CI_STATUSES",
    "REVIEW_VERDICTS",
    "AggregateCandidate",
    "CodeReviewEvidence",
    "IntegrationContract",
    "IntegrationEvidence",
    "IntegrationLifecycle",
    "IntegrationState",
    "IntegrationTransition",
    "IntegrationTransitionError",
    "OrderedStage",
    "ReviewOwner",
    "allowed_integration_transitions",
    "transition_integration",
]
