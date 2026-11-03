"""Fresh, read-only Git observations used by declared reference checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tl_loop.state.reference_verification import (
    ReferenceProjectObservation,
    ReferenceSourceState,
    ReferenceVerificationRequirement,
)
from tl_loop.state.repository_identity import canonical_repository_url

_GIT_TIMEOUT_SECONDS = 5


def observe_reference_project(
    workspace_root: str | Path | None,
    requirement: ReferenceVerificationRequirement,
) -> ReferenceProjectObservation:
    """Observe identity, revision, and cleanliness without fetching or executing checks."""
    if workspace_root is None:
        return _unavailable(requirement)
    try:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        project = (root / requirement.path).resolve(strict=True)
        if not project.is_relative_to(root) or not project.is_dir():
            return _unavailable(requirement)
    except (OSError, RuntimeError):
        return _uninitialized(requirement)
    parent_pointer = _gitlink_pointer(root, requirement.path)
    if parent_pointer is None:
        return _unavailable(requirement)
    if not (project / ".git").exists():
        return _uninitialized(requirement) if _empty(project) else _unavailable(requirement)

    top_level = _git(project, "rev-parse", "--show-toplevel")
    if top_level is None or Path(top_level).resolve() != project:
        return _unavailable(requirement)
    remote_url = _observe_remote(project, requirement.remote_url)
    commit = _git(project, "rev-parse", "--verify", "HEAD")
    if remote_url is None or commit is None or not _valid_oid(commit) or commit != parent_pointer:
        return ReferenceProjectObservation(
            requirement.path,
            remote_url,
            commit if _valid_oid(commit) else None,
            ReferenceSourceState.UNAVAILABLE,
        )
    status = _git_bytes(
        project,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status is None:
        source_state = ReferenceSourceState.UNAVAILABLE
    else:
        source_state = ReferenceSourceState.CLEAN if not status else ReferenceSourceState.DIRTY
    return ReferenceProjectObservation(requirement.path, remote_url, commit, source_state)


def _gitlink_pointer(root: Path, path: str) -> str | None:
    tree = _git_bytes(root, "ls-tree", "-z", "HEAD", "--", path)
    index = _git_bytes(root, "ls-files", "--stage", "-z", "--", path)
    if tree is None or index is None:
        return None
    tree_record = tree.rstrip(b"\0")
    index_record = index.rstrip(b"\0")
    if not tree_record or not index_record or b"\0" in tree_record or b"\0" in index_record:
        return None
    try:
        tree_metadata, tree_path = tree_record.split(b"\t", 1)
        index_metadata, index_path = index_record.split(b"\t", 1)
        tree_mode, tree_type, tree_oid = tree_metadata.split(b" ", 2)
        index_mode, index_oid, stage = index_metadata.split(b" ", 2)
    except ValueError:
        return None
    if (
        tree_path != os.fsencode(path)
        or index_path != tree_path
        or tree_mode != b"160000"
        or tree_type != b"commit"
        or index_mode != tree_mode
        or index_oid != tree_oid
        or stage != b"0"
    ):
        return None
    try:
        return tree_oid.decode("ascii")
    except UnicodeDecodeError:
        return None


def _observe_remote(project: Path, expected: str) -> str | None:
    names = _git(project, "remote")
    if names is None:
        return None
    urls = []
    for name in names.splitlines():
        remote = _git(project, "remote", "get-url", name)
        if remote is None:
            continue
        try:
            urls.append(canonical_repository_url(remote))
        except ValueError:
            continue
    matches = [url for url in urls if url == expected]
    if len(matches) == 1:
        return expected
    unique = set(urls)
    return next(iter(unique)) if len(unique) == 1 else None


def _git(project: Path, *arguments: str) -> str | None:
    output = _run_git(project, *arguments)
    if output is None or output[0] != 0:
        return None
    try:
        return output[1].decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def _git_bytes(project: Path, *arguments: str) -> bytes | None:
    output = _run_git(project, *arguments)
    return output[1] if output is not None and output[0] == 0 else None


def _run_git(project: Path, *arguments: str) -> tuple[int, bytes] | None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(project), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode, result.stdout


def _empty(path: Path) -> bool:
    try:
        return next(path.iterdir(), None) is None
    except OSError:
        return False


def _uninitialized(requirement: ReferenceVerificationRequirement) -> ReferenceProjectObservation:
    return ReferenceProjectObservation(
        requirement.path, None, None, ReferenceSourceState.UNINITIALIZED
    )


def _unavailable(requirement: ReferenceVerificationRequirement) -> ReferenceProjectObservation:
    return ReferenceProjectObservation(
        requirement.path, None, None, ReferenceSourceState.UNAVAILABLE
    )


def _valid_oid(value: str | None) -> bool:
    return (
        value is not None
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )
