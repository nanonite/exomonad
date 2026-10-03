"""The socket root is short enough to bind, whatever ``TMPDIR`` says."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tl_loop.tests import socket_root
from tl_loop.tests.long_tmpdir import long_tmpdir


def test_a_long_tmpdir_cannot_produce_an_unbindable_socket() -> None:
    """An inherited root is refused, and the harness's own roots still fit.

    ``sun_path`` is 107 bytes, so a directory beneath a long ``TMPDIR`` yields
    socket paths the kernel refuses to bind. That is the shape that broke the
    transport tests during #1137 lead verification, where ``TMPDIR`` was 187
    characters.
    """
    with long_tmpdir("socket-root") as base:
        assert len(str(base)) > socket_root.MAX_SOCKET_PATH_BYTES, str(base)
        inherited = Path(tempfile.mkdtemp())
        try:
            with pytest.raises(socket_root.SocketRootError, match="kernel allows"):
                socket_root.socket_path(inherited)
        finally:
            inherited.rmdir()

        with socket_root.temporary_short_root() as root:
            assert root.is_relative_to(socket_root.SHORT_ROOT_BASE), str(root)
            assert socket_root.socket_path(root).parent == root


def test_a_short_root_cannot_be_bound_after_a_setup_failure() -> None:
    """A bind that fails leaves no short root behind.

    Short roots live under ``/tmp``, outside every test run's own directory, so
    nothing would ever sweep one. The allocation has to be inside the removal
    for that to hold.
    """
    before = set(socket_root.short_roots())
    with pytest.raises(socket_root.SocketRootError), socket_root.temporary_short_root() as root:
        raise socket_root.SocketRootError(f"socket path is too long: {root}")
    assert set(socket_root.short_roots()) == before


def test_a_socket_path_under_the_ceiling_is_returned_unchanged() -> None:
    """The check refuses only what the kernel would refuse."""
    with socket_root.temporary_short_root() as root:
        path = socket_root.socket_path(root, "custom.sock")
        assert path == root / "custom.sock"
