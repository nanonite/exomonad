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
        "parent_manifest_scope_id": "root-run",
        "candidate_key": "architecture",
        "child_run_id": "architecture",
        "child_manifest_node_id": "root-run/sub_tl/architecture",
        "child_manifest_scope_id": "root-run/sub_tl/architecture",
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


def _legacy_receipt_document(current: dict[str, object]) -> dict[str, object]:
    legacy = {
        key: value
        for key, value in current.items()
        if key
        not in {
            "parent_manifest_scope_id",
            "candidate_key",
            "child_run_id",
            "child_manifest_node_id",
            "child_manifest_scope_id",
        }
    }
    legacy["child_scope_id"] = current["candidate_key"]
    legacy.pop("receipt_id")
    legacy["receipt_id"] = canonical_digest(approval_identity(legacy))
    return legacy


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
    assert receipt.candidate_key == "architecture"
    assert receipt.child_run_id == "architecture"
    assert receipt.parent_manifest_scope_id == "root-run"
    assert receipt.child_manifest_node_id == "root-run/sub_tl/architecture"
    assert receipt.child_manifest_scope_id == "root-run/sub_tl/architecture"
    assert receipt.to_document() == _receipt_document()


def test_nested_receipt_keeps_runtime_names_separate_from_canonical_scope_ids() -> None:
    document = _receipt_document()
    document.update(
        {
            "parent_run_id": "architecture",
            "parent_scope_path": ["root-run", "architecture"],
            "parent_manifest_scope_id": "root-run/sub_tl/architecture",
            "candidate_key": "component",
            "child_run_id": "component",
            "child_manifest_node_id": "root-run/sub_tl/architecture/sub_tl/component",
            "child_manifest_scope_id": "root-run/sub_tl/architecture/sub_tl/component",
        }
    )
    document["receipt_id"] = canonical_digest(approval_identity(document))

    receipt = validate_receipt_document(document)

    assert receipt.parent_run_id == "architecture"
    assert receipt.parent_manifest_scope_id == "root-run/sub_tl/architecture"
    assert receipt.candidate_key == "component"
    assert receipt.child_manifest_node_id.endswith("/sub_tl/component")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("candidate_key", "sibling"),
        ("child_run_id", "sibling"),
        ("child_manifest_node_id", "root-run/sub_tl/sibling"),
    ],
)
def test_receipt_rejects_conflicting_runtime_and_manifest_identity_fields(
    field: str, replacement: str
) -> None:
    document = _receipt_document()
    document[field] = replacement
    document["receipt_id"] = canonical_digest(approval_identity(document))

    with pytest.raises(AggregateApprovalError):
        validate_receipt_document(document)


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


def test_returning_to_superseded_evidence_appends_a_new_active_decision() -> None:
    coordinates_a = _coordinates(_receipt_document())
    history_a, receipt_a = ensure_parent_aggregate_approval(
        (), coordinates_a, created_at="2026-10-15T22:00:00Z"
    )
    coordinates_b = {**coordinates_a, "ci_status": "neutral"}
    history_b, receipt_b = ensure_parent_aggregate_approval(
        history_a, coordinates_b, created_at="2026-10-15T22:01:00Z"
    )

    history_final, receipt_final = ensure_parent_aggregate_approval(
        history_b, coordinates_a, created_at="2026-10-15T22:02:00Z"
    )
    reloaded = tuple(validate_receipt_document(item.to_document()) for item in history_final)
    replayed_history, replayed_final = ensure_parent_aggregate_approval(
        reloaded, coordinates_a, created_at="2026-10-15T23:00:00Z"
    )

    assert len(history_final) == 3
    assert history_final[:2] == (receipt_a, receipt_b)
    assert receipt_b.supersedes_receipt_id == receipt_a.receipt_id
    assert receipt_final.supersedes_receipt_id == receipt_b.receipt_id
    assert receipt_final.receipt_id != receipt_a.receipt_id
    assert replayed_history == history_final
    assert replayed_final == receipt_final


def test_legacy_scope_receipt_is_preserved_and_renewed_before_merge() -> None:
    current = _receipt_document()
    legacy = _legacy_receipt_document(current)
    legacy_receipt = validate_receipt_document(legacy)

    assert legacy_receipt.to_document() == legacy
    with pytest.raises(AggregateApprovalError, match="must be renewed"):
        approval_reference(legacy_receipt, "epoch-current")

    history, renewed = ensure_parent_aggregate_approval(
        (legacy_receipt,),
        _coordinates(current),
        created_at="2026-10-16T00:00:00Z",
    )

    assert len(history) == 2
    assert history[0].to_document() == legacy
    assert renewed.supersedes_receipt_id == legacy_receipt.receipt_id
    assert renewed.receipt_id != legacy_receipt.receipt_id
    assert validate_receipt_document(renewed.to_document()) == renewed


def test_merge_reference_requires_the_current_controller_epoch() -> None:
    receipt = validate_receipt_document(_receipt_document())

    with pytest.raises(AggregateApprovalError, match="controller epoch is required"):
        approval_reference(receipt, "")


def test_approval_history_survives_runstore_checkpoint_round_trip(tmp_path) -> None:
    current_coordinates = _receipt_document()
    legacy_document = _legacy_receipt_document(current_coordinates)
    legacy_receipt = validate_receipt_document(legacy_document)
    history, receipt = ensure_parent_aggregate_approval(
        (legacy_receipt,),
        _coordinates(current_coordinates),
        created_at="2026-10-16T00:00:00Z",
    )
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
        aggregate_approval_receipts=history,
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
    assert actual.aggregate_approval_receipts == history
    assert actual.active_aggregate_approval_id == receipt.receipt_id
    replayed_history, replayed_receipt = ensure_parent_aggregate_approval(
        actual.aggregate_approval_receipts,
        _coordinates(current_coordinates),
        created_at="2026-10-16T00:00:00Z",
    )
    assert replayed_history == actual.aggregate_approval_receipts
    assert replayed_receipt.receipt_id == actual.active_aggregate_approval_id
