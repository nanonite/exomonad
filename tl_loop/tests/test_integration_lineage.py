"""Real Git object and ancestry tests for aggregate readiness evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tl_loop.loop.integration_lineage import GitLineageError, verify_aggregate_lineage


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, filename: str, content: str, message: str) -> str:
    (repository / filename).write_text(content, encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Test Author")
    _git(path, "config", "user.email", "test@example.invalid")
    _commit(path, "base.txt", "base\n", "base")
    return path


def _tree(repository: Path, commit: str) -> str:
    return _git(repository, "rev-parse", f"{commit}^{{tree}}")


def test_aggregate_lineage_accepts_reviewed_merge_and_bookkeeping_ancestors(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "accepted")
    reviewed_merge = _commit(repository, "child.txt", "reviewed work\n", "reviewed merge")
    bookkeeping = _commit(repository, "CHANGELOG.md", "child entry\n", "bookkeeping")
    aggregate_head = _commit(repository, "aggregate.txt", "aggregate\n", "aggregate integration")

    proof = verify_aggregate_lineage(
        repository,
        aggregate_head,
        _tree(repository, aggregate_head),
        (reviewed_merge, bookkeeping),
    )

    assert proof.aggregate_head_sha == aggregate_head
    assert proof.aggregate_tree_sha == _tree(repository, aggregate_head)
    assert {relation.relation for relation in proof.relations} == {"ancestor"}
    assert {relation.receipt_commit for relation in proof.relations} == {
        reviewed_merge,
        bookkeeping,
    }


def test_aggregate_lineage_rejects_unrelated_head_even_when_its_tree_is_authoritative(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "unrelated")
    reviewed_merge = _commit(repository, "child.txt", "reviewed work\n", "reviewed merge")
    bookkeeping = _commit(repository, "CHANGELOG.md", "child entry\n", "bookkeeping")
    child_tip = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "--orphan", "unrelated")
    _git(repository, "rm", "-rf", ".")
    unrelated_head = _commit(repository, "unrelated.txt", "other work\n", "unrelated")

    with pytest.raises(GitLineageError, match="unrelated_aggregate_history"):
        verify_aggregate_lineage(
            repository,
            unrelated_head,
            _tree(repository, unrelated_head),
            (reviewed_merge, bookkeeping, child_tip),
        )


def test_aggregate_lineage_accepts_rebased_commits_with_unique_patch_proof(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "rebased")
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-b", "reviewed-child")
    reviewed_merge = _commit(repository, "child.txt", "reviewed work\n", "reviewed merge")
    bookkeeping = _commit(repository, "CHANGELOG.md", "child entry\n", "bookkeeping")
    _git(repository, "checkout", "main")
    _commit(repository, "base-followup.txt", "follow-up\n", "base follow-up")
    _git(repository, "checkout", "-b", "rebased-aggregate")
    _git(repository, "cherry-pick", reviewed_merge, bookkeeping)
    aggregate_head = _commit(repository, "aggregate.txt", "aggregate\n", "aggregate integration")

    proof = verify_aggregate_lineage(
        repository,
        aggregate_head,
        _tree(repository, aggregate_head),
        (reviewed_merge, bookkeeping),
    )

    assert all(relation.relation == "patch_equivalent_rebase" for relation in proof.relations)
    assert all(relation.aggregate_commit != relation.receipt_commit for relation in proof.relations)
    assert _git(repository, "merge-base", base, aggregate_head)


def test_aggregate_lineage_fails_closed_on_missing_object_and_tree_mismatch(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "missing")
    head = _git(repository, "rev-parse", "HEAD")
    with pytest.raises(GitLineageError, match="aggregate_tree_mismatch"):
        verify_aggregate_lineage(repository, head, "0" * 40, (head,))
    with pytest.raises(GitLineageError, match="child_commit_unavailable"):
        verify_aggregate_lineage(repository, head, _tree(repository, head), ("1" * 40,))


def test_aggregate_lineage_reports_missing_receipt_commit_without_type_error(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "malformed")
    head = _git(repository, "rev-parse", "HEAD")
    with pytest.raises(GitLineageError, match="malformed_commit_identity"):
        verify_aggregate_lineage(
            repository,
            head,
            _tree(repository, head),
            (None,),  # type: ignore[arg-type] - malformed historical receipt input
        )


@pytest.mark.parametrize(
    ("exception", "code"),
    (
        (FileNotFoundError("git"), "git_unavailable"),
        (subprocess.TimeoutExpired("git", 10), "git_timeout"),
    ),
)
def test_aggregate_lineage_distinguishes_git_tool_failures(
    tmp_path: Path,
    exception: Exception,
    code: str,
) -> None:
    repository = tmp_path / "unavailable"
    repository.mkdir()

    def fail_git(*arguments: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del arguments, kwargs
        raise exception

    with pytest.raises(GitLineageError, match=code):
        verify_aggregate_lineage(repository, "1" * 40, "2" * 40, ("3" * 40,), runner=fail_git)
