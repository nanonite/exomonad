"""Pure transitions for the detailed lifecycle of one controller slice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from tl_loop.fsm.post_merge import PostMergeState
from tl_loop.fsm.post_merge_events import (
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
from tl_loop.fsm.post_merge_transition import advance_post_merge
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    DurableReviewEvidence,
    ReviewValidationDisposition,
    ReviewValidationObservation,
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
    submitted_at: str | None = None
    validated_at: str | None = None
    observed_at: str | None = None
    dismissed: bool = False
    forgejo_stale: bool = False


@dataclass(frozen=True)
class RevalidateReview(SliceEvent):
    """Mark an existing verdict as awaiting authoritative revalidation."""


@dataclass(frozen=True)
class ReviewValidated(SliceEvent):
    """Persist a successful validation without creating a new review round."""

    observation: ReviewValidationObservation
    submitted_at: str | None = None


@dataclass(frozen=True)
class ReviewValidationFailed(SliceEvent):
    """Persist invalidation before the reducer is allowed to derive again."""

    disposition: ReviewValidationDisposition
    reason: str


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
class PostMergeEventObserved(SliceEvent):
    """Apply one typed durable post-merge event to this slice."""

    event: (
        MergeAdopted
        | ParentBranchSynced
        | IssueClosePending
        | IssueCloseConfirmed
        | ChangelogPending
        | ChangelogCommitted
        | ParentPushPending
        | PostMergeComplete
        | PostMergeRebuildRequested
    )


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
            f"No slice transition for {type(state.status).__name__} and {type(event).__name__}"
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
        ci_state = {} if state.reviewed_head != event.head_sha else dict(state.ci_state)
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
            verdict_at=event.submitted_at or event.observed_at or state.verdict_at,
            review_evidence=_review_evidence_from_event(event),
            review_validation_required=(
                event.requires_authenticated_evidence and _review_evidence_from_event(event) is None
            ),
            review_validation_disposition=None,
            review_validation_failure_reason=None,
            action=(
                None if _reviewer_action_matches(state.action, event.head_sha) else state.action
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
        return replace(
            state,
            ci_state=ci_state,
            verdict=verdict,
            verdict_at=verdict_at,
            review_evidence=None if event.status == "failure" else state.review_evidence,
            review_validation_required=False
            if event.status == "failure"
            else state.review_validation_required,
            review_validation_disposition=None
            if event.status == "failure"
            else state.review_validation_disposition,
            review_validation_failure_reason=None
            if event.status == "failure"
            else state.review_validation_failure_reason,
        )
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
            review_evidence=None,
            review_validation_required=False,
            review_validation_disposition=None,
            review_validation_failure_reason=None,
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
            review_evidence=None,
            review_validation_required=False,
            review_validation_disposition=None,
            review_validation_failure_reason=None,
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
            review_evidence=None,
            review_validation_required=False,
            review_validation_disposition=None,
            review_validation_failure_reason=None,
            review_findings={},
            review_patch_digests={},
            ci_state={},
            reviewer_attempt={},
            action=None,
            stall_classification=None,
        )
    if isinstance(event, MergeCompleted):
        return replace(state, status=SliceStatus.MERGED, action=None)
    if isinstance(event, PostMergeEventObserved):
        current = state.post_merge
        if current is None:
            if not isinstance(event.event, MergeAdopted):
                raise IllegalSliceTransition(state, event)
            current = PostMergeState()
        try:
            post_merge = advance_post_merge(current, event.event)
        except (TypeError, ValueError) as error:
            raise IllegalSliceTransition(state, event) from error
        return replace(state, post_merge=post_merge)
    if isinstance(event, RevalidateReview):
        if state.verdict is None or state.reviewed_head is None:
            return state
        return replace(
            state,
            review_validation_required=True,
            action=(
                None
                if _reviewer_action_matches(state.action, state.reviewed_head)
                or (
                    state.action is not None
                    and state.action.kind is ActionKind.MERGE
                    and state.action.head_sha == state.reviewed_head
                )
                else state.action
            ),
        )
    if isinstance(event, ReviewValidated):
        _validate_review_validation_event(state, event)
        current = state.review_evidence
        submitted_at = (
            current.submitted_at
            if current is not None
            else event.submitted_at
            or event.observation.submitted_at
            or event.observation.observed_at
        )
        evidence = DurableReviewEvidence(
            review_id=event.observation.review_id,
            pr_number=event.observation.pr_number,
            head_sha=event.observation.head_sha,
            reviewer_agent_id=event.observation.reviewer_agent_id,
            verdict=event.observation.verdict,
            submitted_at=submitted_at,
            validated_at=event.observation.observed_at,
            reviewer_account_authenticated=event.observation.reviewer_account_authenticated,
            dismissed=event.observation.dismissed,
            forgejo_stale=event.observation.forgejo_stale,
            reviewer_identity_unresolved=event.observation.reviewer_identity_unresolved,
        )
        return replace(
            state,
            review_evidence=evidence,
            review_validation_required=False,
            review_validation_disposition=None,
            review_validation_failure_reason=None,
            reviewer_agent_id=event.observation.reviewer_agent_id,
        )
    if isinstance(event, ReviewValidationFailed):
        return replace(
            state,
            status=SliceStatus.IN_REVIEW,
            review_validation_required=True,
            review_validation_disposition=event.disposition,
            review_validation_failure_reason=event.reason,
            action=None,
        )
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
        or event.dismissed
        or event.forgejo_stale
    ):
        raise IllegalSliceTransition(state, event)


def _review_evidence_from_event(
    event: ReviewVerdictObserved,
) -> DurableReviewEvidence | None:
    if (
        type(event.review_id) is not int
        or event.review_id <= 0
        or event.pr_number is None
        or event.pr_number <= 0
        or not event.reviewer_agent_id
        or not event.reviewer_account_authenticated
        or event.reviewer_identity_unresolved
        or event.dismissed
        or event.forgejo_stale
    ):
        return None
    observed_at = event.observed_at or event.validated_at or event.submitted_at
    submitted_at = event.submitted_at or observed_at
    if not observed_at or not submitted_at:
        return None
    return DurableReviewEvidence(
        review_id=event.review_id,
        pr_number=event.pr_number,
        head_sha=event.head_sha,
        reviewer_agent_id=event.reviewer_agent_id,
        verdict=event.verdict,
        submitted_at=submitted_at,
        validated_at=event.validated_at or observed_at,
        reviewer_account_authenticated=event.reviewer_account_authenticated,
        dismissed=event.dismissed,
        forgejo_stale=event.forgejo_stale,
        reviewer_identity_unresolved=event.reviewer_identity_unresolved,
    )


def _validate_review_validation_event(
    state: SliceState,
    event: ReviewValidated,
) -> None:
    observation = event.observation
    if (
        state.verdict != observation.verdict
        or state.reviewed_head != observation.head_sha
        or state.pr_number != observation.pr_number
        or not observation.reviewer_account_authenticated
        or observation.reviewer_identity_unresolved
        or observation.dismissed
        or observation.forgejo_stale
    ):
        raise IllegalSliceTransition(state, event)
    if state.handoff is not None and (
        state.handoff.pr_number != observation.pr_number
        or state.handoff.head_sha != observation.head_sha
    ):
        raise IllegalSliceTransition(state, event)
    if (
        state.review_evidence is not None
        and state.review_evidence.identity() != observation.identity()
    ):
        raise IllegalSliceTransition(state, event)


def _reviewer_action_matches(action: ActionState | None, head_sha: str) -> bool:
    return (
        action is not None
        and action.kind is ActionKind.REVIEWER_SPAWN
        and action.head_sha == head_sha
    )
