"""Run the real-server recursive crash/restart acceptance matrix."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ORDERED_DIR = Path(__file__).resolve().parents[1] / "ordered-recursive"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(ORDERED_DIR))

import real_server_transport as real
from boundaries import CRASH_BOUNDARIES, CrashBoundary, validate_matrix
from controller import controller, resume, wait_for_crash
from evidence import (
    AcceptanceError,
    assert_checkpoint_progression,
    assert_crash_record,
    assert_effect_events,
    assert_journal_terminal,
    assert_resume_not_redispatched,
)
from fixture import plan, seed_aggregate_publication


def _environment() -> dict[str, str]:
    required = (
        "EXOMONAD_FORGEJO_E2E_URL",
        "EXOMONAD_FORGEJO_E2E_TOKEN",
        "EXOMONAD_FORGEJO_E2E_OWNER",
        "EXOMONAD_FORGEJO_E2E_REPO",
        "EXOMONAD_FORGEJO_E2E_GIT_REMOTE",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise AcceptanceError(
            "#1057 requires a dedicated real Forgejo repository; missing "
            + ", ".join(missing)
        )
    if os.environ.get("EXOMONAD_FORGEJO_E2E_MOCK") == "1":
        raise AcceptanceError("#1057 cannot run with the Forgejo-shaped mock API")
    return {name: os.environ[name] for name in required}


def _case_name(root: Path, boundary: CrashBoundary) -> str:
    return f"crash-{boundary.name}-{boundary.point}-{root.name[-8:]}"


def run_case(
    root: Path,
    repo: Path,
    forgejo_url: str,
    config: dict[str, str],
    boundary: CrashBoundary,
) -> dict[str, Any]:
    work_plan = plan()
    case_name = _case_name(root, boundary)
    if boundary.name in {"publication", "aggregate_publication"}:
        run_id, work_plan = seed_aggregate_publication(root, repo, work_plan)
    elif boundary.name == "spawn":
        run_id, work_plan, _, _ = real.seed_dispatch_restart_run(root, repo, work_plan)
    else:
        seed_boundary = (
            "aggregate_review"
            if boundary.name in {"review", "adoption", "repair"}
            else "merging"
        )
        run_id, _, _, _ = real.seed_delayed_restart_run(
            real.TransportClient(project_root=repo, timeout=10),
            root,
            repo,
            forgejo_url,
            boundary=seed_boundary,
            forgejo_owner=config["EXOMONAD_FORGEJO_E2E_OWNER"],
            forgejo_repo=config["EXOMONAD_FORGEJO_E2E_REPO"],
            forgejo_token=config["EXOMONAD_FORGEJO_E2E_TOKEN"],
            case_name=case_name,
            plan=work_plan,
            review_verdict=(
                "changes_requested" if boundary.name == "repair" else "approved"
            ),
        )
    state_root = root / "controller-state"
    ledger_run_id = real.server_run_id(repo)
    marker = root / "crash-traces" / f"{case_name}.jsonl"
    process = multiprocessing.get_context("fork").Process(
        target=controller,
        args=(
            run_id,
            state_root,
            repo,
            ledger_run_id,
            work_plan,
            boundary,
            marker,
            boundary.name == "review",
        ),
        name=case_name,
    )
    process.start()
    wait_for_crash(process, marker)
    checkpoint = state_root / run_id / "run.json"
    before_restart = root / "crash-traces" / f"{case_name}.before.json"
    shutil.copy2(checkpoint, before_restart)
    resume_trace = root / "crash-traces" / f"{case_name}.resume.jsonl"
    result = resume(run_id, state_root, repo, ledger_run_id, resume_trace)
    after_restart = root / "crash-traces" / f"{case_name}.after.json"
    shutil.copy2(checkpoint, after_restart)
    final_state = real.RunStore(run_id, state_root).load()
    if final_state.fsm.phase is not real.TLPhase.TLDone:
        raise AcceptanceError(f"{case_name} did not converge to TLDone")
    identity = assert_crash_record(marker, boundary.name, boundary.point)
    counts = assert_journal_terminal(state_root / run_id / "action-journal.json")
    assert_checkpoint_progression([before_restart, after_restart])
    resumed_calls = assert_resume_not_redispatched(
        resume_trace, identity, boundary=boundary.name, point=boundary.point
    )
    effects = assert_effect_events(repo, ledger_run_id)
    return {
        "boundary": boundary.name,
        "point": boundary.point,
        "effect_identity": identity,
        "journal_operations": counts,
        "merge_effects": effects,
        "state_version": result.final_state.state_version,
        "cursor": result.final_state.events.last_consumed_offset,
        "resumed_same_effect_calls": resumed_calls,
    }


def _server_repetitions() -> int:
    value = os.environ.get("EXOMONAD_1057_SERVER_RUNS", "3")
    try:
        repetitions = int(value)
    except ValueError as error:
        raise AcceptanceError("EXOMONAD_1057_SERVER_RUNS must be an integer") from error
    if repetitions <= 0:
        raise AcceptanceError("EXOMONAD_1057_SERVER_RUNS must be positive")
    return repetitions


def run_matrix() -> dict[str, Any]:
    validate_matrix()
    config = _environment()
    results: list[dict[str, Any]] = []
    for repetition in range(1, _server_repetitions() + 1):
        for boundary in CRASH_BOUNDARIES:
            with tempfile.TemporaryDirectory(
                prefix=f"exomonad-1057-run{repetition}-{boundary.name}-{boundary.point}-"
            ) as raw:
                root = Path(raw)
                repo, _, _ = real.clone_external_fixture(root)
                server, _ = real.start_server(
                    root,
                    repo,
                    config["EXOMONAD_FORGEJO_E2E_URL"],
                    PROJECT_ROOT,
                    forgejo_token=config["EXOMONAD_FORGEJO_E2E_TOKEN"],
                    forgejo_reviewer_token=os.environ.get(
                        "EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN"
                    ),
                )
                try:
                    result = run_case(
                        root, repo, config["EXOMONAD_FORGEJO_E2E_URL"], config, boundary
                    )
                    result["server_run"] = repetition
                    results.append(result)
                finally:
                    try:
                        real.stop_server(server, repo, "#1057 real-server acceptance")
                    finally:
                        real.cleanup_external_case(
                            repo,
                            config["EXOMONAD_FORGEJO_E2E_URL"],
                            config["EXOMONAD_FORGEJO_E2E_OWNER"],
                            config["EXOMONAD_FORGEJO_E2E_REPO"],
                            config["EXOMONAD_FORGEJO_E2E_TOKEN"],
                            _case_name(root, boundary),
                        )
    return {"passed": True, "server_runs": results}


if __name__ == "__main__":
    print(json.dumps(run_matrix(), indent=2, sort_keys=True))
