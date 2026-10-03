"""Deterministic leaf publication actor for the #1057 real-server matrix."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from boundaries import effect_identity, redacted_arguments  # noqa: E402
from tl_loop.client.effects import EffectClient  # noqa: E402
from tl_loop.client.transport import TransportClient, TransportError  # noqa: E402


class LeafPublicationError(RuntimeError):
    """A prepared leaf could not be published to Forgejo."""


def _required_environment(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise LeafPublicationError(f"missing required environment: {names[0]}")


def _server_socket() -> str:
    """Require the root controller socket exported by the launch wrapper."""
    return _required_environment("EXOMONAD_SOCKET")


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: Mapping[str, object] | None = None,
) -> Any:
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
        raise LeafPublicationError(f"Forgejo request failed: {method} {url}") from error


def _current_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "branch", "--show-current"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise LeafPublicationError(
            "could not resolve the leaf worktree branch"
        ) from error


def _current_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise LeafPublicationError(
            "could not resolve the prepared leaf commit"
        ) from error


def _target_leaf_branch(branch: str) -> bool:
    configured = {
        value
        for value in os.environ.get("EXOMONAD_1057_LEAF_BRANCHES", "").split(",")
        if value
    }
    return branch in configured


def _read_handoff() -> Mapping[str, object] | None:
    """Read the controller's crash/resume contract next to the server socket."""
    socket = os.environ.get("EXOMONAD_SOCKET", "")
    if not socket:
        return None
    path = Path(socket).parent / "e2e-crash-handoff.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _publication_crash_point(
    handoff: Mapping[str, object] | None, arguments: Mapping[str, object]
) -> str | None:
    """Return the publication boundary point this leaf file_pr must honor."""
    if handoff is None or handoff.get("phase") != "crash":
        return None
    if handoff.get("boundary") != "publication":
        return None
    title = arguments.get("title")
    if isinstance(title, str) and title.startswith("Aggregate "):
        return None
    point = handoff.get("point")
    return point if point in {"before", "after"} else None


