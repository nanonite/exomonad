#!/usr/bin/env python3
"""Real-server acceptance harness for slice abandonment and redispatch.

Each run creates a disposable repository, starts the real ExoMonad server,
dispatches the real WASM effects, and drives the operator recovery functions
over the Unix-socket transport.  The three-run loop is deliberately bounded;
every process, tmux session, worktree, and scratch directory is owned by this
module and cleaned up in ``finally`` blocks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORDERED_DIR = PROJECT_ROOT / "tests/e2e/ordered-recursive"
sys.path.insert(0, str(ORDERED_DIR))

import real_server_transport as real

from tl_loop.client.effects import EffectClient
from tl_loop.events.reader import LedgerReader
from tl_loop.loop.abandon import abandon_slice
from tl_loop.loop.driver import (
    LeafTask,
    TLLoopConfig,
    WorkPlan,
    _initial_slices,
)
from tl_loop.loop.redispatch import redispatch_slice
from tl_loop.state.schema import ParkCause, SliceStatus
from tl_loop.state.store import RunStore, create

SLICE = "abandonable-leaf"
AGENT = f"{SLICE}-opencode"
INVOCATION_ID = "e2e-abandon-invocation"
NESTED_SLICE = "nested-abandonable-leaf"
NESTED_AGENT = f"{NESTED_SLICE}-opencode"
NESTED_INVOCATION_ID = "e2e-nested-abandon-invocation"


def checkpoint(
    work: Path, run_id: str = "root", *, state_root: Path | None = None
) -> dict[str, Any]:
    root = state_root or work / ".exo/tl-loop"
    return json.loads((root / run_id / "run.json").read_text())


def slice_of(
    work: Path,
    slice_id: str,
    run_id: str = "root",
    *,
    state_root: Path | None = None,
) -> dict[str, Any]:
    return checkpoint(work, run_id, state_root=state_root)["slices"][slice_id]


def ledger_events(
    work: Path,
    event_type: str,
    run_id: str = "root",
    *,
    state_root: Path | None = None,
    slice_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read committed events through the canonical reader, never a raw glob."""
    reader_root = state_root or work / ".exo/tl-loop"
    effective_run_id = str(
        checkpoint(work, run_id, state_root=reader_root).get("ledger_run_id") or run_id
    )
    reader = LedgerReader(
        work / ".exo/ledger/segments",
        run_id=effective_run_id,
        state_root=reader_root,
        ledger_run_id=effective_run_id,
    )
    events = [
        {"data": event.data, "event_type": event.event_type}
        for event in reader.read_from().events
        if event.event_type == event_type
    ]
    if slice_id is None:
        return events
    return [event for event in events if event["data"].get("slice_id") == slice_id]


def journal_entries(
    work: Path, run_id: str = "root", *, state_root: Path | None = None
) -> list[dict[str, Any]]:
    root = state_root or work / ".exo/tl-loop"
    path = root / run_id / "action-journal.json"
    return json.loads(path.read_text()) if path.exists() else []


def published_heads(work: Path) -> list[dict[str, Any]]:
    payload = json.loads((work / ".exo/published-heads.json").read_text())
    if isinstance(payload, dict):
        payload = payload.get("heads", [])
    if not isinstance(payload, list):
        raise real.HarnessError("published-heads.json did not contain a heads list")
    return [head for head in payload if isinstance(head, dict)]


def published_heads_bytes(work: Path) -> bytes:
    """Return the registry bytes so lifecycle cleanup cannot rewrite evidence."""
    path = work / ".exo/published-heads.json"
    if not path.is_file():
        raise real.HarnessError(f"publication registry is missing: {path}")
    return path.read_bytes()


