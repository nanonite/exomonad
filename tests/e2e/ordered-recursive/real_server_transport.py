#!/usr/bin/env python3
"""Exercise ordered TL ownership and PR evidence through the real server.

This harness deliberately keeps the Forgejo-shaped API local, but does not
replace the product boundary: the Rust server, generated WASM tool, Git
remote, TransportClient, and merge handler are all exercised.  The existing
``ordered_recursive.py`` remains the hermetic ControllerScenario coverage.

Run from the repository root after building the server and development WASM:

    python3 tests/e2e/ordered-recursive/real_server_transport.py

The first probe uses the real spawn_worker effect and ledger tailer with a
local root checkpoint. Later probes cover nested local checkpoint IDs,
restart boundaries, and tmux cleanup in the same temporary server session.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.transport import (
    JsonObject,
    TransportClient,
    TransportError,
)
from tl_loop.events.envelope import EventEnvelope, EventKind, project
from tl_loop.events.queue import LedgerQueue
from tl_loop.events.reader import LedgerReader
from tl_loop.fsm.phase import ChildHandle, TLPhase, TLPlanning, TLWaiting
from tl_loop.loop.driver import (
    LeafTask,
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _initial_slices,
    _manifest_for_plan,
    _supervise_live_sub_tl,
    run_tl_loop,
)
from tl_loop.loop.heartbeat import HeartbeatConfig
from tl_loop.ordered import IntegrationLifecycle
from tl_loop.rlm.store import RlmCallStore, RlmModelChoice, RlmResponse
from tl_loop.state.schema import (
    BudgetLedger,
    HandoffEvidence,
    IntegrationCandidateState,
    IntegrationRuntimeState,
    OrderedStageState,
    PublicationBinding,
    SliceState,
    SliceStatus,
    Verdict,
)
from tl_loop.state.store import CorruptCheckpoint, RunStore, create


class HarnessError(RuntimeError):
    """The server-backed acceptance contract was violated."""


class _RecoveryReviewBackend:
    """Deterministic review boundary for restart recovery evidence."""

    def complete(self, request: Any) -> RlmResponse:
        sections = request.inputs["sections"]
        head = next(
            section["content"]
            for section in sections
            if section["name"] == "reviewed_head"
        )
        return RlmResponse(
            {
                "verdict": Verdict.GO.value,
                "reviewed_head": head,
                "reasons": [],
                "blocking_count": 0,
            }
        )


def _recovery_review_choice() -> RlmModelChoice:
    """Provide an injected, tool-free reviewer for this server harness."""
    return RlmModelChoice(
        model_id="pre-pr-recovery-e2e",
        backend=_RecoveryReviewBackend(),
        store=RlmCallStore(),
        context_length=20_000,
    )


@dataclass
class RecoveryTrace:
    """Bounded, machine-readable evidence for one disposable recovery run."""

    path: Path
    records: list[dict[str, Any]]

    @classmethod
    def open(cls, path: Path) -> RecoveryTrace:
        return cls(path, [])

    def record(
        self,
        *,
        boundary: str,
        point: str,
        run_id: str,
        state_root: Path,
        repo: Path,
        resume_confirmation: Mapping[str, Any] | None = None,
        resume_invocation: Mapping[str, Any] | None = None,
        invocation_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        state = RunStore(run_id, state_root).load()
        journal_path = state_root / run_id / "action-journal.json"
        journal: list[dict[str, object]] = []
        if journal_path.exists():
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list) or any(
                not isinstance(item, dict) for item in payload
            ):
                raise HarnessError(f"invalid recovery action journal: {journal_path}")
            journal = payload
        session = f"ordered-server-e2e-{os.getpid()}"
        panes = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"],
            check=False,
            capture_output=True,
            text=True,
        )
        record = {
            "boundary": boundary,
            "point": point,
            "run_id": run_id,
            "phase": state.fsm.phase.value,
            "cursor": state.events.last_consumed_offset,
            "slices": {
                slice_id: slice_state.status.value
                for slice_id, slice_state in sorted(state.slices.items())
            },
            # Recovery lineage is authoritative only when it is read from the
            # persisted slice checkpoint.  Do not derive generations or rounds
            # from controller phases: those values describe the outer
            # orchestration FSM, not a leaf's recovery invocation.
            "recovery": {
                slice_id: {
                    "phase": recovery.phase.value,
                    "recovery_round": recovery.recovery_round,
                    "invocation_generation": recovery.invocation_generation,
                    "slice_attempt": recovery.slice_attempt,
                    "owner_run_id": recovery.owner_run_id,
                    "owner_agent_id": recovery.owner_agent_id,
                    "invocation_id": recovery.evidence.get("invocation_id"),
                    "branch": slice_state.branch,
                    "worktree": slice_state.worktree,
                    "dispatch_agent_id": slice_state.dispatch_agent_id,
                    "dispatch_invocation_id": slice_state.dispatch_invocation_id,
                }
                for slice_id, slice_state in sorted(state.slices.items())
                if (recovery := slice_state.recovery) is not None
            },
            "resume_confirmation": (
                dict(resume_confirmation) if resume_confirmation is not None else None
            ),
            "resume_invocation": (
                dict(resume_invocation) if resume_invocation is not None else None
            ),
            "invocation": (
                dict(invocation_snapshot) if invocation_snapshot is not None else None
            ),
            "action_keys": [
                {
                    "key": entry.get("key"),
                    "operation": entry.get("operation"),
                    "status": entry.get("status"),
                }
                for entry in journal
            ],
            "window_cardinality": len(panes.stdout.splitlines())
            if panes.returncode == 0
            else None,
            "ledger_events": len(server_ledger_events(repo)),
        }
        self.records.append(record)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"records": self.records}, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class EmptyEventSource:
    """An event source for child plans that finish without external events."""

    def get(self, timeout: float | None = None) -> Any:
        del timeout
        raise queue.Empty

    def acknowledge(self, event: Any) -> int:
        return event.run_seq


class LazyLedgerSource:
    """Create a real ledger queue inside the forked sub-TL controller."""

    def __init__(
        self,
        segments: Path,
        state_root: Path,
        run_id: str,
        ledger_run_id: str,
        scope_agent_id: str | None,
    ) -> None:
        self.segments = segments
        self.state_root = state_root
        self.run_id = run_id
        self.ledger_run_id = ledger_run_id
        self.scope_agent_id = scope_agent_id
        self._queue: LedgerQueue | None = None

    def _ensure_queue(self) -> LedgerQueue:
        if self._queue is None:
            self._queue = LedgerQueue(
                LedgerReader(
                    self.segments,
                    run_id=self.run_id,
                    state_root=self.state_root,
                    ledger_run_id=self.ledger_run_id,
                    scope_agent_id=self.scope_agent_id,
                ),
                poll_interval=0.01,
                active_tail_timeout=5,
            ).start()
        return self._queue

    def get(self, timeout: float | None = None) -> EventEnvelope:
        return self._ensure_queue().get(timeout=timeout)

    def acknowledge(self, event: EventEnvelope) -> int:
        return self._ensure_queue().acknowledge(event)

    def close(self) -> None:
        if self._queue is not None:
            self._queue.close(timeout=5)


class StopAfterSpawnQueue:
    """Expose the real ledger until one authoritative spawn is consumed."""

    def __init__(self, queue: LedgerQueue) -> None:
        self.queue = queue
        self.stopped = False

    def get(self, timeout: float | None = None) -> EventEnvelope:
        if self.stopped:
            raise queue.Empty
        event = self.queue.get(timeout=timeout)
        if event.kind is EventKind.AGENT_SPAWNED:
            self.stopped = True
        return event

    def acknowledge(self, event: EventEnvelope) -> int:
        return self.queue.acknowledge(event)

    def close(self) -> None:
        self.queue.close(timeout=5)


def run_recursive_watcher_controller(root: Path, repo: Path, swarm_id: str) -> None:
    """Run a real root/sub-TL controller pair for watcher routing coverage."""
    state_root = root / "recursive-watcher-state"
    child_source = LazyLedgerSource(
        repo / ".exo" / "ledger" / "segments",
        state_root / "recursive-watcher-root",
        "sub-a",
        swarm_id,
        None,
    )
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "sub-a",
                WorkPlan(
                    leaves=(
                        LeafTask(
                            "real-watcher-leaf",
                            "file a PR and consume a real watcher approval",
                        ),
                    )
                ),
                source=child_source,
                order=1,
            ),
        )
    )
    try:
        run_tl_loop(
            "recursive-watcher-root",
            plan,
            EmptyEventSource(),
            EffectClient(
                TransportClient(project_root=repo, timeout=5),
                role="tl",
                name="recursive-watcher-root",
            ),
            config=TLLoopConfig(
                active=True,
                enable_reviewer_spawn=True,
                keep_alive_on_waiting=True,
                max_events=128,
                max_parallel_slices=1,
                poll_interval=0.01,
                root_dir=state_root,
                run_id="recursive-watcher-root",
                ledger_run_id=swarm_id,
                branch="main",
                worktree=repo,
                working_dir=str(repo),
            ),
            root_dir=state_root,
        )
    finally:
        child_source.close()


class DispatchBoundaryTransportClient(TransportClient):
    """Pause after durable dispatch state is visible through the real server."""

    def __init__(self, project_root: Path, marker: Path, release: Path) -> None:
        super().__init__(project_root=project_root, timeout=5)
        self.marker = marker
        self.release = release

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        response = super().call_tool(role, name, tool_name, arguments)
        if (
            tool_name == "emit_controller_event"
            and arguments.get("event_type") == "tl.slice_status_changed"
            and not self.marker.exists()
        ):
            self.marker.write_text("dispatch-state-visible\n", encoding="utf-8")
            while not self.release.exists():
                time.sleep(0.01)
        return response


class BaseAdvancingTransportClient(TransportClient):
    """Advance the remote base after the first real watcher snapshot."""

    def __init__(self, project_root: Path, repo: Path) -> None:
        super().__init__(project_root=project_root, timeout=5)
        self.repo = repo
        self.watcher_calls = 0
        self.advanced_pr_numbers: set[int] = set()
        self.base_advanced = False

    def call_tool(
        self,
        role: str,
        name: str,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject:
        if tool_name == "watcher_pr_state":
            self.watcher_calls += 1
        response = super().call_tool(role, name, tool_name, arguments)
        # Two startup-reconciliation snapshots establish the old base.  Move
        # it after the first integration snapshot so the controller's second
        # authoritative read observes the change and records a revalidation.
        if tool_name == "watcher_pr_state" and self.watcher_calls == 3:
            pr_number = arguments.get("pr_number")
            if isinstance(pr_number, int):
                self.advanced_pr_numbers.add(pr_number)
                advance_remote_base(self.repo, len(self.advanced_pr_numbers))
                self.base_advanced = True
        return response


class DelayedAggregateEventSource:
    """Deliver review and CI only after a durable controller restart point."""

    def __init__(
        self,
        run_id: str,
        root_dir: Path,
        *,
        initial_delay: float,
    ) -> None:
        self.store = RunStore(run_id, root_dir)
        self.initial_delay = initial_delay
        self.started_at: float | None = None
        self.emitted: list[str] = []
        self.emitted_sequences: list[int] = []
        self.acknowledged: list[int] = []
        self.observed_aliases: list[Mapping[str, object]] = []

    def get(self, timeout: float | None = None) -> EventEnvelope:
        deadline = None if timeout is None else time.monotonic() + timeout
        if self.started_at is None:
            self.started_at = time.monotonic()
        while True:
            event = self._next_event()
            if event is not None:
                return event
            if deadline is not None and time.monotonic() >= deadline:
                raise queue.Empty
            time.sleep(0.01)

    def acknowledge(self, event: EventEnvelope) -> int:
        if event.run_seq is None:
            raise HarnessError("delayed event has no run_seq")
        self.acknowledged.append(event.run_seq)
        state = self.store.load()
        self.store.checkpoint(
            state.fsm,
            state.slices,
            state.budgets,
            max(state.events.last_consumed_offset, event.run_seq),
            current_order=state.current_order,
            ordered_stages=state.ordered_stages,
            integration=state.integration,
        )
        return event.run_seq

    def _next_event(self) -> EventEnvelope | None:
        if (
            self.started_at is None
            or time.monotonic() - self.started_at < self.initial_delay
        ):
            return None
        state = self.store.load()
        for slice_id in sorted(state.slices):
            current = state.slices[slice_id]
            if current.status is not SliceStatus.IN_REVIEW or current.pr_number is None:
                continue
            head_sha = current.reviewed_head
            if not head_sha:
                continue
            if f"ci:{slice_id}" not in self.emitted:
                event = self._event(
                    state,
                    current,
                    "ci.status_changed",
                    {"status": "success"},
                )
                self.emitted.append("ci:" + slice_id)
                self.emitted_sequences.append(event.run_seq or -1)
                return event
            if current.verdict is None:
                event = self._event(
                    state,
                    current,
                    "pr.review",
                    {
                        "review_state": "approved",
                        "kind": "approved",
                        "findings": [],
                        "patch_digest": current.review_patch_digests.get(
                            head_sha, "seed"
                        ),
                    },
                )
                self.emitted.append("review:" + slice_id)
                self.emitted_sequences.append(event.run_seq or -1)
                return event
            if current.ci_state.get(head_sha) not in {"success", "neutral"}:
                event = self._event(
                    state,
                    current,
                    "ci.status_changed",
                    {"status": "success"},
                )
                self.emitted.append("ci:" + slice_id)
                self.emitted_sequences.append(event.run_seq or -1)
                return event
        return None

    def _event(
        self,
        state: Any,
        current: SliceState,
        event_type: str,
        extra: Mapping[str, object],
    ) -> EventEnvelope:
        data = {
            "agent_id": current.dispatch_agent_id or current.id,
            "owner_id": current.dispatch_agent_id or current.id,
            "branch": current.branch or f"main.{current.id}",
            "pr_number": current.pr_number,
            "head_sha": current.reviewed_head,
            **extra,
        }
        self.observed_aliases.append(data)
        raw = {
            "schema_version": 1,
            "event_id": f"delayed-{state.events.last_consumed_offset + 1}",
            "id": f"delayed-{state.events.last_consumed_offset + 1}",
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_seq": state.events.last_consumed_offset + 1,
            "type": event_type,
            "agent_id": current.dispatch_agent_id or current.id,
            # The controller checkpoint has a local run id, while the ledger
            # stream is scoped to the server's swarm/run UUID.  Real watcher
            # rows carry the latter; using the local id makes the TL silently
            # classify every delayed event as stale during restart recovery.
            "run_id": state.ledger_run_id or state.run_id,
            "session_id": "delayed-e2e",
            "lifecycle_state": "observed",
            "data": data,
        }
        return project(raw)


def run_command(command: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise HarnessError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return run_command(["git", "-C", str(repo), *args])


def agent_worktree(repo: Path, branch: str) -> Path:
    """Map a recursive main.* branch to its hierarchical fixture worktree."""
    prefix = "main."
    if not branch.startswith(prefix):
        raise HarnessError(f"recursive fixture branch has no main.* root: {branch!r}")
    parts = tuple(part for part in branch[len(prefix) :].split(".") if part)
    if not parts:
        raise HarnessError(f"recursive fixture branch has no agent path: {branch!r}")
    return repo / ".exo" / "agents" / Path(*parts)


def create_branch_with_commit(
    repo: Path,
    branch: str,
    base: str,
    *,
    relative_path: str,
    content: str,
    message: str,
) -> str:
    """Create and publish a non-empty fixture branch from an exact base."""
    git(repo, "branch", branch, base)
    worktree = (
        repo.parent
        / f".e2e-1057-commit-{hashlib.sha256(branch.encode()).hexdigest()[:12]}"
    )
    run_command(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), branch]
    )
    try:
        path = worktree / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(worktree, "add", relative_path)
        git(worktree, "commit", "-q", "-m", message)
        git(worktree, "push", "-q", "origin", branch)
        return git(worktree, "rev-parse", "HEAD")
    finally:
        run_command(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)]
        )


def commit_fixture_worktree(
    worktree: Path, *, relative_path: str, content: str, message: str
) -> str:
    """Add one deterministic fixture commit to an already-owned child branch."""
    path = worktree / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(worktree, "add", relative_path)
    git(worktree, "commit", "-q", "-m", message)
    branch = git(worktree, "branch", "--show-current")
    git(worktree, "push", "-q", "origin", branch)
    return git(worktree, "rev-parse", "HEAD")


def advance_remote_base(repo: Path, sequence: int) -> None:
    """Publish one new main commit while a candidate is being revalidated."""
    worktree = repo.parent / f".base-revalidation-{sequence}"
    git(repo, "fetch", "-q", "origin", "main")
    run_command(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            "--detach",
            str(worktree),
            "origin/main",
        ]
    )
    try:
        marker = worktree / "base-revalidation.txt"
        marker.write_text(f"advanced base {sequence}\n", encoding="utf-8")
        git(worktree, "add", marker.name)
        git(worktree, "commit", "-q", "-m", "Advance base for revalidation")
        git(worktree, "push", "-q", "origin", "HEAD:main")
    finally:
        run_command(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "remove",
                "--force",
                str(worktree),
            ]
        )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_request(
    method: str,
    url: str,
    payload: JsonObject | None = None,
    *,
    token: str | None = None,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"token {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise HarnessError(f"Forgejo fixture request failed: {method} {url}") from error


def json_objects(value: Any) -> Iterator[dict[str, Any]]:
    """Yield nested JSON objects, including MCP text-content payloads."""
    if isinstance(value, Mapping):
        object_value = dict(value)
        yield object_value
        for child in object_value.values():
            yield from json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_objects(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            yield from json_objects(json.loads(value))
        except json.JSONDecodeError:
            return


def find_object(result: ToolResult, keys: set[str]) -> dict[str, Any]:
    for candidate in json_objects(result.raw):
        if keys <= candidate.keys():
            return candidate
    raise HarnessError(f"tool result did not contain {sorted(keys)}: {result.raw!r}")


def wait_for_server(
    client: TransportClient, process: subprocess.Popen[str], log: Path
) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HarnessError(
                f"server exited early:\n{log.read_text(encoding='utf-8')}"
            )
        try:
            client.list_tools("tl", "root")
            return
        except TransportError:
            time.sleep(0.25)
    raise HarnessError(
        f"timed out waiting for server:\n{log.read_text(encoding='utf-8')}"
    )


def create_fixture(root: Path) -> tuple[Path, Path, str]:
    remote = root / "remote" / "owner" / "repo.git"
    repo = root / "repo"
    remote.parent.mkdir(parents=True)
    run_command(["git", "init", "--bare", str(remote), "-q"])
    run_command(["git", "init", str(repo), "-q", "-b", "main"])
    git(repo, "remote", "add", "origin", f"file://{remote}")
    git(repo, "config", "user.name", "ordered-server-e2e")
    git(repo, "config", "user.email", "ordered-server-e2e@example.com")
    (repo / "README.md").write_text("ordered server fixture\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".exo/\n", encoding="utf-8")
    git(repo, "add", "README.md", ".gitignore")
    git(repo, "commit", "-m", "Create server fixture", "-q")
    git(repo, "push", "-u", "origin", "main", "-q")
    git(repo, "switch", "-c", "feature/ordered-server", "-q")
    (repo / "server-evidence.txt").write_text("evidence\n", encoding="utf-8")
    git(repo, "add", "server-evidence.txt")
    git(repo, "commit", "-m", "Add server evidence change", "-q")
    git(repo, "push", "-u", "origin", "feature/ordered-server", "-q")
    return repo, remote, "feature/ordered-server"


def clone_external_fixture(root: Path) -> tuple[Path, Path, str]:
    """Clone the dedicated Forgejo repository used by acceptance runs.

    The clone is disposable; the remote must be a repository reserved for the
    acceptance job.  Requiring the explicit remote prevents this harness from
    accidentally publishing test branches into an operator checkout.
    """
    remote_url = os.environ.get("EXOMONAD_FORGEJO_E2E_GIT_REMOTE")
    if not remote_url:
        raise HarnessError(
            "EXOMONAD_FORGEJO_E2E_GIT_REMOTE is required for real acceptance"
        )
    repo = root / "repo"
    run_command(["git", "clone", "--quiet", remote_url, str(repo)])
    git(repo, "config", "user.name", "recursive-crash-e2e")
    git(repo, "config", "user.email", "recursive-crash-e2e@example.com")
    branch = os.environ.get("EXOMONAD_FORGEJO_E2E_BASE_BRANCH", "main")
    git(repo, "switch", "--quiet", branch)
    # The clone is the only local repository involved in the run.  Returning it
    # in both repository slots keeps this helper's fixture-shaped API while
    # avoiding a misleading Path conversion for HTTPS/SSH remotes.
    return repo, repo, branch


def start_mock(
    root: Path, project_root: Path, remote: Path
) -> tuple[subprocess.Popen[str], str]:
    port = free_port()
    stdout = (root / "mock.stdout").open("w", encoding="utf-8")
    stderr = (root / "mock.stderr").open("w", encoding="utf-8")
    environment = {
        **os.environ,
        "REMOTE_DIR": str(remote),
        "MOCK_LOG": str(root / "mock.log"),
    }
    process = subprocess.Popen(
        [
            sys.executable,
            str(project_root / "tests/e2e/mock_github.py"),
            "--port",
            str(port),
        ],
        cwd=project_root,
        env=environment,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            json_request("GET", f"{url}/api/v1/repos/owner/repo/pulls")
            return process, url
        except HarnessError:
            time.sleep(0.1)
    stop_subprocess(process, "mock API")
    raise HarnessError(f"timed out waiting for mock API: {stderr.name}")


def start_server(
    root: Path,
    repo: Path,
    forgejo_url: str,
    project_root: Path,
    *,
    forgejo_token: str = "test-token",
    forgejo_reviewer_token: str | None = None,
    identity_agents: Mapping[str, str] | None = None,
) -> tuple[subprocess.Popen[str], TransportClient]:
    wasm = project_root / ".exo/wasm/wasm-guest-devswarm.wasm"
    binary = Path(
        os.environ.get("EXOMONAD_E2E_BIN", project_root / "target/debug/exomonad")
    )
    if not binary.is_file() or not wasm.is_file():
        raise HarnessError(
            "build target/debug/exomonad and .exo/wasm/wasm-guest-devswarm.wasm first"
        )
    (repo / ".exo/wasm").mkdir(parents=True, exist_ok=True)
    default_branches = {
        "sub-a": "main.sub-a",
        "sub-b": "main.sub-b",
        "sub-c": "main.sub-c",
        "recursive-root": "main.recursive-root",
        "nested": "main.nested",
    }
    default_branches.update(identity_agents or {})
    for branch_name in default_branches.values():
        agent_dir = agent_worktree(repo, branch_name)
        agent_dir.parent.mkdir(parents=True, exist_ok=True)
        parent_branch = (
            branch_name.rsplit(".", 1)[0] if branch_name.count(".") >= 2 else "main"
        )
        run_command(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "-q",
                "-b",
                branch_name,
                str(agent_dir),
                parent_branch,
            ]
        )
        (agent_dir / ".birth_branch").write_text(f"{branch_name}\n", encoding="utf-8")
    parent_worktree = repo / ".exo/worktrees/parent"
    parent_worktree.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "main.parent",
            str(parent_worktree),
            "main",
        ]
    )
    parent_agent_dir = repo / ".exo/agents/parent"
    parent_agent_dir.mkdir(parents=True, exist_ok=True)
    (parent_agent_dir / ".birth_branch").write_text("main.parent\n", encoding="utf-8")
    for agent_id, branch in (identity_agents or {}).items():
        agent_dir = agent_worktree(repo, branch)
        agent_dir.mkdir(parents=True, exist_ok=True)
        worktree = agent_dir
        identity = {
            "agent_name": agent_id,
            "slug": agent_id,
            "agent_type": "codex",
            "birth_branch": branch,
            "parent_branch": branch.rsplit(".", 1)[0] if "." in branch else "main",
            "working_dir": str(worktree),
            "display_name": f"🤖 {agent_id}",
            "topology": "worktree_per_agent",
            "ledger_owned": True,
            "slice_id": agent_id,
        }
        (agent_dir / "identity.json").write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        invocation = {
            "invocation_id": f"1057-seeded-{agent_id}",
            "runtime": "codex",
            "trigger": "spawn",
            "mode": "interactive",
            "routing": {"parent_tab": "TL"},
            "started_at": int(time.time()),
            "status": "running",
            "generation": 1,
            "runtime_agent_id": agent_id,
            "slice_id": agent_id,
            "branch": branch,
            "worktree": str(worktree),
        }
        (agent_dir / "invocation.json").write_text(
            json.dumps(invocation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    shutil.copy2(wasm, repo / ".exo/wasm/wasm-guest-devswarm.wasm")
    port = free_port()
    session = f"ordered-server-e2e-{os.getpid()}-{root.name[-8:]}"
    (repo / ".exo").mkdir(parents=True, exist_ok=True)
    (repo / ".exo" / "e2e-tmux-session").write_text(session + "\\n", encoding="utf-8")
    (repo / ".exo/config.toml").write_text(
        "\n".join(
            [
                'default_role = "tl"',
                'wasm_name = "devswarm"',
                'wasm_dir = ".exo/wasm"',
                'project_dir = "."',
                f'tmux_session = "{session}"',
                f"port = {port}",
                "yolo = true",
                'spawn_agent_type = "codex"',
                f'forgejo_url = "{forgejo_url}"',
                f'forgejo_token = "{forgejo_token}"',
                f'forgejo_reviewer_token = "{forgejo_reviewer_token or forgejo_token}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
    fake_codex.chmod(0o755)
    test_path = f"{fake_bin}:{os.environ.get('PATH', '')}"
    run_command(
        ["tmux", "new-session", "-d", "-s", session, "-n", "TL", "sleep", "300"]
    )
    run_command(["tmux", "set-environment", "-t", session, "PATH", test_path])
    log = (root / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(binary), "serve"],
        cwd=repo,
        env={**os.environ, "PATH": test_path},
        stdout=log,
        stderr=log,
        text=True,
    )
    client = TransportClient(project_root=repo, timeout=5)
    try:
        wait_for_server(client, process, Path(log.name))
    except BaseException:
        stop_server(process, repo, "ExoMonad server startup")
        raise
    return process, client


def server_run_id(repo: Path) -> str:
    """Return the immutable swarm UUID written by the running server."""
    path = repo / ".exo" / "run_id"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value and value != "root":
                return value
        time.sleep(0.05)
    raise HarnessError(f"server did not write a swarm UUID to {path}")


def stop_server(process: subprocess.Popen[str], repo: Path, label: str) -> None:
    """Stop the server and remove the tmux session it owns."""
    stop_subprocess(process, label)
    session_path = repo / ".exo" / "e2e-tmux-session"
    try:
        session = session_path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if session:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            capture_output=True,
            text=True,
        )


def cleanup_external_case(
    repo: Path,
    forgejo_url: str,
    forgejo_owner: str,
    forgejo_repo: str,
    forgejo_token: str,
    case_name: str,
) -> None:
    """Close and delete only PRs/branches created by one acceptance case."""
    pulls = json_request(
        "GET",
        f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls?state=all&limit=100",
        token=forgejo_token,
    )
    if not isinstance(pulls, list):
        raise HarnessError(f"Forgejo pull listing is not an array: {pulls!r}")
    branch_prefix = f"aggregate/{case_name}/"
    for pull in pulls:
        if not isinstance(pull, Mapping):
            continue
        head = pull.get("head")
        head_ref = head.get("ref") if isinstance(head, Mapping) else None
        title = pull.get("title")
        belongs = isinstance(head_ref, str) and head_ref.startswith(branch_prefix)
        belongs = belongs or (
            isinstance(title, str)
            and title.startswith("Aggregate ")
            and isinstance(head_ref, str)
            and head_ref
            in {
                "main.sub-a",
                "main.sub-b",
                "main.sub-c",
                "main.sub-a.nested-a",
            }
        )
        if not belongs:
            continue
        number = pull.get("number")
        if type(number) is int and pull.get("state") == "open":
            json_request(
                "PATCH",
                f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls/{number}",
                {"state": "closed"},
                token=forgejo_token,
            )
        if isinstance(head_ref, str):
            result = subprocess.run(
                ["git", "push", "origin", "--delete", head_ref],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode and "remote ref does not exist" not in result.stderr:
                raise HarnessError(
                    f"could not delete acceptance branch {head_ref!r}: {result.stderr}"
                )
    parent_marker = repo / ".exo" / f"1057-parent-branches-{case_name}.json"
    try:
        parent_branches = json.loads(parent_marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        parent_branches = []
    if isinstance(parent_branches, list):
        for branch in parent_branches:
            if not isinstance(branch, str) or not branch:
                continue
            result = subprocess.run(
                ["git", "push", "origin", "--delete", branch],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode and "remote ref does not exist" not in result.stderr:
                raise HarnessError(
                    f"could not delete acceptance parent branch {branch!r}: {result.stderr}"
                )
    try:
        parent_marker.unlink()
    except FileNotFoundError:
        pass


def server_ledger_events(repo: Path) -> list[dict[str, Any]]:
    """Read committed server events without projecting local checkpoint IDs."""
    events: list[dict[str, Any]] = []
    segments = repo / ".exo" / "ledger" / "segments"
    for path in sorted(segments.glob("*")):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
    return events


def reviewer_spawn_events(
    repo: Path, swarm_id: str, pr_number: int
) -> dict[int, Mapping[str, Any]]:
    """Return unique reviewer subtree spawns for one PR."""
    events: dict[int, Mapping[str, Any]] = {}
    for event in server_ledger_events(repo):
        data = event.get("data")
        branch = data.get("branch") if isinstance(data, Mapping) else None
        if (
            event.get("run_id") == swarm_id
            and event.get("type") == "agent.spawned"
            and isinstance(branch, str)
            and (
                branch == f"review-pr-{pr_number}"
                or branch.endswith(f".review-pr-{pr_number}")
                or branch.startswith(f"review-pr-{pr_number}-")
            )
        ):
            run_seq = event.get("run_seq")
            if type(run_seq) is int:
                events[run_seq] = event
    return events


def stop_multiprocessing_process(
    process: multiprocessing.Process,
    label: str,
    timeout: float = 5,
    *,
    process_group: bool = False,
) -> None:
    """Terminate, reap, and kill a child process that ignores termination."""
    if process.is_alive():
        if process_group:
            try:
                process_group_id = os.getpgid(process.pid)
                if process_group_id == process.pid:
                    os.killpg(process_group_id, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
        else:
            process.terminate()
    process.join(timeout=timeout)
    if process.is_alive():
        if process_group:
            try:
                process_group_id = os.getpgid(process.pid)
                if process_group_id == process.pid:
                    os.killpg(process_group_id, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.join(timeout=timeout)
    if process.is_alive():
        raise HarnessError(
            f"{label} did not stop after terminate/kill fallback: "
            f"exitcode={process.exitcode}"
        )


def stop_subprocess(
    process: subprocess.Popen[str], label: str, timeout: float = 10
) -> None:
    """Terminate, reap, and kill a subprocess with bounded cleanup."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
    if process.poll() is None:
        raise HarnessError(f"{label} did not stop after terminate/kill fallback")


