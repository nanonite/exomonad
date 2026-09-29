"""Run the #1117 recreated-publication acceptance and report one line per item.

The driver owns the run's lifetime: it creates the run scope and its temporary
directory, installs the one trap that tears the run down, provisions the run's
own Forgejo and its own Chainlink database, then walks the scenario's items.
Each item prints exactly one ``PASS`` or ``FAIL`` line naming the evidence it
used, and the run's exit status is the conjunction of every item, the teardown,
and the post-teardown leak check.

The teardown runs on every exit path, including a failed item, an exception, and
a signal, and anything it cannot remove is reported as a failure rather than as
a warning.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: Where the harness reads the shared artifacts from.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = PROJECT_ROOT / "tests" / "e2e" / "lib"
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_harness.chainlink_db as chainlink_db  # noqa: E402
import e2e_harness.cleanup as cl  # noqa: E402
import e2e_harness.forgejo_stack as fj  # noqa: E402
from e2e_harness.waiter import Timeout, await_boundary  # noqa: E402
from run_prefix import LEAF_SLICE, PREFIX, SEED_ISSUES  # noqa: E402
from scenario import (  # noqa: E402
    Project,
    ScenarioError,
    bootstrap,
    require,
    leaf_worktree_status,
    require_attach_failure,
    run_init,
)

#: The items, in the order the scenario states them.
ITEMS = (
    "publish_pr_a",
    "confirmed_recreate",
    "new_dispatch",
    "publish_pr_b",
    "no_adoption_of_pr_a",
    "no_orphaned_branches",
    "exactly_once_escalation",
    "no_terminal_failure",
    "leaf_handoff",
    "review",
    "ci",
)

#: Everything the acceptance can fail with. A run that fails for any other
#: reason is a harness fault, not a verdict, so it is reported as such.
ACCEPTANCE_FAILURES = (
    ScenarioError,
    Timeout,
    fj.ForgejoError,
    chainlink_db.ChainlinkError,
    cl.CleanupError,
    OSError,
)


@dataclass
class Report:
    """The run's verdict: one entry per item, plus the leak check."""

    results: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    leaks: list[str] = field(default_factory=list)
    cleanup_problems: list[str] = field(default_factory=list)
    sweep_problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            all(self.results.get(item) == "PASS" for item in ITEMS)
            and not self.leaks
            and not self.cleanup_problems
            and not self.sweep_problems
        )

    def emit(self) -> None:
        """Print one line per item, then any leak, then the verdict."""
        for item in ITEMS:
            status = self.results.get(item, "SKIP")
            detail = json.dumps(self.evidence.get(item, {}), sort_keys=True, default=str)
            print(f"{status} {item} {detail[:4000]}")
        for problem in self.cleanup_problems:
            print(f"FAIL CLEANUP {problem}")
        for problem in self.sweep_problems:
            print(f"FAIL SWEEP {problem}")
        for leak in self.leaks:
            print(f"FAIL LEAK {leak}")
        passed = sum(1 for item in ITEMS if self.results.get(item) == "PASS")
        failed = sum(1 for item in ITEMS if self.results.get(item) == "FAIL")
        skipped = sum(1 for item in ITEMS if item not in self.results)
        verdict = "PASS" if self.passed else "FAIL"
        print(
            f"{verdict} recursive-crash-convergence: {passed}/{len(ITEMS)} items "
            f"passed, {failed} failed, {skipped} not reached, "
            f"{len(self.leaks)} leaks, "
            f"{len(self.cleanup_problems)} cleanup problems, "
            f"{len(self.sweep_problems)} sweep problems"
        )


def run_id() -> str:
    """Return a run id unique to this invocation.

    It becomes the compose project name, the tmux session prefix, the Forgejo
    account and repository names, and the temporary directory, so two runs can
    never reach each other's state. The length is bounded because the tmux
    session name is derived from it and the server keeps only the first 36
    characters of that name.
    """
    return secrets.token_hex(4)


