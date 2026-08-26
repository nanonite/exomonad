"""Build and compare source fingerprints for the packaged TL controller."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FINGERPRINT_FILENAME = "_build_fingerprint.json"
FINGERPRINT_SCHEMA_VERSION = 1
EXCLUDED_NAMES = frozenset(
    {
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        "tests",
        "__pycache__",
        FINGERPRINT_FILENAME,
    }
)


def _source_files(source: Path) -> list[Path]:
    """Return exactly the regular files copied into the archive."""
    return [
        path
        for path in sorted(source.rglob("*"))
        if path.is_file()
        and not any(part in EXCLUDED_NAMES for part in path.relative_to(source).parts)
        and not path.name.endswith(".pyc")
    ]


def source_tree_sha256(source: Path) -> str:
    """Hash archived relative names and bytes in deterministic order."""
    digest = hashlib.sha256()
    for path in _source_files(source):
        relative = path.relative_to(source).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_commit(source: Path) -> str:
    """Return the source checkout's commit, or ``unknown`` outside Git."""
    try:
        result = subprocess.run(
            ["git", "-C", str(source.resolve().parent), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    value = result.stdout.strip()
    return value or "unknown"


def source_fingerprint(source: Path) -> dict[str, Any]:
    """Compute the fingerprint expected for a source tree."""
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "git_commit": git_commit(source),
        "tree_sha256": source_tree_sha256(source),
    }


def _validated(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("controller fingerprint must be an object")
    schema_version = value.get("schema_version")
    git_sha = value.get("git_commit")
    tree_sha = value.get("tree_sha256")
    if (
        type(schema_version) is not int
        or schema_version != FINGERPRINT_SCHEMA_VERSION
        or not isinstance(git_sha, str)
        or not git_sha
        or not isinstance(tree_sha, str)
        or len(tree_sha) != hashlib.sha256().digest_size * 2
    ):
        raise ValueError("controller fingerprint has an unsupported shape")
    return {
        "schema_version": schema_version,
        "git_commit": git_sha,
        "tree_sha256": tree_sha,
    }


def embedded_fingerprint() -> dict[str, Any] | None:
    """Read the build stamp embedded in a zipapp, if this is source code."""
    try:
        resource = importlib.resources.files("tl_loop").joinpath(FINGERPRINT_FILENAME)
        value = json.loads(resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError):
        return None
    return _validated(value)


def fingerprint_report(project_root: Path) -> dict[str, Any]:
    """Compare the running archive stamp with the checkout at ``project_root``."""
    source = project_root / "tl_loop"
    expected = source_fingerprint(source) if source.is_dir() else None
    try:
        embedded = embedded_fingerprint()
    except (TypeError, ValueError) as error:
        return {"status": "invalid", "source": expected, "archive": None, "error": str(error)}
    if embedded is None:
        status = "source"
    elif expected is None:
        status = "source-unavailable"
    elif embedded == expected:
        status = "current"
    else:
        status = "stale"
    return {"status": status, "source": expected, "archive": embedded}
