"""Seed and verify unchanged, uninitialized reference submodules in Phase 8."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

_REFERENCES = {
    "tools/llmlift": "llmlift",
    "tools/skillopt": "skillopt",
}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def seed(repository: Path, projects: Path, output: Path) -> None:
    references = []
    for relative_path, name in _REFERENCES.items():
        source, remote = _create_reference_project(projects, name)
        commit = _git(source, "rev-parse", "HEAD")
        references.append(
            {"path": relative_path, "name": name, "commit": commit, "url": str(remote)}
        )
        _git(
            repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},{relative_path}",
        )
        (repository / relative_path).mkdir(parents=True, exist_ok=True)
        _assert_uninitialized(repository / relative_path)
        if not remote.is_dir():
            raise RuntimeError(f"reference remote was not created for {relative_path}")
    modules = []
    for entry in references:
        modules.extend(
            [
                f'[submodule "{entry["path"]}"]',
                f"path = {entry['path']}",
                f"url = {entry['url']}",
                "",
            ]
        )
    (repository / ".gitmodules").write_text("\n".join(modules), encoding="utf-8")
    _git(repository, "add", ".gitmodules")
    output.write_text(json.dumps({"references": references}, sort_keys=True) + "\n")


def _create_reference_project(projects: Path, name: str) -> tuple[Path, Path]:
    source = projects / name
    remote = projects / f"{name}.git"
    source.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    _git(source, "config", "user.name", "Reference fixture")
    _git(source, "config", "user.email", "reference-fixture@example.invalid")
    (source / "README.md").write_text(
        f"Reference-only fixture for {name}.\n", encoding="utf-8"
    )
    _git(source, "add", "README.md")
    _git(source, "commit", "-q", "-m", "reference fixture")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-q", "origin", "main")
    return source, remote


def verify(repository: Path, baseline_path: Path) -> None:
    baseline: Any = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["commit"] for entry in baseline["references"]}
    head = _tree_entries(repository)
    index = _index_entries(repository)
    if head != expected:
        raise RuntimeError(
            f"HEAD gitlinks changed: expected {expected!r}, found {head!r}"
        )
    if index != expected:
        raise RuntimeError(
            f"index gitlinks changed: expected {expected!r}, found {index!r}"
        )
    for path in expected:
        _assert_uninitialized(repository / path)
    modules = repository / ".git" / "modules"
    if modules.exists():
        raise RuntimeError("reference submodules were initialized during handoff")
    _git(repository, "diff", "--quiet", "HEAD", "--", *expected)
    _git(repository, "diff", "--cached", "--quiet", "HEAD", "--", *expected)


def _tree_entries(repository: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _git(
        repository, "ls-tree", "-r", "HEAD", "--", *_REFERENCES
    ).splitlines():
        metadata, path = line.split("\t", 1)
        mode, object_type, oid = metadata.split()
        if mode != "160000" or object_type != "commit":
            raise RuntimeError(f"HEAD entry is not a gitlink at {path}")
        result[path] = oid
    return result


def _index_entries(repository: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _git(
        repository, "ls-files", "--stage", "--", *_REFERENCES
    ).splitlines():
        metadata, path = line.split("\t", 1)
        mode, oid, stage = metadata.split()
        if mode != "160000" or stage != "0":
            raise RuntimeError(f"index entry is not a resolved gitlink at {path}")
        result[path] = oid
    return result


def _assert_uninitialized(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(
            f"reference path must remain an empty submodule directory: {path}"
        )
    if any(path.iterdir()) or (path / ".git").exists():
        raise RuntimeError(f"reference submodule was initialized or modified: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("--repo", type=Path, required=True)
    seed_parser.add_argument("--projects", type=Path, required=True)
    seed_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--repo", type=Path, required=True)
    verify_parser.add_argument("--baseline", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "seed":
        seed(arguments.repo, arguments.projects, arguments.output)
    else:
        verify(arguments.repo, arguments.baseline)


if __name__ == "__main__":
    main()
