"""Run the real CI typecheck job against a disposable Forgejo and nested dockerd.

Each run owns a fresh Forgejo, runner, and ``docker:28-dind`` under its own
compose project with an ephemeral port. The job image is copied from the host
cache into the nested daemon, ``actions/checkout`` is mirrored into the instance
from the same image, and the scratch repository is a copy of the real project
with the real ``typecheck`` job extracted from ``.forgejo/workflows/ci.yml``.
No step pulls or builds an image, and no step needs the internet.

The run prints one PASS/FAIL line per check: the green tree, the red tree after
one injected strict error, the missing-nested-image failure, the Phase 0 image
precondition, and the project-agnostic contract. Teardown runs on every exit
path and the run fails if any container, network, or volume of its compose
project survives.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where the harness reads this worktree from.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
#: The shared run-scoped package both real-server acceptances import.
LIB_DIR = PROJECT_ROOT / "tests" / "e2e" / "lib"
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "e2e" / "typecheck-ci"))
sys.path.insert(0, str(LIB_DIR))

import actions
import agnostic
import dockerops
import e2e_harness.cleanup as cl
import e2e_harness.forgejo_stack as fj
import images
import scratch
from run_prefix import PREFIX, RUN_DIRECTORY_PREFIX

TEMP_ROOT = "/tmp"

CHECKS = (
    "phase0-cached-images",
    "project-agnostic",
    "green-current-tree",
    "red-injected-strict-error",
    "missing-nested-image",
)

JOB_SUCCEEDED = "Job succeeded"
JOB_FAILED = "Job failed"
RATCHET_PASSED = "typecheck ratchet passed"
RATCHET_FAILED = "typecheck ratchet failed"


def _truncate(text: str, limit: int = 500) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def green_log_is_acceptable(log: str) -> tuple[bool, str]:
    """The green run must succeed and report a passing ratchet."""
    if JOB_FAILED in log:
        return False, "the job reported failure"
    if JOB_SUCCEEDED not in log:
        return False, "the job reported no success marker"
    if RATCHET_PASSED not in log:
        return False, "the job did not run the ratchet"
    return True, "job succeeded and the ratchet passed"


def red_log_is_acceptable(log: str) -> tuple[bool, str]:
    """The red run must fail because the injected error is over the baseline."""
    if JOB_SUCCEEDED in log:
        return False, "the job succeeded with an injected strict error"
    if JOB_FAILED not in log:
        return False, "the job reported no failure marker"
    if RATCHET_FAILED not in log:
        return False, "the ratchet did not reject the injected error"
    if str(scratch.PROBE_RELATIVE_PATH) not in log:
        return False, "the failure does not name the injected file"
    return True, "the ratchet rejected the injected strict error"


def missing_image_log_is_acceptable(log: str, image: str) -> tuple[bool, str]:
    """The job must fail because the image is absent from the nested store.

    The runner fails before it starts a job container, so the job log ends at
    the pull error; there is no ``Job failed`` marker to require. The run's
    terminal status is asserted separately, and the attempted registry access
    is what proves the nested store, not the host cache, was consulted.
    """
    if JOB_SUCCEEDED in log:
        return False, "the job succeeded without its image"
    if image not in log:
        return False, f"the failure does not name {image}"
    if "docker pull" not in log and "Error response from daemon" not in log:
        return False, "the failure does not show the attempted registry access"
    return True, "the job failed at the missing nested image"


@dataclass
class Report:
    """One verdict per check, plus cleanup and leak evidence."""

    results: dict[str, tuple[bool, str]] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    cleanup_problems: list[str] = field(default_factory=list)
    sweep_problems: list[str] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    surviving: list[str] = field(default_factory=list)

    def record(self, name: str, ok: bool, detail: str) -> bool:
        self.results[name] = (ok, detail)
        return ok

    @property
    def passed(self) -> bool:
        return (
            all(self.results.get(name, (False, ""))[0] for name in CHECKS)
            and not self.cleanup_problems
            and not self.sweep_problems
            and not self.leaks
            and not self.surviving
        )

    def emit(self) -> None:
        for name in CHECKS:
            if name not in self.results:
                print(f"SKIP {name} not reached", flush=True)
                continue
            ok, detail = self.results[name]
            print(
                f"{'PASS' if ok else 'FAIL'} {name} {_truncate(detail)}",
                flush=True,
            )
        for problem in self.cleanup_problems:
            print(f"FAIL CLEANUP {_truncate(problem)}", flush=True)
        for problem in self.sweep_problems:
            print(f"FAIL SWEEP {_truncate(problem)}", flush=True)
        for problem in self.leaks:
            print(f"FAIL LEAK {_truncate(problem)}", flush=True)
        for problem in self.surviving:
            print(f"FAIL SURVIVOR {_truncate(problem)}", flush=True)
        passed = sum(1 for ok, _ in self.results.values() if ok)
        verdict = "PASS" if self.passed else "FAIL"
        print(
            f"{verdict} typecheck-ci: {passed}/{len(CHECKS)} checks passed, "
            f"{len(self.cleanup_problems)} cleanup problems, "
            f"{len(self.sweep_problems)} sweep problems, "
            f"{len(self.leaks) + len(self.surviving)} leaks",
            flush=True,
        )


def run_id() -> str:
    """Return a run id unique to this invocation and safe in a project name."""
    return f"e2e1332-{secrets.token_hex(3)}"


def _observe(
    instance: fj.Instance, repository: Path, previous: int, message: str
) -> tuple[int, str, str]:
    """Push one commit, wait for its run, and return run id, status, and log."""
    actions.commit_and_push(repository, instance, message)
    current = actions.wait_new_run(instance, previous)
    status = actions.wait_terminal(instance, current)
    log = actions.job_logs(instance, current)
    return current, status, log


def run(scope: cl.RunScope, report: Report) -> None:
    """Provision the disposable stack and walk the checks in order."""
    report.sweep_problems = cl.sweep_stale(
        PREFIX, fj.actions_template_path(PROJECT_ROOT), RUN_DIRECTORY_PREFIX
    )
    report.evidence["swept_before_run"] = report.sweep_problems
    # The sweep matches every directory carrying this harness's prefix,
    # including the one ``make_root`` just created for this run. Put it back;
    # the sweep's job is to remove the *previous* run's state, not to leave
    # this run without a directory to write into.
    scope.root.mkdir(parents=True, exist_ok=True)

    try:
        images.require_cached_images(images.REQUIRED_IMAGES)
    except images.MissingImage as error:
        report.record("phase0-cached-images", False, str(error))
        return
    report.record(
        "phase0-cached-images", True, ", ".join(images.REQUIRED_IMAGES)
    )

    findings = agnostic.find_project_specific(
        [
            Path(__file__).resolve().parent,
            PROJECT_ROOT / images.DOCKERFILE,
        ]
    )
    report.record(
        "project-agnostic",
        not findings,
        "no project-specific content" if not findings else "; ".join(findings),
    )

    instance = fj.provision_actions(
        scope, PROJECT_ROOT, scope.run_id, images.JOB_IMAGE
    )
    report.evidence["forgejo"] = {
        "compose_project": instance.project,
        "discovered_host": instance.host,
        "owner": instance.owner,
        "repo": instance.repo,
    }

    mirror = dockerops.extract_checkout_mirror(
        images.JOB_IMAGE,
        scope.root / "actions-checkout.git",
        f"{scope.session_prefix}-mirror",
    )
    actions.mirror_checkout(instance, mirror)
    dind = dockerops.service_container(instance.project, instance.compose_file, "dind")
    report.evidence["nested_load"] = dockerops.load_into_nested(
        images.JOB_IMAGE, dind
    ).strip()

    repository = scratch.write_scratch(PROJECT_ROOT, scope.root / "scratch")
    actions.initialize_repository(repository)

    previous = 0
    current, status, log = _observe(instance, repository, previous, "green baseline")
    ok, detail = green_log_is_acceptable(log)
    report.record(
        "green-current-tree",
        ok and status == "success",
        detail if ok else f"{detail}; run status {status!r}",
    )
    report.evidence["green"] = {"run": current, "status": status, "log": log[-4000:]}
    previous = current

    scratch.inject_strict_error(repository)
    current, status, log = _observe(
        instance, repository, previous, "inject one strict mypy error"
    )
    ok, detail = red_log_is_acceptable(log)
    report.record(
        "red-injected-strict-error",
        ok and status == "failure",
        detail if ok else f"{detail}; run status {status!r}",
    )
    report.evidence["red"] = {"run": current, "status": status, "log": log[-4000:]}
    previous = current

    (repository / "e2e-marker.txt").write_text(
        "the job image is absent from the nested store\n", encoding="utf-8"
    )
    dockerops.remove_from_nested(images.JOB_IMAGE, dind)
    current, status, log = _observe(
        instance, repository, previous, "run without the nested job image"
    )
    ok, detail = missing_image_log_is_acceptable(log, images.JOB_IMAGE)
    report.record(
        "missing-nested-image",
        ok and status == "failure",
        detail if ok else f"{detail}; run status {status!r}",
    )
    report.evidence["missing_image"] = {
        "run": current,
        "status": status,
        "log": log[-4000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave this run's stack and directory for inspection",
    )
    arguments = parser.parse_args()

    identifier = run_id()
    root = cl.make_root(TEMP_ROOT, PREFIX)
    scope = cl.RunScope(run_id=identifier, root=root, prefix=PREFIX, keep=arguments.keep)
    report = Report()
    report.evidence["run_id"] = identifier
    report.evidence["run_directory"] = str(root)
    project = ""

    cl.install_trap(scope)
    try:
        run(scope, report)
    except (
        fj.ForgejoError,
        actions.ActionsError,
        dockerops.DockerError,
        scratch.ScratchError,
        cl.CleanupError,
        OSError,
    ) as error:
        report.evidence["setup_error"] = f"{type(error).__name__}: {error}"
        print(f"FAIL SETUP {_truncate(str(error))}", flush=True)
    except BaseException as error:  # noqa: BLE001 - the run still owes its report
        report.evidence["harness_error"] = f"{type(error).__name__}: {error}"
        print(f"FAIL HARNESS {_truncate(str(error))}", flush=True)
    finally:
        # Record the project before teardown forgets it, and read the volumes
        # its containers mount while the containers still exist.
        tracked = sorted(scope.compose_projects)
        project = tracked[0] if tracked else ""
        recorded_volumes: list[str] = []
        if project:
            try:
                recorded_volumes = dockerops.project_volume_names(project)
            except BaseException as error:  # noqa: BLE001 - reported, never raised
                report.cleanup_problems.append(
                    f"could not record volumes: {type(error).__name__}: {error}"
                )
        try:
            report.cleanup_problems.extend(scope.teardown())
        except BaseException as error:  # noqa: BLE001 - reported, never raised
            report.cleanup_problems.append(
                f"teardown raised: {type(error).__name__}: {error}"
            )
        if not arguments.keep:
            try:
                report.leaks = scope.leaks()
            except BaseException as error:  # noqa: BLE001 - reported, never raised
                report.leaks = [f"leak check raised: {type(error).__name__}: {error}"]
            if project:
                try:
                    report.surviving = dockerops.surviving_resources(
                        project, recorded_volumes
                    )
                except BaseException as error:  # noqa: BLE001 - reported, never raised
                    report.surviving = [
                        f"survivor check raised: {type(error).__name__}: {error}"
                    ]
        else:
            print(f"KEPT {root} (compose project {project or 'none'})", flush=True)

    print(json.dumps(report.evidence, indent=2, sort_keys=True, default=str)[:20000])
    report.emit()
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
