"""Independent transition matrix for durable slice lifecycle evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
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
    MergeCompleted,
    RedispatchRequested,
    RepairQueued,
    ReviewDiscarded,
    ReviewRoundsExhausted,
    ReviewValidated,
    ReviewValidationFailed,
    ReviewVerdictObserved,
    ReviewerDispatched,
    SliceStatusChanged,
    slice_transition,
)


def _state(status: SliceStatus = SliceStatus.IN_REVIEW) -> SliceState:
    return SliceState(
        id="slice-a",
        status=status,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="test-model",
        branch="main.slice-a",
        worktree=".worktrees/slice-a",
        pr_number=43,
        reviewed_head="head-a",
        attempts=1,
        verdict=Verdict.GO,
        reviewer_attempt={"head-a": 1},
        reviewer_agent_id="reviewer",
        action=ActionState(
            ActionKind.REVIEWER_SPAWN,
            ActionPhase.CONFIRMED,
            intent_id="review-intent",
            head_sha="head-a",
            attempt=1,
        ),
    )


def _verdict() -> ReviewVerdictObserved:
    return ReviewVerdictObserved(
        head_sha="head-a",
        verdict=Verdict.GO,
        reviewer_agent_id="reviewer",
        review_id=17,
        findings=(),
        source="ledger",  # type: ignore[arg-type]
        pr_number=43,
        reviewer_account_authenticated=True,
        reviewer_identity_unresolved=False,
        self_approval=False,
        requires_authenticated_evidence=True,
        submitted_at="2026-09-01T00:00:00Z",
        validated_at="2026-09-01T00:00:00Z",
        observed_at="2026-09-01T00:00:00Z",
    )


@pytest.mark.parametrize("status", tuple(SliceStatus))
def test_slice_status_matrix_has_the_declared_successor(status: SliceStatus) -> None:
    before = _state()
    transitioned = slice_transition(before, SliceStatusChanged(status))
    assert transitioned == replace(before, status=status)


def test_slice_dispatch_matrix_persists_exact_intent_and_attempt() -> None:
    transitioned = slice_transition(
        _state(),
        ReviewerDispatched("new-review-intent", "head-b", 2, "contract"),
    )

    assert transitioned.action == ActionState(
        ActionKind.REVIEWER_SPAWN,
        ActionPhase.IN_FLIGHT,
        intent_id="new-review-intent",
        head_sha="head-b",
        attempt=2,
        contract_digest="contract",
    )


def test_slice_review_matrix_persists_evidence_and_clears_matching_action() -> None:
    transitioned = slice_transition(_state(), _verdict())

    assert transitioned.reviewed_head == "head-a"
    assert transitioned.verdict is Verdict.GO
    assert transitioned.review_rounds == 1
    assert transitioned.review_evidence is not None
    assert transitioned.review_evidence.review_id == 17
    assert transitioned.action is None


def test_slice_head_and_ci_matrix_invalidates_exact_head_evidence() -> None:
    failed = slice_transition(
        _state(),
        CIStatusObserved("head-a", "failure", "2026-09-01T00:01:00Z"),
    )
    assert failed.verdict is Verdict.NO_GO
    assert failed.ci_state["head-a"] == "failure"
    assert failed.review_evidence is None

    changed = slice_transition(failed, HeadChanged("head-b"))
    assert changed.reviewed_head == "head-b"
    assert changed.verdict is None
    assert changed.action is None
    assert changed.ci_state == {}
    assert changed.review_findings == {}


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (RepairQueued(), SliceStatus.REPAIRING),
        (ReviewRoundsExhausted(), SliceStatus.PARKED),
        (MergeCompleted(43), SliceStatus.MERGED),
        (RedispatchRequested(), SliceStatus.PENDING),
    ],
)
def test_slice_terminal_and_repair_edges_have_exact_successors(
    event: object, status: SliceStatus
) -> None:
    transitioned = slice_transition(_state(), event)  # type: ignore[arg-type]

    assert transitioned.status is status
    assert transitioned.action is None


def test_slice_validation_matrix_requires_authenticated_exact_head_evidence() -> None:
    with pytest.raises(IllegalSliceTransition):
        slice_transition(
            _state(),
            replace(_verdict(), reviewer_account_authenticated=False),
        )

    reviewed = slice_transition(_state(), _verdict())
    requested = replace(reviewed, review_validation_required=True)
    observation = ReviewValidationObservation(
        review_id=17,
        pr_number=43,
        head_sha="head-a",
        reviewer_agent_id="reviewer",
        verdict=Verdict.GO,
        observed_at="2026-09-02T00:00:00Z",
        submitted_at="2026-09-01T00:00:00Z",
    )
    validated = slice_transition(requested, ReviewValidated(observation))
    assert validated.review_validation_required is False
    assert validated.review_evidence is not None
    assert validated.review_evidence.validated_at == "2026-09-02T00:00:00Z"

    failed = slice_transition(
        requested,
        ReviewValidationFailed(
            ReviewValidationDisposition.UNAUTHORIZED,
            "reviewer identity is not authorized",
        ),
    )
    assert failed.review_validation_required is True
    assert failed.review_validation_disposition is ReviewValidationDisposition.UNAUTHORIZED
    assert failed.review_validation_failure_reason == "reviewer identity is not authorized"
    assert failed.action is None


def test_slice_discard_is_exactly_replayable_without_cross_head_residue() -> None:
    discarded = slice_transition(_state(), ReviewDiscarded())

    assert discarded.status is SliceStatus.IN_REVIEW
    assert discarded.reviewed_head is None
    assert discarded.verdict is None
    assert discarded.review_evidence is None
    assert discarded.review_validation_required is False
    assert discarded.action is None
