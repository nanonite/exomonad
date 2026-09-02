"""Durable recursive fixture builders for the #1057 matrix."""

from __future__ import annotations

from pathlib import Path

import real_server_transport as real


def plan() -> real.WorkPlan:
    """Build nested work with same-order parallelism and a later barrier."""
    return real.WorkPlan(
        sub_tls=(
            real.SubTLTask(
                "sub-a",
                real.WorkPlan(
                    sub_tls=(real.SubTLTask("nested-a", real.WorkPlan(), order=1),)
                ),
                order=1,
            ),
            real.SubTLTask("sub-b", real.WorkPlan(), order=1),
            real.SubTLTask("sub-c", real.WorkPlan(), order=2),
        )
    )


def seed_aggregate_publication(
    root: Path, repo: Path, work_plan: real.WorkPlan
) -> tuple[str, real.WorkPlan]:
    """Create terminal child checkpoints so publication is the next effect."""
    run_id, seeded_plan, _, _ = real.seed_dispatch_restart_run(root, repo, work_plan)
    state_root = root / "controller-state"
    parent_dir = state_root / run_id
    head_sha = real.git(repo, "rev-parse", "main")
    for task in seeded_plan.sub_tls:
        child_plan = real.WorkPlan(
            leaves=(real.LeafTask(f"{task.name}-output", "publication fixture"),)
        )
        child_config = real.TLLoopConfig(
            active=True,
            run_id=task.name,
            root_dir=parent_dir,
            branch=f"main.{task.name}",
            parent_branch="main",
            worktree=repo / ".exo" / "agents" / task.name,
            working_dir=str(repo / ".exo" / "agents" / task.name),
        )
        child_store = real.RunStore(task.name, parent_dir)
        child_slices = real._initial_slices(
            child_plan, child_config, parent_dir, task.name
        )
        real.create(
            task.name,
            {
                "owner_branch": f"main.{task.name}",
                "owner_worktree": str(repo / ".exo" / "agents" / task.name),
                "ledger_run_id": real.server_run_id(repo),
                "slices": child_slices,
                "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
            },
            root_dir=parent_dir,
        )
        child_state = child_store.load()
        child_store.set_plan_manifest(
            real._manifest_for_plan(child_plan, task.name, child_config),
            slices=child_state.slices,
        )
        child_state = child_store.load()
        output_id = next(iter(child_state.slices))
        output = real.replace(
            child_state.slices[output_id],
            status=real.SliceStatus.MERGED,
            reviewed_head=head_sha,
            review_patch_digests={head_sha: "publication-fixture"},
        )
        child_store.checkpoint(
            real.TLDone(),
            {output_id: output},
            child_state.budgets,
            child_state.events.last_consumed_offset,
        )
    return run_id, seeded_plan
