"""Read-only planning and protection rules for active plan revisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLRunning
from tl_loop.loop.driver import WorkPlan, _canonical_planning_from_manifest, _manifest_plan
from tl_loop.state.lock import LockTimeout, RunLock
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.plan_revision import PlanRevisionError, analyze_plan_revision
from tl_loop.state.plan_revision_apply import apply_plan_revision
from tl_loop.state.plan_revision_transaction import (
    commit_prepared_revision,
    prepare_revision_transaction,
    recover_plan_revision,
)
from tl_loop.state.schema import OrderedStageState, SliceStatus
from tl_loop.state.store import RunStore, create


def _document(*, issue_id: int | None = None, include_issue_id: bool = True) -> dict[str, object]:
    leaf: dict[str, object] = {"name": "leaf", "task": "implement the change"}
    if include_issue_id:
        leaf["chainlink_issue_id"] = issue_id
    return {
        "run_id": "root",
        "plan": {
            "sub_tls": [
                {
                    "name": "stage",
                    "order": 1,
                    "plan": {"leaves": [leaf]},
                }
            ]
        },
    }


def _project(tmp_path: Path, document: dict[str, object]) -> tuple[Path, RunStore]:
    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    snapshot = json.dumps(document, sort_keys=True).encode()
    (state_root / "plan.snapshot").write_bytes(snapshot)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(snapshot).hexdigest() + "\n",
        encoding="ascii",
    )
    (state_root / "plan.json").write_bytes(snapshot)
    plan = WorkPlan.from_mapping(document["plan"])
    manifest = build_plan_manifest(_manifest_plan(plan), scope_id="root", owned_branch="main")
    create("root", {"plan_manifest": manifest.to_document()}, root_dir=state_root)
    store = RunStore("root", state_root)
    state = store.load()
    store.checkpoint(
        _canonical_planning_from_manifest(manifest, state.slices),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=manifest,
    )
    return state_root, store


def _write_candidate(state_root: Path, document: dict[str, object]) -> bytes:
    candidate = json.dumps(document, sort_keys=True).encode()
    (state_root / "plan.json").write_bytes(candidate)
    return candidate


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_future_leaf_issue_binding_is_a_read_only_revision(tmp_path: Path) -> None:
    state_root, _ = _project(tmp_path, _document())
    candidate = _write_candidate(state_root, _document(issue_id=812))
    before = _tree_bytes(state_root)

    analysis = analyze_plan_revision(tmp_path)

    assert analysis.can_apply, analysis.blockers
    assert (
        analysis.previous_plan_sha256
        == hashlib.sha256((state_root / "plan.snapshot").read_bytes()).hexdigest()
    )
    assert analysis.candidate_plan_sha256 == hashlib.sha256(candidate).hexdigest()
    assert {scope.scope_id for scope in analysis.scopes} == {
        "root",
        "root/sub_tl/stage",
    }
    assert any(
        change.action == "updated" and "chainlink_issue_id" in change.fields
        for change in analysis.changes
    )
    assert "implement the change" not in json.dumps(analysis.to_document())
    assert _tree_bytes(state_root) == before


def test_noop_candidate_does_not_invent_manifest_revisions(tmp_path: Path) -> None:
    _project(tmp_path, _document())

    analysis = analyze_plan_revision(tmp_path)

    assert analysis.can_apply
    assert analysis.scopes == ()
    assert analysis.changes == ()


def test_changed_identity_of_attempted_parent_stage_is_blocked(tmp_path: Path) -> None:
    state_root, store = _project(tmp_path, _document())
    state = store.load()
    slices = dict(state.slices)
    slices["stage"] = replace(slices["stage"], attempts=1)
    store.checkpoint(
        state.fsm,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        state_version=state.state_version,
    )
    _write_candidate(state_root, _document(issue_id=812))

    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply
    assert any(
        blocker.code == "protected_node_mutation" and blocker.node_id == "root/sub_tl/stage"
        for blocker in analysis.blockers
    )


def test_materialized_child_scope_protects_parent_manifest_identity(tmp_path: Path) -> None:
    state_root, store = _project(tmp_path, _document())
    parent_manifest = store.load().plan_manifest
    assert parent_manifest is not None
    child_manifest = parent_manifest.child_manifests["root/sub_tl/stage"]
    create(
        "stage",
        {"plan_manifest": child_manifest.to_document(), "parent_run_id": "root"},
        root_dir=state_root / "root",
    )
    _write_candidate(state_root, _document(issue_id=812))

    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply
    assert any(
        blocker.code == "protected_node_mutation" and blocker.node_id == "root/sub_tl/stage"
        for blocker in analysis.blockers
    )


def test_candidate_must_keep_explicit_issue_binding_key(tmp_path: Path) -> None:
    state_root, _ = _project(tmp_path, _document())
    _write_candidate(state_root, _document(include_issue_id=False))

    with pytest.raises(PlanRevisionError, match="chainlink_issue_id is required") as error:
        analyze_plan_revision(tmp_path)

    assert error.value.code == "plan_invalid"


def test_apply_updates_snapshot_manifest_runtime_and_audit(tmp_path: Path) -> None:
    state_root, store = _project(tmp_path, _document())
    candidate = _write_candidate(state_root, _document(issue_id=812))

    result = apply_plan_revision(
        tmp_path,
        "Bind issue IDs to the remaining planned slices",
        actor="operator",
    )

    state = store.load()
    root_manifest = state.plan_manifest
    assert result.applied
    assert result.audit_record is not None
    assert result.audit_record["reason"] == "Bind issue IDs to the remaining planned slices"
    assert (state_root / "plan.snapshot").read_bytes() == candidate
    assert (state_root / "plan.snapshot.sha256").read_text("ascii").strip() == hashlib.sha256(
        candidate
    ).hexdigest()
    assert root_manifest is not None
    child = root_manifest.child_manifests["root/sub_tl/stage"]
    assert child.node("root/sub_tl/stage/leaf/leaf").chainlink_issue_id == 812
    assert state.revision == 2
    assert state.recursive_fsm is not None
    assert state.recursive_fsm.plan_digest == root_manifest.digest
    audit_lines = (state_root / "plan-revisions.jsonl").read_text("utf-8").splitlines()
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["transition_id"] == result.audit_record["transition_id"]


def test_apply_recovers_interruption_without_duplicate_audit(tmp_path: Path) -> None:
    state_root, _store = _project(tmp_path, _document())
    _write_candidate(state_root, _document(issue_id=812))

    def interrupt_after_first(_index: int, _target: str) -> None:
        raise RuntimeError("simulated process interruption")

    result = apply_plan_revision(
        tmp_path,
        "Recover an interrupted plan revision",
        actor="operator",
        after_target=interrupt_after_first,
    )

    audit_lines = (state_root / "plan-revisions.jsonl").read_text("utf-8").splitlines()
    assert result.applied
    assert result.recovered_during_apply
    assert len(audit_lines) == 1
    assert json.loads(audit_lines[0])["transition_id"] == result.audit_record["transition_id"]
    assert not (state_root / "plan-revision-transition.json").exists()


def test_apply_times_out_while_controller_lifecycle_lock_is_held(tmp_path: Path) -> None:
    state_root, _store = _project(tmp_path, _document())
    _write_candidate(state_root, _document(issue_id=812))
    lock_path = state_root / "controller-lifecycle.lock"

    with RunLock(lock_path), pytest.raises(LockTimeout):
        apply_plan_revision(
            tmp_path,
            "Bind issue IDs to the remaining planned slices",
            actor="operator",
            lock_timeout=0.02,
        )

    assert not (state_root / "plan-revisions.jsonl").exists()


def test_interrupted_transition_recovers_all_participants(tmp_path: Path) -> None:
    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    first = state_root / "plan.snapshot"
    second = state_root / "plan.snapshot.sha256"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    transition_id = "a" * 32
    audit = {"transition_id": transition_id, "reason": "test recovery"}
    prepared = prepare_revision_transaction(
        tmp_path,
        {second: b"new-second", first: b"new-first"},
        audit,
        transition_id=transition_id,
    )

    def interrupt_after_first(index: int, _target: str) -> None:
        if index == 0:
            raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        commit_prepared_revision(prepared, after_target=interrupt_after_first)
    assert first.read_bytes() == b"new-first"
    assert second.read_bytes() == b"old-second"
    assert recover_plan_revision(tmp_path)
    assert first.read_bytes() == b"new-first"
    assert second.read_bytes() == b"new-second"
    assert not (state_root / "plan-revision-transition.json").exists()
    assert not recover_plan_revision(tmp_path)


def test_prepare_refuses_changed_precondition_without_writes(tmp_path: Path) -> None:
    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    snapshot = state_root / "plan.snapshot"
    snapshot.write_bytes(b"operator edit")
    transition_id = "b" * 32

    with pytest.raises(PlanRevisionError, match="changed while preparing"):
        prepare_revision_transaction(
            tmp_path,
            {snapshot: b"stale candidate"},
            {"transition_id": transition_id},
            transition_id=transition_id,
            expected_before={snapshot: hashlib.sha256(b"original").hexdigest()},
        )

    assert snapshot.read_bytes() == b"operator edit"
    assert not (state_root / "plan-revision-transition.json").exists()


def test_apply_rebases_running_scope_and_preserves_completed_stage_evidence(
    tmp_path: Path,
) -> None:
    original = {
        "run_id": "root",
        "plan": {
            "sub_tls": [
                {"name": "stage-one", "order": 1, "plan": {"leaves": []}},
                {
                    "name": "stage-two",
                    "order": 2,
                    "plan": {
                        "leaves": [
                            {"name": "leaf", "task": "future work", "chainlink_issue_id": None}
                        ]
                    },
                },
            ]
        },
    }
    state_root, store = _project(tmp_path, original)
    state = store.load()
    manifest = state.plan_manifest
    assert manifest is not None
    planning = _canonical_planning_from_manifest(manifest, state.slices)
    records = {
        record.child_id: record
        for _order, children in planning.ordered_children
        for record in children
    }
    completed_state = PostMergeState(
        PostMergePhase.COMPLETE,
        {
            "child_id": "stage-one",
            "repository": "owner/repo",
            "parent_branch": "main",
            "pr_number": "45",
            "head_sha": "a" * 40,
            "merge_journal_id": "merge-45",
            "lane_epoch": "epoch-1",
            "parent_commit_sha": "b" * 40,
            "issue_close_outcome": "not_applicable",
            "changelog_outcome": "not_applicable",
        },
    )
    running = TLRunning(
        current_order=2,
        pending_by_order={2: (records["stage-two"],)},
        scope_path=("root",),
        plan_digest=manifest.digest or "",
        completed_children={"stage-one": records["stage-one"]},
        post_merge={
            "stage-one": completed_state,
            "stage-two": PostMergeState(PostMergePhase.NOT_STARTED),
        },
        evidence={"stage-one:aggregate_pr": "45"},
    )
    slices = dict(state.slices)
    slices["stage-one"] = replace(
        slices["stage-one"],
        status=SliceStatus.MERGED,
        post_merge=completed_state,
    )
    store.checkpoint(
        running,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=2,
        ordered_stages=tuple(
            OrderedStageState(order, tuple(manifest.node(node_id).name for node_id in node_ids))
            for order, node_ids in manifest.ordered_stages
        ),
        integration=state.integration,
        plan_manifest=manifest,
    )
    revised = {
        **original,
        "plan": {
            "sub_tls": [
                original["plan"]["sub_tls"][0],
                {
                    **original["plan"]["sub_tls"][1],
                    "plan": {
                        "leaves": [
                            {
                                "name": "leaf",
                                "task": "future work",
                                "chainlink_issue_id": 812,
                            }
                        ]
                    },
                },
            ]
        },
    }
    _write_candidate(state_root, revised)

    result = apply_plan_revision(tmp_path, "Bind the future leaf", actor="operator")

    updated = store.load()
    phase = updated.recursive_fsm
    assert result.applied
    assert isinstance(phase, TLRunning)
    assert phase.current_order == 2
    assert phase.completed_children["stage-one"].manifest_revision == 2
    assert phase.evidence["stage-one:aggregate_pr"] == "45"
    assert phase.post_merge["stage-one"] == completed_state
    assert phase.pending_by_order[2][0].child_id == "stage-two"


def test_removed_scopes_are_reported_and_blocked(tmp_path: Path) -> None:
    state_root, _ = _project(tmp_path, _document())
    candidate = {"run_id": "root", "plan": {"sub_tls": []}}
    _write_candidate(state_root, candidate)

    analysis = analyze_plan_revision(tmp_path)

    assert not analysis.can_apply
    assert any(change.action == "removed" for change in analysis.changes)
    assert any(scope.candidate_revision is None for scope in analysis.scopes)
    assert any(blocker.code == "node_removal" for blocker in analysis.blockers)


def test_unversioned_manifest_drift_is_not_adopted(tmp_path: Path) -> None:
    _, store = _project(tmp_path, _document())
    changed_plan = WorkPlan.from_mapping(
        {"sub_tls": [{"name": "stage", "order": 1, "plan": {"leaves": []}}]}
    )
    changed_manifest = build_plan_manifest(
        _manifest_plan(changed_plan),
        scope_id="root",
        owned_branch="main",
        manifest_revision=2,
    )
    store.set_plan_manifest(changed_manifest)

    with pytest.raises(PlanRevisionError, match="separately verified reconciliation") as error:
        analyze_plan_revision(tmp_path)

    assert error.value.code == "unversioned_plan_drift"


def test_dry_run_refuses_checkpoint_migration_without_writing(tmp_path: Path) -> None:
    state_root, _ = _project(tmp_path, _document())
    checkpoint_path = state_root / "root" / "run.json"
    raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    raw["version"] = 3
    checkpoint_path.write_text(json.dumps(raw), encoding="utf-8")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError, match="needs migration") as error:
        analyze_plan_revision(tmp_path)

    assert error.value.code == "runstore_needs_migration"
    assert _tree_bytes(state_root) == before
    assert not (state_root / "root" / "migration-error.json").exists()


def test_unresolved_plan_transition_fails_closed(tmp_path: Path) -> None:
    state_root, _ = _project(tmp_path, _document())
    (state_root / "plan-transition.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PlanRevisionError, match="unresolved plan transition") as error:
        analyze_plan_revision(tmp_path)

    assert error.value.code == "transition_pending"
