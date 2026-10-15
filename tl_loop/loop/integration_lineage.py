"""Git-backed proof that completed child commits are present in an aggregate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GIT_TIMEOUT_SECONDS = 10


class GitRunner(Protocol):
    """The small subprocess surface used by the lineage verifier."""

    def __call__(self, *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Run a command and capture its byte output."""


class GitLineageError(ValueError):
    """A stable, safe reason why aggregate commit lineage could not be proved."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class CommitRelation:
    """How one receipt commit is represented in the aggregate history."""

    receipt_commit: str
    aggregate_commit: str
    relation: str


@dataclass(frozen=True)
class AggregateLineageProof:
    """Commit and tree facts verified against the local repository object store."""

    aggregate_head_sha: str
    aggregate_tree_sha: str
    relations: tuple[CommitRelation, ...]
    digest: str


def verify_aggregate_lineage(
    repository: str | Path,
    aggregate_head_sha: str,
    expected_head_tree_sha: str,
    receipt_commits: tuple[str, ...],
    *,
    runner: GitRunner = subprocess.run,
) -> AggregateLineageProof:
    """Verify exact head tree and prove each receipt commit is present or rebased."""
    repository_path = Path(repository)
    if not repository_path.is_dir():
        raise GitLineageError("repository_unavailable", "aggregate worktree is missing")
    _require_object_id(aggregate_head_sha, "aggregate head")
    _require_object_id(expected_head_tree_sha, "watcher head tree")
    if not receipt_commits:
        raise GitLineageError("child_commit_proof_missing", "no completed child commits supplied")

    root = _git(repository_path, ("rev-parse", "--show-toplevel"), runner)
    if root.returncode != 0:
        raise GitLineageError("repository_unavailable", "aggregate path is not a Git worktree")
    resolved_head = _git(
        repository_path, ("rev-parse", "--verify", f"{aggregate_head_sha}^{{commit}}"), runner
    )
    head_sha = _output_identity(resolved_head, "aggregate_head_unavailable")
    if head_sha != aggregate_head_sha:
        raise GitLineageError("aggregate_head_mismatch", "Git resolved a different aggregate head")
    tree_result = _git(
        repository_path, ("rev-parse", "--verify", f"{aggregate_head_sha}^{{tree}}"), runner
    )
    tree_sha = _output_identity(tree_result, "aggregate_tree_unavailable")
    if tree_sha != expected_head_tree_sha:
        raise GitLineageError(
            "aggregate_tree_mismatch",
            "local aggregate head tree differs from the authoritative watcher tree",
        )

    relations: list[CommitRelation] = []
    for receipt_commit in receipt_commits:
        _require_object_id(receipt_commit, "completed child commit")
    for receipt_commit in sorted(set(receipt_commits)):
        commit_result = _git(
            repository_path, ("rev-parse", "--verify", f"{receipt_commit}^{{commit}}"), runner
        )
        resolved_commit = _output_identity(commit_result, "child_commit_unavailable")
        if resolved_commit != receipt_commit:
            raise GitLineageError("child_commit_mismatch", "Git resolved a different child commit")
        relation = _commit_relation(repository_path, receipt_commit, aggregate_head_sha, runner)
        relations.append(relation)

    proof_document = {
        "aggregate_head_sha": aggregate_head_sha,
        "aggregate_tree_sha": tree_sha,
        "relations": [
            {
                "receipt_commit": item.receipt_commit,
                "aggregate_commit": item.aggregate_commit,
                "relation": item.relation,
            }
            for item in relations
        ],
    }
    digest = hashlib.sha256(
        json.dumps(proof_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AggregateLineageProof(aggregate_head_sha, tree_sha, tuple(relations), digest)


def _commit_relation(
    repository: Path,
    receipt_commit: str,
    aggregate_head: str,
    runner: GitRunner,
) -> CommitRelation:
    ancestry = _git(
        repository,
        ("merge-base", "--is-ancestor", receipt_commit, aggregate_head),
        runner,
    )
    if ancestry.returncode == 0:
        return CommitRelation(receipt_commit, receipt_commit, "ancestor")
    if ancestry.returncode != 1:
        raise GitLineageError("git_ancestry_unavailable", _stderr(ancestry))
    rebased_commit = _find_rebased_commit(repository, receipt_commit, aggregate_head, runner)
    return CommitRelation(receipt_commit, rebased_commit, "patch_equivalent_rebase")


def _find_rebased_commit(
    repository: Path,
    receipt_commit: str,
    aggregate_head: str,
    runner: GitRunner,
) -> str:
    parent_result = _git(repository, ("rev-parse", "--verify", f"{receipt_commit}^1"), runner)
    source_parent = _output_identity(parent_result, "rebase_source_parent_unavailable")
    base_result = _git(repository, ("merge-base", source_parent, aggregate_head), runner)
    if base_result.returncode != 0:
        raise GitLineageError(
            "unrelated_aggregate_history", "child and aggregate have no common base"
        )
    common_base = _output_identity(base_result, "aggregate_common_base_unavailable")
    source_patch = _patch_id(repository, receipt_commit, runner)
    if source_patch is None:
        raise GitLineageError(
            "rebase_patch_unavailable", "completed child commit has no verifiable patch"
        )
    history_result = _git(repository, ("rev-list", f"{common_base}..{aggregate_head}"), runner)
    if history_result.returncode != 0:
        raise GitLineageError("aggregate_history_unavailable", _stderr(history_result))
    matches = [
        commit
        for commit in _decode_lines(history_result.stdout)
        if _patch_id(repository, commit, runner) == source_patch
    ]
    if len(matches) != 1:
        code = "ambiguous_rebase_match" if matches else "child_patch_not_in_aggregate"
        raise GitLineageError(
            code, "aggregate history does not contain one unique equivalent child patch"
        )
    return matches[0]


def _patch_id(repository: Path, commit: str, runner: GitRunner) -> str | None:
    parent = _git(repository, ("rev-parse", "--verify", f"{commit}^1"), runner)
    if parent.returncode != 0:
        return None
    parent_sha = _output_identity(parent, "commit_parent_unavailable")
    diff = _git(repository, ("diff", "--binary", "--no-ext-diff", parent_sha, commit), runner)
    if diff.returncode != 0:
        raise GitLineageError("commit_patch_unavailable", _stderr(diff))
    patch = _git(repository, ("patch-id", "--stable"), runner, input_bytes=diff.stdout)
    if patch.returncode != 0:
        raise GitLineageError("patch_id_unavailable", _stderr(patch))
    lines = _decode_lines(patch.stdout)
    return lines[0].split()[0] if lines else None


def _git(
    repository: Path,
    arguments: tuple[str, ...],
    runner: GitRunner,
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return runner(
            ("git", "-C", str(repository), *arguments),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GitLineageError(
            "git_timeout", "Git lineage verification exceeded 10 seconds"
        ) from error
    except OSError as error:
        raise GitLineageError(
            "git_unavailable", "Git lineage verification could not start"
        ) from error


def _output_identity(result: subprocess.CompletedProcess[bytes], error_code: str) -> str:
    if result.returncode != 0:
        raise GitLineageError(error_code, _stderr(result))
    identity = result.stdout.decode("ascii", errors="replace").strip()
    _require_object_id(identity, error_code)
    return identity


def _require_object_id(value: object, label: str) -> None:
    if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
        raise GitLineageError("malformed_commit_identity", f"{label} is not a full Git object ID")


def _decode_lines(value: bytes) -> list[str]:
    return [line for line in value.decode("ascii", errors="replace").splitlines() if line]


def _stderr(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    return detail[:300] or f"Git exited with status {result.returncode}"


__all__ = [
    "AggregateLineageProof",
    "CommitRelation",
    "GitLineageError",
    "GitRunner",
    "verify_aggregate_lineage",
]