def assert_controller_alive(process: multiprocessing.Process, phase: str) -> None:
    """Fail the probe if the controller exited before the lifecycle was complete."""
    if process.is_alive():
        return
    process.join(timeout=1)
    raise HarnessError(
        f"recursive controller exited during {phase}: exitcode={process.exitcode}"
    )


def stop_spawned_worker(repo: Path, worker_name: str) -> None:
    """Stop only the temporary worker window created by this probe."""
    config = (repo / ".exo" / "config.toml").read_text(encoding="utf-8")
    session = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in config.splitlines()
        if line.startswith("tmux_session =")
    )
    try:
        windows = run_command(
            [
                "tmux",
                "list-panes",
                "-t",
                session,
                "-F",
                "#{window_id}\t#{pane_id}\t#{pane_title}\t#{pane_current_command}\t#{pane_start_command}",
            ]
        )
    except HarnessError as error:
        if "can't find session" in str(error):
            return
        raise
    window_ids = {
        line.split("\t", 1)[0] for line in windows.splitlines() if worker_name in line
    }
    for window_id in sorted(window_ids):
        run_command(["tmux", "kill-window", "-t", window_id])
    remaining = run_command(
        [
            "tmux",
            "list-panes",
            "-t",
            session,
            "-a",
            "-F",
            "#{pane_title}\t#{pane_current_command}\t#{pane_start_command}",
        ]
    )
    if any(worker_name in line for line in remaining.splitlines()):
        raise HarnessError(f"temporary worker window survived cleanup: {worker_name}")


