"""Focused validation for structured squash post-merge receipts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from tl_loop.client.effects import ToolResult
from tl_loop.loop.driver import (
    TLLoopConfig,
    _adopt_post_merge_slice,
    _advance_post_merge_boundary,
    _attach_forgejo_merge_evidence,
    _forgejo_merge_evidence,
    _reconcile_merged_slice,
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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _squash_git_history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "squash-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "branch", "-M", "main")
    (repo / "base.txt").write_text("base\\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "feature")
    (repo / "feature.txt").write_text("feature\\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    pr_head = _git(repo, "rev-parse", "HEAD")
    pr_head_tree = _git(repo, "rev-parse", f"{pr_head}^{{tree}}")
    _git(repo, "switch", "main")
    _git(repo, "merge", "--squash", "feature")
    _git(repo, "commit", "-m", "squash")
    merge_commit = _git(repo, "rev-parse", "HEAD")
    merge_commit_tree = _git(repo, "rev-parse", f"{merge_commit}^{{tree}}")
    historical_prospective = _git(repo, "merge-tree", "--write-tree", base, pr_head)
    (repo / "later.txt").write_text("later parent change\\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "advance parent")
    advanced_parent = _git(repo, "rev-parse", "HEAD")
    current_prospective = _git(repo, "merge-tree", "--write-tree", advanced_parent, pr_head)
    assert historical_prospective == merge_commit_tree
    assert historical_prospective == pr_head_tree
    assert current_prospective != historical_prospective
    return repo, {
        "base": base,
        "pr_head": pr_head,
        "pr_head_tree": pr_head_tree,
        "merge_commit": merge_commit,
        "merge_commit_tree": merge_commit_tree,
        "historical_prospective": historical_prospective,
        "advanced_parent": advanced_parent,
        "current_prospective": current_prospective,
    }


def test_advanced_parent_refresh_preserves_historical_squash_tree(tmp_path: Path) -> None:
    _, trees = _squash_git_history(tmp_path)

    def watcher_result(base_sha: str, merge_tree_sha: str) -> ToolResult:
        return ToolResult.from_raw(
            {
                "success": True,
                "result": {
                    "pr_number": 99,
                    "found": True,
                    "merged": True,
                    "head_sha": trees["pr_head"],
                    "base_sha": base_sha,
                    "base_branch": "main",
                    "pr_state": "closed",
                    "pr_head_tree_sha": trees["pr_head_tree"],
                    "merge_tree_sha": merge_tree_sha,
                    "merge_commit_sha": trees["merge_commit"],
                    "merge_commit_tree_sha": trees["merge_commit_tree"],
                },
            }
        )

    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)

    class RefreshClient:
        def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
            assert pr_number == 99
            return watcher_result(trees["advanced_parent"], trees["current_prospective"])

    refreshed = _refresh_post_merge_evidence(
        state.slices["slice-a"], TLLoopConfig(active=True), RefreshClient()
    )
    assert refreshed is not None
    assert refreshed["merge_tree_sha"] == trees["current_prospective"]
    observation = _watcher_result_observation(
        watcher_result(trees["advanced_parent"], trees["current_prospective"])
    )
    assert isinstance(observation, WatcherObservation)
    evidence = {
        **_watcher_merge_evidence(observation),
        "repository": "org/repo",
        "parent_branch": "main",
        "lane_epoch": 7,
    }
    adopted = _adopt_post_merge_slice(
        state.slices["slice-a"],
        state,
        99,
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

    refreshed = _refresh_post_merge_evidence(
        state.slices["slice-a"], TLLoopConfig(active=True), RefreshClient()
    )
    assert refreshed is not None
    assert refreshed["merge_tree_sha"] == trees["current_prospective"]
    refreshed_slice = _attach_forgejo_merge_evidence(
        state.slices["slice-a"], _forgejo_merge_evidence(refreshed)
    )
    state = store.checkpoint(
        state.fsm,
        {**state.slices, "slice-a": refreshed_slice},
        state.budgets,
        state.events.last_consumed_offset,
    )
    persisted = dict(state.slices["slice-a"].post_merge.evidence)
    assert persisted["prospective_merge_tree_sha"] == trees["historical_prospective"]
    assert persisted["forgejo_merge_commit_tree_sha"] == trees["merge_commit_tree"]

    class RemoteReconcileClient:
        def post_merge_remote_reconcile(self, **arguments: object) -> ToolResult:
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
                        "remote_head_sha": trees["advanced_parent"],
                        "new_base_sha": trees["advanced_parent"],
                        "remote_ancestry_proof": (
                            "ancestor:" + trees["advanced_parent"] + "->rebuilt-bookkeeping"
                        ),
                        "ancestry_proof": "squash-tree-match",
                        "merge_integration_proof": proof,
                    },
                }
            )

    journal = EffectJournal("advanced-parent", tmp_path / "advanced-parent-journal.json")
    arguments, payload = _remote_reconcile_effect(
        state.slices["slice-a"],
        99,
        persisted,
        TLLoopConfig(active=True, ledger_run_id="advanced-parent"),
        RemoteReconcileClient(),
        journal,
        expected_base_sha=trees["merge_commit"],
    )
    assert arguments["prospective_merge_tree_sha"] == trees["historical_prospective"]
    assert arguments["forgejo_merge_commit_tree_sha"] == trees["merge_commit_tree"]
    assert payload["new_base_sha"] == trees["advanced_parent"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pr_number", 44),
        ("merged", False),
        ("head_sha", "other-head"),
        ("merge_commit_sha", "other-merge"),
        ("merge_commit_tree_sha", "other-tree"),
        ("pr_head_tree_sha", "other-pr-tree"),
    ],
)
def test_conflicting_refreshed_identity_blocks_recovery(
    tmp_path: Path, field: str, value: object
) -> None:
    store, _ = _load_state(tmp_path)
    state = _review_recovery_state(store)
    evidence = {
        "pr_number": 43,
        "merged": True,
        "head_sha": "pr-head",
        "base_sha": "base-head",
        "base_branch": "main",
        "pr_state": "closed",
        "pr_head_tree_sha": "pr-tree",
        "merge_tree_sha": "merged-tree",
        "merge_commit_sha": "merge-commit",
        "merge_commit_tree_sha": "merged-tree",
        "repository": "org/repo",
        "parent_branch": "main",
        "lane_epoch": 7,
    }
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

    conflicting = {**evidence, field: value}

    class RefreshClient:
        def watcher_pr_state(self, *, pr_number: int) -> ToolResult:
            assert pr_number == 99
            return ToolResult.from_raw(
                {
                    "success": True,
                    "result": {
                        **conflicting,
                        "found": True,
                        "pr_state": "closed",
                    },
                }
            )

    refreshed = _refresh_post_merge_evidence(
        state.slices["slice-a"], TLLoopConfig(active=True), RefreshClient()
    )
    assert refreshed is not None
    if field == "merged":
        assert refreshed["merged"] is False

    blocked = _reconcile_merged_slice(
        state,
        "slice-a",
        43,
        "merge-journal",
        TLLoopConfig(active=True),
        object(),
        store,
        [],
        boundary="post_merge_recovery",
        merge_evidence=refreshed,
    )

    current = blocked.slices["slice-a"]
    assert current.dispatch_error is not None
    assert "conflicting refreshed Forgejo evidence" in current.dispatch_error
    assert any(gate.name == "tl-post-merge-slice-a" for gate in blocked.gates)
