"""The only place a harness is allowed to talk to tmux.

Two rules follow from an incident in which a harness run inside a tmux pane
killed the operator's server instead of its own:

* **Every tmux call names its server.** ``tmux`` resolves the server from the
  inherited ``TMUX`` variable *before* it looks at ``TMUX_TMPDIR``, so a bare
  ``tmux`` started from inside a pane talks to whichever server owns that pane
  -- the operator's. Every invocation here therefore passes ``-S`` with an
  absolute socket path inside the run's own directory, and runs with ``TMUX``
  and ``TMUX_PANE`` stripped so the flag cannot be second-guessed.
* **Every child gets an environment that cannot reach an outer server.**
  ``child_env`` removes ``TMUX`` and ``TMUX_PANE`` and points ``TMUX_TMPDIR``
  at the run's directory. The shipped binary invokes tmux without ``-S``, so it
  resolves ``$TMUX_TMPDIR/tmux-<uid>/default`` -- which is exactly the socket
  ``socket_path`` returns. That is what keeps the server ``exomonad init``
  creates, and the windows it spawns agents into, on the run's own server.

Nothing else in ``tests/e2e`` may invoke the tmux binary directly; a contract
test fails the build if any does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping

#: The default socket name tmux uses under a ``TMUX_TMPDIR``.
SOCKET_NAME = "default"

#: Where a short root is created when the caller has none of its own.
SHORT_ROOT_BASE = "/tmp"

#: The prefix a short root carries, so it stays attributable to this harness.
SHORT_ROOT_PREFIX = "exo-e2e-sock-"

#: The length of ``sockaddr_un.sun_path`` on Linux, excluding its terminating
#: NUL. A Unix socket whose path is longer cannot be bound or connected at all,
#: and the failure arrives as ``OSError: AF_UNIX path too long`` from whichever
#: end gets there first.
MAX_SOCKET_PATH_BYTES = 107

#: Bounded wait for a tmux command to answer. tmux answers promptly unless its
#: server is wedged, so this only bounds a hang.
COMMAND_TIMEOUT_SECONDS = 30.0

#: Bounded wait for a server to stop listening after it was asked to.
STOP_TIMEOUT_SECONDS = 15.0


class TmuxError(RuntimeError):
    """Raised when a tmux command could not be run against a named server."""


def socket_path(root: Path) -> Path:
    """Return the absolute socket path the run's tmux server listens on.

    This is tmux's own rule for a server started without ``-S``: the socket
    lives at ``$TMUX_TMPDIR/tmux-<uid>/<name>``. Mirroring it exactly is what
    lets a harness that passes ``-S`` and a shipped binary that does not meet
    on the same server.

    The length is checked here rather than at bind time: ``sun_path`` is 108
    bytes, so a root inherited from a long ``TMPDIR`` produces a path tmux
    cannot bind, and it fails far from the value that caused it. Refusing at
    construction keeps the diagnosis next to the input.
    """
    return _fit(Path(root) / f"tmux-{os.getuid()}" / SOCKET_NAME)


def short_root(prefix: str = SHORT_ROOT_PREFIX) -> Path:
    """Create a root short enough for a Unix socket, ignoring ``TMPDIR``.

    ``tempfile`` honours ``TMPDIR``, and a caller's ``TMPDIR`` can be long
    enough that nothing socket-shaped fits underneath it. A socket root is
    therefore created under ``/tmp`` explicitly, with ``mkdtemp``'s own random
    suffix keeping two callers apart, and is the caller's to remove.
    """
    directory = Path(tempfile.mkdtemp(dir=SHORT_ROOT_BASE, prefix=prefix))
    return _fit(directory)


def _fit(path: Path) -> Path:
    """Refuse a socket path the kernel could not bind."""
    length = len(str(path).encode("utf-8"))
    if length > MAX_SOCKET_PATH_BYTES:
        raise TmuxError(
            f"socket path is {length} bytes and the kernel allows "
            f"{MAX_SOCKET_PATH_BYTES}: {path}"
        )
    return path


def ensure(socket: Path) -> Path:
    """Create the directory a socket lives in, so a server can be started there.

    tmux does not create the socket's parent directory: asked to start a server
    whose socket path cannot be created it reports the failure on stderr and
    still exits 0, which would let ``check=True`` pass while nothing was ever
    started. Creating the directory first is what makes the exit status mean
    what it says.

    It is created mode 0700 because tmux refuses a socket directory with wider
    permissions -- it holds a socket that authorises whoever can reach it --
    and reports that as a failed ``new-session``.
    """
    directory = Path(socket).parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return socket


def child_env(root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment every subprocess of a run must be started with.

    ``TMUX`` and ``TMUX_PANE`` are removed because tmux reads them before any
    flag: a child that inherits them resolves the *outer* server, which is how a
    harness can end up starting, and then killing, somebody else's sessions.
    ``TMUX_TMPDIR`` points at the run's directory so an inner tmux call that
    does not pass ``-S`` -- the shipped binary makes several -- lands on the
    run's own socket. It deliberately creates nothing: see ``ensure``.
    """
    environment = dict(base if base is not None else os.environ)
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    environment["TMUX_TMPDIR"] = str(root)
    return environment