def best_effort_worker_cleanup(
    repo: Path, worker_name: str, diagnostics: list[str]
) -> None:
    """Dispose one probe-owned worker without masking another cleanup attempt."""
    try:
        stop_spawned_worker(repo, worker_name)
    except Exception as error:  # noqa: BLE001 - cleanup must continue for every error
        diagnostics.append(f"{worker_name}: {error}")


def cleanup_recursive_watcher_probe(
    repo: Path,
    swarm_id: str,
    root_process: multiprocessing.Process,
    cleanup_state: Mapping[str, Any],
) -> None:
    """Converge the controller and every worker known to the routing probe."""
    diagnostics: list[str] = []
    try:
        stop_multiprocessing_process(root_process, "recursive watcher controller")
    except Exception as error:  # noqa: BLE001 - cleanup must continue for every error
        diagnostics.append(str(error))

    worker_names: set[str] = set()
    owner_id = cleanup_state.get("owner_id")
    if isinstance(owner_id, str) and owner_id:
        worker_names.add(owner_id)
    pr_number = cleanup_state.get("pr_number")
    if isinstance(pr_number, int):
        for event in reviewer_spawn_events(repo, swarm_id, pr_number).values():
            data = event.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("child_agent"), str):
                worker_names.add(str(data["child_agent"]))
    for worker_name in sorted(worker_names):
        best_effort_worker_cleanup(repo, worker_name, diagnostics)
    if diagnostics:
        raise HarnessError(
            "recursive watcher routing cleanup failed: " + "; ".join(diagnostics)
        )


