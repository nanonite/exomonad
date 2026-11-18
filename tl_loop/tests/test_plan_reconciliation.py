"""Safety tests for audited recovery from unjournaled plan identity drift."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tl_loop.fsm.scope import TLPRFiled
from tl_loop.loop.driver import (
    TLLoopConfig,
    _verify_supplied_plan_before_identity_recovery,
    _work_plan_from_manifest,
)
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.lock import LockTimeout, RunLock
from tl_loop.state.plan_reconciliation import analyze_plan_reconciliation
from tl_loop.state.plan_reconciliation_apply import apply_plan_reconciliation
from tl_loop.state.plan_reconciliation_report import _safe_text
from tl_loop.state.plan_revision_transaction import recover_plan_revision
from tl_loop.state.plan_revision_types import PlanRevisionError
from tl_loop.state.schema import (
    IntegrationCandidateState,
    SliceStatus,
)
from tl_loop.tests.plan_reconciliation_fixtures import (
    _corrupt_parent_slice,
    _direct_root_legacy_project,
    _legacy_tlprfiled_project,
    _manifest,
    _manifest_digest,
    _mapping,
    _mutate_completed_spawn_result,
    _plan_digest,
    _plan_document,
    _pr_filed,
    _project,
    _records,
    _replace_pr_filed_field,
    _required_manifest,
    _required_post_merge,
    _running_phase,
    _scope_phase,
    _strings,
    _tree_bytes,
)

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
    scopes = _records(report["scopes"])
    blockers = _records(report["blockers"])

    assert report["can_apply"] is False
    assert report["evidence_fingerprint"]
    assert {scope["scope_id"] for scope in scopes} == {
        "root",
        "root/sub_tl/architecture",
    }
    assert all(scope["fsm_matches_accepted"] for scope in scopes)
    assert any(
        blocker["code"] == "protected_declaration_evidence_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture/leaf/completed"
        for blocker in blockers
    )
    changed = _records(report["manifest_changes_to_accepted_plan"])
    assert any("task" in _strings(change["fields"]) for change in changed)
    assert any("chainlink_issue_id" in _strings(change["fields"]) for change in changed)
    completed = next(
        change
        for change in _records(scopes[1]["declaration_changes"])
        if change["node_name"] == "completed"
    )
    assert completed["protected"]
    assert completed["disposition"] == "blocked"
    assert all(
        "observed" in field and "proposed" in field for field in _records(completed["fields"])
    )
    candidate_future = next(
        change
        for change in _records(report["candidate_declaration_changes_not_applied"])
        if change["node_name"] == "future"
    )
    assert candidate_future["disposition"] == "not_applied"
    assert _records(candidate_future["fields"])[0]["proposed"] is not None
    assert _records(scopes[1]["runtime_records"])[0]["manifest_revision"] == 1
    rendered = json.dumps(report, sort_keys=True)
    assert "rewritten completed task" not in rendered
    assert "should-not-appear" not in rendered
    assert "user:token" not in rendered
    escaped = _safe_text("branch\npassword=hidden https://user:token@forge.invalid/path")
    assert "\\u000a" in escaped
    assert "hidden" not in escaped
    assert "user:token" not in escaped
    root_ownership = _records(scopes[0]["slice_ownership"])[0]
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
    scopes = _records(report["scopes"])

    assert report["can_apply"] is False
    assert all(not scope["fsm_matches_current"] for scope in scopes)
    assert all(not scope["fsm_matches_accepted"] for scope in scopes)
    assert all(scope["runtime_records"] for scope in scopes)
    root_scope = scopes[0]
    root_ownership = _records(root_scope["slice_ownership"])[0]
    assert root_ownership["park_cause"] == "publication_blocked"
    assert _mapping(root_ownership["park_audit"])["error_kind"] == "missing_invocation_registration"
    completed = next(
        item for item in _records(scopes[1]["slice_ownership"]) if item["slice_id"] == "completed"
    )
    assert completed["pr_number"] == 45
    assert completed["post_merge_phase"] == "complete"
    assert completed["post_merge_evidence_sha256"]
    assert {blocker["code"] for blocker in _records(report["blockers"])} >= {
        "runtime_digest_unproven",
        "protected_declaration_evidence_unproven",
    }
    assert _tree_bytes(state_root) == before


def test_apply_restores_only_proven_future_drift_and_keeps_completion_receipt(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, accepted_bytes, candidate_bytes = _project(tmp_path)
    report = analyze_plan_reconciliation(tmp_path)
    assert report.can_apply, report.blockers
    root_report = _records(report.to_document()["scopes"])[0]
    architecture_change = next(
        change
        for change in _records(root_report["declaration_changes"])
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
    root_report = _records(report.to_document()["scopes"])[0]

    assert report.can_apply, report.blockers
    equivalence = _records(root_report["legacy_default_equivalences"])[0]
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


def test_apply_migrates_unchanged_legacy_aggregate_defaults_and_keeps_receipts(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, accepted_bytes, candidate_bytes = (
        _legacy_tlprfiled_project(tmp_path)
    )
    assert accepted_bytes == candidate_bytes
    before_child = child_store.load()
    assert isinstance(before_child.recursive_fsm, TLPRFiled)
    assert not hasattr(before_child.recursive_fsm, "completed_children")
    before_receipt = before_child.slices["completed"].post_merge
    journal_path = child_store.run_dir / "action-journal.json"
    journal_bytes = journal_path.read_bytes()
    ledger_path = tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    ledger_bytes = ledger_path.read_bytes()

    analysis = analyze_plan_reconciliation(tmp_path)

    assert analysis.can_apply, analysis.to_document()["blockers"]
    transition = analysis.compatibility_transition
    assert transition is not None
    assert transition["persisted_plan_compatibility_version"] == 1
    assert transition["target_plan_compatibility_version"] == 2
    assert transition["from"] == "aggregate_review_owner=aggregate"
    assert transition["to"] == "aggregate_review_owner=none"
    allowed_fields = transition["allowed_fields"]
    assert isinstance(allowed_fields, list)
    assert "integration_contract.aggregate_review_owner" in allowed_fields
    assert "child_manifest_digest" in allowed_fields
    affected_nodes = transition["affected_nodes"]
    assert isinstance(affected_nodes, list)
    assert any(
        isinstance(node, dict)
        and isinstance(node.get("node_id"), str)
        and node["node_id"].endswith("/leaf/completed")
        and isinstance(node.get("fields"), list)
        and "integration_contract.aggregate_review_owner" in node["fields"]
        for node in affected_nodes
    )

    def interrupt_after_first_target(_index: int, _target: str) -> None:
        raise RuntimeError("simulated migration process interruption")

    result = apply_plan_reconciliation(
        tmp_path,
        "Apply the audited legacy aggregate-review default migration",
        analysis.evidence_fingerprint,
        actor="operator",
        after_target=interrupt_after_first_target,
    )

    assert result.applied and result.recovered_during_apply
    root_after = root_store.load()
    child_after = child_store.load()
    assert result.applied
    assert root_after.plan_manifest == _manifest(
        _plan_document(future_issue_id=None, five_ordered_stages=True)
    )
    assert (
        child_after.plan_manifest
        == root_after.plan_manifest.child_manifests["root/sub_tl/architecture"]
    )
    assert _plan_digest(root_after.recursive_fsm) == _manifest_digest(root_after.plan_manifest)
    assert _plan_digest(child_after.recursive_fsm) == _manifest_digest(child_after.plan_manifest)
    assert child_after.recursive_fsm == replace(
        _pr_filed(before_child.recursive_fsm),
        plan_digest=_manifest_digest(child_after.plan_manifest),
    )
    assert (
        root_after.plan_manifest.node("root/sub_tl/architecture").integration_contract[
            "aggregate_review_owner"
        ]
        == "none"
    )
    assert child_after.slices["completed"].post_merge == before_receipt
    assert child_after.slices["completed"].status is SliceStatus.MERGED
    assert child_after.slices["completed"].chainlink_issue_id == 811
    assert [stage.order for stage in root_after.ordered_stages] == [1, 2, 3, 4, 5]
    assert [stage.sub_tls for stage in root_after.ordered_stages] == [
        ("architecture",),
        ("kani",),
        ("stage-three",),
        ("stage-four",),
        ("stage-five",),
    ]
    assert journal_path.read_bytes() == journal_bytes
    assert ledger_path.read_bytes() == ledger_bytes
    assert (state_root / "plan.snapshot").read_bytes() == accepted_bytes
    assert (state_root / "plan.json").read_bytes() == candidate_bytes
    audit = json.loads((state_root / "plan-reconciliations.jsonl").read_text().splitlines()[0])
    assert audit["compatibility_transition"]["from"] == "aggregate_review_owner=aggregate"
    assert audit["compatibility_transition"]["to"] == "aggregate_review_owner=none"
    replay = analyze_plan_reconciliation(tmp_path)
    assert not replay.has_drift
    assert replay.compatibility_transition is None
    assert not apply_plan_reconciliation(
        tmp_path,
        "Replay the already applied compatibility migration",
        replay.evidence_fingerprint,
        actor="operator",
    ).applied

    # Prove the migrated checkpoint is accepted by the same driver guard that
    # checks supplied plan identity before recovery and ordered continuation.
    resumed_plan = _work_plan_from_manifest(root_after.plan_manifest)
    _verify_supplied_plan_before_identity_recovery(
        "root",
        resumed_plan,
        root_after,
        TLLoopConfig(
            root_dir=tmp_path,
            project_root=tmp_path,
            run_id="root",
            branch="main",
            session_mode="continue",
        ),
        state_root,
    )
    assert len(resumed_plan.sub_tls) == 5
    assert root_after.current_order == 1
    assert root_after.slices["architecture"].status is SliceStatus.PARKED


def test_legacy_aggregate_default_reconciliation_accepts_tl_pr_filed_leaf_receipts(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, child_store, accepted_bytes, candidate_bytes = (
        _legacy_tlprfiled_project(tmp_path)
    )
    child_state = child_store.load()
    assert isinstance(child_state.recursive_fsm, TLPRFiled)
    assert not hasattr(child_state.recursive_fsm, "completed_children")
    assert accepted_bytes == candidate_bytes

    analysis = analyze_plan_reconciliation(tmp_path)

    assert analysis.can_apply, analysis.to_document()["blockers"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("scope_path", ("root/sub_tl/wrong",)),
        ("aggregate_pr", "47"),
        ("head_sha", "3" * 40),
        ("base_sha", "4" * 40),
        ("parent_branch", "release"),
        ("handoff", "foreign:owner"),
    ),
)
def test_tl_pr_filed_reconciliation_rejects_conflicting_handoff_coordinates(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _state_root, _root_store, child_store, *_ = _legacy_tlprfiled_project(tmp_path)
    state = child_store.load()
    phase = _replace_pr_filed_field(_pr_filed(state.recursive_fsm), field, value)
    child_store.checkpoint(
        _pr_filed(phase),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )

    analysis = analyze_plan_reconciliation(tmp_path)

    assert not analysis.can_apply
    assert any(
        blocker.get("node_id") == "root/sub_tl/architecture/leaf/completed"
        for blocker in _records(analysis.to_document()["blockers"])
    )


def test_tl_pr_filed_reconciliation_rejects_conflicting_parent_publication_binding(
    tmp_path: Path,
) -> None:
    _state_root, root_store, _child_store, *_ = _legacy_tlprfiled_project(tmp_path)
    state = root_store.load()
    candidate = state.integration.candidates["architecture"]
    conflicting = replace(candidate, aggregate_head_sha="5" * 40)
    root_store.checkpoint(
        _scope_phase(state.recursive_fsm),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=replace(state.integration, candidates={"architecture": conflicting}),
        plan_manifest=state.plan_manifest,
    )

    assert not analyze_plan_reconciliation(tmp_path).can_apply


def test_tl_pr_filed_reconciliation_requires_child_publication_coordinates(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, child_store, *_ = _legacy_tlprfiled_project(tmp_path)
    state = child_store.load()
    integration = replace(state.integration, aggregate_head_sha=None)
    child_store.checkpoint(
        _scope_phase(state.recursive_fsm),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=integration,
        plan_manifest=state.plan_manifest,
    )

    assert not analyze_plan_reconciliation(tmp_path).can_apply


def test_tl_pr_filed_reconciliation_requires_completed_leaf_receipt(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, child_store, *_ = _legacy_tlprfiled_project(tmp_path)
    state = child_store.load()
    slices = dict(state.slices)
    slices["completed"] = replace(slices["completed"], post_merge=None)
    child_store.checkpoint(
        _scope_phase(state.recursive_fsm),
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )

    assert not analyze_plan_reconciliation(tmp_path).can_apply


def test_tl_pr_filed_reconciliation_requires_legacy_spawn_journal(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, child_store, *_ = _legacy_tlprfiled_project(tmp_path)
    (child_store.run_dir / "action-journal.json").unlink()

    assert not analyze_plan_reconciliation(tmp_path).can_apply


def test_tl_finalizing_reconciliation_accepts_corroborated_non_root_handoff(
    tmp_path: Path,
) -> None:
    _project(
        tmp_path,
        candidate_issue_id=None,
        legacy_aggregate_review_default=True,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        legacy_integration_coordinator_binding=True,
        legacy_completed_issue_binding=True,
        controller_alias="architecture",
        five_ordered_stages=True,
        terminal_aggregate_handoff=True,
        terminal_handoff_phase="finalizing",
    )

    assert analyze_plan_reconciliation(tmp_path).can_apply


def test_tl_pr_filed_reconciliation_apply_rejects_stale_evidence_fingerprint(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, child_store, *_ = _legacy_tlprfiled_project(tmp_path)
    analysis = analyze_plan_reconciliation(tmp_path)
    assert analysis.can_apply
    state = child_store.load()
    phase = replace(_pr_filed(state.recursive_fsm), head_sha="6" * 40)
    child_store.checkpoint(
        _pr_filed(phase),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )

    with pytest.raises(PlanRevisionError, match="inputs changed"):
        apply_plan_reconciliation(
            tmp_path,
            "Reject stale terminal handoff evidence",
            analysis.evidence_fingerprint,
            actor="operator",
        )


def test_compatible_legacy_manifest_version_does_not_trigger_migration(tmp_path: Path) -> None:
    _state_root, root_store, _child_store, accepted_bytes, candidate_bytes = _project(
        tmp_path,
        candidate_issue_id=None,
        persisted_compatible_plan_version=1,
    )

    analysis = analyze_plan_reconciliation(tmp_path)

    assert accepted_bytes == candidate_bytes
    assert analysis.compatibility_transition is None
    assert not analysis.has_drift
    assert not analysis.can_apply
    assert _required_manifest(root_store.load().plan_manifest).plan_compatibility_version == 1


def test_candidate_plan_drift_does_not_authorize_default_migration(tmp_path: Path) -> None:
    _state_root, _root_store, _child_store, accepted_bytes, candidate_bytes = _project(
        tmp_path,
        candidate_issue_id=812,
        legacy_aggregate_review_default=True,
    )

    analysis = analyze_plan_reconciliation(tmp_path)

    assert accepted_bytes != candidate_bytes
    assert analysis.compatibility_transition is None
    assert not analysis.can_apply


def test_explicit_aggregate_review_policy_is_not_reinterpreted(tmp_path: Path) -> None:
    _state_root, _root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_issue_id=None,
    )
    snapshot_path = tmp_path / ".exo" / "tl-loop" / "plan.snapshot"
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    architecture = document["plan"]["sub_tls"][0]
    architecture["integration"] = {"aggregate_review_owner": "aggregate"}
    encoded = json.dumps(document, sort_keys=True).encode()
    snapshot_path.write_bytes(encoded)
    (tmp_path / ".exo" / "tl-loop" / "plan.json").write_bytes(encoded)
    (tmp_path / ".exo" / "tl-loop" / "plan.snapshot.sha256").write_text(
        hashlib.sha256(encoded).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(PlanRevisionError) as error:
        analyze_plan_reconciliation(tmp_path)

    assert error.value.code == "unsupported_aggregate_review_policy"


def test_legacy_default_equivalence_does_not_hide_explicit_owner_changes(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
        legacy_leaf_review_owner="aggregate",
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = _records(report["scopes"])[0]

    assert not report["can_apply"]
    equivalent_fields = _strings(_records(root["legacy_default_equivalences"])[0]["fields"])
    assert "integration_contract.leaf_review_owner" not in equivalent_fields
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture"
        for blocker in _records(report["blockers"])
    )


def test_legacy_default_equivalence_requires_accepted_fsm_digest(tmp_path: Path) -> None:
    _state_root, root_store, _child_store, _accepted, _candidate = _project(
        tmp_path,
        legacy_root_defaults=True,
    )
    state = root_store.load()
    phase = replace(
        _running_phase(state.recursive_fsm), plan_digest=_manifest_digest(state.plan_manifest)
    )
    root_store.checkpoint(
        _scope_phase(phase),
        state.slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
        plan_manifest=state.plan_manifest,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = _records(report["scopes"])[0]

    assert not report["can_apply"]
    assert root["legacy_default_equivalences"] == []
    assert any(
        blocker["code"] == "protected_identity_unproven"
        and blocker["node_id"] == "root/sub_tl/architecture"
        for blocker in _records(report["blockers"])
    )


def test_apply_restores_completed_task_and_issue_binding_from_dispatch_evidence(
    tmp_path: Path,
) -> None:
    _state_root, root_store, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
    )
    before_child = child_store.load()
    before_slice = before_child.slices["completed"]
    before_receipt = before_slice.post_merge
    action_journal = child_store.run_dir / "action-journal.json"
    action_journal_bytes = action_journal.read_bytes()

    report = analyze_plan_reconciliation(tmp_path)
    document = report.to_document()
    child_report = next(
        item
        for item in _records(document["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child_report["protected_declaration_proofs"])[0]

    assert report.can_apply, report.blockers
    assert proof["fields"] == ["chainlink_issue_id", "task"]
    assert proof["disposition"] == "proven"
    assert "confirmed_spawn_action_with_accepted_task" in _strings(proof["evidence"])
    assert "completed_issue_closure_receipt" in _strings(proof["evidence"])

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore the completed declaration from its dispatch and closure evidence",
        report.evidence_fingerprint,
        actor="operator",
    )

    after_root = root_store.load()
    after_child = child_store.load()
    restored = after_child.slices["completed"]
    accepted_manifest = _manifest(_plan_document(future_issue_id=None, completed_issue_id=None))
    assert result.applied
    assert after_root.plan_manifest == accepted_manifest
    assert (
        after_child.plan_manifest == accepted_manifest.child_manifests["root/sub_tl/architecture"]
    )
    assert restored.status is SliceStatus.MERGED
    assert restored.pr_number == before_slice.pr_number == 45
    assert restored.chainlink_issue_id is None
    assert restored.dispatch_intent_id == before_slice.dispatch_intent_id
    assert restored.dispatch_agent_id == before_slice.dispatch_agent_id
    assert restored.dispatch_invocation_id == before_slice.dispatch_invocation_id
    assert restored.post_merge == before_receipt
    assert action_journal.read_bytes() == action_journal_bytes


def test_apply_restores_historical_dispatch_without_inventing_invocation_identity(
    tmp_path: Path,
) -> None:
    state_root, _root_store, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    before = child_store.load().slices["completed"]
    before_tree = _tree_bytes(tmp_path)

    report = analyze_plan_reconciliation(tmp_path)
    assert _tree_bytes(tmp_path) == before_tree
    child_report = next(
        item
        for item in _records(report.to_document()["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child_report["protected_declaration_proofs"])[0]

    assert report.can_apply, report.blockers
    assert proof["disposition"] == "proven"
    assert "missing_dispatch_invocation_preserved_as_unknown" in _strings(proof["evidence"])
    dispatch_event = json.loads(
        (tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl").read_text(
            encoding="utf-8"
        )
    )
    controller_invocation = (
        tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    ).read_bytes()
    assert proof["correlation"] == {
        "accepted_boundary_sha256": _mapping(proof["correlation"])["accepted_boundary_sha256"],
        "accepted_task_sha256": _mapping(proof["correlation"])["accepted_task_sha256"],
        "base_branch": "main.agent-architecture",
        "branch": "main.agent-architecture.agent-completed",
        "dispatch_event_seq": 1,
        "dispatch_event_sha256": hashlib.sha256(
            json.dumps(dispatch_event, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "dispatch_event_emitter_agent_id": "agent-architecture",
        "dispatch_event_expected_emitter_agent_id": "agent-architecture",
        "dispatch_event_observed_emitter_agent_id": "agent-architecture",
        "dispatch_intent_id": "dispatch-completed",
        "dispatch_invocation_id": None,
        "dispatch_invocation_status": "unavailable_in_legacy_records",
        "dispatch_owner_agent_id": "agent-completed",
        "pr_base_branch": "main.agent-architecture",
        "pr_head_sha": "a" * 40,
        "pr_number": 45,
        "completion_receipt_sha256": hashlib.sha256(
            json.dumps(
                dict(_required_post_merge(before.post_merge).evidence),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "ledger_run_id": "root-ledger",
        "scope_id": "root/sub_tl/architecture",
        "scope_run_id": "architecture",
        "scope_controller_agent_id": "agent-architecture",
        "scope_controller_invocation_id": "invocation-architecture",
        "scope_controller_invocation_sha256": hashlib.sha256(controller_invocation).hexdigest(),
        "scope_controller_invocation_trigger": "spawn",
        "scope_controller_branch_sha256": hashlib.sha256(b"main.agent-architecture").hexdigest(),
        "scope_controller_worktree_sha256": hashlib.sha256(
            b"/tmp/architecture/agent-architecture"
        ).hexdigest(),
        "parent_scope_run_id": "root",
        "parent_scope_controller_agent_id": "root",
        "parent_dispatch_intent_id": "dispatch-architecture",
        "parent_dispatch_invocation_id": "invocation-architecture",
        "parent_dispatch_event_seq": 1,
        "parent_dispatch_agent_id": "agent-architecture",
        "parent_dispatch_attempts": 1,
        "parent_dispatch_attempt_condition": "positive_with_complete_dispatch_proof",
        "parent_dispatch_owner_record": "dispatch_owner",
        "slice_id": "completed",
        "spawn_action_key": _mapping(proof["correlation"])["spawn_action_key"],
        "spawn_result_agent_id": None,
        "spawn_result_invocation_id": None,
        "slice_worktree_sha256": hashlib.sha256(b"/tmp/architecture/completed").hexdigest(),
        "spawn_result_worktree_sha256": hashlib.sha256(
            b"/tmp/architecture/agent-completed"
        ).hexdigest(),
        "worktree_paths_match": False,
    }

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore the completed declaration from legacy dispatch evidence",
        report.evidence_fingerprint,
        actor="operator",
    )

    restored = child_store.load().slices["completed"]
    audit = json.loads((state_root / "plan-reconciliations.jsonl").read_text().splitlines()[0])
    assert result.applied
    assert restored.status is SliceStatus.MERGED
    assert restored.dispatch_invocation_id is None
    assert restored.dispatch_agent_id == before.dispatch_agent_id
    assert restored.dispatch_intent_id == before.dispatch_intent_id
    assert restored.dispatch_authoritative_event_seq == before.dispatch_authoritative_event_seq
    assert restored.pr_number == before.pr_number == 45
    assert restored.post_merge == before.post_merge
    assert audit["protected_declaration_proofs"][0]["correlation"] == proof["correlation"]


def test_zero_parent_attempts_reconcile_only_with_complete_proof_and_preserve_audit(
    tmp_path: Path,
) -> None:
    state_root, root_store, child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=0,
    )
    root_before = root_store.load().slices["architecture"]
    child_before = child_store.load().slices["completed"]
    report = analyze_plan_reconciliation(tmp_path)
    document = report.to_document()
    child_report = next(
        item
        for item in _records(document["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )
    emitter_proof = _mapping(child_report["scope_emitter_proof"])
    declaration_proof = _records(child_report["protected_declaration_proofs"])[0]

    assert report.can_apply, report.blockers
    assert _mapping(emitter_proof["correlation"])["parent_dispatch_attempts"] == 0
    assert _mapping(emitter_proof["correlation"])["parent_dispatch_attempt_condition"] == (
        "zero_with_complete_dispatch_proof"
    )
    assert "zero_attempt_counter_with_complete_dispatch_proof" in _strings(
        emitter_proof["evidence"]
    )
    assert _mapping(declaration_proof["correlation"])["parent_dispatch_attempts"] == 0
    assert _mapping(declaration_proof["correlation"])["parent_dispatch_attempt_condition"] == (
        "zero_with_complete_dispatch_proof"
    )

    result = apply_plan_reconciliation(
        tmp_path,
        "Restore accepted declarations from complete legacy dispatch proof",
        report.evidence_fingerprint,
        actor="operator",
    )

    root_after = root_store.load().slices["architecture"]
    child_after = child_store.load().slices["completed"]
    audit = json.loads((state_root / "plan-reconciliations.jsonl").read_text().splitlines()[0])
    audit_child = next(
        item for item in _records(audit["scopes"]) if item["scope_id"] == "root/sub_tl/architecture"
    )
    assert result.applied
    assert root_after.attempts == root_before.attempts == 0
    assert root_after.dispatch_intent_id == root_before.dispatch_intent_id
    assert root_after.dispatch_invocation_id == root_before.dispatch_invocation_id
    assert (
        root_after.dispatch_authoritative_event_seq == root_before.dispatch_authoritative_event_seq
    )
    assert child_after.attempts == child_before.attempts
    assert child_after.status is SliceStatus.MERGED
    assert child_after.post_merge == child_before.post_merge
    assert child_after.pr_number == child_before.pr_number
    assert (
        _mapping(_mapping(audit_child["scope_emitter_proof"])["correlation"])[
            "parent_dispatch_attempts"
        ]
        == 0
    )
    assert (
        _mapping(_mapping(audit_child["scope_emitter_proof"])["correlation"])[
            "parent_dispatch_attempt_condition"
        ]
        == "zero_with_complete_dispatch_proof"
    )
    assert len((state_root / "plan-reconciliations.jsonl").read_text().splitlines()) == 1


def test_legacy_dispatch_event_change_invalidates_reconciliation_fingerprint(
    tmp_path: Path,
) -> None:
    _state_root, _root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    report = analyze_plan_reconciliation(tmp_path)
    ledger = tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    event = json.loads(ledger.read_text(encoding="utf-8"))
    event["data"]["branch"] = "main.changed-after-dry-run"
    ledger.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "Reject stale legacy dispatch evidence",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"


def test_parent_attempt_counter_change_invalidates_reconciliation_fingerprint(
    tmp_path: Path,
) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=0,
    )
    report = analyze_plan_reconciliation(tmp_path)
    document = json.loads(root_store.path.read_text(encoding="utf-8"))
    document["slices"]["architecture"]["attempts"] = 1
    root_store.path.write_text(json.dumps(document), encoding="utf-8")
    before_apply = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "Reject a changed dispatch counter after dry run",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before_apply


def test_scope_controller_registration_change_invalidates_fingerprint(tmp_path: Path) -> None:
    _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    report = analyze_plan_reconciliation(tmp_path)
    registration = tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    recorded = dict(report.input_hashes)
    assert recorded[".exo/agents/agent-architecture/invocation.json"] is not None
    registration.write_text(
        registration.read_text(encoding="utf-8").replace(
            "main.agent-architecture", "main.changed-after-dry-run"
        ),
        encoding="utf-8",
    )

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "Reject stale scope controller ownership evidence",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"


@pytest.mark.parametrize(
    ("registration_state", "reason_code"),
    [
        ("missing", "scope_controller_registration_missing"),
        ("malformed", "scope_controller_registration_malformed"),
    ],
)
@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_legacy_dispatch_requires_a_valid_scope_controller_registration(
    tmp_path: Path,
    registration_state: str,
    reason_code: str,
    parent_attempts: int,
) -> None:
    state_root, _root, _child, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    registration = tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    if registration_state == "missing":
        registration.unlink()
    else:
        registration.write_text('{"invocation_id":', encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope
        for scope in _records(report["scopes"])
        if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child["protected_declaration_proofs"])[0]

    assert not report["can_apply"]
    assert proof["reason_code"] == reason_code
    assert _mapping(proof["correlation"])["dispatch_event_observed_emitter_agent_id"] == (
        "agent-architecture"
    )
    expected_counter = "zero" if parent_attempts == 0 else "positive"
    assert (
        _mapping(_mapping(child["scope_emitter_proof"])["correlation"])[
            "parent_dispatch_attempt_condition"
        ]
        == f"{expected_counter}_dispatch_proof_unproven"
    )
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    ("corruption", "reason_code"),
    [
        ("wrong_owner", "scope_controller_invocation_owner_mismatch"),
        ("wrong_lineage", "scope_controller_invocation_lineage_mismatch"),
        ("wrong_branch", "scope_controller_invocation_branch_mismatch"),
    ],
)
@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_legacy_dispatch_rejects_conflicting_scope_controller_registration(
    tmp_path: Path,
    corruption: str,
    reason_code: str,
    parent_attempts: int,
) -> None:
    state_root, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    registration = tmp_path / ".exo" / "agents" / "agent-architecture" / "invocation.json"
    record = json.loads(registration.read_text(encoding="utf-8"))
    if corruption == "wrong_owner":
        record["runtime_agent_id"] = "unrelated-controller"
    elif corruption == "wrong_lineage":
        record["invocation_id"] = "unrelated-invocation"
    elif corruption == "wrong_branch":
        record["branch"] = "main.unrelated"
    registration.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope
        for scope in _records(report["scopes"])
        if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child["protected_declaration_proofs"])[0]

    assert not report["can_apply"]
    assert proof["reason_code"] == reason_code
    assert _mapping(proof["correlation"])["expected"] == "agent-architecture"
    assert _mapping(proof["correlation"])["dispatch_event_observed_emitter_agent_id"] == (
        "agent-architecture"
    )
    assert _tree_bytes(state_root) == before


def test_legacy_dispatch_requires_proven_parent_scope_identity(tmp_path: Path) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
    root_document = json.loads(root_store.path.read_text(encoding="utf-8"))
    root_document["parent_agent_id"] = "unexpected-parent"
    root_store.path.write_text(json.dumps(root_document), encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = next(scope for scope in _records(report["scopes"]) if scope["scope_id"] == "root")
    child = next(
        scope
        for scope in _records(report["scopes"])
        if scope["scope_id"] == "root/sub_tl/architecture"
    )

    assert not report["can_apply"]
    assert _mapping(root["scope_emitter_proof"])["reason_code"] == (
        "scope_controller_root_identity_unproven"
    )
    assert _records(child["protected_declaration_proofs"])[0]["reason_code"] == (
        "scope_controller_parent_identity_unproven"
    )
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    ("corruption", "reason_code"),
    [
        ("missing_event", "legacy_dispatch_event_missing"),
        ("malformed_event", "legacy_dispatch_event_ledger_invalid"),
        ("duplicate_event", "legacy_dispatch_event_ambiguous"),
        ("conflicting_invocation", "legacy_dispatch_result_identity_conflict"),
        ("wrong_branch", "legacy_dispatch_coordinates_mismatch"),
        ("missing_worktree", "legacy_spawn_result_worktree_missing"),
        ("wrong_owner", "legacy_dispatch_coordinates_mismatch"),
        ("wrong_emitter_parent", "legacy_scope_emitter_mismatch"),
        ("wrong_emitter_child", "legacy_scope_emitter_mismatch"),
        ("wrong_emitter_unrelated", "legacy_scope_emitter_mismatch"),
    ],
)
@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_legacy_dispatch_recovery_fails_closed_on_conflicting_or_missing_evidence(
    tmp_path: Path,
    corruption: str,
    reason_code: str,
    parent_attempts: int,
) -> None:
    state_root, _root, child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    ledger = tmp_path / ".exo" / "ledger" / "segments" / "segment-000000000001.jsonl"
    if corruption == "missing_event":
        ledger.unlink()
    elif corruption == "malformed_event":
        ledger.write_text("not-json\n", encoding="utf-8")
    elif corruption == "duplicate_event":
        ledger.write_text(ledger.read_text(encoding="utf-8") * 2, encoding="utf-8")
    elif corruption == "conflicting_invocation":
        _mutate_completed_spawn_result(child_store, "invocation_id", "unrelated-invocation")
    elif corruption == "wrong_branch":
        _mutate_completed_spawn_result(child_store, "branch_name", "main.other")
    elif corruption == "missing_worktree":
        _mutate_completed_spawn_result(child_store, "worktree_path", None)
    elif corruption == "wrong_owner":
        event = json.loads(ledger.read_text(encoding="utf-8"))
        event["data"]["child_agent"] = "unrelated-agent"
        ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
    elif corruption.startswith("wrong_emitter_"):
        emitter = {
            "wrong_emitter_parent": "root",
            "wrong_emitter_child": "agent-completed",
            "wrong_emitter_unrelated": "unrelated-controller",
        }[corruption]
        event = json.loads(ledger.read_text(encoding="utf-8"))
        event["agent_id"] = emitter
        ledger.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item
        for item in _records(report["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )

    assert report["can_apply"] is False
    assert _records(child_report["protected_declaration_proofs"])[0]["reason_code"] == reason_code
    if corruption.startswith("wrong_emitter_"):
        proof = _records(child_report["protected_declaration_proofs"])[0]
        assert _mapping(proof["correlation"])["dispatch_event_expected_emitter_agent_id"] == (
            "agent-architecture"
        )
        assert _mapping(proof["correlation"])["dispatch_event_observed_emitter_agent_id"] == emitter
    assert _tree_bytes(state_root) == before


def test_legacy_dispatch_resolves_sub_tl_controller_alias(tmp_path: Path) -> None:
    alias = "architecture-controller-alias"
    _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        controller_alias=alias,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope
        for scope in _records(report["scopes"])
        if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child["protected_declaration_proofs"])[0]

    assert report["can_apply"]
    assert _mapping(child["scope_emitter_proof"])["agent_id"] == alias
    assert proof["disposition"] == "proven"
    assert _mapping(proof["correlation"])["dispatch_event_expected_emitter_agent_id"] == alias
    assert _mapping(proof["correlation"])["dispatch_event_observed_emitter_agent_id"] == alias


@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_scope_controller_registration_recovers_original_owner_after_aggregate_handoff(
    tmp_path: Path,
    parent_attempts: int,
) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    root = root_store.load()
    controller_branch = "main.agent-architecture"
    controller_worktree = "/tmp/architecture/agent-architecture"
    owner_id = "root:architecture:integration"
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        integration_owner_id=owner_id,
        integration_owner_run_id="architecture",
        integration_owner_branch=controller_branch,
        integration_owner_worktree=controller_worktree,
    )
    slices = {
        **root.slices,
        "architecture": replace(
            root.slices["architecture"],
            dispatch_agent_id=owner_id,
        ),
    }
    root_store.checkpoint(
        _scope_phase(root.recursive_fsm),
        slices,
        root.budgets,
        root.events.last_consumed_offset,
        current_order=root.current_order,
        ordered_stages=root.ordered_stages,
        integration=replace(root.integration, candidates={"architecture": candidate}),
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope
        for scope in _records(report["scopes"])
        if scope["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child["protected_declaration_proofs"])[0]

    assert report["can_apply"]
    assert proof["disposition"] == "proven"
    assert _mapping(proof["correlation"])["scope_controller_agent_id"] == "agent-architecture"
    assert _mapping(proof["correlation"])["parent_dispatch_agent_id"] == owner_id
    assert _mapping(proof["correlation"])["parent_dispatch_owner_record"] == (
        "integration_owner_with_verified_registration"
    )
    assert _mapping(proof["correlation"])["parent_dispatch_attempts"] == parent_attempts
    assert state_root.is_dir()


@pytest.mark.parametrize("parent_attempts", [0, 1])
def test_integration_owner_id_alone_does_not_prove_scope_controller(
    tmp_path: Path,
    parent_attempts: int,
) -> None:
    _state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    root = root_store.load()
    owner_id = "root:architecture:integration"
    slices = {
        **root.slices,
        "architecture": replace(root.slices["architecture"], dispatch_agent_id=owner_id),
    }
    root_store.checkpoint(
        _scope_phase(root.recursive_fsm),
        slices,
        root.budgets,
        root.events.last_consumed_offset,
        current_order=root.current_order,
        ordered_stages=root.ordered_stages,
        integration=root.integration,
    )

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        scope
        for scope in _records(report["scopes"])
        if scope["scope_id"] == "root/sub_tl/architecture"
    )

    assert not report["can_apply"]
    assert _mapping(child["scope_emitter_proof"])["reason_code"] == (
        "scope_controller_parent_owner_conflict"
    )


@pytest.mark.parametrize("parent_attempts", [0, 1])
@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ("intent", "scope_controller_parent_dispatch_unproven"),
        ("never_dispatched", "scope_controller_parent_dispatch_unproven"),
        ("invocation", "scope_controller_parent_dispatch_unproven"),
        ("sequence", "scope_controller_parent_dispatch_unproven"),
        ("branch", "scope_controller_parent_dispatch_unproven"),
        ("worktree", "scope_controller_parent_dispatch_unproven"),
        ("base", "scope_controller_parent_dispatch_unproven"),
        ("record", "scope_controller_parent_dispatch_conflict"),
        ("owner", "scope_controller_parent_owner_conflict"),
    ],
)
def test_parent_dispatch_proof_requires_complete_coordinates_at_any_attempt_count(
    tmp_path: Path,
    parent_attempts: int,
    corruption: str,
    expected_reason: str,
) -> None:
    state_root, root_store, _child_store, *_ = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=parent_attempts,
    )
    state = root_store.load()
    parent_slice = state.slices["architecture"]
    phase = _running_phase(state.recursive_fsm)
    slices = {
        **state.slices,
        "architecture": _corrupt_parent_slice(parent_slice, corruption),
    }
    if corruption == "record":
        records = _running_phase(phase).pending_by_order[1]
        phase = replace(
            phase,
            pending_by_order={
                **_running_phase(phase).pending_by_order,
                1: (replace(records[0], dispatch_intent_id="conflicting-intent"),),
            },
        )
    root_store.checkpoint(
        phase,
        slices,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=state.integration,
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child = next(
        item
        for item in _records(report["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )

    assert not report["can_apply"]
    assert _mapping(child["scope_emitter_proof"])["reason_code"] == expected_reason
    assert _mapping(_mapping(child["scope_emitter_proof"])["correlation"])[
        "parent_dispatch_attempts"
    ] == (parent_attempts)
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize("attempts", [-1, True, "0"])
def test_invalid_parent_attempt_counter_fails_runstore_validation_without_writes(
    tmp_path: Path,
    attempts: object,
) -> None:
    state_root, root_store, _child_store, *_ = _project(tmp_path)
    document = json.loads(root_store.path.read_text(encoding="utf-8"))
    document["slices"]["architecture"]["attempts"] = attempts
    root_store.path.write_text(json.dumps(document), encoding="utf-8")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        analyze_plan_reconciliation(tmp_path)

    assert error.value.code == "runstore_invalid"
    assert _tree_bytes(state_root) == before


@pytest.mark.parametrize(
    ("emitter", "expected_can_apply", "expected_disposition", "expected_reason"),
    [
        ("root", True, "proven", "proven"),
        ("unrelated-controller", False, "blocked", "legacy_scope_emitter_mismatch"),
    ],
)
def test_root_scope_requires_its_proven_controller_emitter(
    tmp_path: Path,
    emitter: str,
    expected_can_apply: bool,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    _direct_root_legacy_project(tmp_path, emitting_controller_id=emitter)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    root = _records(report["scopes"])[0]
    proof = _records(root["protected_declaration_proofs"])[0]

    assert _mapping(root["scope_emitter_proof"])["agent_id"] == "root"
    assert proof["disposition"] == expected_disposition
    assert proof["reason_code"] == expected_reason
    assert _mapping(proof["correlation"])["dispatch_event_expected_emitter_agent_id"] == "root"
    assert _mapping(proof["correlation"])["dispatch_event_observed_emitter_agent_id"] == emitter
    assert report["can_apply"] is expected_can_apply


def test_completed_declaration_restore_blocks_without_spawn_journal(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item
        for item in _records(report["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child_report["protected_declaration_proofs"])[0]

    assert report["can_apply"] is False
    assert proof["disposition"] == "blocked"
    assert proof["reason_code"] == "action_journal_missing"
    assert any(
        item["code"] == "protected_declaration_evidence_unproven"
        for item in _records(report["blockers"])
    )
    assert _tree_bytes(state_root) == before


def test_completed_declaration_restore_blocks_when_spawn_task_conflicts(tmp_path: Path) -> None:
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        spawn_task_override="different dispatch task",
    )
    before = _tree_bytes(state_root)

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item
        for item in _records(report["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )
    proof = _records(child_report["protected_declaration_proofs"])[0]

    assert report["can_apply"] is False
    assert proof["reason_code"] == "spawn_declaration_mismatch"
    assert _tree_bytes(state_root) == before


def test_malformed_spawn_journal_is_reported_as_unproven_evidence(tmp_path: Path) -> None:
    state_root, _root, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
    )
    journal = child_store.run_dir / "action-journal.json"
    journal.write_bytes(b"not-json")

    report = analyze_plan_reconciliation(tmp_path).to_document()
    child_report = next(
        item
        for item in _records(report["scopes"])
        if item["scope_id"] == "root/sub_tl/architecture"
    )

    assert report["can_apply"] is False
    assert _mapping(child_report["action_journal"])["status"] == "invalid"
    assert _records(child_report["protected_declaration_proofs"])[0]["reason_code"] == (
        "action_journal_invalid"
    )
    assert not (state_root / "plan-reconciliations.jsonl").exists()


def test_action_journal_change_invalidates_completed_declaration_proof(tmp_path: Path) -> None:
    state_root, _root, child_store, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
    )
    report = analyze_plan_reconciliation(tmp_path)
    journal = child_store.run_dir / "action-journal.json"
    journal.write_bytes(journal.read_bytes() + b" ")
    before = _tree_bytes(state_root)

    with pytest.raises(PlanRevisionError) as error:
        apply_plan_reconciliation(
            tmp_path,
            "reject stale declaration evidence",
            report.evidence_fingerprint,
            actor="operator",
        )

    assert error.value.code == "reconciliation_evidence_stale"
    assert _tree_bytes(state_root) == before


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
    state_root, _root, _child, _accepted, _candidate = _project(
        tmp_path,
        candidate_completed_task="rewritten completed task",
        accepted_completed_issue_id=None,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
        parent_attempts=0,
    )
    report = analyze_plan_reconciliation(tmp_path)
    assert report.can_apply
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
    state_root, root_store, _child, _accepted, _candidate = _project(
        tmp_path,
        parent_attempts=0,
        include_completed_spawn_journal=True,
        legacy_completed_dispatch=True,
    )
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
    records = (state_root / "plan-reconciliations.jsonl").read_text().splitlines()
    assert len(records) == 1
    audit = json.loads(records[0])
    root_attempts = root_store.load().slices["architecture"].attempts
    assert root_attempts == 0
    child_scope = next(
        item for item in _records(audit["scopes"]) if item["scope_id"] == "root/sub_tl/architecture"
    )
    assert (
        _mapping(_mapping(child_scope["scope_emitter_proof"])["correlation"])[
            "parent_dispatch_attempts"
        ]
        == 0
    )
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
    assert any(
        item["code"] == "materialized_scope_missing" for item in _records(report["blockers"])
    )


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
