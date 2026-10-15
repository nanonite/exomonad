from __future__ import annotations

import pytest

from tl_loop.fsm.phase import TLPhase
from tl_loop.loop.driver import WorkPlan
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.aggregate_approval import (
    AggregateApprovalError,
    UnsupportedAggregateApprovalMode,
    approval_identity,
    approval_reference,
    canonical_digest,
    ensure_parent_aggregate_approval,
    parse_aggregate_approval_mode,
    validate_receipt_document,
)
from tl_loop.state.plan_manifest import build_plan_manifest
from tl_loop.state.schema import (
    BudgetLedger,
    FSMState,
    IntegrationCandidateState,
    IntegrationRuntimeState,
)
from tl_loop.state.store import RunStore, load
from tl_loop.tests.checkpoint_fixtures import create


def _receipt_document() -> dict[str, object]:
    document: dict[str, object] = {
        "receipt_id": "",
        "policy_mode": "parent",
        "parent_run_id": "root-run",
        "parent_scope_path": ["root-run"],
        "child_scope_id": "architecture",
        "parent_agent_id": "root",
        "parent_branch": "main",
        "repository_url": "https://forge.example/org/project.git",
        "plan_manifest_digest": "a" * 64,
        "plan_manifest_revision": 2,
        "approval_policy_digest": "6" * 64,
        "child_manifest_digest": "b" * 64,
        "pr_number": 46,
        "head_sha": "c" * 40,
        "head_tree_sha": "d" * 40,
        "base_sha": "e" * 40,
        "patch_digest": "f" * 64,
        "merge_tree_sha": "1" * 40,
        "ci_status": "success",
        "readiness_receipt_digest": "2" * 64,
        "lineage_proof_digest": "3" * 64,
        "lineage_commit_shas": ["4" * 40, "5" * 40],
        "reviewed_leaf_ids": ["leaf-a"],
        "created_at": "2026-10-15T22:00:00Z",
        "supersedes_receipt_id": None,
    }
    document["receipt_id"] = canonical_digest(approval_identity(document))
    return document


def test_omitted_and_explicit_parent_mode_are_compatible_defaults() -> None:
    assert parse_aggregate_approval_mode() == "parent"
    assert parse_aggregate_approval_mode("parent") == "parent"

    implicit = WorkPlan.from_mapping({"sub_tls": [{"name": "stage", "plan": {}}]})
    explicit = WorkPlan.from_mapping(
        {
            "sub_tls": [
                {"name": "stage", "plan": {}, "integration": {"aggregate_approval_mode": "parent"}}
            ]
        }
    )
    assert implicit.sub_tls[0].integration.aggregate_approval_mode == "parent"
    assert explicit.sub_tls[0].integration.aggregate_approval_mode == "parent"
    implicit_manifest = build_plan_manifest(
        {"sub_tls": [{"name": "stage", "order": 1, "plan": {}}]},
        scope_id="root",
    )
    explicit_manifest = build_plan_manifest(
        {
            "sub_tls": [
                {
                    "name": "stage",
                    "order": 1,
                    "plan": {},
                    "integration": {"aggregate_approval_mode": "parent"},
                }
            ]
        },
        scope_id="root",
    )
    assert implicit_manifest.digest == explicit_manifest.digest


def test_reserved_reviewer_mode_is_rejected_before_dispatch() -> None:
    with pytest.raises(UnsupportedAggregateApprovalMode, match="reserved"):
        parse_aggregate_approval_mode("reviewer")
    with pytest.raises(UnsupportedAggregateApprovalMode, match="reserved"):
        WorkPlan.from_mapping(
            {
                "sub_tls": [
                    {
                        "name": "stage",
                        "plan": {},
                        "integration": {"aggregate_approval_mode": "reviewer"},
                    }
                ]
            }
        )


def test_unknown_approval_mode_is_rejected() -> None:
    with pytest.raises(AggregateApprovalError, match="must be 'parent'"):
        parse_aggregate_approval_mode("skip")


def test_aggregate_approval_receipt_round_trips_and_binds_compare_evidence() -> None:
    receipt = validate_receipt_document(_receipt_document())

    assert receipt.receipt_id == _receipt_document()["receipt_id"]
    assert receipt.pr_number == 46
    assert receipt.head_sha == "c" * 40
    assert receipt.base_sha == "e" * 40
    assert receipt.lineage_commit_shas == ("4" * 40, "5" * 40)
    assert receipt.to_document() == _receipt_document()