def run_root_recursive_lifecycle_probe(
    client: TransportClient, root: Path, repo: Path
) -> str:
    """Consume a real UUID-stamped spawn while keeping the checkpoint ID root."""
    swarm_id = server_run_id(repo)
    state_root = root / "lifecycle-state"
    worker_name = "lifecycle-root-child"
    reader = LedgerReader(
        repo / ".exo" / "ledger" / "segments",
        run_id="root",
        state_root=state_root,
        ledger_run_id=swarm_id,
    )
    source = StopAfterSpawnQueue(
        LedgerQueue(reader, poll_interval=0.01, active_tail_timeout=5).start()
    )
    try:
        result = run_tl_loop(
            "root",
            WorkPlan(leaves=(LeafTask(worker_name, "lifecycle correlation probe"),)),
            source,
            EffectClient(client, role="tl", name="root"),
            config=TLLoopConfig(
                active=True,
                keep_alive_on_waiting=False,
                # StopAfterSpawnQueue intentionally exposes one authoritative
                # event; a one-event budget prevents the controller from
                # spinning on the closed queue while the spawned pane is
                # being cleaned up.
                max_events=1,
                poll_interval=0.01,
                root_dir=state_root,
                run_id="root",
                ledger_run_id=swarm_id,
                branch="main",
                working_dir=str(repo),
                worktree=repo,
            ),
            root_dir=state_root,
        )
        state = RunStore("root", state_root).load()
        child = state.slices[worker_name]
        if child.status is not SliceStatus.SPAWNED or not child.branch:
            raise HarnessError(f"root spawn was not confirmed: {child!r}")
        if (
            state.ledger_run_id != swarm_id
            or child.dispatch_authoritative_event_seq is None
            or state.events.last_consumed_offset
            < child.dispatch_authoritative_event_seq
        ):
            raise HarnessError(
                f"root checkpoint lost authoritative identity: state={state!r}"
            )
        if result.diagnostics.get("correlated") != 1:
            raise HarnessError(
                f"root spawn was not correlated exactly once: {result.diagnostics}"
            )
        spawn_events = [
            event
            for event in server_ledger_events(repo)
            if event.get("type") == "agent.spawned"
            and event.get("run_id") == swarm_id
            and isinstance(event.get("data"), dict)
            and event["data"].get("intent_id") == child.dispatch_intent_id
        ]
        authoritative_events = [
            event
            for event in spawn_events
            if isinstance(event.get("data"), dict)
            and event["data"].get("spawn_type") == "leaf_subtree"
            and event["data"].get("branch")
        ]
        if len(authoritative_events) != 1 or set(state.slices) != {worker_name}:
            raise HarnessError(
                f"expected one canonical UUID-scoped spawn event for {worker_name}: "
                f"events={spawn_events!r} slices={state.slices!r}"
            )
        checkpoint = state_root / "root" / "run.json"
        if not checkpoint.is_file() or checkpoint.parent.name == swarm_id:
            raise HarnessError(
                f"root checkpoint path was not local and stable: {checkpoint}"
            )
        return swarm_id
    finally:
        try:
            source.close()
        finally:
            diagnostics: list[str] = []
            best_effort_worker_cleanup(repo, worker_name, diagnostics)
            if diagnostics:
                raise HarnessError(
                    "root probe cleanup failed: " + "; ".join(diagnostics)
                )


def run_real_watcher_routing_probe(
    client: TransportClient,
    root: Path,
    repo: Path,
    forgejo_url: str,
    swarm_id: str,
) -> None:
    """Route recursive Rust watcher events through the real Python controller."""
    root_process = multiprocessing.get_context("fork").Process(
        target=run_recursive_watcher_controller,
        args=(root, repo, swarm_id),
        name="real-recursive-watcher-controller",
    )
    cleanup_state: dict[str, Any] = {"owner_id": None, "pr_number": None}
    root_process.start()
    try:
        _run_real_watcher_routing_probe_body(
            client,
            root,
            repo,
            forgejo_url,
            swarm_id,
            root_process,
            cleanup_state,
        )
    finally:
        cleanup_recursive_watcher_probe(repo, swarm_id, root_process, cleanup_state)


def _probe_dispatch_phase(
    root_process: multiprocessing.Process,
    child_state_root: Path,
    slice_id: str,
) -> str:
    """Wait for the recursive controller to durably spawn the leaf; return its owner id."""
    owner_id: str | None = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert_controller_alive(root_process, "leaf dispatch")
        try:
            child = RunStore("sub-a", child_state_root).load().slices[slice_id]
        except (FileNotFoundError, KeyError, ValueError):
            time.sleep(0.1)
            continue
        if child.status is SliceStatus.SPAWNED and child.dispatch_agent_id:
            owner_id = child.dispatch_agent_id
            break
        time.sleep(0.1)
    if owner_id is None:
        raise HarnessError(
            "recursive controller did not durably spawn the watcher-routing leaf"
        )
    return owner_id


def _probe_publication_phase(
    client: TransportClient,
    forgejo_url: str,
    owner_id: str,
    cleanup_state: dict[str, Any],
) -> tuple[int, str, str]:
    """File and approve the leaf's PR; return (pr_number, branch, head_sha).

    cleanup_state["pr_number"] is recorded the moment the PR number is known
    -- immediately after file_pr returns -- not after this whole phase
    returns to its caller. A real PR (and any reviewer the watcher spawns in
    response to it) already exists at that point; if watcher_pr_state or the
    approval POST below raises, the finally-block cleanup must still be able
    to find and dispose that reviewer instead of leaking it (chainlink #908).
    """
    owner_effects = EffectClient(client, role="tl", name=owner_id)
    filed = owner_effects.file_pr(
        title="Real recursive watcher routing",
        body="TL-Slice-ID: wrong-leaf\nThe server must ignore this leaf-controlled tag",
        base_branch="main.sub-a",
    )
    filed_data = find_object(filed, {"pr_number", "head_branch"})
    pr_number = int(filed_data["pr_number"])
    branch = str(filed_data["head_branch"])
    cleanup_state["pr_number"] = pr_number
    snapshot = owner_effects.watcher_pr_state(pr_number=pr_number)
    evidence = find_object(snapshot, {"head_sha"})
    head_sha = str(evidence["head_sha"])
    json_request(
        "POST",
        f"{forgejo_url}/api/v1/repos/owner/repo/pulls/{pr_number}/reviews",
        {"event": "APPROVED", "commit_id": head_sha},
    )
    return pr_number, branch, head_sha


def _probe_watcher_delivery_phase(
    root_process: multiprocessing.Process,
    repo: Path,
    swarm_id: str,
    pr_number: int,
    owner_id: str,
    slice_id: str,
    branch: str,
    head_sha: str,
) -> int:
    """Wait for filed/approval/CI events and one reviewer spawn; return the expected cursor."""
    deadline = time.monotonic() + 30
    filed_event: Mapping[str, Any] | None = None
    watcher_event: Mapping[str, Any] | None = None
    ci_event: Mapping[str, Any] | None = None
    reviewer_events: list[Mapping[str, Any]] = []
    while time.monotonic() < deadline:
        assert_controller_alive(root_process, "watcher event delivery")
        for event in server_ledger_events(repo):
            data = event.get("data")
            if (
                event.get("run_id") == swarm_id
                and isinstance(data, Mapping)
                and data.get("pr_number") == pr_number
            ):
                if event.get("type") in {"pr.filed", "pr.updated"}:
                    filed_event = event
                if event.get("type") == "copilot.review":
                    watcher_event = event
                if event.get("type") == "ci.status_changed":
                    ci_event = event
        reviewer_events = list(
            reviewer_spawn_events(repo, swarm_id, pr_number).values()
        )
        if (
            filed_event is not None
            and watcher_event is not None
            and ci_event is not None
            and reviewer_events
        ):
            break
        time.sleep(0.1)
    if (
        filed_event is None
        or watcher_event is None
        or ci_event is None
        or len(reviewer_events) != 1
    ):
        raise HarnessError(
            "recursive watcher did not publish file_pr, approval, CI, and one reviewer spawn: "
            f"filed={filed_event!r} watcher={watcher_event!r} "
            f"ci={ci_event!r} reviewers={reviewer_events!r}"
        )
    watcher_data = watcher_event.get("data")
    if (
        not isinstance(watcher_data, Mapping)
        or watcher_data.get("owner_id") != owner_id
    ):
        raise HarnessError(f"watcher ownership was not canonical: {watcher_event!r}")
    if watcher_data.get("slice_id") != slice_id or watcher_data.get("branch") != branch:
        raise HarnessError(
            f"watcher omitted proven slice/branch identity: {watcher_event!r}"
        )
    ci_data = ci_event.get("data")
    if (
        not isinstance(ci_data, Mapping)
        or ci_data.get("owner_id") != owner_id
        or ci_data.get("slice_id") != slice_id
        or ci_data.get("branch") != branch
        or ci_data.get("head_sha") != head_sha
        or ci_data.get("status") != "success"
    ):
        raise HarnessError(
            f"CI event omitted canonical ownership evidence: {ci_event!r}"
        )

    filed_seq = filed_event.get("run_seq")
    watcher_seq = watcher_event.get("run_seq")
    ci_seq = ci_event.get("run_seq")
    if (
        type(filed_seq) is not int
        or type(watcher_seq) is not int
        or type(ci_seq) is not int
        or watcher_seq <= filed_seq
        or ci_seq <= filed_seq
    ):
        raise HarnessError(
            f"watcher event ordering was not durable: filed={filed_event!r} "
            f"review={watcher_event!r} ci={ci_event!r}"
        )
    return max(watcher_seq, ci_seq)


def _probe_checkpoint_phase(
    root_process: multiprocessing.Process,
    child_state_root: Path,
    slice_id: str,
    swarm_id: str,
    pr_number: int,
    head_sha: str,
    expected_cursor: int,
) -> None:
    """Wait for the child controller to durably persist approval and CI."""
    child_checkpoint = child_state_root / "sub-a" / "run.json"
    checkpoint_deadline = time.monotonic() + 30
    child = None
    while time.monotonic() < checkpoint_deadline:
        assert_controller_alive(root_process, "recursive checkpoint persistence")
        try:
            candidate = RunStore("sub-a", child_state_root).load()
        except CorruptCheckpoint:
            time.sleep(0.1)
            continue
        if not child_checkpoint.is_file() or candidate.ledger_run_id != swarm_id:
            time.sleep(0.1)
            continue
        current = candidate.slices.get(slice_id)
        if (
            current is not None
            and current.pr_number == pr_number
            and current.reviewed_head == head_sha
            and candidate.events.last_consumed_offset >= expected_cursor
            and current.ci_state.get(head_sha) == "success"
            and current.verdict in {Verdict.GO, Verdict.GO_WITH_NITS}
            and current.reviewer_attempt.get(head_sha) == 1
            and len(current.reviewer_attempt) == 1
        ):
            child = candidate
            break
        time.sleep(0.1)
    if child is None:
        raise HarnessError(
            "recursive controller did not durably consume approval and CI: "
            f"checkpoint={child_checkpoint} expected_cursor={expected_cursor}"
        )
    current = child.slices[slice_id]
    if current.pr_number != pr_number or current.reviewed_head != head_sha:
        raise HarnessError(
            f"recursive controller did not route the PR to its leaf: {current!r}"
        )
    if child.events.last_consumed_offset < expected_cursor:
        raise HarnessError(
            "recursive controller did not consume the real watcher events: "
            f"cursor={child.events.last_consumed_offset} expected_cursor={expected_cursor}"
        )
    if current.ci_state.get(head_sha) != "success":
        raise HarnessError(
            f"recursive controller did not persist watcher CI: {current!r}"
        )
    if current.verdict not in {Verdict.GO, Verdict.GO_WITH_NITS}:
        raise HarnessError(
            f"recursive controller did not persist watcher approval: {current!r}"
        )
    attempts = current.reviewer_attempt
    if attempts.get(head_sha) != 1 or len(attempts) != 1:
        raise HarnessError(
            f"reviewer claim was not persisted exactly once: {current!r}"
        )


