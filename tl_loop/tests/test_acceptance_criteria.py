"""TL-owned reviewer acceptance criteria coverage."""

from __future__ import annotations

import pytest

from tl_loop.loop.driver import WorkPlan
from tl_loop.loop.review import AcceptanceCriteriaError, compose_acceptance_criteria
from tl_loop.rlm.decompose import SliceSpec
from tl_loop.state.schema import SliceState, SliceStatus


def _state(*, test_plan: tuple[str, ...], paths: tuple[str, ...]) -> SliceState:
    return SliceState(
        id="slice-a",
        status=SliceStatus.IN_REVIEW,
        paths=paths,
        depends_on=(),
        base_ref="main",
        test_plan=test_plan,
        agent_type="codex",
        model="test-model",
        branch="main.slice-a",
        worktree=None,
        pr_number=42,
        reviewed_head=None,
        attempts=0,
        verdict=None,
    )


def test_composer_uses_only_run_state_and_plan_fields() -> None:
    plan = SliceSpec(
        id="slice-a",
        title="Implement the slice",
        paths=("src/a.py",),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        steps=("Implement the feature",),
        verify=("just tl-loop-lint",),
        boundary=("Do not edit generated files",),
        done_criteria=("The reviewer contract is TL-owned",),
    )

    criteria = compose_acceptance_criteria(
        _state(test_plan=("just tl-loop-test",), paths=("src/a.py",)),
        plan,
    )

    assert criteria == (
        "Run-state test plan: just tl-loop-test",
        "Plan verification: just tl-loop-lint",
        "Owned paths: src/a.py",
        "Plan boundary: Do not edit generated files",
        "DONE CRITERIA: The reviewer contract is TL-owned",
    )


def test_composer_accepts_mapping_plans_and_rejects_empty_contract() -> None:
    state = _state(test_plan=("just test",), paths=("src/**",))
    criteria = compose_acceptance_criteria(
        state,
        {
            "verify": ["just lint"],
            "boundary": ["Keep edits in the owned paths"],
            "done_criteria": ["The change is complete"],
        },
    )

    assert "Plan verification: just lint" in criteria
    assert "DONE CRITERIA: The change is complete" in criteria

    empty = _state(test_plan=(), paths=())
    with pytest.raises(AcceptanceCriteriaError, match="no reviewer acceptance criteria"):
        compose_acceptance_criteria(empty, {})


def test_leaf_plan_retains_done_criteria() -> None:
    plan = WorkPlan.from_mapping(
        {
            "leaves": [
                {
                    "name": "leaf-a",
                    "task": "implement the change",
                    "done_criteria": ["The change is complete"],
                }
            ]
        }
    )

    assert plan.leaves[0].done_criteria == ("The change is complete",)
