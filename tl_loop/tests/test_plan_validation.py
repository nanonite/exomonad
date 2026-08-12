"""Closed-key and authority-boundary tests for plan proposals."""

from __future__ import annotations

import pytest

from tl_loop.plan_validation import (
    PlanValidationError,
    validate_plan_document,
    validate_plan_proposal,
)


def _leaf(name: str, boundary: list[str]) -> dict[str, object]:
    return {"name": name, "task": f"implement {name}", "boundary": boundary}


def test_plan_document_and_proposal_share_workplan_validation() -> None:
    document = {"run_id": "root", "plan": {"leaves": [_leaf("api", ["src/api.py"])]}}

    validated = validate_plan_document(document)
    proposal = validate_plan_proposal({"plan": document["plan"]})

    assert validated["plan"] == proposal["plan"]
    assert validated is not document


def test_unknown_keys_are_rejected_at_document_and_task_boundaries() -> None:
    unknown_document = {"plan": {"leaves": []}, "confirm": True}
    with pytest.raises(PlanValidationError, match="unknown keys: confirm"):
        validate_plan_proposal(unknown_document)

    unknown_task = {"plan": {"leaves": [{**_leaf("api", ["src/api.py"]), "owner": "human"}]}}
    with pytest.raises(PlanValidationError, match="unknown keys: owner"):
        validate_plan_proposal(unknown_task)


def test_proposals_cannot_change_run_identity_or_budget() -> None:
    for forbidden in ({"run_id": "other"}, {"budgets": {"tokens": 1}}):
        with pytest.raises(PlanValidationError, match="unknown keys"):
            validate_plan_proposal({**forbidden, "plan": {"leaves": []}})


def test_overlapping_owned_paths_are_rejected() -> None:
    proposal = {
        "plan": {
            "leaves": [
                _leaf("api", ["src/shared/**"]),
                _leaf("tests", ["src/shared/api.py"]),
            ]
        }
    }
    with pytest.raises(PlanValidationError, match="overlaps"):
        validate_plan_proposal(proposal)


def test_validation_detaches_the_proposed_plan() -> None:
    plan = {"leaves": [_leaf("api", ["src/api.py"])]}
    proposal = validate_plan_proposal({"plan": plan})
    cast_plan = proposal["plan"]
    assert isinstance(cast_plan, dict)

    plan["leaves"].append(_leaf("docs", ["docs/**"]))  # type: ignore[union-attr]
    assert len(cast_plan["leaves"]) == 1  # type: ignore[index]
