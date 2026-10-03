"""Run the real-server recursive crash/restart acceptance matrix.

The run owns everything it touches. For each pass it provisions one Forgejo from
the shared template at ``tests/e2e/lib/forgejo/docker-compose.yml`` under its own
compose project and releases it when that pass ends, creates one fresh
repository per case on that instance, seeds a fresh Chainlink database inside
each case's own directory, and registers every tmux server and server process it
starts with its run scope. Nothing outside this worktree's build output and this
run's own temporary directory is read or written, and no operator-supplied forge,
token, or repository is required or consulted.

Each case is independent by construction. Its own repository means its own
branch namespace, so a case cannot observe another case's published branches
and cannot have its own fixture pushes rejected as non-fast-forwards against
them, and its own directory means its own tmux server, socket, database, and
ledger. There is therefore nothing left for a case to clean up: the pass's
compose project and its volume take every repository on that instance with them,
and every other resource lives inside the run directory.

Each pass is independent for the same reason. One Forgejo for a whole run means
one container carries every case of every pass, and on a loaded host the
container is the thing that dies -- taking with it every case that had not run
yet, each of which then reports an identical provisioning refusal of its own. A
forge per pass bounds that loss to one pass, the next pass brings up its own, and
a health check between cases names the instance that stopped answering instead of
letting the report blame the boundaries.

A case is a crash: the controller runs in a child process whose transport dies
at one named effect boundary, and the resumed run picks the next action from
the persisted manifest rather than from external plan input. See
:func:`run_case` for the evidence every case must produce.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
import e2e_harness.forgejo_stack as fj  # noqa: E402
import e2e_harness.tmuxio as tmuxio  # noqa: E402
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
from run_prefix import MATRIX_PREFIX, new_run_id

#: The branch every case clones and asserts remote ancestry against. The run's
#: repositories are created with this default branch, so it is never a choice a
#: case has to be told about.
BASE_BRANCH = "main"

#: How many passes of the whole matrix an acceptance run makes. One pass proves
#: a boundary converged once; three prove it converges every time.
DEFAULT_REPETITIONS = 3

#: The identity the case clones commit as. It is a fixture identity, not a
#: person: nothing in the run is attributed to whoever is running it.
GIT_USER_NAME = "recursive-crash-e2e"
GIT_USER_EMAIL = "recursive-crash-e2e@example.invalid"

#: The path of the repository inside a case's own directory.
CASE_REPOSITORY = "repo"

#: Everything the matrix can fail with. A run that fails for any other reason is
#: a harness fault, not a verdict, so it is reported as one.
ACCEPTANCE_FAILURES = (
    AcceptanceError,
    fj.ForgejoError,
    chainlink_db.ChainlinkError,
    cl.CleanupError,
    real.HarnessError,
    tmuxio.TmuxError,
    OSError,
)


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


@dataclass
class MatrixReport:
    """The matrix run's verdict: one entry per case, plus the teardown checks."""

    cases: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    cleanup_problems: list[str] = field(default_factory=list)
    sweep_problems: list[str] = field(default_factory=list)
    #: Passes abandoned because their own Forgejo stopped answering. Reported
    #: against the instance rather than duplicated onto every case after it, so
    #: one dead container reads as one incident.
    forge_problems: list[str] = field(default_factory=list)
    effect_problem: str | None = None
    operation_totals: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not (
            self.failures
            or self.effect_problem
            or self.leaks
            or self.cleanup_problems
            or self.sweep_problems
            or self.forge_problems
        )

    def emit(self) -> None:
        """Print one line per case, then any leak, then the verdict."""
        for case in self.cases:
            detail = json.dumps(case, sort_keys=True, default=str)
            print(f"PASS {case['case']} {detail[:2000]}")
        for problem in self.cleanup_problems:
            print(f"FAIL CLEANUP {problem}")
        for problem in self.sweep_problems:
            print(f"FAIL SWEEP {problem}")
        for problem in self.forge_problems:
            print(f"FAIL FORGEJO {problem}")
        for leak in self.leaks:
            print(f"FAIL LEAK {leak}")
        if self.effect_problem:
            print(f"FAIL EFFECTS {self.effect_problem}")
        for failure in self.failures:
            print(f"FAIL CASE {failure}")
        verdict = "PASS" if self.passed else "FAIL"
        print(
            f"{verdict} recursive-crash-convergence matrix: {len(self.cases)} "
            f"cases passed, {len(self.failures)} failed, {len(self.leaks)} leaks, "
            f"{len(self.cleanup_problems)} cleanup problems, "
            f"{len(self.sweep_problems)} sweep problems, "
            f"{len(self.forge_problems)} forge problems"
        )


