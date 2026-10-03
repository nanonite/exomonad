"""The CI provider for one crash-matrix case's repository.

The matrix brings up a Forgejo with Actions disabled and registers no runner
against it, so nothing on that instance ever reports a commit status. Every head
the watcher polls then reads ``ci_status: unknown``, and the controller's merge
gates require ``success`` or ``neutral`` -- ``_execute_aggregate_merges`` for a
sub-TL aggregate and ``_direct_merge_evidence`` for a direct leaf. A case
therefore cannot reach ``TLDone`` for a reason that has nothing to do with the
boundary it crashed at: the forge it was given cannot pass CI.

This actor is the CI provider, and nothing else. It polls the case's own
repository for open pull requests and posts one ``success`` commit status per
head it has not already reported on, which is what a CI provider does and what
the watcher then observes by polling like any other forge fact. It asserts
nothing about the controller, writes nothing into the controller's state, and
cannot make a boundary converge that the product would not converge: a run with
no actor fails exactly as it did before, one status short of the CI gate.

Two properties keep it from becoming a way to fake a green run:

* It reports on heads only. It never writes a slice, a checkpoint, a review, or a
  controller event, so every approval and every publication in a case still has
  to be earned the way the boundary under test earns it.
* It posts ``success`` for a head it saw on a pull request, which is the same
  evidence a real provider posts. A case whose CI was meant to be pending is a
  case the actor is not started for.

The harness starts one actor per case, in its own process, registers it with the
run scope so a leak fails the run, and stops it when the case is released.
"""

from __future__ import annotations

import json
import os
import signal
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: The status context this actor reports under. A forge holds one status per
#: (context, head), so the name is what keeps the actor from overwriting a status
#: something else on the instance reported, and what names the report in the
#: case's own log.
STATUS_CONTEXT = "e2e-1057-ci"

#: How long to wait between polls of the case's pull requests. The server polls
#: its own watch every second, so a head is reported within a couple of seconds
#: of appearing and the watcher sees it on its next cycle.
POLL_SECONDS = 1.0

#: How long the actor serves one case before it gives the case's forge back. A
#: case that runs longer than this is a case that is not converging, and the
#: deadline is what stops the actor from outliving it.
DEFAULT_LIFETIME_SECONDS = 900.0

#: How many pull requests one poll reads. A case publishes a handful of branches
#: and never approaches this.
PAGE_LIMIT = 50


class CIActorError(RuntimeError):
    """The actor could not report CI for the repository it was given."""


def _required_environment(*names: str) -> str:
    """Return the first of ``names`` that carries a value, or refuse to guess."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise CIActorError(f"missing required environment: {names[0]}")


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: Mapping[str, object] | None = None,
) -> Any:
    """Call the forge, or fail rather than report CI it did not report."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"token {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise CIActorError(f"Forgejo request failed: {method} {url}") from error


def _pull_request_heads(endpoint: str, token: str) -> list[tuple[int, str]]:
    """Return ``(pr_number, head_sha)`` for every open pull request."""
    pulls = _request(
        "GET",
        f"{endpoint}/pulls?state=open&limit={PAGE_LIMIT}",
        token=token,
    )
    if not isinstance(pulls, list):
        raise CIActorError(f"pull request listing is not an array: {pulls!r}")
    heads: list[tuple[int, str]] = []
    for pull in pulls:
        if not isinstance(pull, Mapping):
            continue
        number = pull.get("number")
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, Mapping) else None
        if type(number) is int and isinstance(sha, str) and sha:
            heads.append((number, sha))
    return heads


def _report_success(endpoint: str, token: str, head_sha: str) -> None:
    """Post the one success status a CI provider would post for this head."""
    _request(
        "POST",
        f"{endpoint}/statuses/{head_sha}",
        token=token,
        payload={
            "state": "success",
            "context": STATUS_CONTEXT,
            "description": "reported by the #1057 crash matrix",
        },
    )


class CIActor:
    """Report CI for one repository until the case releases it."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        log_path: Path,
        stop_path: Path | None = None,
        *,
        lifetime_seconds: float = DEFAULT_LIFETIME_SECONDS,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.log_path = log_path
        self.stop_path = stop_path
        self.lifetime_seconds = lifetime_seconds
        self.poll_seconds = poll_seconds
        self.reported: set[str] = set()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, record: Mapping[str, object]) -> None:
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, sort_keys=True) + "\n")

    def _stopped(self) -> bool:
        return self.stop_path is not None and self.stop_path.exists()

    def poll_once(self) -> list[str]:
        """Report every open head this actor has not reported on yet."""
        reported: list[str] = []
        for number, head_sha in _pull_request_heads(self.endpoint, self.token):
            if head_sha in self.reported:
                continue
            _report_success(self.endpoint, self.token, head_sha)
            self.reported.add(head_sha)
            reported.append(head_sha)
            self._log(
                {
                    "head_sha": head_sha,
                    "context": STATUS_CONTEXT,
                    "pr_number": number,
                    "state": "success",
                }
            )
        return reported

    def serve(self) -> int:
        """Poll until the case releases the actor, its forge, or its deadline."""
        deadline = time.monotonic() + self.lifetime_seconds
        while time.monotonic() < deadline:
            if self._stopped():
                return 0
            try:
                self.poll_once()
            except CIActorError as error:
                # The case's forge can be released underneath the actor, and a
                # released forge is this case's business, not a fault in it. The
                # failure is recorded and the actor keeps trying until the case
                # stops it, so the case's own report names the boundary it failed.
                self._log({"error": str(error)})
            time.sleep(self.poll_seconds)
        return 1


def _actor_from_environment() -> CIActor:
    """Build the actor from the environment the harness started it with."""
    base_url = _required_environment("EXOMONAD_CI_FORGE_URL").rstrip("/")
    owner = _required_environment("EXOMONAD_CI_OWNER")
    repository = _required_environment("EXOMONAD_CI_REPO")
    token = _required_environment("EXOMONAD_CI_TOKEN")
    log_path = Path(_required_environment("EXOMONAD_CI_LOG"))
    stop = os.environ.get("EXOMONAD_CI_STOP", "").strip()
    lifetime = float(
        os.environ.get("EXOMONAD_CI_LIFETIME_SECONDS", "") or DEFAULT_LIFETIME_SECONDS
    )
    return CIActor(
        f"{base_url}/api/v1/repos/{owner}/{repository}",
        token,
        log_path,
        Path(stop) if stop else None,
        lifetime_seconds=lifetime,
    )


def main() -> int:
    """Serve one case, and stop the moment the harness says the case is over.

    The harness asks the actor to leave twice, deliberately: it writes the stop
    file first, so an actor that is between polls exits on its own terms and
    logs nothing it did not do, and it terminates the process afterwards, so an
    actor that is stuck in a forge call cannot outlive the case that started it.
    """
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    return _actor_from_environment().serve()


if __name__ == "__main__":
    raise SystemExit(main())