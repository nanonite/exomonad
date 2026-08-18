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
    harness.stop_multiprocessing_process(process, "stubborn test controller", timeout=0.05)
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


def test_recovery_action_journal_rejects_unresolved_or_duplicate_keys(tmp_path: Path) -> None:
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


@pytest.mark.parametrize(
    "phase",
    ("dispatch", "publication", "watcher_delivery", "checkpoint", "stabilization"),
)
def test_routing_cleanup_attempts_every_owned_worker_after_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    process = multiprocessing.get_context("fork").Process(target=time.sleep, args=(60,))
    process.start()
    stopped: list[str] = []

    def fake_stop_worker(repo: Path, worker_name: str) -> None:
        stopped.append(worker_name)
        if worker_name == "owner":
            raise RuntimeError(f"injected {phase} cleanup failure")

    monkeypatch.setattr(harness, "stop_spawned_worker", fake_stop_worker)
    monkeypatch.setattr(
        harness,
        "reviewer_spawn_events",
        lambda repo, swarm_id, pr_number: {
            1: {"data": {"child_agent": "reviewer"}}
        },
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