# --------------------------------------------------------------------------
# Case identity
# --------------------------------------------------------------------------


def _case_name(repetition: int, boundary: CrashBoundary) -> str:
    """Return the name that identifies one case inside the whole run.

    It names the seeded aggregate branches, the controller state root, and the
    durable markers a case writes, so it has to be distinct for every
    (pass, boundary) pair rather than merely for every boundary.
    """
    return f"crash-r{repetition}-{boundary.name}-{boundary.point}"


def _repository_name(case_name: str) -> str:
    """Return a repository name Forgejo will accept for one case.

    Deriving it from the case name keeps it unique inside the run's own
    instance without a counter a report would have to carry, and strips the
    underscores a boundary name carries so the name is one Forgejo accepts.
    """
    name = re.sub(r"[^a-z0-9-]+", "-", case_name.lower()).strip("-")
    if not name:
        raise AcceptanceError(f"case name has no usable repository name: {case_name!r}")
    return name


# --------------------------------------------------------------------------
# The case's own resources
# --------------------------------------------------------------------------


def _case_directory(run_root: Path, repetition: int, boundary: CrashBoundary) -> Path:
    """Create this case's own directory inside the run directory.

    Nesting under the run root is what keeps the case inside everything the
    scope tears down and sweeps: a case directory under the temp root instead
    is a directory no prefix-driven sweep of this harness names. The name stays
    short because the case holds a Unix socket, and ``tmuxio.socket_path``
    refuses a path the kernel could not bind.
    """
    root = run_root / f"c{repetition}-{boundary.name}-{boundary.point}"
    root.mkdir(parents=True)
    return root


def _clone_case_repository(root: Path, instance: fj.Instance) -> Path:
    """Clone the case's own repository and give the clone its credential.

    The clone is the only local repository the case touches, and the credential
    is scoped to the run's own forge in the clone's own config, so a push can
    only ever reach a repository this run created.
    """
    repo = root / CASE_REPOSITORY
    real.run_command(["git", "clone", "--quiet", instance.clone_url(), str(repo)])
    real.git(repo, "config", "user.name", GIT_USER_NAME)
    real.git(repo, "config", "user.email", GIT_USER_EMAIL)
    real.git(
        repo, "config", instance.extra_header_key(), instance.author.extra_header()
    )
    real.git(repo, "switch", "--quiet", BASE_BRANCH)
    return repo


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
    """Create the one issue the case's controller closes when it finishes.

    The database is this case's own, created by ``chainlink init`` inside this
    case's own directory, so the row is the case's own evidence and disappears
    with the directory. Nothing else is created and nothing else is read: a
    case that later finds this issue still open failed its ``issue_close``
    boundary, which is why the run no longer closes the row on the way out.
    """
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