def _probe_stabilization_phase(
    root_process: multiprocessing.Process,
    repo: Path,
    swarm_id: str,
    pr_number: int,
) -> None:
    """Confirm the reviewer spawn stabilizes at exactly one event, uniquely owned."""
    stabilization_deadline = time.monotonic() + 3
    while time.monotonic() < stabilization_deadline:
        assert_controller_alive(root_process, "reviewer stabilization")
        reviewer_events = list(
            reviewer_spawn_events(repo, swarm_id, pr_number).values()
        )
        if len(reviewer_events) > 1:
            raise HarnessError(
                f"delayed duplicate reviewer spawn observed: {reviewer_events!r}"
            )
        time.sleep(0.1)
    reviewer_events = list(reviewer_spawn_events(repo, swarm_id, pr_number).values())
    if len(reviewer_events) != 1:
        raise HarnessError(
            f"reviewer spawn did not stabilize at exactly one event: {reviewer_events!r}"
        )
    reviewer_agents = {
        data.get("child_agent")
        for reviewer in reviewer_events
        for data in [reviewer.get("data")]
        if isinstance(data, Mapping) and isinstance(data.get("child_agent"), str)
    }
    if len(reviewer_agents) != 1:
        raise HarnessError(f"reviewer ownership was not unique: {reviewer_events!r}")


def _run_real_watcher_routing_probe_body(
    client: TransportClient,
    root: Path,
    repo: Path,
    forgejo_url: str,
    swarm_id: str,
    root_process: multiprocessing.Process,
    cleanup_state: dict[str, Any],
) -> None:
    """Route recursive Rust watcher events through the real Python controller.

    Composed of five independently testable phases — dispatch, publication,
    watcher_delivery, checkpoint, stabilization — matching the boundaries
    tests/e2e/ordered-recursive/test_real_server_transport_cleanup.py injects
    failures at. cleanup_state is updated as soon as each identity becomes
    known so cleanup_recursive_watcher_probe can dispose it regardless of
    which later phase fails.
    """
    state_root = root / "recursive-watcher-state"
    slice_id = "real-watcher-leaf"
    child_state_root = state_root / "recursive-watcher-root"

    owner_id = _probe_dispatch_phase(root_process, child_state_root, slice_id)
    cleanup_state["owner_id"] = owner_id

    pr_number, branch, head_sha = _probe_publication_phase(
        client, forgejo_url, owner_id, cleanup_state
    )

    expected_cursor = _probe_watcher_delivery_phase(
        root_process, repo, swarm_id, pr_number, owner_id, slice_id, branch, head_sha
    )

    _probe_checkpoint_phase(
        root_process,
        child_state_root,
        slice_id,
        swarm_id,
        pr_number,
        head_sha,
        expected_cursor,
    )

    _probe_stabilization_phase(root_process, repo, swarm_id, pr_number)


def _run_recursive_checkpoint_controller(
    root: Path,
    repo: Path,
    swarm_id: str,
    cancel_event: Any,
    exercise_recovery: bool,
) -> None:
    """Run the nested controller until the probe observes its live boundary."""
    state_root = root / "lifecycle-state"
    sub_tl_source = LazyLedgerSource(
        repo / ".exo" / "ledger" / "segments",
        state_root / "recursive-root",
        "sub-a",
        swarm_id,
        None,
    )
    plan = WorkPlan(
        sub_tls=(
            SubTLTask(
                "sub-a",
                WorkPlan(
                    leaves=(
                        LeafTask("nested-leaf", "recursive spawn correlation probe"),
                    ),
                    sub_tls=(SubTLTask("nested", WorkPlan(), order=1),),
                ),
                source=sub_tl_source,
                order=1,
            ),
            SubTLTask("sub-b", WorkPlan(), order=1),
        )
    )
    try:
        run_tl_loop(
            "recursive-root",
            plan,
            EmptyEventSource(),
            EffectClient(
                TransportClient(project_root=repo, timeout=5),
                role="tl",
                name="recursive-root",
            ),
            config=TLLoopConfig(
                active=True,
                keep_alive_on_waiting=False,
                max_parallel_slices=2,
                max_events=16,
                root_dir=state_root,
                run_id="recursive-root",
                ledger_run_id=swarm_id,
                branch="main",
                worktree=state_root / "recursive-owner",
                working_dir=str(repo),
                project_root=repo if exercise_recovery else None,
                heartbeat=(
                    HeartbeatConfig(
                        interval_seconds=0.05,
                        stall_threshold_seconds=300.0,
                        task_timeout_seconds=None,
                    )
                    if exercise_recovery
                    else None
                ),
                cancel_event=cancel_event,
            ),
            root_dir=state_root,
        )
    except Exception as error:  # noqa: BLE001 - cancellation is the probe boundary
        if not cancel_event.is_set():
            (state_root / "recursive-controller-error.txt").write_text(
                str(error), encoding="utf-8"
            )
    finally:
        sub_tl_source.close()


def _drive_nested_recovery(
    client: TransportClient,
    repo: Path,
    state_root: Path,
    attempt: int,
    trace: RecoveryTrace,
) -> None:
    blocked = EffectClient(client, role="tl", name="root").emit_controller_event(
        event_type="agent.task_blocked",
        payload={
            "outcome": "blocked",
            "slice_id": "nested-leaf",
            "cause": "base_ci_unstable",
            "scope_attribution": "base",
            "needs_human": False,
            "retryable": True,
            "recovery_action": "probe base CI before same-owner resume",
            "declared_difficulty": "standard",
            "matched_difficulty_rule": "standard_slice",
            "attempt": attempt,
        },
    )
    if not blocked.success:
        raise HarnessError(f"nested blocked evidence was rejected: {blocked.raw!r}")
    recovery_deadline = time.monotonic() + 60
    previous_phase: str | None = None
    initial_generation: int | None = None
    while time.monotonic() < recovery_deadline:
        try:
            nested_state = RunStore("sub-a", state_root / "recursive-root").load()
            nested_slice = nested_state.slices["nested-leaf"]
            recovery = nested_slice.recovery
            invocation = _read_nested_invocation(repo, nested_slice)
            if invocation is not None and type(invocation.get("generation")) is int:
                initial_generation = (
                    invocation["generation"]
                    if initial_generation is None
                    else initial_generation
                )
            if recovery is not None and recovery.phase.value != previous_phase:
                trace.record(
                    boundary="nested_recovery",
                    point=recovery.phase.value,
                    run_id="sub-a",
                    state_root=state_root / "recursive-root",
                    repo=repo,
                    invocation_snapshot=invocation,
                )
                previous_phase = recovery.phase.value
            resume_events = [
                event
                for event in server_ledger_events(repo)
                if event.get("type") == "agent.recovery.outcome"
                and event.get("run_id") == nested_state.ledger_run_id
                and isinstance(event.get("data"), Mapping)
                and event["data"].get("slice_id") == "nested-leaf"
                and event["data"].get("outcome") == "recovered"
            ]
            resumed_invocation = (
                invocation
                if invocation is not None
                and type(invocation.get("generation")) is int
                and initial_generation is not None
                and invocation["generation"] > initial_generation
                and invocation.get("status") == "running"
                and invocation.get("slice_id") == "nested-leaf"
                else None
            )
            if resume_events and resumed_invocation is not None:
                trace.record(
                    boundary="nested_recovery",
                    point="resume_confirmed",
                    run_id="sub-a",
                    state_root=state_root / "recursive-root",
                    repo=repo,
                    resume_confirmation=resume_events[-1],
                    resume_invocation=resumed_invocation,
                )
                return
        except (CorruptCheckpoint, FileNotFoundError, KeyError, ValueError):
            pass
        time.sleep(0.1)
    raise HarnessError(
        "nested controller did not persist recovery through an authoritative "
        "same-owner resume confirmation: "
        f"{trace.records!r}"
    )


def _read_nested_invocation(repo: Path, slice_state: Any) -> dict[str, Any] | None:
    """Read the current invocation from the persisted runtime-agent identity."""
    agent_id = slice_state.dispatch_agent_id
    if not isinstance(agent_id, str) or not agent_id:
        return None
    path = repo / ".exo" / "agents" / agent_id / "invocation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def run_recursive_checkpoint_probe(
    client: TransportClient,
    root: Path,
    repo: Path,
    swarm_id: str,
    *,
    exercise_recovery: bool = False,
) -> RecoveryTrace | None:
    """Observe nested live controllers before cancelling the disposable probe."""
    state_root = root / "lifecycle-state"
    trace = (
        RecoveryTrace.open(root / "recursive-recovery-trace.json")
        if exercise_recovery
        else None
    )
    context = multiprocessing.get_context("fork")
    cancel_event = context.Event()
    controller = context.Process(
        target=_run_recursive_checkpoint_controller,
        args=(root, repo, swarm_id, cancel_event, exercise_recovery),
        name="recursive-checkpoint-controller",
    )
    controller.start()
    try:
        deadline = time.monotonic() + 30
        observed = False
        while time.monotonic() < deadline:
            if not controller.is_alive():
                break
            try:
                root_state = RunStore("recursive-root", state_root).load()
                sub_a_state = RunStore("sub-a", state_root / "recursive-root").load()
                nested_leaf = sub_a_state.slices["nested-leaf"]
            except (FileNotFoundError, KeyError, CorruptCheckpoint, ValueError):
                time.sleep(0.1)
                continue
            parent = root_state.slices.get("sub-a")
            if (
                parent is not None
                and parent.status is SliceStatus.SPAWNED
                and nested_leaf.status is SliceStatus.SPAWNED
                and nested_leaf.dispatch_authoritative_event_seq is not None
                and sub_a_state.ledger_run_id == swarm_id
            ):
                observed = True
                break
            time.sleep(0.1)
        if not observed:
            error_path = state_root / "recursive-controller-error.txt"
            detail = (
                error_path.read_text(encoding="utf-8") if error_path.is_file() else ""
            )
            raise HarnessError(
                "recursive controller did not expose a live nested spawn boundary"
                + (f": {detail}" if detail else "")
            )
        if not controller.is_alive():
            raise HarnessError("recursive controller exited before the nested boundary")
        if exercise_recovery:
            assert trace is not None
            _drive_nested_recovery(
                client, repo, state_root, nested_leaf.attempts, trace
            )
    finally:
        cancel_event.set()
        controller.join(timeout=10)
        if controller.is_alive():
            controller.terminate()
            controller.join(timeout=5)
        diagnostics: list[str] = []
        best_effort_worker_cleanup(repo, "nested-leaf", diagnostics)
        if diagnostics:
            raise HarnessError(
                "recursive checkpoint cleanup failed: " + "; ".join(diagnostics)
            )

    local_runs = (
        ("recursive-root", state_root),
        ("sub-a", state_root / "recursive-root"),
        ("nested", state_root / "recursive-root" / "sub-a"),
        ("sub-b", state_root / "recursive-root"),
    )
    for local_id, parent_root in local_runs:
        path = parent_root / local_id / "run.json"
        state = RunStore(local_id, parent_root).load()
        if (
            not path.is_file()
            or state.ledger_run_id != swarm_id
            or local_id == swarm_id
        ):
            raise HarnessError(
                f"recursive checkpoint identity was not isolated for {local_id}: {path}"
            )
    sub_a_state = RunStore("sub-a", state_root / "recursive-root").load()
    nested_leaf = sub_a_state.slices["nested-leaf"]
    if (
        nested_leaf.status is not SliceStatus.SPAWNED
        or nested_leaf.dispatch_authoritative_event_seq is None
        or sub_a_state.ledger_run_id != swarm_id
    ):
        raise HarnessError(
            f"recursive sub-TL spawn was not authoritatively correlated: {nested_leaf!r}"
        )
    recursive_spawn_events = [
        event
        for event in server_ledger_events(repo)
        if event.get("type") == "agent.spawned"
        and event.get("run_id") == swarm_id
        and isinstance(event.get("data"), dict)
        and event["data"].get("intent_id") == nested_leaf.dispatch_intent_id
        and event["data"].get("spawn_type") == "leaf_subtree"
        and event["data"].get("branch")
    ]
    if len(recursive_spawn_events) != 1:
        raise HarnessError(
            f"expected one recursive canonical spawn event: {recursive_spawn_events!r}"
        )
    assert_stage_events(repo, swarm_id)
    return trace


