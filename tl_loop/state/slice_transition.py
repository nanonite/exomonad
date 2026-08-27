"""Pure transitions for the detailed lifecycle of one controller slice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    SliceState,
    SliceStatus,
    Verdict,
)


@dataclass(frozen=True)
class SliceEvent:
    """Base type for all concrete slice lifecycle events."""


@dataclass(frozen=True)
class SliceStatusChanged(SliceEvent):
    """The controller observed a new coarse status for this slice."""

    status: SliceStatus


@dataclass(frozen=True)
class ActionChanged(SliceEvent):
    """The durable action phase was observed or explicitly cleared."""

    action: ActionState | None


@dataclass(frozen=True)
class ReviewerDispatched(SliceEvent):
    """A reviewer spawn intent entered flight."""

    intent_id: str
    head_sha: str
    attempt: int
    contract_digest: str | None = None


@dataclass(frozen=True)
class ReviewVerdictObserved(SliceEvent):
    """An authorized exact-head verdict arrived from live or replayed evidence."""

    head_sha: str
    verdict: Verdict
    reviewer_agent_id: str | None
    review_id: int | None
    findings: tuple[dict[str, str], ...]
    source: Literal["ledger", "watcher_snapshot"]
    pr_number: int | None = None
    reviewer_account_authenticated: bool = True
    reviewer_identity_unresolved: bool = False
    self_approval: bool = False
    stall_classification: str | None = None
    requires_authenticated_evidence: bool = True
    increment_round: bool = True
    next_status: SliceStatus | None = None


@dataclass(frozen=True)
class CIStatusObserved(SliceEvent):
    """CI reported a status for one exact head."""

    head_sha: str
    status: str
    observed_at: str | None = None


@dataclass(frozen=True)
class HeadEvidenceObserved(SliceEvent):
    """A watcher snapshot supplied head/CI evidence without a verdict."""

    head_sha: str
    ci_status: str | None = None
    review_findings: tuple[dict[str, str], ...] = ()
    bind_reviewed_head: bool = True


@dataclass(frozen=True)
class HeadChanged(SliceEvent):
    """A publication head invalidated the prior review decision."""

    head_sha: str | None


@dataclass(frozen=True)
class ReviewDiscarded(SliceEvent):
    """Review evidence was discarded and the slice returned to review."""


@dataclass(frozen=True)
class RepairQueued(SliceEvent):
    """A negative review entered repair without retaining a stale action."""


@dataclass(frozen=True)
class ReviewRoundsExhausted(SliceEvent):
    """The configured review budget was exhausted."""


@dataclass(frozen=True)
class RedispatchRequested(SliceEvent):
    """Reset review-owned evidence before a deliberate redispatch."""


@dataclass(frozen=True)
class MergeCompleted(SliceEvent):
    """The authoritative PR snapshot confirmed a merge."""

    pr_number: int


@dataclass(frozen=True)
class ReviewerIdentityObserved(SliceEvent):
    """Persist the durable reviewer invocation selected for a spawn."""

    reviewer_agent_id: str | None


@dataclass(frozen=True)
class StallClassificationObserved(SliceEvent):
    """Persist the latest durable controller stall classification."""

    classification: str | None


class IllegalSliceTransition(Exception):
    """Raised when an event has no legal transition for the current slice."""

    def __init__(self, state: SliceState, event: SliceEvent) -> None:
        self.state = state
        self.event = event
        super().__init__(
            f"No slice transition for {type(state.status).__name__} "
            f"and {type(event).__name__}"
        )


def slice_transition(state: SliceState, event: SliceEvent) -> SliceState:
    """Apply one pure SliceState transition or raise ``IllegalSliceTransition``."""
    if isinstance(event, SliceStatusChanged):
        return replace(state, status=event.status)
    if isinstance(event, ActionChanged):
        return replace(state, action=event.action)
    if isinstance(event, ReviewerDispatched):
        action = ActionState(
            ActionKind.REVIEWER_SPAWN,
            ActionPhase.IN_FLIGHT,
            intent_id=event.intent_id,
            head_sha=event.head_sha,
            attempt=event.attempt,
            contract_digest=event.contract_digest,
        )
        return replace(state, action=action)
    if isinstance(event, ReviewerIdentityObserved):
        return replace(state, reviewer_agent_id=event.reviewer_agent_id)
    if isinstance(event, StallClassificationObserved):
        return replace(state, stall_classification=event.classification)
    if isinstance(event, ReviewVerdictObserved):
        _validate_review_event(state, event)
        findings = dict(state.review_findings)
        findings[event.head_sha] = event.findings
        ci_state = (
            {}
            if state.reviewed_head != event.head_sha
            else dict(state.ci_state)
        )
        return replace(
            state,
            reviewed_head=event.head_sha,
            verdict=event.verdict,
            review_rounds=state.review_rounds + (1 if event.increment_round else 0),
            review_findings=findings,
            ci_state=ci_state,
            reviewer_agent_id=event.reviewer_agent_id,
            stall_classification=event.stall_classification,
            status=event.next_status or state.status,
            action=(
                None
                if _reviewer_action_matches(state.action, event.head_sha)
                else state.action
            ),
        )
    if isinstance(event, CIStatusObserved):
        ci_state = dict(state.ci_state)
        ci_state[event.head_sha] = event.status
        verdict = state.verdict
        verdict_at = state.verdict_at
        if event.status == "failure" and state.reviewed_head == event.head_sha:
            verdict = Verdict.NO_GO
            verdict_at = event.observed_at
        return replace(state, ci_state=ci_state, verdict=verdict, verdict_at=verdict_at)
    if isinstance(event, HeadEvidenceObserved):
        ci_state = dict(state.ci_state)
        if event.ci_status:
            ci_state[event.head_sha] = event.ci_status
        findings = dict(state.review_findings)
        if event.review_findings:
            findings[event.head_sha] = event.review_findings
        reviewed_head = event.head_sha if event.bind_reviewed_head else state.reviewed_head
        return replace(
            state,
            reviewed_head=reviewed_head,
            review_findings=findings,
            ci_state=ci_state,
        )
    if isinstance(event, HeadChanged):
        return replace(
            state,
            reviewed_head=event.head_sha,
            verdict=None,
            verdict_at=None,
            action=None,
            review_findings={},
            review_patch_digests={},
            ci_state={},
            reviewer_attempt={},
            stall_classification=None,
        )
    if isinstance(event, ReviewDiscarded):
        return replace(
            state,
            status=SliceStatus.IN_REVIEW,
            reviewed_head=None,
            verdict=None,
            verdict_at=None,
            action=None,
            stall_classification=None,
        )
    if isinstance(event, RepairQueued):
        return replace(
            state,
            status=SliceStatus.REPAIRING,
            verdict=Verdict.NO_GO,
            action=None,
        )
    if isinstance(event, ReviewRoundsExhausted):
        return replace(state, status=SliceStatus.PARKED, action=None)
    if isinstance(event, RedispatchRequested):
        return replace(
            state,
            status=SliceStatus.PENDING,
            pr_number=None,
            reviewed_head=None,
            verdict=None,
            verdict_at=None,
            review_findings={},
            review_patch_digests={},
            ci_state={},
            reviewer_attempt={},
            action=None,
            stall_classification=None,
        )
    if isinstance(event, MergeCompleted):
        return replace(state, status=SliceStatus.MERGED, action=None)
    raise IllegalSliceTransition(state, event)


def _validate_review_event(state: SliceState, event: ReviewVerdictObserved) -> None:
    if event.source not in {"ledger", "watcher_snapshot"}:
        raise IllegalSliceTransition(state, event)
    if (
        event.requires_authenticated_evidence
        and state.handoff is not None
        and state.handoff.head_sha != event.head_sha
    ):
        raise IllegalSliceTransition(state, event)
    if state.reviewed_head is not None and state.reviewed_head != event.head_sha:
        raise IllegalSliceTransition(state, event)
    if (
        event.requires_authenticated_evidence
        and state.handoff is not None
        and state.handoff.pr_number != event.pr_number
        and event.pr_number is not None
    ):
        raise IllegalSliceTransition(state, event)
    if event.requires_authenticated_evidence:
        if state.reviewer_agent_id is not None:
            if event.reviewer_agent_id != state.reviewer_agent_id:
                raise IllegalSliceTransition(state, event)
        elif (
            event.reviewer_agent_id is None
            or state.reviewer_attempt.get(event.head_sha, 0) <= 0
            or event.reviewer_agent_id == state.dispatch_agent_id
        ):
            raise IllegalSliceTransition(state, event)
    if event.requires_authenticated_evidence and (
        type(event.review_id) is not int
        or event.review_id <= 0
        or not event.reviewer_agent_id
        or not event.reviewer_account_authenticated
        or event.reviewer_identity_unresolved
        or event.self_approval
    ):
        raise IllegalSliceTransition(state, event)


def _reviewer_action_matches(action: ActionState | None, head_sha: str) -> bool:
    return (
        action is not None
        and action.kind is ActionKind.REVIEWER_SPAWN
        and action.head_sha == head_sha
    )
