#!/usr/bin/env python3
"""File and approve a PR against the mock Forgejo, then seed the durable
records (published-heads.json, agent identity, agent invocation) that
worktree_event_watcher.rs requires before it will route that PR's review/CI
observations to a TL slice (chainlink #907 continuation phase).

Prints "<pr_number> <head_sha>" on success.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path


def post(mock_url: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        mock_url + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-url", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-branch", default="main.leaf-a")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--slice-id", default="leaf-a")
    args = parser.parse_args()

    repo = Path(args.repo)

    pr = post(
        args.mock_url,
        "/api/v1/repos/owner/repo/pulls",
        {
            "title": "leaf-a change",
            "head": args.head_branch,
            "base": "main",
            "body": "init-recovery continuation fixture",
        },
    )
    pr_number = pr["number"]
    post(
        args.mock_url,
        f"/api/v1/repos/owner/repo/pulls/{pr_number}/reviews",
        {"event": "APPROVED", "commit_id": args.head_sha},
    )

    # published-heads.json: durable record that a PR was filed for this
    # slice, exactly as file_pr would have written before the crash.
    published_heads = {
        "schema_version": 1,
        "heads": [
            {
                "pr_number": pr_number,
                "head_branch": args.head_branch,
                "base_branch": "main",
                "head_sha": args.head_sha,
                "author_agent": args.slice_id,
                "author_role": "dev",
                "provenance": "ledger_owned",
                "slice_id": args.slice_id,
                "invocation_id": f"seed-invocation-{args.slice_id}",
                "invocation_trigger": "spawn",
                "invocation_runtime": "codex",
            }
        ],
    }
    (repo / ".exo" / "published-heads.json").write_text(json.dumps(published_heads, indent=2))

    # Agent identity + a terminal ("exited") invocation record: the dev is a
    # one-shot process that already exited after filing its PR. The watcher
    # must route review/CI events to this owner without requiring the dev
    # process to still be alive (chainlink #904).
    agent_dir = repo / ".exo" / "agents" / args.slice_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "identity.json").write_text(
        json.dumps(
            {
                "agent_name": args.slice_id,
                "slug": args.slice_id,
                "agent_type": "codex",
                "birth_branch": args.head_branch,
                "parent_branch": "main",
                "working_dir": f".exo/worktrees/{args.slice_id}",
                "display_name": args.slice_id,
                "topology": "worktree_per_agent",
                "model": None,
                "effort": None,
                "ledger_owned": True,
                "slice_id": args.slice_id,
            },
            indent=2,
        )
    )
    (agent_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": f"seed-invocation-{args.slice_id}",
                "runtime": "codex",
                "trigger": "spawn",
                "mode": "interactive",
                "routing": {"window_id": "@42"},
                "started_at": 1700000000,
                "ended_at": 1700000060,
                "status": "exited",
                "exit_code": 0,
                "pr_number": pr_number,
                "generation": 1,
            },
            indent=2,
        )
    )

    # Reviewer provenance is intentionally durable even though the reviewer
    # identity is not registered as a live agent. This models the one-shot
    # reviewer cleanup window: watcher_pr_state must resolve the reviewer from
    # the exact PR/head-bound Review invocation, not from editable PR metadata
    # or the current identity registry.
    reviewer_agent = f"review-pr-{pr_number}-codex"
    reviewer_dir = repo / ".exo" / "agents" / reviewer_agent
    reviewer_dir.mkdir(parents=True, exist_ok=True)
    (reviewer_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": f"seed-review-invocation-{pr_number}",
                "runtime": "codex",
                "trigger": "review",
                "mode": "interactive",
                "routing": {"window_id": "@42"},
                "started_at": 1700000000,
                "ended_at": 1700000060,
                "status": "exited",
                "exit_code": 0,
                "pr_number": pr_number,
                "head_sha": args.head_sha,
                "generation": 1,
                "runtime_agent_id": reviewer_agent,
            },
            indent=2,
        )
    )

    print(f"{pr_number} {args.head_sha}")


if __name__ == "__main__":
    main()
