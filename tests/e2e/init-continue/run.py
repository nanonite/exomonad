#!/usr/bin/env python3
"""Verify --continue preserves observed ownership across a real init restart.

The shell harness owns disposable infrastructure. This module consumes only
state written by the server: invocation records, the publication registry,
the TL checkpoint, and the authoritative ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def wait_for(predicate: Any, label: str, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {label}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def invocation_records(repo: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted((repo / ".exo" / "agents").glob("*/invocation.json")):
        record = read_json(path)
        if isinstance(record.get("invocation_id"), str):
            records[path.parent.name] = record
    return records


def invocation_ids(records: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        name: record["invocation_id"]
        for name, record in records.items()
        if record.get("invocation_id")
    }


def published_heads(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".exo" / "published-heads.json"
    if not path.is_file():
        return []
    value = read_json(path)
    heads = value.get("heads", value)
    return heads if isinstance(heads, list) else []


def ledger_events(repo: Path) -> list[dict[str, Any]]:
    project_root = Path(__file__).resolve().parents[3]
    ordered_dir = project_root / "tests/e2e/ordered-recursive"
    sys.path.insert(0, str(ordered_dir))
    import real_server_transport as real

    return real.server_ledger_events(repo)


def mcp_call(socket: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    output = subprocess.check_output(
        [
            "curl",
            "-fsS",
            "--unix-socket",
            socket,
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"name": tool, "arguments": arguments}),
            "http://localhost/agents/root/root/tools/call",
        ],
        text=True,
    )
    response = json.loads(output)
    if response.get("success") is not True:
        raise AssertionError(f"{tool} failed: {json.dumps(response, sort_keys=True)}")
    return response


def run_init(exomonad: str, repo: Path, session: str, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [exomonad, "init", mode, "--session", session],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_preserved(before: dict[str, str], after: dict[str, str]) -> None:
    if before != after:
        raise AssertionError(f"invocation ownership changed: before={before}, after={after}")


def check_corrupt_artifact(
    repo: Path,
    exomonad: str,
    session: str,
    owner_before: dict[str, str],
) -> None:
    agent_dir = repo / ".exo" / "agents" / "corrupt-codex"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "identity.json").write_text(
        json.dumps({"agent_name": "corrupt-codex", "slice_id": "corrupt-codex"}),
        encoding="utf-8",
    )
    (agent_dir / "invocation.json").write_text("{not-json", encoding="utf-8")
    result = run_init(exomonad, repo, session, "--continue")
    continuation = agent_dir / "continuation.json"
    if not continuation.is_file():
        raise AssertionError("corrupt invocation produced no continuation classification")
    if read_json(continuation).get("classification") != "recreate":
        raise AssertionError(f"corrupt invocation was not classified as recreate: {continuation.read_text()}")
    assert_preserved(owner_before, invocation_ids(invocation_records(repo)))
    diagnostic = result.stdout.lower() + result.stderr.lower()
    if result.returncode == 0 and "corrupt" not in diagnostic:
        raise AssertionError("corrupt invocation had no operator-visible diagnostic")


def run(
    repo: Path,
    exomonad: str,
    session: str,
    socket: str,
    mutant: bool,
) -> dict[str, Any]:
    mcp_call(
        socket,
        "spawn_leaf",
        {
            "name": "one-shot",
            "task": "Publish the fixture through file_pr and exit.",
            "agent_type": "codex",
        },
    )
    owner_dir = repo / ".exo" / "agents" / "one-shot-codex"
    wait_for(lambda: (owner_dir / "invocation.json").is_file(), "one-shot invocation")
    wait_for(
        lambda: '"status": "exited"' in (owner_dir / "invocation.json").read_text(),
        "one-shot exit",
    )
    wait_for(lambda: bool(published_heads(repo)), "verified publication")

    before = invocation_ids(invocation_records(repo))
    heads = published_heads(repo)
    if not any(
        head.get("slice_id") == "one-shot"
        and head.get("invocation_id") in before.values()
        for head in heads
    ):
        raise AssertionError(f"publication was not bound to the observed invocation: {heads}")
    publication = next(head for head in heads if head.get("slice_id") == "one-shot")
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests/e2e/init-recovery/seed_checkpoint.py"),
            "--repo",
            str(repo),
            "--pr-number",
            str(publication["pr_number"]),
            "--branch",
            str(publication["head_branch"]),
            "--slice-id",
            "one-shot",
        ],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
        check=True,
    )
    plan_path = repo / ".exo" / "tl-loop" / "plan.json"
    plan_before = plan_path.read_bytes() if plan_path.is_file() else None

    tmux = subprocess.run(
        ["tmux", "kill-session", "-t", session],
        check=False,
        capture_output=True,
    )
    if tmux.returncode not in (0, 1):
        raise AssertionError(f"failed to stop the first session: {tmux.stderr.decode()}")
    restarted = run_init(exomonad, repo, session, "--continue")

    after = invocation_ids(invocation_records(repo))
    assert_preserved(before, after)
    root_dir = repo / ".exo" / "tl-loop"
    archived = list(root_dir.glob("root.invalid-*"))
    if archived:
        raise AssertionError("--continue archived the existing TL run")
    unresolved = [
        event
        for event in ledger_events(repo)
        if event.get("type") == "watcher.ownership_unresolved"
        and any(str(head.get("pr_number")) in json.dumps(event) for head in heads)
    ]
    run_path = root_dir / "root" / "run.json"
    checkpoint = read_json(run_path) if run_path.is_file() else {}
    next_action = checkpoint.get("next_transition") or checkpoint.get("fsm") or checkpoint.get("phase")
    if next_action is None:
        raise AssertionError("continue did not leave an observable next lifecycle action")
    if plan_before is not None and plan_path.read_bytes() != plan_before:
        raise AssertionError("--continue modified plan.json")
    check_corrupt_artifact(repo, exomonad, session, before)
    start = run_init(exomonad, repo, session, "--start")
    if start.returncode == 0 or "refusing --start" not in start.stdout + start.stderr:
        raise AssertionError("--start did not refuse the nonterminal run")
    if mutant:
        mutated = dict(before)
        owner = next(iter(mutated))
        mutated[owner] += "-fresh"
        try:
            assert_preserved(before, mutated)
        except AssertionError:
            pass
        else:
            raise AssertionError("mutant accepted a freshly minted invocation")
    return {
        "invocation_ids_before": before,
        "invocation_ids_after": after,
        "ids_preserved": before == after,
        "run_dir_archived": bool(archived),
        "ownership_unresolved_events": len(unresolved),
        "next_action": next_action,
        "start_refused": True,
        "restart_returncode": restarted.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("exomonad")
    parser.add_argument("session")
    parser.add_argument("socket")
    parser.add_argument("result", type=Path)
    parser.add_argument("--mutant", action="store_true")
    args = parser.parse_args()
    try:
        evidence = run(args.repo, args.exomonad, args.session, args.socket, args.mutant)
    except (AssertionError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    args.result.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
