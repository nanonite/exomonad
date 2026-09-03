"""Durable recursive fixture builders for the #1057 matrix."""

from __future__ import annotations

import json
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


def seed_aggregate_publication(
    root: Path,
    repo: Path,
    work_plan: real.WorkPlan,
    *,
    case_name: str = "aggregate-publication",
) -> tuple[str, real.WorkPlan]:
    """Seed only root state; production child controllers own nested work."""
    run_id, seeded_plan, _, _ = real.seed_dispatch_restart_run(root, repo, work_plan)
    parent_marker = repo / ".exo" / f"1057-parent-branches-{case_name}.json"
    parent_branches: list[str] = []
    for task in seeded_plan.sub_tls:
        owner_branch = f"main.{task.name}"
        owner_worktree = real.agent_worktree(repo, owner_branch)
        real.commit_fixture_worktree(
            owner_worktree,
            relative_path=f"e2e-fixtures/{case_name}/{task.name}.txt",
            content=f"{task.name} aggregate source\n",
            message=f"Prepare {task.name} aggregate source",
        )
        parent_branches.append(owner_branch)
        child_plan = (
            task.plan
            if isinstance(task.plan, real.WorkPlan)
            else real.WorkPlan.from_mapping(task.plan)
        )
        for nested in child_plan.sub_tls:
            nested_branch = f"{owner_branch}.{nested.name}"
            nested_worktree = real.agent_worktree(repo, nested_branch)
            real.git(nested_worktree, "merge", "-q", "--ff-only", owner_branch)
            real.git(nested_worktree, "push", "-q", "origin", nested_branch)
            parent_branches.append(nested_branch)
    parent_marker.parent.mkdir(parents=True, exist_ok=True)
    parent_marker.write_text(
        json.dumps(sorted(set(parent_branches)), indent=2) + "\n",
        encoding="utf-8",
    )
    return run_id, seeded_plan
