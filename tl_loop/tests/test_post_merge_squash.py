"""Focused validation for structured squash post-merge receipts."""

from __future__ import annotations

from typing import ClassVar

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import (
    TLLoopConfig,
    _adopt_post_merge_slice,
    _advance_post_merge_boundary,
    _refresh_post_merge_evidence,
    _remote_reconcile_effect,
    _validate_parent_sync_proof,
    _watcher_merge_evidence,
    _watcher_result_observation,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.loop.observation import WatcherObservation
from tl_loop.tests.test_startup_reconciliation import _load_state, _review_recovery_state


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
        "forgejo_merge_commit_sha": "merge-commit",
        "forgejo_merge_commit_tree_sha": "merged-tree",
        "prospective_merge_tree_sha": "merged-tree",
        "reviewed_pr_head_tree_sha": "pr-tree",
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
        "ancestry_proof": "squash-tree-match",
    }


def _squash_proof() -> dict[str, object]:
    return {
        "kind": "squash",
        "pr_number": 43,
        "pr_head_sha": "pr-head",
        "merge_commit_sha": "merge-commit",
        "pr_head_tree_sha": "pr-tree",
        "merge_commit_tree_sha": "merged-tree",
        "reviewed_pr_head_tree_sha": "pr-tree",
        "prospective_merge_tree_sha": "merged-tree",
        "forgejo_merged": True,
        "forgejo_head_sha": "pr-head",
        "forgejo_merge_commit_sha": "merge-commit",
        "forgejo_merge_commit_tree_sha": "merged-tree",
        "forgejo_pr_number": 43,
    }


