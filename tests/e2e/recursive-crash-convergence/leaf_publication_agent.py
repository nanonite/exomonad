"""Deterministic leaf publication actor for the #1057 real-server matrix."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from tl_loop.client.effects import EffectClient
from tl_loop.client.transport import TransportClient, TransportError


class LeafPublicationError(RuntimeError):
    """A prepared leaf could not be published to Forgejo."""


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


def publish_leaf() -> bool:
    """Publish the current configured leaf through the production tool surface."""
    branch = _current_branch()
    if not _target_leaf_branch(branch):
        return False
    parent_branch = branch.rsplit(".", 1)[0]
    leaf_name = branch.rsplit(".", 1)[-1]
    head_sha = _current_head()
    result = EffectClient(
        TransportClient(project_root=Path.cwd(), timeout=10),
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
        publish_leaf()
    except (KeyError, LeafPublicationError, TransportError) as error:
        print(f"leaf publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