def tmux(
    socket: Path,
    *arguments: str,
    check: bool = False,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one tmux command against the server named by ``socket``.

    The command is always run with ``TMUX`` and ``TMUX_PANE`` removed, so the
    ``-S`` flag is the only thing that decides which server answers -- even when
    this harness itself is running inside a pane.

    ``env`` is the caller's own environment for this command and nothing more:
    it is what ``child_env`` builds the run's environment from, so a caller that
    starts the server can put the run's ``PATH`` and database on it. Whatever it
    carries, ``TMUX`` and ``TMUX_PANE`` are stripped from the result.
    """
    if shutil.which("tmux") is None:
        raise TmuxError("tmux is not installed")
    # Only starting a server creates the directory. A read-only probe must not:
    # after a teardown has removed the run directory, a leak check that recreated
    # it would report the run as leaked by its own doing.
    if arguments and arguments[0] == "new-session":
        ensure(socket)
    result = subprocess.run(
        ["tmux", "-S", str(socket), *arguments],
        env=child_env(_socket_root(socket), env),
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode:
        raise TmuxError(
            f"tmux {' '.join(arguments)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def _socket_root(socket: Path) -> Path:
    """Return the run directory a socket lives in, for the child environment."""
    parent = Path(socket).parent
    # <root>/tmux-<uid>/default -> <root>
    return parent.parent


def server_alive(socket: Path) -> bool:
    """Report whether a server is listening on this run's socket."""
    return tmux(socket, "list-sessions").returncode == 0


def kill_server(socket: Path) -> list[str]:
    """Stop the server on this run's socket, reporting a survivor.

    This is the only sanctioned way for a harness to stop a tmux server: the
    socket names it, so a run can only ever stop the server it started. Running
    it against a socket nobody listens on is not an error -- a run whose server
    already exited has nothing to stop.
    """
    # The parent directory is the gate: tmux creates the socket when it starts
    # a server, so a socket whose directory is gone names no server, and asking
    # tmux to stop it anyway would make tmux start one purely in order to kill
    # it.
    if not Path(socket).parent.is_dir():
        return []
    tmux(socket, "kill-server")
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not server_alive(socket):
            return []
        time.sleep(0.1)
    return [f"tmux server on socket {socket} survived kill-server"]


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "MAX_SOCKET_PATH_BYTES",
    "SHORT_ROOT_BASE",
    "SHORT_ROOT_PREFIX",
    "SOCKET_NAME",
    "STOP_TIMEOUT_SECONDS",
    "TmuxError",
    "child_env",
    "ensure",
    "kill_server",
    "server_alive",
    "short_root",
    "socket_path",
    "tmux",
]
