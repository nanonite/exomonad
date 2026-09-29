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
from run_prefix import LEAF_SLICE, LEGS, PREFIX, SEED_ISSUES  # noqa: E402
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
    "no_terminal_failure",
    "review",
    "ci",
    "leaf_handoff",
    "exactly_once_escalation",
)

#: The control leg (#1138 step 3): the same checks against a plain dispatch,
#: with no recreate and no second publication. The items that only describe a
#: recreate are not part of it, and every item it does run asserts on the one
#: PR that dispatch published.
CONTROL_ITEMS = (
    "publish_pr_a",
    "no_orphaned_branches",
    "no_terminal_failure",
    "review",
    "ci",
    "leaf_handoff",
    "exactly_once_escalation",
)

#: The items each leg runs. ``child`` is the #1112 shape: the same recreate
#: scenario with the leaf under a child sub-TL (#1138 step 4).
ITEMS_BY_LEG = {
    "recreate": ITEMS,
    "control": CONTROL_ITEMS,
    "child": ITEMS,
}

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
    items: tuple[str, ...] = ITEMS

    @property
    def passed(self) -> bool:
        return (
            all(self.results.get(item) == "PASS" for item in self.items)
            and not self.leaks
            and not self.cleanup_problems
            and not self.sweep_problems
        )

    def emit(self) -> None:
        """Print one line per item, then any leak, then the verdict."""
        for item in self.items:
            status = self.results.get(item, "SKIP")
            detail = json.dumps(self.evidence.get(item, {}), sort_keys=True, default=str)
            print(f"{status} {item} {detail[:4000]}")
        for problem in self.cleanup_problems:
            print(f"FAIL CLEANUP {problem}")
        for problem in self.sweep_problems:
            print(f"FAIL SWEEP {problem}")
        for leak in self.leaks:
            print(f"FAIL LEAK {leak}")
        passed = sum(1 for item in self.items if self.results.get(item) == "PASS")
        failed = sum(1 for item in self.items if self.results.get(item) == "FAIL")
        skipped = sum(1 for item in self.items if item not in self.results)
        verdict = "PASS" if self.passed else "FAIL"
        print(
            f"{verdict} recursive-crash-convergence: {passed}/{len(self.items)} items "
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

    def __init__(self, project: Project, leg: str = "recreate") -> None:
        self.project = project
        self.leg = leg
        self.items = ITEMS_BY_LEG[leg]
        self.results: dict[str, str] = {}
        self.evidence: dict[str, Any] = {}
        self.pr_a: int | None = None
        self.pr_a_head: str | None = None
        self.pr_b: int | None = None
        self.pr_b_head: str | None = None
        self.dispatch_generation: int | None = None
        self.controller_epoch: str | None = None

    def steps(self) -> list[tuple[str, Callable[[], Any]]]:
        selected = [
            ("publish_pr_a", self.publish_pr_a),
            ("confirmed_recreate", self.confirmed_recreate),
            ("new_dispatch", self.new_dispatch),
            ("publish_pr_b", self.publish_pr_b),
            ("no_adoption_of_pr_a", self.no_adoption_of_pr_a),
            ("no_orphaned_branches", self.no_orphaned_branches),
            ("no_terminal_failure", self.no_terminal_failure),
            ("review", self.review),
            ("ci", self.ci),
            ("leaf_handoff", self.leaf_handoff),
            ("exactly_once_escalation", self.exactly_once_escalation),
        ]
        return [step for step in selected if step[0] in self.items]

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
        if self.leg == "control":
            # The control leg never recreates, so the dispatch's own PR is the
            # one every later check asserts on.
            self.pr_b = self.pr_a
            self.pr_b_head = self.pr_a_head
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

    def _escalation_records(self) -> list[dict[str, Any]]:
        """Every escalation intent the active run forest holds.

        The intent is written by whichever run owns the parked slice, which is
        the child's own directory when the leaf lives under a child sub-TL.
        """
        records: list[dict[str, Any]] = []
        for directory in sorted(self._active_run_dirs()):
            escalation = directory / "escalations"
            if not escalation.is_dir():
                continue
            records.extend(
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(escalation.glob("intent-*.json"))
            )
        return records

    def _active_run_dirs(self) -> list[Path]:
        """Return every run directory of the active forest, archives excluded."""
        tl_root = self.project.repo / ".exo" / "tl-loop"
        archives = {a.name for a in self.project.archives()}
        directories: list[Path] = []
        for run_json in sorted(tl_root.rglob("run.json")):
            relative = run_json.relative_to(tl_root)
            if len(relative.parts) > 1 and relative.parts[0] in archives:
                continue
            directories.append(run_json.parent)
        return directories

    def _leaf_slice(self) -> dict[str, Any]:
        """The leaf's slice, wherever in the active forest it is persisted.

        A leaf dispatched under a child sub-TL keeps its state in the child's
        own checkpoint, so an assertion about the leaf has to read the whole
        active forest rather than the root document alone.
        """
        found: set[str] = set()
        for document in self._active_documents():
            state = (document.get("slices") or {}).get(LEAF_SLICE)
            if isinstance(state, MappingLike):
                return dict(state)
            found.update((document.get("slices") or {}).keys())
        raise ScenarioError(
            f"no active checkpoint holds the {LEAF_SLICE!r} slice: "
            f"{sorted(found)!r}"
        )

    def _park_rows(self) -> list[dict[str, Any]]:
        return self.project.typed("tl.slice_parked")

    def _database_issues(self) -> tuple[list[dict[str, Any]], set[int]]:
        listed = chainlink_db._chainlink(
            "list", "--json", database=self.project.database
        )
        rows = json.loads(listed)
        rows = rows if isinstance(rows, list) else rows.get("issues", [])
        issues = [row for row in rows if isinstance(row, MappingLike)]
        return issues, {row.get("id") for row in issues}

    def _escalation_diagnostic(self) -> str:
        """Describe everything an escalation could have been recorded in."""
        checkpoint = json.loads(
            (self.project.active_run() / "run.json").read_text(encoding="utf-8")
        )
        slices = {
            name: {
                key: state.get(key)
                for key in (
                    "status",
                    "pr_number",
                    "head_sha",
                    "reviewed_head",
                    "verdict",
                    "publication",
                    "handoff",
                    "park_cause",
                    "park_issue_id",
                    "dispatch_agent_id",
                )
                if key in state
            }
            for name, state in (checkpoint.get("slices") or {}).items()
            if isinstance(state, MappingLike)
        }
        polls = len(self.project.typed("watcher.poll_cycle"))
        return (
            f"intents={self._escalation_records()!r} "
            f"parks={[ (p.get('data') or {}) for p in self._park_rows() ]!r} "
            f"phase={(checkpoint.get('fsm') or {}).get('phase')!r} "
            f"watcher_polls={polls} slices={json.dumps(slices, sort_keys=True, default=str)}"
        )

    def exactly_once_escalation(self) -> dict[str, Any]:
        """One escalation for one park, and never a second.

        The #1112 defect was a *duplicated* escalation, so proving "at most
        once" is not enough: this drives a real one. The leaf's PR is closed on
        the forge, which is an observation only the watcher can make and the
        controller can reconcile into a park. The park must produce exactly one
        escalation intent and exactly one new Chainlink issue in the run's own
        database, and a restart that reconciles the same observation again must
        produce neither a second intent nor a second issue.
        """
        require(self.pr_b is not None, "PR B was never published")
        records = self._escalation_records()
        require(
            not records,
            f"the run had already escalated before the park was induced: {records!r}",
        )
        seeded = set(SEED_ISSUE_IDS)

        # The watcher only reconciles a closed PR it can attribute to this
        # slice, which needs the controller's own publication binding first.
        try:
            await_boundary(
                lambda: (
                    self._leaf_slice()
                    if self._leaf_slice().get("pr_number") == self.pr_b
                    else None
                ),
                description="the controller binding PR B to its slice",
                timeout=600.0,
            )
        except (Timeout, ScenarioError) as error:
            raise ScenarioError(
                f"the controller never bound PR {self.pr_b} to its slice, so a "
                f"closed PR could not be reconciled; {self._escalation_diagnostic()}"
            ) from error

        fj.api(
            "PATCH",
            f"{self.project.instance.repository_api_url()}/pulls/{self.pr_b}",
            token=self.project.instance.author.token,
            payload={"state": "closed"},
        )

        # The run records its escalation intent in two durable phases: the
        # `requested` phase is written *before* the issue is created, and the
        # `created` phase carries the issue id once the run's own database holds
        # it. That split is what makes a crash between the two recoverable, so
        # an intent file appearing proves only that the run wanted an
        # escalation. The boundary therefore waits for a *completed* one. The
        # exactly-once assertions below still read every intent, so a second
        # one is still caught.
        def completed_escalation() -> list[dict[str, Any]] | None:
            completed = [
                record
                for record in self._escalation_records()
                if type(record.get("issue_id")) is int and record["issue_id"] > 0
            ]
            return completed or None

        try:
            await_boundary(
                completed_escalation,
                description=(
                    f"a completed escalation intent for the closed PR #{self.pr_b}"
                ),
                timeout=300.0,
            )
        except Timeout as error:
            raise ScenarioError(
                f"closing PR #{self.pr_b} did not drive an escalation through "
                f"the shipped path; {self._escalation_diagnostic()}"
            ) from error

        records = self._escalation_records()
        require(
            len(records) == 1,
            f"one park produced {len(records)} escalation intents: {records!r}",
        )
        intent = records[0]
        require(
            type(intent.get("issue_id")) is int and intent.get("issue_id") > 0,
            f"the escalation intent carries no issue id: {intent!r}",
        )
        issues, identifiers = self._database_issues()
        require(
            identifiers == seeded | {intent["issue_id"]},
            f"the run's database does not hold its seeded issues plus exactly one "
            f"escalation: seeded={sorted(seeded)} escalation={intent['issue_id']} "
            f"found={sorted(x for x in identifiers if x)}",
        )
        created = next(row for row in issues if row.get("id") == intent["issue_id"])
        require(
            created.get("title") == intent.get("title"),
            f"the issue created is not the one the intent names: "
            f"intent={intent.get('title')!r} issue={created.get('title')!r}",
        )

        parks_before = len(self._park_rows())
        self.project.stop_for_restart()
        output = run_init(self.project, "--continue")
        require_attach_failure(output, ("--continue",))
        try:
            await_boundary(
                lambda: (len(self._park_rows()) > parks_before) or None,
                description="the restarted controller parking the same slice again",
                timeout=300.0,
            )
        except Timeout as error:
            raise ScenarioError(
                f"after restarting, the controller did not reconcile the closed "
                f"PR again; {self._escalation_diagnostic()}"
            ) from error

        after = self._escalation_records()
        require(
            len(after) == 1,
            f"a second reconciliation created another escalation intent: {after!r}",
        )
        issues_after, identifiers_after = self._database_issues()
        require(
            identifiers_after == identifiers,
            f"the restart created another Chainlink issue: "
            f"before={sorted(x for x in identifiers if x)} "
            f"after={sorted(x for x in identifiers_after if x)}",
        )
        keys = {
            (record.get("slice_id"), record.get("cause"), record.get("attempt"))
            for record in after
        }
        require(
            len(keys) == len(after),
            f"the run recorded a duplicate escalation: {after!r}",
        )
        issue_ids = [record.get("issue_id") for record in after if record.get("issue_id")]
        require(
            len(set(issue_ids)) == len(issue_ids),
            f"one Chainlink issue was used for two escalations: {issue_ids!r}",
        )
        unexpected = identifiers_after - seeded - set(issue_ids)
        require(
            not unexpected,
            f"the disposable database holds issues this run did not create: "
            f"{sorted(x for x in unexpected if x)}",
        )
        return {
            "cause": intent.get("cause"),
            "slice_id": intent.get("slice_id"),
            "attempt": intent.get("attempt"),
            "escalations_before_restart": len(records),
            "escalations_after_restart": len(after),
            "escalation_issue_ids": issue_ids,
            "parks_before_restart": parks_before,
            "parks_after_restart": len(self._park_rows()),
            "seeded_issues": sorted(seeded),
            "database_issues": sorted(x for x in identifiers_after if x),
            "issues_after_restart": len(issues_after),
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

    def _handoff_diagnostic(self) -> str:
        """Describe every place a handoff could have been recorded.

        A failing handoff assertion is a claim about the product, so the
        failure carries what was actually there: each active slice's own
        publication fields, and the ledger's rows for the events that would
        carry a handoff.
        """
        slices: dict[str, Any] = {}
        for document in self._active_documents():
            for name, state in (document.get("slices") or {}).items():
                if not isinstance(state, MappingLike):
                    continue
                slices[name] = {
                    key: value
                    for key, value in sorted(state.items())
                    if key
                    in {
                        "status",
                        "pr_number",
                        "head_sha",
                        "handoff",
                        "publication",
                        "dispatch_agent_id",
                        "dispatch_intent_id",
                        "invocation_id",
                        "reviewed_head",
                        "verdict",
                    }
                }
        counts: dict[str, int] = {}
        for event in self.project.ledger():
            event_type = str(event.get("type"))
            if (
                "handoff" in event_type
                or event_type.startswith("pr.")
                or event_type.startswith("tl.review")
            ):
                counts[event_type] = counts.get(event_type, 0) + 1
        events = counts
        return f"slices={json.dumps(slices, sort_keys=True, default=str)[:1500]} events={events}"

    def leaf_handoff(self) -> dict[str, Any]:
        """The controller recorded a durable handoff of PR B to the run.

        This is asserted against the controller's own record and nothing else.
        The publication registry and the ledger's `pr.filed` row prove a
        publication exists; they do not prove the controller took the leaf's
        handoff of it, which is the property this item is about.
        """
        require(self.pr_b is not None, "PR B was never published")

        def handoff_for_pr_b() -> list[dict[str, Any]] | None:
            records = [
                record
                for document in self._active_documents()
                for record in _collect_field(document, "handoff")
            ]
            return [r for r in records if r.get("pr_number") == self.pr_b] or None

        # The controller reduces the publication asynchronously, so this waits
        # for its own record instead of reading once and calling a snapshot the
        # answer. A record that never arrives is the finding.
        try:
            matching = await_boundary(
                handoff_for_pr_b,
                description=f"a controller handoff of PR #{self.pr_b}",
                timeout=600.0,
            )
        except Timeout as error:
            raise ScenarioError(
                f"the controller recorded no handoff of PR #{self.pr_b} to the "
                f"run; {self._handoff_diagnostic()}"
            ) from error
        handoff = matching[0]
        require(
            handoff.get("head_sha") == self.pr_b_head,
            f"the handoff of PR #{self.pr_b} names head "
            f"{handoff.get('head_sha')!r}, not {self.pr_b_head}: {handoff!r}",
        )
        invocation = handoff.get("invocation_id") or handoff.get("invocation")
        require(
            isinstance(invocation, str) and invocation,
            f"the handoff of PR #{self.pr_b} carries no invocation: {handoff!r}",
        )
        return {
            "pr_number": handoff.get("pr_number"),
            "head_sha": handoff.get("head_sha"),
            "invocation": invocation,
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
        reviews = fj.api(
            "GET",
            f"{self.project.instance.repository_api_url()}/pulls/{self.pr_b}/reviews",
            token=self.project.instance.author.token,
        )
        if not isinstance(reviews, list):
            raise ScenarioError(f"review listing is not an array: {reviews!r}")
        approvals = [
            review
            for review in reviews
            if isinstance(review, MappingLike)
            and review.get("state") == "APPROVED"
        ]
        require(
            len(approvals) == 1,
            f"expected exactly one approval on PR #{self.pr_b}, found {approvals!r}",
        )
        approval = approvals[0]
        require(
            (approval.get("user") or {}).get("login")
            == self.project.instance.reviewer.username,
            f"the approval on PR #{self.pr_b} is not by the reviewer account: "
            f"{approval.get('user')!r}",
        )
        require(
            approval.get("commit_id") == self.pr_b_head,
            f"the approval on PR #{self.pr_b} is bound to commit "
            f"{approval.get('commit_id')!r}, not PR B's head {self.pr_b_head}",
        )
        recorded = await_boundary(
            lambda: next(
                (
                    e
                    for e in self.project.typed("pr.review")
                    if e.get("data", {}).get("kind") == "approved"
                    and e.get("data", {}).get("pr_number") == self.pr_b
                ),
                None,
            ),
            description=f"the watcher's approved row for PR #{self.pr_b}",
            timeout=180.0,
        )
        data = recorded.get("data") or {}
        require(
            data.get("verdict") == "approved",
            f"the watcher recorded no approved verdict for PR #{self.pr_b}: {data!r}",
        )
        require(
            data.get("head_sha") == self.pr_b_head
            and data.get("review_head_sha") == self.pr_b_head,
            f"the watcher's approval for PR #{self.pr_b} is not bound to its head "
            f"{self.pr_b_head}: {data!r}",
        )
        require(
            approval.get("id") is not None and data.get("review_id") == approval.get("id"),
            f"the watcher's approval does not name the forge's review: "
            f"review_id={data.get('review_id')!r} forge={approval.get('id')!r}",
        )
        return {
            "pr_number": self.pr_b,
            "head_sha": data.get("head_sha"),
            "verdict": data.get("verdict"),
            "review_id": data.get("review_id"),
            "reviewer": self.project.instance.reviewer.username,
            "reviewer_agent_id": data.get("reviewer_agent_id"),
            "reviewer_identity_unresolved": data.get("reviewer_identity_unresolved"),
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


def walk(project: Project, leg: str = "recreate") -> Scenario:
    """Run every item of this leg, continuing past a failure.

    One failing item must not hide the verdict of the ones after it: the items
    are independent, so every item is attempted, each reports its own verdict,
    and the run's status is the conjunction of all of them.
    """
    state = Scenario(project, leg)
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
    parser.add_argument(
        "--leg",
        choices=LEGS,
        default="recreate",
        help="which acceptance shape to run (default: recreate)",
    )
    arguments = parser.parse_args()
    items = ITEMS_BY_LEG[arguments.leg]

    identifier = run_id()
    root = cl.make_root(cl.TEMP_ROOT, PREFIX)
    scope = cl.RunScope(
        run_id=identifier, root=root, prefix=PREFIX, keep=arguments.keep
    )
    report = Report(items=items)
    report.evidence["run_id"] = identifier
    report.evidence["leg"] = arguments.leg
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
        # The database has to be the one `exomonad init` anchors this run's
        # session to -- `<project>/.chainlink`, the path `build_spawn_env`
        # gives every spawned agent -- and the project does not exist until
        # the scenario clones it. So the path is known before bootstrap, the
        # file is created straight after it, and the escalation the controller
        # writes lands in the database this acceptance reads.
        repo = scope.root / "repo"
        database = chainlink_db.database_path_for(repo)
        project = bootstrap(
            scope, instance, database, session=scope.session_prefix, leg=arguments.leg
        )
        database = chainlink_db.create(root, project_dir=repo)
        SEED_ISSUE_IDS.extend(chainlink_db.seed(database, SEED_ISSUES))
        report.evidence["chainlink_database"] = str(database)
        report.evidence["seeded_issues"] = list(SEED_ISSUE_IDS)
        state = walk(project, arguments.leg)
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
