#!/usr/bin/env python3
"""Validate the one-shot lifecycle through the live server and MCP surface."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def mcp_call(socket: str, role: str, agent: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    request = json.dumps({"name": tool, "arguments": arguments})
    command = [
        "curl",
        "-fsS",
        "--unix-socket",
        socket,
        "-H",
        "Content-Type: application/json",
        "-d",
        request,
        f"http://localhost/agents/{role}/{agent}/tools/call",
    ]
    output = subprocess.check_output(command, text=True)
    return json.loads(output)


def http_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
    command = ["curl", "-fsS", "-X", method]
    if payload is not None:
        command += ["-H", "Content-Type: application/json", "-d", json.dumps(payload)]
    command.append(url)
    return json.loads(subprocess.check_output(command, text=True))


def require_success(response: dict[str, Any], label: str) -> dict[str, Any]:
    if response.get("success") is not True:
        raise AssertionError(f"{label} failed: {json.dumps(response, sort_keys=True)}")
    return response


def wait_for(predicate: Callable[[], bool], label: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {label}")


def invocation(repo: Path, agent: str) -> dict[str, Any] | None:
    path = repo / ".exo" / "agents" / agent / "invocation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def wait_for_exit(repo: Path, agent: str) -> dict[str, Any]:
    result: dict[str, Any] | None = None

    def finished() -> bool:
        nonlocal result
        result = invocation(repo, agent)
        return result is not None and result.get("status") == "exited"

    wait_for(finished, f"{agent} to exit")
    assert result is not None
    return result


def log_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def inbox_rows(repo: Path, agent: str) -> list[tuple[str, str | None]]:
    with sqlite3.connect(repo / ".exo" / "inbox.db") as connection:
        return connection.execute(
            "SELECT content, read_at FROM messages WHERE to_agent = ? ORDER BY id",
            (agent,),
        ).fetchall()


def published_heads(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".exo" / "published-heads.json"
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    return value.get("heads", value) if isinstance(value, dict) else value


def open_prs(mock_url: str) -> list[dict[str, Any]]:
    value = http_json(f"{mock_url}/api/v1/repos/test-owner/one-shot/pulls")
    assert isinstance(value, list)
    return value


def spawn_leaf(socket: str, name: str, task: str) -> None:
    require_success(
        mcp_call(
            socket,
            "root",
            "root",
            "spawn_leaf",
            {"name": name, "task": task, "agent_type": "codex"},
        ),
        f"spawn_leaf {name}",
    )


def send_message(socket: str, recipient: str, content: str, summary: str) -> dict[str, Any]:
    return require_success(
        mcp_call(
            socket,
            "root",
            "root",
            "send_tmux_message",
            {"recipient": recipient, "content": content, "summary": summary},
        ),
        f"send_tmux_message to {recipient}",
    )


def run_scenarios(repo: Path, socket: str, mock_url: str, fake_log: Path) -> None:
    spawn_leaf(socket, "one-shot", "Publish the fixture and exit after filing its PR.")
    first = wait_for_exit(repo, "one-shot-codex")
    if first.get("generation") != 1:
        raise AssertionError(f"first one-shot generation was not 1: {first}")
    wait_for(lambda: len(open_prs(mock_url)) == 1, "published PR")
    # The watcher is a sensor. A direct reviewer spawn here is a forbidden
    # bypass; controller-driven spawning is exercised by the ordered
    # real-server harness, which runs tl_loop through its reducer/executor.
    time.sleep(2.0)
    if "reviewer_spawned=true" in log_text(fake_log):
        raise AssertionError("watcher directly spawned a reviewer instead of emitting facts")
    heads = published_heads(repo)
    if len(heads) != 1 or heads[0].get("head_branch") != "main.one-shot-codex":
        raise AssertionError(f"unexpected verified PublishedHead records: {heads}")

    spawn_leaf(socket, "no-handoff", "Complete the fixture but do not file a PR.")
    no_handoff = wait_for_exit(repo, "no-handoff-codex")
    if no_handoff.get("status") != "exited":
        raise AssertionError(f"no-handoff invocation did not exit cleanly: {no_handoff}")
    if len(open_prs(mock_url)) != 1 or len(published_heads(repo)) != 1:
        raise AssertionError("clean exit without handoff created a PR or publication")
    if "review-pr-2-codex" in log_text(fake_log):
        raise AssertionError("watcher spawned a reviewer for the no-handoff leaf")

    worker = require_success(
        mcp_call(
            socket,
            "root",
            "root",
            "spawn_worker",
            {"name": "live-guidance", "task": "Wait for exact-pane guidance.", "agent_type": "codex"},
        ),
        "spawn_worker live-guidance",
    )
    spawned = worker.get("result", {}).get("spawned", [])
    worker_result = spawned[0] if spawned else {}
    pane_id = worker_result.get("pane_id") or worker_result.get("paneId")
    if not pane_id:
        raise AssertionError(f"spawn_worker did not return a pane id: {worker}")
    live = send_message(socket, "live-guidance", "[LIVE-EXACT-PANE] exact pane", "live guidance")
    if "tmux" not in json.dumps(live).lower():
        raise AssertionError(f"live guidance did not use tmux delivery: {live}")
    wait_for(lambda: "[LIVE-STDIN]" in log_text(fake_log), "live exact-pane receipt")

    routing = repo / ".exo" / "agents" / "live-guidance-codex" / "routing.json"
    routing.write_text(json.dumps({"pane_id": "%999999", "parent_tab": "TL"}) + "\n", encoding="utf-8")
    stale = send_message(socket, "live-guidance", "[STALE-PANE] durable fallback", "stale pane")
    if "durable" not in json.dumps(stale).lower():
        raise AssertionError(f"stale pane was not rejected to durable delivery: {stale}")
    wait_for(
        lambda: any(row[0] == "[STALE-PANE] durable fallback" and row[1] is None for row in inbox_rows(repo, "live-guidance-codex")),
        "stale-pane durable inbox row",
    )
    require_success(
        mcp_call(socket, "root", "root", "close_worker_pane", {"pane_id": pane_id}),
        "close live-guidance worker",
    )

    owner_routing = repo / ".exo" / "agents" / "one-shot-codex" / "routing.json"
    owner_routing.write_text(json.dumps({"pane_id": "%999998", "parent_tab": "TL"}) + "\n", encoding="utf-8")
    dormant = "[DORMANT-RESUME] guidance survives the exited Codex invocation"
    dormant_delivery = send_message(socket, "one-shot", dormant, "dormant resume guidance")
    if "durable" not in json.dumps(dormant_delivery).lower():
        raise AssertionError(f"dormant guidance was not durable: {dormant_delivery}")
    wait_for(
        lambda: any(row[0] == dormant and row[1] is None for row in inbox_rows(repo, "one-shot-codex")),
        "dormant inbox row",
    )

    pr = open_prs(mock_url)[0]
    pr_number = int(pr["number"])
    current_sha = pr["head"]["sha"]
    http_json(
        f"{mock_url}/_control/stale_once",
        "POST",
        {"pr_number": pr_number, "stale_sha": "stale-for-cas", "fresh_sha": current_sha},
    )
    resume_args = {"pr_number": pr_number, "task": "Resume the existing owner and consume its dormant guidance."}
    stale_resume = mcp_call(socket, "root", "root", "resume_pr", resume_args)
    if stale_resume.get("success") is not False or "head SHA changed" not in json.dumps(stale_resume):
        raise AssertionError(f"stale expected_head_sha was not rejected: {stale_resume}")

    resumed = require_success(mcp_call(socket, "root", "root", "resume_pr", resume_args), "matching resume_pr")
    if resumed.get("result", {}).get("head_sha") != current_sha:
        raise AssertionError(f"matching resume returned the wrong head SHA: {resumed}")
    resumed_record = wait_for_exit(repo, "one-shot-codex")
    if resumed_record.get("generation") != 2:
        raise AssertionError(f"resume_pr did not create generation 2: {resumed_record}")
    wait_for(
        lambda: dormant in log_text(fake_log) and "[RESUME-INBOX]" in log_text(fake_log),
        "dormant guidance in resumed Codex inbox",
    )

    worktrees = subprocess.check_output(["git", "-C", str(repo), "worktree", "list", "--porcelain"], text=True)
    owner_paths = [line for line in worktrees.splitlines() if line.rstrip().endswith("/.exo/worktrees/one-shot-codex")]
    if len(owner_paths) != 1 or "one-shot-codex-2" in worktrees:
        raise AssertionError(f"resume_pr created a sibling owner/worktree: {worktrees}")
    branches = subprocess.check_output(["git", "-C", str(repo), "branch", "--format=%(refname:short)"], text=True)
    if branches.count("main.one-shot-codex\n") != 1 or "main.one-shot-codex-2" in branches:
        raise AssertionError(f"resume_pr changed the owner branch identity: {branches}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("socket")
    parser.add_argument("mock_url")
    parser.add_argument("fake_log", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    failures: list[str] = []
    try:
        run_scenarios(args.repo, args.socket, args.mock_url, args.fake_log)
    except (AssertionError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        failures.append(str(error))
    args.result.write_text(
        "One-shot lifecycle E2E validation\n"
        f"Failures: {len(failures)}\n"
        + "".join(f"- {failure}\n" for failure in failures),
        encoding="utf-8",
    )
    if failures:
        print(f"FAIL: {failures[0]}", file=sys.stderr)
        return 1
    print("PASS: clean handoff, no-handoff, dormant resume, exact pane, stale pane, and SHA CAS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
