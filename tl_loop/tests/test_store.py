"""Checkpoint creation, reconstruction, and corruption checks."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.recovery import begin_recovery
from tl_loop.ordered import ChildRecoverySummary, IntegrationLifecycle, SubTLLifecycle
from tl_loop.state.schema import (
    BudgetLedger,
    DeadlineLedger,
    FSMState,
    GateStatus,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    SliceState,
    SliceStatus,
    SuspendedDependencyState,
    Verdict,
)
from tl_loop.state.store import (
    CorruptCheckpoint,
    RunStore,
    WorktreeClaimError,
    create,
    load,
    resume,
)


def test_mid_wave_resume_reconstructs_exact_local_state(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    fsm = FSMState(TLPhase.TLWaiting, ("in-review", "spawned"))
    slices = {
        "merged": _slice("merged", SliceStatus.MERGED, "src/merged.py"),
        "in-review": _slice("in-review", SliceStatus.IN_REVIEW, "src/review.py"),
        "spawned": _slice("spawned", SliceStatus.SPAWNED, "src/spawned.py"),
    }
    budgets = BudgetLedger(tokens=321, wall_seconds=45)

    checkpointed = store.checkpoint(fsm, slices, budgets, offset=17)
    loaded = load(store.path)
    resumed = resume("run-1", root_dir=tmp_path)

    assert checkpointed.fsm == fsm
    assert loaded.fsm == fsm
    assert dict(loaded.slices) == slices
    assert resumed.fsm == fsm
    assert dict(resumed.slices) == slices
    assert resumed.budgets == budgets
    assert resumed.offset == 17
    assert loaded.revision == 1


def test_legacy_checkpoint_defaults_new_review_state(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("in-review",)),
        {"in-review": _slice("in-review", SliceStatus.IN_REVIEW, "src/review.py")},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
    )
    document = json.loads(store.path.read_text(encoding="utf-8"))
    record = document["slices"]["in-review"]
    for key in ("review_findings", "ci_state", "reviewer_attempt", "repair_attempts"):
        record.pop(key)
    store.path.write_text(json.dumps(document), encoding="utf-8")

    restored = load(store.path).slices["in-review"]
    assert restored.review_findings == {}
    assert restored.ci_state == {}
    assert restored.reviewer_attempt == {}
    assert restored.repair_attempts == 0


def test_dependency_recovery_suspension_round_trips(tmp_path: Path) -> None:
    store = RunStore("recovery-run", tmp_path)
    create("recovery-run", {}, root_dir=tmp_path)
    dependent = replace(
        _slice("dependent", SliceStatus.PENDING, "src/dependent.py"),
        depends_on=("blocker",),
        recovery=None,
        suspended_dependency=SuspendedDependencyState(
            blocked_by="blocker",
            prior_status=SliceStatus.PENDING,
            recovery_generation=1,
        ),
    )
    blocker = replace(
        _slice("blocker", SliceStatus.SPAWNED, "src/blocker.py"),
        recovery=begin_recovery(
            cause="external_dependency",
            owner_run_id="recovery-run",
            slice_attempt=1,
            owner_agent_id="agent",
            entered_at=1,
        ),
    )

    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("blocker",)),
        {"blocker": blocker, "dependent": dependent},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
    )

    restored = load(store.path)
    assert restored.slices["dependent"].suspended_dependency == dependent.suspended_dependency
    assert restored.slices["blocker"].recovery == blocker.recovery


def test_deadline_ledger_round_trips(tmp_path: Path) -> None:
    store = RunStore("deadline-run", tmp_path)
    create("deadline-run", {}, root_dir=tmp_path)
    value = replace(
        _slice("leaf", SliceStatus.SPAWNED, "src/leaf.py"),
        dispatch_intent_id="intent",
        dispatch_agent_id="agent-leaf",
        dispatch_authoritative_event_seq=1,
        deadline_ledger=DeadlineLedger(
            execution_deadline_at=100.0,
            recovery_deadline_at=200.0,
            run_deadline_at=300.0,
            suspended_at=50.0,
            execution_seconds=40.0,
            recovery_wait_seconds=10.0,
        ),
    )
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("leaf",)),
        {"leaf": value},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
    )

    assert load(store.path).slices["leaf"].deadline_ledger == value.deadline_ledger


def test_nested_child_recovery_projection_round_trips(tmp_path: Path) -> None:
    store = RunStore("parent", tmp_path)
    create("parent", {}, root_dir=tmp_path)
    summary = ChildRecoverySummary(
        owner_run_id="child",
        child_path=("parent", "child", "leaf"),
        slice_id="leaf",
        cause="external_dependency",
        recovery_round=1,
        next_probe_at=10.0,
    )
    store.checkpoint(
        FSMState(TLPhase.TLPlanning, ()),
        {},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
        integration=IntegrationRuntimeState(
            sub_tl_states={"child": SubTLLifecycle.HUMAN_GATE},
            sub_tl_recovery={"child": summary},
        ),
    )

    restored = load(store.path)
    assert restored.integration.sub_tl_states["child"] is SubTLLifecycle.HUMAN_GATE
    assert restored.integration.sub_tl_recovery["child"] == summary


def test_ordered_state_round_trips_and_resume_preserves_progress(tmp_path: Path) -> None:
    store = RunStore("ordered-run", tmp_path)
    create("ordered-run", {}, root_dir=tmp_path)
    stages = (
        OrderedStageState(1, ("auth", "sessions")),
        OrderedStageState(2, ("docs",)),
    )
    integration = IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        sub_tl_states={
            "auth": IntegrationLifecycle.MERGED,
            "sessions": IntegrationLifecycle.MERGED,
        },
        aggregate_pr_number=42,
        aggregate_head_sha="head-42",
        aggregate_patch_digest="patch-42",
        aggregate_original_base_sha="base-1",
        integration_owner_id="tl/root",
        head_sha="head-42",
        patch_digest="patch-42",
        validated_base_sha="base-0",
        merge_tree_sha="tree-42",
        ci_status="success",
        merge_attempts=1,
        base_revalidation_count=2,
        stage_verification="passed",
        candidates={
            "auth": IntegrationCandidateState(
                lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
                aggregate_pr_number=42,
                aggregate_head_sha="head-auth",
                aggregate_patch_digest="patch-auth",
                aggregate_original_base_sha="base-1",
                integration_owner_id="tl/auth",
                head_sha="head-auth",
                patch_digest="patch-auth",
                validated_base_sha="base-1",
                merge_tree_sha="tree-auth",
                integration_evidence_at="2026-01-01T00:00:00Z",
                ci_status="success",
                merge_attempts=1,
                stage_verification="passed",
            ),
            "sessions": IntegrationCandidateState(
                lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
                aggregate_pr_number=43,
                aggregate_head_sha="head-sessions",
                aggregate_patch_digest="patch-sessions",
                aggregate_original_base_sha="base-1",
                integration_owner_id="tl/sessions",
                head_sha="head-sessions",
                patch_digest="patch-sessions",
                validated_base_sha="base-1",
                merge_tree_sha="tree-sessions",
                integration_evidence_at="2026-01-01T00:00:00Z",
                ci_status="success",
                merge_attempts=1,
                stage_verification="passed",
            ),
        },
    )

    state = store.set_ordered_state(2, stages, integration)
    resumed = store.resume()

    assert state.current_order == 2
    assert state.ordered_stages == stages
    assert state.integration == integration
    assert resumed.current_order == 2
    assert resumed.ordered_stages == stages
    assert resumed.integration == integration
    assert resumed.integration.candidates["auth"].head_sha == "head-auth"
    assert resumed.integration.candidates["sessions"].head_sha == "head-sessions"


def test_legacy_run_defaults_ordered_state_without_rewrite(tmp_path: Path) -> None:
    store = RunStore("legacy-run", tmp_path)
    create("legacy-run", {}, root_dir=tmp_path)
    document = json.loads(store.path.read_text(encoding="utf-8"))

    restored = store.load()

    assert restored.current_order == 1
    assert restored.ordered_stages == ()
    assert restored.integration.lifecycle is IntegrationLifecycle.RUNNING
    assert "ordered_stages" not in document


def test_answer_gate_requires_an_existing_gate(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)

    with pytest.raises(ValueError, match="does not exist"):
        store.answer_gate("missing", GateStatus.APPROVED)

    store.set_gate("review")
    answered = store.answer_gate("review", GateStatus.APPROVED)
    assert answered.gates[0].status is GateStatus.APPROVED


def test_load_rejects_waiting_slice_with_terminal_status(tmp_path: Path) -> None:
    store = RunStore("run-1", tmp_path)
    create("run-1", {}, root_dir=tmp_path)
    store.checkpoint(
        FSMState(TLPhase.TLWaiting, ("merged",)),
        {"merged": _slice("merged", SliceStatus.SPAWNED, "src/merged.py")},
        BudgetLedger(tokens=0, wall_seconds=0),
        offset=0,
    )
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["slices"]["merged"]["status"] = SliceStatus.MERGED.value
    store.path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CorruptCheckpoint, match="waiting set is inconsistent"):
        load(store.path)

    assert document["fsm"] == {"phase": TLPhase.TLWaiting.value, "waiting": ["merged"]}


def _slice(slice_id: str, status: SliceStatus, path: str) -> SliceState:
    return SliceState(
        id=slice_id,
        status=status,
        paths=(path,),
        depends_on=(),
        base_ref="main",
        test_plan=("just tl-loop-test",),
        agent_type="codex",
        model="gpt-5",
        branch=f"task/{slice_id}",
        worktree=f".worktrees/{slice_id}",
        pr_number=42 if status is SliceStatus.IN_REVIEW else None,
        reviewed_head="abc123" if status in {SliceStatus.IN_REVIEW, SliceStatus.MERGED} else None,
        review_findings={
            "abc123": (
                {
                    "severity": "info",
                    "path": path,
                    "rationale": "covered",
                },
            )
        },
        ci_state={"abc123": "success"},
        reviewer_attempt={"abc123": 2},
        repair_attempts=3,
        attempts=1,
        verdict=Verdict.GO if status is SliceStatus.MERGED else None,
        dispatch_intent_id="store-intent-1" if status is SliceStatus.SPAWNED else None,
        dispatch_agent_id="agent-spawned" if status is SliceStatus.SPAWNED else None,
        dispatch_authoritative_event_seq=1 if status is SliceStatus.SPAWNED else None,
    )


def test_live_run_cannot_claim_an_owned_worktree_twice(tmp_path: Path) -> None:
    worktree = str(tmp_path / "shared-worktree")
    create("first", {"owner_branch": "main", "owner_worktree": worktree}, root_dir=tmp_path)

    with pytest.raises(WorktreeClaimError, match="already claimed"):
        create(
            "second", {"owner_branch": "main.second", "owner_worktree": worktree}, root_dir=tmp_path
        )
