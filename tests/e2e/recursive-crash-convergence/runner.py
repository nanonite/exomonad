"""Run the real-server recursive crash/restart acceptance matrix."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ORDERED_DIR = Path(__file__).resolve().parents[1] / "ordered-recursive"
# tests/e2e/recursive-crash-convergence/runner.py -> parents[3] is the repo root
# that owns target/debug/exomonad and .exo/wasm/. parents[2] would resolve to
# the tests/ directory and make start_server reject every acceptance run.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = PROJECT_ROOT / "tests" / "e2e" / "lib"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(ORDERED_DIR))
sys.path.insert(0, str(LIB_DIR))

import e2e_harness.chainlink_db as chainlink_db  # noqa: E402
import e2e_harness.cleanup as cl  # noqa: E402
import real_server_transport as real  # noqa: E402
from boundaries import CRASH_BOUNDARIES, CrashBoundary, validate_matrix
from controller import controller, resume, wait_for_crash
from evidence import (
    AcceptanceError,
    assert_checkpoint_progression,
    assert_crash_record,
    assert_effect_events,
    assert_recursive_effect_cardinality,
    assert_remote_ancestry,
    assert_required_effects,
    assert_resume_not_redispatched,
)
from fixture import plan, seed_aggregate_publication


def _environment() -> dict[str, str]:
    required = (
        "EXOMONAD_FORGEJO_E2E_URL",
        "EXOMONAD_FORGEJO_E2E_TOKEN",
        "EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN",
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


def _issue_id(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, Mapping):
        for key in ("id", "issue_id", "number"):
            candidate = value.get(key)
            if type(candidate) is int and candidate > 0:
                return candidate
        for child in value.values():
            found = _issue_id(child)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found = _issue_id(child)
            if found is not None:
                return found
    return None


def _chainlink_command_with_db(database: Path, *arguments: str) -> Any:
    environment = {**os.environ, "CHAINLINK_DB": str(database)}
    result = subprocess.run(
        ["chainlink", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AcceptanceError(
            f"Chainlink command failed ({result.returncode}): {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AcceptanceError(
            f"Chainlink command returned non-JSON output: {result.stdout!r}"
        ) from error


def _create_fixture_issue(database: Path, case_name: str) -> int:
    value = _chainlink_command_with_db(
        database,
        "create",
        f"Verify recursive crash convergence {case_name}",
        "--priority",
        "low",
        "--label",
        "test",
        "--json",
        "--quiet",
    )
    issue_id = _issue_id(value)
    if issue_id is None or issue_id == 1057:
        raise AcceptanceError(f"Chainlink did not create a disposable issue: {value!r}")
    return issue_id


def _cleanup_fixture_issue(database: Path, issue_id: int) -> None:
    value = _chainlink_command_with_db(database, "show", str(issue_id), "--json")
    status = _status(value)
    if status == "closed":
        return
    result = subprocess.run(
        ["chainlink", "close", str(issue_id), "--no-changelog", "--quiet"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "CHAINLINK_DB": str(database)},
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AcceptanceError(
            f"Chainlink cleanup failed ({result.returncode}): {result.stderr.strip()}"
        )


def _status(value: Any) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status")
        if isinstance(status, str):
            return status.lower()
        for child in value.values():
            found = _status(child)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            found = _status(child)
            if found is not None:
                return found
    return None


def _identity_agents(work_plan: real.WorkPlan) -> dict[str, str]:
    identities: dict[str, str] = {}

    def add_scope(scope: real.WorkPlan, branch: str) -> None:
        for task in (*scope.workers, *scope.leaves):
            identities[task.name] = f"{branch}.{task.name}"
        for task in scope.sub_tls:
            child_branch = f"{branch}.{task.name}"
            identities[task.name] = child_branch
            child_plan = (
                task.plan
                if isinstance(task.plan, real.WorkPlan)
                else real.WorkPlan.from_mapping(task.plan)
            )
            add_scope(child_plan, child_branch)

    add_scope(work_plan, "main")
    return identities


def _leaf_branches(work_plan: real.WorkPlan) -> tuple[str, ...]:
    branches: list[str] = []

    def add_scope(scope: real.WorkPlan, branch: str) -> None:
        branches.extend(f"{branch}.{leaf.name}" for leaf in scope.leaves)
        for task in scope.sub_tls:
            child_plan = (
                task.plan
                if isinstance(task.plan, real.WorkPlan)
                else real.WorkPlan.from_mapping(task.plan)
            )
            add_scope(child_plan, f"{branch}.{task.name}")

    add_scope(work_plan, "main")
    return tuple(sorted(branches))


def _case_name(root: Path, boundary: CrashBoundary) -> str:
    return f"crash-{boundary.name}-{boundary.point}-{root.name[-8:]}"


def _nested_aggregate_evidence(
    state_root: Path, baseline_marker: Path
) -> tuple[int, str, str]:
    """Read the nested aggregate identity from the durable child checkpoint."""
    try:
        baseline_document = json.loads(baseline_marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        raise AcceptanceError(
            f"nested baseline head marker is missing: {baseline_marker}"
        ) from error
    baseline_head = (
        baseline_document.get("nested-a")
        if isinstance(baseline_document, Mapping)
        else None
    )
    if not isinstance(baseline_head, str) or not baseline_head:
        raise AcceptanceError(
            f"nested baseline marker lacks nested-a: {baseline_document!r}"
        )

    evidence: set[tuple[int, str]] = set()
    for checkpoint in state_root.rglob("run.json"):
        try:
            document = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        integration = document.get("integration")
        if not isinstance(integration, Mapping):
            continue
        records: list[Mapping[str, Any]] = [integration]
        candidates = integration.get("candidates")
        if isinstance(candidates, Mapping):
            records.extend(
                candidate
                for candidate in candidates.values()
                if isinstance(candidate, Mapping)
            )
        for record in records:
            if (
                record.get("integration_owner_run_id") != "nested-a"
                or record.get("integration_owner_branch") != "main.sub-a.nested-a"
            ):
                continue
            pr_number = record.get("aggregate_pr_number")
            head_sha = record.get("aggregate_head_sha")
            if type(pr_number) is int and pr_number > 0 and isinstance(head_sha, str):
                evidence.add((pr_number, head_sha))
    if len(evidence) != 1:
        raise AcceptanceError(
            "nested aggregate checkpoint must contain exactly one durable PR/head: "
            f"{sorted(evidence)!r}"
        )
    pr_number, head_sha = next(iter(evidence))
    if head_sha == baseline_head:
        raise AcceptanceError(
            "nested aggregate head did not advance beyond the pre-work baseline"
        )
    return pr_number, head_sha, baseline_head


def _assert_nested_aggregate_pr(
    config: dict[str, str],
    forgejo_url: str,
    repo: Path,
    case_name: str,
    state_root: Path,
) -> None:
    """Prove production created the nested aggregate against its direct parent."""
    marker = repo / ".exo" / f"1057-nested-baseline-heads-{case_name}.json"
    expected_pr, expected_head, _ = _nested_aggregate_evidence(state_root, marker)
    pulls = real.json_request(
        "GET",
        f"{forgejo_url}/api/v1/repos/{config['EXOMONAD_FORGEJO_E2E_OWNER']}/"
        f"{config['EXOMONAD_FORGEJO_E2E_REPO']}/pulls?state=all&limit=100",
        token=config["EXOMONAD_FORGEJO_E2E_TOKEN"],
    )
    if not isinstance(pulls, list):
        raise AcceptanceError(f"Forgejo pull listing is not an array: {pulls!r}")
    matches = []
    for pull in pulls:
        if not isinstance(pull, Mapping):
            continue
        head = pull.get("head")
        base = pull.get("base")
        head_ref = head.get("ref") if isinstance(head, Mapping) else None
        base_ref = base.get("ref") if isinstance(base, Mapping) else None
        head_sha = head.get("sha") if isinstance(head, Mapping) else None
        title = pull.get("title")
        if (
            pull.get("number") == expected_pr
            and head_ref == "main.sub-a.nested-a"
            and base_ref == "main.sub-a"
            and title == "Aggregate nested-a into main.sub-a"
            and head_sha == expected_head
        ):
            matches.append(pull)
    if len(matches) != 1:
        raise AcceptanceError(
            "production did not create exactly one nested aggregate PR targeting "
            f"main.sub-a: {matches!r}"
        )


def run_case(
    root: Path,
    repo: Path,
    forgejo_url: str,
    config: dict[str, str],
    boundary: CrashBoundary,
    chainlink_issue_id: int,
    chainlink_db: Path,
) -> dict[str, Any]:
    state_root = root / "controller-state"
    ledger_run_id = real.server_run_id(repo)
    # Structure-only plan for seeding (sources are not serialized into the
    # manifest, so seeding is unaffected by them).
    work_plan = plan()
    case_name = _case_name(root, boundary)
    if boundary.name in {"publication", "aggregate_publication"}:
        run_id, work_plan = seed_aggregate_publication(
            root,
            repo,
            work_plan,
            case_name=case_name,
        )
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
            forgejo_reviewer_token=config["EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN"],
            case_name=case_name,
            plan=work_plan,
            review_verdict=(
                "changes_requested" if boundary.name == "repair" else "approved"
            ),
        )
    # Rebuild with distinct child ledger sources now that the parent run id is
    # known. The declaration structure is identical, so the persisted manifest
    # digest is unchanged; only the in-memory sources differ.
    work_plan = plan(
        segments=repo / ".exo" / "ledger" / "segments",
        state_root=state_root / run_id,
        ledger_run_id=ledger_run_id,
    )
    marker = root / "crash-traces" / f"{case_name}.jsonl"
    # The leaf actor writes its own file_pr attempts here before calling, so the
    # crash handoff must carry the path before the controller process starts.
    resume_trace = root / "crash-traces" / f"{case_name}.resume.jsonl"
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
            resume_trace,
            boundary.name == "review",
            chainlink_issue_id,
            chainlink_db,
        ),
        name=case_name,
    )
    process.start()
    wait_for_crash(process, marker)
    checkpoint = state_root / run_id / "run.json"
    before_restart = root / "crash-traces" / f"{case_name}.before.json"
    shutil.copy2(checkpoint, before_restart)
    result = resume(
        run_id,
        state_root,
        repo,
        ledger_run_id,
        resume_trace,
        chainlink_issue_id,
        chainlink_db,
    )
    after_restart = root / "crash-traces" / f"{case_name}.after.json"
    shutil.copy2(checkpoint, after_restart)
    after_document = json.loads(checkpoint.read_text(encoding="utf-8"))
    final_state = real.RunStore(run_id, state_root).load()
    if final_state.fsm.phase is not real.TLPhase.TLDone:
        raise AcceptanceError(f"{case_name} did not converge to TLDone")
    if boundary.name in {"publication", "aggregate_publication"}:
        _assert_nested_aggregate_pr(config, forgejo_url, repo, case_name, state_root)
    identity = assert_crash_record(marker, boundary.name, boundary.point)
    counts = assert_recursive_effect_cardinality(state_root / run_id)
    assert_checkpoint_progression([before_restart, after_restart])
    assert_remote_ancestry(
        after_document,
        workspace=repo,
        remote=config["EXOMONAD_FORGEJO_E2E_GIT_REMOTE"],
        remote_branch="main",
    )
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
    operation_totals: dict[str, int] = {}
    for repetition in range(1, _server_repetitions() + 1):
        for boundary in CRASH_BOUNDARIES:
            # `dir` is pinned: a case directory holds the server's Unix
            # socket, and `tempfile` would otherwise honour a caller's TMPDIR,
            # which can be long enough that the socket path does not fit.
            with tempfile.TemporaryDirectory(
                prefix=f"exomonad-1057-run{repetition}-{boundary.name}-{boundary.point}-",
                dir=cl.TEMP_ROOT,
            ) as raw:
                root = Path(raw)
                repo, _, _ = real.clone_external_fixture(root)
                case_name = _case_name(root, boundary)
                # A fresh database inside this case's own directory: the
                # operator's database is never read, so the case cannot depend
                # on a row some other run left behind.
                database = chainlink_db.create(root)
                issue_id = _create_fixture_issue(database, case_name)
                server = None
                try:
                    server, _ = real.start_server(
                        root,
                        repo,
                        config["EXOMONAD_FORGEJO_E2E_URL"],
                        PROJECT_ROOT,
                        forgejo_token=config["EXOMONAD_FORGEJO_E2E_TOKEN"],
                        forgejo_reviewer_token=config[
                            "EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN"
                        ],
                        identity_agents=_identity_agents(plan()),
                        leaf_branches=_leaf_branches(plan()),
                        chainlink_db=database,
                    )
                    result = run_case(
                        root,
                        repo,
                        config["EXOMONAD_FORGEJO_E2E_URL"],
                        config,
                        boundary,
                        issue_id,
                        database,
                    )
                    result["server_run"] = repetition
                    results.append(result)
                    for operation, count in result["journal_operations"].items():
                        operation_totals[operation] = (
                            operation_totals.get(operation, 0) + count
                        )
                finally:
                    try:
                        if server is not None:
                            real.stop_server(
                                server, repo, "#1057 real-server acceptance"
                            )
                        real.cleanup_external_case(
                            repo,
                            config["EXOMONAD_FORGEJO_E2E_URL"],
                            config["EXOMONAD_FORGEJO_E2E_OWNER"],
                            config["EXOMONAD_FORGEJO_E2E_REPO"],
                            config["EXOMONAD_FORGEJO_E2E_TOKEN"],
                            case_name,
                        )
                    finally:
                        _cleanup_fixture_issue(database, issue_id)
    assert_required_effects(operation_totals)
    return {
        "passed": True,
        "server_runs": results,
        "operation_totals": dict(sorted(operation_totals.items())),
    }


if __name__ == "__main__":
    print(json.dumps(run_matrix(), indent=2, sort_keys=True))
