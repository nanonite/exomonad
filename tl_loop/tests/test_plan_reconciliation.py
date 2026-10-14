"""Safety tests for audited recovery from unjournaled plan identity drift."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.fsm.child import ChildKind, ChildRecord
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLRunning
from tl_loop.loop.driver import WorkPlan, _manifest_plan
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.lock import LockTimeout, RunLock
from tl_loop.state.plan_manifest import ManifestNode, PlanManifest, build_plan_manifest
from tl_loop.state.plan_reconciliation import PlanRevisionError, analyze_plan_reconciliation
from tl_loop.state.plan_reconciliation_apply import apply_plan_reconciliation
from tl_loop.state.plan_reconciliation_report import _safe_text
from tl_loop.state.plan_revision_transaction import recover_plan_revision
from tl_loop.state.schema import (
    IntegrationRuntimeState,
    OrderedStageState,
    ParkCause,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create


def _plan_document(*, future_issue_id: int | None, completed_task: str = "accepted completed task"):
    return {
        "run_id": "root",
        "plan": {
            "sub_tls": [
                {
                    "name": "architecture",
                    "order": 1,
                    "plan": {
                        "leaves": [
                            {
                                "name": "completed",
                                "task": completed_task,
                                "chainlink_issue_id": 811,
                            },
                            {
                                "name": "future",
                                "task": "future work",
                                "chainlink_issue_id": future_issue_id,
                            },
                        ]
                    },
                }
            ]
        },
    }


def _manifest(document: dict[str, object]) -> PlanManifest:
    plan = WorkPlan.from_mapping(document["plan"])
    return build_plan_manifest(_manifest_plan(plan), scope_id="root", owned_branch="main")


def _legacy_root_defaults(
    manifest: PlanManifest,
    *,
    leaf_review_owner: str | None = None,
) -> PlanManifest:
    owner_fields = (
        "leaf_review_owner",
        "aggregate_review_owner",
        "aggregate_repair_owner",
    )
    nodes = []
    for node in manifest.nodes:
        if node.name != "architecture":
            nodes.append(node)
            continue
        declaration = dict(node.declaration)
        declaration.pop("order_explicit", None)
        contract = dict(node.integration_contract)
        for name in owner_fields:
            contract.pop(name, None)
        if leaf_review_owner is not None:
            contract["leaf_review_owner"] = leaf_review_owner
        nodes.append(replace(node, declaration=declaration, integration_contract=contract))
    return replace(manifest, nodes=tuple(nodes), digest=None)


def _post_merge_receipt() -> PostMergeState:
    evidence = {
        "child_id": "completed",
        "repository": "repo",
        "parent_branch": "main",
        "pr_number": "45",
        "head_sha": "a" * 40,
        "merge_journal_id": "merge-45",
        "lane_epoch": "epoch-1",
        "parent_commit_sha": "b" * 40,
        "issue_id": "811",
        "issue_close_intent_id": "close-811",
        "issue_close_journal_id": "chainlink-close:811",
        "changelog_intent_id": "changelog-811",
        "changelog_generation": "1",
        "changelog_commit_sha": "c" * 40,
        "parent_push_intent_id": "push-811",
        "push_journal_id": "push-journal-811",
        "expected_base_sha": "d" * 40,
        "push_receipt_id": "receipt-811",
        "pushed_commit": "e" * 40,
        "bookkeeping_commit": "f" * 40,
        "observed_remote_head": "f" * 40,
        "ancestry_proof": "push-ancestry-811",
    }
    return PostMergeState(PostMergePhase.COMPLETE, evidence)


def _child_record(node: ManifestNode, kind: ChildKind, **evidence: str) -> ChildRecord:
    return ChildRecord(
        child_id=node.name,
        kind=kind,
        evidence={
            "manifest_node_id": node.node_id,
            "manifest_revision": "1",
            **evidence,
        },
        manifest_node_id=node.node_id,
        manifest_revision=1,
    )


def _project(
    tmp_path: Path,
    *,
    candidate_issue_id: int | None = 812,
    candidate_completed_task: str = "accepted completed task",
    stale_runtime_digest: bool = False,
    legacy_root_defaults: bool = False,
    legacy_leaf_review_owner: str | None = None,
) -> tuple[Path, RunStore, RunStore, bytes, bytes]:
    accepted_document = _plan_document(future_issue_id=None)
    candidate_document = _plan_document(
        future_issue_id=candidate_issue_id,
        completed_task=candidate_completed_task,
    )
    accepted_bytes = json.dumps(accepted_document, sort_keys=True).encode()
    candidate_bytes = json.dumps(candidate_document, sort_keys=True).encode()
    state_root = tmp_path / ".exo" / "tl-loop"
    state_root.mkdir(parents=True)
    (state_root / "plan.snapshot").write_bytes(accepted_bytes)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(accepted_bytes).hexdigest() + "\n", encoding="ascii"
    )
    (state_root / "plan.json").write_bytes(candidate_bytes)

    accepted_root = _manifest(accepted_document)
    current_root = _manifest(candidate_document)
    if legacy_root_defaults:
        current_root = _legacy_root_defaults(
            current_root,
            leaf_review_owner=legacy_leaf_review_owner,
        )
    accepted_child = accepted_root.child_manifests["root/sub_tl/architecture"]
    current_child = current_root.child_manifests["root/sub_tl/architecture"]
    runtime_digest = "0" * 64 if stale_runtime_digest else None
    root_store = _create_root(state_root, current_root, accepted_root, runtime_digest)
    child_store = _create_child(state_root, current_child, accepted_child, runtime_digest)
    return state_root, root_store, child_store, accepted_bytes, candidate_bytes


def _create_root(
    state_root: Path,
    current: PlanManifest,
    accepted: PlanManifest,
    runtime_digest: str | None = None,
) -> RunStore:
    create("root", {"plan_manifest": current.to_document()}, root_dir=state_root)
    store = RunStore("root", state_root)
    state = store.load()
    stage = current.node("root/sub_tl/architecture")
    slices = dict(state.slices)
    slices["architecture"] = replace(
        slices["architecture"],
        status=SliceStatus.PARKED,
        attempts=1,
        dispatch_intent_id="dispatch-architecture",
        dispatch_agent_id="agent-architecture",
        dispatch_invocation_id="invocation-architecture",
        dispatch_authoritative_event_seq=1,
        park_cause=ParkCause.PUBLICATION_BLOCKED,
        park_issue_id=46,
        park_audit={
            "error_kind": "missing_invocation_registration",
            "missing_evidence": ["invocation_id", "slice_id", "head_branch"],
        },
    )
    record = _child_record(
        stage,
        ChildKind.SUB_TL,
        dispatch_intent_id="dispatch-architecture",
        invocation_id="invocation-architecture",
    )
    phase = TLRunning(
        current_order=1,
        pending_by_order={1: (record,)},
        scope_path=("root",),
        plan_digest=runtime_digest or accepted.digest or "",
    )
    store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=(OrderedStageState(1, ("architecture",)),),
        integration=IntegrationRuntimeState(
            sub_tl_states={"architecture": IntegrationLifecycle.RUNNING}
        ),
        plan_manifest=current,
    )
    return store


def _create_child(
    state_root: Path,
    current: PlanManifest,
    accepted: PlanManifest,
    runtime_digest: str | None = None,
) -> RunStore:
    create(
        "architecture",
        {"parent_run_id": "root", "plan_manifest": current.to_document()},
        root_dir=state_root / "root",
    )
    store = RunStore("architecture", state_root / "root")
    state = store.load()
    receipt = _post_merge_receipt()
    slices = dict(state.slices)
    slices["completed"] = replace(
        slices["completed"],
        status=SliceStatus.MERGED,
        attempts=1,
        pr_number=45,
        post_merge=receipt,
    )
    completed = _child_record(
        current.node("root/sub_tl/architecture/leaf/completed"),
        ChildKind.LEAF,
    )
    future = _child_record(
        current.node("root/sub_tl/architecture/leaf/future"),
        ChildKind.LEAF,
    )
    phase = TLRunning(
        current_order=0,
        pending_by_order={},
        scope_path=(current.scope_id,),
        plan_digest=runtime_digest or accepted.digest or "",
        parallel_pending=(future,),
        completed_children={"completed": completed},
        post_merge={"completed": receipt},
    )
    store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        plan_manifest=current,
    )
    return store


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }


def test_dry_run_reports_snapshot_manifest_fsm_and_protected_leaf_drift(
    tmp_path: Path,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task=(
            "rewritten completed task\npassword=should-not-appear "
            "https://user:token@forge.invalid/path"
        ),
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()

    assert report["can_apply"] is False
    assert report["evidence_fingerprint"]
    assert {scope["scope_id"] for scope in report["scopes"]} == {
        "root",
        "root/sub_tl/architecture",
    }
    assert all(scope["fsm_matches_accepted"] for scope in report["scopes"])
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture/leaf/completed"
        for blocker in report["blockers"]
    )
    changed = report["manifest_changes_to_accepted_plan"]
    assert any("task" in change["fields"] for change in changed)
    assert any("chainlink_issue_id" in change["fields"] for change in changed)
    completed = next(
        change
        for change in report["scopes"][1]["declaration_changes"]
        if change["node_name"] == "completed"
    )
    assert completed["protected"]
    assert completed["disposition"] == "blocked"
    assert all("observed" in field and "proposed" in field for field in completed["fields"])
    candidate_future = next(
        change
        for change in report["candidate_declaration_changes_not_applied"]
        if change["node_name"] == "future"
    )
    assert candidate_future["disposition"] == "not_applied"
    assert candidate_future["fields"][0]["proposed"] is not None
    assert report["scopes"][1]["runtime_records"][0]["manifest_revision"] == 1
    rendered = json.dumps(report, sort_keys=True)
    assert "rewritten completed task" not in rendered
    assert "should-not-appear" not in rendered
    assert "user:token" not in rendered
    escaped = _safe_text("branch\npassword=hidden https://user:token@forge.invalid/path")
    assert "\\u000a" in escaped
    assert "hidden" not in escaped
    assert "user:token" not in escaped
    root_ownership = report["scopes"][0]["slice_ownership"][0]
    assert root_ownership["dispatch_intent_id"] == "dispatch-architecture"
    assert root_ownership["publication"] is None
    assert root_ownership["park_cause"] == "publication_blocked"
    assert root_ownership["park_issue_id"] == 46
    assert _tree_bytes(state_root) == before


def test_stale_fsm_digests_do_not_hide_manifest_and_ownership_drift(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        stale_runtime_digest=True,
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()

    assert report["can_apply"] is False
    assert all(not scope["fsm_matches_current"] for scope in report["scopes"])
    assert all(not scope["fsm_matches_accepted"] for scope in report["scopes"])
    assert all(scope["runtime_records"] for scope in report["scopes"])
    root_scope = report["scopes"][0]
    assert root_scope["slice_ownership"][0]["park_cause"] == "publication_blocked"
    assert (
        root_scope["slice_ownership"][0]["park_audit"]["error_kind"]
        == "missing_invocation_registration"
    )
    completed = next(
        item for item in report["scopes"][1]["slice_ownership"] if item["slice_id"] == "completed"
    )
    assert completed["pr_number"] == 45
    assert completed["post_merge_phase"] == "complete"
    assert completed["post_merge_evidence_sha256"]
    assert {blocker["code"] for blocker in report["blockers"]} >= {
        "runtime_digest_unproven",
        "protected_identity_unproven",
    }
    assert _tree_bytes(state_root) == before


def test_apply_restores_only_proven_future_drift_and_keeps_completion_receipt(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, accepted_bytes, candidate_bytes = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    assert report.can_apply, report.blockers
    root_report = report.to_document()["scopes"][0]
    architecture_change = next(
        change
        for change in root_report["declaration_changes"]
        if change["node_name"] == "architecture"
    )
    assert architecture_change["protected"]
    assert architecture_change["disposition"] == "restore"
    before_receipt = child_store.load().slices["completed"].post_merge

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore accepted manifests before revising future metadata",
        report.evidence_fingerprint,
        actor="operator",
    )

    root_state = root_store.load()
    child_state = child_store.load()
    assert result.applied
    assert root_state.plan_manifest is not None
    assert root_state.plan_manifest.digest == _manifest(_plan_document(future_issue_id=None)).digest
    assert child_state.plan_manifest is not None
    assert (
        child_state.plan_manifest.digest
        == _manifest(_plan_document(future_issue_id=None))
        .child_manifests["root/sub_tl/architecture"]
        .digest
    )
    assert child_state.slices["future"].chainlink_issue_id is None
    assert child_state.slices["completed"].post_merge == before_receipt
    assert root_state.slices["architecture"].dispatch_invocation_id == "invocation-architecture"
    assert (state_root / "plan.snapshot").read_bytes() == accepted_bytes
    assert (state_root / "plan.json").read_bytes() == candidate_bytes
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1
    assert not (state_root / "plan-revision-transition.json").exists()
    assert not analyze_plan_reconciliation(tmp_path).has_drift
    repeat = analyze_plan_reconciliation(tmp_path)
    second = apply_plan_reconciliation(
        tmp_path,
        "repeat already completed reconciliation",
        repeat.evidence_fingerprint,
        actor="operator",
    )
    assert not second.applied
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1


def test_apply_accepts_exact_legacy_parent_defaults_with_fsm_and_dispatch_proof(
    tmp_path: Path,
) -> None:
    state_root, root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
    )

    report = analyze_plan_reconciliation(tmp_path)
    root_report = report.to_document()["scopes"][0]

    assert report.can_apply, report.blockers
    equivalence = root_report["legacy_default_equivalences"][0]
    assert equivalence["node_id"] == "root/sub_tl/architecture"
    assert equivalence["fields"] == [
        "declaration.order_explicit",
        "integration_contract.aggregate_repair_owner",
        "integration_contract.aggregate_review_owner",
        "integration_contract.leaf_review_owner",
    ]
    assert equivalence["evidence_source"] == (
        "verified_snapshot_and_matching_fsm_dispatch_identity"
    )

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore the accepted manifest after proving legacy defaults",
        report.evidence_fingerprint,
        actor="operator",
    )

    assert result.applied
    assert root_store.load().plan_manifest == _manifest(_plan_document(future_issue_id=None))
    assert (state_root / "plan-reconciliations.jsonl").is_file()


def test_legacy_default_equivalence_does_not_hide_explicit_owner_changes(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
        legacy_leaf_review_owner="aggregate",
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = report["scopes"][0]

    assert not report["can_apply"]
    equivalent_fields = root["legacy_default_equivalences"][0]["fields"]
    assert "integration_contract.leaf_review_owner" not in equivalent_fields
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture"
        for blocker in report["blockers"]
    )


def test_legacy_default_equivalence_requires_accepted_fsm_digest(tmp_path: Path) -> None:
    _state_root, root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
    )
    state = root_store.load()
    phase = replace(state.recursive_fsm, plan_digest=state.plan_manifest.digest)
    root_store.checkpoint(
        phase,
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = report["scopes"][0]

    assert not report["can_apply"]
    assert root["legacy_default_equivalences"] == []
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture"
        for blocker in report["blockers"]
    )


def test_apply_requires_the_exact_dry_run_fingerprint(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    before = _tree_bytes(state_root)
    with pytest.raises(PlanRevisionError, match="inputs changed") as error:
        apply_plan_reconciliation(tmp_path, "restore", "0" * 64, actor="operator")
    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    "relative_path",
    ("plan.json", "root/run.json", "root/architecture/run.json"),
)
def test_apply_rejects_changed_inputs_after_dry_run(
    tmp_path: Path,
    relative_path: str,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    changed_path = state_root / relative_path
    changed_path.write_bytes(changed_path.read_bytes() + b" ")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "reject a stale candidate",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before


def test_apply_refuses_pending_plan_revision_journal_without_replaying_it(
    tmp_path: Path,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    journal = state_root / "plan-revision-transition.json"
    journal.write_text("pending recovery", encoding="utf-8")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "do not mix transaction replay with reconciliation",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "transition_pending"
    assert _tree_bytes(state_root) == before


def test_apply_rejects_controller_lifecycle_lock_timeout(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    with RunLock(state_root / "controller-lifecycle.lock"), pytest.raises(LockTimeout):
        apply_plan_reconciliation(
            tmp_path,
            "restore",
            report.evidence_fingerprint,
            actor="operator",
            lock_timeout=0.02,
        )
    assert not (state_root / "plan-reconciliations.jsonl").exists()


@pytest.mark.parametrize("lock_path", ["plan-revision.lock", "run.lock", "root/run.lock"])
def test_apply_rejects_revision_and_scope_lock_timeouts(
    tmp_path: Path,
    lock_path: str,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    with RunLock(state_root / lock_path), pytest.raises(LockTimeout):
        apply_plan_reconciliation(
            tmp_path,
            "wait for the scope lock",
            report.evidence_fingerprint,
            actor="operator",
            lock_timeout=0.02,
        )
    assert not (state_root / "plan-reconciliations.jsonl").exists()


@pytest.mark.parametrize("interrupt_index", [0, 1, 2])
def test_interrupted_apply_recovers_without_duplicate_audit(
    tmp_path: Path,
    interrupt_index: int,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)

    def interrupt_after_target(index: int, _target: str) -> None:
        if index == interrupt_index:
            raise RuntimeError("simulated interruption")

    result = apply_plan_reconciliation(
        tmp_path,
        "recover an interrupted reconciliation",
        report.evidence_fingerprint,
        actor="operator",
        after_target=interrupt_after_target,
    )

    assert result.applied and result.recovered_during_apply
    records = (state_root / "plan-reconciliations.jsonl").read_text().splitlines()
    assert len(records) == 1
    assert json.loads(records[0])["evidence_fingerprint"] == report.evidence_fingerprint
    assert not (state_root / "plan-revision-transition.json").exists()


def test_pending_reconciliation_journal_replays_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)

    def stop_recovery(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated process loss")

    import tl_loop.state.plan_reconciliation_apply as apply_module

    monkeypatch.setattr(apply_module, "_recover_locked", stop_recovery)

    def interrupt_after_first(_index: int, _target: str) -> None:
        raise RuntimeError("simulated process loss")

    with pytest.raises(PlanRevisionError, match="remains journaled"):
        apply_plan_reconciliation(
            tmp_path,
            "recover after restart",
            report.evidence_fingerprint,
            actor="operator",
            after_target=interrupt_after_first,
        )
    assert (state_root / "plan-revision-transition.json").exists()
    monkeypatch.undo()

    assert recover_plan_revision(tmp_path)
    assert not (state_root / "plan-revision-transition.json").exists()
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1
    assert not analyze_plan_reconciliation(tmp_path).has_drift


def test_symlinked_snapshot_and_bad_digest_fail_closed(tmp_path: Path) -> None:
    state_root, _root, _child, accepted, _candidate = _project(tmp_path)
    snapshot = state_root / "plan.snapshot"
    snapshot.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(accepted)
    snapshot.symlink_to(outside)
    with pytest.raises(PlanRevisionError, match="symlink"):
        analyze_plan_reconciliation(tmp_path)

    snapshot.unlink()
    snapshot.write_bytes(accepted)
    (state_root / "plan.snapshot.sha256").write_text("bad\n", encoding="ascii")
    with pytest.raises(PlanRevisionError, match="SHA-256"):
        analyze_plan_reconciliation(tmp_path)

    malformed = b"{"
    snapshot.write_bytes(malformed)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(malformed).hexdigest() + "\n", encoding="ascii"
    )
    with pytest.raises(
        PlanRevisionError, match="accepted snapshot plan failed validation"
    ) as error:
        analyze_plan_reconciliation(tmp_path)
    assert error.value.code == "plan_invalid"


def test_missing_child_runstore_evidence_blocks_restoration(tmp_path: Path) -> None:
    _state_root, _root_store, child_store, _accepted, _candidate = _project(tmp_path)
    child_store.path.unlink()

    report = analyze_plan_reconciliation(tmp_path).to_document()

    assert report["can_apply"] is False
    assert any(item["code"] == "materialized_scope_missing" for item in report["blockers"])


def test_malformed_materialized_runstore_fails_closed(tmp_path: Path) -> None:
    _state_root, _root_store, child_store, _accepted, _candidate = _project(tmp_path)
    child_store.path.write_bytes(b"{")

    with pytest.raises(PlanRevisionError, match="failed validation") as error:
        analyze_plan_reconciliation(tmp_path)

    assert error.value.code == "runstore_invalid"


def test_operator_cli_emits_structured_dry_run(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _project(tmp_path)
    from tl_loop.__main__ import main

    exit_code = main(
        [
            "plan-reconciliation",
            "--project-root",
            str(tmp_path),
            "--dry-run",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["operation"] == "plan_reconciliation"
    assert report["evidence_fingerprint"]
