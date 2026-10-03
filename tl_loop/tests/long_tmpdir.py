"""A ``TMPDIR`` long enough that no Unix socket path fits beneath it.

The lead verified #1137 with a 187-character ``TMPDIR``, which is where this
fixture's shape comes from: ``tempfile`` resolves its root from ``TMPDIR``, so
a directory like this hands every test that creates a temporary directory a
root whose socket paths the kernel refuses to bind. Tests use it to prove that
the socket roots in :mod:`tl_loop.tests.socket_root` are independent of it.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tl_loop.tests import socket_root

#: The prefix a long ``TMPDIR`` carries, so a leaked one stays attributable.
LONG_TMPDIR_PREFIX = "exo-tl-test-longtmpdir-"

#: How long the fixture's ``TMPDIR`` has to be. Longer than
#: :data:`~tl_loop.tests.socket_root.MAX_SOCKET_PATH_BYTES` so that no socket
#: fits under it, and past 150 to match the reported lead environment.
MIN_LONG_TMPDIR_CHARS = 150


@contextmanager
def long_tmpdir(label: str) -> Iterator[Path]:
    """Point ``TMPDIR`` and ``tempfile`` at a directory no socket fits under.

    ``tempfile`` caches the directory it resolved, so setting ``TMPDIR`` alone
    would not move it; the cached value is pointed at the long directory too,
    and both are restored afterwards.
    """
    directory = _long_root(label)
    directory.mkdir(parents=True)
    previous_env = os.environ.get("TMPDIR")
    previous_tempdir = tempfile.tempdir
    try:
        os.environ["TMPDIR"] = str(directory)
        tempfile.tempdir = str(directory)
        yield directory
    finally:
        tempfile.tempdir = previous_tempdir
        if previous_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_env
        shutil.rmtree(directory, ignore_errors=True)


def _long_root(label: str) -> Path:
    """Return a path long enough to refuse a socket and to clear 150 characters.

    Padding the name keeps the length independent of the label and of the
    process id, and keeps the fixture a single directory under ``/tmp`` whether
    or not the caller's ``TMPDIR`` is usable.
    """
    candidate = Path(socket_root.SHORT_ROOT_BASE) / f"{LONG_TMPDIR_PREFIX}{label}-{os.getpid()}"
    while _socket_fits_beneath(candidate) or len(str(candidate)) <= MIN_LONG_TMPDIR_CHARS:
        candidate = candidate.with_name(f"{candidate.name}x")
    return candidate


def _socket_fits_beneath(root: Path) -> bool:
    """Report whether a socket root under ``root`` would still be bindable."""
    path = root / socket_root.SERVER_SOCKET_NAME
    return len(str(path).encode("utf-8")) <= socket_root.MAX_SOCKET_PATH_BYTES


__all__ = ["LONG_TMPDIR_PREFIX", "MIN_LONG_TMPDIR_CHARS", "long_tmpdir"]
