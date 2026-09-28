"""Run-scoped ownership and teardown for the #1111 acceptance.

Every resource the acceptance creates is registered here, and one trap removes
all of them. The teardown is idempotent, so running it twice, or running it
after a step already cleaned up, is not an error. After teardown the scope is
asked what it can still see; anything left is a leak, and a leak fails the run
rather than being reported as a warning.

The four kinds of resource this covers are the four an integration test leaks:

* tmux sessions, named from the run id so a session is attributable
* processes, tracked by pid and killed by process group
* docker compose projects, with their project-scoped volumes
* the run's temporary directory, created with ``mktemp -d`` and never a fixed
  path, so two runs cannot collide and cleanup can name it exactly
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: Every run-scoped name begins with this, so a leak is attributable to a
#: harness without knowing which harness started it.
LEAK_PREFIX = "exo-e2e-1111-"

#: Bounded wait for a terminated process to disappear before it is reported.
STOP_TIMEOUT_SECONDS = 15.0

#: Bounded wait for tmux to drop a session.
TMUX_TIMEOUT_SECONDS = 15.0

#: Bounded wait for a compose project to be removed.
COMPOSE_TIMEOUT_SECONDS = 120.0


class CleanupError(RuntimeError):
    """Raised when a resource could not be removed or is still present."""


@dataclass
class RunScope:
    """Every resource one acceptance run owns, and how to give them all back."""

    run_id: str
    root: Path
    sessions: set[str] = field(default_factory=set)
    processes: dict[int, str] = field(default_factory=dict)
    compose_projects: set[str] = field(default_factory=set)
    compose_files: dict[str, Path] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    released: bool = False
    #: When set, the run is being kept for inspection and the trap leaves
    #: everything alone. The trap runs on every exit path, so an operator
    #: keeping a failed run needs the flag to reach the teardown too.
    keep: bool = False

    @property
    def session_prefix(self) -> str:
        """Return the tmux session prefix this run owns exclusively."""
        return f"{LEAK_PREFIX}{self.run_id}-"

    def track_session(self, session: str) -> str:
        """Record a tmux session this run created."""
        if not session.startswith(self.session_prefix):
            raise CleanupError(
                f"refusing to track session {session!r}: it does not begin with "
                f"this run's prefix {self.session_prefix!r}"
            )
        self.sessions.add(session)
        return session

    def track_process(self, process: Any, label: str) -> Any:
        """Record a process this run started, killing its group on teardown."""
        self.processes[int(process.pid)] = label
        return process

    def track_compose(self, project: str, compose_file: Path) -> str:
        """Record a compose project this run brought up."""
        if not project.startswith(self.session_prefix):
            raise CleanupError(
                f"refusing to track compose project {project!r}: it does not "
                f"begin with this run's prefix {self.session_prefix!r}"
            )
        self.compose_projects.add(project)
        self.compose_files[project] = compose_file
        return project

    def teardown(self) -> list[str]:
        """Remove everything this run created. Safe to call more than once."""
        if self.released or self.keep:
            return list(self.problems)
        self.released = True
        for session in sorted(self.sessions):
            self.problems.extend(_kill_session(session))
        self.sessions.clear()
        for pid, label in sorted(self.processes.items()):
            self.problems.extend(_kill_process(pid, label))
        self.processes.clear()
        for project in sorted(self.compose_projects):
            self.problems.extend(
                _compose(project, self.compose_files[project], "down", "-v", "--remove-orphans")
            )
        self.compose_projects.clear()
        shutil.rmtree(self.root, ignore_errors=True)
        if self.root.exists():
            self.problems.append(f"run directory still exists: {self.root}")
        return list(self.problems)

    def leaks(self) -> list[str]:
        """Return everything from this run that is still visible on the host.

        This is checked after teardown, so it reports what cleanup failed to
        remove rather than what the run was doing.
        """
        if self.keep:
            return []
        found: list[str] = []
        found.extend(_sessions_with_prefix(self.session_prefix))
        found.extend(_processes_under(self.root))
        found.extend(_compose_projects_with_prefix(self.session_prefix))
        found.extend(_volumes_with_prefix(self.session_prefix))
        if self.root.exists():
            found.append(f"run directory survived: {self.root}")
        return found

    def require_clean(self) -> None:
        """Fail the run when anything this run created is still present."""
        found = self.leaks()
        if found:
            raise CleanupError(
                "the acceptance run leaked resources: " + "; ".join(sorted(found))
            )


def install_trap(scope: RunScope) -> Callable[[], None]:
    """Install one handler for EXIT, INT, and TERM that always tears the run down.

    Returning the handler makes it directly testable: the contract check starts
    a real session, process, and compose project through a scope, then calls
    this and asserts nothing is left.
    """
    state = {"signalled": None}

    def handler(signum: int = 0, _frame: Any = None) -> None:
        if state["signalled"] is not None:
            # A second signal means the operator is insisting; do not re-enter.
            return
        state["signalled"] = signum
        scope.teardown()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handler)
    import atexit

    atexit.register(handler)
    return handler


def _kill_session(session: str) -> list[str]:
    """Kill one named tmux session and report whether it is gone."""
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        check=False,
        capture_output=True,
        text=True,
    )
    deadline = time.monotonic() + TMUX_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _session_exists(session):
            return []
        time.sleep(0.1)
    return [f"tmux session survived cleanup: {session}"]


def _session_exists(session: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", session],
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _kill_process(pid: int, label: str) -> list[str]:
    """Kill one process and report whether it is gone.

    The signal goes to the process's own group only when that group is not this
    process's group. A child that inherited this process's group would
    otherwise make the group signal a self-inflicted kill, which would take the
    teardown down with the very thing it was cleaning up.
    """
    own_group = os.getpgrp()
    for signum in (signal.SIGTERM, signal.SIGKILL):
        if not _process_alive(pid):
            return []
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            return []
        try:
            if group != own_group:
                os.killpg(group, signum)
            else:
                os.kill(pid, signum)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                return []
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                return []
            time.sleep(0.1)
    return [f"{label} process {pid} survived cleanup"]


def _process_alive(pid: int) -> bool:
    """Report whether a process is still running.

    A process that has exited but has not been reaped keeps its ``/proc``
    entry, so presence alone would report a finished server as a survivor and
    fail every run. The process state is read for that reason: ``Z`` is a
    zombie, which is a process that is already gone.
    """
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _, _, remainder = status.rpartition(") ")
    if not remainder:
        return False
    return remainder.split(" ", 1)[0] not in {"Z", "X", "x"}


def _processes_under(root: Path) -> list[str]:
    """Return every live process whose working directory is under ``root``.

    A process that outlives its run and still points into the run's temporary
    directory is the leak this acceptance is required to prove cannot happen,
    so it is detected by the filesystem rather than by a name prefix.
    """
    found: list[str] = []
    resolved = str(root.resolve()) if root.exists() else str(root)
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except (OSError, PermissionError):
            continue
        if cwd == resolved or cwd.startswith(resolved.rstrip("/") + "/"):
            command = _command_line(entry)
            found.append(f"process {entry.name} still runs in {cwd}: {command}")
    return found


def _command_line(entry: Path) -> str:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return "<unreadable>"
    return " ".join(part for part in raw.decode("utf-8", "replace").split("\0") if part)


def _sessions_with_prefix(prefix: str) -> list[str]:
    """Return every live tmux session carrying the run's prefix."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return []
    return [
        f"tmux session survived cleanup: {name}"
        for name in result.stdout.split()
        if name.startswith(prefix)
    ]


