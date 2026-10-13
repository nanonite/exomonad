"""Durable, symlink-safe filesystem primitives for plan revision journals."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .plan_revision_types import PlanRevisionError


def controller_lifecycle_lock_path(project_root: Path) -> Path:
    """Return the shared lifecycle lock after rejecting symlinked parents."""
    state_root = project_root / ".exo" / "tl-loop"
    lock_path = state_root / "controller-lifecycle.lock"
    if any(path.is_symlink() for path in (project_root / ".exo", state_root, lock_path)):
        raise PlanRevisionError(
            "controller_lock_symlink",
            "controller lifecycle lock path contains a symlink",
        )
    return lock_path


def safe_target(root: Path, relative: str) -> Path:
    """Resolve a journal path beneath the project root and reject symlinks."""
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise PlanRevisionError("transition_path_invalid", "journal target path is unsafe")
    if candidate.parts[:2] != (".exo", "tl-loop") or len(candidate.parts) < 3:
        raise PlanRevisionError("transition_path_invalid", "journal target is outside TL state")
    target = root.joinpath(*candidate.parts)
    current = root
    for part in candidate.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise PlanRevisionError(
                "transition_path_symlink",
                f"refusing symlink in transition target path {relative}",
            )
    if target.is_symlink():
        raise PlanRevisionError(
            "transition_path_symlink",
            f"refusing symlink transition target {relative}",
        )
    return target


def relative_to_root(root: Path, target: Path) -> str:
    """Return a checked, portable project-relative journal target."""
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise PlanRevisionError(
            "transition_path_invalid",
            "plan revision target is outside the project directory",
        ) from error
    return PurePosixPath(*relative.parts).as_posix()


def read_regular_file(path: Path) -> tuple[bytes | None, int | None]:
    """Read a regular file without following symlinks, or return absent."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise PlanRevisionError("transition_read_failed", f"cannot read {path}: {error}") from error
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PlanRevisionError("transition_target_invalid", f"not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            contents = stream.read()
        return contents, stat.S_IMODE(file_stat.st_mode)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    """Write bytes through a same-directory fsynced temporary and rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PlanRevisionError("transition_path_symlink", f"refusing symlink target {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def fsync_directory(path: Path) -> None:
    """Persist directory entry changes where the filesystem supports it."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            unsupported = {errno.EINVAL, errno.ENOTSUP}
            if hasattr(errno, "EOPNOTSUPP"):
                unsupported.add(errno.EOPNOTSUPP)
            if error.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)
