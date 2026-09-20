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
  * The shipped ``run_tl_loop`` continuation path runs in its own process
    group with ``session_mode="continue"``. It must reopen the terminal parent,
    relaunch the ordered child controller, and let that child reopen its own
    scope and dispatch its leaf exactly once. A second continuation must
    reconcile the accepted leaf dispatch without minting another one.

Scenario 2 -- recreate followed by a same-plan restart:
  * A disposable repository carries an identity-less orphan ordered branch
    (branch present, no identity, no worktree) -- the Beast #1100 shape.
  * The real ``exomonad init --recreate --confirm-recreate`` binary must remove
    that branch.
  * The same plan restarts through the real embedded controller and provisions
    the stage with no HTTP 409 and no identity-less branch.
  * The only accepted init failure is the documented non-TTY tmux attach error;
    any other exit code or failure marker fails the acceptance.

The harness is intentionally narrower than the full ``real_server_transport``
suite: it is the acceptance the #1100/#1103 review recorded as outstanding.
Full live leaf respawn is covered by ``tl_loop/tests`` and the ordered server
suite.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORDERED_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ORDERED_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import real_server_transport as real

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import ServerError
from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import LedgerReader
from tl_loop.fsm.scope import TLFailed
from tl_loop.loop.driver import (
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _bind_initial_slices,
    _ensure_canonical_scope,
    _initial_slices,
    _manifest_for_plan,
    _release_canonical_scope,
    _sub_tl_worktree,
    _supervise_live_sub_tl,
    run_tl_loop,
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


def _kill_tmux_session(repo: Path) -> None:
    """Tear down the server session created by the fixture, if any."""
    marker = repo / ".exo" / "e2e-tmux-session"
    if not marker.is_file():
        return
    # start_server writes the session name with a literal "\\n" suffix.
    session = marker.read_text(encoding="utf-8").strip().removesuffix("\\n").strip()
    if not session:
        return
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        check=False,
        capture_output=True,
    )


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


PARENT_RUN = "recovery-parent"
CHILD_NAME = "stage-a"
LEAF_NAME = "leaf"


def _controller_config(
    state_root: Path, repo: Path, swarm_id: str, parent_run: str
) -> TLLoopConfig:
    return TLLoopConfig(
        active=True,
        keep_alive_on_waiting=False,
        max_parallel_slices=2,
        max_events=8,
        poll_interval=0.1,
        root_dir=state_root,
        run_id=parent_run,
        ledger_run_id=swarm_id,
        branch="main",
        worktree=repo,
        project_root=repo,
        session_mode="continue",
    )


def _start_production_continuation(
    parent_run: str,
    plan: WorkPlan,
    state_root: Path,
    repo: Path,
    swarm_id: str,
    *,
    use_ledger: bool,
) -> multiprocessing.Process:
    """Run the shipped ``run_tl_loop`` continuation in an isolated process group.

    ``use_ledger=False`` uses an empty source, which stops the child after it
    requests the leaf spawn but before it consumes the authoritative
    ``agent.spawned`` event -- the durable ``dispatch_unconfirmed`` state a real
    crash-before-correlation leaves behind. ``use_ledger=True`` reads the real
    ledger so the restart reconciles that persisted dispatch.
    """
    context = multiprocessing.get_context("fork")

    def entry() -> None:
        os.setsid()
        if use_ledger:
            reader = LedgerReader(
                repo / ".exo" / "ledger" / "segments",
                run_id=parent_run,
                state_root=state_root,
                ledger_run_id=swarm_id,
            )
            source: Any = LedgerQueue(
                reader, poll_interval=0.01, active_tail_timeout=5
            ).start()
        else:
            source = real.EmptyEventSource()
        effects = EffectClient(
            real.TransportClient(project_root=repo, timeout=5),
            role="tl",
            name="root",
        )
        run_tl_loop(
            parent_run,
            plan,
            source,
            effects,
            config=_controller_config(state_root, repo, swarm_id, parent_run),
            root_dir=state_root,
        )

    process = context.Process(target=entry, name="ordered-recovery-controller")
    process.start()
    return process


