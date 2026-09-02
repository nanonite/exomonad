"""Controller process and restart mechanics for the #1057 matrix."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from typing import Any

import real_server_transport as real
from boundaries import CrashBoundary
from crash_transport import CrashBoundaryTransport, RecordingTransport
from evidence import AcceptanceError


def controller(
    run_id: str,
    state_root: Path,
    repo: Path,
    ledger_run_id: str,
    plan: real.WorkPlan | None,
    boundary: CrashBoundary,
    trace_path: Path,
    advance_base: bool,
) -> None:
    """Run one controller invocation until the injected process death."""
    transport = CrashBoundaryTransport(
        repo,
        trace_path,
        boundary,
        advance_base_after_watcher=advance_base,
    )
    source = real.LazyLedgerSource(
        repo / ".exo" / "ledger" / "segments",
        state_root,
        run_id,
        ledger_run_id,
        None,
    )
    try:
        real.run_tl_loop(
            run_id,
            plan,
            source,
            real.EffectClient(transport, role="tl", name="parent"),
            config=real.TLLoopConfig(
                active=True,
                session_mode="continue",
                keep_alive_on_waiting=True,
                max_parallel_slices=2,
                max_events=64,
                root_dir=state_root,
                branch="main",
                worktree=repo,
                working_dir=str(repo / ".exo/worktrees/parent"),
                project_root=repo,
                ledger_run_id=ledger_run_id,
                review_model_choice=real._recovery_review_choice(),
            ),
            root_dir=state_root,
        )
    finally:
        source.close()


def wait_for_crash(
    process: multiprocessing.Process, marker: Path, *, timeout: float = 45.0
) -> None:
    """Require the selected marker and process death, never a timed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file() and not process.is_alive():
            process.join(timeout=1)
            return
        if not process.is_alive():
            raise AcceptanceError(
                f"controller exited without the requested crash marker: {marker}"
            )
        time.sleep(0.05)
    real.stop_multiprocessing_process(
        process, "crash-boundary controller", process_group=True
    )
    raise AcceptanceError(f"timed out waiting for crash boundary {marker}")


def resume(
    run_id: str,
    state_root: Path,
    repo: Path,
    ledger_run_id: str,
    trace_path: Path,
) -> Any:
    """Resume from the persisted manifest, recording all resumed UDS calls."""
    source = real.LazyLedgerSource(
        repo / ".exo" / "ledger" / "segments",
        state_root,
        run_id,
        ledger_run_id,
        None,
    )
    try:
        return real.run_tl_loop(
            run_id,
            None,
            source,
            real.EffectClient(
                RecordingTransport(repo, trace_path),
                role="tl",
                name="parent",
            ),
            config=real.TLLoopConfig(
                active=True,
                session_mode="continue",
                keep_alive_on_waiting=False,
                max_parallel_slices=2,
                max_events=128,
                root_dir=state_root,
                branch="main",
                worktree=repo,
                working_dir=str(repo / ".exo/worktrees/parent"),
                project_root=repo,
                ledger_run_id=ledger_run_id,
                review_model_choice=real._recovery_review_choice(),
            ),
            root_dir=state_root,
        )
    finally:
        source.close()
