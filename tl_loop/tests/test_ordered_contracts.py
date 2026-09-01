"""Contracts and transition-table tests for ordered recursive integration."""

from __future__ import annotations

import pytest

from tl_loop.loop.driver import WorkPlan
from tl_loop.ordered import (
    AggregateCandidate,
    CodeReviewEvidence,
    IntegrationEvidence,
    IntegrationLifecycle,
    IntegrationState,
    IntegrationTransition,
    IntegrationTransitionError,
    OrderedStage,
    allowed_integration_transitions,
    transition_integration,
)


def test_ordered_stages_group_siblings_and_reset_at_recursive_boundaries() -> None:
    plan = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {"name": "later", "order": 2, "plan": {}},
                {"name": "same", "order": 1, "plan": {}},
                {"name": "first", "order": 1, "plan": {}},
            ]
        }
    )

    assert plan.ordered_stages == (
        OrderedStage(1, ("first", "same")),
        OrderedStage(2, ("later",)),
    )
    assert plan.sub_tls[0].plan.ordered_stages == ()  # type: ignore[union-attr]


def test_missing_order_preserves_the_legacy_single_stage_contract() -> None:
    task = WorkPlan.from_mapping({"sub_tls": [{"name": "legacy", "plan": {}}]}).sub_tls[0]

    assert task.order == 1
    assert task.integration.aggregate_pr_required is True
    assert task.integration.base_revalidation_required is True


def test_contract_rejects_invalid_order_and_integration_keys() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        WorkPlan.from_mapping({"sub_tls": [{"name": "bad", "order": 0, "plan": {}}]})
    with pytest.raises(ValueError, match="unknown keys: mystery"):
        WorkPlan.from_mapping(
            {"sub_tls": [{"name": "bad", "plan": {}, "integration": {"mystery": True}}]}
        )


def test_ordered_plan_normalizes_numeric_stage_order() -> None:
    plan = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {"name": "stage-two", "order": 2, "plan": {}},
                {"name": "stage-one", "order": 1, "plan": {}},
            ]
        }
    )

    assert [task.name for task in plan.sub_tls] == ["stage-one", "stage-two"]


@pytest.mark.parametrize(
    ("sub_tls", "message"),
    [
        (
            [{"name": "one", "order": 1, "plan": {}}, {"name": "missing", "plan": {}}],
            "sub_tls[1].order",
        ),
        (
            [{"name": "zero", "order": 0, "plan": {}}],
            "sub_tls[0].order",
        ),
        (
            [{"name": "one", "order": 1, "plan": {}}, {"name": "three", "order": 3, "plan": {}}],
            "contiguous",
        ),
    ],
)
def test_ordered_plan_rejects_mixed_invalid_or_non_contiguous_orders(
    sub_tls: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ValueError) as error:
        WorkPlan.from_mapping({"sub_tls": sub_tls})
    assert message in str(error.value)


def test_ordered_plan_keeps_top_level_leaves_and_rejects_recursive_overlap() -> None:
    plan = WorkPlan.from_mapping(
        {
            "leaves": [{"name": "top", "task": "parallel"}],
            "sub_tls": [{"name": "stage", "order": 1, "plan": {}}],
        }
    )
    assert [leaf.name for leaf in plan.leaves] == ["top"]
    assert plan.ordered_stages[0].sub_tls == ("stage",)
    with pytest.raises(ValueError, match="ownership overlaps"):
        WorkPlan.from_mapping(
            {
                "sub_tls": [
                    {
                        "name": "one",
                        "order": 1,
                        "plan": {"leaves": [{"name": "a", "task": "a", "boundary": ["src/**"]}]},
                    },
                    {
                        "name": "two",
                        "order": 1,
                        "plan": {
                            "sub_tls": [
                                {
                                    "name": "nested",
                                    "plan": {
                                        "leaves": [
                                            {
                                                "name": "b",
                                                "task": "b",
                                                "boundary": ["src/api.py"],
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    },
                ]
            }
        )


def test_evidence_is_bound_to_the_required_dimensions() -> None:
    candidate = AggregateCandidate("stage-one", 7, "head-7", "patch-7", "base-1")
    review = CodeReviewEvidence("head-7", "patch-7", "GO", "2026-08-15T00:00:00Z")
    integration = IntegrationEvidence(
        "base-1", "head-7", "tree-7", "success", "2026-08-15T00:01:00Z"
    )

    assert candidate.sub_tl_id == "stage-one"
    assert review.head_sha == candidate.head_sha
    assert review.patch_digest == candidate.patch_digest
    assert integration.base_sha == candidate.original_base_sha

    with pytest.raises(ValueError, match="verdict"):
        CodeReviewEvidence("head-7", "patch-7", "APPROVED", "now")
    with pytest.raises(ValueError, match="ci_status"):
        IntegrationEvidence("base-1", "head-7", "tree-7", "green", "now")


def test_every_declared_transition_is_allowed_and_every_other_edge_is_rejected() -> None:
    for lifecycle in IntegrationLifecycle:
        state = IntegrationState(lifecycle=lifecycle)
        allowed = allowed_integration_transitions(lifecycle)
        for event in IntegrationTransition:
            if event in allowed:
                transition_integration(state, event)
            else:
                with pytest.raises(IntegrationTransitionError):
                    transition_integration(state, event)


def test_base_and_head_invalidation_have_distinct_recovery_states() -> None:
    ready = IntegrationState(IntegrationLifecycle.READY_FOR_INTEGRATION)

    base = transition_integration(ready, IntegrationTransition.BASE_INVALIDATED)
    head = transition_integration(ready, IntegrationTransition.HEAD_INVALIDATED)

    assert base.lifecycle is IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    assert head.lifecycle is IntegrationLifecycle.REPAIRING_AGGREGATE


def test_terminal_lifecycles_reject_late_events() -> None:
    for lifecycle in (
        IntegrationLifecycle.MERGED,
        IntegrationLifecycle.FAILED,
        IntegrationLifecycle.PARKED,
    ):
        with pytest.raises(IntegrationTransitionError):
            transition_integration(
                IntegrationState(lifecycle), IntegrationTransition.CHILDREN_MERGED
            )
