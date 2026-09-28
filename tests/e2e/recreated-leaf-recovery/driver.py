"""Run the #1111 recreated-leaf acceptance and report one line per T-item.

The driver owns the run's lifetime: it creates the run scope and its temporary
directory, installs the one trap that tears the run down, provisions the run's
own Forgejo, its Chainlink database, and its project, then walks T1 through T9.
Each T-item prints exactly one ``PASS`` or ``FAIL`` line naming the evidence it
used, and the run's exit status is the conjunction of every item, the teardown,
and the post-teardown leak check.

The teardown runs on every exit path, including a failed T-item, an exception,
and a signal, and anything it cannot remove is reported as a failure rather
than as a warning.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import chainlink_db
import cleanup as cl
import forgejo as fj
import project as pj
import scenarios as sc
from scenarios import ScenarioError
from waiter import Timeout

#: Where the harness reads the shared artifacts from.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Where the run's temporary directory is created. The directory is named by
#: ``mktemp -d``, never a fixed path, so two runs cannot share a project and a
#: leftover directory is attributable to exactly one run.
TEMP_ROOT = "/tmp"

#: The T-items, in the order the issue states them.
ITEMS = ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9")

#: Everything the acceptance can fail with. A run that fails for any other
#: reason is a harness fault, not a verdict, so it is reported as such.
ACCEPTANCE_FAILURES = (
    ScenarioError,
    Timeout,
    fj.ForgejoError,
    pj.ProjectError,
    pj.HarnessError,
    cl.CleanupError,
    OSError,
)


@dataclass
class Report:
    """The run's verdict: one entry per T-item, plus the leak check."""

    results: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    leaks: list[str] = field(default_factory=list)
    cleanup_problems: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            all(self.results.get(item) == "PASS" for item in ITEMS)
            and not self.leaks
            and not self.cleanup_problems
        )

    def emit(self) -> None:
        """Print one line per T-item, then any leak, then the verdict."""
        for item in ITEMS:
            status = self.results.get(item, "FAIL")
            detail = json.dumps(self.evidence.get(item, {}), sort_keys=True, default=str)
            print(f"{status} {item} {detail[:700]}")
        for problem in self.cleanup_problems:
            print(f"FAIL CLEANUP {problem}")
        for leak in self.leaks:
            print(f"FAIL LEAK {leak}")
        passed_items = sum(1 for item in ITEMS if self.results.get(item) == "PASS")
        verdict = "PASS" if self.passed else "FAIL"
        print(
            f"{verdict} recreated-leaf-recovery: {passed_items}/{len(ITEMS)} items "
            f"passed, {len(self.leaks)} leaks, "
            f"{len(self.cleanup_problems)} cleanup problems"
        )


def run_id() -> str:
    """Return a run id unique to this invocation.

    It becomes the compose project name, the tmux session prefix, the Forgejo
    account and repository names, and the temporary directory, so two runs can
    never reach each other's state.
    """
    return f"e2e1111-{secrets.token_hex(4)}"


class Walk:
    """The T-items in order, each keeping what the next ones assert on."""

    def __init__(self, scope: cl.RunScope, instance: fj.Instance) -> None:
        self.scope = scope
        self.instance = instance
        self.results: dict[str, str] = {}
        self.evidence: dict[str, Any] = {}
        self.project: pj.Project
        self.publication: dict[str, Any]
        #: How many times the plan has been started, so T5 can require one
        #: spawn per start rather than a total it would have to guess at.
        self.starts = 0

    def steps(self) -> list[tuple[str, Callable[[], Any]]]:
        """Return the ordered T-items and the work each one does."""
        return [
            ("T1", self._t1),
            ("T2", self._t2),
            ("T3", self._t3),
            ("T4", self._t4),
            ("T5", self._t5),
            ("T6", self._t6),
            ("T7", self._t7),
            ("T8", self._t8),
            ("T9", self._t9),
        ]

    def _t1(self) -> Any:
        """Provision the ordered child and publish the leaf, once."""
        self.starts += 1
        result = sc.t1_ordered_child_and_leaf(self.project, self.instance)
        self.publication = result
        return result

    def _t2(self) -> Any:
        """Recreate the session; every later item runs against the new server."""
        self.project = sc.t2_recreate_preserves_branch(self.project)["recreated"]
        return {
            "session": self.project.session,
            "head": self.publication["leaf_head"],
            "branch": self.publication["leaf_branch"],
        }

    def _t3(self) -> Any:
        """Prove both attachment shapes against the preserved branch."""
        head = self.publication["leaf_head"]
        reuse = sc.t3_reuse_preserved_worktree(self.project, head)
        self.starts += 1
        attach = sc.t3_attach_preserved_branch(self.project, self.instance, head)
        self.starts += 1
        return {"reuse": reuse, "attach": attach}

    def _t4(self) -> Any:
        return sc.t4_head_cwd_branch(self.project, self.publication)

    def _t5(self) -> Any:
        return sc.t5_exactly_one(self.project, self.instance, self.starts)

    def _t6(self) -> Any:
        return sc.t6_recreate_is_idempotent(
            self.project, self.publication["leaf_head"]
        )

    def _t7(self) -> Any:
        return sc.t7(self.project, self.instance, self.publication["pr_number"])

    def _t8(self) -> Any:
        return sc.t8_sink_does_not_create_planned_dir(self.project)

    def _t9(self) -> Any:
        return {
            "retryable": sc.t9_retryable_refusal_reconciles(self.project),
            "terminal": sc.t9_terminal_conflict_fails_closed(self.project),
        }


