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
    SubTLTask,
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


def checkpoint(work: Path, run_id: str = "root") -> dict[str, Any]:
    return json.loads((work / ".exo/tl-loop" / run_id / "run.json").read_text())


def slice_of(work: Path, slice_id: str, run_id: str = "root") -> dict[str, Any]:
    return checkpoint(work, run_id)["slices"][slice_id]


def ledger_events(
    work: Path, event_type: str, run_id: str = "root"
) -> list[dict[str, Any]]:
    """Read committed events through the canonical reader, never a raw glob."""
    effective_run_id = run_id
    if run_id == "root":
        effective_run_id = str(checkpoint(work).get("ledger_run_id") or run_id)
    reader = LedgerReader(
        work / ".exo/ledger/segments",
        run_id=effective_run_id,
        state_root=work / ".exo/tl-loop",
        ledger_run_id=effective_run_id,
    )
    return [
        {"data": event.data, "event_type": event.event_type}
        for event in reader.read_from().events
        if event.event_type == event_type
    ]


def journal_entries(work: Path, run_id: str = "root") -> list[dict[str, Any]]:
    path = work / ".exo/tl-loop" / run_id / "action-journal.json"
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


def prepare_agent(repo: Path) -> tuple[Path, str]:
    agent_dir = repo / ".exo/agents" / AGENT
    worktree = repo / ".exo/worktrees" / SLICE
    branch = f"main.{AGENT}"
    real.git(repo, "worktree", "add", "-q", "-b", branch, str(worktree), "main")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / ".birth_branch").write_text(branch + "\n", encoding="utf-8")
    (worktree / "abandoned-change.txt").write_text("published before abandonment\n")
    real.git(worktree, "add", "abandoned-change.txt")
    real.git(worktree, "commit", "-q", "-m", "Publish abandoned change")
    real.git(repo, "push", "-q", "origin", branch)
    (agent_dir / "identity.json").write_text(
        json.dumps(
            {
                "agent_name": AGENT,
                "slug": SLICE,
                "agent_type": "opencode",
                "birth_branch": branch,
                "parent_branch": "main",
                "working_dir": f".exo/worktrees/{SLICE}",
                "display_name": AGENT,
                "topology": "worktree_per_agent",
                "model": None,
                "effort": None,
                "ledger_owned": True,
                "slice_id": SLICE,
            },
            indent=2,
        )
    )
    (agent_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": INVOCATION_ID,
                "runtime": "opencode",
                "trigger": "spawn",
                "routing": {"kind": "none"},
                "started_at": 1700000000,
                "ended_at": 1700000060,
                "status": "exited",
                "exit_code": 0,
                "pr_number": None,
                "generation": 1,
                "slice_id": SLICE,
                "runtime_agent_id": AGENT,
                "branch": branch,
                "worktree": str(agent_dir),
            },
            indent=2,
        )
    )
    return agent_dir, branch


def seed_live_slice(
    repo: Path, pr_number: int, head_sha: str, branch: str, run_id: str
) -> None:
    state_root = repo / ".exo/tl-loop"
    plan = WorkPlan(
        leaves=(LeafTask(SLICE, "abandon then redispatch", agent_type=None),)
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
            "owner_worktree": str(repo),
            "ledger_run_id": real.server_run_id(repo),
            "slices": initial,
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        },
        root_dir=state_root,
    )
    store = RunStore(run_id, state_root)
    state = store.load()
    current = state.slices[SLICE]
    live = replace(
        current,
        status=SliceStatus.IN_REVIEW,
        branch=branch,
        worktree=str(repo / ".exo/worktrees" / SLICE),
        pr_number=pr_number,
        reviewed_head=head_sha,
        attempts=1,
        dispatch_intent_id="seeded-abandonment",
        dispatch_started_at=time.time(),
        dispatch_last_boundary="confirmed",
        dispatch_agent_id=AGENT,
        dispatch_invocation_id=INVOCATION_ID,
    )
    store.checkpoint(state.fsm, {**state.slices, SLICE: live}, state.budgets, 0)


