#!/usr/bin/env python3
"""Compiled-host acceptance for ``resume_blocked_leaf`` recovery boundaries.

The in-process UDS stub cannot show that a *compiled* ``exomonad serve`` and its
real tmux launch are adopted after the old 10s client timeout, nor that a host
killed at the spawn boundary never becomes a second owner.  This harness runs
the shipped binary against a disposable repository, a mock forge, a run-scoped
tmux server and the production controller journal/reconciler.

Cases
-----
``slow-adoption``
    The host handler is held past the old 10s client deadline, the controller
    times out and journals ``unknown``, the host then launches, and the
    production reconciliation hook adopts exactly one launch from the ledger.

``init-continue-before-window``
    The compiled host is SIGKILLed after it recorded the intent but before a
    worker window exists.  A real ``exomonad init --continue`` starts the new
    server and controller; the pending host journal launches exactly one
    successor and the controller adopts it.

``sigkill-after-window``
    The compiled host is SIGKILLed after the worker window and routing record
    exist but before ``agent.spawned`` is written.  The restarted host re-drives
    the pending journal, adopts the already-live successor and writes exactly
    one launch row; the controller adopts it.  Exactly one owner/window
    survives and nothing is relaunched.

Every tmux call goes through ``e2e_harness.tmuxio`` with the run's own ``-S``
socket.  A missing tmux/binary/WASM is an explicit SKIP with a reason.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ORDERED_DIR = PROJECT_ROOT / "tests/e2e/ordered-recursive"
GATE_DIR = PROJECT_ROOT / "tests/e2e/task-blocked-human-gate"
for path in (ORDERED_DIR, GATE_DIR, PROJECT_ROOT, PROJECT_ROOT / "tests/e2e/lib"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import real_server_transport as real
import run as gate
from e2e_harness import tmuxio

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import TransportClient, TransportError
from tl_loop.fsm.phase import ChildHandle, TLWaiting
from tl_loop.loop.driver import (
    EffectIntent,
    TLLoopConfig,
    WorkPlan,
    _manifest_for_plan,
    _reconcile_unknown_launch_actions,
)
from tl_loop.loop.journal import EffectJournal
from tl_loop.state.schema import (
    RepositoryIdentity,
    RunState,
    SliceState,
    SliceStatus,
)
from tl_loop.state.store import RunStore, _encode_slice, create

SLICE_ID = "issue-814-substitution-model-kani"
OWNER_HINT = "issue-814-substitution-model-kani"
ISSUE_ID = 814
RUN_ID = "root"
# The old interactive transport default.  The host is held well past it so the
# controller's timeout is real, not an injected exception.
OLD_CLIENT_TIMEOUT_SECONDS = 10.0
PAUSE_PAST_TIMEOUT_SECONDS = 13.0

SMART_CODEX = """#!/bin/sh
if [ "$1" = "debug" ] && [ "$2" = "models" ]; then
  printf '%s\\n' '{"models":[{"slug":"gpt-5.6-luna","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]},{"slug":"gpt-6-luna","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]},{"slug":"gpt-6.1-sol","supported_reasoning_levels":[{"effort":"minimal"},{"effort":"low"},{"effort":"medium"},{"effort":"high"},{"effort":"xhigh"}]}]}'
  exit 0
