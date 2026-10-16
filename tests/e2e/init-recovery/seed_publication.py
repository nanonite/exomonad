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
    parser.add_argument("--reviewer-token", required=True)
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
    request = urllib.request.Request(
        args.mock_url + f"/api/v1/repos/owner/repo/pulls/{pr_number}/reviews",
        data=json.dumps({"event": "APPROVED", "commit_id": args.head_sha}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"token {args.reviewer_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        review = json.load(response)
    if type(review.get("id")) is not int or review["id"] <= 0:
        raise ValueError("mock Forgejo review omitted its durable ID")

    # published-heads.json: an isolated registry fixture for the already-filed
    # PR. The continuation under test exercises the production merge tool.
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
    events = repo / ".exo" / "events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "reviewer-invocations.jsonl").write_text(
        json.dumps(
            {
                "type": "agent.invocation.finished",
                "agent_id": reviewer_agent,
                "data": {
                    "trigger": "review",
                    "pr_number": pr_number,
                    "head_sha": args.head_sha,
                    "runtime_agent_id": reviewer_agent,
                },
            }
        )
        + "\n"
    )

    print(f"{pr_number} {args.head_sha}")


if __name__ == "__main__":
    main()
