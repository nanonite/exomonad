#!/usr/bin/env python3
"""Seed a reviewed, completed leaf so init --continue reaches the real merge tool."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--mock-url", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--branch", default="main.leaf-a")
    parser.add_argument("--slice-id", default="leaf-a")
    args = parser.parse_args()

    from tl_loop.fsm.phase import ChildHandle, TLWaiting
    from tl_loop.loop.driver import TLLoopConfig, WorkPlan, _manifest_for_plan
    from tl_loop.state.repository_identity import canonical_repository_url
    from tl_loop.state.schema import (
        DurableReviewEvidence,
        HandoffEvidence,
        PublicationBinding,
        RepositoryIdentity,
        SliceState,
        SliceStatus,
        Verdict,
    )
    from tl_loop.state.store import RunStore, _encode_slice, create

    repo = Path(args.repo)
    state_root = repo / ".exo" / "tl-loop"
    run_id = "root"
    slice_id = args.slice_id
    plan_document = json.loads((state_root / "plan.json").read_text(encoding="utf-8"))
    plan = plan_document.get("plan")
    if not isinstance(plan, dict):
        raise ValueError("plan.json must contain a plan object")
    work_plan = WorkPlan.from_mapping(plan)
    remote_url = subprocess.check_output(
        ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
        text=True,
    ).strip()
    repository_identity = RepositoryIdentity(canonical_repository_url(remote_url))
    with urllib.request.urlopen(
        f"{args.mock_url}/api/v1/repos/owner/repo/pulls/{args.pr_number}/reviews"
    ) as response:
        reviews = json.load(response)
    exact_reviews = [
        item
        for item in reviews
        if item.get("state", "").lower() == "approved"
        and item.get("commit_id") == args.head_sha
        and item.get("user", {}).get("login") == "reviewer-e2e"
    ]
    if len(exact_reviews) != 1:
        raise ValueError("fixture requires one authenticated approval on the exact PR head")
    review = exact_reviews[0]
    reviewer_agent_id = f"review-pr-{args.pr_number}-codex"
    invocation_id = f"seed-invocation-{slice_id}"
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    slice_state = SliceState(
        id=slice_id,
        status=SliceStatus.IN_REVIEW,
        paths=("leaf.txt",),
        depends_on=(),
        base_ref="main",
        test_plan=("true",),
        agent_type="codex",
        model=None,
        branch=args.branch,
        worktree=str(repo / ".exo" / "worktrees" / slice_id),
        pr_number=args.pr_number,
        reviewed_head=args.head_sha,
        attempts=1,
        verdict=Verdict.GO,
        reviewer_agent_id=reviewer_agent_id,
        reviewer_attempt={args.head_sha: 1},
        review_evidence=DurableReviewEvidence(
            review_id=review["id"],
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            reviewer_agent_id=reviewer_agent_id,
            verdict=Verdict.GO,
            submitted_at=review["submitted_at"],
            validated_at=observed_at,
        ),
        dispatch_intent_id=f"seed-{slice_id}",
        dispatch_started_at=time.time() - 120,
        dispatch_agent_id=slice_id,
        dispatch_invocation_id=invocation_id,
        dispatch_generation=1,
        dispatch_authoritative_event_seq=1,
        dispatch_last_boundary="agent.completed",
        publication=PublicationBinding(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            head_branch=args.branch,
            base_branch="main",
            attempt=1,
            invocation_id=invocation_id,
        ),
        handoff=HandoffEvidence(
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            attempt=1,
            invocation_id=invocation_id,
            agent_id=slice_id,
            observed_at=observed_at,
        ),
    )
    create(
        run_id,
        {
            "slices": {slice_id: _encode_slice(slice_id, slice_state)},
            "owner_branch": "main",
            "owner_worktree": str(repo),
            "repository_identity": repository_identity.to_document(),
        },
        root_dir=state_root,
    )
    store = RunStore(run_id, state_root)
    store.set_plan_manifest(
        _manifest_for_plan(
            work_plan,
            run_id,
            TLLoopConfig(run_id=run_id, root_dir=state_root, branch="main"),
        ),
        slices={slice_id: slice_state},
    )
    waiting = TLWaiting({slice_id: ChildHandle(slice_id, args.branch, "codex")})
    store.checkpoint(waiting, {slice_id: slice_state}, {"tokens": 0, "wall_seconds": 0}, 0)
    print(f"Seeded reviewed leaf checkpoint: {store.load().fsm}")


if __name__ == "__main__":
    main()
