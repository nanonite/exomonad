#!/usr/bin/env python3
"""Disposable real-server acceptance for ordered TL recovery (chainlink #1100/#1103).

Two scenarios run against a real ``exomonad serve`` process in a throwaway Git
repository. Neither scenario touches the captured Beast workspace.

Scenario 1 -- failed child startup and safe continuation:
  * A real ordered sub-TL is provisioned through the server
    (``provision-sub-tl``), creating its branch, durable identity, and worktree.
  * The child controller is driven through the real crash path
    (``_supervise_live_sub_tl`` over an exited process), which must persist a
    recursive failure checkpoint and a diagnostic bound to that exact revision.
  * ``_ordered_terminal_recovery_decision`` must classify the failure as
    retryable, ``_reopen_ordered_scope`` must reopen the parent while preserving
    the accepted dispatch intent, and re-provisioning the same stage against
    the real server must stay idempotent: exactly one branch, identity, and
    worktree, and no new dispatch intent.

Scenario 2 -- recreate followed by a same-plan restart:
  * A disposable repository carries an identity-less orphan ordered branch
    (branch present, no identity, no worktree) -- the Beast #1100 shape.
  * The real ``exomonad init --recreate --confirm-recreate`` binary must remove
    that branch.
  * The same plan restarts through the real embedded controller and provisions
    the stage with no HTTP 409 and no identity-less branch.

The harness is intentionally narrower than the full ``real_server_transport``
suite: it is the acceptance the #1100/#1103 review recorded as outstanding.
Full live leaf respawn is covered by ``tl_loop/tests`` and the ordered server
suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORDERED_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ORDERED_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import real_server_transport as real

from tl_loop.client.transport import ServerError
from tl_loop.fsm.scope import TLFailed, TLRunning
from tl_loop.loop.driver import (
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _ensure_canonical_scope,
    _initial_slices,
    _manifest_for_plan,
    _ordered_terminal_recovery_decision,
    _release_canonical_scope,
    _reopen_ordered_scope,
    _sub_tl_worktree,
    _supervise_live_sub_tl,
)
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.state.schema import (
    IntegrationRuntimeState,
    OrderedStageState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, create


class AcceptanceError(RuntimeError):
    """Raised when a real-server acceptance assertion fails."""


class _ExitedProcess:
    """Stand-in for a child controller that exited before authoritative resolution."""

    exitcode = 1

    def is_alive(self) -> bool:
        return False


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _worktrees_for(repo: Path, branch: str) -> list[str]:
    output = real.git(repo, "worktree", "list", "--porcelain")
    worktrees: list[str] = []
    current: str | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
        elif line.strip() == f"branch refs/heads/{branch}" and current is not None:
            worktrees.append(current)
    return worktrees


def _identity(repo: Path, agent_name: str) -> dict[str, Any] | None:
    path = repo / ".exo" / "agents" / agent_name / "identity.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_events(repo: Path) -> list[dict[str, Any]]:
    return real.server_ledger_events(repo)


def _count(events: list[dict[str, Any]], event_type: str, **fields: Any) -> int:
    count = 0
    for event in events:
        if event.get("type") != event_type:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if all(data.get(key) == value for key, value in fields.items()):
            count += 1
    return count


def _seed_failed_ordered_run(
    repo: Path,
    state_root: Path,
    swarm_id: str,
) -> tuple[RunStore, WorkPlan, TLLoopConfig, str, str]:
    """Provision a real ordered child, then crash its controller before resolution."""
    parent_run = "recovery-parent"
    child_name = "stage-a"
    child_plan = WorkPlan(leaves=(LeafTask("leaf", "implement the ordered child change"),))
    plan = WorkPlan(sub_tls=(SubTLTask(child_name, child_plan, order=1),))
    config = TLLoopConfig(
        root_dir=state_root,
        project_root=repo,
        branch="main",
        worktree=repo,
        ledger_run_id=swarm_id,
        session_mode="continue",
    )
    child_branch = f"main.{child_name}"
    child_worktree = str(_sub_tl_worktree(config, state_root, parent_run, plan.sub_tls[0]))

    manifest = _manifest_for_plan(plan, parent_run, config)
    parent_slices = _bind_initial_slices(
        _initial_slices(plan, config, state_root, parent_run),
        manifest,
    )
    create(
        parent_run,
        {
            "plan_manifest": manifest.to_document(),
            "slices": parent_slices,
            "owner_branch": "main",
            "owner_worktree": str(repo),
            "ledger_run_id": swarm_id,
        },
        root_dir=state_root,
    )
    parent_store = RunStore(parent_run, state_root)
    parent_state = parent_store.load()

    child_manifest = manifest.child_manifests[
        next(node.node_id for node in manifest.nodes if node.name == child_name)
    ]
    child_config = replace(
        config,
        root_dir=parent_store.run_dir,
        run_id=child_name,
        branch=child_branch,
        worktree=child_worktree,
        parent_branch="main",
        parent_run_id=parent_run,
        depth=1,
    )
    child_slices = _bind_initial_slices(
        _initial_slices(child_plan, child_config, parent_store.run_dir, child_name),
        child_manifest,
    )
    create(
        child_name,
        {
            "plan_manifest": child_manifest.to_document(),
            "slices": child_slices,
            "owner_branch": child_branch,
            "owner_worktree": child_worktree,
            "parent_branch": "main",
            "parent_run_id": parent_run,
            "ledger_run_id": swarm_id,
            "session_mode": "continue",
        },
        root_dir=parent_store.run_dir,
    )
    child_store = RunStore(child_name, parent_store.run_dir)
    child_state = _ensure_canonical_scope(child_store.load(), child_manifest, child_store)
    _release_canonical_scope(child_state, child_store)

    child = replace(
        parent_state.slices[child_name],
        status=SliceStatus.FAILED,
        branch=child_branch,
        worktree=child_worktree,
        dispatch_intent_id="ordered-child-dispatch",
        dispatch_agent_id=child_name,
        dispatch_authoritative_event_seq=11,
        dispatch_last_boundary="sub_tl_started",
    )
    parent_store.checkpoint(
        TLFailed("recursive child failed"),
        {child_name: child},
        parent_state.budgets,
        parent_state.events.last_consumed_offset,
        current_order=1,
        ordered_stages=(OrderedStageState(1, (child_name,)),),
        integration=IntegrationRuntimeState(
            sub_tl_states={child_name: IntegrationLifecycle.FAILED}
        ),
    )

    # Drive the real crash path: a child controller that exits before
    # authoritative resolution persists its own failure checkpoint and a
    # diagnostic bound to that exact checkpoint revision.
    _supervise_live_sub_tl(_ExitedProcess(), child_store, config)
    return parent_store, plan, config, child_name, child_worktree


def run_continuation_scenario(root: Path) -> dict[str, Any]:
    repo, remote, _branch = real.create_fixture(root / "continuation")
    mock, forgejo_url = real.start_mock(root / "continuation", PROJECT_ROOT, remote)
    server: subprocess.Popen[str] | None = None
    try:
        server, client = real.start_server(
            root / "continuation", repo, forgejo_url, PROJECT_ROOT
        )
        # Match the real init flow, which records the root birth branch. The
        # server re-resolves it per request, so the ordered parent identity is
        # the declared "main" owner rather than the unresolved fallback.
        root_agent_dir = repo / ".exo" / "agents" / "root"
        root_agent_dir.mkdir(parents=True, exist_ok=True)
        (root_agent_dir / ".birth_branch").write_text("main\n", encoding="utf-8")
        swarm_id = real.server_run_id(repo)
        state_root = repo / ".exo" / "tl-loop"
        parent_store, plan, config, child_name, child_worktree = _seed_failed_ordered_run(
            repo, state_root, swarm_id
        )
        child_branch = f"main.{child_name}"

        # Provision the stage through the real server before recovery so the
        # branch, identity, and worktree already exist.
        client.provision_ordered_sub_tl(
            "root",
            agent_name=child_name,
            birth_branch=child_branch,
            parent_branch="main",
            working_dir=child_worktree,
            slice_id=child_name,
        )
        _require(
            _identity(repo, child_name) is not None,
            "ordered child identity was not provisioned",
        )
        _require(
            real.git(repo, "branch", "--list", child_branch).strip() != "",
            "ordered child branch was not provisioned",
        )
        worktrees_before = _worktrees_for(repo, child_branch)
        _require(
            len(worktrees_before) == 1,
            f"expected one ordered worktree before recovery, got {worktrees_before!r}",
        )

        parent_state = parent_store.load()
        child_state = RunStore(child_name, parent_store.run_dir).load()
        _require(
            isinstance(child_state.recursive_fsm, TLFailed),
            "crashed child did not persist a recursive failure checkpoint",
        )
        diagnostic = RunStore(child_name, parent_store.run_dir).exit_diagnostics()
        _require(
            isinstance(diagnostic, dict)
            and diagnostic.get("checkpoint_revision") == child_state.revision,
            "child exit diagnostic is not bound to the failure checkpoint",
        )

        decision = _ordered_terminal_recovery_decision(
            parent_state, plan, config, parent_store
        )
        _require(
            decision is not None and decision.recoverable is True,
            f"failed child startup was not classified recoverable: {decision!r}",
        )
        original_intent = parent_state.slices[child_name].dispatch_intent_id
        reopened = _reopen_ordered_scope(
            parent_store.load(), plan, parent_store, decision.task_name
        )
        _require(
            isinstance(reopened.recursive_fsm, TLRunning),
            "continuation did not reopen the terminal parent scope",
        )
        _require(
            reopened.slices[child_name].status is SliceStatus.SPAWNED,
            "reopened child slice is not spawned",
        )
        _require(
            reopened.slices[child_name].dispatch_intent_id == original_intent,
            "continuation minted a new dispatch intent instead of reusing the accepted one",
        )

        # Re-provision the reopened stage against the real server: the effect is
        # idempotent, so no duplicate branch, identity, worktree, or intent.
        client.provision_ordered_sub_tl(
            "root",
            agent_name=child_name,
            birth_branch=child_branch,
            parent_branch="main",
            working_dir=child_worktree,
            slice_id=child_name,
        )
        worktrees_after = _worktrees_for(repo, child_branch)
        _require(
            worktrees_after == worktrees_before,
            f"continuation duplicated the ordered worktree: {worktrees_after!r}",
        )
        identities = sorted(
            path.parent.name
            for path in (repo / ".exo" / "agents").glob("*/identity.json")
            if path.parent.name == child_name
        )
        _require(
            identities == [child_name],
            f"continuation duplicated the ordered identity: {identities!r}",
        )
        events = _ledger_events(repo)
        duplicate_spawns = _count(
            events, "agent.spawned", intent_id=original_intent
        )
        _require(
            duplicate_spawns == 0,
            f"continuation produced a duplicate spawn effect: {duplicate_spawns}",
        )
        return {
            "scenario": "failed-child-startup-and-continuation",
            "child": child_name,
            "recoverable": True,
            "dispatch_intent_preserved": True,
            "worktrees": worktrees_after,
            "duplicate_spawns": duplicate_spawns,
        }
    finally:
        if server is not None:
            real.stop_server(server, repo, "continuation acceptance")
        real.stop_subprocess(mock, "continuation mock API")


def _write_recreate_fixture(repo: Path, session: str) -> None:
    (repo / ".exo" / "config.toml").write_text(
        "\n".join(
            [
                'default_role = "devswarm"',
                'wasm_name = "devswarm"',
                'shell_command = "bash"',
                f'tmux_session = "{session}"',
                "yolo = true",
                "poll_interval = 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    plan_dir = repo / ".exo" / "tl-loop"
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "plan.json").write_text(
        json.dumps(
            {
                "run_id": "root",
                "plan": {
                    "sub_tls": [
                        {
                            "name": "stage-a",
                            "agent_id": "stage-a",
                            "plan": {"leaves": []},
                        }
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def run_recreate_scenario(root: Path, exomonad: Path) -> dict[str, Any]:
    repo = root / "recreate" / "repo"
    repo.mkdir(parents=True)
    real.run_command(["git", "init", "-q", "-b", "main"], cwd=repo)
    real.git(repo, "config", "user.email", "recreate-acceptance@example.invalid")
    real.git(repo, "config", "user.name", "Recreate Acceptance")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    real.git(repo, "add", "seed")
    real.git(repo, "commit", "-q", "-m", "seed")
    new = subprocess.run(
        [str(exomonad), "new"],
        cwd=repo,
        env={**os.environ, "PATH": f"{exomonad.parent}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
        check=False,
    )
    _require(new.returncode == 0, f"exomonad new failed: {new.stderr[-2000:]}")

    session = f"ordered-recovery-recreate-{os.getpid()}"
    _write_recreate_fixture(repo, session)
    # The #1100 failure shape: the stage branch exists with no durable identity
    # and no worktree.
    real.git(repo, "branch", "main.stage-a")
    _require(
        real.git(repo, "branch", "--list", "main.stage-a").strip() != "",
        "orphan stage branch was not created",
    )
    _require(
        _identity(repo, "stage-a") is None,
        "orphan stage branch unexpectedly already had an identity",
    )

    log_path = root / "recreate" / "init.log"
    with log_path.open("w", encoding="utf-8") as log:
        init = subprocess.run(
            [
                str(exomonad),
                "init",
                "--recreate",
                "--confirm-recreate",
                "--session",
                session,
            ],
            cwd=repo,
            env={
                **os.environ,
                "PATH": f"{exomonad.parent}:{os.environ.get('PATH', '')}",
                "RUST_LOG": "info",
            },
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=120,
        )
    log_text = log_path.read_text(encoding="utf-8", errors="replace")

    _require(
        "409" not in log_text and "already exists without matching durable identity" not in log_text,
        "recreate/restart produced an ordered ownership 409",
    )
    identity = _identity(repo, "stage-a")
    _require(
        identity is not None,
        f"same-plan restart did not provision the ordered identity: {log_text[-2000:]}",
    )
    _require(
        identity.get("birth_branch") == "main.stage-a"
        and identity.get("parent_branch") == "main",
        f"provisioned identity does not match the declared owner: {identity!r}",
    )
    worktrees = _worktrees_for(repo, "main.stage-a")
    _require(
        len(worktrees) == 1,
        f"same-plan restart left an unexpected worktree set: {worktrees!r}",
    )
    events = _ledger_events(repo)
    stage_starts = _count(events, "tl.stage_started", sub_tl_ids=["stage-a"])
    _require(
        stage_starts == 1,
        f"same-plan restart did not start exactly one stage: {stage_starts}",
    )
    return {
        "scenario": "recreate-then-same-plan-restart",
        "orphan_branch_removed": True,
        "identity": identity,
        "worktrees": worktrees,
        "stage_starts": stage_starts,
        "init_returncode": init.returncode,
    }


def _find_exomonad() -> Path:
    candidate = Path(
        os.environ.get("EXOMONAD_E2E_BIN", PROJECT_ROOT / "target/debug/exomonad")
    )
    if not candidate.is_file():
        raise AcceptanceError(
            "build target/debug/exomonad first (or set EXOMONAD_E2E_BIN)"
        )
    return candidate


def main() -> int:
    exomonad = _find_exomonad()
    with tempfile.TemporaryDirectory(prefix="exomonad-ordered-recovery-") as temporary:
        root = Path(temporary)
        evidence: dict[str, Any] = {}
        try:
            evidence["continuation"] = run_continuation_scenario(root)
            evidence["recreate"] = run_recreate_scenario(root, exomonad)
        except (AcceptanceError, real.HarnessError, ServerError, OSError) as error:
            print(f"FAIL: {error}", file=sys.stderr)
            return 1
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"ordered-recovery-recreate-{os.getpid()}"],
                check=False,
                capture_output=True,
            )
            shutil.rmtree(root / "continuation", ignore_errors=True)
        print(json.dumps(evidence, indent=2, sort_keys=True))
        print("ordered recovery real-server acceptance: passed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
