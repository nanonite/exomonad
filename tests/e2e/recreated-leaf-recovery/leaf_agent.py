"""Deterministic leaf actor for the #1111 acceptance.

The acceptance needs a leaf that behaves like a real one without spending a
real model call: it must produce a unique commit on its own branch, push it,
and file exactly one pull request through the shipped ``file_pr`` tool. The
server spawns this in place of an agent binary, in the leaf's own worktree,
through the same tmux window and the same tool surface a real leaf uses.

The branch and the commit it writes are both named after the invocation, so a
rerun of the acceptance produces a different commit on a different branch and
nothing here is order-dependent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tl_loop.client.effects import EffectClient  # noqa: E402
from tl_loop.client.transport import TransportClient, TransportError  # noqa: E402

#: The leaf branches this actor may publish, supplied by the launch wrapper.
LEAF_BRANCHES = "EXOMONAD_1111_LEAF_BRANCHES"

#: The file the leaf writes, relative to its own worktree root.
PAYLOAD = "e2e-1111-leaf.txt"

#: The prompt that assigns a reviewer also contains a PR number, which is what
#: distinguishes a review assignment from a spawn.
_ASSIGNMENT = re.compile(r"Review PR #([1-9][0-9]*)\b")


class LeafActorError(RuntimeError):
    """Raised when the deterministic leaf cannot complete its publication."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise LeafActorError(f"missing required environment: {name}")
    return value


def _current_branch() -> str:
    return subprocess.check_output(
        ["git", "branch", "--show-current"], text=True
    ).strip()


def _current_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def _target_branches() -> set[str]:
    return {
        value for value in os.environ.get(LEAF_BRANCHES, "").split(",") if value
    }


def _evidence_path() -> Path:
    """Return the file this actor appends its durable record to.

    The record is how the acceptance learns that the leaf really ran, without
    polling the actor's process or its window.
    """
    socket = _required("EXOMONAD_SOCKET")
    return Path(socket).parent / "e2e-1111-leaf-evidence.jsonl"


def _record(entry: Mapping[str, Any]) -> None:
    path = _evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _unique_token() -> str:
    """Return a token that is unique to this invocation of the leaf.

    ``git`` itself does not refuse an empty commit, but the acceptance proves a
    *unique* commit, so the payload always carries a fresh token.
    """
    import uuid

    return uuid.uuid4().hex


def publish_leaf() -> bool:
    """Publish this branch once, and recognise its own work when it returns.

    The acceptance asserts that a recreated session reattaches to the leaf's
    *published* head, so this actor has to be idempotent: a relaunch onto a
    branch that already carries its payload must not manufacture a second
    commit, or the head the acceptance is asserting about would move under it.
    The payload carries a token, so the first publication is unique per run and
    every later launch on that branch is recognised by the file it already
    wrote.
    """
    branch = _current_branch()
    if branch not in _target_branches():
        return False
    parent = branch.rsplit(".", 1)[0]
    leaf_name = branch.rsplit(".", 1)[-1]
    if Path(PAYLOAD).is_file() and not _payload_is_modified():
        _record(
            {
                "event": "leaf_already_published",
                "branch": branch,
                "leaf": leaf_name,
                "head": _current_head(),
                "payload": PAYLOAD,
            }
        )
        return True
    token = _unique_token()
    Path(PAYLOAD).write_text(f"leaf={leaf_name}\ntoken={token}\n", encoding="utf-8")
    subprocess.run(["git", "add", PAYLOAD], check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"Add deterministic leaf payload {token[:12]}"],
        check=True,
    )
    # The push is what makes the publication possible at all: ``file_pr`` opens
    # a pull request against a branch the forge has to already know.
    subprocess.run(["git", "push", "-q", "-u", "origin", branch], check=True)
    head = _current_head()
    title = f"Leaf {leaf_name} into {parent}"
    body = (
        f"Deterministic #1111 leaf publication for {leaf_name}.\n\n"
        f"Payload token: {token}\n"
        f"Prepared head: {head}\n"
        "## Acceptance Criteria\n"
        "- The recreated session attaches to this branch instead of recreating it.\n"
    )
    result = EffectClient(
        TransportClient(
            socket_path=_required("EXOMONAD_SOCKET"),
            project_root=Path.cwd(),
            timeout=30,
        ),
        role="tl",
        name=leaf_name,
    ).file_pr(title=title, body=body, base_branch=parent)
    if result.success is not True:
        raise LeafActorError(result.error or "file_pr returned no success")
    _record(
        {
            "event": "leaf_published",
            "branch": branch,
            "parent": parent,
            "leaf": leaf_name,
            "head": head,
            "token": token,
            "payload": PAYLOAD,
        }
    )
    return True


def _payload_is_modified() -> bool:
    """Report whether this actor's own payload has uncommitted changes.

    A payload that is present *and* modified means the worktree is mid-flight
    rather than already published, so the actor finishes the publication instead
    of treating the branch as done.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", PAYLOAD],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise LeafActorError(f"could not read the leaf worktree status: {result.stderr}")
    return bool(result.stdout.strip())


def review_pr(pr_number: int) -> bool:
    """Submit one approval for the exact current head of an assigned PR."""
    from leaf_publication_agent import review_assigned_pr

    return review_assigned_pr(pr_number)


def _hold_window() -> None:
    """Keep the agent's window open, the way a real agent's stays open.

    The server proves a spawned agent is live by finding its tmux window ready.
    An actor that returned immediately would close that window out from under
    the check, so the window is held by blocking on standard input: it ends when
    the run's teardown kills the session, and it never depends on a duration
    being long enough.
    """
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass


def main() -> int:
    try:
        assignment = _ASSIGNMENT.search(" ".join(sys.argv[1:]))
        if assignment is not None:
            review_pr(int(assignment.group(1)))
            _hold_window()
        elif not publish_leaf():
            # Not a branch this acceptance owns; idle like a real agent.
            _hold_window()
        else:
            _hold_window()
    except (KeyError, LeafActorError, TransportError, subprocess.CalledProcessError) as error:
        print(f"deterministic leaf failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