def prepare_agent(
    repo: Path,
    *,
    slice_id: str = SLICE,
    agent_id: str = AGENT,
    invocation_id: str = INVOCATION_ID,
) -> tuple[Path, str]:
    agent_dir = repo / ".exo/agents" / agent_id
    worktree = repo / ".exo/worktrees" / slice_id
    branch = f"main.{agent_id}"
    real.git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), "main")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ".birth_branch").write_text(branch + "\n", encoding="utf-8")
    (worktree / "abandoned-change.txt").write_text(
        f"published before abandonment for {slice_id}\n"
    )
    real.git(worktree, "add", "abandoned-change.txt")
    real.git(worktree, "commit", "-q", "-m", f"Publish abandoned change for {slice_id}")
    real.git(repo, "push", "-q", "origin", branch)
    (agent_dir / "identity.json").write_text(
        json.dumps(
            {
                "agent_name": agent_id,
                "slug": slice_id,
                "agent_type": "opencode",
                "birth_branch": branch,
                "parent_branch": "main",
                "working_dir": f".exo/worktrees/{slice_id}",
                "display_name": agent_id,
                "topology": "worktree_per_agent",
                "model": None,
                "effort": None,
                "ledger_owned": True,
                "slice_id": slice_id,
            },
            indent=2,
        )
    )
    (agent_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": invocation_id,
                "runtime": "opencode",
                "trigger": "spawn",
                "routing": {"kind": "none"},
                "started_at": 1700000000,
                "ended_at": 1700000060,
                "status": "exited",
                "exit_code": 0,
                "pr_number": None,
                "generation": 1,
                "slice_id": slice_id,
                "runtime_agent_id": agent_id,
                "branch": branch,
                "worktree": str(agent_dir),
            },
            indent=2,
        )
    )
    return agent_dir, branch


def publish_closed_pr(
    repo: Path,
    forgejo: str,
    effects: EffectClient,
    *,
    slice_id: str,
    branch: str,
) -> tuple[int, str, dict[str, Any]]:
    filed = effects.file_pr(
        title=f"Abandonment fixture {slice_id}",
        body="closed unmerged",
        base_branch="main",
    )
    filed_data = real.find_object(filed, {"pr_number", "head_branch"})
    pr_number = int(filed_data["pr_number"])
    branch = str(filed_data["head_branch"])
    head_sha = real.git(repo, "rev-parse", branch)
    real.json_request(
        "POST",
        f"{forgejo}/api/v1/repos/owner/repo/pulls/{pr_number}/merge",
        {},
    )
    real.git(repo, "push", "-q", "origin", "--delete", branch)
    real.run_command(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "remove",
            "--force",
            str(repo / ".exo/worktrees" / slice_id),
        ]
    )
    real.git(repo, "branch", "-D", branch)
    ref_lines = real.git(
        repo, "for-each-ref", "--format=%(refname) %(objectname)"
    ).splitlines()
    for line in ref_lines:
        ref_name, _, object_name = line.partition(" ")
        if object_name == head_sha:
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", "-d", ref_name],
                check=False,
                capture_output=True,
            )
    for ref in (f"refs/pull/{pr_number}/head", f"refs/pull/{pr_number}/merge"):
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "-d", ref],
            check=False,
            capture_output=True,
        )
        remote_url = real.git(repo, "remote", "get-url", "origin")
        remote_path = Path(remote_url.removeprefix("file://"))
        subprocess.run(
            ["git", "-C", str(remote_path), "update-ref", "-d", ref],
            check=False,
            capture_output=True,
        )
    real.git(repo, "worktree", "prune")
    real.git(repo, "reflog", "expire", "--expire=now", "--all")
    real.git(repo, "gc", "--prune=now", "--quiet")
    watcher = effects.watcher_pr_state(pr_number=pr_number)
    watcher_data = real.find_object(
        watcher,
        {"found", "pr_state", "merged", "head_reachable"},
    )
    if (
        watcher_data["found"] is not True
        or watcher_data["pr_state"] != "closed"
        or watcher_data["merged"] is not False
        or watcher_data["head_reachable"] is not False
    ):
        raise real.HarnessError(
            f"closed/deleted PR was not observed authoritatively: {watcher_data!r}"
        )
    if "publication registry" in repr(watcher.raw).lower():
        raise real.HarnessError(
            f"watcher_pr_state leaked a registry-domain error: {watcher.raw!r}"
        )
    return pr_number, head_sha, watcher_data


