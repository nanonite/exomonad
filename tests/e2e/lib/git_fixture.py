"""Safe Git subprocesses for ExoMonad E2E fixture inspection."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

GIT_REPOSITORY_SELECTION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def scrubbed_git_environment() -> dict[str, str]:
    """Return the process environment without inherited repository selection."""

    environment = os.environ.copy()
    for variable in GIT_REPOSITORY_SELECTION_VARIABLES:
        environment.pop(variable, None)
    return environment


def _canonical(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


def _within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _assert_fixture_repository(fixture_root: Path, command_directory: Path) -> None:
    """Reject a Git directory that resolves outside the fixture root."""

    result = subprocess.run(
        ["git", "-C", str(command_directory), "rev-parse", "--show-toplevel"],
        env=scrubbed_git_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        resolved = _canonical(result.stdout.strip())
        if _within(fixture_root, resolved):
            return
        raise RuntimeError(
            f"Git fixture resolved outside root: {resolved} (root {fixture_root})"
        )

    # Bare remotes do not have a working-tree top level. Their absolute Git
    # directory is the equivalent boundary for read-only inspection.
    result = subprocess.run(
        ["git", "-C", str(command_directory), "rev-parse", "--absolute-git-dir"],
        env=scrubbed_git_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Git fixture is not a repository: {command_directory}"
        )
    resolved_git_dir = _canonical(result.stdout.strip())
    if not _within(fixture_root, resolved_git_dir):
        raise RuntimeError(
            "Git fixture directory resolved outside root: "
            f"{resolved_git_dir} (root {fixture_root})"
        )


def run_fixture_git(
    args: Sequence[str],
    *,
    fixture_root: str | os.PathLike[str],
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = 5,
) -> subprocess.CompletedProcess[str]:
    """Run a scrubbed, root-checked Git command for an E2E fixture."""

    root = _canonical(fixture_root)
    command_directory = _canonical(cwd if cwd is not None else root)
    if not _within(root, command_directory):
        raise RuntimeError(
            f"Git fixture cwd escapes root: {command_directory} (root {root})"
        )
    _assert_fixture_repository(root, command_directory)
    return subprocess.run(
        ["git", "-C", str(command_directory), *args],
        env=scrubbed_git_environment(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
