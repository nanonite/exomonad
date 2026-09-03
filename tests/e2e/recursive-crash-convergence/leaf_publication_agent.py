"""Deterministic leaf publication actor for the #1057 real-server matrix."""

from __future__ import annotations

import json
import os
import re
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


def review_assigned_pr(pr_number: int) -> bool:
    """Submit one authoritative approval for the exact assigned PR head."""
    forgejo_url = _required_environment(
        "EXOMONAD_FORGEJO_E2E_URL", "FORGEJO_URL"
    ).rstrip("/")
    owner = _required_environment("EXOMONAD_FORGEJO_E2E_OWNER", "FORGEJO_OWNER")
    repository = _required_environment("EXOMONAD_FORGEJO_E2E_REPO", "FORGEJO_REPO")
    token = _required_environment(
        "EXOMONAD_FORGEJO_E2E_REVIEWER_TOKEN", "FORGEJO_REVIEWER_TOKEN"
    )
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
    result = EffectClient(
        TransportClient(
            socket_path=_server_socket(),
            project_root=Path.cwd(),
            timeout=10,
        ),
        role="tl",
        name=leaf_name,
    ).file_pr(
        title=f"Leaf {leaf_name} into {parent_branch}",
        body=(
            f"Deterministic #1057 leaf publication for {leaf_name}.\n\n"
            f"Prepared head: {head_sha}\n"
            f"TL-Slice-ID: {leaf_name}\n"
            "## Acceptance Criteria\n"
            "- Publish the prepared leaf commit to its direct parent branch."
        ),
        base_branch=parent_branch,
    )
    if result.success is not True:
        raise LeafPublicationError(result.error or "file_pr returned no success")
    return True


def main() -> int:
    try:
        review_number = _review_pr_number(sys.argv[1:])
        if review_number is not None:
            review_assigned_pr(review_number)
        elif not publish_leaf():
            time.sleep(300)
    except (KeyError, LeafPublicationError, TransportError) as error:
        print(f"leaf publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