def seed_live_slice(
    repo: Path,
    pr_number: int,
    head_sha: str,
    branch: str,
    run_id: str,
    *,
    state_root: Path | None = None,
    slice_id: str = SLICE,
    agent_id: str = AGENT,
    invocation_id: str = INVOCATION_ID,
    parent_run_id: str | None = None,
) -> None:
    state_root = state_root or repo / ".exo/tl-loop"
    plan = WorkPlan(
        leaves=(LeafTask(slice_id, "abandon then redispatch", agent_type=None),)
    )
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=repo,
        run_id=run_id,
        ledger_run_id=real.server_run_id(repo),
        branch="main",
        worktree=repo,
    )
    initial = _initial_slices(plan, config, state_root, run_id)
    create(
        run_id,
        {
            "owner_branch": "main",
            "owner_worktree": str(
                repo
                if parent_run_id is None
                else repo / ".exo/worktrees" / f"{slice_id}-controller"
            ),
            "parent_run_id": parent_run_id,
            "depth": 1 if parent_run_id else 0,
            "ledger_run_id": real.server_run_id(repo),
            "slices": initial,
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        },
        root_dir=state_root,
    )
    store = RunStore(run_id, state_root)
    state = store.load()
    current = state.slices[slice_id]
    live = replace(
        current,
        status=SliceStatus.IN_REVIEW,
        branch=branch,
        worktree=str(repo / ".exo/worktrees" / slice_id),
        pr_number=pr_number,
        reviewed_head=head_sha,
        attempts=1,
        dispatch_intent_id="seeded-abandonment",
        dispatch_started_at=time.time(),
        dispatch_last_boundary="confirmed",
        dispatch_agent_id=agent_id,
        dispatch_invocation_id=invocation_id,
    )
    store.checkpoint(state.fsm, {**state.slices, slice_id: live}, state.budgets, 0)


