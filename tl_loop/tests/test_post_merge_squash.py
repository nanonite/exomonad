"""Focused validation for structured squash post-merge receipts."""

from __future__ import annotations

import pytest

from tl_loop.loop.driver import _validate_parent_sync_proof


def _arguments() -> dict[str, object]:
    return {
        "child_id": "slice-a",
        "pr_number": 43,
        "repository": "org/repo",
        "parent_branch": "main",
        "merged_head_sha": "pr-head",
        "expected_base_sha": "base-head",
        "lane_epoch": 7,
        "forgejo_pr_number": 43,
        "forgejo_merged": True,
        "forgejo_head_sha": "pr-head",
        "reviewed_pr_head_tree_sha": "tree-a",
    }


def _receipt() -> dict[str, str]:
    return {
        "child_id": "slice-a",
        "pr_number": "43",
        "repository": "org/repo",
        "parent_branch": "main",
        "merged_head_sha": "pr-head",
        "expected_base_sha": "base-head",
        "lane_epoch": "7",
        "parent_commit_sha": "merge-commit",
        "remote_head_sha": "merge-commit",
        "ancestry_proof": "squash-tree-equality",
    }


def _squash_proof() -> dict[str, object]:
    return {
        "kind": "squash",
        "pr_number": 43,
        "pr_head_sha": "pr-head",
        "merge_commit_sha": "merge-commit",
        "pr_head_tree_sha": "tree-a",
        "merge_commit_tree_sha": "tree-a",
        "reviewed_pr_head_tree_sha": "tree-a",
        "forgejo_merged": True,
        "forgejo_head_sha": "pr-head",
        "forgejo_pr_number": 43,
    }


def test_squash_receipt_accepts_exact_forgejo_and_tree_binding() -> None:
    _validate_parent_sync_proof(
        {**_receipt(), "merge_integration_proof": _squash_proof()},
        _arguments(),
        _receipt(),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("merge_commit_tree_sha", "tree-b"),
        ("forgejo_head_sha", "other-head"),
        ("forgejo_pr_number", 44),
        ("forgejo_merged", False),
    ],
)
def test_squash_receipt_rejects_changed_authority_or_tree(field: str, value: object) -> None:
    proof = _squash_proof()
    proof[field] = value
    with pytest.raises(ValueError):
        _validate_parent_sync_proof(
            {**_receipt(), "merge_integration_proof": proof},
            _arguments(),
            _receipt(),
        )


def test_tree_equality_without_forgejo_binding_is_rejected() -> None:
    arguments = _arguments()
    for key in (
        "forgejo_pr_number",
        "forgejo_merged",
        "forgejo_head_sha",
        "reviewed_pr_head_tree_sha",
    ):
        arguments.pop(key)
    with pytest.raises(ValueError, match="PR binding"):
        _validate_parent_sync_proof(
            {**_receipt(), "merge_integration_proof": _squash_proof()},
            arguments,
            _receipt(),
        )


def test_ancestry_receipt_keeps_the_existing_proof_contract() -> None:
    arguments = _arguments()
    for key in (
        "forgejo_pr_number",
        "forgejo_merged",
        "forgejo_head_sha",
        "reviewed_pr_head_tree_sha",
    ):
        arguments.pop(key)
    receipt = _receipt()
    receipt["ancestry_proof"] = "ancestor:pr-head->merge-commit"
    proof = {
        "kind": "ancestry",
        "pr_number": 43,
        "pr_head_sha": "pr-head",
        "merge_commit_sha": "merge-commit",
        "ancestry": "ancestor:pr-head->merge-commit",
    }
    _validate_parent_sync_proof(
        {**receipt, "merge_integration_proof": proof},
        arguments,
        receipt,
    )