def test_forged_or_stale_receipt_coordinates_fail_content_binding() -> None:
    forged = _receipt_document()
    forged["head_sha"] = "9" * 40

    with pytest.raises(AggregateApprovalError, match="ID does not match"):
        validate_receipt_document(forged)


@pytest.mark.parametrize("ci_status", ["pending", "failure", "unknown"])
def test_receipt_cannot_approve_without_mergeable_ci(ci_status: str) -> None:
    document = _receipt_document()
    document["ci_status"] = ci_status
    document["receipt_id"] = canonical_digest(approval_identity(document))

    with pytest.raises(AggregateApprovalError, match="CI must be success or neutral"):
        validate_receipt_document(document)


def test_receipt_requires_declared_parent_scope_path() -> None:
    document = _receipt_document()
    document["parent_scope_path"] = ["other-run"]
    document["receipt_id"] = canonical_digest(approval_identity(document))

    with pytest.raises(AggregateApprovalError, match="must end at parent_run_id"):
        validate_receipt_document(document)


def _coordinates(document: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"receipt_id", "policy_mode", "created_at", "supersedes_receipt_id"}
    }


def test_repeated_parent_decision_reuses_one_immutable_receipt() -> None:
    first_history, first = ensure_parent_aggregate_approval(
        (), _coordinates(_receipt_document()), created_at="2026-10-15T22:00:00Z"
    )
    replay_history, replay = ensure_parent_aggregate_approval(
        first_history, _coordinates(_receipt_document()), created_at="2026-10-16T00:00:00Z"
    )

    assert replay_history == first_history
    assert replay == first


@pytest.mark.parametrize("field", ["head_sha", "base_sha", "approval_policy_digest"])
def test_changed_aggregate_evidence_or_policy_appends_a_linked_decision(field: str) -> None:
    history, first = ensure_parent_aggregate_approval(
        (), _coordinates(_receipt_document()), created_at="2026-10-15T22:00:00Z"
    )
    changed = _receipt_document()
    changed[field] = "9" * (40 if field in {"head_sha", "base_sha"} else 64)
    next_history, replacement = ensure_parent_aggregate_approval(
        history, _coordinates(changed), created_at="2026-10-16T00:00:00Z"
    )

    assert len(next_history) == 2
    assert next_history[0] == first
    assert replacement.supersedes_receipt_id == first.receipt_id
    assert replacement.receipt_id != first.receipt_id


def test_merge_reference_requires_the_current_controller_epoch() -> None:
    receipt = validate_receipt_document(_receipt_document())

    with pytest.raises(AggregateApprovalError, match="controller epoch is required"):
        approval_reference(receipt, "")


def test_approval_history_survives_runstore_checkpoint_round_trip(tmp_path) -> None:
    receipt = validate_receipt_document(_receipt_document())
    store = RunStore("root-run", tmp_path)
    create("root-run", {}, root_dir=tmp_path)
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.READY_FOR_INTEGRATION,
        aggregate_pr_number=46,
        aggregate_head_sha=receipt.head_sha,
        aggregate_patch_digest=receipt.patch_digest,
        aggregate_original_base_sha=receipt.base_sha,
        head_sha=receipt.head_sha,
        patch_digest=receipt.patch_digest,
        validated_base_sha=receipt.base_sha,
        merge_tree_sha=receipt.merge_tree_sha,
        ci_status="success",
        aggregate_approval_receipts=(receipt,),
        active_aggregate_approval_id=receipt.receipt_id,
    )
    store.checkpoint(
        FSMState(TLPhase.TLPlanning, ()),
        {},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
        integration=IntegrationRuntimeState(candidates={"architecture": candidate}),
    )

    restored = load(store.path)
    actual = restored.integration.candidates["architecture"]
    assert actual.aggregate_approval_receipts == (receipt,)
    assert actual.active_aggregate_approval_id == receipt.receipt_id
    replayed_history, replayed_receipt = ensure_parent_aggregate_approval(
        actual.aggregate_approval_receipts,
        _coordinates(_receipt_document()),
        created_at="2026-10-16T00:00:00Z",
    )
    assert replayed_history == actual.aggregate_approval_receipts
    assert replayed_receipt.receipt_id == actual.active_aggregate_approval_id