def assert_abandonment_contract(
    repo: Path,
    pr_number: int,
    branch: str,
    run_id: str,
    *,
    state_root: Path | None = None,
    slice_id: str = SLICE,
    agent_id: str = AGENT,
) -> None:
    state_root = state_root or repo / ".exo/tl-loop"
    registry_before = published_heads_bytes(repo)
    before = slice_of(repo, slice_id, run_id, state_root=state_root)["attempts"]
    effects = EffectClient(
        real.TransportClient(project_root=repo, timeout=5), role="tl", name="root"
    )
    first = abandon_slice(repo, run_id, slice_id, effects=effects)
    if first.get("status") != "abandoned":
        raise real.HarnessError(f"first abandon did not park the attempt: {first!r}")
    events = ledger_events(
        repo,
        "tl.slice_abandoned",
        run_id,
        state_root=state_root,
        slice_id=slice_id,
    )
    if len(events) != 1:
        raw_events = real.server_ledger_events(repo)
        server_log = (repo.parent / "server.log").read_text(encoding="utf-8")
        raise real.HarnessError(
            f"expected one abandonment event, got {events!r}; raw={raw_events!r}; "
            f"server_log_tail={server_log[-4000:]}"
        )
    data = events[0]["data"]
    required = {
        "slice_id",
        "attempt",
        "pr_number",
        "head_sha",
        "invocation_id",
        "operator_source",
        "cause",
    }
    if (
        not required <= data.keys()
        or data["slice_id"] != slice_id
        or data["attempt"] != 1
    ):
        raise real.HarnessError(f"abandonment payload lost contract fields: {data!r}")
    if data["pr_number"] != pr_number or not data["operator_source"]:
        raise real.HarnessError(
            f"abandonment payload has wrong PR/operator data: {data!r}"
        )
    log = (repo.parent / "server.log").read_text(encoding="utf-8")
    if "InvalidInput" in log and "tl.slice_abandoned" in log:
        raise real.HarnessError("tl.slice_abandoned was rejected by the event contract")
    current = slice_of(repo, slice_id, run_id, state_root=state_root)
    if current["park_cause"] != ParkCause.ATTEMPT_ABANDONED.value:
        raise real.HarnessError(f"abandonment did not park the slice: {current!r}")
    if current["attempts"] != before:
        raise real.HarnessError("abandonment charged an unexpected attempt")
    assert_disposal(repo, branch, pr_number, slice_id=slice_id, agent_id=agent_id)
    if published_heads_bytes(repo) != registry_before:
        raise real.HarnessError(
            "abandonment rewrote published-heads.json instead of preserving evidence"
        )
    second = abandon_slice(repo, run_id, slice_id, effects=effects)
    if second.get("status") != "already_abandoned":
        raise real.HarnessError(f"second abandon was not idempotent: {second!r}")
    if (
        len(
            ledger_events(
                repo,
                "tl.slice_abandoned",
                run_id,
                state_root=state_root,
                slice_id=slice_id,
            )
        )
        != 1
    ):
        raise real.HarnessError("second abandon emitted a duplicate event")
    if slice_of(repo, slice_id, run_id, state_root=state_root)["attempts"] != before:
        raise real.HarnessError("second abandon charged another attempt")
    cleanup_keys = [
        entry.get("key")
        for entry in journal_entries(repo, run_id, state_root=state_root)
        if entry.get("operation") == "cleanup"
    ]
    if len(cleanup_keys) != len(set(cleanup_keys)) or len(cleanup_keys) != 1:
        raise real.HarnessError(
            f"cleanup journal was not deduplicated: {cleanup_keys!r}"
        )
    if published_heads_bytes(repo) != registry_before:
        raise real.HarnessError(
            "repeated abandonment changed published-heads.json evidence"
        )


def assert_disposal(
    repo: Path,
    branch: str,
    pr_number: int,
    *,
    slice_id: str = SLICE,
    agent_id: str = AGENT,
) -> None:
    agent_dir = repo / ".exo/agents" / agent_id
    real.git(repo, "worktree", "prune")
    worktree_lines = real.git(repo, "worktree", "list", "--porcelain").splitlines()
    active_worktree = any(
        line.startswith("worktree ")
        and Path(line.removeprefix("worktree ")).name == slice_id
        for line in worktree_lines
    )
    if agent_dir.exists() or active_worktree:
        raise real.HarnessError(
            "abandoned worktree or agent directory survived cleanup: "
            f"agent_exists={agent_dir.exists()} worktrees={real.git(repo, 'worktree', 'list')}"
        )
    if real.git(repo, "branch", "--list", branch).strip():
        raise real.HarnessError("abandoned branch survived cleanup")
    if not any(
        head.get("slice_id") == slice_id and head.get("pr_number") == pr_number
        for head in published_heads(repo)
    ):
        raise real.HarnessError("publication evidence was removed with the worktree")
    if branch in real.git(repo, "branch", "--all"):
        raise real.HarnessError("closed PR branch remained in local refs")