fi
exec sleep 300
"""


class HarnessError(RuntimeError):
    """The compiled-host contract was violated."""


def log(message: str) -> None:
    print(message, flush=True)


def wait_until(predicate: Any, description: str, timeout: float = 90.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise HarnessError(
        f"timed out after {timeout:.0f}s waiting for {description}; last={last!r}"
    )


# ---------------------------------------------------------------------------
# Fixture setup


def prepare_repository(root: Path) -> tuple[Path, Path, subprocess.Popen[str], str]:
    repo, remote, _ = real.create_fixture(root)
    # Repository identity resolution rejects file:// URLs.  Keep the writable
    # transport on the disposable bare repository while the canonical remote
    # is a normal https-shaped name, exactly as the init-continue acceptance
    # does.
    real.git(repo, "remote", "set-url", "origin", "https://forgejo-e2e/owner/repo.git")
    real.git(repo, "remote", "set-url", "--push", "origin", str(remote))
    (repo / ".chainlink").mkdir(parents=True, exist_ok=True)
    gitignore = repo / ".gitignore"
    # init --continue writes .mcp.json into the project root.  The TL spawn
    # preflight refuses a dirty worktree, so keep that generated file untracked
    # but ignored: the fixture owns its own state and the restart must not turn
    # init's own scaffolding into a spawn refusal.
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8")
        + ".chainlink/\n.mcp.json\n",
        encoding="utf-8",
    )
    real.git(repo, "add", ".gitignore")
    real.git(repo, "commit", "-q", "-m", "Ignore disposable Chainlink runtime")
    real.git(repo, "update-ref", "refs/heads/main", "HEAD")
    mock, forgejo = real.start_mock(root, PROJECT_ROOT, remote)
    return repo, remote, mock, forgejo


def start_server(
    root: Path, repo: Path, forgejo: str
) -> tuple[subprocess.Popen[str], TransportClient]:
    chainlink_db = repo / ".chainlink/issues.db"
    return real.start_server(
        root,
        repo,
        forgejo,
        PROJECT_ROOT,
        spawn_agent_type="codex",
        worker_model="gpt-6-luna",
        worker_effort="xhigh",
        chainlink_db=chainlink_db,
    )


def create_blocked_owner(
    repo: Path, client: TransportClient
) -> tuple[Path, Path, dict[str, Any], dict[str, Any], str, str]:
    effects = EffectClient(client, role="tl", name="root")
    created = effects.chainlink_issue_create(
        title="E2E compiled blocked resume",
        description="Disposable acceptance issue; the run removes it.",
        labels=("bug",),
        priority="high",
    )
    if not created.success:
        raise HarnessError(f"real Chainlink issue creation failed: {created.raw!r}")
    chainlink_db = repo / ".chainlink/issues.db"
    import sqlite3

    with sqlite3.connect(chainlink_db) as database:
        database.execute(
            "UPDATE issues SET id = ? WHERE id = ?", (ISSUE_ID, gate.issue_number(created))
        )
        database.execute(
            "UPDATE sqlite_sequence SET seq = ? WHERE name = 'issues'", (ISSUE_ID,)
        )
    spawned = effects.spawn_leaf(
        name=OWNER_HINT,
        task=f"Work on Chainlink issue #{ISSUE_ID}; this owner will be parked.",
        agent_type="codex",
        model="gpt-6-luna",
    )
    if not spawned.success:
        raise HarnessError(f"real leaf spawn failed: {spawned.raw!r}")
    owner_dir = gate.owner_for(repo)
    worktree = gate.worktree_for(repo, owner_dir)
    (owner_dir / "active_issue").write_text(str(ISSUE_ID), encoding="utf-8")
    initial = gate.invocation(owner_dir)
    identity = gate.identity(owner_dir)
    branch = str(identity["birth_branch"])
    fingerprint = gate.worktree_fingerprint(worktree)
    gate.kill_owner_target(owner_dir)
    wait_until(
        lambda: gate.invocation(owner_dir).get("status") in {"exited", "failed", "killed"},
        "the blocked owner invocation to become terminal",
    )
    parked = effects.emit_controller_event(
        event_type="tl.slice_parked",
        payload={
            "slice_id": SLICE_ID,
            "park_cause": "base_ci_unstable",
            "attempts": 1,
            "reason": "base CI failed independently of the leaf",
        },
    )
    blocked = effects.emit_controller_event(
        event_type="agent.task_blocked",
        payload={
            "outcome": "blocked",
            "slice_id": SLICE_ID,
            "cause": "base_ci_unstable",
            "scope_attribution": "base",
            "needs_human": True,
            "retryable": True,
            "recovery_action": "stabilize base CI, then resume the parked owner",
            "declared_difficulty": "standard",
            "matched_difficulty_rule": "standard_slice",
            "attempt": 1,
        },
    )
    if not parked.success or not blocked.success:
        raise HarnessError(
            f"real blocked events failed: {parked.raw!r} {blocked.raw!r}"
        )
    return owner_dir, worktree, initial, identity, branch, fingerprint


def resume_arguments(
    repo: Path,
    client: TransportClient,
    owner_dir: Path,
    worktree: Path,
    initial: Mapping[str, Any],
    identity: Mapping[str, Any],
    branch: str,
    fingerprint: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    route = gate.configured_resume_route(
        client,
        repo=repo,
        owner_dir=owner_dir,
        identity_record=identity,
        invocation_record=initial,
        branch=branch,
        worktree=worktree,
        worktree_fingerprint=fingerprint,
        issue_id=ISSUE_ID,
    )
    gate.seed_resume_dispatch_declaration(repo, route)
    request = gate.resume_arguments(
        {
            "chainlink_issue_id": ISSUE_ID,
            "expected_invocation_id": initial["invocation_id"],
            "expected_branch": branch,
            "expected_worktree_fingerprint": fingerprint,
            "task": "Complete the parked assignment on the preserved owner.",
            "human_approved": True,
        },
        route,
    )
    return request, route


def server_environment(process: subprocess.Popen[str]) -> dict[str, str]:
    """Capture the exact environment the compiled server was started with.

    A replacement server has to inherit the run's PATH, isolated Codex home,
    Chainlink database and tmux socket directory; the harness never rebuilds
    them from its own process, which would silently lose fixture isolation.
    """
    raw = Path(f"/proc/{process.pid}/environ").read_bytes()
    environment: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            environment[key.decode()] = value.decode(errors="replace")
    return environment


def start_attached_server(
    root: Path,
    repo: Path,
    environment: Mapping[str, str],
    *,
    extra: Mapping[str, str] | None = None,
    label: str,
) -> tuple[subprocess.Popen[str], TransportClient]:
    env = dict(environment)
    env.update(extra or {})
    log_path = root / f"{label}.log"
    handle = log_path.open("w", encoding="utf-8")
    binary = Path(os.environ.get("EXOMONAD_E2E_BIN", PROJECT_ROOT / "target/debug/exomonad"))
    process = subprocess.Popen(
        [str(binary), "serve"],
        cwd=repo,
        env=env,
        stdout=handle,
        stderr=handle,
        text=True,
        start_new_session=True,
    )
    client = TransportClient(project_root=repo, timeout=5, launch_timeout=300)
    try:
        real.wait_for_server(client, process, log_path)
    except BaseException:
        real.stop_subprocess(process, label)
        raise
    return process, client


def stop_server_process(process: subprocess.Popen[str]) -> None:
    real.stop_subprocess(process, "compiled host")


def sigkill(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def smart_codex(root: Path) -> None:
    path = root / "fake-bin" / "codex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SMART_CODEX, encoding="utf-8")
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# Controller journal


def controller_journal(
    repo: Path,
    owner_dir: Path,
    worktree: Path,
    identity: Mapping[str, Any],
    initial: Mapping[str, Any],
    request: Mapping[str, Any],
    route: Mapping[str, str],
) -> tuple[EffectJournal, EffectIntent]:
    state_root = repo / ".exo/tl-loop" / RUN_ID
    state_root.mkdir(parents=True, exist_ok=True)
    arguments = dict(request)
    arguments.update(
        {
            "owner_agent_name": owner_dir.name,
            "prior_generation": initial.get("generation", 0),
            "expected_worktree": str(worktree),
            "expected_worktree_recorded": str(identity["working_dir"]),
            "dispatch_intent_id": route["dispatch_intent_id"],
            "dispatch_attempt": 1,
            "dispatch_started_at": time.time(),
            "dispatch_generation": 1,
            "dispatch_ledger_floor": gate._highest_sequence(repo),
            "controller_epoch": "e2e-blocked-resume-host",
            "route_reconciliation_source": "proven_preexecution_retry",
        }
    )
    journal = EffectJournal(RUN_ID, state_root / "action-journal.json")
    intent = EffectIntent("resume_blocked_leaf", SLICE_ID, arguments, True)
    journal.append(intent)
    return journal, intent


def tick_until_resolved(
    repo: Path,
    journal: EffectJournal,
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run the production reconciliation hook repeatedly, as an idle controller would.

    The hook owns the run-scoped lock, reads the immutable ledger and mutates
    only the journal through its own API.  This is deliberately a loop, not one
    call: a held entry must be re-evaluated on later ticks.
    """
    state_root = repo / ".exo/tl-loop"
    store = RunStore(RUN_ID, state_root)
    deadline = time.monotonic() + timeout
    ticks = 0
    while time.monotonic() < deadline:
        ticks += 1
        _, resolved = _reconcile_unknown_launch_actions(
            cast(RunState, object()),
            store,
            journal,
            project_root=repo,
            ledger_run_id=real.server_run_id(repo),
        )
        if resolved:
            entry = journal.entries_for("resume_blocked_leaf", SLICE_ID)[0]
            entry["ticks"] = ticks
            return entry
        time.sleep(0.5)
    entries = journal.entries_for("resume_blocked_leaf", SLICE_ID)
    raise HarnessError(f"launch reconciliation did not adopt after {ticks} ticks: {entries!r}")


