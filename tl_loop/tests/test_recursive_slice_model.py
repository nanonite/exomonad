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
from tl_loop.fsm.post_merge_events import MergeAdopted
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
    ActionChanged,
    HeadEvidenceObserved,
    PostMergeEventObserved,
    RevalidateReview,
    ReviewerIdentityObserved,
    StallClassificationObserved,
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


def _validation_observation() -> ReviewValidationObservation:
    return ReviewValidationObservation(
        review_id=17,
        pr_number=43,
        head_sha="head-a",
        reviewer_agent_id="reviewer",
        verdict=Verdict.GO,
        observed_at="2026-09-02T00:00:00Z",
        submitted_at="2026-09-01T00:00:00Z",
    )


def _merge_adopted() -> MergeAdopted:
    return MergeAdopted(
        child_id="slice-a",
        pr_number=43,
        head_sha="head-a",
        journal_id="merge-journal",
        repository="repo",
        parent_branch="main",
    )


def _slice_event_matrix() -> tuple[tuple[str, object], ...]:
    return (
        ("status", SliceStatusChanged(SliceStatus.READY)),
        ("action", ActionChanged(None)),
        ("dispatch", ReviewerDispatched("new-intent", "head-b", 2, "contract")),
        ("verdict", _verdict()),
        ("revalidate", RevalidateReview()),
        ("validated", ReviewValidated(_validation_observation())),
        (
            "validation_failed",
            ReviewValidationFailed(
                ReviewValidationDisposition.UNAUTHORIZED,
                "identity is not authorized",
            ),
        ),
        ("ci", CIStatusObserved("head-a", "failure", "2026-09-01T00:01:00Z")),
        ("head_evidence", HeadEvidenceObserved("head-b", "success")),
        ("head_changed", HeadChanged("head-b")),
        ("discard", ReviewDiscarded()),
        ("repair", RepairQueued()),
        ("rounds_exhausted", ReviewRoundsExhausted()),
        ("redispatch", RedispatchRequested()),
        ("merge_completed", MergeCompleted(43)),
        ("post_merge", PostMergeEventObserved(_merge_adopted())),
        ("reviewer_identity", ReviewerIdentityObserved("historical-reviewer")),
        ("stall", StallClassificationObserved("review_stuck")),
    )


@pytest.mark.parametrize("status", tuple(SliceStatus))
def test_every_slice_event_has_an_oracle_successor_for_every_status(
    status: SliceStatus,
) -> None:
    before = _state(status)

    for name, event in _slice_event_matrix():
        after = slice_transition(before, event)  # type: ignore[arg-type]
        assert after.id == before.id
        if name == "status":
            assert after.status is SliceStatus.READY
        elif name == "action":
            assert after.action is None
        elif name == "dispatch":
            assert after.action == ActionState(
                ActionKind.REVIEWER_SPAWN,
                ActionPhase.IN_FLIGHT,
                intent_id="new-intent",
                head_sha="head-b",
                attempt=2,
                contract_digest="contract",
            )
        elif name == "verdict":
            assert after.reviewed_head == "head-a"
            assert after.verdict is Verdict.GO
            assert after.review_evidence is not None
            assert after.review_evidence.review_id == 17
            assert after.action is None
        elif name == "revalidate":
            assert after.review_validation_required is True
        elif name == "validated":
            assert after.review_validation_required is False
            assert after.review_evidence is not None
            assert after.review_evidence.validated_at == "2026-09-02T00:00:00Z"
        elif name == "validation_failed":
            assert after.status is SliceStatus.IN_REVIEW
            assert after.review_validation_failure_reason == "identity is not authorized"
            assert after.action is None
        elif name == "ci":
            assert after.verdict is Verdict.NO_GO
            assert after.ci_state["head-a"] == "failure"
        elif name == "head_evidence":
            assert after.reviewed_head == "head-b"
            assert after.ci_state["head-b"] == "success"
        elif name == "head_changed":
            assert after.reviewed_head == "head-b"
            assert after.verdict is None
            assert after.action is None
        elif name == "discard":
            assert after.status is SliceStatus.IN_REVIEW
            assert after.reviewed_head is None
            assert after.verdict is None
        elif name == "repair":
            assert after.status is SliceStatus.REPAIRING
        elif name == "rounds_exhausted":
            assert after.status is SliceStatus.PARKED
        elif name == "redispatch":
            assert after.status is SliceStatus.PENDING
            assert after.reviewed_head is None
        elif name == "merge_completed":
            assert after.status is SliceStatus.MERGED
        elif name == "post_merge":
            assert after.post_merge is not None
            assert after.post_merge.phase.value == "remote_merge_adopted"
        elif name == "reviewer_identity":
            assert after.reviewer_agent_id == "historical-reviewer"
        elif name == "stall":
            assert after.stall_classification == "review_stuck"


@pytest.mark.parametrize("status", tuple(SliceStatus))
@pytest.mark.parametrize(
    "changes",
    [
        {"reviewer_account_authenticated": False},
        {"reviewer_identity_unresolved": True},
        {"self_approval": True},
        {"dismissed": True},
        {"forgejo_stale": True},
        {"review_id": None},
        {"reviewer_agent_id": "other-reviewer"},
    ],
)
def test_slice_review_matrix_rejects_invalid_evidence_in_every_status(
    status: SliceStatus, changes: dict[str, object]
) -> None:
    with pytest.raises(IllegalSliceTransition):
        slice_transition(_state(status), replace(_verdict(), **changes))


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