def assert_redispatch_contract(
    repo: Path,
    pr_number: int,
    abandoned_branch: str,
    run_id: str,
    *,
    state_root: Path | None = None,
    slice_id: str = SLICE,
    agent_id: str = AGENT,
) -> None:
    state_root = state_root or repo / ".exo/tl-loop"
    registry_before = published_heads_bytes(repo)
    effects = EffectClient(
        real.TransportClient(project_root=repo, timeout=5), role="tl", name="root"
    )
    plan = WorkPlan(
        leaves=(LeafTask(slice_id, "fresh attempt from the original specification"),)
    )
    config = TLLoopConfig(
        active=True,
        root_dir=state_root,
        project_root=repo,
        run_id=run_id,
        ledger_run_id=real.server_run_id(repo),
        branch="main",
        worktree=repo,
        effects=effects,
    )
    result = redispatch_slice(
        repo, run_id, slice_id, plan, effects=effects, config=config
    )
    if result.get("status") != "dispatched":
        raise real.HarnessError(f"redispatch did not start a fresh attempt: {result!r}")
    fresh = slice_of(repo, slice_id, run_id, state_root=state_root)
    if fresh["status"] not in {
        "dispatching",
        "dispatch_unconfirmed",
        "spawned",
        "pending",
    }:
        raise real.HarnessError(f"fresh attempt has unexpected status: {fresh!r}")
    for field in ("pr_number", "reviewed_head", "verdict", "branch", "worktree"):
        if field in {"branch", "worktree"}:
            continue
        if fresh[field] is not None:
            raise real.HarnessError(f"fresh attempt inherited {field}: {fresh!r}")
    if fresh["park_cause"] not in {None, "dispatch_unconfirmed"}:
        raise real.HarnessError(f"fresh attempt inherited park_cause: {fresh!r}")
    runtime_name = result.get("runtime_name")
    if fresh["id"] != slice_id or runtime_name == agent_id:
        raise real.HarnessError(
            f"fresh attempt did not receive a new runtime identity: {fresh!r}"
        )
    if abandoned_branch in real.git(repo, "log", "--all", "--oneline"):
        raise real.HarnessError("fresh attempt retained the abandoned PR head")
    new_agent = fresh.get("dispatch_agent_id") or runtime_name
    if isinstance(new_agent, str) and new_agent:
        effects.cleanup(issue=new_agent, force=False, subrepo="")
    if pr_number == fresh.get("pr_number"):
        raise real.HarnessError("fresh attempt retained the closed PR number")
    if published_heads_bytes(repo) != registry_before:
        raise real.HarnessError(
            "redispatch rewrote published-heads.json before a new publication"
        )


