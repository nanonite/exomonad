"""Revision and authority checks for bounded plan clarification."""

from __future__ import annotations

import pytest

from tl_loop.plan_clarification import (
    PlanClarificationError,
    build_clarification,
    clarification_audit,
    invariant_digest,
    validate_clarification,
)
from tl_loop.plan_validation import validate_plan_proposal


def _plan(boundary: str = "src/api.py", task: str = "implement api") -> dict[str, object]:
    return {
        "leaves": [
            {
                "name": "api",
                "task": task,
                "boundary": [boundary],
                "verify": ["git diff --check"],
                "done_criteria": ["tests pass"],
            }
        ]
    }


def test_in_scope_continuation_keeps_digest_and_revision() -> None:
    prior = _plan()
    proposed = _plan(task="continue the existing api implementation")
    clarification = build_clarification(
        prior_revision=4,
        prior_plan=prior,
        proposed_plan=proposed,
        continuation_task="finish the remaining verification",
    )

    assert clarification.changed_fields == ()
    assert clarification.requires_human is False
    validate_clarification(
        clarification,
        current_revision=4,
        current_digest=invariant_digest(prior),
    )
    assert "continuation_task" not in clarification_audit(clarification)


def test_material_scope_change_requires_human_authorization() -> None:
    prior = _plan()
    clarification = build_clarification(
        prior_revision=4,
        prior_plan=prior,
        proposed_plan=_plan("src/other.py"),
        continuation_task="work on the changed path",
    )

    assert "scope" in clarification.changed_fields
    with pytest.raises(PlanClarificationError, match="human authorization"):
        validate_clarification(
            clarification,
            current_revision=4,
            current_digest=invariant_digest(prior),
        )
    validate_clarification(
        clarification,
        current_revision=4,
        current_digest=invariant_digest(prior),
        human_authorized=True,
    )


def test_stale_revision_and_digest_fail_closed() -> None:
    plan = _plan()
    clarification = build_clarification(
        prior_revision=4,
        prior_plan=plan,
        proposed_plan=plan,
        continuation_task="continue",
    )
    with pytest.raises(PlanClarificationError, match="revision is stale"):
        validate_clarification(
            clarification, current_revision=5, current_digest=invariant_digest(plan)
        )
    with pytest.raises(PlanClarificationError, match="digest is stale"):
        validate_clarification(clarification, current_revision=4, current_digest="0" * 64)


def test_proposal_validator_accepts_only_typed_clarification() -> None:
    plan = _plan()
    proposal = validate_plan_proposal(
        {
            "plan": plan,
            "clarification": {
                "prior_revision": 4,
                "proposed_revision": 5,
                "invariant_digest": invariant_digest(plan),
                "continuation_task": "continue",
                "changed_fields": [],
                "requires_human": False,
            },
        }
    )
    assert proposal["clarification"]["proposed_revision"] == 5  # type: ignore[index]
