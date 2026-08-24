"""Deterministic cleanup regressions for the managed recursive routing probe."""

from __future__ import annotations

import json
import multiprocessing
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import real_server_transport as harness

from tl_loop.state.store import create


def _ignore_termination() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)


def test_multiprocessing_cleanup_kills_and_reaps_stubborn_controller() -> None:
    process = multiprocessing.get_context("fork").Process(target=_ignore_termination)
    process.start()
    harness.stop_multiprocessing_process(
        process, "stubborn test controller", timeout=0.05
    )
    assert not process.is_alive()
    assert process.exitcode is not None


def test_subprocess_cleanup_kills_and_reaps_stubborn_server() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ]
    )
    harness.stop_subprocess(process, "stubborn test server", timeout=0.05)
    assert process.poll() is not None


def test_recovery_trace_persists_checkpoint_cursor_and_action_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_root = tmp_path / "state"
    create("run", {}, root_dir=state_root)
    journal = state_root / "run" / "action-journal.json"
    journal.write_text(
        json.dumps(
            [
                {
                    "key": "action-1",
                    "operation": "spawn_worker",
                    "status": "confirmed",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(harness, "server_ledger_events", lambda _: [])
    trace = harness.RecoveryTrace.open(tmp_path / "recovery-trace.json")

    trace.record(
        boundary="dispatch",
        point="after_recovery",
        run_id="run",
        state_root=state_root,
        repo=tmp_path,
    )

    payload = json.loads((tmp_path / "recovery-trace.json").read_text())
    assert payload["records"][0]["cursor"] == 0
    assert payload["records"][0]["action_keys"] == [
        {"key": "action-1", "operation": "spawn_worker", "status": "confirmed"}
    ]


def test_recovery_action_journal_rejects_unresolved_or_duplicate_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "actions.json"
    path.write_text(
        json.dumps(
            [
                {"key": "same", "operation": "merge_pr", "status": "confirmed"},
                {"key": "same", "operation": "merge_pr", "status": "confirmed"},
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(harness.HarnessError, match="not unique"):
        harness.assert_action_journal_converged(path)

    path.write_text(
        json.dumps([{"key": "unknown", "operation": "merge_pr", "status": "unknown"}]),
        encoding="utf-8",
    )
    with pytest.raises(harness.HarnessError, match="unresolved"):
        harness.assert_action_journal_converged(path)


def test_real_server_mismatched_runtime_name_retains_terminal_exit_context(
    tmp_path: Path,
) -> None:
    """Production-shaped fixture for a slice slug and suffixed runtime owner.

    The server writes the terminal invocation record under the runtime agent
    directory; the slice's worktree remains available for later reconciliation.
    """
    runtime_name = "tunable-operator-body-opencode"
    slice_id = "tunable-operator-body"
    repo = tmp_path / "repo"
    agent_dir = repo / ".exo" / "agents" / runtime_name
    worktree = repo / ".exo" / "worktrees" / runtime_name
    agent_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (agent_dir / "invocation.json").write_text(
        json.dumps(
            {
                "invocation_id": "inv-mismatch-1",
                "runtime": "opencode",
                "trigger": "spawn",
                "started_at": 1,
                "ended_at": 2,
                "status": "killed",
                "exit_code": None,
                "generation": 4,
                "runtime_agent_id": runtime_name,
                "slice_id": slice_id,
                "branch": "main.tunable-operator-body",
                "worktree": str(worktree),
                "exit_reason": "tmux_target_exited_without_exit_marker",
                "exit_classification": "missing_exit_marker",
                "stderr_tail": "opencode exited before writing exit marker",
            }
        ),
        encoding="utf-8",
    )
    segments = repo / ".exo" / "ledger" / "segments"
    segments.mkdir(parents=True)
    (segments / "0001.jsonl").write_text(
        json.dumps(
            {
                "type": "agent.invocation.finished",
                "agent_id": runtime_name,
                "data": {
                    "invocation_id": "inv-mismatch-1",
                    "runtime_agent_id": runtime_name,
                    "slice_id": slice_id,
                    "generation": 4,
                    "exit_classification": "missing_exit_marker",
                    "stderr_tail": "opencode exited before writing exit marker",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = harness.server_ledger_events(repo)
    finished = next(
        event for event in events if event["type"] == "agent.invocation.finished"
    )
    assert finished["agent_id"] == runtime_name
    assert finished["data"]["slice_id"] == slice_id
    assert finished["data"]["exit_classification"] == "missing_exit_marker"
    assert worktree.is_dir(), (
        "recovery must preserve the worktree before reconciliation"
    )


def test_routing_cleanup_attempts_every_owned_worker_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cleanup_recursive_watcher_probe itself: a failure disposing one owned
    worker must not prevent best-effort disposal of the others."""
    process = multiprocessing.get_context("fork").Process(target=time.sleep, args=(60,))
    process.start()
    stopped: list[str] = []

    def fake_stop_worker(repo: Path, worker_name: str) -> None:
        stopped.append(worker_name)
        if worker_name == "owner":
            raise RuntimeError("injected owner cleanup failure")

    monkeypatch.setattr(harness, "stop_spawned_worker", fake_stop_worker)
    monkeypatch.setattr(
        harness,
        "reviewer_spawn_events",
        lambda repo, swarm_id, pr_number: {1: {"data": {"child_agent": "reviewer"}}},
    )
    try:
        with pytest.raises(harness.HarnessError, match="cleanup failed"):
            harness.cleanup_recursive_watcher_probe(
                tmp_path,
                "swarm",
                process,
                {"owner_id": "owner", "pr_number": 42},
            )
        assert stopped == ["owner", "reviewer"]
        assert not process.is_alive()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


_PROBE_PHASES = (
    "dispatch",
    "publication",
    "watcher_delivery",
    "checkpoint",
    "stabilization",
)


def _install_fake_probe_phases(
    monkeypatch: pytest.MonkeyPatch, failing_phase: str
) -> None:
    """Replace each of run_real_watcher_routing_probe's five phases with a
    fast fake that returns deterministic values, except failing_phase, which
    raises. This exercises the real probe wrapper and its finally-block
    cleanup — not a direct call to the cleanup helper — so the assertions
    prove cleanup actually fires from inside each real phase boundary."""

    def dispatch(root_process: object, child_state_root: Path, slice_id: str) -> str:
        if failing_phase == "dispatch":
            raise RuntimeError("injected dispatch phase failure")
        return "owner"

    def publication(
        client: object,
        forgejo_url: str,
        owner_id: str,
        cleanup_state: dict[str, object],
    ) -> tuple[int, str, str]:
        # Mirrors the real phase: the PR number is known (and a reviewer
        # may already exist for it) as soon as it is filed, before this
        # phase can fail on a later step such as watcher_pr_state or the
        # approval POST, so cleanup_state must record it first -- on both
        # the success and the injected-failure path.
        cleanup_state["pr_number"] = 42
        if failing_phase == "publication":
            raise RuntimeError("injected publication phase failure")
        return 42, "main.sub-a.real-watcher-leaf", "deadbeef"

    def watcher_delivery(
        root_process: object,
        repo: Path,
        swarm_id: str,
        pr_number: int,
        owner_id: str,
        slice_id: str,
        branch: str,
        head_sha: str,
    ) -> int:
        if failing_phase == "watcher_delivery":
            raise RuntimeError("injected watcher_delivery phase failure")
        return 5

    def checkpoint(
        root_process: object,
        child_state_root: Path,
        slice_id: str,
        swarm_id: str,
        pr_number: int,
        head_sha: str,
        expected_cursor: int,
    ) -> None:
        if failing_phase == "checkpoint":
            raise RuntimeError("injected checkpoint phase failure")

    def stabilization(
        root_process: object, repo: Path, swarm_id: str, pr_number: int
    ) -> None:
        if failing_phase == "stabilization":
            raise RuntimeError("injected stabilization phase failure")

    monkeypatch.setattr(harness, "_probe_dispatch_phase", dispatch)
    monkeypatch.setattr(harness, "_probe_publication_phase", publication)
    monkeypatch.setattr(harness, "_probe_watcher_delivery_phase", watcher_delivery)
    monkeypatch.setattr(harness, "_probe_checkpoint_phase", checkpoint)
    monkeypatch.setattr(harness, "_probe_stabilization_phase", stabilization)


@pytest.mark.parametrize("phase", _PROBE_PHASES)
def test_probe_failure_at_each_phase_disposes_controller_and_owned_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    """chainlink #908: a failure injected into the real dispatch, publication,
    watcher_delivery, checkpoint, or stabilization phase of
    run_real_watcher_routing_probe must still reach its finally block and
    dispose the controller process and every owned worker/reviewer known by
    that point — not just when cleanup is invoked directly."""
    # Not started here: run_real_watcher_routing_probe owns start(), matching
    # its real control flow (create → start → run body → finally cleanup).
    process = multiprocessing.get_context("fork").Process(
        target=_ignore_termination, name=f"fake-controller-{phase}"
    )
    monkeypatch.setattr(
        harness.multiprocessing.get_context("fork"),
        "Process",
        lambda *a, **k: process,
    )

    stopped: list[str] = []
    monkeypatch.setattr(
        harness,
        "stop_spawned_worker",
        lambda repo, worker_name: stopped.append(worker_name),
    )
    monkeypatch.setattr(
        harness,
        "reviewer_spawn_events",
        lambda repo, swarm_id, pr_number: {1: {"data": {"child_agent": "reviewer"}}},
    )
    _install_fake_probe_phases(monkeypatch, phase)

    try:
        with pytest.raises(RuntimeError, match=f"injected {phase} phase failure"):
            harness.run_real_watcher_routing_probe(
                client=object(),
                root=tmp_path,
                repo=tmp_path,
                forgejo_url="http://forgejo.invalid",
                swarm_id="swarm",
            )
        assert not process.is_alive(), (
            "controller process must be disposed after probe failure"
        )

        phase_index = _PROBE_PHASES.index(phase)
        expected_workers: set[str] = set()
        if phase_index >= _PROBE_PHASES.index("publication"):
            # cleanup_state["owner_id"] is recorded right after the dispatch
            # phase returns, so it is known — and must be cleaned up — for
            # every later phase's failure, including publication's own.
            expected_workers.add("owner")
        if phase_index >= _PROBE_PHASES.index("publication"):
            # cleanup_state["pr_number"] is recorded the moment the PR is
            # filed, inside the publication phase itself -- not after it
            # returns -- so a failure injected mid-phase (chainlink #908,
            # e.g. watcher_pr_state or the approval POST raising after
            # file_pr already succeeded) must still find and dispose the
            # reviewer, not just a later phase's failure.
            expected_workers.add("reviewer")
        assert set(stopped) == expected_workers, (
            f"phase={phase} stopped={stopped} expected={expected_workers}"
        )
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)


def test_merge_convergence_assertion_requires_intent_decision_and_reconciliation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events = [
        {
            "type": "tl.action_queued",
            "run_id": "swarm",
            "data": {
                "payload": {
                    "action": "merge_aggregate",
                    "action_key": "merge-key-1",
                }
            },
        },
        {
            "type": "tl.merge_decided",
            "run_id": "swarm",
            "data": {"payload": {"decision": "merge"}},
        },
        {
            "type": "tl.merge_reconciled",
            "run_id": "swarm",
            "data": {"payload": {"reconciliation": "authoritative_merge_reconciled"}},
        },
    ]
    monkeypatch.setattr(harness, "server_ledger_events", lambda _: events)

    harness.assert_merge_convergence_events(tmp_path, "swarm")

    monkeypatch.setattr(harness, "server_ledger_events", lambda _: events[:1])
    with pytest.raises(harness.HarnessError, match="merge decision"):
        harness.assert_merge_convergence_events(tmp_path, "swarm")