def run_recovery_sequence(
    repo: Path,
    forgejo: str,
    *,
    run_id: str,
    branch: str,
    state_root: Path | None = None,
    slice_id: str = SLICE,
    agent_id: str = AGENT,
    invocation_id: str = INVOCATION_ID,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    effects = EffectClient(
        real.TransportClient(project_root=repo, timeout=5),
        role="tl",
        name=agent_id,
    )
    pr_number, head_sha, watcher_data = publish_closed_pr(
        repo,
        forgejo,
        effects,
        slice_id=slice_id,
        branch=branch,
    )
    seed_live_slice(
        repo,
        pr_number,
        head_sha,
        branch,
        run_id,
        state_root=state_root,
        slice_id=slice_id,
        agent_id=agent_id,
        invocation_id=invocation_id,
        parent_run_id=parent_run_id,
    )
    seeded = slice_of(repo, slice_id, run_id, state_root=state_root)
    assert_abandonment_contract(
        repo,
        pr_number,
        branch,
        run_id,
        state_root=state_root,
        slice_id=slice_id,
        agent_id=agent_id,
    )
    abandoned = slice_of(repo, slice_id, run_id, state_root=state_root)
    assert_redispatch_contract(
        repo,
        pr_number,
        branch,
        run_id,
        state_root=state_root,
        slice_id=slice_id,
        agent_id=agent_id,
    )
    redispatched = slice_of(repo, slice_id, run_id, state_root=state_root)
    return {
        "run_id": run_id,
        "slice_id": slice_id,
        "pr_number": pr_number,
        "ledger_cursor": len(
            ledger_events(
                repo,
                "tl.slice_abandoned",
                run_id,
                state_root=state_root,
                slice_id=slice_id,
            )
        ),
        "watcher": {
            "pr_state": watcher_data["pr_state"],
            "head_reachable": watcher_data["head_reachable"],
            "merged": watcher_data["merged"],
        },
        "state_trace": [
            {
                "point": "seeded_live_attempt",
                "status": seeded["status"],
                "park_cause": seeded.get("park_cause"),
            },
            {
                "point": "operator_abandoned",
                "status": abandoned["status"],
                "park_cause": abandoned.get("park_cause"),
            },
            {
                "point": "fresh_redispatch",
                "status": redispatched["status"],
                "park_cause": redispatched.get("park_cause"),
            },
        ],
    }


def run_case(index: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"exomonad-abandon-{index}-") as raw_root:
        root = Path(raw_root)
        repo, remote, _ = real.create_fixture(root)
        agent_dir, branch = prepare_agent(repo)
        nested_agent_dir, nested_branch = prepare_agent(
            repo,
            slice_id=NESTED_SLICE,
            agent_id=NESTED_AGENT,
            invocation_id=NESTED_INVOCATION_ID,
        )
        mock, forgejo = real.start_mock(root, PROJECT_ROOT, remote)
        server = None
        try:
            server, _ = real.start_server(root, repo, forgejo, PROJECT_ROOT)
            swarm_id = real.server_run_id(repo)
            root_evidence = run_recovery_sequence(
                repo,
                forgejo,
                run_id="root",
                branch=branch,
            )
            nested_evidence = run_recovery_sequence(
                repo,
                forgejo,
                run_id="nested-sub-tl",
                branch=nested_branch,
                slice_id=NESTED_SLICE,
                agent_id=NESTED_AGENT,
                invocation_id=NESTED_INVOCATION_ID,
                parent_run_id="root",
            )
            return {
                "run": index,
                "phase": "root_and_nested_closed_unmerged_recovery",
                "swarm_id": swarm_id,
                "cleanup": True,
                "negative_controls": ["NC1", "NC2", "NC3", "NC4", "NC5"],
                "root": root_evidence,
                "nested": nested_evidence,
            }
        finally:
            if server is not None:
                real.stop_subprocess(server, "abandonment E2E server")
            subprocess.run(
                [
                    "tmux",
                    "kill-session",
                    "-t",
                    f"ordered-server-e2e-{__import__('os').getpid()}",
                ],
                check=False,
            )
            real.stop_subprocess(mock, "abandonment E2E mock")
            for path in (agent_dir, nested_agent_dir):
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)


def assert_tool_surface_negative_control() -> None:
    """The #940 mutation must fail the source-derived tool-surface check."""
    with tempfile.TemporaryDirectory(prefix="exomonad-tool-mutation-") as raw_root:
        root = Path(raw_root)
        for relative in (
            "haskell/wasm-guest/src/ExoMonad/Guest/Tools",
            ".exo/roles/devswarm",
        ):
            shutil.copytree(PROJECT_ROOT / relative, root / relative)
        for relative in (
            "tl_loop/client/effects.py",
            "tl_loop/client/readonly.py",
            "scripts/check_tool_surface.py",
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PROJECT_ROOT / relative, target)
        role = root / ".exo/roles/devswarm/TLRole.hs"
        source = role.read_text()
        mutated = source.replace(
            "            resolveLivePrForSlice = mkHandler @ResolveLivePrForSlice,\n",
            "",
        )
        if mutated == source:
            raise real.HarnessError(
                "NC1 mutation no longer matches the role registration"
            )
        role.write_text(mutated)
        probe = subprocess.run(
            [
                sys.executable,
                str(root / "scripts/check_tool_surface.py"),
                "--project-root",
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            raise real.HarnessError(
                "NC1 unregistered-tool mutation unexpectedly passed"
            )


def main() -> None:
    assert_tool_surface_negative_control()
    evidence = [run_case(index) for index in range(1, 4)]
    print(
        json.dumps(
            {
                "runs": evidence,
                "negative_controls": ["NC1", "NC2", "NC3", "NC4", "NC5"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
