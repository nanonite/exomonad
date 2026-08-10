"""Single atomic mutation path for durable TL run state."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeAlias

from .lock import RunLock
from .schema import SchemaError, validate

Document: TypeAlias = dict[str, object]
Mutator: TypeAlias = Callable[[Document], Document]


class ConcurrentWrite(RuntimeError):
    """The run state changed after this mutation observed it."""


class StateReadError(RuntimeError):
    """The run-state file could not be read as a JSON object."""


class MutationError(TypeError):
    """A mutator did not return a JSON object."""


@dataclass(frozen=True)
class WriteHooks:
    """Test-only seam invoked after temp fsync and before the final CAS."""

    before_rename: Callable[[], None] | None = None


@dataclass(frozen=True)
class _Snapshot:
    document: Document
    revision: int
    mtime_ns: int
    digest: str
    inode: tuple[int, int]


def apply(
    run_dir: str | Path,
    mutator: Mutator,
    *,
    lock_timeout: float = 5.0,
    hooks: WriteHooks | None = None,
) -> Document:
    """Apply one validated mutation through the only run-state write path."""
    if not callable(mutator):
        raise TypeError("run-state mutator must be callable")
    directory = Path(run_dir)
    target = directory / "run.json"
    lock_path = directory.parent / "run.lock"

    with RunLock(lock_path, timeout=lock_timeout):
        observed = _read_valid_snapshot(target)
        candidate_value = mutator(copy.deepcopy(observed.document))
        if not isinstance(candidate_value, dict):
            raise MutationError("run-state mutator must return an object")
        candidate = copy.deepcopy(candidate_value)
        candidate["revision"] = observed.revision + 1
        validate(candidate)

        _assert_unchanged(target, observed)
        reobserved = _read_valid_snapshot(target)
        _assert_same_snapshot(observed, reobserved)

        temporary = _write_temp(directory, candidate)
        try:
            if hooks and hooks.before_rename:
                hooks.before_rename()
            _assert_unchanged(target, observed)
            _replace_atomically(temporary, target, directory)
        except BaseException:
            _remove_temp(temporary)
            raise
        return candidate


def _read_valid_snapshot(target: Path) -> _Snapshot:
    try:
        data = _read_bytes(target)
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StateReadError(f"could not read run state {target}: {error}") from error
    if not isinstance(value, dict):
        raise StateReadError(f"run state {target} must contain a JSON object")
    document = value
    validate(document)
    stat = target.stat()
    revision = document.get("revision")
    if type(revision) is not int:
        raise SchemaError([("run.revision", "must be an integer")])
    return _Snapshot(
        document=document,
        revision=revision,
        mtime_ns=stat.st_mtime_ns,
        digest=hashlib.sha256(data).hexdigest(),
        inode=(stat.st_dev, stat.st_ino),
    )


def _read_bytes(target: Path) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, os.O_RDONLY | nofollow)
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


def _assert_unchanged(target: Path, observed: _Snapshot) -> None:
    current = _read_valid_snapshot(target)
    _assert_same_snapshot(observed, current)


def _assert_same_snapshot(observed: _Snapshot, current: _Snapshot) -> None:
    if (
        observed.revision != current.revision
        or observed.mtime_ns != current.mtime_ns
        or observed.digest != current.digest
        or observed.inode != current.inode
    ):
        raise ConcurrentWrite("run state changed during the mutation")


def _write_temp(directory: Path, candidate: Document) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=".run.json.", suffix=".tmp", dir=directory)
    temporary = Path(raw_path)
    payload = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _remove_temp(temporary)
        raise
    return temporary


def _replace_atomically(temporary: Path, target: Path, directory: Path) -> None:
    try:
        target_stat = target.lstat()
    except FileNotFoundError as error:
        raise ConcurrentWrite(f"run state disappeared before commit: {target}") from error
    if not stat.S_ISREG(target_stat.st_mode):
        raise StateReadError(f"run state target is not a regular file: {target}")
    os.replace(temporary, target)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.EPERM, errno.EACCES, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _remove_temp(temporary: Path) -> None:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass


__all__ = [
    "ConcurrentWrite",
    "Document",
    "MutationError",
    "StateReadError",
    "WriteHooks",
    "apply",
]
