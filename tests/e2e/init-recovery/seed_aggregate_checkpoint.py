"""Seed a failed root awaiting integration of a completed sub-TL aggregate."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def api(mock_url: str, path: str, payload: dict[str, object] | None = None) -> Any:
    request = urllib.request.Request(
        mock_url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def create_pr(mock_url: str, head: str, base: str, title: str) -> int:
    result = api(
        mock_url,
        "/api/v1/repos/owner/repo/pulls",
        {
            "head": head,
            "base": base,
            "title": title,
            "body": "Disposable recovery fixture.",
        },
    )
    return int(result["number"])


def merge_pr(mock_url: str, pr_number: int) -> str:
    result = api(mock_url, f"/api/v1/repos/owner/repo/pulls/{pr_number}/merge", {})
    return str(result["sha"])


def seed_git_history(
    repository: Path, mock_url: str
) -> tuple[int, int, str, str, str, str]:
    git(repository, "checkout", "-b", "main.architecture")
    git(repository, "push", "-u", "origin", "main.architecture")
    git(repository, "checkout", "-b", "main.issue-811", "main")
    (repository / "architecture.txt").write_text(
        "completed issue 811\n", encoding="utf-8"
    )
    git(repository, "add", "architecture.txt")
    git(repository, "commit", "-m", "Complete architecture issue 811")
    git(repository, "push", "-u", "origin", "main.issue-811")
    leaf_head = git(repository, "rev-parse", "HEAD")

    leaf_pr = create_pr(
        mock_url, "main.issue-811", "main.architecture", "Issue 811 architecture"
    )
    merge_pr(mock_url, leaf_pr)
    git(repository, "fetch", "origin")
    git(repository, "checkout", "-B", "main.architecture", "origin/main.architecture")
    aggregate_head = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "main")
    aggregate_pr = create_pr(
        mock_url,
        "main.architecture",
        "main",
        "Aggregate architecture into main",
    )
    base_sha = git(repository, "rev-parse", "main")
    merged_remote_head = merge_pr(mock_url, aggregate_pr)
    git(repository, "fetch", "origin")
    aggregate_record = api(mock_url, f"/api/v1/repos/owner/repo/pulls/{aggregate_pr}")
    if (
        not aggregate_record.get("merged")
        or aggregate_record.get("merge_commit_sha") != merged_remote_head
    ):
        raise ValueError(
            "aggregate fixture merge is not authoritatively recorded by Forgejo"
        )
    return (
        leaf_pr,
        aggregate_pr,
        leaf_head,
        aggregate_head,
        base_sha,
        merged_remote_head,
    )


def install_publication_owner(
    repository: Path,
    aggregate_pr: int,
    aggregate_head: str,
    architecture_worktree: Path,
) -> str:
    invocation_id = "seed-architecture-aggregate-invocation"
    exo = repository / ".exo"
    published_heads = {
        "schema_version": 1,
        "heads": [
            {
                "pr_number": aggregate_pr,
                "head_branch": "main.architecture",
                "base_branch": "main",
                "head_sha": aggregate_head,
                "author_agent": "architecture",
                "author_role": "dev",
                "provenance": "ledger_owned",
                "slice_id": "architecture",
                "invocation_id": invocation_id,
                "invocation_trigger": "spawn",
                "invocation_runtime": "codex",
            }
        ],
    }
    (exo / "published-heads.json").write_text(
        json.dumps(published_heads, indent=2) + "\n", encoding="utf-8"
    )
    agent_dir = exo / "agents" / "architecture"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "identity.json").write_text(
        json.dumps(
            {
                "agent_name": "architecture",
                "slug": "architecture",
                "agent_type": "codex",
                "birth_branch": "main.architecture",
                "parent_branch": "main",
                "working_dir": ".exo/worktrees/architecture",
                "display_name": "architecture",
                "topology": "worktree_per_agent",
                "model": None,
                "effort": None,
                "ledger_owned": True,
                "slice_id": "architecture",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (agent_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": invocation_id,
                "runtime": "codex",
                "trigger": "spawn",
                "mode": "interactive",
                "routing": {"window_id": "@42"},
                "started_at": 1700000000,
                "ended_at": 1700000060,
                "status": "exited",
                "exit_code": 0,
                "pr_number": aggregate_pr,
                "head_sha": aggregate_head,
                "generation": 1,
                "runtime_agent_id": "architecture",
                "slice_id": "architecture",
                "branch": "main.architecture",
                "worktree": str(architecture_worktree),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return invocation_id


def seed_child_scope(
    repository: Path,
    root_store: Any,
    root_manifest: Any,
    root_config: Any,
    work_plan: Any,
    repository_identity: Any,
    *,
    leaf_pr: int,
    aggregate_pr: int,
    leaf_head: str,
    aggregate_head: str,
    base_sha: str,
    patch_digest: str,
    architecture_worktree: Path,
) -> tuple[Any, Any]:
    from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
    from tl_loop.fsm.scope import TLPRFiled
    from tl_loop.loop.driver import (
        TLLoopConfig,
        _bind_initial_slices,
        _initial_slices,
    )
    from tl_loop.state.schema import (
        HandoffEvidence,
        IntegrationLifecycle,
        IntegrationRuntimeState,
        PublicationBinding,
        SliceStatus,
        Verdict,
    )
    from tl_loop.state.store import RunStore, create

    architecture_task = next(
        task for task in work_plan.sub_tls if task.name == "architecture"
    )
    architecture_node = next(
        node for node in root_manifest.nodes if node.name == "architecture"
    )
    child_manifest = root_manifest.child_manifests[architecture_node.node_id]
    child_plan = architecture_task.plan
    child_config = TLLoopConfig(
        root_dir=root_store.run_dir,
        run_id="architecture",
        branch="main.architecture",
        worktree=str(architecture_worktree),
        project_root=repository,
        parent_branch="main",
        parent_run_id="root",
        parent_agent_id="root",
        depth=1,
        repository_identity=repository_identity,
        agent_id="architecture",
        declared_manifest=child_manifest,
    )
    encoded_child_slices = _bind_initial_slices(
        _initial_slices(child_plan, child_config, root_store.run_dir, "architecture"),
        child_manifest,
    )
    create(
        "architecture",
        {
            "plan_manifest": child_manifest.to_document(),
            "slices": encoded_child_slices,
            "repository_identity": repository_identity.to_document(),
            "owner_branch": "main.architecture",
            "owner_worktree": str(architecture_worktree),
            "parent_branch": "main",
            "parent_run_id": "root",
            "parent_agent_id": "root",
            "depth": 1,
        },
        root_dir=root_store.run_dir,
    )
    child_store = RunStore("architecture", root_store.run_dir)
    child_store.set_plan_manifest(child_manifest, slices=encoded_child_slices)
    child_state = child_store.load()
    leaf = child_state.slices["issue-811"]
    leaf_invocation = "seed-issue-811-invocation"
    leaf_post_merge = PostMergeState(
        PostMergePhase.COMPLETE,
        {
            "child_id": "issue-811",
            "repository": repository_identity.remote_url,
            "parent_branch": "main.architecture",
            "pr_number": str(leaf_pr),
            "head_sha": leaf_head,
            "merge_journal_id": "seed-issue-811-merge-journal",
            "lane_epoch": "1",
            "parent_commit_sha": aggregate_head,
            "issue_id": "811",
            "issue_close_intent_id": "seed-issue-811-close-intent",
            "issue_close_journal_id": "seed-issue-811-close-journal",
            "issue_close_outcome": "closed",
            "changelog_intent_id": "seed-issue-811-changelog-intent",
            "changelog_generation": "1",
            "changelog_commit_sha": aggregate_head,
            "parent_push_intent_id": "seed-issue-811-push-intent",
            "push_journal_id": "seed-issue-811-push-journal",
            "expected_base_sha": base_sha,
            "push_receipt_id": "seed-issue-811-push-receipt",
            "pushed_commit": aggregate_head,
            "bookkeeping_commit": aggregate_head,
            "observed_remote_head": aggregate_head,
            "ancestry_proof": f"ancestor:{aggregate_head}->{aggregate_head}",
        },
    )
    completed_leaf = replace(
        leaf,
        status=SliceStatus.MERGED,
        pr_number=leaf_pr,
        reviewed_head=leaf_head,
        attempts=1,
        verdict=Verdict.GO,
        review_patch_digests={leaf_head: "seed-reviewed-leaf-patch"},
        ci_state={leaf_head: "success"},
        dispatch_intent_id="seed-issue-811-dispatch",
        dispatch_started_at=1700000000.0,
        dispatch_agent_id="issue-811",
        dispatch_invocation_id=leaf_invocation,
        dispatch_generation=1,
        dispatch_authoritative_event_seq=1,
        dispatch_last_boundary="agent.completed",
        publication=PublicationBinding(
            pr_number=leaf_pr,
            head_sha=leaf_head,
            head_branch="main.issue-811",
            base_branch="main.architecture",
            attempt=1,
            invocation_id=leaf_invocation,
        ),
        handoff=HandoffEvidence(
            pr_number=leaf_pr,
            head_sha=leaf_head,
            attempt=1,
            invocation_id=leaf_invocation,
            agent_id="issue-811",
            observed_at="2026-10-16T00:00:00Z",
        ),
        post_merge=leaf_post_merge,
    )
    child_candidate = IntegrationRuntimeState(
        lifecycle=IntegrationLifecycle.AGGREGATE_PR_OPEN,
        aggregate_pr_number=aggregate_pr,
        aggregate_head_sha=aggregate_head,
        aggregate_patch_digest=patch_digest,
        aggregate_original_base_sha=base_sha,
        integration_owner_id="root:architecture:integration",
        integration_owner_run_id="architecture",
        integration_owner_branch="main.architecture",
        integration_owner_worktree=str(architecture_worktree),
        head_sha=aggregate_head,
        patch_digest=patch_digest,
    )
    child_state = child_store.checkpoint(
        TLPRFiled(
            aggregate_pr=str(aggregate_pr),
            head_sha=aggregate_head,
            base_sha=base_sha,
            parent_branch="main",
            handoff="root:architecture:integration",
            scope_path=(child_manifest.scope_id,),
            plan_digest=child_manifest.digest,
        ),
        {**child_state.slices, "issue-811": completed_leaf},
        child_state.budgets,
        child_state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=(),
        integration=child_candidate,
    )
    return child_store, child_state


def seed_root(
    repository: Path,
    mock_url: str,
    *,
    leaf_pr: int,
    aggregate_pr: int,
    leaf_head: str,
    aggregate_head: str,
    base_sha: str,
    merged_remote_head: str,
) -> dict[str, object]:
    from tl_loop.client.effects import ToolResult
    from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
    from tl_loop.fsm.scope import TLFailed
    from tl_loop.loop.driver import (
        AGGREGATE_PUBLICATION_GATE_PREFIX,
        EffectIntent,
        TLLoopConfig,
        WorkPlan,
        _initial_slices,
        _manifest_for_plan,
        _parent_aggregate_approval,
        _parent_sync_arguments,
        _parent_sync_identity_fields,
        _verified_child_completion_evidence,
    )
    from tl_loop.loop.journal import EffectJournal
    from tl_loop.ordered import AggregateCandidate, IntegrationLifecycle
    from tl_loop.state.repository_identity import canonical_repository_url
    from tl_loop.state.schema import (
        GateStatus,
        HandoffEvidence,
        IntegrationCandidateState,
        IntegrationRuntimeState,
        OrderedStageState,
        ParkCause,
        PublicationBinding,
        RepositoryIdentity,
        SliceStatus,
    )
    from tl_loop.state.store import RunStore, create

    state_root = repository / ".exo" / "tl-loop"
    remote_url = git(repository, "config", "--get", "remote.origin.url")
    identity = RepositoryIdentity(canonical_repository_url(remote_url))
    plan_spec = {
        "sub_tls": [
            {
                "name": "architecture",
                "order": 1,
                "integration": {
                    "aggregate_pr_required": True,
                    "aggregate_approval_mode": "parent",
                },
                "plan": {
                    "leaves": [
                        {
                            "name": "issue-811",
                            "task": "Complete the architecture change for issue 811",
                            "chainlink_issue_id": 811,
                            "boundary": ["architecture.txt"],
                        }
                    ]
                },
            },
            {
                "name": "kani",
                "order": 2,
                "integration": {"aggregate_pr_required": False},
                "plan": {"leaves": []},
            },
        ]
    }
    work_plan = WorkPlan.from_mapping(plan_spec)
    state_root.mkdir(parents=True, exist_ok=True)
    root_agent_dir = repository / ".exo" / "agents" / "root"
    root_agent_dir.mkdir(parents=True, exist_ok=True)
    (root_agent_dir / "identity.json").write_text(
        json.dumps(
            {
                "agent_name": "root",
                "slug": "root",
                "agent_type": "codex",
                "birth_branch": "main",
                "parent_branch": "main",
                "working_dir": ".",
                "display_name": "TL loop",
                "topology": "shared_dir",
                "model": None,
                "effort": None,
                "ledger_owned": True,
                "slice_id": None,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    plan_bytes = (
        json.dumps({"run_id": "root", "plan": plan_spec}, indent=2) + "\n"
    ).encode()
    (state_root / "plan.json").write_bytes(plan_bytes)
    (state_root / "plan.snapshot").write_bytes(plan_bytes)
    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(plan_bytes).hexdigest() + "\n", encoding="utf-8"
    )
    root_config = TLLoopConfig(
        root_dir=state_root,
        run_id="root",
        branch="main",
        worktree=str(repository),
        project_root=repository,
        repository_identity=identity,
        agent_id="root",
        active=True,
    )
    manifest = _manifest_for_plan(work_plan, "root", root_config)
    root_slices = _initial_slices(work_plan, root_config, state_root, "root")
    create(
        "root",
        {
            "slices": root_slices,
            "repository_identity": identity.to_document(),
            "owner_branch": "main",
            "owner_worktree": str(repository),
            "depth": 0,
        },
        root_dir=state_root,
    )
    root_store = RunStore("root", state_root)
    root_store.set_plan_manifest(manifest, slices=root_slices)
    root_state = root_store.set_ordered_state(
        1,
        (
            OrderedStageState(1, ("architecture",)),
            OrderedStageState(2, ("kani",)),
        ),
        IntegrationRuntimeState(
            sub_tl_states={
                "architecture": IntegrationLifecycle.MERGING,
                "kani": IntegrationLifecycle.RUNNING,
            }
        ),
    )
    root_state = root_store.set_controller_epoch("e2e-parent-controller-epoch")
    architecture_worktree = repository / ".exo" / "worktrees" / "architecture"
    git(repository, "worktree", "add", str(architecture_worktree), "main.architecture")
    invocation_id = install_publication_owner(
        repository, aggregate_pr, aggregate_head, architecture_worktree
    )
    patch_bytes = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "diff",
            "--binary",
            "--no-ext-diff",
            base_sha,
            aggregate_head,
        ),
        check=True,
        capture_output=True,
    ).stdout
    patch_digest = hashlib.sha256(patch_bytes).hexdigest()
    child_store, _ = seed_child_scope(
        repository,
        root_store,
        manifest,
        root_config,
        work_plan,
        identity,
        leaf_pr=leaf_pr,
        aggregate_pr=aggregate_pr,
        leaf_head=leaf_head,
        aggregate_head=aggregate_head,
        base_sha=base_sha,
        patch_digest=patch_digest,
        architecture_worktree=architecture_worktree,
    )
    merge_tree = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "merge-tree",
            "--write-tree",
            base_sha,
            aggregate_head,
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[0]
    aggregate_head_tree = git(repository, "rev-parse", f"{aggregate_head}^{{tree}}")
    merge_commit_tree = git(repository, "rev-parse", f"{merged_remote_head}^{{tree}}")
    aggregate_record = api(mock_url, f"/api/v1/repos/owner/repo/pulls/{aggregate_pr}")
    if (
        aggregate_record.get("merged") is not True
        or aggregate_record.get("head", {}).get("sha") != aggregate_head
        or aggregate_record.get("merge_commit_sha") != merged_remote_head
        or merge_commit_tree != merge_tree
    ):
        raise ValueError(
            "merged aggregate record does not match its Git compare evidence"
        )
    watcher = {
        "found": True,
        "pr_number": aggregate_pr,
        "merged": True,
        "head_sha": aggregate_head,
        "base_sha": base_sha,
        "patch_digest": patch_digest,
        "merge_tree_sha": merge_tree,
        "pr_head_tree_sha": aggregate_head_tree,
        "ci_status": "success",
        "head_branch": "main.architecture",
        "base_branch": "main",
        "head_reachable": True,
        "publication_ownership_verified": True,
        "publication_ownership_error": "",
        "publication": {
            "invocation_id": invocation_id,
            "slice_id": "architecture",
            "author_agent": "architecture",
            "succession_invocation_ids": [],
        },
    }
    readiness = _verified_child_completion_evidence(
        work_plan.sub_tls[0],
        child_store.load(),
        AggregateCandidate(
            "architecture", aggregate_pr, aggregate_head, patch_digest, base_sha
        ),
        watcher=watcher,
        repository=architecture_worktree,
    )
    if readiness is None:
        raise ValueError(
            "production child-completion verifier rejected the merged fixture"
        )
    approval_history, approval_receipt = _parent_aggregate_approval(
        work_plan.sub_tls[0],
        root_state,
        readiness,
        root_store,
        root_config,
        pr_number=aggregate_pr,
        head_sha=aggregate_head,
        head_tree_sha=aggregate_head_tree,
        base_sha=base_sha,
        patch_digest=patch_digest,
        merge_tree_sha=merge_tree,
        ci_status="success",
    )
    architecture = root_state.slices["architecture"]
    publication = PublicationBinding(
        pr_number=aggregate_pr,
        head_sha=aggregate_head,
        head_branch="main.architecture",
        base_branch="main",
        attempt=1,
        invocation_id=invocation_id,
    )
    journal = EffectJournal("root", root_store.run_dir / "action-journal.json")
    rejected_intent = EffectIntent(
        "merge_pr", "architecture", {"pr_number": aggregate_pr, "attempt": 1}, True
    )
    journal.append(rejected_intent)
    rejected = ToolResult(
        raw={
            "success": False,
            "error": f"No Forgejo reviewer response yet on PR #{aggregate_pr}.",
        },
        success=False,
        result={
            "success": False,
            "error": f"No Forgejo reviewer response yet on PR #{aggregate_pr}.",
        },
        error=f"No Forgejo reviewer response yet on PR #{aggregate_pr}.",
    )
    journal.mark_result(rejected_intent, rejected)
    merge_arguments = {
        "pr_number": aggregate_pr,
        "base_sha": base_sha,
        "head_sha": aggregate_head,
        "aggregate_approval_receipt_id": approval_receipt.receipt_id,
        "attempt": 2,
    }
    merged_intent = EffectIntent("merge_pr", "architecture", merge_arguments, True)
    journal.append(merged_intent)
    journal.mark_result(
        merged_intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {
                    **merge_arguments,
                    "merged": True,
                    "merge_commit_sha": merged_remote_head,
                },
            }
        ),
    )
    merge_journal_id = journal.key_for(merged_intent)
    post_merge_evidence = {
        "child_id": "architecture",
        "repository": identity.remote_url,
        "parent_branch": "main",
        "pr_number": str(aggregate_pr),
        "head_sha": aggregate_head,
        "merge_journal_id": merge_journal_id,
        "lane_epoch": "1",
        "forgejo_pr_number": str(aggregate_pr),
        "forgejo_merged": "true",
        "forgejo_head_sha": aggregate_head,
        "forgejo_merge_commit_sha": merged_remote_head,
        "forgejo_merge_commit_tree_sha": merge_commit_tree,
        "prospective_merge_tree_sha": merge_tree,
        "reviewed_pr_head_tree_sha": aggregate_head_tree,
    }
    parked_architecture = replace(
        architecture,
        status=SliceStatus.PARKED,
        base_ref="main",
        branch="main.architecture",
        worktree=str(architecture_worktree),
        pr_number=aggregate_pr,
        reviewed_head=aggregate_head,
        attempts=1,
        dispatch_intent_id="seed-architecture-dispatch",
        dispatch_started_at=1700000000.0,
        dispatch_agent_id="architecture",
        dispatch_invocation_id=invocation_id,
        dispatch_generation=1,
        dispatch_authoritative_event_seq=1,
        dispatch_last_boundary="aggregate_merged",
        publication=publication,
        handoff=HandoffEvidence(
            pr_number=aggregate_pr,
            head_sha=aggregate_head,
            attempt=1,
            invocation_id=invocation_id,
            agent_id="architecture",
            observed_at="2026-10-16T00:00:00Z",
        ),
        park_cause=ParkCause.PUBLICATION_BLOCKED,
        park_audit={
            "error_kind": "post_merge_sync_refused",
            "reason": "a prior local parent synchronization was refused before mutation",
            "retryable": True,
        },
        action=None,
        post_merge=PostMergeState(
            PostMergePhase.REMOTE_MERGE_ADOPTED,
            post_merge_evidence,
        ),
        reconciliation={
            "confirmed_stage": "aggregate_merged",
            "next_action": "post_merge_recovery",
            "authoritative_evidence": ["merge_pr_journal", "forgejo_merge_record"],
            "missing_evidence": [],
            "conflicts": [],
            "merge_base_sha": base_sha,
            "merge_head_sha": aggregate_head,
            "merge_journal_id": merge_journal_id,
        },
    )
    # Keep a prior legacy-format synchronization refusal in the journal. It
    # represents a managed Git lease refusal before any sync mutation; the
    # next invocation must preserve it and continue with a new attempt.
    sync_arguments = _parent_sync_arguments(
        parked_architecture,
        aggregate_pr,
        {
            "repository": identity.remote_url,
            "parent_branch": "main",
            "head_sha": aggregate_head,
            "merge_journal_id": merge_journal_id,
            "lane_epoch": 1,
        },
        base_sha,
        ".",
    )
    legacy_sync_arguments = {
        key: value
        for key, value in _parent_sync_identity_fields(sync_arguments).items()
        if key != "merge_journal_id"
    }
    legacy_sync_arguments.update({"expected_base_sha": base_sha, "sync_attempt": 1})
    sync_intent = EffectIntent(
        "post_merge_parent_sync", "architecture", legacy_sync_arguments, True
    )
    journal.append(sync_intent)
    journal.mark_result(
        sync_intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {
                    **sync_arguments,
                    "sync_attempt": 1,
                    "sync_outcome": "pre_mutation_refusal",
                    "sync_phase": "preflight",
                    "completed_steps": [],
                    "retry_safe": True,
                    "diagnostic": "prior managed Git lock prevented local synchronization",
                },
            }
        ),
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.MERGED,
        aggregate_pr_number=aggregate_pr,
        aggregate_head_sha=aggregate_head,
        aggregate_patch_digest=patch_digest,
        aggregate_original_base_sha=base_sha,
        integration_owner_id="root:architecture:integration",
        integration_owner_run_id="architecture",
        integration_owner_branch="main.architecture",
        integration_owner_worktree=str(architecture_worktree),
        head_sha=aggregate_head,
        patch_digest=patch_digest,
        validated_base_sha=base_sha,
        merge_tree_sha=merge_tree,
        integration_evidence_at="2026-10-16T00:00:00Z",
        ci_status="success",
        merge_attempts=2,
        stage_verification="passed",
        aggregate_approval_receipts=approval_history,
        active_aggregate_approval_id=approval_receipt.receipt_id,
    )
    root_store.checkpoint(
        TLFailed("recursive child is not recoverable"),
        {**root_state.slices, "architecture": parked_architecture},
        root_state.budgets,
        root_state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=root_state.ordered_stages,
        integration=IntegrationRuntimeState(
            sub_tl_states={
                "architecture": IntegrationLifecycle.MERGED,
                "kani": IntegrationLifecycle.RUNNING,
            },
            candidates={"architecture": candidate},
        ),
    )
    root_store.set_gate(
        f"{AGGREGATE_PUBLICATION_GATE_PREFIX}architecture", GateStatus.PENDING
    )
    root_store.set_gate("tl-ordered-child-recovery-ambiguous", GateStatus.PENDING)
    return {
        "root": str(root_store.path),
        "child": str(child_store.path),
        "leaf_pr": leaf_pr,
        "aggregate_pr": aggregate_pr,
        "leaf_head": leaf_head,
        "aggregate_head": aggregate_head,
        "aggregate_remote_head": merged_remote_head,
        "base_sha": base_sha,
        "patch_digest": patch_digest,
        "merge_tree": merge_tree,
    }


def seed_local_edits(repository: Path) -> dict[str, str]:
    """Leave independent staged, unstaged, and untracked edits for recovery."""
    (repository / "preserved-staged.txt").write_text(
        "staged local change\n", encoding="utf-8"
    )
    git(repository, "add", "preserved-staged.txt")
    (repository / "preserved-unstaged.txt").write_text(
        "unstaged local change\n", encoding="utf-8"
    )
    (repository / "preserved-untracked.txt").write_text(
        "untracked local change\n", encoding="utf-8"
    )
    return {
        "staged": "preserved-staged.txt",
        "unstaged": "preserved-unstaged.txt",
        "untracked": "preserved-untracked.txt",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--mock-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    (
        leaf_pr,
        aggregate_pr,
        leaf_head,
        aggregate_head,
        base_sha,
        merged_remote_head,
    ) = seed_git_history(args.repo, args.mock_url)
    result = seed_root(
        args.repo,
        args.mock_url,
        leaf_pr=leaf_pr,
        aggregate_pr=aggregate_pr,
        leaf_head=leaf_head,
        aggregate_head=aggregate_head,
        base_sha=base_sha,
        merged_remote_head=merged_remote_head,
    )
    result["preserved_local_edits"] = seed_local_edits(args.repo)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