def _wait_for_leaf_dispatch_request(
    child_store: RunStore, deadline: float
) -> tuple[Any, Any]:
    """Wait for the child to durably request the leaf spawn (unconfirmed).

    This is the crash-before-correlation boundary: the spawn request was
    accepted by the server, but the controller has not yet consumed the
    authoritative ``agent.spawned`` event, so the slice is
    ``dispatch_unconfirmed``.
    """
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = child_store.load()
        except (OSError, ValueError):
            time.sleep(0.1)
            continue
        leaf = last.slices.get(LEAF_NAME)
        if (
            leaf is not None
            and leaf.status is SliceStatus.DISPATCH_UNCONFIRMED
            and leaf.dispatch_intent_id
            and leaf.dispatch_last_boundary == "spawn_request_accepted"
        ):
            return last, leaf
        time.sleep(0.1)
    detail = f": {last!r}" if last is not None else ""
    raise AcceptanceError(
        "production continuation did not durably request the leaf spawn" + detail
    )


def _leaf_spawn_events(repo: Path, intent_id: str) -> list[dict[str, Any]]:
    """Return only the authoritative spawn events for one leaf intent.

    The server records a second, non-authoritative ``agent.spawned`` event for
    the same intent from the WASM log path (no ``spawn_type`` or ``branch``).
    The shipped ordered probes treat the ``leaf_subtree`` event with a branch as
    the single authoritative spawn, so the exactly-once assertion must too.
    """
    return [
        event
        for event in _ledger_events(repo)
        if event.get("type") == "agent.spawned"
        and isinstance(event.get("data"), dict)
        and event["data"].get("intent_id") == intent_id
        and event["data"].get("spawn_type") == "leaf_subtree"
        and event["data"].get("branch")
    ]


def _child_leaf_spawns(repo: Path, child_name: str) -> list[dict[str, Any]]:
    """Return every authoritative leaf spawn requested by one ordered child.

    Counting by the requesting controller rather than by a single intent means a
    second continuation that minted a fresh intent and spawned another leaf
    cannot hide behind the first intent's count.
    """
    return [
        event
        for event in _ledger_events(repo)
        if event.get("type") == "agent.spawned"
        and isinstance(event.get("data"), dict)
        and event["data"].get("spawn_type") == "leaf_subtree"
        and event["data"].get("branch")
        and event.get("agent_id") == child_name
    ]


def _leaf_dispatch_requests(repo: Path) -> list[dict[str, Any]]:
    """Return every controller dispatch request for the leaf slice.

    A repeated continuation that re-dispatched the leaf would add one of these
    even if the authoritative spawn count somehow stayed at one.
    """
    return [
        event
        for event in _ledger_events(repo)
        if event.get("type") in {"tl.spawn_requested", "tl.dispatch_intended"}
        and isinstance(event.get("data"), dict)
        and event["data"].get("slice_id") == LEAF_NAME
    ]


def _reconciliation_events(repo: Path, intent_id: str) -> list[dict[str, Any]]:
    """Return durable owner-found reconciliation events for one leaf intent."""
    return [
        event
        for event in _ledger_events(repo)
        if event.get("type") == "tl.dispatch_reconciliation_completed"
        and isinstance(event.get("data"), dict)
        and event["data"].get("slice_id") == LEAF_NAME
        and event["data"].get("intent_id") == intent_id
        and event["data"].get("boundary") == "owner_found"
    ]


