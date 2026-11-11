"""Emit an aggregate approval artifact through the production Python producers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tl_loop.loop import driver
from tl_loop.loop.driver import SubTLTask, TLLoopConfig, WorkPlan
from tl_loop.ordered import IntegrationReadinessEvidence
from tl_loop.state.aggregate_approval import (
    approval_reference,
    ensure_parent_aggregate_approval,
    validate_receipt_document,
)
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.fsm.phase import TLPhase
from tl_loop.state.schema import (
    BudgetLedger,
    EventCursor,
    FSMState,
    RepositoryIdentity,
    RunState,
    SCHEMA_VERSION,
)
from tl_loop.state.store import RunStore


def _required_string(request: dict[str, object], key: str) -> str:
    value = request[key]
    if not isinstance(value, str):
        raise ValueError(f"request field {key!r} must be a string")
    return value


def _string_list(request: dict[str, object], key: str) -> list[str]:
    value = request[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"request field {key!r} must be a string array")
    return list(value)


def _required_integer(request: dict[str, object], key: str) -> int:
    value = request[key]
    if type(value) is not int:
        raise ValueError(f"request field {key!r} must be an integer")
    return value


def _manifest_chain(request: dict[str, object]) -> tuple[PlanManifest, dict[str, PlanManifest]]:
    parent_path = _string_list(request, "parent_scope_path")
    parent_branches = _string_list(request, "ancestor_child_branches")
    parent_branch = _required_string(request, "parent_branch")
    child_run_id = _required_string(request, "child_run_id")
    child_branch = _required_string(request, "child_branch")
    child_task = _required_string(request, "child_task")
    contract = {
        "aggregate_pr_required": True,
        "base_revalidation_required": True,
        "aggregate_review_owner": "none",
        "leaf_review_owner": "leaf",
        "aggregate_repair_owner": "aggregate",
        "merge_strategy": "merge",
        "aggregate_approval_mode": "parent",
    }
    plan: dict[str, object] = {
        "sub_tls": [
            {
                "name": child_run_id,
                "order": 1,
                "branch": child_branch,
                "integration": contract,
                "plan": {"leaves": [{"name": "issue-slice", "task": child_task}]},
            }
        ]
    }
    for name, branch in reversed(list(zip(parent_path[1:], parent_branches, strict=True))):
        plan = {
            "sub_tls": [
                {
                    "name": name,
                    "order": 1,
                    "branch": branch,
                    "integration": contract,
                    "plan": plan,
                }
            ]
        }
    root = build_plan_manifest(
        plan,
        scope_id=parent_path[0],
        role="root",
        owned_branch=_required_string(request, "root_branch"),
        manifest_revision=2,
    )
    chain = {parent_path[0]: root}
    current = root
    for run_name in parent_path[1:]:
        node = next(
            item for item in current.nodes if item.kind == "sub_tl" and item.name == run_name
        )
        current = current.child_manifests[node.node_id]
        chain[run_name] = current
    if current.owned_branch != parent_branch:
        raise ValueError("generated parent manifest branch does not match the requested scope")
    PlanManifest.from_document(root.to_document())
    PlanManifest.from_document(current.to_document())
    return root, chain


def produce(request: dict[str, object]) -> dict[str, object]:
    parent_path = _string_list(request, "parent_scope_path")
    parent_run_id = _required_string(request, "parent_run_id")
    child_run_id = _required_string(request, "child_run_id")
    repository_url = _required_string(request, "repository_url")
    root, chain = _manifest_chain(request)
    parent_manifest = chain[parent_run_id]
    node = next(
        item
        for item in parent_manifest.nodes
        if item.kind == "sub_tl" and item.name == child_run_id
    )
    child_manifest = parent_manifest.child_manifests[node.node_id]
    child_manifest_digest = child_manifest.digest
    if child_manifest_digest is None:
        raise ValueError("generated child manifest has no digest")
    leaves = sorted(leaf.node_id for leaf in child_manifest.nodes if leaf.kind == "leaf")
    readiness = IntegrationReadinessEvidence(
        child_run_id=child_run_id,
        child_manifest_digest=child_manifest_digest,
        aggregate_pr_number=_required_integer(request, "pr_number"),
        aggregate_head_sha=_required_string(request, "head_sha"),
        aggregate_base_sha=_required_string(request, "base_sha"),
        aggregate_patch_digest=_required_string(request, "patch_digest"),
        aggregate_head_tree_sha=_required_string(request, "head_tree_sha"),
        reviewed_leaf_ids=tuple(leaves),
        lineage_commit_shas=tuple(sorted(_string_list(request, "lineage_commit_shas"))),
        lineage_proof_digest=_required_string(request, "lineage_proof_digest"),
        receipt_digest=_required_string(request, "readiness_receipt_digest"),
    )
    task = SubTLTask(name=child_run_id, plan=WorkPlan())
    config = TLLoopConfig(
        agent_id=_required_string(request, "parent_agent_id"),
        branch=_required_string(request, "parent_branch"),
        depth=len(parent_path) - 1,
    )
    scope_root = Path(_required_string(request, "scope_root"))
    parent_directory = scope_root.joinpath(*parent_path[:-1])
    state_root = parent_directory
    store = RunStore(parent_run_id, state_root)
    state = RunState(
        version=SCHEMA_VERSION,
        revision=0,
        run_id=parent_run_id,
        fsm=FSMState(TLPhase.TLPlanning, ()),
        slices={},
        budgets=BudgetLedger(tokens=0, wall_seconds=0),
        gates=(),
        events=EventCursor(last_consumed_offset=0),
        plan_manifest=parent_manifest,
        owner_branch=_required_string(request, "parent_branch"),
        controller_epoch=_required_string(request, "controller_epoch"),
        repository_identity=RepositoryIdentity(repository_url),
    )
    canonical_parent_path = driver._controller_scope_path(store, config.depth)
    policy_digest = driver._approval_policy_digest(parent_manifest, node, readiness)
    scope_coordinates = driver._aggregate_scope_coordinates(
        task,
        state,
        config,
        parent_manifest,
        node,
        readiness,
        canonical_parent_path,
        policy_digest,
    )
    compare_coordinates = driver._aggregate_compare_coordinates(
        readiness,
        readiness.aggregate_pr_number,
        readiness.aggregate_head_sha,
        readiness.aggregate_head_tree_sha,
        readiness.aggregate_base_sha,
        readiness.aggregate_patch_digest,
        _required_string(request, "merge_tree_sha"),
        _required_string(request, "ci_status"),
    )
    coordinates = {**scope_coordinates, **compare_coordinates}
    history_a, receipt_a = ensure_parent_aggregate_approval(
        (), coordinates, created_at="2026-10-15T22:00:00Z"
    )
    history_b, receipt_b = ensure_parent_aggregate_approval(
        history_a,
        {**coordinates, "ci_status": "neutral"},
        created_at="2026-10-15T22:01:00Z",
    )
    history, receipt = ensure_parent_aggregate_approval(
        history_b, coordinates, created_at="2026-10-15T22:02:00Z"
    )
    retained = tuple(validate_receipt_document(item.to_document()) for item in history)
    replay_history, replay_receipt = ensure_parent_aggregate_approval(
        retained, coordinates, created_at="2026-10-15T23:00:00Z"
    )
    if (
        len(history_a) != 1
        or len(history_b) != 2
        or len(history) != 3
        or history_b[-1] != receipt_b
        or receipt.supersedes_receipt_id != receipt_b.receipt_id
        or replay_history != history
        or replay_receipt != receipt
        or receipt.receipt_id == receipt_a.receipt_id
    ):
        raise ValueError("Python approval replay failed to preserve latest A-B-A history")

    checkpoints: list[dict[str, object]] = []
    for index, run_name in enumerate(parent_path):
        manifest = chain[run_name]
        checkpoint = {
            "version": 5,
            "revision": 1,
            "run_id": run_name,
            "depth": index,
            "owner_branch": manifest.owned_branch,
            "controller_epoch": _required_string(request, "controller_epoch"),
            "repository_identity": {"remote_url": repository_url},
            "plan_manifest": manifest.to_document(),
            "integration": {"candidates": {}},
        }
        if run_name == parent_run_id:
            checkpoint["integration"] = {
                "candidates": {
                    _required_string(scope_coordinates, "candidate_key"): {
                        "lifecycle": "MERGING",
                        "aggregate_pr_number": receipt.pr_number,
                        "aggregate_head_sha": receipt.head_sha,
                        "aggregate_patch_digest": receipt.patch_digest,
                        "aggregate_original_base_sha": receipt.base_sha,
                        "integration_owner_id": f"{parent_run_id}:{child_run_id}:integration",
                        "integration_owner_run_id": child_run_id,
                        "integration_owner_branch": node.owned_branch,
                        "integration_owner_worktree": _required_string(request, "worktree"),
                        "head_sha": receipt.head_sha,
                        "patch_digest": receipt.patch_digest,
                        "validated_base_sha": receipt.base_sha,
                        "merge_tree_sha": receipt.merge_tree_sha,
                        "ci_status": receipt.ci_status,
                        "aggregate_approval_receipts": [item.to_document() for item in history],
                        "active_aggregate_approval_id": receipt.receipt_id,
                    }
                }
            }
        checkpoints.append({"scope_path": parent_path[: index + 1], "state": checkpoint})
    return {
        "checkpoints": checkpoints,
        "reference": approval_reference(receipt, _required_string(request, "controller_epoch")),
        "superseded_reference": approval_reference(
            receipt_a, _required_string(request, "controller_epoch")
        ),
        "superseded_receipt_id": receipt_a.receipt_id,
        "receipt_id": receipt.receipt_id,
        "parent_manifest_scope_id": parent_manifest.scope_id,
        "child_manifest_node_id": node.node_id,
        "child_manifest_scope_id": child_manifest.scope_id,
        "root_manifest_digest": root.digest,
    }


if __name__ == "__main__":
    raw_request = json.load(sys.stdin)
    if not isinstance(raw_request, dict) or not all(
        isinstance(key, str) for key in raw_request
    ):
        raise ValueError("aggregate approval request must be a string-keyed object")
    request = {key: value for key, value in raw_request.items()}
    print(json.dumps(produce(request), sort_keys=True, separators=(",", ":")))