# --------------------------------------------------------------------------
# Plan identities
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


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
    instance: fj.Instance,
    repo: Path,
    case_name: str,
    state_root: Path,
) -> None:
    """Prove production created the nested aggregate against its direct parent."""
    marker = repo / ".exo" / f"1057-nested-baseline-heads-{case_name}.json"
    expected_pr, expected_head, _ = _nested_aggregate_evidence(state_root, marker)
    pulls = real.json_request(
        "GET",
        f"{instance.repository_api_url()}/pulls?state=all&limit=100",
        token=instance.author.token,
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


def _seed_case_run(
    instance: fj.Instance,
    root: Path,
    repo: Path,
    work_plan: real.WorkPlan,
    boundary: CrashBoundary,
    case_name: str,
) -> tuple[str, real.WorkPlan]:
    """Leave the case's controller state at the boundary it is going to crash at."""
    if boundary.name in {"publication", "aggregate_publication"}:
        return seed_aggregate_publication(root, repo, work_plan, case_name=case_name)
    if boundary.name == "spawn":
        run_id, work_plan, _, _ = real.seed_dispatch_restart_run(
            root, repo, work_plan
        )
        return run_id, work_plan
    seed_boundary = (
        "aggregate_review"
        if boundary.name in {"review", "adoption", "repair"}
        else "merging"
    )
    run_id, work_plan, _, _ = real.seed_delayed_restart_run(
        real.TransportClient(project_root=repo, timeout=10),
        root,
        repo,
        instance.base_url,
        boundary=seed_boundary,
        forgejo_owner=instance.owner,
        forgejo_repo=instance.repo,
        forgejo_token=instance.author.token,
        forgejo_reviewer_token=instance.reviewer.token,
        case_name=case_name,
        plan=work_plan,
        review_verdict=("changes_requested" if boundary.name == "repair" else "approved"),
    )
    return run_id, work_plan


@dataclass
class Crash:
    """Where one case's crash and its restart left their durable evidence.

    The controller is run in a child process that dies at a named effect
    boundary, so the run has to hold the trace, the marker, and the checkpoint
    on both sides of the restart: they are the only records that survive the
    process whose convergence they are about to judge.
    """

    run_id: str
    state_root: Path
    ledger_run_id: str
    marker: Path
    resume_trace: Path
    before_restart: Path
    after_restart: Path

    @property
    def checkpoint(self) -> Path:
        return self.state_root / self.run_id / "run.json"


def run_case(
    root: Path,
    repo: Path,
    instance: fj.Instance,
    boundary: CrashBoundary,
    case_name: str,
    chainlink_issue_id: int,
    chainlink_db: Path,
) -> dict[str, Any]:
    """Crash one case at one boundary, resume it, and return its evidence.

    ``instance`` is scoped to this case's own repository, so every forge read
    here and every assertion below is about this case's state alone.
    """
    state_root = root / "controller-state"
    ledger_run_id = real.server_run_id(repo)
    # Structure-only plan for seeding (sources are not serialized into the
    # manifest, so seeding is unaffected by them).
    work_plan = plan()
    run_id, work_plan = _seed_case_run(
        instance, root, repo, work_plan, boundary, case_name
    )
    # Rebuild with distinct child ledger sources now that the parent run id is
    # known. The declaration structure is identical, so the persisted manifest
    # digest is unchanged; only the in-memory sources differ.
    work_plan = plan(
        segments=repo / ".exo" / "ledger" / "segments",
        state_root=state_root / run_id,
        ledger_run_id=ledger_run_id,
    )
    traces = root / "crash-traces"
    crash = Crash(
        run_id=run_id,
        state_root=state_root,
        ledger_run_id=ledger_run_id,
        marker=traces / f"{case_name}.jsonl",
        # The leaf actor writes its own file_pr attempts here before calling, so
        # the crash handoff must carry the path before the controller starts.
        resume_trace=traces / f"{case_name}.resume.jsonl",
        before_restart=traces / f"{case_name}.before.json",
        after_restart=traces / f"{case_name}.after.json",
    )
    _crash_the_controller(crash, repo, work_plan, boundary, case_name, chainlink_issue_id, chainlink_db)
    result = resume(
        crash.run_id,
        crash.state_root,
        repo,
        crash.ledger_run_id,
        crash.resume_trace,
        chainlink_issue_id,
        chainlink_db,
    )
    shutil.copy2(crash.checkpoint, crash.after_restart)
    return _assert_case_evidence(repo, instance, boundary, case_name, crash, result)


def _crash_the_controller(
    crash: Crash,
    repo: Path,
    work_plan: real.WorkPlan,
    boundary: CrashBoundary,
    case_name: str,
    chainlink_issue_id: int,
    chainlink_db: Path,
) -> None:
    """Run the controller until the injected process death, and snapshot it.

    The crash has to be observed, not waited out: the marker is written by the
    transport immediately before it kills its own process, so the marker and the
    process's death together are the boundary, and a timeout means the boundary
    was never reached.
    """
    process = multiprocessing.get_context("fork").Process(
        target=controller,
        args=(
            crash.run_id,
            crash.state_root,
            repo,
            crash.ledger_run_id,
            work_plan,
            boundary,
            crash.marker,
            crash.resume_trace,
            boundary.name == "review",
            chainlink_issue_id,
            chainlink_db,
        ),
        name=case_name,
    )
    process.start()
    wait_for_crash(process, crash.marker)
    shutil.copy2(crash.checkpoint, crash.before_restart)


def _assert_case_evidence(
    repo: Path,
    instance: fj.Instance,
    boundary: CrashBoundary,
    case_name: str,
    crash: Crash,
    result: Any,
) -> dict[str, Any]:
    """Prove the resumed case converged, and return what it proved."""
    after_document = json.loads(crash.checkpoint.read_text(encoding="utf-8"))
    final_state = real.RunStore(crash.run_id, crash.state_root).load()
    if final_state.fsm.phase is not real.TLPhase.TLDone:
        raise AcceptanceError(f"{case_name} did not converge to TLDone")
    if boundary.name in {"publication", "aggregate_publication"}:
        _assert_nested_aggregate_pr(instance, repo, case_name, crash.state_root)
    identity = assert_crash_record(crash.marker, boundary.name, boundary.point)
    counts = assert_recursive_effect_cardinality(crash.state_root / crash.run_id)
    assert_checkpoint_progression([crash.before_restart, crash.after_restart])
    assert_remote_ancestry(
        after_document,
        workspace=repo,
        remote=instance.clone_url(),
        remote_branch=BASE_BRANCH,
    )
    resumed_calls = assert_resume_not_redispatched(
        crash.resume_trace,
        identity,
        boundary=boundary.name,
        point=boundary.point,
    )
    effects = assert_effect_events(repo, crash.ledger_run_id)
    return {
        "case": case_name,
        "boundary": boundary.name,
        "point": boundary.point,
        "effect_identity": identity,
        "journal_operations": counts,
        "merge_effects": effects,
        "state_version": result.final_state.state_version,
        "cursor": result.final_state.events.last_consumed_offset,
        "resumed_same_effect_calls": resumed_calls,
    }


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def _run_case(
    scope: cl.RunScope,
    instance: fj.Instance,
    run_root: Path,
    repetition: int,
    boundary: CrashBoundary,
) -> dict[str, Any]:
    """Give one case its own forge state, its own server, and its own directory."""
    root = _case_directory(run_root, repetition, boundary)
    case_name = _case_name(repetition, boundary)
    repository = fj.create_repository(instance, _repository_name(case_name))
    repo = _clone_case_repository(root, repository)
    # The database goes where the shipped controller resolves it for this
    # project -- ``<repo>/.chainlink/issues.db``. ``exomonad init`` anchors
    # ``CHAINLINK_DB`` there and ``build_spawn_env`` does the same for every
    # agent it spawns, so a database anywhere else is one the controller never
    # writes an escalation to, and an absent one kills it mid-park.
    database = chainlink_db.create(root, project_dir=repo)
    issue_id = _create_fixture_issue(database, case_name)
    server, _ = real.start_server(
        root,
        repo,
        repository.base_url,
        PROJECT_ROOT,
        forgejo_token=repository.author.token,
        forgejo_reviewer_token=repository.reviewer.token,
        forgejo_owner=repository.owner,
        forgejo_repo=repository.repo,
        identity_agents=_identity_agents(plan()),
        leaf_branches=_leaf_branches(plan()),
        chainlink_db=database,
    )
    scope.track_process(server, f"#1057 {case_name} server")
    scope.track_tmux_server(tmuxio.socket_path(root))
    failure: BaseException | None = None
    result: dict[str, Any] | None = None
    try:
        result = run_case(
            root, repo, repository, boundary, case_name, issue_id, database
        )
    except BaseException as error:  # noqa: BLE001 - released, then re-raised
        failure = error
    _release_case(scope, server, repo, root, case_name, failure)
    if failure is not None:
        raise failure
    assert result is not None
    result["server_run"] = repetition
    return result


def _release_case(
    scope: cl.RunScope,
    server: Any,
    repo: Path,
    root: Path,
    case_name: str,
    failure: BaseException | None,
) -> None:
    """Stop this case's server and remove its directory, keeping the first failure.

    A teardown that raised here would replace the failure the case actually hit,
    and the check it raises on -- the Codex sentinel -- is the one most likely to
    trip on a host another harness is also writing to, so the case's real
    diagnosis would be the one thing lost.
    """
    try:
        real.stop_server(server, repo, f"#1057 {case_name} acceptance")
    except BaseException as error:  # noqa: BLE001 - reported or raised, never lost
        if failure is None:
            raise
        print(f"FAIL TEARDOWN {case_name}: {type(error).__name__}: {error}", flush=True)
    finally:
        # The scope's teardown removes the run directory anyway; this only keeps
        # one case's server log and ledger out of the next case's run.
        if not scope.keep:
            shutil.rmtree(root, ignore_errors=True)


def run_matrix(
    repetitions: int = DEFAULT_REPETITIONS, keep: bool = False
) -> MatrixReport:
    """Run every boundary of the matrix and return the run's verdict.

    The run is self-contained: it sweeps what an interrupted predecessor left,
    provisions its own forge for each pass, gives every case its own repository,
    database, directory, and server, and finally tears all of that down and fails
    if any of it outlived the run.
    """
    validate_matrix()
    if repetitions <= 0:
        raise AcceptanceError(f"the matrix needs at least one pass, not {repetitions}")
    report = MatrixReport()
    identifier = new_run_id()
    run_root = cl.make_root(cl.TEMP_ROOT, MATRIX_PREFIX)
    scope = cl.RunScope(
        run_id=identifier, root=run_root, prefix=MATRIX_PREFIX, keep=keep
    )
    report.evidence["run_id"] = identifier
    report.evidence["run_directory"] = str(run_root)
    report.evidence["repetitions"] = repetitions

    cl.install_trap(scope)
    try:
        swept = cl.sweep_stale(
            MATRIX_PREFIX, fj.template_path(PROJECT_ROOT), MATRIX_PREFIX
        )
        report.sweep_problems = swept
        report.evidence["swept_before_run"] = swept
        _walk(scope, run_root, repetitions, identifier, report)
    except ACCEPTANCE_FAILURES as error:
        report.failures.append(f"setup: {type(error).__name__}: {error}")
        print(f"FAIL SETUP {report.failures[-1]}", flush=True)
    except BaseException as error:  # noqa: BLE001 - the run still owes its report
        report.failures.append(f"harness: {type(error).__name__}: {error}")
        print(f"FAIL HARNESS {report.failures[-1]}", flush=True)
    finally:
        # The report is owed even when teardown misbehaves, so nothing here may
        # raise. Teardown removes each compose project and its volume, which is
        # what takes every case's repository with it, so there is no per-record
        # cleanup left to fail.
        try:
            report.cleanup_problems = scope.teardown()
        except BaseException as error:  # noqa: BLE001 - reported, never raised
            report.cleanup_problems = [f"teardown raised: {type(error).__name__}: {error}"]
        try:
            report.leaks = scope.leaks()
        except BaseException as error:  # noqa: BLE001 - reported, never raised
            report.leaks = [f"leak check raised: {type(error).__name__}: {error}"]
        if keep:
            print(
                f"KEPT {run_root} (per-pass compose projects still up: "
                f"{', '.join(_forge_projects(report)) or 'none'})",
                flush=True,
            )
    return report


def _forge_projects(report: MatrixReport) -> list[str]:
    """Return the compose projects this run brought up, one per pass.

    Read with a default rather than by key: a run that never reached its first
    pass -- or that kept its state for inspection with nothing provisioned --
    still owes the operator that line.
    """
    forges = report.evidence.get("forges", ())
    return [str(entry["compose_project"]) for entry in forges]


def _walk(
    scope: cl.RunScope,
    run_root: Path,
    repetitions: int,
    run_id: str,
    report: MatrixReport,
) -> None:
    """Walk every pass, each against a Forgejo this run brought up for it.

    One Forgejo per pass, released with ``down -v`` when the pass ends, rather
    than one instance for all of them. A pass walks every boundary before and after
    each operation -- 28 cases that each push, review, and merge against the same
    container -- and asking one instance to carry every pass of a run is what made
    a container killed by a loaded host take down the cases that had not run yet:
    the run then read as twenty connection-refused boundary verdicts instead of one
    dead resource. With a per-pass instance the loss is bounded to a pass, the next
    pass provisions its own, and the run says which instance died.

    A failing case still does not end its pass: the cases are independent, so
    every case is attempted, each reports its own verdict, and the run's status
    is the conjunction of all of them. A pass whose *instance* is gone is the one
    exception, because every remaining case in it would fail identically for a
    reason that is not about the boundary it is exercising.
    """
    totals: dict[str, int] = {}
    for repetition in range(1, repetitions + 1):
        instance = _forge_for_pass(scope, run_id, repetition, report)
        if instance is None:
            continue
        try:
            _walk_pass(scope, instance, run_root, repetition, report, totals)
        finally:
            _release_pass(scope, instance, repetition)
    report.operation_totals = dict(sorted(totals.items()))
    try:
        assert_required_effects(report.operation_totals)
    except AcceptanceError as error:
        report.effect_problem = str(error)


def _forge_for_pass(
    scope: cl.RunScope,
    run_id: str,
    repetition: int,
    report: MatrixReport,
) -> fj.Instance | None:
    """Bring up this pass's own Forgejo and record what it is.

    A pass whose instance cannot be provisioned reports against the pass and
    returns nothing, so the walk moves on to the next one instead of ending the
    run: the passes are independent, and a pass that never got a forge has
    attempted no case that could have succeeded.
    """
    batch = _batch_label(repetition)
    try:
        instance = fj.provision(scope, PROJECT_ROOT, run_id, batch=batch)
    except fj.ForgejoError as error:
        report.forge_problems.append(
            f"pass {repetition} has no Forgejo: {type(error).__name__}: {error}; "
            f"its {len(CRASH_BOUNDARIES)} cases were not attempted"
        )
        print(f"FAIL FORGEJO {report.forge_problems[-1][:2000]}", flush=True)
        return None
    report.evidence.setdefault("forges", []).append(
        {
            "pass": repetition,
            "compose_project": instance.project,
            "discovered_host": instance.host,
            "admin": instance.admin_username,
            "owner": instance.owner,
        }
    )
    return instance


def _batch_label(repetition: int) -> str:
    """Return the compose-name suffix that distinguishes one pass's instance.

    The label is the pass number, so the compose project names both the run and
    the pass: ``exo-e2e-1057-<run id>forgejo-p2`` says which run brought it up
    and which pass it served, which is what makes a leftover from a run killed
    mid-pass attributable after the fact.
    """
    return f"p{repetition}"


def _release_pass(
    scope: cl.RunScope,
    instance: fj.Instance,
    repetition: int,
) -> None:
    """Give this pass's instance back, and say so if it will not go.

    The instance is released whether the pass passed, failed, or was abandoned,
    because the run scope still owns it until something removes it. A refusal is
    printed here rather than appended to this report: ``fj.release`` already
    recorded it on the scope, so teardown reports it against the project it
    names, and a refused release that appeared under two headings would read as
    two failures. Raising is not an option either -- the run still owes the report
    for every pass it walked, and a teardown that raised here would replace the
    verdict of the pass that just finished.
    """
    problems = fj.release(scope, instance)
    if problems:
        print(
            f"FAIL FORGEJO pass {repetition} left its Forgejo "
            f"{instance.project!r} behind: {'; '.join(problems)[:1500]}",
            flush=True,
        )


def _walk_pass(
    scope: cl.RunScope,
    instance: fj.Instance,
    run_root: Path,
    repetition: int,
    report: MatrixReport,
    totals: dict[str, int],
) -> None:
    """Run every case of one pass against that pass's own instance."""
    for index, boundary in enumerate(CRASH_BOUNDARIES):
        case = _case_name(repetition, boundary)
        unattempted = len(CRASH_BOUNDARIES) - index - 1
        if not _forge_answers(instance, case, unattempted, repetition, report):
            return
        try:
            result = _run_case(scope, instance, run_root, repetition, boundary)
        except BaseException as error:  # noqa: BLE001 - one case, then on
            # Every failure is attributed to the case it happened in and the walk
            # continues: an exception the harness does not recognise is still one
            # case's verdict, and stopping there would report every later boundary
            # as untried when it was merely unattempted.
            report.failures.append(f"{case}: {type(error).__name__}: {error}")
            print(f"FAIL {case} {report.failures[-1][:2000]}", flush=True)
            continue
        report.cases.append(result)
        for operation, count in result["journal_operations"].items():
            totals[operation] = totals.get(operation, 0) + count
        print(
            f"PASS {result['case']} "
            f"{json.dumps(result, sort_keys=True, default=str)[:2000]}",
            flush=True,
        )


def _forge_answers(
    instance: fj.Instance,
    case: str,
    unattempted: int,
    repetition: int,
    report: MatrixReport,
) -> bool:
    """Return whether this pass's instance is still there, naming it if not.

    The check is between cases, because that is where it can change the report:
    the instance was answering when the previous case finished, so a refusal here
    is one incident rather than the fault of the case that happened to come next.
    The failure names the compose project and the host, and says how many cases
    the pass did not attempt, so a lost instance cannot be read as twenty
    boundary verdicts.
    """
    try:
        fj.assert_answering(instance)
    except fj.ForgejoError as error:
        report.forge_problems.append(
            f"pass {repetition} abandoned before {case}: {error}; "
            f"{unattempted} further case(s) of this pass were not attempted"
        )
        print(f"FAIL FORGEJO {report.forge_problems[-1][:2000]}", flush=True)
        return False
    return True
