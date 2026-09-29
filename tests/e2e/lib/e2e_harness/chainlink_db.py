"""The disposable Chainlink database an acceptance seeds for itself.

The acceptance creates a fresh database inside its own disposable project with
``chainlink init`` and seeds exactly the issue state the scenario needs. No
existing database is copied or read, and the operator's own database is never
touched.

Where it is created is not free. ``exomonad init`` anchors ``CHAINLINK_DB`` to
``<project>/.chainlink`` for every window it starts, and ``build_spawn_env``
does the same for every spawned agent, so a database anywhere else is one the
controller never writes an escalation to -- and, if that directory does not
exist, one whose absence kills the controller mid-park. An acceptance whose
controller runs through ``exomonad init`` therefore creates its database at
``database_path_for(project)``, which is the path the shipped binary resolves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


class ChainlinkError(RuntimeError):
    """Raised when the disposable Chainlink database cannot be prepared."""


def _chainlink(
    *arguments: str, database: Path, cwd: Path | None = None
) -> str:
    """Run one chainlink command against the disposable database only."""
    result = subprocess.run(
        ["chainlink", *arguments],
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        env={**os.environ, "CHAINLINK_DB": str(database)},
    )
    if result.returncode:
        raise ChainlinkError(
            f"chainlink {' '.join(arguments)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def database_path_for(project: Path) -> Path:
    """The database the shipped controller resolves for one project.

    ``exomonad init`` sets ``CHAINLINK_DB`` to ``<project>/.chainlink`` for the
    session it starts, and ``build_spawn_env`` sets the same value for every
    agent it spawns: the project-root database is the canonical one. An
    acceptance that keeps its database anywhere else reads a file its own
    escalations never touch.
    """
    return project / ".chainlink" / "issues.db"


def create(root: Path, *, project_dir: Path | None = None) -> Path:
    """Create the run's Chainlink database inside the disposable project.

    ``chainlink init`` is run *inside* the project directory it creates, so
    everything it writes -- the rules it seeds and the database itself -- lands
    under the run's own directory and disappears with it. Running it anywhere
    else would let a first run initialize a database in a directory that outlives
    the run, which is exactly the shared state this layout exists to avoid.

    ``project_dir`` names the project the database belongs to; it defaults to
    the run's own chainlink project directory, and an acceptance whose
    controller resolves ``<project>/.chainlink`` passes its repository instead
    (see :func:`database_path_for`).
    """
    project = project_dir if project_dir is not None else project_dir_for(root)
    project.mkdir(parents=True, exist_ok=True)
    database = database_path_for(project)
    _chainlink(
        "init",
        "--no-hooks",
        "--db",
        str(database),
        database=database,
        cwd=project,
    )
    return database


def project_dir_for(root: Path) -> Path:
    """Return the disposable Chainlink project directory for a run directory."""
    return root / "chainlink-project"


def project_dir(database: Path) -> Path:
    """Return the project directory that owns a database created by ``create``.

    The layout is ``<run>/chainlink-project/.chainlink/issues.db``, so the
    database's grandparent is the directory the whole disposable project lives
    in. Cleanup and leak checks operate on that directory, because a project
    that removes only the database file would leave the seeded rules behind.
    """
    return database.parents[1]


def seed(
    database: Path, issues: Sequence[Mapping[str, Any]]
) -> tuple[int, ...]:
    """Seed exactly the issues the scenario needs, and return their ids.

    Seeding happens only in the disposable database. No issue in any other
    database is created, read, or modified by this harness.
    """
    identifiers: list[int] = []
    for issue in issues:
        arguments = [
            "quick",
            str(issue["title"]),
            "-p",
            str(issue["priority"]),
        ]
        for label in issue.get("labels", ()):
            arguments.extend(["-l", str(label)])
        output = _chainlink(*arguments, database=database)
        identifier = _issue_id(output)
        if identifier is None:
            raise ChainlinkError(
                f"could not read the seeded issue id from chainlink output: {output!r}"
            )
        identifiers.append(identifier)
    return tuple(identifiers)


def _issue_id(output: str) -> int | None:
    """Read the id chainlink reported for a created issue."""
    for line in output.splitlines():
        stripped = line.strip()
        for prefix in ("Created issue #", "Issue #", "#"):
            if stripped.startswith(prefix):
                digits = stripped[len(prefix) :].split()[0].strip(":,.")
                if digits.isdigit():
                    return int(digits)
    return None


def issue_state(database: Path, issue_id: int) -> dict[str, Any]:
    """Return one seeded issue exactly as the disposable database holds it."""
    output = _chainlink("issue", "show", str(issue_id), "--json", database=database)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ChainlinkError(
            f"chainlink issue show did not answer with JSON: {output!r}"
        ) from error
    if not isinstance(value, dict):
        raise ChainlinkError(f"seeded issue is not an object: {value!r}")
    return value


def database_files(database: Path) -> list[Path]:
    """Return every file the run's Chainlink project owns, for cleanup checks."""
    root = project_dir(database)
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def remove(database: Path) -> None:
    """Remove the disposable Chainlink project directory."""
    shutil.rmtree(project_dir(database), ignore_errors=True)


__all__ = [
    "ChainlinkError",
    "create",
    "database_files",
    "issue_state",
    "project_dir",
    "database_path_for",
    "project_dir_for",
    "remove",
    "seed",
]
