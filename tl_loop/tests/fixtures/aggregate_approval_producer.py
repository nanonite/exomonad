"""Emit an aggregate approval artifact through the production Python producers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from tl_loop.loop import driver
from tl_loop.state.aggregate_approval import (
    approval_reference,
    ensure_parent_aggregate_approval,
    validate_receipt_document,
)
from tl_loop.state.plan_manifest import PlanManifest, build_plan_manifest
from tl_loop.state.store import RunStore


def _manifest_chain(request: dict[str, object]) -> tuple[PlanManifest, dict[str, PlanManifest]]:
    parent_path = cast(list[str], request["parent_scope_path"])
    parent_branches = cast(list[str], request["ancestor_child_branches"])
    parent_branch = cast(str, request["parent_branch"])
    child_run_id = cast(str, request["child_run_id"])
    child_branch = cast(str, request["child_branch"])
    child_task = cast(str, request["child_task"])
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
        owned_branch=cast(str, request["root_branch"]),
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
    parent_path = cast(list[str], request["parent_scope_path"])
    parent_run_id = cast(str, request["parent_run_id"])
    child_run_id = cast(str, request["child_run_id"])
    repository_url = cast(str, request["repository_url"])
    root, chain = _manifest_chain(request)
    parent_manifest = chain[parent_run_id]
    node = next(
        item
        for item in parent_manifest.nodes
        if item.kind == "sub_tl" and item.name == child_run_id
    )
    child_manifest = parent_manifest.child_manifests[node.node_id]
    leaves = sorted(leaf.node_id for leaf in child_manifest.nodes if leaf.kind == "leaf")
    readiness = SimpleNamespace(
        child_manifest_digest=child_manifest.digest,
        receipt_digest=cast(str, request["readiness_receipt_digest"]),
        lineage_proof_digest=cast(str, request["lineage_proof_digest"]),
        lineage_commit_shas=sorted(cast(list[str], request["lineage_commit_shas"])),
        reviewed_leaf_ids=leaves,
    )
    state = SimpleNamespace(
        run_id=parent_run_id,
        repository_identity=SimpleNamespace(remote_url=repository_url),
        plan_manifest=parent_manifest,
        controller_epoch=cast(str, request["controller_epoch"]),
        owner_branch=cast(str, request["parent_branch"]),
    )
    task = SimpleNamespace(
        name=child_run_id,
        integration=SimpleNamespace(aggregate_approval_mode="parent"),
    )
    config = SimpleNamespace(
        agent_id=cast(str, request["parent_agent_id"]),
        branch=cast(str, request["parent_branch"]),
        depth=len(parent_path) - 1,
    )
    scope_root = Path(cast(str, request["scope_root"]))
    parent_directory = scope_root.joinpath(*parent_path[:-1])
    store = RunStore(parent_run_id, parent_directory)
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
        cast(int, request["pr_number"]),
        cast(str, request["head_sha"]),
        cast(str, request["head_tree_sha"]),
        cast(str, request["base_sha"]),
        cast(str, request["patch_digest"]),
        cast(str, request["merge_tree_sha"]),
        cast(str, request["ci_status"]),
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
            "controller_epoch": cast(str, request["controller_epoch"]),
            "repository_identity": {"remote_url": repository_url},
            "plan_manifest": manifest.to_document(),
            "integration": {"candidates": {}},
        }
        if run_name == parent_run_id:
            checkpoint["integration"] = {
                "candidates": {
                    cast(str, scope_coordinates["candidate_key"]): {
                        "lifecycle": "MERGING",
                        "aggregate_pr_number": receipt.pr_number,
                        "aggregate_head_sha": receipt.head_sha,
                        "aggregate_patch_digest": receipt.patch_digest,
                        "aggregate_original_base_sha": receipt.base_sha,
                        "integration_owner_id": f"{parent_run_id}:{child_run_id}:integration",
                        "integration_owner_run_id": child_run_id,
                        "integration_owner_branch": node.owned_branch,
                        "integration_owner_worktree": cast(str, request["worktree"]),
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
        "reference": approval_reference(receipt, cast(str, request["controller_epoch"])),
        "superseded_reference": approval_reference(
            receipt_a, cast(str, request["controller_epoch"])
        ),
        "superseded_receipt_id": receipt_a.receipt_id,
        "receipt_id": receipt.receipt_id,
        "parent_manifest_scope_id": parent_manifest.scope_id,
        "child_manifest_node_id": node.node_id,
        "child_manifest_scope_id": child_manifest.scope_id,
        "root_manifest_digest": root.digest,
    }


if __name__ == "__main__":
    print(json.dumps(produce(json.load(sys.stdin)), sort_keys=True, separators=(",", ":")))
