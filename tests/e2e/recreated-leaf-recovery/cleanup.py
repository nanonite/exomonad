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
from typing import Any, Callable, Sequence, Sequence

#: Every run-scoped name begins with this, so a leak is attributable to a
#: harness without knowing which harness started it.
LEAK_PREFIX = "exo-e2e-1111-"

#: The prefix of the run directory each run is given by ``mktemp -d``. A sweep
#: looks for this so it can reclaim a run that was killed before its teardown.
RUN_DIRECTORY_PREFIX = "exomonad-e2e-1111-"

#: Where those run directories are created.
TEMP_ROOT = "/tmp"

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

    A process whose directory has already been removed still reports it, with
    ``(deleted)`` appended by the kernel, and that is the case that matters
    most: the run directory is gone, so nothing else would notice the process.
    """
    found: list[str] = []
    for pid, cwd, command in _live_processes():
        if _is_within(cwd, [str(root)]):
            found.append(f"process {pid} still runs in {cwd}: {command}")
    return found


def _is_within(candidate: str, roots: Sequence[str]) -> bool:
    """Report whether a path is one of the roots or inside one of them."""
    for root in roots:
        if not root:
            continue
        if candidate == root or candidate.startswith(root.rstrip("/") + "/"):
            return True
    return False


def _live_processes() -> list[tuple[str, str, str]]:
    """Return ``(pid, working directory, command line)`` for every live process.

    The working directory has the kernel's ``(deleted)`` suffix stripped, so a
    process running in a directory that no longer exists is still matched by the
    path it was in when the run was torn down.
    """
    found: list[tuple[str, str, str]] = []
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            raw = os.readlink(entry / "cwd")
        except (OSError, PermissionError):
            continue
        if raw.endswith(" (deleted)"):
            raw = raw[: -len(" (deleted)")]
        found.append((entry.name, raw, _command_line(entry)))
    return found


def processes_in_scopes(roots: Sequence[str]) -> list[tuple[str, str, str]]:
    """Return every live process running in, or naming, one of ``roots``.

    Both signals matter. A server whose directory was already removed still
    reports that directory through ``/proc``, but a process that was started
    with a path argument rather than a working directory only names the run's
    directory on its command line.
    """
    return [
        (pid, cwd, command)
        for pid, cwd, command in _live_processes()
        if _is_within(cwd, roots) or any(root and root in command for root in roots)
    ]


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
    return [
        f"compose project survived cleanup: {name}"
        for name in _compose_projects()
        if name.startswith(prefix)
    ]


def _compose_projects() -> list[str]:
    """Return the name of every compose project Docker knows about."""
    result = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return []
    import json

    try:
        projects = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        str(entry.get("Name"))
        for entry in projects
        if isinstance(entry, dict) and entry.get("Name")
    ]


def stale_run_roots(run_prefix: str) -> list[str]:
    """Return every run directory this harness has ever named, live or not.

    A directory whose run was killed before its teardown is still on disk, and a
    process whose directory was already removed still reports that path, so a
    sweep has to look for both: the directories that exist, and the paths that
    live processes still claim.
    """
    roots = [
        str(path)
        for path in Path(TEMP_ROOT).glob(f"{RUN_DIRECTORY_PREFIX}*")
        if path.is_dir()
    ]
    for _pid, cwd, command in _live_processes():
        for candidate in (cwd, *command.split()):
            if candidate.startswith(str(Path(TEMP_ROOT) / RUN_DIRECTORY_PREFIX)):
                if candidate not in roots:
                    roots.append(candidate)
    return roots


def sweep_stale(run_prefix: str, compose_file: Path) -> list[str]:
    """Remove everything a previous, interrupted run of this harness left behind.

    Teardown is the normal path, but it cannot run when the harness is killed
    from outside: a SIGKILL, a host reboot, or a Docker restart takes the trap
    with it. This runs at the start of every run and reclaims what such a run
    left, so an interrupted run cannot accumulate processes, sessions, compose
    projects, or directories that outlive it.

    It is deliberately prefix-driven rather than known-run-driven, because the
    thing being reclaimed is by definition a run this harness does not have a
    record of.
    """
    problems: list[str] = []
    roots = stale_run_roots(run_prefix)
    for pid, cwd, command in processes_in_scopes(roots):
        problems.extend(_kill_process(int(pid), f"stale process in {cwd} ({command})"))
    for name in _sessions_with_prefix(run_prefix):
        session = name.split(": ", 1)[-1]
        problems.extend(_kill_session(session))
    for project in _compose_projects():
        if project.startswith(run_prefix):
            problems.extend(_compose(project, compose_file, "down", "-v", "--remove-orphans"))
    for name in _volumes_with_prefix(run_prefix):
        problems.extend(_remove_volume(name.split(": ", 1)[-1]))
    for root in roots:
        if Path(root).is_dir():
            shutil.rmtree(root, ignore_errors=True)
            if Path(root).is_dir():
                problems.append(f"stale run directory survived the sweep: {root}")
    return problems


def _remove_volume(name: str) -> list[str]:
    """Remove one docker volume, reporting a refusal rather than hiding it."""
    result = subprocess.run(
        ["docker", "volume", "rm", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode and "no such volume" not in result.stderr:
        return [f"could not remove docker volume {name}: {result.stderr.strip()}"]
    return []


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
    "RUN_DIRECTORY_PREFIX",
    "RunScope",
    "install_trap",
    "make_root",
    "processes_in_scopes",
    "stale_run_roots",
    "sweep_stale",
]
