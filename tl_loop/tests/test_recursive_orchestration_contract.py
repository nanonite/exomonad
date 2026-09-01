"""Executable tests for the target recursive TL orchestration contract."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.lane import (
    LaneAbandoned,
    LaneBookkeepingStarted,
    LaneIntegrationStarted,
    LaneParkRequested,
    LanePhase,
    LaneRecoveryRequested,
    LaneReleased,
    LaneReserved,
    LaneState,
)
from tl_loop.fsm.orchestration import (
    IllegalTransition,
    stable_integration_order,
    transition,
    transition_lane,
)
from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.post_merge_events import (
    ChangelogCommitted,
    ChangelogPending,
    IssueCloseConfirmed,
    IssueClosePending,
    MergeAdopted,
    ParentBranchSynced,
    ParentPushPending,
    PostMergeComplete,
    PostMergeRebuildRequested,
)
from tl_loop.fsm.post_merge_evidence import PushReceipt
from tl_loop.fsm.scope import (
    FailureRecorded,
    FinalizationComplete,
    FinalizationRequested,
    ParkRequested,
    ScopeRole,
    StageReleased,
    TLAllMerged,
    TLDone,
    TLFailed,
    TLFinalizing,
    TLParked,
    TLPlanning,
    TLPRFiled,
    TLRunning,
    WorkerCompleted,
)
from tl_loop.loop.driver import WorkPlan, derive_child_branch, derive_child_worktree
from tl_loop.ordered import (
    AggregateCandidate,
    CodeReviewEvidence,
    IntegrationEvidence,
    IntegrationLifecycle,
    IntegrationState,
    IntegrationTransition,
    transition_integration,
)
from tl_loop.state.schema import BudgetLedger, FSMState, IntegrationRuntimeState
from tl_loop.state.store import RunStore, create


def _child(
    child_id: str,
    kind: ChildKind = ChildKind.SUB_TL,
) -> ChildRecord:
    return ChildRecord(
        child_id,
        kind,
        dispatch_intent_id=f"intent-{child_id}",
        invocation_id=f"invocation-{child_id}",
        evidence={"publication": f"publication-{child_id}"},
        lane_id=f"lane-{child_id}",
    )


def _running(
    *groups: tuple[int, tuple[ChildRecord, ...]],
    current: int = 1,
    parallel: tuple[ChildRecord, ...] = (),
) -> TLRunning:
    return TLRunning(
        current_order=current,
        pending_by_order=dict(groups),
        parallel_pending=parallel,
    )


def _receipt(
    child_id: str,
    commit: str,
    *,
    repository: str = "repo",
    parent_branch: str = "parent",
    lane_epoch: int = 1,
    push_intent_id: str | None = None,
    push_journal_id: str | None = None,
    expected_base_sha: str | None = None,
) -> PushReceipt:
    return PushReceipt(
        repository=repository,
        parent_branch=parent_branch,
        child_id=child_id,
        lane_epoch=lane_epoch,
        push_intent_id=push_intent_id or f"push-{child_id}",
        push_journal_id=push_journal_id or f"push-journal-{child_id}",
        push_receipt_id=f"receipt-{child_id}",
        expected_base_sha=expected_base_sha or f"base-{child_id}",
        pushed_commit=commit,
        observed_remote_head=f"remote-{commit}",
        ancestry_proof=f"ancestor:{commit}",
    )


def _adopted(
    child_id: str,
    pr_number: int,
    head_sha: str,
    journal_id: str,
) -> MergeAdopted:
    return MergeAdopted(child_id, pr_number, head_sha, journal_id, "repo", "parent")


def _complete_pr(phase: TLRunning, child_id: str, pr_number: int) -> TLRunning | TLAllMerged:
    phase = transition(
        phase, _adopted(child_id, pr_number, f"head-{child_id}", f"merge-{child_id}")
    )
    phase = transition(phase, ParentBranchSynced(child_id, "parent", f"parent-{child_id}"))
    phase = transition(phase, IssueClosePending(child_id, f"issue-{child_id}", f"close-{child_id}"))
    phase = transition(
        phase,
        IssueCloseConfirmed(
            child_id, f"issue-{child_id}", f"close-{child_id}", f"close-journal-{child_id}"
        ),
    )
    phase = transition(phase, ChangelogPending(child_id, f"log-{child_id}"))
    phase = transition(
        phase, ChangelogCommitted(child_id, f"log-{child_id}", f"log-commit-{child_id}")
    )
    phase = transition(
        phase,
        ParentPushPending(
            child_id,
            f"push-{child_id}",
            f"base-{child_id}",
            f"push-journal-{child_id}",
        ),
    )
    return transition(
        phase,
        PostMergeComplete(
            child_id,
            f"merge-{child_id}",
            f"push-{child_id}",
            f"log-commit-{child_id}",
            _receipt(
                child_id,
                f"log-commit-{child_id}",
                push_journal_id=f"push-journal-{child_id}",
            ),
        ),
    )


def test_recursive_order_scopes_reset_at_each_nested_plan() -> None:
    plan = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {
                    "name": "later",
                    "order": 2,
                    "plan": {
                        "sub_tls": [
                            {"name": "nested-later", "order": 2, "plan": {}},
                            {"name": "nested-first", "order": 1, "plan": {}},
                        ]
                    },
                },
                {
                    "name": "first",
                    "order": 1,
                    "plan": {
                        "sub_tls": [
                            {"name": "parallel-b", "order": 1, "plan": {}},
                            {"name": "parallel-a", "order": 1, "plan": {}},
                        ]
                    },
                },
                {"name": "also-first", "order": 1, "plan": {}},
            ]
        }
    )

    assert [stage.order for stage in plan.ordered_stages] == [1, 2]
    assert plan.ordered_stages[0].sub_tls == ("also-first", "first")
    assert plan.ordered_stages[1].sub_tls == ("later",)
    nested = next(task.plan for task in plan.sub_tls if task.name == "first")
    assert [stage.order for stage in nested.ordered_stages] == [1]
    later_nested = next(task.plan for task in plan.sub_tls if task.name == "later")
    assert [stage.order for stage in later_nested.ordered_stages] == [1, 2]


def test_ordered_scope_keeps_direct_workers_and_leaves_in_parallel_block() -> None:
    plan = WorkPlan.from_mapping(
        {
            "workers": [{"name": "worker", "task": "inspect"}],
            "leaves": [{"name": "leaf", "task": "publish"}],
            "sub_tls": [{"name": "stage", "order": 1, "plan": {}}],
        }
    )

    assert [worker.name for worker in plan.workers] == ["worker"]
    assert [leaf.name for leaf in plan.leaves] == ["leaf"]
    assert plan.ordered_stages[0].sub_tls == ("stage",)


def test_target_phases_retain_scope_dispatch_and_lane_payloads() -> None:
    planning = TLPlanning(
        ((1, (_child("sub-z"), _child("sub-a"))),),
        scope_path=("root", "integration"),
        plan_digest="plan-sha",
    )
    running = transition(planning, StageReleased(1, ("sub-a", "sub-z")))

    assert running.scope_path == ("root", "integration")
    assert running.plan_digest == "plan-sha"
    assert running.dispatch_intents["sub-a"] == "intent-sub-a"
    assert running.lane_bindings["sub-z"] == "lane-sub-z"
    assert running.pending_by_order[1][0].evidence["publication"] == "publication-sub-a"


def test_empty_plan_has_an_explicit_no_work_successor() -> None:
    result = transition(
        TLPlanning(scope_path=("root", "empty"), plan_digest="empty-plan"),
        StageReleased(0, ()),
    )
    assert result == TLAllMerged(scope_path=("root", "empty"), plan_digest="empty-plan")


def test_direct_worker_and_leaf_pre_stage_is_typed_and_ordered() -> None:
    planning = TLPlanning(
        ((1, (_child("sub-tl"),)),),
        parallel_children=(_child("leaf", ChildKind.LEAF), _child("worker", ChildKind.WORKER)),
    )
    running = transition(planning, StageReleased(1, ("sub-tl",)))
    assert isinstance(running, TLRunning)
    running = transition(running, WorkerCompleted("worker", "worker-result"))
    assert running.current_order == 1
    assert tuple(record.child_id for record in running.parallel_pending) == ("leaf",)
    with pytest.raises(IllegalTransition):
        transition(running, WorkerCompleted("leaf", "wrong-result"))
    running = _complete_pr(running, "leaf", 7)
    assert isinstance(running, TLRunning)
    assert running.current_order == 1


def test_same_order_integration_is_stable_and_independent_of_review_arrival() -> None:
    assert stable_integration_order(("sub-z", "sub-a")) == ("sub-a", "sub-z")
    assert stable_integration_order(("sub-a", "sub-z")) == ("sub-a", "sub-z")


def test_child_coordinates_are_relative_to_the_direct_parent() -> None:
    assert derive_child_branch("parent.feature", "review") == "parent.feature.review"
    assert derive_child_worktree(Path("/tmp/exomonad/parent"), "review") == Path(
        "/tmp/exomonad/parent/review"
    )


def test_repository_lane_releases_only_after_bookkeeping_push() -> None:
    lane = LaneState(repository="repo", parent_branch="parent")
    reserved = transition_lane(lane, LaneReserved("aggregate", 1, "base-1"))
    integrating = transition_lane(reserved, LaneIntegrationStarted("aggregate", "head-1"))
    bookkeeping = transition_lane(
        integrating,
        LaneBookkeepingStarted(
            "aggregate",
            "merge-journal-1",
            "push-intent-1",
            "push-journal-1",
            "bookkeeping-1",
            "base-2",
        ),
    )
    receipt = _receipt(
        "aggregate",
        "bookkeeping-1",
        push_intent_id="push-intent-1",
        push_journal_id="push-journal-1",
        expected_base_sha="base-2",
    )
    released = transition_lane(bookkeeping, LaneReleased("aggregate", receipt))

    assert integrating.phase is LanePhase.INTEGRATING
    assert bookkeeping.phase is LanePhase.BOOKKEEPING
    assert bookkeeping.expected_base_sha == "base-2"
    assert released.phase is LanePhase.IDLE
    assert released.repository == "repo"
    assert released.last_push_receipt_id == "receipt-aggregate"
    assert released.last_remote_head == "remote-bookkeeping-1"
    with pytest.raises(IllegalTransition):
        transition_lane(reserved, LaneReleased("aggregate", receipt))
    with pytest.raises(ValueError, match="intent"):
        transition_lane(
            bookkeeping,
            LaneReleased(
                "aggregate",
                _receipt(
                    "aggregate",
                    "bookkeeping-1",
                    push_intent_id="other-intent",
                    expected_base_sha="base-1",
                ),
            ),
        )
    with pytest.raises(ValueError, match="child"):
        transition_lane(
            bookkeeping,
            LaneReleased(
                "aggregate",
                _receipt(
                    "other-child",
                    "bookkeeping-1",
                    push_intent_id="push-intent-1",
                    push_journal_id="push-journal-1",
                    expected_base_sha="base-1",
                ),
            ),
        )
    with pytest.raises(ValueError, match="repository"):
        transition_lane(
            bookkeeping,
            LaneReleased(
                "aggregate",
                _receipt(
                    "aggregate",
                    "bookkeeping-1",
                    repository="other-repository",
                    push_intent_id="push-intent-1",
                    push_journal_id="push-journal-1",
                    expected_base_sha="base-1",
                ),
            ),
        )
    invalid_receipts = (
        ("parent branch", _receipt("aggregate", "bookkeeping-1", parent_branch="other")),
        ("epoch", _receipt("aggregate", "bookkeeping-1", lane_epoch=2)),
        (
            "intent",
            _receipt(
                "aggregate",
                "bookkeeping-1",
                push_intent_id="other-intent",
                push_journal_id="push-journal-1",
                expected_base_sha="base-1",
            ),
        ),
        (
            "journal",
            _receipt(
                "aggregate",
                "bookkeeping-1",
                push_intent_id="push-intent-1",
                push_journal_id="other-journal",
                expected_base_sha="base-1",
            ),
        ),
        (
            "base",
            _receipt(
                "aggregate",
                "bookkeeping-1",
                push_intent_id="push-intent-1",
                push_journal_id="push-journal-1",
                expected_base_sha="other-base",
            ),
        ),
        (
            "commit",
            _receipt(
                "aggregate",
                "other-commit",
                push_intent_id="push-intent-1",
                push_journal_id="push-journal-1",
                expected_base_sha="base-2",
            ),
        ),
    )
    for field, invalid in invalid_receipts:
        with pytest.raises(ValueError, match=field):
            transition_lane(bookkeeping, LaneReleased("aggregate", invalid))

    recovering = transition_lane(integrating, LaneRecoveryRequested("merge response unknown"))
    assert recovering.phase is LanePhase.RECOVERY
    with pytest.raises(ValueError, match="unresolved child"):
        transition_lane(recovering, LaneReserved("other-child", 2, "base-2"))
    parked = transition_lane(recovering, LaneParkRequested("operator_gate", "reconcile merge"))
    assert parked.phase is LanePhase.PARKED
    with pytest.raises(IllegalTransition):
        transition_lane(parked, LaneReserved("aggregate", 2, "base-2"))
    recovered_parked = transition_lane(
        parked, LaneRecoveryRequested("operator will resolve the parked lane")
    )
    assert recovered_parked.phase is LanePhase.RECOVERY
    released = transition_lane(
        recovered_parked, LaneAbandoned("operator_gate", "release resource lane")
    )
    assert released.phase is LanePhase.IDLE
    reusable = transition_lane(released, LaneReserved("next-child", 2, "base-2"))
    assert reusable.phase is LanePhase.RESERVED
    assert reusable.child_id == "next-child"


def test_repository_lanes_are_durable_and_reserve_atomically(tmp_path: Path) -> None:
    store = RunStore("lane-run", tmp_path)
    create("lane-run", {}, root_dir=tmp_path)
    store.checkpoint(
        FSMState(TLPhase.TLPlanning, ()),
        {},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
        integration=IntegrationRuntimeState(),
    )

    reserved = store.transition_lane("repo", "main", LaneReserved("child-a", 1, "base-a"))
    assert reserved.integration.lanes["repo:main"].child_id == "child-a"
    assert reserved.integration.lanes["repo:main"].last_lane_epoch == 1

    with pytest.raises(ValueError, match="no lane transition"):
        store.transition_lane("repo", "main", LaneReserved("child-b", 1, "base-b"))
    assert store.resume().integration.lanes["repo:main"].child_id == "child-a"

    other_branch = store.transition_lane("repo", "release", LaneReserved("child-b", 1, "base-b"))
    assert other_branch.integration.lanes["repo:release"].child_id == "child-b"
    assert other_branch.integration.lanes["repo:main"].child_id == "child-a"


def test_same_parent_race_serializes_but_different_parents_progress_concurrently(
    tmp_path: Path,
) -> None:
    store = RunStore("lane-race", tmp_path)
    create("lane-race", {}, root_dir=tmp_path)
    store.checkpoint(
        FSMState(TLPhase.TLPlanning, ()),
        {},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
        integration=IntegrationRuntimeState(),
    )

    def reserve(branch: str, child_id: str, start: Barrier) -> tuple[str, str]:
        start.wait(timeout=2)
        try:
            result = store.transition_lane(
                "repo",
                branch,
                LaneReserved(child_id, 1, f"base-{child_id}"),
            )
        except ValueError as error:
            return "error", str(error)
        return "ok", result.integration.lanes[f"repo:{branch}"].child_id or ""

    same_parent_start = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        same_parent = list(
            pool.map(
                lambda args: reserve(*args),
                (
                    ("main", "child-a", same_parent_start),
                    ("main", "child-b", same_parent_start),
                ),
            )
        )
    assert sorted(result[0] for result in same_parent) == ["error", "ok"]
    assert store.load().integration.lanes["repo:main"].child_id in {"child-a", "child-b"}

    different_parent_start = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        different_parents = list(
            pool.map(
                lambda args: reserve(*args),
                (
                    ("release", "child-c", different_parent_start),
                    ("develop", "child-d", different_parent_start),
                ),
            )
        )
    assert [result[0] for result in different_parents] == ["ok", "ok"]
    lanes = store.load().integration.lanes
    assert lanes["repo:release"].child_id == "child-c"
    assert lanes["repo:develop"].child_id == "child-d"


def test_review_and_ci_evidence_remain_bound_to_head_and_base() -> None:
    candidate = AggregateCandidate("aggregate", 43, "head-43", "patch-43", "base-1")
    review = CodeReviewEvidence("head-43", "patch-43", "GO", "2026-08-29T00:00:00Z")
    integration = IntegrationEvidence(
        "base-1", "head-43", "tree-43", "success", "2026-08-29T00:01:00Z"
    )
    ready = IntegrationState(
        IntegrationLifecycle.READY_FOR_INTEGRATION,
        candidate=candidate,
        code_review=review,
        integration=integration,
    )

    assert ready.code_review.head_sha == candidate.head_sha
    assert ready.integration.base_sha == candidate.original_base_sha
    assert transition_integration(ready, IntegrationTransition.BASE_INVALIDATED).lifecycle is (
        IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    )
    assert transition_integration(ready, IntegrationTransition.HEAD_INVALIDATED).lifecycle is (
        IntegrationLifecycle.REPAIRING_AGGREGATE
    )


def test_live_and_replayed_inputs_share_the_same_typed_transition() -> None:
    running = _running((1, (_child("aggregate"),)))
    live = _adopted("aggregate", 43, "head-43", "journal-43")
    replayed = _adopted("aggregate", 43, "head-43", "journal-43")

    assert live == replayed
    assert transition(running, live) == transition(running, replayed)
    assert transition(running, live).post_merge["aggregate"].phase is (
        PostMergePhase.REMOTE_MERGE_ADOPTED
    )


def test_numeric_orders_advance_only_after_the_complete_post_merge_sequence() -> None:
    planning = TLPlanning(
        (
            (1, (_child("sub-a"), _child("sub-z"))),
            (2, (_child("sub-tl"),)),
        )
    )
    running = transition(planning, StageReleased(1, ("sub-a", "sub-z")))

    with pytest.raises(IllegalTransition):
        transition(running, ParentBranchSynced("sub-a", "parent", "parent-a"))
    with pytest.raises(IllegalTransition):
        transition(
            running,
            PostMergeComplete(
                "sub-a",
                "merge-sub-a",
                "push-sub-a",
                "commit-sub-a",
                _receipt("sub-a", "commit-sub-a"),
            ),
        )

    after_a = _complete_pr(running, "sub-a", 1)
    assert isinstance(after_a, TLRunning)
    assert after_a.current_order == 1
    assert after_a.post_merge["sub-a"].phase is PostMergePhase.COMPLETE
    assert tuple(record.child_id for record in after_a.pending_by_order[1]) == ("sub-z",)
    after_z = _complete_pr(after_a, "sub-z", 2)
    assert isinstance(after_z, TLRunning)
    assert after_z.current_order == 2
    assert tuple(record.child_id for record in after_z.pending_by_order[2]) == ("sub-tl",)
    complete = _complete_pr(after_z, "sub-tl", 3)
    assert isinstance(complete, TLAllMerged)
    assert all(
        state.phase is PostMergePhase.COMPLETE for state in complete.completed_children.values()
    )


def test_post_merge_events_require_matching_predecessor_evidence() -> None:
    running = _running((1, (_child("aggregate"),)))
    completion = PostMergeComplete(
        "aggregate",
        "merge-journal",
        "push-intent",
        "log-commit",
        _receipt(
            "aggregate",
            "log-commit",
            push_intent_id="push-intent",
            push_journal_id="push-journal",
            expected_base_sha="base",
        ),
    )
    with pytest.raises(IllegalTransition):
        transition(running, completion)
    merged = transition(running, _adopted("aggregate", 43, "head", "merge-journal"))
    with pytest.raises(IllegalTransition):
        transition(merged, IssueClosePending("aggregate", "issue", "close-intent"))
    with pytest.raises(IllegalTransition):
        transition(merged, completion)
    synced = transition(merged, ParentBranchSynced("aggregate", "parent", "parent-head"))
    with pytest.raises(IllegalTransition):
        transition(synced, completion)
    pending = transition(synced, IssueClosePending("aggregate", "issue", "close-intent"))
    with pytest.raises(IllegalTransition):
        transition(pending, completion)
    with pytest.raises(IllegalTransition):
        transition(
            pending, IssueCloseConfirmed("aggregate", "other-issue", "close-intent", "journal")
        )
    with pytest.raises(IllegalTransition):
        transition(pending, ChangelogPending("aggregate", "log-intent"))
    confirmed = transition(
        pending, IssueCloseConfirmed("aggregate", "issue", "close-intent", "close-journal")
    )
    with pytest.raises(IllegalTransition):
        transition(confirmed, completion)
    changelog_pending = transition(confirmed, ChangelogPending("aggregate", "log-intent"))
    with pytest.raises(IllegalTransition):
        transition(changelog_pending, completion)
    committed = transition(
        changelog_pending, ChangelogCommitted("aggregate", "log-intent", "log-commit")
    )
    with pytest.raises(IllegalTransition):
        transition(committed, completion)
    push_pending = transition(
        committed,
        ParentPushPending("aggregate", "push-intent", "base", "push-journal"),
    )
    with pytest.raises(IllegalTransition, match="changelog_commit_sha"):
        transition(
            push_pending,
            PostMergeComplete(
                "aggregate",
                "merge-journal",
                "push-intent",
                "different-commit",
                _receipt(
                    "aggregate",
                    "different-commit",
                    push_intent_id="push-intent",
                    push_journal_id="push-journal",
                    expected_base_sha="base",
                ),
            ),
        )
    complete = transition(push_pending, completion)
    assert isinstance(complete, TLAllMerged)
    assert complete.completed_children["aggregate"].phase is PostMergePhase.COMPLETE


def test_rebased_bookkeeping_requires_an_explicit_rebuild_generation() -> None:
    phase: TLRunning | TLAllMerged = _running((1, (_child("aggregate"),)))
    phase = transition(phase, _adopted("aggregate", 43, "head", "merge-journal"))
    phase = transition(phase, ParentBranchSynced("aggregate", "parent", "parent-head"))
    phase = transition(phase, IssueClosePending("aggregate", "issue", "close-intent"))
    phase = transition(
        phase, IssueCloseConfirmed("aggregate", "issue", "close-intent", "close-journal")
    )
    phase = transition(phase, ChangelogPending("aggregate", "log-intent"))
    phase = transition(phase, ChangelogCommitted("aggregate", "log-intent", "log-commit"))
    phase = transition(phase, ParentPushPending("aggregate", "push-intent", "base", "push-journal"))
    assert isinstance(phase, TLRunning)
    rebuilt = transition(
        phase,
        PostMergeRebuildRequested(
            "aggregate",
            1,
            "push-intent",
            "push-journal",
            "compare-and-push rejected",
            "remote-before-rebuild",
            "base-after-rebuild",
            "merge-remains-ancestor",
            "parent branch rebased",
        ),
    )
    assert isinstance(rebuilt, TLRunning)
    assert rebuilt.post_merge["aggregate"].phase is PostMergePhase.CHANGELOG_PENDING
    with pytest.raises(IllegalTransition, match="fresh changelog intent"):
        transition(rebuilt, ChangelogCommitted("aggregate", "log-intent", "rebuilt-commit"))
    rebuilt = transition(
        rebuilt,
        ChangelogCommitted("aggregate", "rebuilt-log-intent", "rebuilt-log-commit"),
    )
    assert rebuilt.post_merge["aggregate"].evidence["rebuild_applied"] == "true"
    assert rebuilt.post_merge["aggregate"].evidence["rebuild_commit_sha"] == "rebuilt-log-commit"
    with pytest.raises(IllegalTransition, match="push intent must be fresh"):
        transition(
            rebuilt,
            ParentPushPending("aggregate", "push-intent", "base-after-rebuild", "fresh-journal"),
        )
    with pytest.raises(IllegalTransition, match="push journal must be fresh"):
        transition(
            rebuilt,
            ParentPushPending("aggregate", "fresh-intent", "base-after-rebuild", "push-journal"),
        )
    accepted = transition(
        rebuilt,
        ParentPushPending("aggregate", "fresh-intent", "base-after-rebuild", "fresh-journal"),
    )
    assert accepted.post_merge["aggregate"].evidence["parent_push_intent_id"] == "fresh-intent"
    with pytest.raises(IllegalTransition):
        transition(
            rebuilt,
            PostMergeRebuildRequested(
                "aggregate",
                1,
                "push-intent",
                "push-journal",
                "same generation",
                "remote-before-rebuild",
                "base-after-rebuild",
                "merge-remains-ancestor",
                "same generation",
            ),
        )


def test_all_merged_rejects_fabricated_complete_evidence() -> None:
    with pytest.raises(ValueError, match="requires complete evidence"):
        TLAllMerged(completed_children={"child": PostMergeState(PostMergePhase.COMPLETE, {})})


def test_intermediate_checkpoints_retain_cumulative_predecessor_evidence() -> None:
    with pytest.raises(ValueError, match="complete evidence"):
        PostMergeState(
            PostMergePhase.CHANGELOG_PENDING,
            {"changelog_intent_id": "intent", "changelog_generation": "0"},
        )


def test_post_merge_completion_rejects_a_mismatched_merge_journal() -> None:
    running = _running((1, (_child("aggregate"),)))
    running = transition(running, _adopted("aggregate", 43, "head", "merge-journal"))
    running = transition(running, ParentBranchSynced("aggregate", "parent", "parent-head"))
    running = transition(running, IssueClosePending("aggregate", "issue", "close-intent"))
    running = transition(
        running, IssueCloseConfirmed("aggregate", "issue", "close-intent", "close-journal")
    )
    running = transition(running, ChangelogPending("aggregate", "log-intent"))
    running = transition(running, ChangelogCommitted("aggregate", "log-intent", "log-commit"))
    running = transition(
        running, ParentPushPending("aggregate", "push-intent", "base", "push-journal")
    )
    with pytest.raises(IllegalTransition, match="evidence mismatch"):
        transition(
            running,
            PostMergeComplete(
                "aggregate",
                "wrong-journal",
                "push-intent",
                "commit",
                _receipt("aggregate", "commit"),
            ),
        )


def test_child_kind_prevents_worker_completion_of_pr_children() -> None:
    running = _running(
        (1, (_child("leaf", ChildKind.LEAF), _child("sub-tl", ChildKind.SUB_TL))),
        current=0,
        parallel=(_child("worker", ChildKind.WORKER),),
    )
    running = transition(running, WorkerCompleted("worker", "worker-result"))
    assert running.current_order == 1
    with pytest.raises(IllegalTransition):
        transition(running, WorkerCompleted("leaf", "wrong-result"))
    with pytest.raises(IllegalTransition):
        transition(running, _adopted("worker", 43, "head", "journal"))


def test_non_root_finalization_produces_durable_tlprfiled() -> None:
    completed = _complete_pr(_running((1, (_child("child"),))), "child", 43)
    assert isinstance(completed, TLAllMerged)
    all_merged = TLAllMerged(
        scope_path=("root", "child"),
        plan_digest="plan-child",
        completed_children=completed.completed_children,
    )
    finalizing = transition(all_merged, FinalizationRequested(ScopeRole.NON_ROOT))
    assert isinstance(finalizing, TLFinalizing)
    filed = transition(
        finalizing,
        FinalizationComplete(
            ScopeRole.NON_ROOT,
            {
                "aggregate_pr": "43",
                "head_sha": "head-43",
                "base_sha": "base-42",
                "parent_branch": "main",
                "handoff": "handoff-43",
            },
        ),
    )
    assert isinstance(filed, TLPRFiled)
    assert filed.aggregate_pr == "43"
    assert filed.handoff == "handoff-43"
    assert not isinstance(filed, TLDone)


def test_root_finalization_retains_evidence_and_non_root_requires_all_fields() -> None:
    finalizing = transition(TLAllMerged(), FinalizationRequested(ScopeRole.ROOT))
    done = transition(
        finalizing,
        FinalizationComplete(
            ScopeRole.ROOT,
            {"root_branch": "main", "local_checkout": "checkout-clean"},
        ),
    )
    assert isinstance(done, TLDone)
    assert done.finalization_evidence["root_branch"] == "main"
    non_root = transition(TLAllMerged(), FinalizationRequested(ScopeRole.NON_ROOT))
    with pytest.raises(ValueError, match="incomplete"):
        transition(non_root, FinalizationComplete(ScopeRole.NON_ROOT, {"aggregate_pr": "43"}))


def test_terminal_phases_are_closed_to_automatic_events() -> None:
    terminal = (
        TLDone(finalization_evidence={"root_branch": "main"}),
        TLPRFiled("43", "head", "base", "main", "handoff"),
        TLFailed("failed"),
        TLParked("blocked", "diagnostic"),
    )
    events = (
        StageReleased(1, ("child",)),
        WorkerCompleted("child", "result"),
        _adopted("child", 43, "head", "journal"),
        ParentBranchSynced("child", "main", "parent-head"),
        PostMergeComplete(
            "child",
            "journal",
            "push-intent",
            "commit",
            _receipt("child", "commit", push_intent_id="push-intent"),
        ),
        FinalizationRequested(ScopeRole.ROOT),
        FailureRecorded("failure"),
        ParkRequested("blocked", "diagnostic"),
    )
    for phase in terminal:
        for event in events:
            with pytest.raises(IllegalTransition):
                transition(phase, event)


def test_failure_and_park_are_typed_for_non_terminal_scopes() -> None:
    assert transition(
        TLPlanning(((1, (_child("child"),)),)), FailureRecorded("failed")
    ) == TLFailed("failed")
    assert transition(
        _running((1, (_child("child"),))), ParkRequested("review_stuck", "awaiting review")
    ) == TLParked("review_stuck", "awaiting review")