def journal_entry(journal: EffectJournal) -> dict[str, Any]:
    entries = journal.entries_for("resume_blocked_leaf", SLICE_ID)
    if len(entries) != 1:
        raise HarnessError(f"expected one blocked-resume journal entry, found {entries!r}")
    return entries[0]


# ---------------------------------------------------------------------------
# Ledger and tmux assertions


def intent_launches(repo: Path, intent: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in real.server_ledger_events(repo)
        if row.get("type") == "agent.spawned"
        and isinstance(row.get("data"), Mapping)
        and row["data"].get("intent_id") == intent
    ]


def intent_failures(repo: Path, intent: str) -> list[Mapping[str, Any]]:
    return [
        row
        for row in real.server_ledger_events(repo)
        if row.get("type") == "agent.spawn_failed"
        and isinstance(row.get("data"), Mapping)
        and row["data"].get("intent_id") == intent
    ]


def intent_calls(repo: Path, intent: str) -> list[Mapping[str, Any]]:
    return [
        row["data"]
        for row in real.server_ledger_events(repo)
        if row.get("type") == "tool.called"
        and isinstance(row.get("data"), Mapping)
        and row["data"].get("dispatch_intent_id") == intent
    ]


def owner_windows(repo: Path, owner_dir: Path, identity: Mapping[str, Any]) -> list[str]:
    socket = real.tmux_socket(repo)
    slug = str(identity.get("slug") or owner_dir.name)
    rows = tmuxio.tmux(
        socket, "list-windows", "-a", "-F", "#{window_id}\t#{window_name}", check=False
    ).stdout.splitlines()
    return [
        row
        for row in rows
        if slug in row.split("\t", 1)[-1] or owner_dir.name in row.split("\t", 1)[-1]
    ]


def host_journal_phase(owner_dir: Path) -> str | None:
    path = owner_dir / "blocked_resume_journal.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    phase = value.get("phase")
    return phase if isinstance(phase, str) else None