def check_pr_evidence(
    client: TransportClient, branch: str, repo: Path, forgejo_url: str
) -> None:
    owner_effects = EffectClient(client, role="tl", name="sub-a")
    parent_effects = EffectClient(client, role="tl", name="parent")
    merge_worktree = repo.parent / "merge-parent"
    run_command(
        [
            "git",
            "clone",
            "-q",
            git(repo, "remote", "get-url", "origin"),
            str(merge_worktree),
        ]
    )
    filed = owner_effects.file_pr(
        title="Ordered server evidence",
        body="TransportClient acceptance fixture",
        base_branch="main",
    )
    filed_data = find_object(filed, {"pr_number"})
    pr_number = int(filed_data["pr_number"])
    snapshot = parent_effects.watcher_pr_state(pr_number=pr_number)
    evidence_keys = {"head_sha", "base_sha", "patch_digest", "merge_tree_sha"}
    evidence = find_object(snapshot, evidence_keys)
    if any(
        not isinstance(evidence[key], str) or not evidence[key] for key in evidence_keys
    ):
        raise HarnessError(f"watcher returned incomplete evidence: {evidence!r}")
    if evidence.get("head_branch") != branch:
        raise HarnessError(f"watcher branch mismatch: {evidence!r}")
    json_request(
        "POST",
        f"{forgejo_url}/api/v1/repos/owner/repo/pulls/{pr_number}/reviews",
        {"event": "APPROVED", "commit_id": evidence["head_sha"]},
    )
    stale = parent_effects.merge_pr(
        pr_number=pr_number,
        strategy="merge",
        expected_base_sha=evidence["base_sha"],
        expected_head_sha=evidence["head_sha"],
        expected_patch_digest="stale-patch-digest",
        expected_merge_tree_sha=evidence["merge_tree_sha"],
        working_dir=str(merge_worktree),
    )
    if not any(
        candidate.get("success") is False for candidate in json_objects(stale.raw)
    ):
        raise HarnessError("merge handler accepted a stale patch digest")
    merged = parent_effects.merge_pr(
        pr_number=pr_number,
        strategy="merge",
        expected_base_sha=evidence["base_sha"],
        expected_head_sha=evidence["head_sha"],
        expected_patch_digest=evidence["patch_digest"],
        expected_merge_tree_sha=evidence["merge_tree_sha"],
        working_dir=str(merge_worktree),
    )
    if not any(
        candidate.get("success") is True for candidate in json_objects(merged.raw)
    ):
        raise HarnessError(f"merge handler rejected matching evidence: {merged.raw!r}")


def run_live_ordered_probe(
    client: TransportClient, root: Path, repo: Path, swarm_id: str
) -> None:
    effects = EffectClient(client, role="tl", name="root")
    result = run_tl_loop(
        "ordered-server-live",
        WorkPlan(
            sub_tls=(
                SubTLTask("sub-a", WorkPlan(), order=1),
                SubTLTask("sub-b", WorkPlan(), order=1),
            )
        ),
        EmptyEventSource(),
        effects,
        config=TLLoopConfig(
            active=True,
            keep_alive_on_waiting=False,
            max_parallel_slices=2,
            max_events=8,
            root_dir=root / "controller-state",
            run_id="ordered-server-live",
            ledger_run_id=swarm_id,
            branch="main",
            worktree=repo,
        ),
        root_dir=root / "controller-state",
    )
    if result.final_state.fsm.phase is not TLPhase.TLDone:
        raise HarnessError(
            f"live ordered run did not finish: {result.final_state.fsm.phase!r}"
        )
    if {state.status for state in result.final_state.slices.values()} != {
        SliceStatus.MERGED
    }:
        raise HarnessError(
            f"live child ownership did not complete: {result.final_state.slices!r}"
        )
    state_root = root / "controller-state"
    for name in ("sub-a", "sub-b"):
        child = RunStore(name, state_root / "ordered-server-live").load()
        if child.owner_branch != f"main.{name}" or not child.owner_worktree:
            raise HarnessError(
                f"child owner record is incomplete for {name}: {child!r}"
            )


def mock_request_count(log_path: Path, *, method: str, suffix: str) -> int:
    if not log_path.exists():
        return 0
    count = 0
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("method") == method and str(request.get("path", "")).endswith(
            suffix
        ):
            count += 1
    return count


def mock_merge_count(log_path: Path) -> int:
    return sum(
        mock_request_count(log_path, method=method, suffix="/merge")
        for method in ("POST", "PUT")
    )


