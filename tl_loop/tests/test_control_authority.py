"""Control-plane authority and stale-observation regression coverage."""

from __future__ import annotations

from tl_loop.loop.review import ReviewHeadMismatch, verify_review
from tl_loop.plan_validation import validate_plan_proposal
from tl_loop.state.schema import SliceState, SliceStatus, Verdict


def _reviewed_slice() -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.IN_REVIEW,
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just test",),
        agent_type="codex",
        model="gpt-5",
        branch="task/a",
        worktree=".worktrees/a",
        pr_number=12,
        reviewed_head="head-old",
        attempts=1,
        verdict=Verdict.GO,
        verdict_at=None,
        ci_state={"head-old": "success"},
    )


def test_inert_proposal_contains_no_workflow_authority() -> None:
    proposal = validate_plan_proposal({"plan": {"leaves": []}})

    assert proposal == {"plan": {"leaves": []}}
    assert "gates" not in proposal
    assert "budgets" not in proposal
    assert "verdict" not in proposal


def test_stale_read_cannot_authorize_a_moved_head() -> None:
    try:
        verify_review(_reviewed_slice(), "head-new")
    except ReviewHeadMismatch as error:
        assert "current head" in str(error)
    else:
        raise AssertionError("a verdict for head-old must not authorize head-new")
