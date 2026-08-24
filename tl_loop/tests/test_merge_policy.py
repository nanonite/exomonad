"""Decision-table coverage for persisted direct and aggregate merge policy."""

from __future__ import annotations

from dataclasses import replace

from tl_loop.loop.reconcile import ExternalIntent, InternalTransition, Quiescent, derive_next_action
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    BudgetLedger,
    EventCursor,
    FSMState,
    HandoffEvidence,
    IntegrationRuntimeState,
    PublicationBinding,
    RunState,
    SliceState,
    SliceStatus,
    TLPhase,
    Verdict,
)
from tl_loop.tests.test_reconcile import _slice


def _mergeable() -> SliceState:
    return replace(
        _slice(SliceStatus.IN_REVIEW),
        pr_number=42,
        reviewed_head="head-a",
        publication=PublicationBinding(42, "head-a", "task/a", "main", 1),
        handoff=HandoffEvidence(42, "head-a", 1, "inv-1", "agent-a", "2026-08-24T00:00:00Z"),
        reviewer_attempt={"head-a": 1},
        verdict=Verdict.GO,
        ci_state={"head-a": "success"},
    )


def test_direct_merge_policy_is_order_independent_and_head_bound() -> None:
    state = _mergeable()

    first = derive_next_action(state)
    second = derive_next_action(replace(state, observation_provenance=None))

    assert isinstance(first, ExternalIntent)
    assert first.operation == "merge"
    assert first.arguments == {"pr_number": 42, "head_sha": "head-a"}
    assert isinstance(second, ExternalIntent)
    assert second.arguments == first.arguments


def test_direct_policy_priority_rejects_mismatched_head_before_merge() -> None:
    state = replace(_mergeable(), publication=PublicationBinding(42, "head-b", "task/a", "main", 2))

    decision = derive_next_action(state)

    assert decision == InternalTransition("in_review", "head_reset")


def test_direct_policy_requires_handoff_review_and_ci_in_order() -> None:
    state = _mergeable()
    assert derive_next_action(replace(state, handoff=None)) == Quiescent("await_handoff")
    assert derive_next_action(replace(state, reviewer_attempt={})) == ExternalIntent(
        "spawn_reviewer", "slice-a", {"pr_number": 42, "head_sha": "head-a"}
    )
    assert derive_next_action(replace(state, verdict=None)) == Quiescent("await_review")
    assert derive_next_action(replace(state, ci_state={"head-a": "pending"})) == Quiescent(
        "await_ci"
    )


def test_direct_policy_routes_failure_and_inflight_merge_to_durable_actions() -> None:
    state = _mergeable()
    assert derive_next_action(replace(state, ci_state={"head-a": "failure"})) == ExternalIntent(
        "repair", "slice-a", {"head_sha": "head-a"}
    )
    assert derive_next_action(
        replace(
            state,
            action=ActionState(ActionKind.MERGE, ActionPhase.IN_FLIGHT, intent_id="merge-1"),
        )
    ) == Quiescent("await_merge_recovery")


def test_aggregate_policy_requires_stronger_integration_evidence() -> None:
    base = RunState(
        version=2,
        revision=1,
        run_id="aggregate-run",
        fsm=FSMState(TLPhase.TLWaiting, ()),
        slices={},
        budgets=BudgetLedger(0, 0),
        gates=(),
        events=EventCursor(0),
    )
    ready = replace(
        base,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.INTEGRATION_VALIDATED,
            aggregate_pr_number=77,
            aggregate_patch_digest="patch-a",
            integration_owner_id="aggregate-owner",
            integration_owner_run_id="child-a",
            integration_owner_branch="task/child-a",
            integration_owner_worktree=".worktrees/child-a",
            head_sha="aggregate-head",
            patch_digest="patch-a",
            validated_base_sha="base-a",
            merge_tree_sha="tree-a",
            ci_status="success",
            stage_verification="passed",
        ),
    )

    decision = derive_next_action(ready)
    blocked = derive_next_action(
        replace(ready, integration=replace(ready.integration, merge_tree_sha=None))
    )

    assert decision == ExternalIntent(
        "merge_aggregate",
        "aggregate-owner",
        {"pr_number": 77, "head_sha": "aggregate-head"},
    )
    assert blocked == Quiescent("await_integration_evidence")
