"""Independent bounded model coverage for recursive orchestration contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

import pytest

from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.lane import (
    LaneAbandoned,
    LaneBookkeepingStarted,
    LaneIntegrationStarted,
    LaneParkRequested,
    LanePhase,
    LaneRecoveryRequested,
    LaneRecoveryResolved,
    LaneReleased,
    LaneReserved,
    LaneState,
)
from tl_loop.fsm.orchestration import IllegalTransition, transition, transition_lane
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
)
from tl_loop.fsm.post_merge_evidence import PushReceipt
from tl_loop.fsm.scope import (
    BaseInvalidated,
    ChildDispatchRequested,
    ChildSpawned,
    ChildTerminal,
    CIObserved,
    FailureRecorded,
    FinalizationComplete,
    FinalizationRequested,
    Heartbeat,
    IntegrationValidated,
    LeafCompleted,
    MergeRequested,
    ParkRequested,
    PlanLoaded,
    PostMergeObserved,
    PublicationFiled,
    RecoveryObserved,
    RepairRequested,
    ReviewObserved,
    ScopeRole,
    StageReleased,
    TLAllMerged,
    TLDone,
    TLFinalizing,
    TLPlanning,
    TLPRFiled,
    TLRunning,
    WorkerCompleted,
)
from tl_loop.loop.driver import WorkPlan
from tl_loop.loop.fsm import HeartbeatTick, step
from tl_loop.ordered import (
    AggregateCandidate,
    CodeReviewEvidence,
    IntegrationEvidence,
    IntegrationLifecycle,
    IntegrationState,
    IntegrationTransition,
    IntegrationTransitionError,
    allowed_integration_transitions,
    transition_integration,
)
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.schema import BudgetLedger, EventCursor, FSMState, RunState


def _child(child_id: str, kind: ChildKind = ChildKind.SUB_TL) -> ChildRecord:
    return ChildRecord(
        child_id,
        kind,
        dispatch_intent_id=f"intent-{child_id}",
        invocation_id=f"invocation-{child_id}",
        lane_id=f"lane-{child_id}",
        evidence={"publication": f"publication-{child_id}"},
    )


def _running() -> TLRunning:
    return TLRunning(
        current_order=1,
        pending_by_order={1: (_child("child"),)},
        parallel_pending=(_child("worker", ChildKind.WORKER), _child("leaf", ChildKind.LEAF)),
    )


class ModelScopePhase(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    ALL_MERGED = "all_merged"
    FINALIZING = "finalizing"
    TERMINAL = "terminal"


_SCOPE_ALLOWED: dict[ModelScopePhase, frozenset[str]] = {
    ModelScopePhase.PLANNING: frozenset(
        {"PlanLoaded", "StageReleased", "FailureRecorded", "ParkRequested"}
    ),
    ModelScopePhase.RUNNING: frozenset(
        {
            "ChildDispatchRequested",
            "ChildSpawned",
            "ChildTerminal",
            "PublicationFiled",
            "ReviewObserved",
            "CIObserved",
            "BaseInvalidated",
            "IntegrationValidated",
            "MergeRequested",
            "PostMergeObserved",
            "RepairRequested",
            "RecoveryObserved",
            "Heartbeat",
            "WorkerCompleted",
            "LeafCompleted",
            "FailureRecorded",
            "ParkRequested",
        }
    ),
    ModelScopePhase.ALL_MERGED: frozenset(
        {"FinalizationRequested", "FailureRecorded", "ParkRequested"}
    ),
    ModelScopePhase.FINALIZING: frozenset(
        {"FinalizationComplete", "FailureRecorded", "ParkRequested"}
    ),
    ModelScopePhase.TERMINAL: frozenset(),
}


def _scope_events() -> dict[str, object]:
    return {
        "PlanLoaded": PlanLoaded(("root",), "plan", ("child",), (1,)),
        "StageReleased": StageReleased(1, ("child",), ("root",)),
        "ChildDispatchRequested": ChildDispatchRequested("child", "invocation", 1, "intent"),
        "ChildSpawned": ChildSpawned("child", "invocation", 1, "main.child", ".child"),
        "ChildTerminal": ChildTerminal("worker", "completed", {"result_digest": "worker-result"}),
        "PublicationFiled": PublicationFiled("child", 43, "head", "main", "digest"),
        "ReviewObserved": ReviewObserved("child", 1, "GO", "head", {"head_sha": "head"}),
        "CIObserved": CIObserved("child", "head", "success", {"head_sha": "head"}),
        "BaseInvalidated": BaseInvalidated("child", "base-old", "base-new"),
        "IntegrationValidated": IntegrationValidated("child", "base", "head", "tree", "success"),
        "MergeRequested": MergeRequested("child", 43, "head", "merge-intent"),
        "PostMergeObserved": PostMergeObserved("child", {"phase": "remote_merge_adopted"}),
        "RepairRequested": RepairRequested("child", "repair", ({"path": "src/a"},), 1),
        "RecoveryObserved": RecoveryObserved("child", "journal", "confirmed"),
        "Heartbeat": Heartbeat("2026-09-02T00:00:00Z"),
        "WorkerCompleted": WorkerCompleted("worker", "worker-result"),
        "LeafCompleted": LeafCompleted("leaf", "leaf-result"),
        "FinalizationRequested": FinalizationRequested(ScopeRole.ROOT),
        "FinalizationComplete": FinalizationComplete(
            ScopeRole.ROOT, {"root_branch": "main", "local_checkout": "checkout"}
        ),
        "FailureRecorded": FailureRecorded("failure"),
        "ParkRequested": ParkRequested("gate", "diagnostic"),
    }


def _scope_phase(phase: ModelScopePhase) -> object:
    if phase is ModelScopePhase.PLANNING:
        return TLPlanning(((1, (_child("child"),)),))
    if phase is ModelScopePhase.RUNNING:
        return _running()
    if phase is ModelScopePhase.ALL_MERGED:
        return TLAllMerged()
    if phase is ModelScopePhase.FINALIZING:
        return TLFinalizing(ScopeRole.ROOT)
    return TLDone()


def test_scope_reference_matrix_closes_terminal_phases() -> None:
    events = _scope_events()

    for phase, allowed in _SCOPE_ALLOWED.items():
        for name, event in events.items():
            if name in allowed:
                transition(_scope_phase(phase), event)  # type: ignore[arg-type]
            else:
                with pytest.raises(IllegalTransition):
                    transition(_scope_phase(phase), event)  # type: ignore[arg-type]


def test_scope_model_has_no_automatic_terminal_successors() -> None:
    assert all(
        not _SCOPE_ALLOWED[ModelScopePhase.TERMINAL]
        for _ in (TLDone(), TLPRFiled("43", "head", "base", "main", "handoff"))
    )


@dataclass(frozen=True)
class RecursivePlanShape:
    depth: int
    width: int

    def to_mapping(self) -> dict[str, object]:
        if self.depth == 0:
            return {
                "workers": [{"name": "worker", "task": "parallel"}],
                "leaves": [{"name": "leaf", "task": "parallel"}],
            }
        return {
            "workers": [{"name": "worker", "task": "parallel"}],
            "leaves": [{"name": "leaf", "task": "parallel"}],
            "sub_tls": [
                {
                    "name": f"scope-{self.depth}-{index}",
                    "order": index + 1,
                    "plan": RecursivePlanShape(self.depth - 1, self.width).to_mapping(),
                }
                for index in range(self.width)
            ],
        }


@pytest.mark.parametrize("depth", (0, 1, 2))
@pytest.mark.parametrize("width", (1, 2))
def test_bounded_recursive_plans_preserve_nested_serial_and_parallel_scopes(
    depth: int, width: int
) -> None:
    shape = RecursivePlanShape(depth, width)
    plan = WorkPlan.from_mapping(shape.to_mapping())
    manifest = build_plan_manifest(shape.to_mapping(), scope_id="root")

    assert len(plan.workers) == 1
    assert len(plan.leaves) == 1
    assert len(plan.ordered_stages) == (width if depth else 0)
    assert len(manifest.child_manifests) == (width if depth else 0)
    if depth:
        assert all(manifest.child_manifests[node_id].nodes for node_id in manifest.child_manifests)
        assert all(node.parent_id == "root" for node in manifest.nodes if node.kind == "sub_tl")


_POST_PHASE_FIELDS: tuple[tuple[PostMergePhase, tuple[str, ...]], ...] = (
    (
        PostMergePhase.REMOTE_MERGE_ADOPTED,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
        ),
    ),
    (
        PostMergePhase.PARENT_BRANCH_SYNCED,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
        ),
    ),
    (
        PostMergePhase.ISSUE_CLOSE_PENDING,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
            "issue_id",
            "issue_close_intent_id",
        ),
    ),
    (
        PostMergePhase.ISSUE_CLOSE_CONFIRMED,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
            "issue_id",
            "issue_close_intent_id",
            "issue_close_journal_id",
        ),
    ),
    (
        PostMergePhase.CHANGELOG_PENDING,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
            "issue_id",
            "issue_close_intent_id",
            "issue_close_journal_id",
            "changelog_intent_id",
            "changelog_generation",
        ),
    ),
    (
        PostMergePhase.CHANGELOG_COMMITTED,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
            "issue_id",
            "issue_close_intent_id",
            "issue_close_journal_id",
            "changelog_intent_id",
            "changelog_generation",
            "changelog_commit_sha",
        ),
    ),
    (
        PostMergePhase.PARENT_PUSH_PENDING,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
            "issue_id",
            "issue_close_intent_id",
            "issue_close_journal_id",
            "changelog_intent_id",
            "changelog_generation",
            "changelog_commit_sha",
            "parent_push_intent_id",
            "push_journal_id",
            "expected_base_sha",
        ),
    ),
    (
        PostMergePhase.COMPLETE,
        (
            "child_id",
            "repository",
            "parent_branch",
            "pr_number",
            "head_sha",
            "merge_journal_id",
            "lane_epoch",
            "parent_commit_sha",
            "issue_id",
            "issue_close_intent_id",
            "issue_close_journal_id",
            "changelog_intent_id",
            "changelog_generation",
            "changelog_commit_sha",
            "parent_push_intent_id",
            "push_journal_id",
            "expected_base_sha",
            "push_receipt_id",
            "pushed_commit",
            "bookkeeping_commit",
            "observed_remote_head",
            "ancestry_proof",
        ),
    ),
)

_POST_EVIDENCE = {
    "child_id": "child",
    "repository": "repo",
    "parent_branch": "main",
    "pr_number": "43",
    "head_sha": "head",
    "merge_journal_id": "merge-journal",
    "lane_epoch": "1",
    "parent_commit_sha": "parent-head",
    "issue_id": "issue-43",
    "issue_close_intent_id": "close-intent",
    "issue_close_journal_id": "close-journal",
    "changelog_intent_id": "changelog-intent",
    "changelog_generation": "0",
    "changelog_commit_sha": "changelog-commit",
    "parent_push_intent_id": "push-intent",
    "push_journal_id": "push-journal",
    "expected_base_sha": "parent-head",
    "push_receipt_id": "receipt",
    "pushed_commit": "changelog-commit",
    "bookkeeping_commit": "changelog-commit",
    "observed_remote_head": "remote-bookkeeping",
    "ancestry_proof": "ancestor:changelog-commit",
}


def _post_merge_state(phase: PostMergePhase) -> TLRunning:
    fields = dict(_POST_EVIDENCE)
    if phase is PostMergePhase.NOT_STARTED:
        fields = {}
    else:
        required = dict(_POST_PHASE_FIELDS)[phase]
        fields = {name: _POST_EVIDENCE[name] for name in required}
    return TLRunning(
        current_order=1,
        pending_by_order={1: (_child("child"),)},
        post_merge={"child": PostMergeState(phase, fields)},
    )


def _post_merge_events() -> tuple[object, ...]:
    receipt = PushReceipt(
        repository="repo",
        parent_branch="main",
        child_id="child",
        lane_epoch=1,
        push_intent_id="push-intent",
        push_journal_id="push-journal",
        push_receipt_id="receipt",
        expected_base_sha="parent-head",
        pushed_commit="changelog-commit",
        observed_remote_head="remote-bookkeeping",
        ancestry_proof="ancestor:changelog-commit",
    )
    return (
        MergeAdopted("child", 43, "head", "merge-journal", "repo", "main"),
        ParentBranchSynced("child", "main", "parent-head"),
        IssueClosePending("child", "issue-43", "close-intent"),
        IssueCloseConfirmed("child", "issue-43", "close-intent", "close-journal"),
        ChangelogPending("child", "changelog-intent", 0),
        ChangelogCommitted("child", "changelog-intent", "changelog-commit"),
        ParentPushPending("child", "push-intent", "parent-head", "push-journal"),
        PostMergeComplete("child", "merge-journal", "push-intent", "changelog-commit", receipt),
    )


def test_post_merge_reference_matrix_requires_every_predecessor() -> None:
    phases = (PostMergePhase.NOT_STARTED,) + tuple(phase for phase, _ in _POST_PHASE_FIELDS)
    events = _post_merge_events()

    for phase_index, phase in enumerate(phases[:-1]):
        result = transition(_post_merge_state(phase), events[phase_index])
        if phase_index == len(events) - 1:
            assert isinstance(result, TLAllMerged)
        else:
            assert result.post_merge["child"].phase is phases[phase_index + 1]
        for skipped_event in events[phase_index + 1 :]:
            with pytest.raises(IllegalTransition):
                transition(_post_merge_state(phase), skipped_event)


def test_post_merge_checkpoints_reject_missing_cumulative_evidence() -> None:
    for phase, fields in _POST_PHASE_FIELDS:
        missing = {name: _POST_EVIDENCE[name] for name in fields[:-1]}
        with pytest.raises(ValueError):
            PostMergeState(phase, missing)


def test_replaying_complete_post_merge_is_identity_idempotent() -> None:
    phase = _post_merge_state(PostMergePhase.COMPLETE)
    replayed = transition(phase, _post_merge_events()[-1])

    assert replayed is phase


def test_post_merge_evidence_is_monotonic_and_rejects_divergence() -> None:
    phase = _post_merge_state(PostMergePhase.PARENT_PUSH_PENDING)
    event = _post_merge_events()[-1]
    conflicting = replace(event, bookkeeping_commit="different-commit")

    with pytest.raises(IllegalTransition, match="evidence mismatch"):
        transition(phase, conflicting)


def _receipt() -> PushReceipt:
    return PushReceipt(
        repository="repo",
        parent_branch="main",
        child_id="child",
        lane_epoch=1,
        push_intent_id="push-intent",
        push_journal_id="push-journal",
        push_receipt_id="receipt",
        expected_base_sha="base",
        pushed_commit="commit",
        observed_remote_head="remote",
        ancestry_proof="ancestor:commit",
    )


_LANE_ALLOWED: dict[LanePhase, frozenset[str]] = {
    LanePhase.IDLE: frozenset({"reserved"}),
    LanePhase.RESERVED: frozenset({"integrating", "recovery", "parked", "abandoned"}),
    LanePhase.INTEGRATING: frozenset(
        {"integrating", "bookkeeping", "recovery", "parked", "abandoned"}
    ),
    LanePhase.BOOKKEEPING: frozenset(
        {"bookkeeping", "released", "recovery", "parked", "abandoned"}
    ),
    LanePhase.RECOVERY: frozenset({"reserved", "resolved", "parked", "abandoned"}),
    LanePhase.PARKED: frozenset({"recovery", "abandoned"}),
}


def _lane_state(phase: LanePhase) -> LaneState:
    if phase is LanePhase.IDLE:
        return LaneState("repo", "main")
    return LaneState(
        "repo",
        "main",
        phase=phase,
        child_id="child",
        lane_epoch=1,
        expected_base_sha="base",
        head_sha="head",
        merge_journal_id="merge-journal",
        push_intent_id="push-intent",
        push_journal_id="push-journal",
        changelog_commit="commit",
        last_lane_epoch=1,
    )


def _lane_events(phase: LanePhase) -> dict[str, object]:
    reserve_epoch = 2 if phase is LanePhase.RECOVERY else 1
    return {
        "reserved": LaneReserved("child", reserve_epoch, "base"),
        "integrating": LaneIntegrationStarted("child", "head"),
        "bookkeeping": LaneBookkeepingStarted(
            "child", "merge-journal", "push-intent", "push-journal", "commit", "base"
        ),
        "released": LaneReleased("child", _receipt()),
        "recovery": LaneRecoveryRequested("uncertain result"),
        "resolved": LaneRecoveryResolved("child", "head"),
        "parked": LaneParkRequested("failure", "operator gate"),
        "abandoned": LaneAbandoned("failure", "release lane"),
    }


def test_lane_reference_matrix_rejects_cross_phase_edges() -> None:
    for phase, allowed in _LANE_ALLOWED.items():
        for name, event in _lane_events(phase).items():
            if name in allowed:
                transition_lane(_lane_state(phase), event)
            else:
                with pytest.raises(IllegalTransition):
                    transition_lane(_lane_state(phase), event)


_INTEGRATION_MATRIX: dict[IntegrationLifecycle, frozenset[IntegrationTransition]] = {
    IntegrationLifecycle.RUNNING: frozenset(
        {
            IntegrationTransition.CHILDREN_MERGED,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.CHILDREN_MERGED: frozenset(
        {
            IntegrationTransition.AGGREGATE_PR_OPENED,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.AGGREGATE_PR_OPEN: frozenset(
        {
            IntegrationTransition.CODE_REVIEW_ACCEPTED,
            IntegrationTransition.REPAIR_STARTED,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.CODE_REVIEWED: frozenset(
        {
            IntegrationTransition.CODE_REVIEW_ACCEPTED,
            IntegrationTransition.BASE_INVALIDATED,
            IntegrationTransition.HEAD_INVALIDATED,
            IntegrationTransition.REPAIR_STARTED,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.READY_FOR_INTEGRATION: frozenset(
        {
            IntegrationTransition.BASE_INVALIDATED,
            IntegrationTransition.HEAD_INVALIDATED,
            IntegrationTransition.REPAIR_STARTED,
            IntegrationTransition.INTEGRATION_VALIDATED,
            IntegrationTransition.INTEGRATION_CONFLICT,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.NEEDS_BASE_REVALIDATION: frozenset(
        {
            IntegrationTransition.INTEGRATION_VALIDATED,
            IntegrationTransition.BASE_INVALIDATED,
            IntegrationTransition.INTEGRATION_CONFLICT,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.INTEGRATION_VALIDATED: frozenset(
        {
            IntegrationTransition.MERGE_STARTED,
            IntegrationTransition.BASE_INVALIDATED,
            IntegrationTransition.INTEGRATION_CONFLICT,
            IntegrationTransition.FAILED,
        }
    ),
    IntegrationLifecycle.MERGING: frozenset(
        {
            IntegrationTransition.MERGED,
            IntegrationTransition.BASE_INVALIDATED,
            IntegrationTransition.INTEGRATION_CONFLICT,
            IntegrationTransition.FAILED,
        }
    ),
    IntegrationLifecycle.MERGED: frozenset(),
    IntegrationLifecycle.REPAIRING_AGGREGATE: frozenset(
        {
            IntegrationTransition.REPAIR_COMPLETED,
            IntegrationTransition.HEAD_INVALIDATED,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.INTEGRATION_CONFLICT: frozenset(
        {
            IntegrationTransition.REPAIR_STARTED,
            IntegrationTransition.BASE_INVALIDATED,
            IntegrationTransition.FAILED,
            IntegrationTransition.PARKED,
        }
    ),
    IntegrationLifecycle.FAILED: frozenset(),
    IntegrationLifecycle.PARKED: frozenset(),
}


def test_integration_reference_matrix_rejects_every_unlisted_edge() -> None:
    for lifecycle, allowed in _INTEGRATION_MATRIX.items():
        assert allowed_integration_transitions(lifecycle) == allowed
        for event in IntegrationTransition:
            if event in allowed:
                transition_integration(IntegrationState(lifecycle), event)
            else:
                with pytest.raises(IntegrationTransitionError):
                    transition_integration(IntegrationState(lifecycle), event)


def test_head_and_base_evidence_invalidate_different_integration_paths() -> None:
    candidate = AggregateCandidate("child", 43, "head", "patch", "base")
    state = IntegrationState(
        IntegrationLifecycle.READY_FOR_INTEGRATION,
        candidate=candidate,
        code_review=CodeReviewEvidence("head", "patch", "GO", "now"),
        integration=IntegrationEvidence("base", "head", "tree", "success", "now"),
    )

    assert (
        transition_integration(state, IntegrationTransition.BASE_INVALIDATED).lifecycle
        is IntegrationLifecycle.NEEDS_BASE_REVALIDATION
    )
    assert (
        transition_integration(state, IntegrationTransition.HEAD_INVALIDATED).lifecycle
        is IntegrationLifecycle.REPAIRING_AGGREGATE
    )


def test_heartbeat_model_step_is_a_state_version_noop() -> None:
    state = RunState(
        version=1,
        revision=0,
        run_id="model-run",
        fsm=FSMState(TLPhase.TLPlanning, ()),
        slices={},
        budgets=BudgetLedger(0, 0),
        gates=(),
        events=EventCursor(0),
        recursive_fsm=TLPlanning(),
    )

    heartbeat = step(state, HeartbeatTick(10.0))

    assert heartbeat.state is state
    assert heartbeat.state.state_version == state.state_version
