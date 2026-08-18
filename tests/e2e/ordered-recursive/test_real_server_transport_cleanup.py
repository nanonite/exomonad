"""Deterministic cleanup regressions for the managed recursive routing probe."""

from __future__ import annotations

import multiprocessing
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import real_server_transport as harness


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