def _append_record(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _inject_publication_crash(
    handoff: Mapping[str, object],
    arguments: Mapping[str, object],
    *,
    success: bool | None,
) -> None:
    """Record the publication boundary and terminate the owning controller.

    Only the controller dies at the boundary; the leaf keeps running so the
    resumed controller observes the real publication exactly as it would when
    the controller crashes while a child is mid-effect.
    """
    record: dict[str, object] = {
        "boundary": "publication",
        "point": handoff.get("point"),
        "tool_name": "file_pr",
        "identity": effect_identity(arguments, "file_pr"),
        "arguments": redacted_arguments(arguments),
    }
    if success is not None:
        record["success"] = success
    marker = handoff.get("marker")
    if not isinstance(marker, str) or not marker:
        raise LeafPublicationError("crash handoff is missing its marker path")
    _append_record(Path(marker), record)
    owner = handoff.get("owner_pid")
    if isinstance(owner, int) and owner > 0 and owner != os.getpid():
        try:
            os.kill(owner, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _record_file_pr_attempt(
    handoff: Mapping[str, object],
    arguments: Mapping[str, object],
    *,
    crash_point: str | None,
) -> None:
    """Record the leaf's file_pr attempt before the call is made.

    The leaf owns the publication boundary, so recording only after a successful
    call lets a call made during a ``publication:before`` crash escape the
    resumed trace. The attempt is written first, tagged with the crash point, so
    the acceptance check can prove exactly-once publication whether the crash
    preceded or followed the call.
    """
    trace = handoff.get("resume_trace")
    if not isinstance(trace, str) or not trace:
        return
    _append_record(
        Path(trace),
        {
            "tool_name": "file_pr",
            "identity": effect_identity(arguments, "file_pr"),
            "arguments": redacted_arguments(arguments),
            "crash_point": crash_point,
        },
    )


def _review_pr_number(arguments: list[str]) -> int | None:
    prompt = " ".join(arguments)
    match = re.search(r"\bReview PR #([1-9][0-9]*):", prompt)
    return int(match.group(1)) if match else None


def _reviewer_login(forgejo_url: str, token: str) -> str:
    user = _request("GET", f"{forgejo_url}/api/v1/user", token=token)
    login = user.get("login") if isinstance(user, Mapping) else None
    if not isinstance(login, str) or not login:
        raise LeafPublicationError("reviewer token did not resolve an account")
    return login


def _review_current_head(endpoint: str, token: str, pr_number: int) -> str:
    pull = _request("GET", f"{endpoint}/pulls/{pr_number}", token=token)
    head = pull.get("head") if isinstance(pull, Mapping) else None
    sha = head.get("sha") if isinstance(head, Mapping) else None
    if not isinstance(sha, str) or not sha:
        raise LeafPublicationError(
            f"PR #{pr_number} has no authoritative current head SHA"
        )
    return sha


def _review_already_submitted(
    endpoint: str, token: str, pr_number: int, head_sha: str, login: str
) -> bool:
    reviews = _request("GET", f"{endpoint}/pulls/{pr_number}/reviews", token=token)
    if not isinstance(reviews, list):
        raise LeafPublicationError(f"review listing is not an array: {reviews!r}")
    for review in reviews:
        if not isinstance(review, Mapping):
            continue
        user = review.get("user")
        reviewer_login = user.get("login") if isinstance(user, Mapping) else None
        if (
            reviewer_login == login
            and review.get("commit_id") == head_sha
            and str(review.get("state", "")).upper() == "APPROVED"
        ):
            return True
    return False


def _review_owned_by_harness() -> bool:
    """Whether this run's harness, rather than the spawned reviewer, approves.

    The #1117 acceptance posts the reviewer's approval itself (its `review`
    item), because it has no model to review with. The shipped controller still
    spawns its reviewer as soon as a slice reaches review, and that stand-in
    resolves through the same shim -- so if it also submitted, the run would
    hold two approvals for one head and the acceptance's "exactly one" would
    be the harness's own doing. The spawn itself is still exercised; only the
    duplicate submission is left to the harness that owns this run's reviews.
    """
    return os.environ.get("EXOMONAD_REVIEW_OWNED_BY_HARNESS", "").strip() == "1"


def review_assigned_pr(pr_number: int) -> bool:
    """Submit one authoritative approval for the exact assigned PR head.

    The coordinates come from the shim that started this actor, which the harness
    wrote from the Forgejo instance it provisioned. There is no fallback to an
    operator-supplied instance: an actor that could not reach the forge this run
    started must fail, not approve on somebody else's repository.
    """
    forgejo_url = _required_environment("FORGEJO_URL").rstrip("/")
    owner = _required_environment("FORGEJO_OWNER")
    repository = _required_environment("FORGEJO_REPO")
    token = _required_environment("FORGEJO_REVIEWER_TOKEN")
    endpoint = f"{forgejo_url}/api/v1/repos/{owner}/{repository}"
    head_sha = _review_current_head(endpoint, token, pr_number)
    login = _reviewer_login(forgejo_url, token)
    if _review_already_submitted(endpoint, token, pr_number, head_sha, login):
        return False
    _request(
        "POST",
        f"{endpoint}/pulls/{pr_number}/reviews",
        token=token,
        payload={"event": "APPROVED", "commit_id": head_sha},
    )
    return True


def publish_leaf() -> bool:
    """Publish the current configured leaf through the production tool surface."""
    branch = _current_branch()
    if not _target_leaf_branch(branch):
        return False
    parent_branch = branch.rsplit(".", 1)[0]
    leaf_name = branch.rsplit(".", 1)[-1]
    head_sha = _current_head()
    title = f"Leaf {leaf_name} into {parent_branch}"
    body = (
        f"Deterministic #1057 leaf publication for {leaf_name}.\n\n"
        f"Prepared head: {head_sha}\n"
        f"TL-Slice-ID: {leaf_name}\n"
        "## Acceptance Criteria\n"
        "- Publish the prepared leaf commit to its direct parent branch."
    )
    arguments: dict[str, object] = {
        "title": title,
        "body": body,
        "base_branch": parent_branch,
    }
    handoff = _read_handoff()
    crash_point = _publication_crash_point(handoff, arguments)
    if crash_point == "before":
        assert handoff is not None
        _inject_publication_crash(handoff, arguments, success=None)
    if handoff is not None:
        # Record the attempt before the call so a call made after a
        # publication:before crash is still in the resumed trace.
        _record_file_pr_attempt(handoff, arguments, crash_point=crash_point)
    result = EffectClient(
        TransportClient(
            socket_path=_server_socket(),
            project_root=Path.cwd(),
            timeout=10,
        ),
        role="tl",
        name=leaf_name,
    ).file_pr(title=title, body=body, base_branch=parent_branch)
    if result.success is not True:
        raise LeafPublicationError(result.error or "file_pr returned no success")
    if crash_point == "after":
        assert handoff is not None
        _inject_publication_crash(handoff, arguments, success=result.success)
    return True


def main() -> int:
    try:
        review_number = _review_pr_number(sys.argv[1:])
        if review_number is not None:
            if _review_owned_by_harness():
                return 0
            review_assigned_pr(review_number)
        elif not publish_leaf():
            time.sleep(300)
    except (KeyError, LeafPublicationError, TransportError) as error:
        print(f"leaf publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