def owner_routing_target(owner_dir: Path) -> str | None:
    path = owner_dir / "routing.json"
    if not path.is_file():
        return None
    routing = json.loads(path.read_text(encoding="utf-8"))
    target = routing.get("window_id") or routing.get("pane_id")
    return target if isinstance(target, str) else None


def assert_one_owner(
    repo: Path, owner_dir: Path, identity: Mapping[str, Any], *, context: str
) -> dict[str, Any]:
    windows = owner_windows(repo, owner_dir, identity)
    if len(windows) != 1:
        raise HarnessError(f"{context}: expected exactly one owner window, found {windows!r}")
    invocation = gate.invocation(owner_dir)
    if invocation.get("status") != "running":
        raise HarnessError(f"{context}: owner invocation is not live: {invocation!r}")
    target = owner_routing_target(owner_dir)
    if target is None:
        raise HarnessError(f"{context}: owner routing target is missing")
    if target not in windows[0]:
        raise HarnessError(
            f"{context}: owner routing {target} does not name the surviving window {windows[0]!r}"
        )
    return {"windows": windows, "target": target, "invocation_id": invocation["invocation_id"]}


# ---------------------------------------------------------------------------
# Controller checkpoint seeding for init --continue


def seed_controller_run(
    repo: Path,
    owner_dir: Path,
    *,
    branch: str,
    worktree: Path,
    invocation: Mapping[str, Any],
) -> None:
    state_root = repo / ".exo/tl-loop"
    state_root.mkdir(parents=True, exist_ok=True)
    plan_document = {
        "run_id": RUN_ID,
        "budgets": {"tokens": 120000, "wall_seconds": 1800},
        "plan": {
            "workers": [],
            "leaves": [
                {
                    "name": SLICE_ID,
                    "agent_type": "codex",
                    "task": "Hold the compiled blocked-resume fixture.",
                    "chainlink_issue_id": None,
                    "verify": ["true"],
                    "depends_on": [],
                }
            ],
            "sub_tls": [],
        },
    }
    plan_bytes = (json.dumps(plan_document, indent=2, sort_keys=True) + "\n").encode()
    (state_root / "plan.json").write_bytes(plan_bytes)
    (state_root / "plan.snapshot").write_bytes(plan_bytes)
    import hashlib

    (state_root / "plan.snapshot.sha256").write_text(
        hashlib.sha256(plan_bytes).hexdigest() + "\n", encoding="utf-8"
    )
    work_plan = WorkPlan.from_mapping(plan_document["plan"])
    identity = RepositoryIdentity("https://forgejo-e2e/owner/repo.git")
    slice_state = SliceState(
        id=SLICE_ID,
        status=SliceStatus.SPAWNED,
        paths=("README.md",),
        depends_on=(),
        base_ref="main",
        test_plan=("true",),
        agent_type="codex",
        model="gpt-6-luna",
        branch=branch,
        worktree=str(worktree),
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        chainlink_issue_id=None,
        dispatch_intent_id="fixture-holder-intent",
        dispatch_started_at=time.time() - 10,
        dispatch_last_boundary="agent.spawned",
        dispatch_agent_id=owner_dir.name,
        dispatch_invocation_id=invocation["invocation_id"],
        dispatch_authoritative_event_seq=1,
    )
    create(
        RUN_ID,
        {
            "slices": {SLICE_ID: _encode_slice(SLICE_ID, slice_state)},
            "owner_branch": "main",
            "owner_worktree": str(repo),
            "repository_identity": identity.to_document(),
        },
        root_dir=state_root,
    )
    store = RunStore(RUN_ID, state_root)
    store.set_plan_manifest(
        _manifest_for_plan(
            work_plan,
            RUN_ID,
            TLLoopConfig(run_id=RUN_ID, root_dir=state_root, branch="main"),
        ),
        slices={SLICE_ID: slice_state},
    )
    store.checkpoint(
        TLWaiting({SLICE_ID: ChildHandle(SLICE_ID, branch, "codex")}),
        {SLICE_ID: slice_state},
        {"tokens": 0, "wall_seconds": 0},
        0,
    )


def install_reviewer_route(repo: Path) -> None:
    config = (repo / ".exo/config.toml").read_text(encoding="utf-8")
    if "[reviewer]" not in config:
        (repo / ".exo/config.toml").write_text(
            config
            + '\n[reviewer]\nagent_type = "codex"\nmodel = "gpt-5.6-luna"\neffort_level = "medium"\n',
            encoding="utf-8",
        )
    for name in ("harness_policy.toml", "harness_capability.toml", "review-policy.toml"):
        source = PROJECT_ROOT / ".exo" / name
        if source.is_file():
            (repo / ".exo" / name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )


def session_name(repo: Path) -> str:
    config = (repo / ".exo/config.toml").read_text(encoding="utf-8")
    return next(
        line.split("=", 1)[1].strip().strip('"')
        for line in config.splitlines()
        if line.startswith("tmux_session =")
    )


