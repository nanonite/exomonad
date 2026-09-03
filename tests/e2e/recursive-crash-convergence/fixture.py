"""Durable recursive fixture builders for the #1057 matrix."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import real_server_transport as real


def plan() -> real.WorkPlan:
    """Build nested work with same-order parallelism and a later barrier."""
    nested = real.WorkPlan(
        leaves=(real.LeafTask("nested-output", "nested publication fixture"),)
    )
    return real.WorkPlan(
        sub_tls=(
            real.SubTLTask(
                "sub-a",
                real.WorkPlan(sub_tls=(real.SubTLTask("nested-a", nested, order=1),)),
                agent_id="sub-a",
                order=1,
            ),
            real.SubTLTask(
                "sub-b",
                real.WorkPlan(
                    leaves=(
                        real.LeafTask("sub-b-output", "parallel publication fixture"),
                    )
                ),
                agent_id="sub-b",
                order=1,
            ),
            real.SubTLTask(
                "sub-c",
                real.WorkPlan(
                    leaves=(
                        real.LeafTask("sub-c-output", "ordered publication fixture"),
                    )
                ),
                agent_id="sub-c",
                order=2,
            ),
        )
    )


def _work_plan(value: real.WorkPlan | Mapping[str, object]) -> real.WorkPlan:
    if isinstance(value, real.WorkPlan):
        return value
    return real.WorkPlan.from_mapping(value)


def _create_nested_aggregate(
    repo: Path,
    forgejo_url: str,
    forgejo_owner: str,
    forgejo_repo: str,
    forgejo_token: str,
    reviewer_token: str,
    case_name: str,
    task: real.SubTLTask,
    nested: real.SubTLTask,
) -> tuple[int, str, str, str]:
    """Publish and review a real nested aggregate against the direct parent branch."""
    parent_branch = f"main.{task.name}"
    real.git(repo, "push", "-q", "origin", parent_branch)
    branch = f"aggregate/{case_name}/{task.name}/{nested.name}"
    head_sha = real.create_branch_with_commit(
        repo,
        branch,
        parent_branch,
        relative_path=f"e2e-fixtures/{case_name}/{task.name}-{nested.name}.txt",
        content="nested aggregate publication\n",
        message=f"Publish {nested.name} into {parent_branch}",
    )
    created = real.json_request(
        "POST",
        f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls",
        {
            "title": f"Nested aggregate {nested.name} into {parent_branch}",
            "body": "#1057 nested PR-on-PR fixture",
            "head": branch,
            "base": parent_branch,
        },
        token=forgejo_token,
    )
    if not isinstance(created, Mapping) or type(created.get("number")) is not int:
        raise real.HarnessError(f"nested aggregate PR creation failed: {created!r}")
    number = int(created["number"])
    review = real.json_request(
        "POST",
        f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls/{number}/reviews",
        {"event": "APPROVED", "commit_id": head_sha},
        token=reviewer_token,
    )
    if not isinstance(review, Mapping) or type(review.get("id")) is not int:
        raise real.HarnessError(f"nested aggregate review creation failed: {review!r}")
    details = real.json_request(
        "GET",
        f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls/{number}",
        token=forgejo_token,
    )
    head = details.get("head") if isinstance(details, Mapping) else None
    base = details.get("base") if isinstance(details, Mapping) else None
    head_ref = head.get("ref") if isinstance(head, Mapping) else None
    base_ref = base.get("ref") if isinstance(base, Mapping) else None
    observed_head = head.get("sha") if isinstance(head, Mapping) else None
    if (
        head_ref != branch
        or base_ref != parent_branch
        or observed_head != head_sha
        or head_sha == real.git(repo, "rev-parse", parent_branch)
    ):
        raise real.HarnessError(
            "nested aggregate PR lost direct-parent identity or a meaningful head: "
            f"{details!r}"
        )
    return number, head_sha, real.git(repo, "rev-parse", parent_branch), branch


def _seed_terminal_scope(
    *,
    parent_dir: Path,
    repo: Path,
    task: real.SubTLTask,
    child_plan: real.WorkPlan,
    owner_branch: str,
    owner_worktree: Path,
    heads: Mapping[str, tuple[str, str]],
    candidates: Mapping[str, real.IntegrationCandidateState] | None = None,
) -> real.RunState:
    """Persist a terminal scope using its real recursive plan and all child nodes."""
    config = real.TLLoopConfig(
        active=True,
        run_id=task.name,
        root_dir=parent_dir,
        branch=owner_branch,
        parent_branch="main",
        worktree=owner_worktree,
        working_dir=str(owner_worktree),
    )
    store = real.RunStore(task.name, parent_dir)
    slices = real._initial_slices(child_plan, config, parent_dir, task.name)
    real.create(
        task.name,
        {
            "owner_branch": owner_branch,
            "owner_worktree": str(owner_worktree),
            "ledger_run_id": real.server_run_id(repo),
            "slices": slices,
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        },
        root_dir=parent_dir,
    )
    state = store.load()
    store.set_plan_manifest(
        real._manifest_for_plan(child_plan, task.name, config), slices=state.slices
    )
    state = store.load()
    updated = dict(state.slices)
    for slice_id, current in state.slices.items():
        head_sha, patch_digest = heads.get(
            slice_id,
            (real.git(repo, "rev-parse", owner_branch), f"fixture:{slice_id}"),
        )
        updated[slice_id] = real.replace(
            current,
            status=real.SliceStatus.MERGED,
            reviewed_head=head_sha,
            review_patch_digests={head_sha: patch_digest},
        )
    candidate_records = dict(candidates or {})
    integration = real.replace(
        state.integration,
        sub_tl_states={
            slice_id: real.IntegrationLifecycle.MERGED for slice_id in updated
        },
        candidates=candidate_records,
    )
    return store.checkpoint(
        real.TLDone(),
        updated,
        state.budgets,
        state.events.last_consumed_offset,
        current_order=state.current_order,
        ordered_stages=state.ordered_stages,
        integration=integration,
    )


def seed_aggregate_publication(
    root: Path,
    repo: Path,
    work_plan: real.WorkPlan,
    *,
    forgejo_url: str | None = None,
    forgejo_owner: str = "owner",
    forgejo_repo: str = "repo",
    forgejo_token: str | None = None,
    forgejo_reviewer_token: str | None = None,
    case_name: str = "aggregate-publication",
) -> tuple[str, real.WorkPlan]:
    """Seed actual recursive child checkpoints and a real nested PR-on-PR."""
    run_id, seeded_plan, _, _ = real.seed_dispatch_restart_run(root, repo, work_plan)
    state_root = root / "controller-state"
    parent_dir = state_root / run_id
    parent_marker = repo / ".exo" / f"1057-parent-branches-{case_name}.json"
    parent_branches: list[str] = []
    for task in seeded_plan.sub_tls:
        child_plan = _work_plan(task.plan)
        owner_branch = f"main.{task.name}"
        owner_worktree = repo / ".exo" / "agents" / task.name
        real.commit_fixture_worktree(
            owner_worktree,
            relative_path=f"e2e-fixtures/{case_name}/{task.name}.txt",
            content=f"{task.name} aggregate source\n",
            message=f"Prepare {task.name} aggregate source",
        )
        parent_branches.append(owner_branch)
        parent_marker.parent.mkdir(parents=True, exist_ok=True)
        parent_marker.write_text(
            json.dumps(sorted(set(parent_branches)), indent=2) + "\n",
            encoding="utf-8",
        )
        heads: dict[str, tuple[str, str]] = {}
        candidates: dict[str, real.IntegrationCandidateState] = {}
        for nested in child_plan.sub_tls:
            if not (forgejo_url and forgejo_token and forgejo_reviewer_token):
                continue
            pr_number, head_sha, base_sha, branch = _create_nested_aggregate(
                repo,
                forgejo_url,
                forgejo_owner,
                forgejo_repo,
                forgejo_token,
                forgejo_reviewer_token,
                case_name,
                task,
                nested,
            )
            merged = real.json_request(
                "POST",
                f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls/{pr_number}/merge",
                {},
                token=forgejo_token,
            )
            if not isinstance(merged, Mapping) or merged.get("merged") is not True:
                raise real.HarnessError(
                    f"nested aggregate PR did not merge into {owner_branch}: {merged!r}"
                )
            merged_snapshot = real.json_request(
                "GET",
                f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls/{pr_number}",
                token=forgejo_token,
            )
            merged_state = (
                merged_snapshot.get("state")
                if isinstance(merged_snapshot, Mapping)
                else None
            )
            merged_flag = (
                merged_snapshot.get("merged")
                if isinstance(merged_snapshot, Mapping)
                else None
            )
            merged_base = (
                merged_snapshot.get("base")
                if isinstance(merged_snapshot, Mapping)
                else None
            )
            merged_base_ref = (
                merged_base.get("ref") if isinstance(merged_base, Mapping) else None
            )
            if (
                merged_state != "closed"
                or merged_flag is not True
                or merged_base_ref != owner_branch
            ):
                raise real.HarnessError(
                    "nested aggregate merge was not durably observed on its direct parent: "
                    f"{merged_snapshot!r}"
                )
            real.git(repo, "fetch", "-q", "origin", owner_branch)
            real.git(
                owner_worktree, "merge", "-q", "--ff-only", f"origin/{owner_branch}"
            )
            real.git(
                repo,
                "merge-base",
                "--is-ancestor",
                head_sha,
                f"origin/{owner_branch}",
            )
            heads[nested.name] = (head_sha, f"nested:{pr_number}:{head_sha}")
            candidates[nested.name] = real.IntegrationCandidateState(
                lifecycle=real.IntegrationLifecycle.MERGED,
                aggregate_pr_number=pr_number,
                aggregate_head_sha=head_sha,
                aggregate_patch_digest=f"nested:{pr_number}:{head_sha}",
                aggregate_original_base_sha=base_sha,
                integration_owner_id=f"{task.name}:{nested.name}:integration",
                integration_owner_run_id=nested.name,
                integration_owner_branch=branch,
                integration_owner_worktree=str(owner_worktree),
                head_sha=head_sha,
                patch_digest=f"nested:{pr_number}:{head_sha}",
                validated_base_sha=base_sha,
                ci_status="success",
                stage_verification="passed",
            )
            nested_plan = _work_plan(nested.plan)
            nested_worktree = repo / ".exo" / "agents" / nested.name
            _seed_terminal_scope(
                parent_dir=parent_dir / task.name,
                repo=repo,
                task=nested,
                child_plan=nested_plan,
                owner_branch=branch,
                owner_worktree=nested_worktree,
                heads={
                    leaf.name: (head_sha, f"nested:{pr_number}:{head_sha}")
                    for leaf in nested_plan.leaves
                },
            )
        _seed_terminal_scope(
            parent_dir=parent_dir,
            repo=repo,
            task=task,
            child_plan=child_plan,
            owner_branch=owner_branch,
            owner_worktree=owner_worktree,
            heads=heads,
            candidates=candidates,
        )
    return run_id, seeded_plan