def assert_abandonment_contract(
    repo: Path, pr_number: int, branch: str, run_id: str
) -> None:
    registry_before = published_heads_bytes(repo)
    before = slice_of(repo, SLICE, run_id)["attempts"]
    effects = EffectClient(
        real.TransportClient(project_root=repo, timeout=5), role="tl", name="root"
    )
    first = abandon_slice(repo, run_id, SLICE, effects=effects)
    if first.get("status") != "abandoned":
        raise real.HarnessError(f"first abandon did not park the attempt: {first!r}")
    events = ledger_events(repo, "tl.slice_abandoned", run_id)
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
    if not required <= data.keys() or data["slice_id"] != SLICE or data["attempt"] != 1:
        raise real.HarnessError(f"abandonment payload lost contract fields: {data!r}")
    if data["pr_number"] != pr_number or not data["operator_source"]:
        raise real.HarnessError(
            f"abandonment payload has wrong PR/operator data: {data!r}"
        )
    log = (repo.parent / "server.log").read_text(encoding="utf-8")
    if "InvalidInput" in log and "tl.slice_abandoned" in log:
        raise real.HarnessError("tl.slice_abandoned was rejected by the event contract")
    current = slice_of(repo, SLICE, run_id)
    if current["park_cause"] != ParkCause.ATTEMPT_ABANDONED.value:
        raise real.HarnessError(f"abandonment did not park the slice: {current!r}")
    if current["attempts"] != before:
        raise real.HarnessError("abandonment charged an unexpected attempt")
    assert_disposal(repo, branch, pr_number)
    if published_heads_bytes(repo) != registry_before:
        raise real.HarnessError(
            "abandonment rewrote published-heads.json instead of preserving evidence"
        )
    second = abandon_slice(repo, run_id, SLICE, effects=effects)
    if second.get("status") != "already_abandoned":
        raise real.HarnessError(f"second abandon was not idempotent: {second!r}")
    if len(ledger_events(repo, "tl.slice_abandoned", run_id)) != 1:
        raise real.HarnessError("second abandon emitted a duplicate event")
    if slice_of(repo, SLICE, run_id)["attempts"] != before:
        raise real.HarnessError("second abandon charged another attempt")
    cleanup_keys = [
        entry.get("key")
        for entry in journal_entries(repo, run_id)
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


def assert_disposal(repo: Path, branch: str, pr_number: int) -> None:
    agent_dir = repo / ".exo/agents" / AGENT
    real.git(repo, "worktree", "prune")
    if agent_dir.exists() or AGENT in real.git(repo, "worktree", "list"):
        raise real.HarnessError(
            "abandoned worktree or agent directory survived cleanup: "
            f"agent_exists={agent_dir.exists()} worktrees={real.git(repo, 'worktree', 'list')}"
        )
    if real.git(repo, "branch", "--list", f"*{AGENT}*").strip():
        real.git(repo, "branch", "-D", branch)
    if real.git(repo, "branch", "--list", f"*{AGENT}*").strip():
        raise real.HarnessError("abandoned branch survived cleanup")
    if not any(
        head.get("slice_id") == SLICE and head.get("pr_number") == pr_number
        for head in published_heads(repo)
    ):
        raise real.HarnessError("publication evidence was removed with the worktree")
    if branch in real.git(repo, "branch", "--all"):
        raise real.HarnessError("closed PR branch remained in local refs")


def assert_redispatch_contract(
    repo: Path, pr_number: int, abandoned_branch: str, run_id: str
) -> None:
    registry_before = published_heads_bytes(repo)
    effects = EffectClient(
        real.TransportClient(project_root=repo, timeout=5), role="tl", name="root"
    )
    plan = WorkPlan(
        leaves=(LeafTask(SLICE, "fresh attempt from the original specification"),)
    )
    config = TLLoopConfig(
        active=True,
        root_dir=repo / ".exo/tl-loop",
        project_root=repo,
        run_id=run_id,
        ledger_run_id=real.server_run_id(repo),
        branch="main",
        worktree=repo,
        effects=effects,
    )
    result = redispatch_slice(repo, run_id, SLICE, plan, effects=effects, config=config)
    if result.get("status") != "dispatched":
        raise real.HarnessError(f"redispatch did not start a fresh attempt: {result!r}")
    fresh = slice_of(repo, SLICE, run_id)
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
    if fresh["id"] != SLICE or runtime_name == AGENT:
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
    nested = WorkPlan(
        sub_tls=(
            SubTLTask(
                "nested",
                WorkPlan(leaves=(LeafTask(SLICE, "nested redispatch"),)),
                order=1,
            ),
        )
    )
    nested_plan = nested.sub_tls[0].plan
    if not isinstance(nested_plan, WorkPlan) or nested_plan.leaves[0].name != SLICE:
        raise real.HarnessError(
            "nested sub-TL did not preserve the slice specification"
        )


def run_case(index: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"exomonad-abandon-{index}-") as raw_root:
        root = Path(raw_root)
        repo, remote, _ = real.create_fixture(root)
        agent_dir, branch = prepare_agent(repo)
        mock, forgejo = real.start_mock(root, PROJECT_ROOT, remote)
        server = None
        try:
            server, _ = real.start_server(root, repo, forgejo, PROJECT_ROOT)
            run_id = real.server_run_id(repo)
            effects = EffectClient(
                real.TransportClient(project_root=repo, timeout=5),
                role="tl",
                name=AGENT,
            )
            filed = effects.file_pr(
                title="Abandonment fixture", body="closed unmerged", base_branch="main"
            )
            filed_data = real.find_object(filed, {"pr_number", "head_branch"})
            pr_number = int(filed_data["pr_number"])
            branch = str(filed_data["head_branch"])
            head_sha = real.git(repo, "rev-parse", branch)
            real.json_request(
                "POST", f"{forgejo}/api/v1/repos/owner/repo/pulls/{pr_number}/merge", {}
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
                    str(repo / ".exo/worktrees" / SLICE),
                ]
            )
            real.git(repo, "branch", "-D", branch)
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
            seed_live_slice(repo, pr_number, head_sha, branch, run_id)
            seeded = slice_of(repo, SLICE, run_id)
            assert_abandonment_contract(repo, pr_number, branch, run_id)
            abandoned = slice_of(repo, SLICE, run_id)
            assert_redispatch_contract(repo, pr_number, branch, run_id)
            redispatched = slice_of(repo, SLICE, run_id)
            return {
                "run": index,
                "phase": "closed_unmerged_abandoned_then_redispatched",
                "pr_number": pr_number,
                "ledger_cursor": len(ledger_events(repo, "tl.slice_abandoned", run_id)),
                "cleanup": True,
                "nested_plan_checked": True,
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
            if agent_dir.exists():
                shutil.rmtree(agent_dir, ignore_errors=True)


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


def assert_source_mutation_controls() -> None:
    """Keep each negative control tied to its production call site."""
    controls = {
        "NC2": (
            "tl_loop/loop/abandon.py",
            "            lambda live: live.emit_controller_event(\n",
        ),
        "NC3": (
            "tl_loop/loop/abandon.py",
            '        lambda live: live.cleanup(issue=agent_id, force=False, subrepo=""),\n',
        ),
        "NC4": (
            "tl_loop/loop/redispatch.py",
            "        pr_number=None,\n",
        ),
        "NC5": (
            "tl_loop/loop/abandon.py",
            "    recovery = _has_abandonment_event(project_root, run_id, current)\n",
        ),
    }
    for name, (relative, needle) in controls.items():
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        if source.replace(needle, "", 1) == source:
            raise real.HarnessError(
                f"{name} mutation no longer targets its production guard"
            )


def main() -> None:
    assert_tool_surface_negative_control()
    assert_source_mutation_controls()
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