def _compose_projects_with_prefix(prefix: str) -> list[str]:
    """Return every live compose project carrying the run's prefix."""
    result = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return [f"could not list compose projects: {result.stderr.strip()}"]
    import json

    try:
        projects = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [f"compose project listing was not JSON: {result.stdout.strip()!r}"]
    return [
        f"compose project survived cleanup: {entry.get('Name')}"
        for entry in projects
        if isinstance(entry, dict) and str(entry.get("Name", "")).startswith(prefix)
    ]


def _volumes_with_prefix(prefix: str) -> list[str]:
    """Return every docker volume carrying the run's prefix."""
    result = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return [f"could not list docker volumes: {result.stderr.strip()}"]
    return [
        f"docker volume survived cleanup: {name}"
        for name in result.stdout.split()
        if name.startswith(prefix)
    ]


def _compose(project: str, compose_file: Path, *arguments: str) -> list[str]:
    """Run one compose command for a project and report a refusal."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(compose_file),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=COMPOSE_TIMEOUT_SECONDS,
    )
    if result.returncode:
        return [
            f"docker compose {' '.join(arguments)} failed for {project}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        ]
    return []


def make_root(prefix: str) -> Path:
    """Create this run's temporary directory with ``mktemp -d``.

    A fixed path would let two runs share a project, would survive a crash into
    the next run, and would make a leak indistinguishable from ordinary state.
    """
    result = subprocess.run(
        ["mktemp", "-d", f"{prefix.rstrip('/')}/exomonad-e2e-1111-XXXXXXXX"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise CleanupError(f"could not create a run directory: {result.stderr.strip()}")
    return Path(result.stdout.strip())


__all__ = [
    "COMPOSE_TIMEOUT_SECONDS",
    "CleanupError",
    "LEAK_PREFIX",
    "RunScope",
    "install_trap",
    "make_root",
]
