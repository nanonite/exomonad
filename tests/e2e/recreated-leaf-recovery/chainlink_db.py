"""The disposable Chainlink database for the #1111 acceptance.

The acceptance seeds the issues its scenario needs into a database created
inside the disposable project. The operator's own database is never read or
written: the server and the controller both receive this path through the
``CHAINLINK_DB`` the run exports, and the file is removed with the project.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

#: Every issue the scenario is seeded with. Each is created fresh in the
#: disposable database on every run, so no run depends on another's rows.
SEED_ISSUES: tuple[dict[str, Any], ...] = (
    {
        "title": "Prove recreated leaf recovery end to end",
        "priority": "high",
        "labels": ("bug",),
    },
    {
        "title": "Preserve the recreated leaf branch across the session boundary",
        "priority": "high",
        "labels": ("bug",),
    },
)


class ChainlinkError(RuntimeError):
    """Raised when the disposable Chainlink database cannot be prepared."""


def _chainlink(*arguments: str, database: Path) -> str:
    """Run one chainlink command against the disposable database only."""
    result = subprocess.run(
        ["chainlink", *arguments],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "CHAINLINK_DB": str(database)},
    )
    if result.returncode:
        raise ChainlinkError(
            f"chainlink {' '.join(arguments)} failed ({result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def create(root: Path) -> Path:
    """Create the run's Chainlink database inside the disposable project."""
    project = root / "chainlink-project"
    project.mkdir(parents=True, exist_ok=True)
    database = project / "issues.db"
    _chainlink("init", database=database)
    return database


def seed(database: Path, issues: Sequence[dict[str, Any]] = SEED_ISSUES) -> tuple[int, ...]:
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
    """Return every file the run's Chainlink database owns, for cleanup checks."""
    if not database.parent.is_dir():
        return []
    return sorted(path for path in database.parent.rglob("*") if path.is_file())


def remove(database: Path) -> None:
    """Remove the disposable database's project directory."""
    shutil.rmtree(database.parent, ignore_errors=True)