def run_init_continue(
    root: Path,
    repo: Path,
    environment: Mapping[str, str],
    *,
    label: str,
    extra: Mapping[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Path]:
    env = tmuxio.child_env(root, {**environment, **(extra or {})})
    env.setdefault("NO_COLOR", "1")
    log_path = root / f"{label}.log"
    handle = log_path.open("w", encoding="utf-8")
    binary = Path(os.environ.get("EXOMONAD_E2E_BIN", PROJECT_ROOT / "target/debug/exomonad"))
    process = subprocess.Popen(
        [str(binary), "init", "--continue", "--session", session_name(repo)],
        cwd=repo,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    return process, log_path


def init_succeeded_or_detached(process: subprocess.Popen[str], log_path: Path) -> None:
    try:
        status = process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        raise HarnessError(f"real init --continue did not return: {log_path}")
    if status == 0:
        return
    text = log_path.read_text(encoding="utf-8", errors="replace")
    if "open terminal failed: not a terminal" in text:
        return
    raise HarnessError(f"real init --continue exited {status}:\n{text[-4000:]}")


KEEP_RUNS = False


def teardown(root: Path, repo: Path, mock: subprocess.Popen[str] | None) -> None:
    pid_file = repo / ".exo/server.pid"
    if pid_file.is_file():
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    try:
        tmuxio.tmux(real.tmux_socket(repo), "kill-server", check=False)
    except (OSError, RuntimeError):
        pass
    if mock is not None:
        real.stop_subprocess(mock, "mock forge")
    if not KEEP_RUNS:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cases


def case_slow_adoption(root: Path) -> dict[str, Any]:
    repo, _remote, mock, forgejo = prepare_repository(root)
    server: subprocess.Popen[str] | None = None
    try:
        server, client = start_server(root, repo, forgejo)
        owner_dir, worktree, initial, identity, branch, fingerprint = create_blocked_owner(
            repo, client
        )
        request, route = resume_arguments(
            repo, client, owner_dir, worktree, initial, identity, branch, fingerprint
        )
        intent = route["dispatch_intent_id"]
        journal, effect = controller_journal(
            repo, owner_dir, worktree, identity, initial, request, route
        )
        environment = server_environment(server)
        stop_server_process(server)
        marker_dir = root / "interrupt"
        server, client = start_attached_server(
            root,
            repo,
            environment,
            extra={
                "EXOMONAD_E2E_INTERRUPT_AT": "blocked_resume_before_launch",
                "EXOMONAD_E2E_INTERRUPT_SCOPE": intent,
                "EXOMONAD_E2E_INTERRUPT_DIR": str(marker_dir),
                "EXOMONAD_E2E_INTERRUPT_ACTION": "pause",
            },
            label="slow-server",
        )
        release = threading.Thread(
            target=_release_after_timeout, args=(marker_dir, "blocked_resume_before_launch")
        )
        release.start()
        started = time.monotonic()
        timeout_error: BaseException | None = None
        result: Mapping[str, Any] | None = None
        # Force the old interactive budget so the host's real >10s handler
        # proves the controller's timeout path, not a slow test client.
        timeout_client = TransportClient(
            project_root=repo, timeout=5, launch_timeout=OLD_CLIENT_TIMEOUT_SECONDS
        )
        try:
            result = timeout_client.call_tool("tl", "root", "resume_blocked_leaf", request)
        except TransportError as error:
            timeout_error = error
        elapsed = time.monotonic() - started
        if timeout_error is None:
            raise HarnessError(
                "the old 10s client deadline did not expire while the host was held: "
                f"result={result!r}"
            )
        if elapsed < OLD_CLIENT_TIMEOUT_SECONDS:
            raise HarnessError(
                f"client returned before the old deadline: {elapsed:.2f}s"
            )
        journal.mark_unknown(effect, timeout_error)
        # Keep the production reconciliation hook ticking while the host is
        # still held.  The early ticks must observe no evidence and hold; a
        # later tick after the host launch adopts.  This is the idle-heartbeat
        # path, not a single post-hoc call.
        adopted: list[dict[str, Any]] = []

        def ticker() -> None:
            adopted.append(tick_until_resolved(repo, journal, timeout=180))

        tick_thread = threading.Thread(target=ticker)
        tick_thread.start()
        release.join(timeout=60)
        launches = wait_until(
            lambda: intent_launches(repo, intent) or None,
            "the held host launch to become durable",
            timeout=120,
        )
        if len(launches) != 1:
            raise HarnessError(f"expected one launch row, found {launches!r}")
        tick_thread.join(timeout=180)
        if not adopted:
            raise HarnessError("the long-running reconciliation tick did not finish")
        entry = adopted[0]
        if int(entry.get("ticks", 0)) < 2:
            raise HarnessError(
                f"reconciliation adopted on the first tick; no held re-evaluation occurred: {entry!r}"
            )
        if entry.get("status") != "confirmed":
            raise HarnessError(f"journal was not adopted: {entry!r}")
        reconciliation = entry.get("launch_reconciliation")
        if not isinstance(reconciliation, Mapping) or reconciliation.get("outcome") != "adopt":
            raise HarnessError(f"journal has no adopt audit: {entry!r}")
        if len(intent_launches(repo, intent)) != 1:
            raise HarnessError("adoption produced a second launch row")
        calls = intent_calls(repo, intent)
        if len(calls) != 1 or calls[0].get("success") is not True:
            raise HarnessError(f"expected one successful completion, found {calls!r}")
        owner = assert_one_owner(repo, owner_dir, gate.identity(owner_dir), context="slow-adoption")
        return {
            "case": "slow-adoption",
            "client_timeout_seconds": round(elapsed, 2),
            "old_client_timeout_seconds": OLD_CLIENT_TIMEOUT_SECONDS,
            "host_launch_count": len(launches),
            "adoption_ticks": entry.get("ticks"),
            "completion_count": len(calls),
            "adopted_invocation": entry.get("result", {}).get("result", {}).get("invocation", {}).get("invocation_id")
            if isinstance(entry.get("result"), Mapping)
            else None,
            "reconciliation": dict(reconciliation),
            "owner": owner,
        }
    finally:
        if server is not None:
            stop_server_process(server)
        teardown(root, repo, mock)


def _release_after_timeout(marker_dir: Path, boundary: str) -> None:
    marker = marker_dir / f"{boundary}.json"
    wait_until(marker.is_file, f"host pause marker {marker}", timeout=60)
    time.sleep(PAUSE_PAST_TIMEOUT_SECONDS)
    (marker_dir / f"{boundary}.release").write_text("release\n", encoding="utf-8")


def case_init_continue_before_window(root: Path) -> dict[str, Any]:
    return _case_sigkill_boundary(
        root,
        boundary="blocked_resume_before_launch",
        after_window=False,
    )


def case_sigkill_after_window(root: Path) -> dict[str, Any]:
    return _case_sigkill_boundary(
        root,
        boundary="blocked_resume_window_before_launch_row",
        after_window=True,
    )


def _case_sigkill_boundary(
    root: Path, *, boundary: str, after_window: bool
) -> dict[str, Any]:
    repo, _remote, mock, forgejo = prepare_repository(root)
    server: subprocess.Popen[str] | None = None
    try:
        server, client = start_server(root, repo, forgejo)
        owner_dir, worktree, initial, identity, branch, fingerprint = create_blocked_owner(
            repo, client
        )
        environment = server_environment(server)
        install_reviewer_route(repo)
        smart_codex(root)
        seed_controller_run(
            repo, owner_dir, branch=branch, worktree=worktree, invocation=initial
        )
        # The accepted role configuration includes the reviewer route.  Restart
        # once after installing it so the route/declaration are minted against
        # the same identity the boundary server will accept, then start the
        # boundary server from that identity.
        stop_server_process(server)
        server, client = start_attached_server(
            root, repo, environment, label="configured-server"
        )
        request, route = resume_arguments(
            repo, client, owner_dir, worktree, initial, identity, branch, fingerprint
        )
        intent = route["dispatch_intent_id"]
        journal, effect = controller_journal(
            repo, owner_dir, worktree, identity, initial, request, route
        )
        stop_server_process(server)
        marker_dir = root / "interrupt"
        server, client = start_attached_server(
            root,
            repo,
            environment,
            extra={
                "EXOMONAD_E2E_INTERRUPT_AT": boundary,
                "EXOMONAD_E2E_INTERRUPT_SCOPE": intent,
                "EXOMONAD_E2E_INTERRUPT_DIR": str(marker_dir),
                "EXOMONAD_E2E_INTERRUPT_ACTION": "pause",
            },
            label="boundary-server",
        )
        request_error: list[Exception] = []

        def send() -> None:
            try:
                TransportClient(project_root=repo, timeout=5, launch_timeout=300).call_tool(
                    "tl", "root", "resume_blocked_leaf", request
                )
            except (TransportError, OSError, EOFError) as error:
                # The host is about to be SIGKILLed; a transport failure is the
                # expected observation, not a harness error.
                request_error.append(error)

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        wait_until(
            (marker_dir / f"{boundary}.json").is_file,
            f"host reached {boundary}",
            timeout=60,
        )
        launches_before = intent_launches(repo, intent)
        if after_window:
            if launches_before:
                raise HarnessError(
                    f"{boundary} must precede agent.spawned, found {launches_before!r}"
                )
            if owner_routing_target(owner_dir) is None:
                raise HarnessError(f"{boundary}: the owner window/routing did not exist")
        sigkill(server)
        server = None
        sender.join(timeout=15)
        timeout_error = request_error[0] if request_error else ConnectionError(
            "compiled host was SIGKILLed at the spawn boundary"
        )
        journal.mark_unknown(effect, timeout_error)

        # Before a window exists the old session is disposable, so a real
        # `init --continue` starts the replacement server and controller.  A
        # managed controller target does not exist in this fixture, so the
        # after-window case restarts the compiled server in the preserved
        # session directly: killing the session would remove the orphan window
        # whose survival is the property under test.
        socket = real.tmux_socket(repo)
        if after_window:
            server, _restarted = start_attached_server(
                root, repo, environment, label="after-window-server"
            )
            # The re-drive either adopts the already-live successor (the host
            # journal moves to `launched` because the invocation no longer
            # matches the requested predecessor) or records a terminal
            # spawn_failed.  Both are safe; neither may launch a second owner.
            outcome = wait_until(
                lambda: (
                    "launched"
                    if host_journal_phase(owner_dir) == "launched"
                    else (intent_failures(repo, intent) or None)
                ),
                "the restarted host to adopt or terminally refuse the pending journal",
                timeout=120,
            )
            failures = intent_failures(repo, intent)
            if outcome == "launched":
                launches = wait_until(
                    lambda: intent_launches(repo, intent) or None,
                    "the adopted successor's launch row",
                    timeout=60,
                )
                if len(launches) != 1:
                    raise HarnessError(f"adoption wrote {len(launches)} launch rows")
            elif len(failures) != 1:
                raise HarnessError(f"expected one terminal failure, found {failures!r}")
            owner = assert_one_owner(
                repo, owner_dir, gate.identity(owner_dir), context=boundary
            )
            successor = gate.invocation(owner_dir)
            if (
                successor.get("trigger") != "resume_blocked"
                or successor.get("prior_invocation_id") != initial["invocation_id"]
                or successor.get("generation") != int(initial.get("generation", 0)) + 1
            ):
                raise HarnessError(
                    f"the preserved successor is not the boundary launch: {successor!r}"
                )
            if intent_launches(repo, intent):
                entry = tick_until_resolved(repo, journal)
                if entry.get("status") != "confirmed":
                    raise HarnessError(f"the adopted launch was not adopted: {entry!r}")
                reconciliation = entry.get("launch_reconciliation")
            else:
                # No launch proof: a terminal failure row only is a deliberate
                # hold, never an authorization to relaunch.
                state_root = repo / ".exo/tl-loop"
                store = RunStore(RUN_ID, state_root)
                for _ in range(3):
                    _reconcile_unknown_launch_actions(
                        cast(RunState, object()),
                        store,
                        journal,
                        project_root=repo,
                        ledger_run_id=real.server_run_id(repo),
                    )
                    time.sleep(0.2)
                entry = journal_entry(journal)
                if entry.get("status") != "unknown":
                    raise HarnessError(
                        f"the held entry was resolved by absent evidence: {entry!r}"
                    )
                reconciliation = entry.get("launch_reconciliation")
                if not isinstance(reconciliation, Mapping) or reconciliation.get(
                    "outcome"
                ) != "hold":
                    raise HarnessError(f"the entry has no hold audit: {entry!r}")
            return {
                "case": boundary,
                "owner": owner,
                "launch_count": len(intent_launches(repo, intent)),
                "failure_count": len(failures),
                "host_journal_phase": host_journal_phase(owner_dir),
                "journal_status": entry.get("status"),
                "reconciliation": dict(reconciliation)
                if isinstance(reconciliation, Mapping)
                else None,
            }

        tmuxio.tmux(socket, "kill-server", check=False)
        init_process, init_log = run_init_continue(
            root, repo, environment, label=f"init-{boundary}"
        )
        init_succeeded_or_detached(init_process, init_log)
        launches = wait_until(
            lambda: intent_launches(repo, intent) or None,
            "real init --continue's server to recover the pending journal",
            timeout=120,
        )
        if len(launches) != 1:
            raise HarnessError(f"expected one recovered launch, found {launches!r}")
        owner = assert_one_owner(repo, owner_dir, gate.identity(owner_dir), context=boundary)
        entry = tick_until_resolved(repo, journal)
        if entry.get("status") != "confirmed":
            raise HarnessError(
                f"controller did not adopt the init-recovered launch: {entry!r}"
            )
        # A second real init --continue must not add a launch or a window.
        before = len(intent_launches(repo, intent))
        repeat_process, repeat_log = run_init_continue(
            root, repo, environment, label="init-repeat"
        )
        init_succeeded_or_detached(repeat_process, repeat_log)
        time.sleep(2)
        if len(intent_launches(repo, intent)) != before:
            raise HarnessError("repeat init --continue added a second launch")
        owner = assert_one_owner(repo, owner_dir, gate.identity(owner_dir), context="repeat")
        return {
            "case": boundary,
            "owner": owner,
            "launch_count": len(intent_launches(repo, intent)),
            "journal_status": entry.get("status"),
            "restart_adoption_ticks": entry.get("ticks"),
            "repeat_launch_count": len(intent_launches(repo, intent)),
        }
    finally:
        if server is not None:
            sigkill(server)
        teardown(root, repo, mock)


def case_terminal_failure_hold(root: Path) -> dict[str, Any]:
    """A restarted host that records only a terminal failure leaves a hold."""
    repo, _remote, mock, forgejo = prepare_repository(root)
    server: subprocess.Popen[str] | None = None
    try:
        server, client = start_server(root, repo, forgejo)
        owner_dir, worktree, initial, identity, branch, fingerprint = create_blocked_owner(
            repo, client
        )
        request, route = resume_arguments(
            repo, client, owner_dir, worktree, initial, identity, branch, fingerprint
        )
        intent = route["dispatch_intent_id"]
        journal, effect = controller_journal(
            repo, owner_dir, worktree, identity, initial, request, route
        )
        environment = server_environment(server)
        stop_server_process(server)
        marker_dir = root / "interrupt"
        server, client = start_attached_server(
            root,
            repo,
            environment,
            extra={
                "EXOMONAD_E2E_INTERRUPT_AT": "blocked_resume_before_launch",
                "EXOMONAD_E2E_INTERRUPT_SCOPE": intent,
                "EXOMONAD_E2E_INTERRUPT_DIR": str(marker_dir),
                "EXOMONAD_E2E_INTERRUPT_ACTION": "pause",
            },
            label="terminal-boundary-server",
        )
        request_error: list[Exception] = []

        def send() -> None:
            try:
                TransportClient(project_root=repo, timeout=5, launch_timeout=300).call_tool(
                    "tl", "root", "resume_blocked_leaf", request
                )
            except (TransportError, OSError, EOFError) as error:
                request_error.append(error)

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        wait_until(
            (marker_dir / "blocked_resume_before_launch.json").is_file,
            "host reached blocked_resume_before_launch",
            timeout=60,
        )
        sigkill(server)
        server = None
        sender.join(timeout=15)
        journal.mark_unknown(
            effect,
            request_error[0]
            if request_error
            else ConnectionError("host was SIGKILLed before launch"),
        )
        # Make the restarted host's re-drive refuse before any launch.  The
        # resulting host row is a terminal spawn_failed with no launch proof.
        (worktree / "dirty-at-recovery.txt").write_text(
            "the worktree is deliberately dirty at recovery\n", encoding="utf-8"
        )
        server, _client = start_attached_server(
            root, repo, environment, label="terminal-restart-server"
        )
        failures = wait_until(
            lambda: intent_failures(repo, intent) or None,
            "the restarted host to record a terminal failure",
            timeout=120,
        )
        if len(failures) != 1:
            raise HarnessError(f"expected one terminal failure, found {failures!r}")
        if intent_launches(repo, intent):
            raise HarnessError("a failed re-drive still launched an owner")
        if owner_windows(repo, owner_dir, identity):
            raise HarnessError("a failed re-drive created an owner window")
        store = RunStore(RUN_ID, repo / ".exo/tl-loop")
        for _ in range(3):
            _reconcile_unknown_launch_actions(
                cast(RunState, object()),
                store,
                journal,
                project_root=repo,
                ledger_run_id=real.server_run_id(repo),
            )
            time.sleep(0.2)
        entry = journal_entry(journal)
        if entry.get("status") != "unknown":
            raise HarnessError(f"a terminal failure resolved the entry: {entry!r}")
        hold = entry.get("launch_reconciliation")
        if not isinstance(hold, Mapping) or hold.get("outcome") != "hold":
            raise HarnessError(f"a terminal failure has no hold audit: {entry!r}")
        diagnostic = str(hold.get("diagnostic") or "")
        if "terminal failure" not in diagnostic:
            raise HarnessError(f"hold diagnostic does not name the failure: {diagnostic!r}")
        return {
            "case": "terminal-failure-hold",
            "launch_count": len(intent_launches(repo, intent)),
            "failure_count": len(failures),
            "owner_windows": owner_windows(repo, owner_dir, identity),
            "journal_status": entry.get("status"),
            "reconciliation": dict(hold),
        }
    finally:
        if server is not None:
            sigkill(server)
        teardown(root, repo, mock)


CASES = {
    "slow-adoption": case_slow_adoption,
    "init-continue-before-window": case_init_continue_before_window,
    "sigkill-after-window": case_sigkill_after_window,
    "terminal-failure-hold": case_terminal_failure_hold,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "case",
        nargs="*",
        choices=tuple(CASES),
        help="cases to run (default: all)",
    )
    parser.add_argument("--keep", action="store_true", help="keep a failed run directory")
    arguments = parser.parse_args()
    global KEEP_RUNS
    KEEP_RUNS = arguments.keep
    if shutil.which("tmux") is None:
        print(json.dumps({"skipped": "tmux is unavailable"}))
        return 77
    binary = Path(os.environ.get("EXOMONAD_E2E_BIN", PROJECT_ROOT / "target/debug/exomonad"))
    wasm = Path(
        os.environ.get(
            "EXOMONAD_E2E_WASM", PROJECT_ROOT / ".exo/wasm/wasm-guest-devswarm.wasm"
        )
    )
    if not binary.is_file() or not wasm.is_file():
        print(
            json.dumps(
                {
                    "skipped": "compiled exomonad or devswarm WASM is missing",
                    "binary": str(binary),
                    "wasm": str(wasm),
                }
            )
        )
        return 77
    selected = arguments.case or list(CASES)
    results: list[dict[str, Any]] = []
    for name in selected:
        root = Path(tempfile.mkdtemp(prefix=f"exo-blocked-resume-{name}-", dir="/tmp/opencode"))
        log(f"=== {name} root={root}")
        try:
            result = CASES[name](root)
        except BaseException:
            if arguments.keep:
                log(f"kept failed run directory: {root}")
            raise
        results.append(result)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        log(f"PASS {name}")
    print(json.dumps({"cases": results}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
