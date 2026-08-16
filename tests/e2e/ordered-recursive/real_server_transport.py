#!/usr/bin/env python3
"""Exercise ordered TL ownership and PR evidence through the real server.

This harness deliberately keeps the Forgejo-shaped API local, but does not
replace the product boundary: the Rust server, generated WASM tool, Git
remote, TransportClient, and merge handler are all exercised.  The existing
``ordered_recursive.py`` remains the hermetic ControllerScenario coverage.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tl_loop.client.effects import EffectClient, ToolResult
from tl_loop.client.transport import JsonObject, TransportClient, TransportError
from tl_loop.fsm.phase import ChildHandle, TLPhase, TLWaiting
from tl_loop.loop.driver import (
    SubTLTask,
    TLLoopConfig,
    WorkPlan,
    _supervise_live_sub_tl,
    run_tl_loop,
)
from tl_loop.state.schema import SliceState, SliceStatus
from tl_loop.state.store import BudgetLedger, RunStore, create


class HarnessError(RuntimeError):
    """The server-backed acceptance contract was violated."""


class EmptyEventSource:
    """An event source for child plans that finish without external events."""

    def get(self, timeout: float | None = None) -> Any:
        del timeout
        raise queue.Empty

    def acknowledge(self, event: Any) -> int:
        return event.run_seq


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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_request(method: str, url: str, payload: JsonObject | None = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
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
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Create server fixture", "-q")
    git(repo, "push", "-u", "origin", "main", "-q")
    git(repo, "switch", "-c", "feature/ordered-server", "-q")
    (repo / "server-evidence.txt").write_text("evidence\n", encoding="utf-8")
    git(repo, "add", "server-evidence.txt")
    git(repo, "commit", "-m", "Add server evidence change", "-q")
    git(repo, "push", "-u", "origin", "feature/ordered-server", "-q")
    return repo, remote, "feature/ordered-server"


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
    process.terminate()
    raise HarnessError(f"timed out waiting for mock API: {stderr.name}")


def start_server(
    root: Path, repo: Path, forgejo_url: str, project_root: Path
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
    for child_name in ("sub-a", "sub-b"):
        agent_dir = repo / ".exo/agents" / child_name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / ".birth_branch").write_text("main\n", encoding="utf-8")
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
    shutil.copy2(wasm, repo / ".exo/wasm/wasm-guest-devswarm.wasm")
    port = free_port()
    session = f"ordered-server-e2e-{os.getpid()}"
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
                f'forgejo_url = "{forgejo_url}"',
                'forgejo_token = "test-token"',
                'forgejo_reviewer_token = "test-token"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    log = (root / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(binary), "serve"], cwd=repo, stdout=log, stderr=log, text=True
    )
    client = TransportClient(project_root=repo, timeout=5)
    wait_for_server(client, process, Path(log.name))
    return process, client


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


def run_live_ordered_probe(client: TransportClient, root: Path, repo: Path) -> None:
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
            keep_alive_on_waiting=True,
            max_parallel_slices=2,
            max_events=8,
            idle_timeout=0.2,
            dispatch_timeout=0.2,
            root_dir=root / "controller-state",
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


def main() -> None:
    project_root = PROJECT_ROOT
    with tempfile.TemporaryDirectory(prefix="exomonad-ordered-server-") as temporary:
        root = Path(temporary)
        repo, remote, branch = create_fixture(root)
        mock, forgejo_url = start_mock(root, project_root, remote)
        server: subprocess.Popen[str] | None = None
        try:
            server, client = start_server(root, repo, forgejo_url, project_root)
            check_pr_evidence(client, branch, repo, forgejo_url)
            run_live_ordered_probe(client, root, repo)
            run_waiting_supervision_probe(root / "waiting-state")
            print("real server TransportClient ordered recursion: passed")
        finally:
            if server is not None:
                server.terminate()
                server.wait(timeout=10)
            mock.terminate()
            mock.wait(timeout=10)


if __name__ == "__main__":
    main()
