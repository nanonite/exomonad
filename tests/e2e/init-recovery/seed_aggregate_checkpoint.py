"""Seed a failed later child with an already completed sub-TL aggregate."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

DORMANT_KANI_INVOCATION_ID = "seed-kani-dormant-controller-invocation"


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
        _child_controller_name,
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
        parent_agent_id=_child_controller_name(architecture_task),
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
            "parent_agent_id": _child_controller_name(architecture_task),
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
    active_kani_blocker: bool,
    terminal_provider_failure: bool = False,
) -> dict[str, object]:
    from tl_loop.client.effects import EffectClient, ToolResult
    from tl_loop.fsm.child import ChildKind, ChildRecord
    from tl_loop.fsm.lane import LanePhase, LaneState
    from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
    from tl_loop.fsm.post_merge_events import (
        ParentBranchSynced,
        PostMergeNotApplicable,
        UnlinkedChangelogJudged,
    )
    from tl_loop.fsm.scope import TLFailed as RecursiveTLFailed
    from tl_loop.fsm.scope import TLRunning as RecursiveTLRunning
    from tl_loop.loop.driver import (
        AGGREGATE_PUBLICATION_GATE_PREFIX,
        ORDERED_RECOVERY_GATE_PREFIX,
        DispatchAttempt,
        EffectIntent,
        TLLoopConfig,
        WorkPlan,
        _checkpoint_post_merge_event,
        _child_controller_name,
        _initial_slices,
        _manifest_for_plan,
        _parent_aggregate_approval,
        _parent_sync_arguments,
        _parent_sync_identity_fields,
        _record_terminal_dispatch_failure,
        _reopen_ordered_scope,
        _sub_tl_worktree,
        _verified_child_completion_evidence,
        derive_child_branch,
        stable_action_key,
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
                "plan": {
                    "leaves": [
                        {
                            "name": "issue-814-substitution-model-kani",
                            "task": "Complete the Kani stage recovery fixture for issue 814",
                            "agent_type": "opencode/opencode-go/deepseek-v4.1-flash",
                            "chainlink_issue_id": 814
                            if terminal_provider_failure
                            else None,
                            "boundary": ["kani-recovery.txt"],
                            "verify": ["test -s kani-recovery.txt"],
                        }
                    ]
                },
            },
            {
                "name": "handoff-proof",
                "order": 3,
                "integration": {"aggregate_pr_required": False},
                "plan": {
                    "leaves": [
                        {
                            "name": "post-tidy-leaf",
                            "task": "Create and publish the post-tidy handoff fixture change",
                            "chainlink_issue_id": None,
                            "boundary": ["post-tidy-leaf.txt"],
                            "read_first": ["README.md"],
                            "steps": [
                                "Write post-tidy-leaf.txt and publish its branch"
                            ],
                            "verify": ["test -s post-tidy-leaf.txt"],
                            "done_criteria": [
                                "The fixture leaf is merged into its declared subTL branch"
                            ],
                        }
                    ]
                },
            },
        ]
    }
    if terminal_provider_failure:
        # Keep the terminal-owner acceptance focused on the declared two-stage
        # architecture -> Kani workflow. The generic fixture's later handoff
        # stage belongs to #1267 and can fail independently after this recovery
        # has completed, preventing its repeat-init assertions from running.
        plan_spec["sub_tls"] = plan_spec["sub_tls"][:2]
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
        tuple(
            OrderedStageState(int(row["order"]), (str(row["name"]),))
            for row in plan_spec["sub_tls"]
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
    kani_task = next(task for task in work_plan.sub_tls if task.name == "kani")
    kani_branch = derive_child_branch("main", _child_controller_name(kani_task))
    kani_worktree = str(_sub_tl_worktree(root_config, state_root, "root", kani_task))
    kani_manifest = manifest.child_manifests[
        next(node.node_id for node in manifest.nodes if node.name == "kani")
    ]
    kani_config = replace(
        root_config,
        root_dir=root_store.run_dir,
        run_id="kani",
        branch=kani_branch,
        worktree=kani_worktree,
        parent_branch="main",
        parent_run_id="root",
        parent_agent_id=_child_controller_name(kani_task),
        agent_id=_child_controller_name(kani_task),
        depth=1,
        declared_manifest=kani_manifest,
    )
    kani_slices = _initial_slices(kani_task.plan, kani_config, state_root, "kani")
    create(
        "kani",
        {
            "slices": kani_slices,
            "repository_identity": identity.to_document(),
            "owner_branch": kani_branch,
            "owner_worktree": kani_worktree,
            "parent_branch": "main",
            "parent_run_id": "root",
            "parent_agent_id": _child_controller_name(kani_task),
            "depth": 1,
        },
        root_dir=root_store.run_dir,
    )
    kani_store = RunStore("kani", root_store.run_dir)
    kani_store.set_plan_manifest(kani_manifest, slices=kani_slices)
    kani_state = kani_store.load()
    kani_task_plan = kani_task.plan
    kani_state = _reopen_ordered_scope(kani_state, kani_task_plan, kani_store, None)
    failed_leaf_name = "issue-814-substitution-model-kani"
    failed_leaf_task = kani_task_plan.leaves[0]
    if terminal_provider_failure:
        if not active_kani_blocker:
            raise ValueError(
                "terminal-provider failure fixture requires the #814 human gate"
            )
        if kani_state.slices[failed_leaf_name].chainlink_issue_id != 814:
            raise ValueError(
                "terminal-provider failure fixture requires the accepted #814 binding"
            )
        kani_failure = None
        failed_kani = replace(
            root_state.slices["kani"],
            status=SliceStatus.SPAWNED,
            attempts=1,
            branch=kani_branch,
            worktree=kani_worktree,
            dispatch_intent_id="seed-kani-dispatch-intent",
            dispatch_agent_id=_child_controller_name(kani_task),
            dispatch_invocation_id=DORMANT_KANI_INVOCATION_ID,
            dispatch_generation=1,
            dispatch_authoritative_event_seq=1,
            dispatch_last_boundary="sub_tl_started",
        )
    else:
        failed_intent_id = "seed-issue-814-rejected-spawn-intent"
        dirty_preflight_error = (
            "BLOCKED: cannot spawn agent into a dirty TL worktree. 2 file(s) have uncommitted changes:\n"
            "  .exo/harness_policy.toml\n"
            "  CHANGELOG.md\n"
            "Commit the scaffold (per scaffold-fork-converge) or run `discard_worker_output` "
            "if throwaway, then retry. Workers spawn in-place and would inherit this state; "
            "dev-leaves fork from your branch HEAD and would not see your uncommitted work."
        )
        failed_leaf = replace(
            kani_state.slices[failed_leaf_name],
            status=SliceStatus.DISPATCHING,
            attempts=1,
            dispatch_intent_id=failed_intent_id,
            dispatch_started_at=1700000001.0,
            dispatch_generation=1,
            dispatch_ledger_floor=0,
            dispatch_last_boundary="dispatch_intended",
        )
        kani_state = kani_store.checkpoint(
            kani_state.recursive_fsm,
            {**kani_state.slices, failed_leaf_name: failed_leaf},
            kani_state.budgets,
            kani_state.events.last_consumed_offset,
        )
        failed_attempt = DispatchAttempt(
            intent_id=failed_intent_id,
            started_at=1700000001.0,
            harness="codex",
            agent_type="codex",
            attempt=1,
            controller_epoch=kani_state.controller_epoch,
            dispatch_generation=1,
            ledger_floor=0,
        )
        failed_spawn = EffectIntent(
            "spawn_leaf",
            failed_leaf_name,
            {
                "name": failed_leaf_name,
                "task": failed_leaf_task.task,
                "intent_id": failed_intent_id,
            },
            True,
        )
        kani_journal = EffectJournal("kani", kani_store.run_dir / "action-journal.json")
        kani_journal.append(failed_spawn)
        kani_journal.mark_result(
            failed_spawn,
            ToolResult.from_raw(
                {
                    "success": False,
                    "result": None,
                    "error": dirty_preflight_error,
                    "error_kind": None,
                    "error_context": None,
                }
            ),
        )

    if not terminal_provider_failure:

        class _SeedTransport:
            def call_tool(
                self, role: str, name: str, tool_name: str, arguments: dict[str, object]
            ) -> dict[str, object]:
                del role, name, tool_name, arguments
                raise AssertionError("offline historical fixture must not call tools")

        failure_config = replace(
            kani_config,
            active=False,
            session_mode="continue",
        )
        _record_terminal_dispatch_failure(
            kani_store,
            kani_state,
            failed_leaf_name,
            failed_attempt,
            dirty_preflight_error,
            "agent_error",
            failure_config,
            EffectClient(_SeedTransport()),
            kani_journal,
        )
        kani_state = kani_store.load()
        kani_failure = kani_state.recursive_fsm.reason
        if active_kani_blocker:
            blocked_leaf = replace(
                kani_state.slices[failed_leaf_name],
                status=SliceStatus.BLOCKED,
                park_cause=ParkCause.HUMAN_DECISION_REQUIRED,
            )
            kani_state = kani_store.checkpoint(
                kani_state.recursive_fsm,
                {**kani_state.slices, failed_leaf_name: blocked_leaf},
                kani_state.budgets,
                kani_state.events.last_consumed_offset,
            )
        failed_kani = replace(
            root_state.slices["kani"],
            status=SliceStatus.FAILED,
            attempts=1,
            branch=kani_branch,
            worktree=kani_worktree,
            dispatch_intent_id="seed-kani-dispatch-intent",
            dispatch_agent_id=_child_controller_name(kani_task),
            dispatch_invocation_id=DORMANT_KANI_INVOCATION_ID,
            dispatch_generation=1,
            dispatch_authoritative_event_seq=1,
            dispatch_last_boundary="sub_tl_started",
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
    kani_dispatch_arguments = {
        "event_type": "tl.dispatch_confirmed",
        "payload": {
            "slice_id": "kani",
            "intent_id": "seed-kani-dispatch-intent",
            "boundary": "sub_tl_started",
            "started_at": 1700000001.0,
            "attempt": 1,
            "controller_epoch": "e2e-parent-controller-epoch",
            "dispatch_generation": 1,
        },
    }
    kani_dispatch_intent = EffectIntent(
        "emit_controller_event", "kani", kani_dispatch_arguments, True
    )
    journal.append(kani_dispatch_intent)
    journal.mark_result(
        kani_dispatch_intent,
        ToolResult.from_raw(
            {
                "success": True,
                "result": {"event_type": "tl.dispatch_confirmed", "run_seq": 1},
            }
        ),
    )
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
    sync_identity = _parent_sync_identity_fields(sync_arguments)
    sync_operation_id = stable_action_key(
        "root", "post_merge_parent_sync_operation", "architecture", sync_identity
    )
    confirmed_sync_arguments = {
        "sync_operation_id": sync_operation_id,
        "sync_attempt": 2,
    }
    confirmed_sync_intent = EffectIntent(
        "post_merge_parent_sync", "architecture", confirmed_sync_arguments, True
    )
    confirmed_sync_payload = {
        **sync_arguments,
        **confirmed_sync_arguments,
        "sync_outcome": "confirmed",
        "sync_phase": "complete",
        "completed_steps": ["fetch", "fast_forward"],
        "retry_safe": False,
        "parent_commit_sha": merged_remote_head,
        "remote_head_sha": merged_remote_head,
        "ancestry_proof": f"ancestor:{aggregate_head}->{merged_remote_head}",
        "remote_name": "origin",
    }
    journal.append(confirmed_sync_intent)
    journal.mark_result(
        confirmed_sync_intent,
        ToolResult.from_raw({"success": True, "result": confirmed_sync_payload}),
    )
    candidate = IntegrationCandidateState(
        lifecycle=IntegrationLifecycle.CHILDREN_MERGED,
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
    if terminal_provider_failure:
        root_phase = RecursiveTLRunning(
            current_order=2,
            pending_by_order={
                2: (
                    ChildRecord(
                        "kani",
                        ChildKind.SUB_TL,
                        dispatch_intent_id="seed-kani-dispatch-intent",
                        invocation_id=DORMANT_KANI_INVOCATION_ID,
                        manifest_node_id=next(
                            node.node_id
                            for node in manifest.nodes
                            if node.name == "kani"
                        ),
                        manifest_revision=manifest.manifest_revision,
                    ),
                )
            },
            scope_path=(manifest.scope_id,),
            plan_digest=manifest.digest,
        )
        kani_lifecycle = IntegrationLifecycle.RUNNING
    else:
        root_phase = RecursiveTLFailed(f"Kani child failed: {kani_failure}")
        kani_lifecycle = IntegrationLifecycle.FAILED
    root_store.checkpoint(
        root_phase,
        {**root_state.slices, "architecture": parked_architecture},
        root_state.budgets,
        root_state.events.last_consumed_offset,
        current_order=2,
        ordered_stages=root_state.ordered_stages,
        integration=IntegrationRuntimeState(
            lifecycle=IntegrationLifecycle.MERGED,
            sub_tl_states={
                "architecture": IntegrationLifecycle.MERGED,
                "kani": IntegrationLifecycle.FAILED,
            },
            # The aggregate is already merged. The later Kani dispatch refusal
            # is the root's current outcome and must not change this candidate.
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
            candidates={
                "architecture": replace(
                    candidate, lifecycle=IntegrationLifecycle.MERGED
                )
            },
        ),
    )
    root_store.set_gate(
        f"{AGGREGATE_PUBLICATION_GATE_PREFIX}architecture", GateStatus.PENDING
    )
    state = root_store.load()
    synced = _checkpoint_post_merge_event(
        root_store,
        state,
        "architecture",
        ParentBranchSynced("architecture", "main", merged_remote_head),
        {
            "remote_head_sha": merged_remote_head,
            "ancestry_proof": confirmed_sync_payload["ancestry_proof"],
            "remote_name": "origin",
        },
    )
    synced = _checkpoint_post_merge_event(
        root_store,
        synced,
        "architecture",
        UnlinkedChangelogJudged(
            "architecture",
            "retained-architecture-changelog-judgment",
            "captured",
            "an existing unreleased entry already covers the merged work",
            model="retained-tl-model",
            tokens=17,
        ),
    )
    synced = _checkpoint_post_merge_event(
        root_store,
        synced,
        "architecture",
        PostMergeNotApplicable(
            "architecture",
            "captured",
            "an existing unreleased entry already covers the merged work",
        ),
    )
    synced_architecture = replace(
        synced.slices["architecture"],
        status=SliceStatus.MERGED,
        park_cause=None,
        dispatch_error=None,
    )

    lane_key = f"{identity.remote_url}:main"
    idle_lane = LaneState(
        identity.remote_url,
        "main",
        phase=LanePhase.IDLE,
        last_lane_epoch=2,
    )
    if terminal_provider_failure:
        root_phase = RecursiveTLRunning(
            current_order=2,
            pending_by_order={
                2: (
                    ChildRecord(
                        "kani",
                        ChildKind.SUB_TL,
                        dispatch_intent_id="seed-kani-dispatch-intent",
                        invocation_id=DORMANT_KANI_INVOCATION_ID,
                        manifest_node_id=next(
                            node.node_id
                            for node in manifest.nodes
                            if node.name == "kani"
                        ),
                        manifest_revision=manifest.manifest_revision,
                    ),
                )
            },
            scope_path=(manifest.scope_id,),
            plan_digest=manifest.digest,
        )
        kani_lifecycle = IntegrationLifecycle.RUNNING
    else:
        root_phase = RecursiveTLFailed(f"Kani child failed: {kani_failure}")
        kani_lifecycle = IntegrationLifecycle.FAILED
    root_store.checkpoint(
        root_phase,
        {
            **synced.slices,
            "architecture": synced_architecture,
            "kani": failed_kani,
        },
        synced.budgets,
        synced.events.last_consumed_offset,
        current_order=2,
        ordered_stages=synced.ordered_stages,
        integration=replace(
            synced.integration,
            lanes={lane_key: idle_lane},
            sub_tl_states={
                **synced.integration.sub_tl_states,
                "kani": kani_lifecycle,
            },
        ),
    )
    if not terminal_provider_failure:
        root_store.set_gate(f"{ORDERED_RECOVERY_GATE_PREFIX}kani", GateStatus.PENDING)
    git(repository, "merge", "--ff-only", merged_remote_head)
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
        "failed_child": "kani",
        "failed_child_run": str(kani_store.path),
        "failed_child_worktree": kani_worktree,
        "failed_child_branch": kani_branch,
    }


def seed_dormant_kani_controller(repository: Path, result: dict[str, object]) -> None:
    """Retain the owned Kani scope and a killed no-PR controller invocation."""
    agent_name = "kani"
    branch = str(result["failed_child_branch"])
    worktree = Path(str(result["failed_child_worktree"]))
    git(repository, "worktree", "add", "--quiet", "-b", branch, str(worktree), "main")
    agent_dir = repository / ".exo" / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    identity = {
        "agent_name": agent_name,
        "slug": agent_name,
        "agent_type": "codex",
        "birth_branch": branch,
        "parent_branch": "main",
        "working_dir": str(worktree),
        "display_name": "🤖 kani",
        "topology": "worktree_per_agent",
        "model": None,
        "effort": None,
        "ledger_owned": True,
        "slice_id": "kani",
    }
    (agent_dir / "identity.json").write_text(
        json.dumps(identity, indent=2) + "\n", encoding="utf-8"
    )
    started_at = int(time.time()) - 10
    invocation = {
        "invocation_id": DORMANT_KANI_INVOCATION_ID,
        "runtime": "codex",
        "trigger": "spawn",
        "mode": "interactive",
        "routing": {"window_id": "@999999"},
        "started_at": started_at,
        "ended_at": started_at + 1,
        "status": "killed",
        "exit_code": 137,
        "pr_number": None,
        "head_sha": None,
        "model": None,
        "effort": None,
        "prior_invocation_id": None,
        "recovery_round": 0,
        "authorization_source": None,
        "generation": 1,
        "runtime_agent_id": agent_name,
        "slice_id": "kani",
        "branch": branch,
        "worktree": str(worktree),
        "exit_reason": "tmux_target_exited_without_exit_marker",
        "exit_classification": "missing_exit_marker",
        "stderr_tail": None,
    }
    (agent_dir / "invocation.json").write_text(
        json.dumps(invocation, indent=2) + "\n", encoding="utf-8"
    )


def seed_local_edits(repository: Path) -> dict[str, str]:
    """Leave representative all-changes edits for the next scope handoff."""
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
    (repository / "preserved-deleted.txt").unlink()
    changelog = repository / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8")
        + "\n- Record disposable ordered handoff test change.\n",
        encoding="utf-8",
    )
    policy = repository / ".exo/harness_policy.toml"
    policy.write_text(
        policy.read_text(encoding="utf-8") + "\n# disposable handoff policy change\n",
        encoding="utf-8",
    )
    (repository / ".exo/tracked-runtime-policy.toml").write_text(
        "tracked runtime policy change\n", encoding="utf-8"
    )
    git(
        repository,
        "add",
        ".exo/harness_policy.toml",
        ".exo/tracked-runtime-policy.toml",
    )
    ignored_untracked = (
        repository / ".exo/e2e-runtime-ignored-untracked/runtime-state.txt"
    )
    ignored_untracked.parent.mkdir(parents=True, exist_ok=True)
    ignored_untracked.write_text("ignored disposable runtime state\n", encoding="utf-8")
    return {
        "staged": "preserved-staged.txt",
        "unstaged": "preserved-unstaged.txt",
        "untracked": "preserved-untracked.txt",
        "deleted": "preserved-deleted.txt",
        "changelog": "CHANGELOG.md",
        "policy": ".exo/harness_policy.toml",
        "tracked_ignored": ".exo/tracked-runtime-policy.toml",
        "ignored_untracked": ".exo/e2e-runtime-ignored-untracked/runtime-state.txt",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--mock-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--active-kani-blocker", action="store_true")
    parser.add_argument("--terminal-provider-failure", action="store_true")
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
        active_kani_blocker=args.active_kani_blocker,
        terminal_provider_failure=args.terminal_provider_failure,
    )
    result["active_kani_blocker"] = args.active_kani_blocker
    result["terminal_provider_failure"] = args.terminal_provider_failure
    seed_dormant_kani_controller(args.repo, result)
    result["dormant_controller_invocation_id"] = DORMANT_KANI_INVOCATION_ID
    result["preserved_local_edits"] = seed_local_edits(args.repo)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
