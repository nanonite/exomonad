"""A Unix-socket root short enough to bind, whatever ``TMPDIR`` says.

``sockaddr_un.sun_path`` holds 108 bytes on Linux, so a socket path may be 107.
``tempfile`` -- and therefore ``tempfile.TemporaryDirectory`` -- resolves its
root from ``TMPDIR``, and a caller whose ``TMPDIR`` is long enough that nothing
socket-shaped fits underneath it turns every test that binds a Unix socket
into ``OSError: AF_UNIX path too long``. That is not hypothetical: a lead
verified #1137 with a 187-character ``TMPDIR`` and four transport tests failed
on it while the rest of the suite passed.

A root that will hold a socket is therefore created under ``/tmp`` explicitly,
and the path's length is checked where the path is built -- next to the value
that produced it -- rather than at bind time, where the only symptom is an
``OSError`` from whichever end arrived first.

This mirrors the e2e harness's ``e2e_harness.tmuxio`` rules, which were the
first thing in this repository to hit the same ceiling (#1117).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Where a short root is created, so ``TMPDIR`` cannot decide its length.
SHORT_ROOT_BASE = "/tmp"

#: The prefix a short root carries, so it stays attributable to these tests.
SHORT_ROOT_PREFIX = "exo-tl-test-sock-"

#: The length of ``sockaddr_un.sun_path`` on Linux, excluding its terminating
#: NUL. A socket path longer than this cannot be bound or connected at all.
MAX_SOCKET_PATH_BYTES = 107

#: The name the transport tests bind their stub server to.
SERVER_SOCKET_NAME = "server.sock"


class SocketRootError(RuntimeError):
    """Raised for a socket path the kernel could not bind."""


def short_root(prefix: str = SHORT_ROOT_PREFIX) -> Path:
    """Create a root short enough for a Unix socket, ignoring ``TMPDIR``.

    ``mkdtemp``'s own random suffix keeps two callers apart. The root is
    created outside every caller's own run directory, so it is the caller's to
    remove; prefer :func:`temporary_short_root`, which removes it for the body
    of a ``with``.
    """
    return _fit(Path(tempfile.mkdtemp(dir=SHORT_ROOT_BASE, prefix=prefix)))


@contextmanager
def temporary_short_root(prefix: str = SHORT_ROOT_PREFIX) -> Iterator[Path]:
    """Create a short socket root for the body of a ``with`` and remove it after.

    The allocation happens inside the block that removes it: a failure in
    ``mkdir``, in ``bind``, or in any assertion between them cannot leave an
    ``/tmp/exo-tl-test-sock-*`` directory behind. An abandoned short root sits
    outside every test run's directory prefix, so no sweep of a run would ever
    find it.
    """
    root = short_root(prefix)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def socket_path(root: Path, name: str = SERVER_SOCKET_NAME) -> Path:
    """Return the socket path under ``root``, refusing one the kernel cannot bind.

    The check belongs here rather than at bind time so the diagnosis arrives
    with the root that caused it.
    """
    return _fit(Path(root) / name)


def short_roots(prefix: str = SHORT_ROOT_PREFIX) -> list[Path]:
    """Return every short root carrying ``prefix``, live or abandoned.

    A short root has no run directory tying it to a caller, so this is the only
    way a test can see one at all; deciding whether one is stale is the
    caller's job.
    """
    return sorted(path for path in Path(SHORT_ROOT_BASE).glob(f"{prefix}*") if path.is_dir())


def _fit(path: Path) -> Path:
    """Refuse a path the kernel could not bind."""
    length = len(str(path).encode("utf-8"))
    if length > MAX_SOCKET_PATH_BYTES:
        raise SocketRootError(
            f"socket path is {length} bytes and the kernel allows {MAX_SOCKET_PATH_BYTES}: {path}"
        )
    return path


__all__ = [
    "MAX_SOCKET_PATH_BYTES",
    "SERVER_SOCKET_NAME",
    "SHORT_ROOT_BASE",
    "SHORT_ROOT_PREFIX",
    "SocketRootError",
    "short_root",
    "short_roots",
    "socket_path",
    "temporary_short_root",
]