def test_watcher_evidence_persists_and_drives_squash_remote_reconcile(tmp_path) -> None:
    watcher_result = ToolResult.from_raw(
        {
            "success": True,
            "result": {
                "pr_number": 43,
                "found": True,
                "merged": True,
                "head_sha": "pr-head",
                "base_sha": "parent-before",
                "base_branch": "main",
                "pr_state": "closed",
                "pr_head_tree_sha": "pr-tree",
                "merge_tree_sha": "merged-tree",
                "merge_commit_sha": "merge-commit",
                "merge_commit_tree_sha": "merged-tree",
            },
        }
    )
    observation = _watcher_result_observation(watcher_result)
    assert isinstance(observation, WatcherObservation)
    watcher_evidence = _watcher_merge_evidence(observation)
    assert watcher_evidence["pr_head_tree_sha"] == "pr-tree"
    assert watcher_evidence["merge_commit_tree_sha"] == "merged-tree"
    assert watcher_evidence["merge_tree_sha"] == "merged-tree"
    evidence = {
        **watcher_evidence,
        "repository": "org/repo",
        "parent_branch": "main",
        "lane_epoch": 7,
    }

    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)

    class RefreshClient:
        def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
            assert pr_number == 99
            return watcher_result

    refreshed_evidence = _refresh_post_merge_evidence(
        state.slices["slice-a"], TLLoopConfig(active=True), RefreshClient()
    )
    assert refreshed_evidence == watcher_evidence

    adopted = _adopt_post_merge_slice(
        state.slices["slice-a"],
        state,
        43,
        "merge-journal",
        "post_merge_recovery",
        evidence,
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": adopted},
        state.budgets,
        state.events.last_consumed_offset,
    )
    persisted_evidence = dict(store.load().slices["slice-a"].post_merge.evidence)

    class RemoteReconcileClient:
        calls: ClassVar[list[dict[str, object]]] = []

        def post_merge_parent_sync(self, **arguments: object) -> ToolResult:
            self.calls.append(arguments)
            proof = {
                "kind": "squash",
                "pr_number": arguments["pr_number"],
                "pr_head_sha": arguments["merged_head_sha"],
                "merge_commit_sha": "merge-commit",
                "pr_head_tree_sha": arguments["reviewed_pr_head_tree_sha"],
                "merge_commit_tree_sha": arguments["forgejo_merge_commit_tree_sha"],
                "prospective_merge_tree_sha": arguments["prospective_merge_tree_sha"],
                "reviewed_pr_head_tree_sha": arguments["reviewed_pr_head_tree_sha"],
                "forgejo_merged": True,
                "forgejo_head_sha": arguments["forgejo_head_sha"],
                "forgejo_pr_number": arguments["forgejo_pr_number"],
                "forgejo_merge_commit_sha": arguments["forgejo_merge_commit_sha"],
                "forgejo_merge_commit_tree_sha": arguments["forgejo_merge_commit_tree_sha"],
            }
            return ToolResult.from_raw(
                {
                    "success": True,
                    "result": {
                        **arguments,
                        "parent_commit_sha": "merge-commit",
                        "remote_head_sha": "merge-commit",
                        "ancestry_proof": "squash-tree-match",
                        "merge_integration_proof": proof,
                    },
                }
            )

        def post_merge_remote_reconcile(self, **arguments: object) -> ToolResult:
            self.calls.append(arguments)
            proof = {
                "kind": "squash",
                "pr_number": arguments["pr_number"],
                "pr_head_sha": arguments["merged_head_sha"],
                "merge_commit_sha": arguments["expected_base_sha"],
                "pr_head_tree_sha": arguments["reviewed_pr_head_tree_sha"],
                "merge_commit_tree_sha": arguments["forgejo_merge_commit_tree_sha"],
                "prospective_merge_tree_sha": arguments["prospective_merge_tree_sha"],
                "reviewed_pr_head_tree_sha": arguments["reviewed_pr_head_tree_sha"],
                "forgejo_merged": True,
                "forgejo_head_sha": arguments["forgejo_head_sha"],
                "forgejo_pr_number": arguments["forgejo_pr_number"],
                "forgejo_merge_commit_sha": arguments["forgejo_merge_commit_sha"],
                "forgejo_merge_commit_tree_sha": arguments["forgejo_merge_commit_tree_sha"],
            }
            return ToolResult.from_raw(
                {
                    "success": True,
                    "result": {
                        **arguments,
                        "parent_commit_sha": "rebuilt-bookkeeping",
                        "rebuilt_commit_sha": "rebuilt-bookkeeping",
                        "remote_head_sha": "advanced-parent",
                        "new_base_sha": "advanced-parent",
                        "remote_ancestry_proof": ("ancestor:advanced-parent->rebuilt-bookkeeping"),
                        "ancestry_proof": "squash-tree-match",
                        "merge_integration_proof": proof,
                    },
                }
            )

    client = RemoteReconcileClient()
    journal = EffectJournal("reconcile", tmp_path / "action-journal.json")
    arguments, payload = _remote_reconcile_effect(
        state.slices["slice-a"],
        43,
        persisted_evidence,
        TLLoopConfig(active=True, ledger_run_id="reconcile"),
        client,
        journal,
        expected_base_sha="merge-commit",
    )
    assert arguments["forgejo_merge_commit_sha"] == "merge-commit"
    assert arguments["forgejo_merge_commit_tree_sha"] == "merged-tree"
    assert arguments["prospective_merge_tree_sha"] == "merged-tree"
    assert arguments["reviewed_pr_head_tree_sha"] == "pr-tree"
    assert payload["new_base_sha"] == "advanced-parent"
    assert len(journal.confirmed_entries("post_merge_remote_reconcile", "slice-a")) == 1

    advanced = _advance_post_merge_boundary(
        state,
        "slice-a",
        43,
        TLLoopConfig(active=True, ledger_run_id="reconcile"),
        client,
        journal,
        store,
    )
    assert advanced.slices["slice-a"].post_merge.phase.value == "parent_branch_synced"
    assert journal.pending_entries() == []
    assert len(journal.confirmed_entries("post_merge_parent_sync", "slice-a")) == 1


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
        ("forgejo_merge_commit_tree_sha", "tree-b"),
        ("prospective_merge_tree_sha", "other-prospective-tree"),
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
        "forgejo_merge_commit_sha",
        "forgejo_merge_commit_tree_sha",
        "prospective_merge_tree_sha",
        "reviewed_pr_head_tree_sha",
    ):
        arguments.pop(key)
    with pytest.raises(ValueError, match="authoritative Forgejo evidence"):
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
        "forgejo_merge_commit_sha",
        "forgejo_merge_commit_tree_sha",
        "prospective_merge_tree_sha",
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
