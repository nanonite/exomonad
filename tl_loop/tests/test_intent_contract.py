"""Closed operator-intent union contract coverage."""

from __future__ import annotations

import pytest

from tl_loop.rlm.intent_contract import (
    GateAnswerIntent,
    IntentValidationError,
    PlanProposalIntent,
    QueryIntent,
    UnclearIntent,
    parse_operator_intent,
)


def test_query_variants_are_typed_and_read_only() -> None:
    run = parse_operator_intent({"kind": "query", "run_id": "root", "view": "run"})
    transitions = parse_operator_intent(
        {"kind": "query", "run_id": "root", "view": "transitions", "limit": 10}
    )
    slice_query = parse_operator_intent(
        {"kind": "query", "run_id": "root", "view": "slice", "slice_id": "api"}
    )

    assert run == QueryIntent("root", "run")
    assert transitions.to_mapping()["limit"] == 10
    assert slice_query.to_mapping()["slice_id"] == "api"


def test_gate_answer_is_named_and_closed() -> None:
    intent = parse_operator_intent(
        {
            "kind": "gate_answer",
            "run_id": "root",
            "gate_name": "tl-timeout",
            "decision": "approve",
        }
    )

    assert intent == GateAnswerIntent("root", "tl-timeout", "approve")
    assert "merge" not in intent.to_mapping()


def test_plan_proposal_is_validated_by_the_plan_contract() -> None:
    intent = parse_operator_intent(
        {
            "kind": "plan_proposal",
            "run_id": "root",
            "plan": {"leaves": [{"name": "api", "task": "implement api"}]},
        }
    )

    assert intent == PlanProposalIntent(
        "root", {"leaves": [{"name": "api", "task": "implement api"}]}
    )

    with pytest.raises(IntentValidationError, match="overlaps"):
        parse_operator_intent(
            {
                "kind": "plan_proposal",
                "run_id": "root",
                "plan": {
                    "leaves": [
                        {"name": "a", "task": "a", "boundary": ["src/**"]},
                        {"name": "b", "task": "b", "boundary": ["src/a.py"]},
                    ]
                },
            }
        )


def test_unclear_is_first_class() -> None:
    intent = parse_operator_intent(
        {"kind": "unclear", "reason": "two runs match", "question": "Which run?"}
    )

    assert intent == UnclearIntent("two runs match", "Which run?")


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "query", "run_id": "root", "view": "slice"},
        {"kind": "query", "run_id": "root", "view": "run", "slice_id": "api"},
        {"kind": "query", "run_id": "root", "view": "run", "limit": 101},
        {"kind": "gate_answer", "run_id": "root", "gate_name": "", "decision": "approve"},
        {"kind": "unclear", "reason": "", "question": "try again"},
        {"kind": "query", "run_id": "../root", "view": "run"},
        {"kind": "query", "run_id": "root\\nested", "view": "run"},
        {"kind": "query", "run_id": "root", "view": "run", "extra": True},
    ],
)
def test_invalid_union_members_fail_closed(value: dict[str, object]) -> None:
    with pytest.raises(IntentValidationError):
        parse_operator_intent(value)