class Scenario:
    """The items in order, each keeping what the later ones assert on."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.results: dict[str, str] = {}
        self.evidence: dict[str, Any] = {}
        self.pr_a: int | None = None
        self.pr_a_head: str | None = None
        self.pr_b: int | None = None
        self.pr_b_head: str | None = None
        self.dispatch_generation: int | None = None
        self.controller_epoch: str | None = None

    def steps(self) -> list[tuple[str, Callable[[], Any]]]:
        return [
            ("publish_pr_a", self.publish_pr_a),
            ("confirmed_recreate", self.confirmed_recreate),
            ("new_dispatch", self.new_dispatch),
            ("publish_pr_b", self.publish_pr_b),
            ("no_adoption_of_pr_a", self.no_adoption_of_pr_a),
            ("no_orphaned_branches", self.no_orphaned_branches),
            ("exactly_once_escalation", self.exactly_once_escalation),
            ("no_terminal_failure", self.no_terminal_failure),
            ("leaf_handoff", self.leaf_handoff),
            ("review", self.review),
            ("ci", self.ci),
        ]

    # -- helpers ----------------------------------------------------------

    def _pulls(self) -> list[MappingLike]:
        listed = fj.api(
            "GET",
            f"{self.project.instance.repository_api_url()}/pulls?state=all&limit=100",
            token=self.project.instance.author.token,
        )
        if not isinstance(listed, list):
            raise ScenarioError(f"pull listing is not an array: {listed!r}")
        return [pull for pull in listed if isinstance(pull, MappingLike)]

    def _open_pulls(self) -> list[MappingLike]:
        return [pull for pull in self._pulls() if pull.get("state") == "open"]

    def _find(self, number: int) -> MappingLike:
        for pull in self._pulls():
            if pull.get("number") == number:
                return pull
        raise ScenarioError(f"pull request #{number} is gone")

    def _branch_of(self, pull: MappingLike) -> str:
        head = pull.get("head")
        branch = head.get("ref") if isinstance(head, MappingLike) else None
        if not isinstance(branch, str) or not branch:
            raise ScenarioError(f"pull request has no head branch: {pull!r}")
        return branch

    def _head_of(self, pull: MappingLike) -> str:
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, MappingLike) else None
        if not isinstance(sha, str) or not sha:
            raise ScenarioError(f"pull request has no head sha: {pull!r}")
        return sha

    def _active_documents(self) -> list[Any]:
        """Return every checkpoint of the *active* run forest, archives excluded.

        The leaf's publication state is not necessarily on the root checkpoint:
        a dispatched slice keeps its own scope, so an assertion about what the
        run currently owns has to read the whole active forest and ignore only
        the archived generations.
        """
        tl_root = self.project.repo / ".exo" / "tl-loop"
        archives = {a.name for a in self.project.archives()}
        documents: list[Any] = []
        for path in sorted(tl_root.rglob("run.json")):
            relative = path.relative_to(tl_root)
            if len(relative.parts) > 1 and relative.parts[0] in archives:
                continue
            try:
                documents.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return documents

    def _published_registry(self) -> list[Any]:
        """Return the project-level publication registry's entries."""
        path = self.project.repo / ".exo" / "published-heads.json"
        if not path.is_file():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
        heads = value.get("heads") if isinstance(value, dict) else None
        return [entry for entry in heads if isinstance(entry, dict)] if isinstance(heads, list) else []

    def _epoch(self) -> str:
        path = self.project.active_run().parent / "root.controller-epoch"
        if not path.is_file():
            raise ScenarioError(f"the controller epoch is missing: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ScenarioError(f"the controller epoch is empty: {path}")
        return value

    # -- items ------------------------------------------------------------

    def publish_pr_a(self) -> dict[str, Any]:
        """The shipped controller publishes PR A on the disposable forge."""
        output = run_init(self.project, "--start")
        require_attach_failure(output, ("--start",))
        leaf_branch = self.project.leaf_branch
        pull = await_boundary(
            lambda: next(
                (
                    p
                    for p in self._open_pulls()
                    if self._branch_of(p) == leaf_branch
                ),
                None,
            ),
            description=f"an open pull request on {leaf_branch}",
            timeout=300.0,
        )
        self.pr_a = int(pull["number"])
        self.pr_a_head = self._head_of(pull)
        self.controller_epoch = self._epoch()
        filed = self.project.typed("pr.filed")
        require(
            any(e.get("data", {}).get("pr_number") == self.pr_a for e in filed),
            f"the ledger has no pr.filed row for #{self.pr_a}",
        )
        return {
            "pr_number": self.pr_a,
            "head_branch": leaf_branch,
            "head_sha": self.pr_a_head,
            "controller_epoch": self.controller_epoch,
            "ledger_filed": len(filed),
        }

    def confirmed_recreate(self) -> dict[str, Any]:
        """The shipped --recreate --confirm-recreate path disposes PR A."""
        require(self.pr_a is not None, "PR A was never published")
        status = leaf_worktree_status(self.project)
        output = run_init(self.project, "--recreate", "--confirm-recreate")
        try:
            require_attach_failure(output, ("--recreate", "--confirm-recreate"))
        except ScenarioError as error:
            raise ScenarioError(f"{error}; leaf worktree status: {status}") from None
        archives = self.project.archives()
        require(
            len(archives) == 1,
            f"expected exactly one archived run, found {[a.name for a in archives]}",
        )
        receipts = self.project.repo / ".exo" / "recreate-receipts.json"
        require(receipts.is_file(), f"the recreate wrote no receipts: {receipts}")
        mode = json.loads(
            (self.project.repo / ".exo" / "tl-loop" / "session-mode.json").read_text(
                encoding="utf-8"
            )
        )
        require(
            mode.get("session_mode") == "recreate",
            f"the run was not recorded as recreated: {mode!r}",
        )
        pull = self._find(self.pr_a)
        require(
            pull.get("state") == "closed",
            f"recreate left PR #{self.pr_a} open: {pull.get('state')!r}",
        )
        return {
            "archive": archives[0].name,
            "receipts": json.loads(receipts.read_text(encoding="utf-8")).get(
                "schema_version"
            ),
            "session_mode": mode.get("session_mode"),
            "pr_a_state": pull.get("state"),
        }

    def new_dispatch(self) -> dict[str, Any]:
        """A new dispatch generation follows the recreate."""
        epoch = self._epoch()
        require(
            epoch != self.controller_epoch,
            f"the controller epoch did not change across recreate: {epoch}",
        )
        confirmed = await_boundary(
            lambda: [
                e
                for e in self.project.typed("tl.dispatch_confirmed")
                if e.get("data", {}).get("dispatch_generation", 0) > 0
            ]
            or None,
            description="a dispatch confirmed under a new controller epoch",
            timeout=300.0,
        )
        before = self.controller_epoch
        self.controller_epoch = epoch
        return {
            "controller_epoch_before": before,
            "controller_epoch_after": epoch,
            "dispatch_confirmed": len(confirmed),
            "dispatch_generation": max(
                int(e.get("data", {}).get("dispatch_generation", 0))
                for e in confirmed
            ),
        }

    def publish_pr_b(self) -> dict[str, Any]:
        """The recreated dispatch publishes PR B, and only B is open."""
        require(self.pr_a is not None, "PR A was never published")
        leaf_branch = self.project.leaf_branch
        pull = await_boundary(
            lambda: next(
                (
                    p
                    for p in self._open_pulls()
                    if self._branch_of(p) == leaf_branch
                    and p.get("number") != self.pr_a
                ),
                None,
            ),
            description=f"an open pull request other than #{self.pr_a}",
            timeout=300.0,
        )
        self.pr_b = int(pull["number"])
        self.pr_b_head = self._head_of(pull)
        require(self.pr_b != self.pr_a, "PR B reused PR A's number")
        open_pulls = self._open_pulls()
        require(
            len(open_pulls) == 1,
            f"expected exactly one open pull request, found "
            f"{[p.get('number') for p in open_pulls]}",
        )
        return {
            "pr_a": self.pr_a,
            "pr_b": self.pr_b,
            "pr_b_head": self.pr_b_head,
            "open_pull_requests": len(open_pulls),
        }

    def no_adoption_of_pr_a(self) -> dict[str, Any]:
        """The active run binds PR B; PR A exists only as archived evidence."""
        require(self.pr_b is not None, "PR B was never published")
        bound: set[int] = set()
        for document in self._active_documents():
            bound |= _collect_numbers(document, "pr_number")
        bound |= {
            entry.get("pr_number")
            for entry in self._published_registry()
            if type(entry.get("pr_number")) is int
        }
        require(
            self.pr_b in bound,
            f"the active run does not own PR #{self.pr_b}: {sorted(bound)}",
        )
        require(
            self.pr_a not in bound,
            f"the active run adopted PR #{self.pr_a}: {sorted(bound)}",
        )
        # What survives the recreate is the ledger, not the checkpoint: a
        # publication row is permanent audit evidence and the archived
        # generation's state never carries a bound publication. Retention is
        # therefore proved on the ledger -- PR A's own pr.filed row is still
        # there, and the assertion above proves the active run does not own it.
        filed = {
            e.get("data", {}).get("pr_number")
            for e in self.project.typed("pr.filed")
            if type(e.get("data", {}).get("pr_number")) is int
        }
        require(
            self.pr_a in filed,
            f"PR #{self.pr_a} left no audit row: {sorted(filed)}",
        )
        return {
            "active_bound": sorted(bound),
            "archived_run": self.project.archives()[0].name,
            "audit_rows": sorted(filed),
            "adopted_pr_a": False,
        }

    def no_orphaned_branches(self) -> dict[str, Any]:
        """Every remote branch except the base is still owned by a worktree."""
        branches = fj.api(
            "GET",
            f"{self.project.instance.repository_api_url()}/branches",
            token=self.project.instance.author.token,
        )
        if not isinstance(branches, list):
            raise ScenarioError(f"branch listing is not an array: {branches!r}")
        names = sorted(
            b.get("name") for b in branches if isinstance(b, MappingLike) and b.get("name")
        )
        registered = _registered_branches(self.project)
        orphaned = [name for name in names if name != "main" and name not in registered]
        require(
            not orphaned,
            f"remote branches with no registered worktree: {orphaned!r}",
        )
        return {
            "remote_branches": names,
            "registered_branches": sorted(registered),
            "orphaned": orphaned,
        }

    def exactly_once_escalation(self) -> dict[str, Any]:
        """The seeded issues and the run's escalations are each counted once."""
        intents = sorted(
            (self.project.active_run()).glob("escalations/intent-*.json")
        )
        records = [json.loads(p.read_text(encoding="utf-8")) for p in intents]
        keys = {
            (
                record.get("slice_id"),
                record.get("cause"),
                record.get("attempt"),
            )
            for record in records
        }
        require(
            len(keys) == len(records),
            f"the run recorded a duplicate escalation: {records!r}",
        )
        issue_ids = [r.get("issue_id") for r in records if r.get("issue_id")]
        require(
            len(set(issue_ids)) == len(issue_ids),
            f"one Chainlink issue was used for two escalations: {issue_ids!r}",
        )
        seeded = set(SEED_ISSUE_IDS)
        listed = chainlink_db._chainlink(
            "list", "--json", database=self.project.database
        )
        rows = json.loads(listed)
        rows = rows if isinstance(rows, list) else rows.get("issues", [])
        identifiers = {
            row.get("id") for row in rows if isinstance(row, MappingLike)
        }
        unexpected = identifiers - seeded - set(issue_ids)
        require(
            not unexpected,
            f"the disposable database holds issues this run did not create: "
            f"{sorted(x for x in unexpected if x)}",
        )
        return {
            "escalations": len(records),
            "escalation_issue_ids": sorted(issue_ids),
            "seeded_issues": sorted(seeded),
            "database_issues": sorted(x for x in identifiers if x),
        }

    def no_terminal_failure(self) -> dict[str, Any]:
        """The active run did not end in the failure state the bug produced."""
        checkpoint = json.loads(
            (self.project.active_run() / "run.json").read_text(encoding="utf-8")
        )
        phase = (checkpoint.get("fsm") or {}).get("phase")
        require(
            phase not in {"tl_failed", "TLFailed"},
            f"the recreated run terminally failed at phase {phase!r}",
        )
        return {"phase": phase, "terminal_failure": False}

    def leaf_handoff(self) -> dict[str, Any]:
        """The active run carries handoff evidence for the leaf it owns."""
        require(self.pr_b is not None, "PR B was never published")
        handoffs: list[Any] = []
        for document in self._active_documents():
            handoffs.extend(_collect_field(document, "handoff"))
        numbers = sorted(
            {
                h.get("pr_number")
                for h in handoffs
                if isinstance(h, MappingLike) and h.get("pr_number")
            }
        )
        registry = {
            entry.get("pr_number")
            for entry in self._published_registry()
            if type(entry.get("pr_number")) is int
        }
        filed = {
            e.get("data", {}).get("pr_number")
            for e in self.project.typed("pr.filed")
            if type(e.get("data", {}).get("pr_number")) is int
        }
        require(
            self.pr_b in numbers or (self.pr_b in registry and self.pr_b in filed),
            f"the leaf handed no publication of PR #{self.pr_b} to the run: "
            f"handoff={numbers!r} registry={sorted(registry)} filed={sorted(filed)}",
        )
        return {
            "handoff_pr_numbers": numbers,
            "registry_pr_numbers": sorted(registry),
            "filed_pr_numbers": sorted(filed),
            "leaf": LEAF_SLICE,
        }

    def review(self) -> dict[str, Any]:
        """The harness posts the reviewer's approval and the watcher records it."""
        require(self.pr_b is not None, "PR B was never published")
        fj.api(
            "POST",
            f"{self.project.instance.repository_api_url()}/pulls/{self.pr_b}/reviews",
            token=self.project.instance.reviewer.token,
            payload={"event": "APPROVED", "commit_id": self.pr_b_head},
        )
        recorded = await_boundary(
            lambda: next(
                (
                    e
                    for e in self.project.typed("pr.review")
                    if e.get("data", {}).get("pr_number") == self.pr_b
                ),
                None,
            ),
            description=f"a pr.review row for PR #{self.pr_b}",
            timeout=180.0,
        )
        return {
            "pr_number": self.pr_b,
            "review_state": recorded.get("data", {}).get("review_state"),
            "review_rows": len(self.project.typed("pr.review")),
        }

    def ci(self) -> dict[str, Any]:
        """The harness posts the commit status and the watcher records it."""
        require(self.pr_b_head is not None, "PR B was never published")
        fj.api(
            "POST",
            f"{self.project.instance.repository_api_url()}/statuses/{self.pr_b_head}",
            token=self.project.instance.author.token,
            payload={
                "state": "success",
                "context": "e2e-1117",
                "description": "posted by the acceptance harness",
            },
        )
        recorded = await_boundary(
            lambda: next(
                (
                    e
                    for e in self.project.typed("ci.status_changed")
                    if e.get("data", {}).get("head_sha") == self.pr_b_head
                ),
                None,
            ),
            description="a ci.status_changed row for PR B's head",
            timeout=180.0,
        )
        return {
            "head_sha": self.pr_b_head,
            "status": recorded.get("data", {}).get("status"),
            "ci_rows": len(self.project.typed("ci.status_changed")),
        }


MappingLike = dict  # noqa: E305 - a readable alias for the forge's JSON objects

SEED_ISSUE_IDS: list[int] = []


def _collect_numbers(value: Any, key: str, found: set[int] | None = None) -> set[int]:
    """Collect every integer carried under ``key`` anywhere in a checkpoint."""
    if found is None:
        found = set()
    if isinstance(value, MappingLike):
        for child_key, child in value.items():
            if child_key == key and type(child) is int and child > 0:
                found.add(child)
            else:
                _collect_numbers(child, key, found)
    elif isinstance(value, list):
        for child in value:
            _collect_numbers(child, key, found)
    return found


def _collect_field(value: Any, key: str, found: list[Any] | None = None) -> list[Any]:
    """Collect every value carried under ``key`` anywhere in a checkpoint."""
    if found is None:
        found = []
    if isinstance(value, MappingLike):
        for child_key, child in value.items():
            if child_key == key and isinstance(child, MappingLike):
                found.append(child)
            else:
                _collect_field(child, key, found)
    elif isinstance(value, list):
        for child in value:
            _collect_field(child, key, found)
    return found


def _registered_branches(project: Project) -> set[str]:
    """Return every branch git registers a worktree for in this project."""
    result = subprocess_run(
        ["git", "-C", str(project.repo), "worktree", "list", "--porcelain"]
    )
    branches: set[str] = set()
    for line in result.splitlines():
        if line.startswith("branch "):
            branches.add(line.removeprefix("branch refs/heads/"))
    return branches


def subprocess_run(command: list[str]) -> str:
    import subprocess

    completed = subprocess.run(
        command, text=True, capture_output=True, check=False
    )
    if completed.returncode:
        raise ScenarioError(
            f"{' '.join(command[:4])} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def walk(project: Project) -> Scenario:
    """Run every item, continuing past a failure.

    One failing item must not hide the verdict of the ones after it: the items
    are independent, so every item is attempted, each reports its own verdict,
    and the run's status is the conjunction of all of them.
    """
    state = Scenario(project)
    for item, step in state.steps():
        try:
            result = step()
        except ACCEPTANCE_FAILURES as error:
            state.results[item] = "FAIL"
            state.evidence[item] = {"error": f"{type(error).__name__}: {error}"}
            print(f"FAIL {item} {json.dumps(state.evidence[item])[:4000]}", flush=True)
            continue
        state.results[item] = "PASS"
        state.evidence[item] = result
        print(
            f"PASS {item} {json.dumps(result, sort_keys=True, default=str)[:4000]}",
            flush=True,
        )
    return state


def main() -> int:
    """Run the acceptance and return its exit status."""
    parser = argparse.ArgumentParser(description="Run the #1117 acceptance.")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave this run's Forgejo, session, and directory for inspection",
    )
    arguments = parser.parse_args()

    identifier = run_id()
    root = cl.make_root(cl.TEMP_ROOT, PREFIX)
    scope = cl.RunScope(
        run_id=identifier, root=root, prefix=PREFIX, keep=arguments.keep
    )
    report = Report()
    report.evidence["run_id"] = identifier
    report.evidence["run_directory"] = str(root)
    keep = arguments.keep
    instance: fj.Instance | None = None

    cl.install_trap(scope)
    try:
        swept = cl.sweep_stale(
            PREFIX, fj.template_path(PROJECT_ROOT), PREFIX
        )
        report.sweep_problems = swept
        report.evidence["swept_before_run"] = swept
        instance = fj.provision(scope, PROJECT_ROOT, identifier)
        report.evidence["forgejo"] = {
            "compose_project": instance.project,
            "discovered_host": instance.host,
            "owner": instance.owner,
            "repo": instance.repo,
        }
        database = chainlink_db.create(root)
        SEED_ISSUE_IDS.extend(chainlink_db.seed(database, SEED_ISSUES))
        report.evidence["chainlink_database"] = str(database)
        report.evidence["seeded_issues"] = list(SEED_ISSUE_IDS)
        project = bootstrap(scope, instance, database, session=scope.session_prefix)
        state = walk(project)
        report.results = state.results
        report.evidence.update(state.evidence)
    except ACCEPTANCE_FAILURES as error:
        report.evidence["setup_error"] = f"{type(error).__name__}: {error}"
        print(f"FAIL SETUP {report.evidence['setup_error']}", flush=True)
    except BaseException as error:  # noqa: BLE001 - the run still owes its report
        report.evidence["harness_error"] = f"{type(error).__name__}: {error}"
        print(f"FAIL HARNESS {report.evidence['harness_error']}", flush=True)
    finally:
        try:
            report.cleanup_problems = scope.teardown()
        except BaseException as error:  # noqa: BLE001 - reported, never raised
            report.cleanup_problems = [
                f"teardown raised: {type(error).__name__}: {error}"
            ]
        try:
            report.leaks = scope.leaks()
        except BaseException as error:  # noqa: BLE001 - reported, never raised
            report.leaks = [f"leak check raised: {type(error).__name__}: {error}"]
        if keep:
            print(f"KEPT {root}", flush=True)

    print(json.dumps(report.evidence, indent=2, sort_keys=True, default=str)[:20000])
    report.emit()
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
