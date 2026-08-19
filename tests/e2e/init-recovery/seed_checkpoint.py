#!/usr/bin/env python3
"""Seed a real nonterminal root checkpoint for the init-recovery continuation
phase (chainlink #907/#904).

Writes a SPAWNED slice whose PR number is known but whose review outcome
(reviewed_head, verdict, ci_state) is NOT yet recorded -- exactly the shape
of a one-shot dev that filed its PR and exited before the controller
durably recorded the review/CI evidence (the motivating PR #42 crash
window). The embedded controller started by `exomonad init` must recover
that evidence itself from live watcher/ledger observations; this script
only seeds the "before" state.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--branch", default="main.leaf-a")
    parser.add_argument("--slice-id", default="leaf-a")
    args = parser.parse_args()

    from tl_loop.fsm.phase import ChildHandle, TLWaiting
    from tl_loop.state.schema import SliceState, SliceStatus
    from tl_loop.state.store import RunStore, _encode_slice, create

    repo = Path(args.repo)
    state_root = repo / ".exo" / "tl-loop"
    run_id = "root"
    slice_id = args.slice_id

    slice_state = SliceState(
        id=slice_id,
        status=SliceStatus.SPAWNED,
        paths=("leaf.txt",),
        depends_on=(),
        base_ref="main",
        test_plan=("true",),
        agent_type="codex",
        model=None,
        branch=args.branch,
        worktree=str(repo / ".exo" / "worktrees" / slice_id),
        pr_number=args.pr_number,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        dispatch_intent_id=f"seed-{slice_id}",
        dispatch_started_at=time.time() - 120,
        dispatch_agent_id=slice_id,
        dispatch_authoritative_event_seq=1,
    )
    create(
        run_id,
        {
            "slices": {slice_id: _encode_slice(slice_id, slice_state)},
            "owner_branch": "main",
            "owner_worktree": str(repo),
        },
        root_dir=state_root,
    )
    store = RunStore(run_id, state_root)
    waiting = TLWaiting({slice_id: ChildHandle(slice_id, args.branch, "codex")})
    store.checkpoint(waiting, {slice_id: slice_state}, {"tokens": 0, "wall_seconds": 0}, 0)
    print(f"Seeded nonterminal checkpoint: {store.load().fsm}")


if __name__ == "__main__":
    main()