def _wait_for_leaf_reconciliation(
    child_store: RunStore,
    repo: Path,
    intent_id: str,
    process: multiprocessing.Process,
    deadline: float,
) -> tuple[Any, Any, bool]:
    """Wait until the second continuation durably reconciles the leaf dispatch.

    The boundary is leaf-specific: the restart must record an owner-found
    dispatch reconciliation for the persisted intent *and* adopt the leaf back
    to ``spawned`` under that same intent. Only then is the exactly-once spawn
    count meaningful. Returns ``(state, leaf, process_alive_at_boundary)``.
    """
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = child_store.load()
        except (OSError, ValueError):
            last = None
        leaf = last.slices.get(LEAF_NAME) if last is not None else None
        adopted = (
            leaf is not None
            and leaf.status is SliceStatus.SPAWNED
            and leaf.dispatch_intent_id == intent_id
            and leaf.dispatch_authoritative_event_seq is not None
        )
        if adopted and _reconciliation_events(repo, intent_id):
            return last, leaf, process.is_alive()
        if not process.is_alive():
            break
        time.sleep(0.1)
    raise AcceptanceError(
        "second continuation did not durably reconcile the leaf dispatch"
        + (f": {last!r}" if last is not None else "")
    )


def run_continuation_scenario(root: Path) -> dict[str, Any]:
    repo, remote, _branch = real.create_fixture(root / "continuation")
    mock, forgejo_url = real.start_mock(root / "continuation", PROJECT_ROOT, remote)
    server: subprocess.Popen[str] | None = None
    processes: list[multiprocessing.Process] = []
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
        parent_store, plan, _config, child_name, child_worktree = (
            _seed_failed_ordered_run(repo, state_root, swarm_id)
        )
        child_branch = f"main.{child_name}"
        child_store = RunStore(child_name, parent_store.run_dir)

        # Provision the stage through the real server so the failed startup
        # happened after the branch, identity, and worktree already existed.
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

        child_state = child_store.load()
        _require(
            isinstance(child_state.recursive_fsm, TLFailed),
            "crashed child did not persist a recursive failure checkpoint",
        )
        diagnostic = child_store.exit_diagnostics()
        _require(
            isinstance(diagnostic, dict)
            and diagnostic.get("checkpoint_revision") == child_state.revision,
            "child exit diagnostic is not bound to the failure checkpoint",
        )
        original_intent = parent_store.load().slices[child_name].dispatch_intent_id

        # First shipped continuation: reopen the terminal parent, relaunch the
        # child, and let the child reopen its own scope and request the leaf
        # spawn. The empty source stops the child before it consumes the
        # authoritative agent.spawned event, leaving the durable
        # dispatch_unconfirmed state a real crash-before-correlation produces.
        first = _start_production_continuation(
            PARENT_RUN, plan, state_root, repo, swarm_id, use_ledger=False
        )
        processes.append(first)
        child_state, leaf = _wait_for_leaf_dispatch_request(
            child_store, time.monotonic() + 120
        )
        _require(
            first.is_alive(),
            f"production continuation exited before leaf dispatch: {first.exitcode}",
        )
        _require(
            child_state.recursive_fsm is not None
            and not isinstance(child_state.recursive_fsm, TLFailed),
            "production continuation did not reopen the failed child scope",
        )
        parent_after = parent_store.load()
        _require(
            parent_after.slices[child_name].status is SliceStatus.SPAWNED,
            "production continuation did not relaunch the ordered child slice",
        )
        _require(
            parent_after.slices[child_name].dispatch_intent_id == original_intent,
            "production continuation minted a new child dispatch intent",
        )
        first_intent = leaf.dispatch_intent_id
        spawn_events = _leaf_spawn_events(repo, first_intent)
        _require(
            len(spawn_events) == 1,
            "resumed leaf was not spawned exactly once: "
            + json.dumps(spawn_events, default=str)[:3000],
        )
        first_spawns = len(spawn_events)

        real.stop_multiprocessing_process(
            first, "production continuation", process_group=True
        )
        processes.remove(first)

        # Repeated continuation: the leaf is dispatch_unconfirmed. The shipped
        # restart must durably reconcile the persisted intent and adopt the
        # existing owner, never mint a new intent or spawn a second leaf. Wait
        # for the leaf-specific owner-found reconciliation boundary before
        # counting, and count authoritative spawns across every intent.
        dispatch_requests_before = _leaf_dispatch_requests(repo)
        second = _start_production_continuation(
            PARENT_RUN, plan, state_root, repo, swarm_id, use_ledger=True
        )
        processes.append(second)
        _reconciled_state, reconciled_leaf, alive_at_boundary = (
            _wait_for_leaf_reconciliation(
                child_store, repo, first_intent, second, time.monotonic() + 120
            )
        )
        _require(
            reconciled_leaf.dispatch_intent_id == first_intent,
            "reconciliation changed the leaf dispatch intent",
        )
        dispatch_requests_after = _leaf_dispatch_requests(repo)
        _require(
            dispatch_requests_after == dispatch_requests_before,
            "repeated continuation issued a new leaf dispatch request: "
            f"before={len(dispatch_requests_before)}, "
            f"after={len(dispatch_requests_after)}",
        )
        repeated_events = _child_leaf_spawns(repo, child_name)
        _require(
            len(repeated_events) == 1,
            "repeated continuation duplicated the leaf dispatch across intents: "
            + json.dumps(repeated_events, default=str)[:3000],
        )
        repeated_spawns = len(repeated_events)
        real.stop_multiprocessing_process(
            second, "repeated continuation", process_group=True
        )
        processes.remove(second)

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
        return {
            "scenario": "failed-child-startup-and-continuation",
            "child": child_name,
            "leaf": LEAF_NAME,
            "production_path": "run_tl_loop(session_mode=continue)",
            "child_relaunched": True,
            "child_dispatch_intent_preserved": True,
            "leaf_dispatched_once": first_spawns == 1,
            "repeated_continuation_spawns": repeated_spawns,
            "repeated_continuation_reconciled": True,
            "repeated_continuation_alive_at_boundary": alive_at_boundary,
            "repeated_continuation_new_dispatch_requests": 0,
            "worktrees": worktrees_after,
        }
    finally:
        for process in processes:
            try:
                real.stop_multiprocessing_process(
                    process, "continuation controller", process_group=True
                )
            except real.HarnessError:
                pass
        diagnostics: list[str] = []
        real.best_effort_worker_cleanup(repo, LEAF_NAME, diagnostics)
        if server is not None:
            real.stop_server(server, repo, "continuation acceptance")
        real.stop_subprocess(mock, "continuation mock API")
        _kill_tmux_session(repo)


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

    # The only expected failure is the final tmux attach, which cannot succeed
    # without a TTY. Any other exit code or failure marker means the recreate
    # or same-plan restart did not actually complete, so the acceptance must
    # fail loudly rather than accept an unexplained non-zero exit.
    _require(
        init.returncode == 1,
        f"exomonad init --recreate exited {init.returncode}, expected 1 from the "
        f"documented non-TTY attach failure: {log_text[-2000:]}",
    )
    _require(
        "open terminal failed: not a terminal" in log_text,
        "exomonad init --recreate did not fail with the documented tmux attach error",
    )
    _require(
        "Attaching to session" in log_text and "Creating session" in log_text,
        "exomonad init --recreate failed before creating and attaching to the session",
    )
    _require(
        "panicked" not in log_text.lower()
        and "refusing --recreate" not in log_text
        and "ordered_sub_tl" not in log_text,
        f"exomonad init --recreate failed for an unexpected reason: {log_text[-2000:]}",
    )
    # Match the exact ordered-ownership conflict markers rather than a bare
    # "409", which can appear incidentally in ports, pids, or addresses.
    _require(
        "ordered_sub_tl_identity_conflict" not in log_text
        and "already exists without matching durable identity" not in log_text
        and "does not match its authenticated parent" not in log_text
        and "ordered sub-TL identity" not in log_text,
        "recreate/restart produced an ordered ownership conflict",
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
        "init_failure": "tmux attach (non-TTY)",
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
