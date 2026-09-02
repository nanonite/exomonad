"""Operator read-model projection and body-leakage coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

from tl_loop.events.envelope import project
from tl_loop.events.reader import SequenceStatus
from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.lane import LanePhase, LaneState
from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.recovery import begin_recovery
from tl_loop.fsm.scope import TLAllMerged, TLRunning
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.read_model import GateReadModel, project_read_model
from tl_loop.state.schema import (
    ActionKind,
    ActionPhase,
    ActionState,
    BudgetCharge,
    BudgetLedger,
    EventCursor,
    FSMState,
    GateState,
    GateStatus,
    GoalState,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    ObservationProvenance,
    OrderedStageState,
    ParkCause,
    RunState,
    SliceState,
    SliceStatus,
    Verdict,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ledger_projection_events.json"


def test_projection_carries_cursor_and_bounded_operator_evidence() -> None:
    state = _state()
    raw_events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    events = tuple(project(raw_event) for raw_event in raw_events)

    model = project_read_model(
        state,
        events,
        sequence_status=SequenceStatus.COMPLETE,
        recent_transition_limit=3,
    )
    document = model.to_document()
    head = next(head for head in model.slices["task-a"].heads if head.head_sha == "bbb222")

    assert model.ledger_cursor == 112
    assert model.ledger_sequence_status == "complete"
    assert [transition.run_seq for transition in model.recent_transitions] == [110, 111, 112]
    assert head.head_sha == "bbb222"
    assert head.review_kind == "merge_ready"
    assert head.review_state == "changes_requested"
    assert head.review_verdict == "GO"
    assert head.review_finding_count == 1
    assert head.ci_status == "success"
    assert head.reviewer_attempt == 2
    assert model.park_causes == {"task-a": "review_stuck"}
    assert model.gates == (GateReadModel("review", "pending"),)
    assert model.controller_started_at == 100.0
    assert model.elapsed_seconds is not None and model.elapsed_seconds >= 0.0
    assert model.last_authoritative_event_seq == 112
    assert model.last_observed_progress_at == 150.0
    assert model.slices["task-a"].task_started_at == 90.0
    budgets = cast(dict[str, object], document["budgets"])
    assert budgets["tokens"] == 321


def test_projection_does_not_leak_event_or_finding_bodies() -> None:
    state = _state()
    raw_events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    events = tuple(project(raw_event) for raw_event in raw_events)

    encoded = json.dumps(project_read_model(state, events).to_document(), sort_keys=True)

    assert "[MERGE READY]" not in encoded
    assert "tests failed" not in encoded
    assert "private agent body" not in encoded
    assert "rationale" not in encoded


def test_projection_ignores_events_after_state_cursor() -> None:
    state = _state()
    raw_events = cast(list[dict[str, object]], json.loads(FIXTURE.read_text(encoding="utf-8")))
    events = tuple(project(raw_event) for raw_event in raw_events)

    model = project_read_model(state, events, recent_transition_limit=100)

    assert model.recent_transitions
    assert max(transition.run_seq for transition in model.recent_transitions) == 112


def test_projection_exposes_ordered_progress_and_next_transition() -> None:
    state = _state()
    state = RunState(
        **{
            **state.__dict__,
            "current_order": 1,
            "ordered_stages": (OrderedStageState(1, ("task-a",)),),
            "integration": IntegrationRuntimeState(
                lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
                sub_tl_states={"task-a": IntegrationLifecycle.READY_FOR_INTEGRATION},
                aggregate_pr_number=101,
                aggregate_head_sha="bbb222",
                integration_owner_id="aggregate-owner",
                head_sha="bbb222",
                validated_base_sha="base-1",
                merge_tree_sha="tree-1",
                ci_status="success",
                merge_attempts=3,
                base_revalidation_count=2,
                stage_verification="passed",
                candidates={
                    "task-a": IntegrationCandidateState(
                        lifecycle=IntegrationLifecycle.INTEGRATION_CONFLICT,
                        aggregate_pr_number=202,
                        aggregate_head_sha="candidate-head",
                        aggregate_patch_digest="candidate-patch",
                        head_sha="candidate-head",
                        patch_digest="candidate-patch",
                        validated_base_sha="candidate-base",
                        merge_tree_sha="candidate-tree",
                        integration_evidence_at="2026-08-15T12:00:00Z",
                        ci_status="failure",
                        merge_attempts=4,
                        base_revalidation_count=7,
                        stage_verification="failed",
                        integration_owner_id="candidate-owner",
                        integration_owner_run_id="candidate-run",
                        integration_owner_branch="main.candidate",
                        integration_owner_worktree=".exo/worktrees/candidate",
                    )
                },
            ),
        }
    )

    document = project_read_model(state).to_document()

    assert document["schema_version"] == 2
    assert document["current_order"] == 1
    stage = cast(list[dict[str, object]], document["ordered_stages"])[0]
    sub_tl = cast(list[dict[str, object]], stage["sub_tls"])[0]
    assert sub_tl["lifecycle"] == "INTEGRATION_CONFLICT"
    assert sub_tl["aggregate_pr_number"] == 202
    assert sub_tl["head_sha"] == "candidate-head"
    assert sub_tl["patch_digest"] == "candidate-patch"
    assert sub_tl["validated_base_sha"] == "candidate-base"
    assert sub_tl["merge_tree_sha"] == "candidate-tree"
    assert sub_tl["integration_ci"] == "failure"
    assert sub_tl["revalidation_count"] == 7
    assert sub_tl["repair_count"] == 1
    assert sub_tl["stage_verification"] == "failed"
    assert sub_tl["owner_id"] == "candidate-owner"
    integration = cast(dict[str, object], document["integration"])
    assert integration["merge_tree_sha"] == "tree-1"
    assert integration["base_revalidation_count"] == 2
    assert document["next_transition"] == "answer_gate:review"


def test_projection_exposes_recursive_scope_and_durable_blocking_details() -> None:
    worker = ChildRecord(
        "worker-a",
        ChildKind.WORKER,
        manifest_node_id="root/worker-a",
        manifest_revision=3,
    )
    sub_tl = ChildRecord(
        "sub-tl-a",
        ChildKind.SUB_TL,
        dispatch_intent_id="dispatch-sub-tl-a",
        invocation_id="invocation-sub-tl-a",
        lane_id="repo/main",
        manifest_node_id="root/sub-tl-a",
        manifest_revision=3,
    )
    recursive = TLRunning(
        current_order=1,
        pending_by_order={1: (sub_tl,)},
        scope_path=("root", "sub-tl-a"),
        plan_digest="recursive-digest",
        parallel_pending=(worker,),
        dispatch_intents={"sub-tl-a": "dispatch-sub-tl-a"},
        lane_bindings={"sub-tl-a": "repo/main"},
    )
    post_merge = PostMergeState(
        PostMergePhase.REMOTE_MERGE_ADOPTED,
        {
            "child_id": "task-a",
            "repository": "org/repo",
            "parent_branch": "main",
            "pr_number": "101",
            "head_sha": "bbb222",
            "merge_journal_id": "merge-journal",
            "lane_epoch": "4",
        },
    )
    recovery = begin_recovery(
        cause="unknown merge result",
        owner_run_id="run-1",
        slice_attempt=2,
        owner_agent_id="agent-a",
        invocation_generation=3,
        plan_revision=3,
        evidence={"head_sha": "bbb222", "private_reason": "must not escape"},
        entered_at=12.0,
    )
    lane = LaneState(
        repository="org/repo",
        parent_branch="main",
        phase=LanePhase.RECOVERY,
        child_id="task-a",
        lane_epoch=4,
        expected_base_sha="base-1",
        merge_journal_id="merge-journal",
    )
    slice_state = replace(
        _state().slices["task-a"],
        status=SliceStatus.IN_REVIEW,
        park_cause=None,
        park_issue_id=None,
        action=ActionState(
            ActionKind.MERGE,
            ActionPhase.UNKNOWN,
            state_version=7,
            intent_id="merge-intent",
            head_sha="bbb222",
            attempt=2,
            contract_digest="contract",
        ),
        post_merge=post_merge,
        recovery=recovery,
        manifest_node_id="root/task-a",
        manifest_revision=3,
    )
    state = replace(
        _state(),
        parent_run_id="root",
        recursive_fsm=recursive,
        slices={"task-a": slice_state},
        integration=replace(
            _state().integration,
            lanes={"org/repo:main": lane},
        ),
    )

    document = project_read_model(state).to_document()
    scope = cast(dict[str, object], document["scope"])
    slice_document = cast(dict[str, object], cast(dict[str, object], document["slices"])["task-a"])
    post_document = cast(dict[str, object], slice_document["post_merge"])
    recovery_document = cast(dict[str, object], slice_document["recovery"])
    lane_document = cast(
        dict[str, object], cast(dict[str, object], document["lanes"])["org/repo:main"]
    )

    assert scope["scope_path"] == ["root", "sub-tl-a"]
    assert scope["role"] == "non_root"
    assert scope["plan_digest"] == "recursive-digest"
    assert scope["phase"] == "tl_running"
    assert scope["current_order"] == 1
    assert scope["active_barrier"] == ["worker-a", "sub-tl-a"]
    assert scope["parallel_pending"] == ["worker-a"]
    assert cast(dict[str, object], scope["pending_by_order"])["1"] == ["sub-tl-a"]
    assert slice_document["manifest_node_id"] == "root/task-a"
    assert slice_document["authority"] == "ambiguous"
    assert slice_document["blocking_state"] == "unknown_action:merge-intent"
    assert cast(dict[str, object], document["blocking"]) == {
        "task-a": "unknown_action:merge-intent"
    }
    assert document["recursive_phase"] == "tl_running"
    assert document["scope_role"] == "non_root"
    assert slice_document["next_transition"] == "reconcile_action:merge-intent"
    action_document = cast(dict[str, object], slice_document["action"])
    assert action_document["kind"] == "merge"
    assert action_document["phase"] == "unknown"
    assert action_document["intent_id"] == "merge-intent"
    direct_integration = cast(dict[str, object], slice_document["integration"])
    assert direct_integration["head_sha"] == "bbb222"
    assert direct_integration["freshness"] == "observed"
    assert post_document["phase"] == "remote_merge_adopted"
    assert post_document["next_transition"] == "sync_parent_branch"
    assert recovery_document["phase"] == "diagnosing"
    assert recovery_document["owner_run_id"] == "run-1"
    assert "private_reason" not in cast(str, json.dumps(recovery_document))
    assert lane_document["phase"] == "recovery"
    assert lane_document["child_id"] == "task-a"
    assert lane_document["next_transition"] == "reconcile_or_gate_lane"
    child_records = cast(dict[str, object], scope["child_records"])
    assert cast(dict[str, object], child_records["sub-tl-a"])["kind"] == "sub_tl"
    assert scope["dispatch_intents"] == {"sub-tl-a": "dispatch-sub-tl-a"}
    replay = cast(dict[str, object], document["replay"])
    assert replay["cursor"] == 112
    assert replay["authority"] == "consumed_ledger_prefix"
    assert replay["state_version"] == state.state_version


def test_projection_distinguishes_stale_review_and_recursive_terminal_scope() -> None:
    base = _state()
    state = replace(
        base,
        gates=(),
        recursive_fsm=TLAllMerged(scope_path=("root",), plan_digest="done-digest"),
        slices={
            "task-a": replace(
                base.slices["task-a"],
                status=SliceStatus.IN_REVIEW,
                review_validation_required=True,
                park_cause=None,
                park_issue_id=None,
            )
        },
    )

    document = project_read_model(state).to_document()
    slice_document = cast(dict[str, object], cast(dict[str, object], document["slices"])["task-a"])
    scope = cast(dict[str, object], document["scope"])

    assert slice_document["authority"] == "stale"
    assert slice_document["blocking_state"] == "review_revalidation_required"
    assert slice_document["waiting_reason"] == "revalidate exact-head review"
    assert scope["phase"] == "tl_all_merged"
    assert scope["next_transition"] == "finalize_scope"


def test_projection_reports_observed_unknown_and_failed_states() -> None:
    base = _state()
    observed = replace(
        base.slices["task-a"],
        status=SliceStatus.IN_REVIEW,
        park_cause=None,
        park_issue_id=None,
        observation_provenance=ObservationProvenance(
            source="watcher",
            observed_at="2026-08-30T00:00:00Z",
            event_seq=12,
            snapshot_id="snapshot-12",
        ),
    )
    unknown = replace(
        observed,
        observation_provenance=None,
        reviewed_head=None,
        verdict=None,
    )
    failed = replace(unknown, status=SliceStatus.FAILED)
    for slice_state, expected in (
        (observed, "observed"),
        (unknown, "unknown"),
        (failed, "failed"),
    ):
        model = project_read_model(
            replace(base, gates=(), slices={"task-a": slice_state}),
        )
        assert model.slices["task-a"].authority == expected


def _state() -> RunState:
    return RunState(
        version=1,
        revision=4,
        run_id="run-1",
        fsm=FSMState(TLPhase.TLWaiting, ("task-a",)),
        slices={
            "task-a": SliceState(
                id="task-a",
                status=SliceStatus.PARKED,
                paths=("src/a.py",),
                depends_on=(),
                base_ref="main",
                test_plan=("just test",),
                agent_type="codex",
                model="gpt-5",
                branch="task-a",
                worktree=".worktrees/task-a",
                pr_number=101,
                reviewed_head="bbb222",
                attempts=2,
                verdict=Verdict.GO,
                review_findings={
                    "bbb222": (
                        {
                            "severity": "blocking",
                            "path": "src/a.py",
                            "rationale": "private agent body",
                        },
                    )
                },
                ci_state={"bbb222": "success"},
                reviewer_attempt={"bbb222": 2},
                repair_attempts=1,
                park_cause=ParkCause.REVIEW_STUCK,
                park_issue_id=404,
                dispatch_started_at=90.0,
            )
        },
        budgets=BudgetLedger(
            tokens=321,
            wall_seconds=45,
            role_spent={"worker": 321},
            harness_spent={"codex": 321},
            charges=(
                BudgetCharge(
                    slice_id="task-a",
                    attempt=1,
                    role="worker",
                    harness="codex",
                    estimated_tokens=300,
                    actual=321,
                    delta_tokens=21,
                    warning=False,
                    reconciled=True,
                ),
            ),
        ),
        gates=(GateState("review", GateStatus.PENDING),),
        events=EventCursor(last_consumed_offset=112),
        goals=GoalState(
            controller_started_at=100.0,
            last_authoritative_event_seq=112,
            last_progress_at=150.0,
        ),
    )