def walk(scope: cl.RunScope, instance: fj.Instance) -> Walk:
    """Run every T-item, stopping at the first that fails."""
    database = chainlink_db.create(scope.root)
    seeded = chainlink_db.seed(database)
    run = pj.new_run(scope, instance, database, leaf_branches=[pj.LEAF_BRANCH])
    walk_state = Walk(scope, instance)
    walk_state.project = pj.start(run)
    walk_state.evidence["seeded_issues"] = list(seeded)
    walk_state.evidence["chainlink_database"] = str(database)
    for item, step in walk_state.steps():
        try:
            result = step()
        except ACCEPTANCE_FAILURES as error:
            walk_state.results[item] = "FAIL"
            walk_state.evidence[item] = {
                "error": f"{type(error).__name__}: {error}"
            }
            print(f"FAIL {item} {json.dumps(walk_state.evidence[item])[:700]}", flush=True)
            return walk_state
        walk_state.results[item] = "PASS"
        walk_state.evidence[item] = result
        print(
            f"PASS {item} "
            f"{json.dumps(result, sort_keys=True, default=str)[:700]}",
            flush=True,
        )
    return walk_state


def main() -> int:
    """Run the acceptance and return its exit status."""
    parser = argparse.ArgumentParser(description="Run the #1111 acceptance.")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave this run's Forgejo, session, and directory for inspection",
    )
    arguments = parser.parse_args()

    identifier = run_id()
    root = cl.make_root(TEMP_ROOT)
    scope = cl.RunScope(run_id=identifier, root=root, keep=arguments.keep)
    report = Report()
    report.evidence["run_id"] = identifier
    report.evidence["run_directory"] = str(root)
    keep = arguments.keep
    instance: fj.Instance | None = None

    cl.install_trap(scope)
    try:
        instance = fj.provision(scope, PROJECT_ROOT, identifier)
        report.evidence["forgejo"] = {
            "compose_project": instance.project,
            "discovered_host": instance.host,
            "owner": instance.owner,
            "repo": instance.repo,
            "admin": instance.admin_username,
        }
        walk_state = walk(scope, instance)
        report.results = walk_state.results
        report.evidence.update(walk_state.evidence)
    except ACCEPTANCE_FAILURES as error:
        report.evidence["setup_error"] = f"{type(error).__name__}: {error}"
        print(f"FAIL SETUP {report.evidence['setup_error']}", flush=True)
    except BaseException as error:  # noqa: BLE001 - the run still owes its report
        report.evidence["harness_error"] = f"{type(error).__name__}: {error}"
        print(f"FAIL HARNESS {report.evidence['harness_error']}", flush=True)
    finally:
        # The report is owed even when teardown misbehaves, so nothing here may
        # raise. Teardown removes the compose project and its volume, which is
        # what takes the run's accounts and repository with it, so there is no
        # per-record cleanup left to fail.
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
                f"KEPT {root} (compose project {instance.project if instance else 'none'})",
                flush=True,
            )

    print(json.dumps(report.evidence, indent=2, sort_keys=True, default=str)[:20000])
    report.emit()
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