def seed_delayed_restart_run(
    client: TransportClient,
    root: Path,
    repo: Path,
    forgejo_url: str,
    *,
    boundary: str,
    forgejo_owner: str = "owner",
    forgejo_repo: str = "repo",
    forgejo_token: str | None = None,
    forgejo_reviewer_token: str | None = None,
    case_name: str | None = None,
    plan: WorkPlan | None = None,
    review_verdict: str = "approved",
) -> tuple[str, WorkPlan, int, int]:
    """Seed two real aggregate PRs, then resume them through the controller."""
    if boundary not in {"aggregate_review", "base_revalidation", "merging"}:
        raise HarnessError(f"unsupported aggregate restart boundary: {boundary}")
    case_slug = case_name or boundary
    run_id = f"ordered-server-{case_slug}-restart"
    state_root = root / "controller-state"
    parent_worktree = repo / ".exo/worktrees/parent"
    plan = plan or WorkPlan(
        sub_tls=(
            SubTLTask("sub-a", WorkPlan(), order=1),
            SubTLTask("sub-b", WorkPlan(), order=1),
        )
    )
    if review_verdict not in {"approved", "changes_requested"}:
        raise HarnessError(f"unsupported seeded review verdict: {review_verdict}")
    config = TLLoopConfig(
        active=True,
        keep_alive_on_waiting=True,
        max_parallel_slices=2,
        max_events=16,
        root_dir=state_root,
        branch="main",
        worktree=repo,
        working_dir=str(parent_worktree),
    )
    initial = _initial_slices(plan, config, state_root, run_id)
    seeded_slices = RunStore(run_id, state_root)
    create(
        run_id,
        {
            "owner_branch": "main",
            "owner_worktree": str(repo),
            "ledger_run_id": server_run_id(repo),
            "slices": initial,
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        },
        root_dir=state_root,
    )
    state = seeded_slices.load()
    seeded_slices.set_plan_manifest(
        _manifest_for_plan(plan, run_id, config),
        slices=state.slices,
    )
    state = seeded_slices.load()
    parent_effects = EffectClient(client, role="tl", name="parent")
    candidates: dict[str, IntegrationCandidateState] = {}
    updated_slices = dict(state.slices)
    lifecycle = {
        "aggregate_review": IntegrationLifecycle.AGGREGATE_PR_OPEN,
        "base_revalidation": IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        "merging": IntegrationLifecycle.MERGING,
    }[boundary]
    verdict = None if boundary == "aggregate_review" else Verdict.GO
    direct_sub_tls = tuple(plan.sub_tls)
    review_ids: dict[str, int] = {}
    for name in (task.name for task in direct_sub_tls):
        branch = f"aggregate/{case_slug}/{name}"
        create_branch_with_commit(
            repo,
            branch,
            "main",
            relative_path=f"e2e-fixtures/{case_slug}/{name}.txt",
            content=f"{name} aggregate source\n",
            message=f"Prepare delayed {name} aggregate source",
        )
        filed = json_request(
            "POST",
            f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls",
            {
                "title": f"Delayed aggregate {name}",
                "body": "Controller restart acceptance fixture",
                "head": branch,
                "base": "main",
            },
            token=forgejo_token,
        )
        if not isinstance(filed, Mapping) or type(filed.get("number")) is not int:
            raise HarnessError(f"mock Forgejo did not create delayed PR: {filed!r}")
        pr_number = int(filed["number"])
        snapshot = parent_effects.watcher_pr_state(pr_number=pr_number)
        evidence = find_object(
            snapshot,
            {"head_sha", "base_sha", "patch_digest", "merge_tree_sha"},
        )
        head_sha = str(evidence["head_sha"])
        patch_digest = str(evidence["patch_digest"])
        review = json_request(
            "POST",
            f"{forgejo_url}/api/v1/repos/{forgejo_owner}/{forgejo_repo}/pulls/{pr_number}/reviews",
            {
                "event": "APPROVED"
                if review_verdict == "approved"
                else "REQUEST_CHANGES",
                "commit_id": head_sha,
                "body": "Acceptance repair finding"
                if review_verdict != "approved"
                else "",
            },
            token=forgejo_reviewer_token or forgejo_token,
        )
        review_id = review.get("id") if isinstance(review, Mapping) else None
        if type(review_id) is not int or review_id <= 0:
            raise HarnessError(f"Forgejo review omitted its durable ID: {review!r}")
        review_ids[name] = review_id
        current = state.slices[name]
        owner_id = f"{run_id}:{name}:integration"
        updated_slices[name] = replace(
            current,
            status=SliceStatus.IN_REVIEW,
            base_ref="main",
            branch=branch,
            worktree=str(parent_worktree / name),
            pr_number=pr_number,
            reviewed_head=head_sha,
            review_patch_digests={head_sha: patch_digest},
            dispatch_intent_id=f"seeded-{name}",
            dispatch_started_at=time.time(),
            dispatch_last_boundary=boundary,
            dispatch_agent_id=owner_id,
            dispatch_authoritative_event_seq=1,
            verdict=verdict,
            reviewer_attempt={head_sha: 1},
            reviewer_agent_id=f"{owner_id}:reviewer",
            publication=PublicationBinding(
                pr_number=pr_number,
                head_sha=head_sha,
                head_branch=branch,
                base_branch="main",
                attempt=1,
                invocation_id=f"{owner_id}:publication",
            ),
            handoff=HandoffEvidence(
                pr_number=pr_number,
                head_sha=head_sha,
                attempt=1,
                invocation_id=f"{owner_id}:handoff",
                agent_id=owner_id,
                observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        candidates[name] = IntegrationCandidateState(
            lifecycle=lifecycle,
            aggregate_pr_number=pr_number,
            aggregate_head_sha=head_sha,
            aggregate_patch_digest=patch_digest,
            aggregate_original_base_sha=str(evidence["base_sha"]),
            integration_owner_id=owner_id,
            integration_owner_run_id=name,
            integration_owner_branch=branch,
            integration_owner_worktree=str(parent_worktree / name),
            head_sha=head_sha,
            patch_digest=patch_digest,
            validated_base_sha=(
                str(evidence["base_sha"])
                if boundary in {"base_revalidation", "merging"}
                else None
            ),
            merge_tree_sha=(
                str(evidence["merge_tree_sha"])
                if boundary in {"base_revalidation", "merging"}
                else None
            ),
            integration_evidence_at=(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                if boundary in {"base_revalidation", "merging"}
                else None
            ),
            ci_status="success" if boundary == "merging" else "unknown",
            stage_verification="passed" if boundary == "merging" else "pending",
        )
    stages = tuple(
        OrderedStageState(stage.order, stage.sub_tls) for stage in plan.ordered_stages
    )
    integration = IntegrationRuntimeState(
        sub_tl_states={
            task.name: IntegrationLifecycle.AGGREGATE_PR_OPEN for task in direct_sub_tls
        },
        candidates=candidates,
    )
    waiting = {
        task.name: ChildHandle(task.name, f"main.{task.name}", "sub-tl")
        for task in direct_sub_tls
    }
    seeded_slices.checkpoint(
        TLWaiting(waiting),
        updated_slices,
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
        current_order=1,
        ordered_stages=stages,
        integration=integration,
    )
    for task in direct_sub_tls:
        current = updated_slices[task.name]
        review_payload = {
            "slice_id": task.name,
            "pr_number": current.pr_number,
            "head_sha": current.reviewed_head,
            "review_head_sha": current.reviewed_head,
            "review_id": review_ids[task.name],
            "review_state": review_verdict,
            "kind": "approved" if review_verdict == "approved" else "changes_requested",
            "verdict": "GO" if review_verdict == "approved" else "NO-GO",
            "reviewer_agent_id": current.reviewer_agent_id,
            "reviewer_account_authenticated": True,
            "reviewer_identity_unresolved": False,
            "findings": []
            if review_verdict == "approved"
            else [
                {
                    "severity": "blocking",
                    "path": "acceptance",
                    "rationale": "Acceptance repair finding",
                }
            ],
        }
        review_result = parent_effects.emit_controller_event(
            event_type="pr.review", payload=review_payload
        )
        if not review_result.success:
            raise HarnessError(
                f"could not seed review ledger evidence: {review_result.raw!r}"
            )
        ci_result = parent_effects.emit_controller_event(
            event_type="ci.status_changed",
            payload={
                "slice_id": task.name,
                "pr_number": current.pr_number,
                "head_sha": current.reviewed_head,
                "status": "success",
                "ci_status": "success",
            },
        )
        if not ci_result.success:
            raise HarnessError(f"could not seed CI ledger evidence: {ci_result.raw!r}")
    return (
        run_id,
        plan,
        mock_request_count(root / "mock.log", method="POST", suffix="/pulls"),
        mock_merge_count(root / "mock.log"),
    )


def seed_dispatch_restart_run(
    root: Path,
    repo: Path,
    plan: WorkPlan | None = None,
) -> tuple[str, WorkPlan, int, int]:
    """Leave both live sub-TL owners at the dispatch boundary before restart."""
    run_id = "ordered-server-dispatch-restart"
    state_root = root / "controller-state"
    plan = plan or WorkPlan(
        sub_tls=(
            SubTLTask("sub-a", WorkPlan(), order=1),
            SubTLTask("sub-b", WorkPlan(), order=1),
        )
    )
    config = TLLoopConfig(
        active=True,
        keep_alive_on_waiting=True,
        max_parallel_slices=2,
        max_events=16,
        root_dir=state_root,
        branch="main",
        worktree=repo,
        working_dir=str(repo / ".exo/worktrees/parent"),
    )
    initial = _initial_slices(plan, config, state_root, run_id)
    store = RunStore(run_id, state_root)
    create(
        run_id,
        {
            "owner_branch": "main",
            "owner_worktree": str(repo),
            "ledger_run_id": server_run_id(repo),
            "slices": initial,
            "budgets": {"ledger": {"tokens": 0, "wall_seconds": 0}},
        },
        root_dir=state_root,
    )
    state = store.load()
    store.set_plan_manifest(
        _manifest_for_plan(plan, run_id, config), slices=state.slices
    )
    state = store.load()
    stages = tuple(
        OrderedStageState(stage.order, stage.sub_tls) for stage in plan.ordered_stages
    )
    integration = IntegrationRuntimeState(
        sub_tl_states={name: IntegrationLifecycle.RUNNING for name in state.slices},
        candidates={name: IntegrationCandidateState() for name in state.slices},
    )
    store.checkpoint(
        TLPlanning(),
        state.slices,
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
        current_order=1,
        ordered_stages=stages,
        integration=integration,
    )
    return (
        run_id,
        plan,
        mock_request_count(root / "mock.log", method="POST", suffix="/pulls"),
        mock_merge_count(root / "mock.log"),
    )


def wait_for_restart_boundary(
    run_id: str,
    state_root: Path,
    boundary: str,
    *,
    process: multiprocessing.Process,
    timeout: float = 10.0,
) -> Any:
    """Wait for the persisted crash barrier instead of sleeping blindly."""
    expected = {
        "aggregate_review": IntegrationLifecycle.AGGREGATE_PR_OPEN,
        "base_revalidation": IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        "merging": IntegrationLifecycle.MERGING,
    }.get(boundary)
    deadline = time.monotonic() + timeout
    store = RunStore(run_id, state_root)
    while time.monotonic() < deadline:
        if not process.is_alive():
            raise HarnessError(f"{boundary} controller exited before its crash barrier")
        try:
            state = store.load()
        except CorruptCheckpoint:
            time.sleep(0.01)
            continue
        if boundary == "dispatch":
            if any(
                slice_state.status
                in {SliceStatus.DISPATCH_UNCONFIRMED, SliceStatus.SPAWNED}
                for slice_state in state.slices.values()
            ):
                return state
        elif (
            expected is not None
            and state.integration.candidates
            and all(
                candidate.lifecycle is expected
                for candidate in state.integration.candidates.values()
            )
        ):
            return state
        time.sleep(0.01)
    raise HarnessError(f"{boundary} controller did not reach its durable crash barrier")


def assert_action_journal_converged(path: Path) -> None:
    """Require one stable action key and a terminal outcome per side effect."""
    if not path.exists():
        raise HarnessError(f"recovery action journal missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or any(
        not isinstance(item, Mapping) for item in payload
    ):
        raise HarnessError(f"invalid recovery action journal: {path}")
    keys = [item.get("key") for item in payload]
    if any(not isinstance(key, str) or not key for key in keys) or len(
        set(keys)
    ) != len(keys):
        raise HarnessError(f"recovery action keys are not unique: {path}")
    unresolved = [
        item for item in payload if item.get("status") in {"intended", "unknown"}
    ]
    if unresolved:
        raise HarnessError(f"recovery actions have unresolved outcomes: {unresolved!r}")


def _run_restart_case(
    client: TransportClient,
    root: Path,
    repo: Path,
    forgejo_url: str,
    *,
    boundary: str,
) -> RecoveryTrace:
    if boundary == "dispatch":
        run_id, plan, pr_count_before, merge_count_before = seed_dispatch_restart_run(
            root, repo
        )
    else:
        run_id, plan, pr_count_before, merge_count_before = seed_delayed_restart_run(
            client, root, repo, forgejo_url, boundary=boundary
        )
    state_root = root / "controller-state"
    trace = RecoveryTrace.open(root / "recovery-trace.json")
    context = multiprocessing.get_context("fork")
    dispatch_marker = root / "dispatch-state-visible"
    dispatch_release = root / "dispatch-release"
    dispatch_events_before = (
        server_event_count(repo, "tl.dispatch_confirmed")
        if boundary == "dispatch"
        else None
    )

    def controller(delay: float) -> None:
        os.setsid()
        source = DelayedAggregateEventSource(run_id, state_root, initial_delay=delay)
        transport: TransportClient = TransportClient(project_root=repo, timeout=5)
        if boundary == "dispatch" and delay > 1:
            transport = DispatchBoundaryTransportClient(
                repo, dispatch_marker, dispatch_release
            )
        effects = EffectClient(
            transport,
            role="tl",
            name="parent",
        )
        config = TLLoopConfig(
            active=True,
            keep_alive_on_waiting=True,
            max_parallel_slices=2,
            max_events=16,
            root_dir=state_root,
            branch="main",
            worktree=repo,
            working_dir=str(repo / ".exo/worktrees/parent"),
            project_root=repo,
            ledger_run_id=server_run_id(repo),
            review_model_choice=_recovery_review_choice(),
        )
        run_tl_loop(run_id, plan, source, effects, config=config, root_dir=state_root)

    first = context.Process(target=controller, args=(5.0,))
    first.start()
    if boundary == "dispatch":
        deadline = time.monotonic() + 10
        while (
            not dispatch_marker.exists()
            and first.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        if not dispatch_marker.exists():
            stop_multiprocessing_process(
                first, f"{boundary} restart controller", process_group=True
            )
            raise HarnessError("dispatch controller did not reach its durable boundary")
    waiting_state = wait_for_restart_boundary(
        run_id,
        state_root,
        boundary,
        process=first,
    )
    expected_lifecycle = {
        "dispatch": IntegrationLifecycle.RUNNING,
        "aggregate_review": IntegrationLifecycle.AGGREGATE_PR_OPEN,
        "base_revalidation": IntegrationLifecycle.NEEDS_BASE_REVALIDATION,
        "merging": IntegrationLifecycle.MERGING,
    }[boundary]
    expected_heads = {
        name: waiting_state.integration.candidates[name].head_sha
        for name in waiting_state.integration.candidates
    }
    expected_bases = {
        name: waiting_state.integration.candidates[name].validated_base_sha
        for name in waiting_state.integration.candidates
    }
    if any(
        candidate.lifecycle is not expected_lifecycle
        for candidate in waiting_state.integration.candidates.values()
    ):
        stop_multiprocessing_process(
            first, f"{boundary} restart controller", process_group=True
        )
        raise HarnessError(
            f"{boundary} restart was not captured at its requested lifecycle: "
            f"{waiting_state.integration.candidates!r}"
        )
    if not first.is_alive() or any(
        current.status
        not in {
            SliceStatus.PENDING,
            SliceStatus.DISPATCHING,
            SliceStatus.DISPATCH_UNCONFIRMED,
            SliceStatus.SPAWNED,
            SliceStatus.IN_REVIEW,
        }
        for current in waiting_state.slices.values()
    ):
        stop_multiprocessing_process(
            first, f"{boundary} restart controller", process_group=True
        )
        raise HarnessError(
            f"{boundary} restart did not remain at a recoverable boundary: "
            f"{waiting_state!r}"
        )
    trace.record(
        boundary=boundary,
        point="before_crash",
        run_id=run_id,
        state_root=state_root,
        repo=repo,
    )
    stop_multiprocessing_process(
        first, f"{boundary} restart controller", process_group=True
    )
    if first.exitcode == 0:
        raise HarnessError("restart probe controller exited before the forced restart")

    source = DelayedAggregateEventSource(run_id, state_root, initial_delay=0.05)
    resumed_transport: TransportClient = client
    if boundary == "base_revalidation":
        resumed_transport = BaseAdvancingTransportClient(repo, repo)
    effects = EffectClient(resumed_transport, role="tl", name="parent")
    config = TLLoopConfig(
        active=True,
        keep_alive_on_waiting=True,
        max_parallel_slices=2,
        max_events=16,
        root_dir=state_root,
        branch="main",
        worktree=repo,
        working_dir=str(repo / ".exo/worktrees/parent"),
        project_root=repo,
        ledger_run_id=server_run_id(repo),
        review_model_choice=_recovery_review_choice(),
    )
    result = run_tl_loop(
        run_id,
        plan,
        source,
        effects,
        config=config,
        root_dir=state_root,
    )
    trace.record(
        boundary=boundary,
        point="after_recovery",
        run_id=run_id,
        state_root=state_root,
        repo=repo,
    )
    assert_action_journal_converged(state_root / run_id / "action-journal.json")
    if result.final_state.fsm.phase is not TLPhase.TLDone:
        raise HarnessError(
            f"delayed restart did not converge: emitted={source.emitted!r} "
            f"acknowledged={source.acknowledged!r}"
        )
    if boundary == "aggregate_review" and (
        not source.observed_aliases
        or any(
            "slice_id" in alias
            or not isinstance(alias.get("agent_id"), str)
            or not isinstance(alias.get("owner_id"), str)
            or not isinstance(alias.get("branch"), str)
            or not isinstance(alias.get("pr_number"), int)
            for alias in source.observed_aliases
        )
    ):
        raise HarnessError(
            f"{boundary} did not consume watcher-shaped owner aliases: "
            f"{source.observed_aliases!r}"
        )
    required_events: set[str] = set()
    if boundary == "aggregate_review":
        required_events |= {"review:sub-a", "review:sub-b"}
        required_events |= {"ci:sub-a", "ci:sub-b"}
    if (
        not required_events <= set(source.emitted)
        or any(source.emitted.count(name) != 1 for name in required_events)
        or len(set(source.emitted)) != len(source.emitted)
        or sorted(source.emitted_sequences) != sorted(source.acknowledged)
        or len(set(source.acknowledged)) != len(source.acknowledged)
    ):
        raise HarnessError(
            f"{boundary} events were not delivered and acknowledged exactly once: "
            f"emitted={source.emitted!r} sequences={source.emitted_sequences!r} "
            f"acknowledged={source.acknowledged!r}"
        )
    expected_pr_count = pr_count_before
    if (
        mock_request_count(root / "mock.log", method="POST", suffix="/pulls")
        != expected_pr_count
    ):
        actual_pr_count = mock_request_count(
            root / "mock.log", method="POST", suffix="/pulls"
        )
        raise HarnessError(
            f"{boundary} restart created a duplicate aggregate PR: "
            f"before={pr_count_before} after={actual_pr_count} expected={expected_pr_count}"
        )
    expected_merge_count = merge_count_before + (0 if boundary == "dispatch" else 2)
    if mock_merge_count(root / "mock.log") != expected_merge_count:
        raise HarnessError(
            f"{boundary} restart did not perform exactly one merge per candidate: "
            f"before={merge_count_before} after={mock_merge_count(root / 'mock.log')}"
        )
    final_candidates = result.final_state.integration.candidates
    if boundary == "base_revalidation":
        if not isinstance(resumed_transport, BaseAdvancingTransportClient):
            raise HarnessError(
                "base revalidation did not use the advancing watcher client"
            )
        if (
            not resumed_transport.base_advanced
            or resumed_transport.watcher_calls < 2
            or not resumed_transport.advanced_pr_numbers
        ):
            raise HarnessError(
                "base revalidation did not observe an advanced remote base through "
                "watcher_pr_state"
            )
        revalidated = {
            name: candidate
            for name, candidate in final_candidates.items()
            if candidate.base_revalidation_count >= 1
        }
        if not revalidated:
            raise HarnessError(
                f"base revalidation did not persist an attempt: {final_candidates!r}"
            )
        for name, candidate in revalidated.items():
            if candidate.head_sha != expected_heads[name]:
                raise HarnessError(
                    f"{name} changed head during base revalidation: "
                    f"before={expected_heads[name]!r} after={candidate.head_sha!r}"
                )
            if candidate.validated_base_sha == expected_bases[name]:
                raise HarnessError(
                    f"{name} revalidated against the original base: {candidate!r}"
                )
    if boundary == "dispatch":
        if dispatch_events_before is None:
            raise HarnessError("dispatch event baseline was not captured")
        dispatch_delta = (
            server_event_count(repo, "tl.dispatch_confirmed") - dispatch_events_before
        )
        if dispatch_delta != len(plan.sub_tls):
            raise HarnessError(
                f"dispatch restart produced {dispatch_delta} authoritative confirmations; "
                f"expected {len(plan.sub_tls)}"
            )
        intents = [
            current.dispatch_intent_id for current in result.final_state.slices.values()
        ]
        owners = [
            current.dispatch_agent_id for current in result.final_state.slices.values()
        ]
        branches = [current.branch for current in result.final_state.slices.values()]
        sequences = [
            current.dispatch_authoritative_event_seq
            for current in result.final_state.slices.values()
        ]
        if (
            any(value is None for value in (*intents, *owners, *branches, *sequences))
            or len(set(intents)) != len(intents)
            or len(set(owners)) != len(owners)
            or len(set(branches)) != len(branches)
        ):
            raise HarnessError(
                f"dispatch restart did not preserve unique owners and branches: "
                f"slices={result.final_state.slices!r}"
            )
    return trace


def server_event_count(repo: Path, event_type: str) -> int:
    """Count one event type in the server's durable JSONL ledger."""
    count = 0
    seen: set[tuple[str, str]] = set()
    for path in (repo / ".exo").rglob("*"):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(value, Mapping)
                or value.get("type", value.get("event_type")) != event_type
            ):
                continue
            identity = value.get("event_id", value.get("id", value.get("run_seq")))
            if isinstance(identity, (str, int)):
                key = (event_type, str(identity))
                if key in seen:
                    continue
                seen.add(key)
            count += 1
    return count


def run_delayed_restart_probe(
    client: TransportClient,
    root: Path,
    repo: Path,
    forgejo_url: str,
) -> dict[str, RecoveryTrace]:
    """Exercise restart recovery at every ordered-controller boundary."""
    traces: dict[str, RecoveryTrace] = {}
    for boundary in ("dispatch", "aggregate_review", "base_revalidation", "merging"):
        traces[boundary] = _run_restart_case(
            client,
            root,
            repo,
            forgejo_url,
            boundary=boundary,
        )
    return traces


def assert_stage_events(repo: Path, expected_run_id: str | None = None) -> None:
    observed: list[str] = []
    for value in server_ledger_events(repo):
        event_type = value.get("type", value.get("event_type"))
        if isinstance(event_type, str):
            observed.append(event_type)
            if (
                expected_run_id is not None
                and event_type in {"tl.stage_started", "tl.stage_completed"}
                and value.get("run_id") != expected_run_id
            ):
                raise HarnessError(
                    f"stage event used a local run ID instead of the swarm UUID: {value!r}"
                )
    required = {"tl.stage_started", "tl.stage_completed"}
    if not required <= set(observed):
        raise HarnessError(
            f"server ledger did not contain ordered stage events: {sorted(set(observed))!r}"
        )


def assert_merge_convergence_events(repo: Path, expected_run_id: str) -> None:
    """Require durable merge intent, decision, and reconciliation evidence."""
    events = server_ledger_events(repo)
    queued: list[Mapping[str, Any]] = []
    decisions: list[Mapping[str, Any]] = []
    reconciled: list[Mapping[str, Any]] = []
    action_keys: set[str] = set()
    for event in events:
        if event.get("run_id") != expected_run_id:
            continue
        event_type = event.get("type", event.get("event_type"))
        data = event.get("data")
        payload = data.get("payload") if isinstance(data, Mapping) else data
        if not isinstance(payload, Mapping):
            payload = event
        if event_type == "tl.action_queued" and payload.get("action") in {
            "merge",
            "merge_aggregate",
        }:
            queued.append(payload)
            action_key = payload.get("action_key")
            if not isinstance(action_key, str) or not action_key:
                raise HarnessError(f"merge intent has no stable action key: {event!r}")
            if action_key in action_keys:
                raise HarnessError(
                    f"duplicate merge action key in ledger: {action_key}"
                )
            action_keys.add(action_key)
        elif event_type == "tl.merge_decided" and payload.get("decision") == "merge":
            decisions.append(payload)
        elif event_type == "tl.merge_reconciled":
            reconciled.append(payload)
    if not queued:
        raise HarnessError("real-server run emitted no durable merge intent")
    if not decisions:
        raise HarnessError("real-server run emitted no merge decision")
    if not reconciled:
        raise HarnessError(
            "real-server run emitted no authoritative merge reconciliation"
        )


def waiting_child(run_root: str) -> None:
    create("waiting-child", {}, root_dir=Path(run_root))
    waiting_slice = SliceState(
        id="waiting",
        status=SliceStatus.SPAWNED,
        paths=("src/waiting",),
        depends_on=(),
        base_ref="main",
        test_plan=(),
        agent_type=None,
        model=None,
        branch="main.waiting",
        worktree=None,
        pr_number=None,
        reviewed_head=None,
        attempts=1,
        verdict=None,
        dispatch_intent_id="waiting-intent",
        dispatch_started_at=time.time(),
        dispatch_last_boundary="sub_tl_started",
        dispatch_agent_id="waiting",
        dispatch_authoritative_event_seq=1,
    )
    RunStore("waiting-child", Path(run_root)).checkpoint(
        TLWaiting({"waiting": ChildHandle("waiting", "main.waiting", "sub-tl")}),
        {"waiting": waiting_slice},
        BudgetLedger(tokens=0, wall_seconds=0),
        0,
    )
    time.sleep(1.0)


def run_waiting_supervision_probe(root: Path) -> None:
    context = multiprocessing.get_context("fork")
    process = context.Process(target=waiting_child, args=(str(root),))
    process.start()
    try:
        store = RunStore("waiting-child", root)
        deadline = time.monotonic() + 5
        while not store.path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        started = time.monotonic()
        state = _supervise_live_sub_tl(
            process, store, TLLoopConfig(keep_alive_on_waiting=True), 0.05
        )
        elapsed = time.monotonic() - started
        if state is None or state.fsm.phase is not TLPhase.TLWaiting or elapsed < 0.8:
            raise HarnessError(
                "waiting child was terminated at the supervision timeout: "
                f"state={state!r} elapsed={elapsed:.3f} exitcode={process.exitcode}"
            )
    finally:
        stop_multiprocessing_process(process, "waiting supervision child")


def main() -> None:
    project_root = PROJECT_ROOT
    with tempfile.TemporaryDirectory(prefix="exomonad-ordered-server-") as temporary:
        root = Path(temporary)
        repo, remote, branch = create_fixture(root)
        mock, forgejo_url = start_mock(root, project_root, remote)
        server: subprocess.Popen[str] | None = None
        try:
            server, client = start_server(root, repo, forgejo_url, project_root)
            if os.environ.get("EXOMONAD_MERGE_CONVERGENCE_ONLY") == "1":
                run_delayed_restart_probe(client, root, repo, forgejo_url)
                assert_merge_convergence_events(repo, server_run_id(repo))
                print("real server merge convergence: passed")
                return
            swarm_id = run_root_recursive_lifecycle_probe(client, root, repo)
            run_real_watcher_routing_probe(client, root, repo, forgejo_url, swarm_id)
            run_recursive_checkpoint_probe(client, root, repo, swarm_id)
            run_live_ordered_probe(client, root, repo, swarm_id)
            check_pr_evidence(client, branch, repo, forgejo_url)
            run_delayed_restart_probe(client, root, repo, forgejo_url)
            assert_stage_events(repo, swarm_id)
            assert_merge_convergence_events(repo, swarm_id)
            run_waiting_supervision_probe(root / "waiting-state")
            print("real server TransportClient ordered recursion: passed")
        finally:
            cleanup_errors: list[str] = []
            if server is not None:
                try:
                    stop_subprocess(server, "ExoMonad server")
                except Exception as error:  # noqa: BLE001 - cleanup must continue for every error
                    cleanup_errors.append(str(error))
            subprocess.run(
                ["tmux", "kill-session", "-t", f"ordered-server-e2e-{os.getpid()}"],
                check=False,
                capture_output=True,
            )
            try:
                stop_subprocess(mock, "mock API")
            except Exception as error:  # noqa: BLE001 - cleanup must continue for every error
                cleanup_errors.append(str(error))
            if cleanup_errors:
                raise HarnessError(
                    "managed E2E cleanup failed: " + "; ".join(cleanup_errors)
                )


if __name__ == "__main__":
    main()
