from dataclasses import replace

import pytest

from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    HandoffEvidence,
    ReviewValidationDisposition,
    ReviewValidationObservation,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.slice_transition import (
    CIStatusObserved,
    HeadChanged,
    IllegalSliceTransition,
    RevalidateReview,
    ReviewValidated,
    ReviewValidationFailed,
    ReviewVerdictObserved,
    slice_transition,
)


def _state(*, reviewed_head: str | None = None) -> SliceState:
    head = "head-a"
    return SliceState(
        id="slice-a",
        status=SliceStatus.IN_REVIEW,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="test-model",
        branch="main.slice-a",
        worktree=".worktrees/slice-a",
        pr_number=43,
        reviewed_head=reviewed_head,
        attempts=1,
        verdict=None,
        reviewer_attempt={head: 1},
        reviewer_agent_id="review-invocation",
        dispatch_agent_id="worker-invocation",
        handoff=HandoffEvidence(
            pr_number=43,
            head_sha=head,
            attempt=1,
            invocation_id="worker-invocation",
            agent_id="worker-agent",
            observed_at="2026-08-27T00:00:00Z",
        ),
        action=ActionState(
            ActionKind.REVIEWER_SPAWN,
            ActionPhase.CONFIRMED,
            intent_id="review-intent",
            head_sha=head,
            attempt=1,
        ),
    )


def _verdict_event(source: str = "ledger") -> ReviewVerdictObserved:
    return ReviewVerdictObserved(
        head_sha="head-a",
        verdict=Verdict.GO,
        reviewer_agent_id="review-invocation",
        review_id=17,
        findings=(),
        source=source,  # type: ignore[arg-type]
        pr_number=43,
        reviewer_account_authenticated=True,
        reviewer_identity_unresolved=False,
        self_approval=False,
        submitted_at="2026-08-27T00:00:00Z",
        validated_at="2026-08-27T00:00:00Z",
        observed_at="2026-08-27T00:00:00Z",
    )


def test_live_and_snapshot_verdicts_share_one_transition() -> None:
    live = slice_transition(_state(), _verdict_event())
    replay = slice_transition(_state(), _verdict_event("watcher_snapshot"))

    assert live == replay
    assert live.reviewed_head == "head-a"
    assert live.verdict is Verdict.GO
    assert live.review_rounds == 1
    assert live.action is None


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewer_account_authenticated": False},
        {"reviewer_identity_unresolved": True},
        {"self_approval": True},
        {"review_id": None},
        {"reviewer_agent_id": "other-reviewer"},
    ],
)
def test_authenticated_exact_head_guards_live_in_reducer(changes: dict[str, object]) -> None:
    with pytest.raises(IllegalSliceTransition):
        slice_transition(_state(), replace(_verdict_event(), **changes))


def test_stale_verdict_cannot_clear_current_review_action() -> None:
    with pytest.raises(IllegalSliceTransition):
        slice_transition(_state(reviewed_head="old-head"), _verdict_event())


def test_verdict_for_another_head_does_not_clear_outstanding_reviewer() -> None:
    state = _state()
    state = replace(
        state,
        action=replace(state.action, head_sha="other-head"),
    )
    transitioned = slice_transition(state, _verdict_event())
    assert transitioned.action is not None
    assert transitioned.action.head_sha == "other-head"


def test_ci_failure_and_new_head_invalidation_are_reducer_events() -> None:
    reviewed = slice_transition(_state(), _verdict_event())
    failed = slice_transition(
        reviewed,
        CIStatusObserved(head_sha="head-a", status="failure", observed_at="now"),
    )
    assert failed.ci_state["head-a"] == "failure"
    assert failed.verdict is Verdict.NO_GO

    changed = slice_transition(failed, HeadChanged("head-b"))
    assert changed.reviewed_head == "head-b"
    assert changed.verdict is None
    assert changed.ci_state == {}
    assert changed.action is None


def test_revalidation_refresh_preserves_submission_and_review_round() -> None:
    reviewed = slice_transition(_state(), _verdict_event())
    assert reviewed.review_evidence is not None
    stale = replace(
        reviewed,
        review_evidence=replace(
            reviewed.review_evidence,
            validated_at="2026-08-27T00:00:00Z",
        ),
        review_validation_required=True,
    )

    requested = slice_transition(stale, RevalidateReview())
    refreshed = slice_transition(
        requested,
        ReviewValidated(
            ReviewValidationObservation(
                review_id=17,
                pr_number=43,
                head_sha="head-a",
                reviewer_agent_id="review-invocation",
                verdict=Verdict.GO,
                observed_at="2026-08-29T00:00:00Z",
                submitted_at="2026-08-27T00:00:00Z",
            )
        ),
    )

    assert refreshed.review_evidence is not None
    assert refreshed.review_evidence.submitted_at == "2026-08-27T00:00:00Z"
    assert refreshed.review_evidence.validated_at == "2026-08-29T00:00:00Z"
    assert refreshed.review_validation_required is False
    assert refreshed.verdict is Verdict.GO
    assert refreshed.review_rounds == reviewed.review_rounds
    assert refreshed.handoff == reviewed.handoff


def test_revalidation_does_not_change_state_for_missing_verdict() -> None:
    state = _state()
    assert slice_transition(state, RevalidateReview()) == state


def test_newer_same_head_review_uses_a_new_review_round() -> None:
    reviewed = slice_transition(_state(), _verdict_event())
    superseding = slice_transition(
        reviewed,
        replace(
            _verdict_event(),
            review_id=18,
            verdict=Verdict.NO_GO,
            findings=({"severity": "blocking", "path": "src/a.py", "rationale": "fix it"},),
        ),
    )

    assert superseding.verdict is Verdict.NO_GO
    assert superseding.review_rounds == reviewed.review_rounds + 1
    assert superseding.review_evidence is not None
    assert superseding.review_evidence.review_id == 18


def test_failed_revalidation_is_durable_without_erasing_review_identity() -> None:
    reviewed = slice_transition(_state(), _verdict_event())
    failed = slice_transition(
        reviewed,
        ReviewValidationFailed(
            disposition=ReviewValidationDisposition.UNAUTHORIZED,
            reason="review_validation_unauthorized",
        ),
    )

    assert failed.review_validation_required is True
    assert failed.review_validation_disposition is ReviewValidationDisposition.UNAUTHORIZED
    assert failed.stall_classification == reviewed.stall_classification
    assert failed.review_validation_failure_reason == "review_validation_unauthorized"
    assert failed.verdict is Verdict.GO
    assert failed.review_evidence == reviewed.review_evidence
    assert failed.action is None
