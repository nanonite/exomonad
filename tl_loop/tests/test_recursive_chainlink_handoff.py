"""Recursive handoff and unlinked post-merge replay regressions for #1169 and #1170."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import JsonObject
from tl_loop.fsm.phase import TLPhase
from tl_loop.fsm.post_merge import PostMergePhase, PostMergeState
from tl_loop.fsm.scope import TLAllMerged as RecursiveTLAllMerged
from tl_loop.fsm.scope import TLDone as RecursiveTLDone
from tl_loop.loop.driver import (
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _child_handoff_ready,
    _initial_slices,
    _manifest_for_plan,
    _sub_tl_worktree,
    derive_child_branch,
    run_tl_loop,
)
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmResponse
from tl_loop.select.policy import validate_policy
from tl_loop.state.schema import RepositoryIdentity, SliceStatus
from tl_loop.state.store import RunStore, create
from tl_loop.tests.test_driver import (
    RecordingTransport,
    ReviewBackend,
    SyntheticQueue,
    _selector_policy,
)

ROOT_RUN_ID = "root"
CHILD_RUN_ID = "implementation"
LEAF_ID = "feature"
MERGED_PR = 45
HEAD_SHA = "a" * 40
PARENT_COMMIT_SHA = "b" * 40
REPOSITORY_IDENTITY = RepositoryIdentity("org", "repo", "main")


@dataclass
class ChangelogRecordingTransport(RecordingTransport):
    """Answers the unlinked CHANGELOG context read for the merged child slice."""

    def call_tool(
        self, role: str, name: str, tool_name: str, arguments: JsonObject
    ) -> JsonObject:
        if tool_name == "post_merge_changelog_context":
            self.calls.append((tool_name, arguments))
            return {
                "success": True,
                "result": {
                    "child_id": arguments["child_id"],
                    "changelog_present": True,
                    "unreleased_present": True,
                    "unreleased_section": "### Added\n- Complete the merged work (#45)",
                    "changed_paths": ["src/feature.rs"],
                    "pr_edited_changelog": True,
                    "pr_title": "Complete merged work",
                    "pr_body": "",
                },
            }
        return super().call_tool(role, name, tool_name, arguments)


def _recursive_plan() -> tuple[dict[str, object], WorkPlan]:
    document: dict[str, object] = {
        "sub_tls": [
            {
                "name": CHILD_RUN_ID,
                "order": 1,
                "integration": {"aggregate_pr_required": False},
                "plan": {
                    "leaves": [{"name": LEAF_ID, "task": "complete merged work"}]
                },
            },
            {
                "name": "next-stage",
                "order": 2,
                "integration": {"aggregate_pr_required": False},
                "plan": {},
            },
        ]
    }
    return document, WorkPlan.from_mapping(document)


def _seed_parent_and_captured_child(tmp_path: Path) -> tuple[WorkPlan, TLLoopConfig, RunStore]:
    document, plan = _recursive_plan()
    config = TLLoopConfig(
        run_id=ROOT_RUN_ID,
        root_dir=tmp_path,
        project_root=tmp_path,
        active=True,
        branch="main",
        max_parallel_slices=1,
        keep_alive_on_waiting=False,
        repository_identity=REPOSITORY_IDENTITY,
        policy=validate_policy(_selector_policy()),
        tl_model_choice=RlmModelChoice(
            model_id="tl-model",
            backend=ReviewBackend(
                [RlmResponse({"action": "captured", "reason": "the PR added its own entry"})]
            ),
            role="tl",
            store=RlmCallStore(),
            context_length=20_000,
        ),
    )
    parent_manifest = _manifest_for_plan(plan, ROOT_RUN_ID, config)
    parent_slices = _bind_initial_slices(
        _initial_slices(plan, config, tmp_path, ROOT_RUN_ID), parent_manifest
    )
    create(
        ROOT_RUN_ID,
        {
            "plan_manifest": parent_manifest.to_document(),
            "slices": parent_slices,
            "repository_identity": {
                "owner": REPOSITORY_IDENTITY.owner,
                "repo": REPOSITORY_IDENTITY.repo,
                "base_branch": REPOSITORY_IDENTITY.base_branch,
            },
        },
        root_dir=tmp_path,
    )
    parent_store = RunStore(ROOT_RUN_ID, tmp_path)
    child_task = plan.sub_tls[0]
    child_manifest = parent_manifest.child_manifests[
        f"{ROOT_RUN_ID}/sub_tl/{CHILD_RUN_ID}"
    ]
    child_branch = derive_child_branch(config.branch, CHILD_RUN_ID)
    child_worktree = _sub_tl_worktree(config, parent_store.run_dir, ROOT_RUN_ID, child_task)
    child_config = replace(
        config,
        run_id=CHILD_RUN_ID,
        root_dir=parent_store.run_dir,
        branch=child_branch,
        worktree=str(child_worktree),
        parent_branch=config.branch,
        parent_run_id=ROOT_RUN_ID,
        parent_agent_id=ROOT_RUN_ID,
        depth=1,
        declared_manifest=child_manifest,
    )
    child_plan = child_task.plan
    assert isinstance(child_plan, WorkPlan)
    child_slices = _bind_initial_slices(
        _initial_slices(child_plan, child_config, parent_store.run_dir, CHILD_RUN_ID),
        child_manifest,
    )
    create(
        CHILD_RUN_ID,
        {
            "plan_manifest": child_manifest.to_document(),
            "slices": child_slices,
            "owner_branch": child_branch,
            "owner_worktree": str(child_worktree),
            "parent_branch": config.branch,
            "parent_run_id": ROOT_RUN_ID,
            "parent_agent_id": ROOT_RUN_ID,
            "depth": 1,
            "repository_identity": {
                "owner": REPOSITORY_IDENTITY.owner,
                "repo": REPOSITORY_IDENTITY.repo,
                "base_branch": REPOSITORY_IDENTITY.base_branch,
            },
        },
        root_dir=parent_store.run_dir,
    )
    child_store = RunStore(CHILD_RUN_ID, parent_store.run_dir)
    child_state = child_store.load()
    current = replace(
        child_state.slices[LEAF_ID],
        status=SliceStatus.MERGED,
        base_ref=child_branch,
        pr_number=MERGED_PR,
        reviewed_head=HEAD_SHA,
        post_merge=PostMergeState(
            PostMergePhase.PARENT_BRANCH_SYNCED,
            {
                "child_id": LEAF_ID,
                "repository": "org/repo",
                "parent_branch": child_branch,
                "pr_number": str(MERGED_PR),
                "head_sha": HEAD_SHA,
                "merge_journal_id": "merge-pr-45-confirmed",
                "lane_epoch": "1",
                "parent_commit_sha": PARENT_COMMIT_SHA,
            },
        ),
        reconciliation={
            "confirmed_stage": "merge",
            "authoritative_evidence": ["published_pr", "pr_state", "merged"],
            "missing_evidence": [],
            "conflicts": [],
            "next_action": "adopt_merged",
            "merge_journal_id": "merge-pr-45-confirmed",
            "remote_head_sha": PARENT_COMMIT_SHA,
        },
    )
    child_store.checkpoint(
        RecursiveTLAllMerged(
            scope_path=(child_manifest.scope_id,),
            plan_digest=child_manifest.digest or "",
        ),
        {**child_state.slices, LEAF_ID: current},
        child_state.budgets,
        child_state.events.last_consumed_offset,
    )
    return plan, config, parent_store


def test_merged_but_unfinished_child_is_not_ready_for_parent_handoff(tmp_path: Path) -> None:
    plan, config, parent_store = _seed_parent_and_captured_child(tmp_path)
    child_state = RunStore(CHILD_RUN_ID, parent_store.run_dir).load()

    assert child_state.slices[LEAF_ID].chainlink_issue_id is None
    assert not _child_handoff_ready(child_state, ROOT_RUN_ID)

    transport = ChangelogRecordingTransport()
    result = run_tl_loop(
        ROOT_RUN_ID,
        plan,
        SyntheticQueue([]),
        EffectClient(transport),
        config=config,
        root_dir=tmp_path,
    )

    final_parent = result.final_state
    final_child = RunStore(CHILD_RUN_ID, parent_store.run_dir).load()
    completed_slice = final_child.slices[LEAF_ID]
    assert isinstance(final_parent.recursive_fsm, RecursiveTLDone) or (
        final_parent.fsm.phase is TLPhase.TLDone
    )
    assert final_parent.current_order == 2
    assert final_parent.slices[CHILD_RUN_ID].status is SliceStatus.MERGED
    assert final_parent.slices["next-stage"].status is SliceStatus.MERGED
    assert completed_slice.post_merge is not None
    assert completed_slice.post_merge.phase is PostMergePhase.COMPLETE
    assert completed_slice.post_merge.evidence["issue_close_outcome"] == "not_applicable"
    assert completed_slice.post_merge.evidence["changelog_outcome"] == "captured"
    assert completed_slice.chainlink_issue_id is None

    effect_names = [name for name, _ in transport.calls]
    assert "chainlink_issue_close" not in effect_names
    assert "chainlink_issue_create" not in effect_names
    assert "post_merge_changelog" not in effect_names
    assert "post_merge_changelog_entry" not in effect_names
    assert effect_names.count("post_merge_changelog_context") == 1
    assert "merge_pr" not in effect_names
    assert "file_pr" not in effect_names
    assert "spawn_leaf" not in effect_names
    assert "spawn_worker" not in effect_names
    assert all(arguments.get("pr_number") != 44 for _, arguments in transport.calls)
    assert _child_handoff_ready(final_child, ROOT_RUN_ID)
