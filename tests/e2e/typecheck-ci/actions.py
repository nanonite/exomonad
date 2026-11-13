"""Drive the disposable instance's repositories and Actions runs.

The run never reads the runner container's logs to decide a verdict: the
instance's own Actions API records the workflow run and its status, and the
job log endpoint returns the steps' output. That is the durable boundary the
acceptance waits on, so a slow job is never mistaken for a failed one.
"""

from __future__ import annotations

import base64
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import e2e_harness.forgejo_stack as fj

#: The checkout action the real workflow uses.
CHECKOUT_NAMESPACE = "actions"
CHECKOUT_REPOSITORY = "checkout"

#: Workflow run states that are not terminal.
_ACTIVE_STATUSES = {"", "waiting", "queued", "running", "in_progress"}


class ActionsError(RuntimeError):
    """Raised when the run cannot observe the workflow it pushed."""


def _runs_payload(instance: fj.Instance) -> Mapping[str, Any]:
    payload = fj.api(
        "GET",
        instance.api_url(f"repos/{instance.owner}/{instance.repo}/actions/runs?limit=50"),
        token=instance.author.token,
    )
    if not isinstance(payload, Mapping):
        raise ActionsError(f"the runs endpoint returned {payload!r}")
    return payload


def latest_run_id(instance: fj.Instance) -> int:
    """Return the highest workflow-run id the instance reports, or zero."""
    runs = _runs_payload(instance).get("workflow_runs") or []
    ids = [
        int(run["id"])
        for run in runs
        if isinstance(run, Mapping) and isinstance(run.get("id"), int)
    ]
    return max(ids, default=0)


def wait_new_run(instance: fj.Instance, previous: int, timeout: float = 120.0) -> int:
    """Block until a run newer than ``previous`` exists, and return its id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = latest_run_id(instance)
        if current > previous:
            return current
        time.sleep(1.0)
    raise ActionsError(
        f"no workflow run appeared within {timeout:.0f}s after a push "
        f"(last id {previous})"
    )


def wait_terminal(instance: fj.Instance, run_id: int, timeout: float = 900.0) -> str:
    """Block until the workflow run reaches a terminal state, and return it."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        run = fj.api(
            "GET",
            instance.api_url(
                f"repos/{instance.owner}/{instance.repo}/actions/runs/{run_id}"
            ),
            token=instance.author.token,
        )
        status = str(run.get("status") or "") if isinstance(run, Mapping) else ""
        if status.lower() not in _ACTIVE_STATUSES:
            return status.lower()
        last = status
        time.sleep(1.0)
    raise ActionsError(
        f"workflow run {run_id} did not reach a terminal state within "
        f"{timeout:.0f}s (last status {last!r})"
    )


def job_logs(instance: fj.Instance, run_id: int, timeout: float = 120.0) -> str:
    """Return the first job's log for a completed run.

    The instance's ROOT_URL is ``http://forgejo:3000``, a name that exists only
    on its compose network, so the request uses ``--connect-to`` to reach the
    discovered ephemeral port while keeping the Host the instance expects.
    """
    port = instance.host.rsplit(":", 1)[1]
    url = (
        f"http://forgejo:3000/{instance.owner}/{instance.repo}"
        f"/actions/runs/{run_id}/jobs/0/attempt/1/logs"
    )
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "curl",
                "-fsS",
                "--retry",
                "5",
                "--retry-connrefused",
                "--retry-delay",
                "1",
                "--connect-to",
                f"forgejo:3000:127.0.0.1:{port}",
                "-u",
                instance.author.basic_auth(),
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
        last = result.stderr.strip()
        time.sleep(1.0)
    raise ActionsError(f"could not read the log for run {run_id}: {last}")


def _git_auth_header(account: fj.Account) -> str:
    encoded = base64.b64encode(account.basic_auth().encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {encoded}"


def mirror_checkout(instance: fj.Instance, mirror: Path) -> None:
    """Publish the bare checkout mirror into the instance as actions/checkout.

    ``DEFAULT_ACTIONS_URL`` points the runner at the instance itself, so the
    action source has to exist there. The namespace is an ordinary account and
    the push uses that account's token; no external network is involved.
    """
    account = fj.create_account(instance, CHECKOUT_NAMESPACE)
    fj.create_repository_for(
        instance, account, CHECKOUT_REPOSITORY, auto_init=False
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(mirror),
            "-c",
            f"http.extraHeader={_git_auth_header(account)}",
            "push",
            "--force",
            f"{instance.base_url}/{CHECKOUT_NAMESPACE}/{CHECKOUT_REPOSITORY}.git",
            "refs/heads/*:refs/heads/*",
            "refs/tags/*:refs/tags/*",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ActionsError(
            "could not mirror actions/checkout into the instance: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def commit_and_push(
    repository: Path, instance: fj.Instance, message: str
) -> None:
    """Commit the scratch tree's current state and push it to the instance."""
    for arguments in (
        ["add", "-A"],
        ["commit", "--allow-empty", "-q", "-m", message],
    ):
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ActionsError(
                f"git {' '.join(arguments)} failed: "
                + (result.stderr.strip() or result.stdout.strip())
            )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            f"http.extraHeader={_git_auth_header(instance.author)}",
            "push",
            "--force",
            instance.clone_url(),
            "main",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ActionsError(
            "could not push the scratch repository: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def initialize_repository(repository: Path) -> None:
    """Initialize the scratch working tree with a local identity.

    The identity is repository-local and exists only in the run's temporary
    directory; it never touches this worktree's configuration.
    """
    commands = (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "typecheck-ci acceptance"],
        ["config", "user.email", "typecheck-ci@example.invalid"],
    )
    for arguments in commands:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ActionsError(
                f"git {' '.join(arguments)} failed: "
                + (result.stderr.strip() or result.stdout.strip())
            )
